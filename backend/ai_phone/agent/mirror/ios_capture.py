"""iOS 镜像通道：pymobiledevice3 DVT 截图轮询 → ffmpeg image2pipe → fmp4 → MSE。

为什么不直接走 H.264：
- iOS 真正的 "screen mirroring (H.264 流)" 走 RemoteXPC + DVT 上的
  ``DTScreenCaptureService``，比单帧截图复杂，本期先用截图轮询。M4 阶段考虑替换
- DVT ``Screenshot.get_screenshot()`` 单张 ~80~150ms，可以做到 ~8-10fps，
  对手动调试和 VLM 都够用，且实现复杂度极低
- 通过 ``ffmpeg -f image2pipe`` 把这一帧帧 PNG 编码成 H.264 → fmp4 → MSE，
  **浏览器侧完全无感知**（前端不需要按平台分支）

iOS 17+ 必走 RSD（关键）：
- 老的 ``com.apple.mobile.screenshotr`` lockdown service 在 iOS 17+ 已被 Apple 移除
- 唯一可用的截图通道是 DVT 上的 ``Screenshot``，DVT 必须通过 RemoteXPC（tunneld）
  连接的 ``RemoteServiceDiscoveryService``（RSD）拿，**不能用 LockdownClient**
- 因此本类的依赖：mac 上必须先跑 ``sudo pymobiledevice3 remote tunneld``，
  ``pymobiledevice3 mounter auto-mount`` 挂 DDI；细节见 README『iOS 接入』

帧率 / 延迟取舍：
- 端到端延迟 ≈ 截图(120ms) + ffmpeg encode(20ms) + fmp4 frag(50ms) + MSE(400ms)
  ≈ 600ms，比 Android 的 200ms 慢，但对"看着跑用例"的场景 OK

设备旋转：
- 每张截图的 PIL.Image 直接读 size，发现 W/H 互换就调 ``FMp4Streamer.restart()``，
  逻辑和 Android 那边等价，前端 useMseMirror 一样能识别 init segment 尺寸变化
"""
from __future__ import annotations

import io
import threading
import time
from typing import Callable, Optional, Tuple

from loguru import logger

from .fmp4 import FMp4Streamer


# pmd3 ScreenCaptureService 的"理论上限"取决于设备 + USB；超过这个值毫无意义
_MIN_INTERVAL_S = 1.0 / 30
# 也别逼太紧，10fps 是甜点
_DEFAULT_TARGET_FPS = 10

# 喂给 ffmpeg 之前的最长边降采样阈值（像素）。iPhone 16 Pro Max 是 1290×2796，
# 单张 PNG ~5MB，PNG 解码 + 5MB 数据 H.264 编码会把 ffmpeg 压到 2-3fps，端到端
# 延迟 3s+。降到 720（超采样到 720 长边）后单张 JPEG ~80-200KB，ffmpeg 就能跑
# 满 8-10fps，端到端延迟降到 ~600ms
_DOWNSCALE_LONG_EDGE = 720
# 重编码成 JPEG 的质量。85 在视觉上接近原图，体积比 PNG 小 25x+
_JPEG_QUALITY = 80


