"""HarmonyOS 镜像 —— P3-A：hmdriver2 截图轮询 → JPEG passthrough。

**设计选择**：复用 iOS ``mjpeg_passthrough`` 的链路（``MSG_MIRROR_JPEG`` 协议 +
前端 ``useJpegMirror``）。每帧独立 JPEG 送给浏览器 ``<img>`` 绘制，旋转 / 分辨率
变化天然自适应，零帧间状态。

**对齐 Android / iOS 的接口**（供 ``_HarmonyMirrorSession`` 调用，签名和
``IosMjpegPassthroughStreamer`` 几乎一致）：

- ``start()`` 起后台线程开始推帧
- ``stop()`` 优雅停
- ``is_alive`` property 反映线程状态
- ``restart()`` 留空（本 streamer 无状态，start/stop 就够）

**性能边界**：
- hmdriver2 screenshot 底层 = ``hdc shell snapshot_display -f ...`` + ``hdc file recv``
- 单帧 200-400ms（USB 2.0 + JPEG 编码 + hdc 往返），**绝对上限 ~10fps**
- CPU 占用主要在 Pillow 重压 + base64；Mac 侧 <10% 单设备

**未来 P3-B**：用 HOScrcpy WebSocket 协议的 Python client 替换整个 streamer，
拿到 raw H.264 NAL units 后**喂给 ``FMp4Streamer``**（和 Android scrcpy 同一套
fmp4 链路），帧率 60fps / 延迟 <100ms。届时本文件改造为可选 backend，本类作兜底。
"""
from __future__ import annotations

import io
import threading
import time
from typing import Any, Callable, Optional, Tuple

from PIL import Image
from loguru import logger