class IosScreenStreamer:
    """单台 iOS 设备的截图 → fmp4 推流器。

    生命周期：
        s = IosScreenStreamer(lockdown, on_init, on_segment, ...)
        s.start()      # 启动后台抓帧线程 + ffmpeg 子进程
        ...
        s.stop()       # 关线程 + ffmpeg

    线程模型：
        - 后台 daemon 线程 ``_capture_loop`` 阻塞调用 pmd3 ScreenCaptureService.get_screenshot()
        - 拿到一张 PNG 字节 → 立刻 ``ffmpeg.feed(png_bytes)``
        - ffmpeg 自己的 stdout reader 线程产 init / media segment，回调到 on_init / on_segment
    """

    def __init__(
        self,
        serial: str,
        on_init: Callable[[bytes], None],
        on_segment: Callable[[bytes], None],
        target_fps: int = _DEFAULT_TARGET_FPS,
        frag_ms: int = 50,
        gop_sec: int = 1,
        log_tag: str = "ios-fmp4",
    ) -> None:
        self._serial = serial
        self._target_fps = max(1, min(30, int(target_fps)))
        self._interval_s = max(_MIN_INTERVAL_S, 1.0 / self._target_fps)
        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._on_init = on_init
        self._on_segment = on_segment
        self._log_tag = log_tag

        self._fmp4 = FMp4Streamer(
            on_init=on_init,
            on_segment=on_segment,
            framerate=self._target_fps,
            frag_ms=frag_ms,
            gop_sec=gop_sec,
            log_tag=log_tag,
            input_args=[
                # 关键：让 ffmpeg 按真实墙钟时间标 PTS。如果用 -framerate=N 让
                # demuxer 按 1/N 秒推进 PTS，但实际我们 feed 不到 N fps，浏览器
                # buffered.end 推进慢于 wall-clock → MSE 实时同步阈值永不触发
                # → 延迟越堆越多（实测能堆到 3s）。用 wall-clock 后 PTS 完全
                # 跟手指动作时间对齐，buffer 不会虚长。重编码路径（libx264）
                # 不会触发 fmp4.py 里 -c:v copy + wallclock 的退出问题
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                "image2pipe",
                "-framerate",
                str(self._target_fps),
                "-i",
                "pipe:0",
            ],
        )

        self._last_size: Optional[Tuple[int, int]] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"ios-cap-{self._log_tag}",
        )
        self._thread.start()
        logger.info("[{}] iOS 截图推流已启动 fps≤{}", self._log_tag, self._target_fps)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self._fmp4.stop()
        except Exception:  # noqa: BLE001
            pass
        # 抓帧线程是 daemon，不显式 join；如果用户连续 start/stop，下一轮 start 会重建

    def restart(self) -> None:
        """强制 ffmpeg 重启（如旋转变更）；自身抓帧线程不动。"""
        try:
            self._fmp4.restart()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] fmp4 restart 失败: {}", self._log_tag, exc)
        self._last_size = None

    @property
    def is_alive(self) -> bool:
        return not self._stopped and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    def _capture_loop(self) -> None:
        # iOS 17+ 截图必须走 DVT (via RSD via tunneld)。lockdown 直连已被 Apple 移除。
        # pmd3 9.x 大改造：
        #   1. ``DvtSecureSocketProxyService`` 模块整个被删，新入口是
        #      ``DvtProvider``（在 ``services.dvt.instruments.dvt_provider``）
        #   2. ``tunneld`` 顶层包直接 export ``get_tunneld_device_by_udid`` 且改成
        #      async；不再走 ``.api`` 子模块
        #   3. ``DvtProvider.connect`` / ``Screenshot.connect`` / ``get_screenshot``
        #      全部 async，必须丢到 ``ios.py::_PMD3_LOOP`` 那个长寿命 loop 里 await，
        #      否则 stateful socket 会"绑在不同 loop"上炸
        try:
            from pymobiledevice3.tunneld.api import (  # noqa: PLC0415
                get_tunneld_device_by_udid,
            )
            from pymobiledevice3.services.dvt.instruments.dvt_provider import (  # noqa: PLC0415
                DvtProvider,
            )
            from pymobiledevice3.services.dvt.instruments.screenshot import (  # noqa: PLC0415
                Screenshot,
            )
            # 借用 ios.py 的 sync→pmd3-loop 桥
            from ai_phone.agent.drivers.ios import _maybe_sync  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[{}] 没装 pymobiledevice3 / 找不到 DVT Screenshot: {}；"
                "iOS 镜像不可用。pip install -e \".[ios]\" 后重启 agent 再试。",
                self._log_tag, exc,
            )
            self._stopped = True
            return

        rsd = None
        try:
            rsd = _maybe_sync(get_tunneld_device_by_udid(self._serial))
            if rsd is None:
                logger.error(
                    "[{}] tunneld 没有这个 udid={}（tunneld 是否在跑？"
                    "终端跑 `sudo pymobiledevice3 remote tunneld` 后重试）",
                    self._log_tag, self._serial,
                )
                self._stopped = True
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[{}] 连接 tunneld 失败: {}；"
                "请确保 mac 上跑了 `sudo pymobiledevice3 remote tunneld`",
                self._log_tag, exc,
            )
            self._stopped = True
            return

        # 起 DVT provider → Screenshot client；这是个长连接，全程复用。
        # 9.x 没了 sync ctx manager（``__enter__`` 直接 raise），改成显式
        # connect/close 配合 try/finally。
        provider = None
        shooter = None
        try:
            provider = DvtProvider(lockdown=rsd)
            _maybe_sync(provider.connect())
            shooter = Screenshot(provider)
            _maybe_sync(shooter.connect())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[{}] 创建 DVT Screenshot 失败: {}（DDI 是否挂上？"
                "终端跑 `pymobiledevice3 mounter auto-mount` 后重试）",
                self._log_tag, exc,
            )
            if provider is not None:
                try:
                    _maybe_sync(provider.close())
                except Exception:  # noqa: BLE001
                    pass
            self._stopped = True
            return

        consecutive_errors = 0
        next_deadline = time.monotonic()
        # 每 N 帧打一次实际帧率 + 分解耗时，便于定位瓶颈
        _stat_window = 30
        _stat_count = 0
        _stat_dvt_ms = 0.0
        _stat_pil_ms = 0.0
        _stat_total_start = time.monotonic()
        try:
            while not self._stopped:
                t0 = time.monotonic()
                if t0 < next_deadline:
                    time.sleep(next_deadline - t0)
                    t0 = time.monotonic()
                next_deadline = t0 + self._interval_s

                try:
                    _t_dvt0 = time.monotonic()
                    png = _maybe_sync(shooter.get_screenshot())
                    _stat_dvt_ms += (time.monotonic() - _t_dvt0) * 1000
                except Exception as exc:  # noqa: BLE001
                    consecutive_errors += 1
                    if consecutive_errors <= 3 or consecutive_errors % 30 == 0:
                        logger.warning(
                            "[{}] 截图失败 #{}: {}",
                            self._log_tag, consecutive_errors, exc,
                        )
                    if consecutive_errors >= 60:
                        logger.error(
                            "[{}] 连续 60 帧截图失败，停止 iOS 镜像", self._log_tag,
                        )
                        break
                    continue

                consecutive_errors = 0
                if not png:
                    continue

                # 关键路径：DVT 给的 PNG iOS 自己已经压缩过（实测 ~50KB），
                # 但它是 RGB48 + 1290x2796 全分辨率，对 ffmpeg PNG 解码 + libx264
                # 编码压力很大。我们 PIL resize 到 720 长边 + JPEG 重编一遍，让
                # ffmpeg 编 720x... 而不是原始 1290x2796，CPU 负载和帧延迟都降。
                # 同时也用 resize 后的尺寸做旋转检测——逻辑等价（W/H 比例不变）。
                _t_pil0 = time.monotonic()
                try:
                    frame_bytes, size = self._downscale_to_jpeg(png)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[{}] PIL 降采样失败，退回原 PNG: {}", self._log_tag, exc)
                    frame_bytes = png
                    size = self._peek_size(png)
                _stat_pil_ms += (time.monotonic() - _t_pil0) * 1000

                if size is not None and self._last_size is not None and size != self._last_size:
                    logger.info(
                        "[{}] 检测到截图尺寸变化 {}→{}，重启 ffmpeg",
                        self._log_tag, self._last_size, size,
                    )
                    self.restart()
                if size is not None:
                    self._last_size = size

                self._fmp4.feed(frame_bytes)
                _stat_count += 1
                if _stat_count >= _stat_window:
                    elapsed = time.monotonic() - _stat_total_start
                    actual_fps = _stat_count / elapsed if elapsed > 0 else 0
                    logger.info(
                        "[{}] capture stat: 实际 {:.1f}fps (target≤{}fps) "
                        "DVT 平均 {:.0f}ms PIL 平均 {:.0f}ms 输出 {} 张",
                        self._log_tag,
                        actual_fps,
                        self._target_fps,
                        _stat_dvt_ms / _stat_count,
                        _stat_pil_ms / _stat_count,
                        _stat_count,
                    )
                    _stat_count = 0
                    _stat_dvt_ms = 0.0
                    _stat_pil_ms = 0.0
                    _stat_total_start = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[{}] 抓帧循环异常退出: {}", self._log_tag, exc)
        finally:
            if shooter is not None:
                try:
                    _maybe_sync(shooter.close())
                except Exception:  # noqa: BLE001
                    pass
            if provider is not None:
                try:
                    _maybe_sync(provider.close())
                except Exception:  # noqa: BLE001
                    pass
            self._stopped = True
            logger.info("[{}] iOS 截图推流线程已退出", self._log_tag)

    @staticmethod
    def _peek_size(png_bytes: bytes) -> Optional[Tuple[int, int]]:
        """快速读 PNG / JPEG 尺寸，不解码全图（用 PIL 也行，但 PIL.open 是惰性的，
        ``Image.size`` 立刻可用，且支持 PNG/JPEG/HEIC 等所有格式）。
        """
        try:
            from PIL import Image  # 已是主依赖
            with Image.open(io.BytesIO(png_bytes)) as im:
                return int(im.size[0]), int(im.size[1])
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _downscale_to_jpeg(png_bytes: bytes) -> Tuple[bytes, Tuple[int, int]]:
        """把 DVT 截图（PNG ~5MB）resize 到 720 长边并重编为 JPEG。

        返回 ``(jpeg_bytes, (new_w, new_h))``。

        - 维持原宽高比；只在原图最长边 > 阈值时缩放，不放大
        - JPEG 必须是 RGB（PIL ``mode != 'RGB'`` 会被 ``.convert('RGB')`` 一下，
          兼容 RGBA / RGB48 等 PNG 模式）
        - 不再 BytesIO 包一层；返回原始 ``bytes``
        """
        from PIL import Image  # noqa: PLC0415 - 主依赖
        with Image.open(io.BytesIO(png_bytes)) as im:
            if im.mode != "RGB":
                im = im.convert("RGB")
            w, h = im.size
            long_edge = max(w, h)
            if long_edge > _DOWNSCALE_LONG_EDGE:
                ratio = _DOWNSCALE_LONG_EDGE / float(long_edge)
                new_w = max(2, int(round(w * ratio)))
                new_h = max(2, int(round(h * ratio)))
                # H.264 编码器（libx264 / yuv420p）要求宽高都是偶数，否则
                # ffmpeg 直接 "width not divisible by 2" 退出。强制对齐偶数
                new_w -= new_w % 2
                new_h -= new_h % 2
                # LANCZOS 质量最好；BILINEAR 更快但 1290→720 这个量级 LANCZOS 仍 <10ms
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            else:
                # 原图也可能宽 / 高某一边奇数（虽然 iOS 通常都是偶数），保险起见
                new_w = w - (w % 2)
                new_h = h - (h % 2)
                if (new_w, new_h) != (w, h):
                    im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=False)
            return buf.getvalue(), (new_w, new_h)


__all__ = ["IosScreenStreamer"]