class HarmonyScreenshotStreamer:
    """鸿蒙截图轮询流。和 ``IosMjpegPassthroughStreamer`` 同级。

    Args:
        serial: 设备 udid，用于日志
        driver: 已就绪的 ``HarmonyDriver`` 实例（本类不自己 new driver，
            让上层 ``_HarmonyMirrorSession`` 统一管 ``_driver_cache``，
            避免 hmdriver2 singleton 被从两个地方争用）
        on_jpeg: 每帧回调，签名 ``(jpeg_bytes, width, height) -> None``
        target_fps: 目标帧率。实测 10fps 是 hmdriver2 的上限；>10 会退化成
            尽力而为，实际 fps 受 hdc 往返耗时限制。
        jpeg_quality: 重压质量 1-100。设备端 snapshot_display 原始 ~80Q，
            过网前重压到 50-60 减一半带宽、VLM / 人眼都无感。
        long_edge: 最长边降采样阈值。0 或 -1 = 不降采样原分辨率直送。
        log_tag: 日志前缀，便于多设备并存时区分。
    """

    def __init__(
        self,
        serial: str,
        driver: Any,  # HarmonyDriver，类型注解避免 circular import
        on_jpeg: Callable[[bytes, int, int], None],
        *,
        target_fps: int = 8,
        jpeg_quality: int = 55,
        long_edge: int = 720,
        log_tag: str = "hm-shot",
    ) -> None:
        self._serial = serial
        self._driver = driver
        self._on_jpeg = on_jpeg
        self._target_fps = max(1, min(30, int(target_fps)))
        self._jpeg_quality = max(10, min(100, int(jpeg_quality)))
        self._long_edge = max(0, int(long_edge))
        self._log_tag = log_tag

        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        # 上一帧尺寸，变化时主动 log 方便排查旋转/分辨率切换
        self._last_size: Optional[Tuple[int, int]] = None

    # ------------------------------------------------------------------
    # 生命周期（接口对齐 IosScreenStreamer / IosMjpegStreamer / scrcpy-server）
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopped = False
        self._thread = threading.Thread(
            target=self._pump_loop,
            name=f"harmony-mirror-{self._serial[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[{}] 启动截图轮询 target_fps={} quality={} long_edge={}",
            self._log_tag, self._target_fps, self._jpeg_quality, self._long_edge,
        )

    def stop(self) -> None:
        self._stopped = True
        t = self._thread
        if t is not None and t.is_alive():
            # 不 join 让主线程等，setDaemon=True 的线程在进程结束时自动回收；
            # stop() 要尽快返回以免 web 重启 mirror 卡住
            pass
        self._thread = None
        logger.info("[{}] 停止截图轮询", self._log_tag)

    def restart(self) -> None:
        """占位。本 streamer 无编码状态，不需要像 FMp4Streamer 那样 restart。"""
        pass

    @property
    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stopped
        )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _pump_loop(self) -> None:
        interval = 1.0 / float(self._target_fps)
        # 统计窗口，每 30 帧打一行 fps / 平均耗时
        stat_n = 30
        stat_count = 0
        stat_total_ms = 0.0
        stat_window_start = time.monotonic()

        while not self._stopped:
            frame_start = time.monotonic()
            try:
                jpeg = self._grab_frame()
            except Exception as exc:  # noqa: BLE001
                # 单帧失败不中断循环；连错 10 次停，让上层看 is_alive=False 回收
                logger.warning(
                    "[{}] 单帧抓取失败: {}", self._log_tag, exc,
                )
                # 简单兜底：sleep 更久让设备/uitest 喘口气
                time.sleep(min(2.0, interval * 3))
                continue

            if jpeg is None:
                time.sleep(interval)
                continue

            w, h = self._peek_size(jpeg)
            if (w, h) != self._last_size and self._last_size is not None:
                logger.info(
                    "[{}] 画面尺寸变化 {} → {}（旋转/分辨率切换）",
                    self._log_tag, self._last_size, (w, h),
                )
            self._last_size = (w, h)

            try:
                self._on_jpeg(jpeg, w, h)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[{}] on_jpeg 回调异常: {}", self._log_tag, exc)

            elapsed = time.monotonic() - frame_start
            stat_count += 1
            stat_total_ms += elapsed * 1000.0
            if stat_count >= stat_n:
                window = time.monotonic() - stat_window_start
                actual_fps = stat_count / max(0.001, window)
                avg_ms = stat_total_ms / stat_count
                logger.info(
                    "[{}] mirror stat: 实际 {:.1f}fps (target≤{}fps) 单帧平均 {:.1f}ms 输出 {} 张",
                    self._log_tag, actual_fps, self._target_fps, avg_ms, stat_count,
                )
                stat_count = 0
                stat_total_ms = 0.0
                stat_window_start = time.monotonic()

            # 简单节流：如果单帧已经超过 interval（hdc 慢），立刻进下一帧；否则 sleep 补齐
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    # ------------------------------------------------------------------
    # 帧抓取 & 重压
    # ------------------------------------------------------------------
    def _grab_frame(self) -> Optional[bytes]:
        """抓一帧 JPEG。委托给 HarmonyDriver.screenshot_jpeg 做缩放 + 重压。

        返空意味着驱动临时不可用（大概率是 uitest daemon 抽风），调用方会 sleep 重试。
        """
        try:
            return self._driver.screenshot_jpeg(
                quality=self._jpeg_quality,
                max_side=self._long_edge if self._long_edge > 0 else None,
            )
        except Exception as exc:  # noqa: BLE001
            # 非 RuntimeError 记 debug；RuntimeError 冒泡让 _pump_loop 统一处理
            logger.debug("[{}] screenshot_jpeg 异常: {}", self._log_tag, exc)
            raise

    @staticmethod
    def _peek_size(jpeg: bytes) -> Tuple[int, int]:
        """从 JPEG 字节拿尺寸。Pillow 惰性解析（不真的 decode 像素），很便宜。

        拿不到返回 (0, 0)；前端 ``<img>`` 会在 load 时自己读 naturalWidth/Height，
        这里是 best-effort，只为 server 日志里看见画面分辨率方便排障。
        """
        try:
            img = Image.open(io.BytesIO(jpeg))
            return int(img.size[0]), int(img.size[1])
        except Exception:  # noqa: BLE001
            return 0, 0


__all__ = [
    "HarmonyScreenshotStreamer",
]
