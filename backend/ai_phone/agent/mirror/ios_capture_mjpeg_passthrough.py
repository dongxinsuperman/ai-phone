"""iOS 镜像通道（方案 C 默认）：WDA MJPEG server → JPEG 直通浏览器。

和 ``ios_capture_mjpeg.IosMjpegStreamer`` 的区别只有"拿到 JPEG 之后怎么办"：

- ``IosMjpegStreamer``（wda_mjpeg 后端）：JPEG → ffmpeg → H.264 → fmp4 → MSE
- ``IosMjpegPassthroughStreamer``（mjpeg_passthrough 后端，本模块，**默认**）：
  JPEG → base64 → MSG_MIRROR_JPEG → 浏览器 ``<img>`` / canvas 绘制

为什么选 passthrough 做默认：

1. **每帧独立**，设备旋转 / 分辨率变化天然自适应——浏览器重设 ``img.src`` 就是
   新尺寸新方向。不需要像 MSE 那样在"WDA canvas 尺寸定死"与"H.264 init segment
   定死分辨率"两头打补丁
2. 无 ffmpeg 编码开销，agent CPU 最低
3. 延迟最小（省掉 H.264 编码 + MSE buffer）
4. 这正是 Sonic / Appium inspector 在 iOS 17+ 实际跑的方案

带宽和 wda_mjpeg 差不多：quality=60 720p 约 30-50KB/帧，20fps ≈ 700KB/s。

端口转发 / 切帧 / settings 下发 / orientation watcher 全部复用父类代码。
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from loguru import logger

from .ios_capture_mjpeg import IosMjpegStreamer


class IosMjpegPassthroughStreamer(IosMjpegStreamer):
    """JPEG 直通版——不经 ffmpeg，直接回调每帧字节给上层。

    对外 API 保持和 ``IosMjpegStreamer`` 一致（``start`` / ``stop`` /
    ``restart`` / ``is_alive``），让 ``_IosMirrorSession`` 可以统一管理。

    ``restart()`` 对 passthrough 来说没有实际"流重启"需求（每帧独立，不存在
    init segment），保留为 no-op 以兼容父类调用约定。
    """

    def __init__(
        self,
        serial: str,
        on_jpeg: Callable[[bytes, int, int], None],
        *,
        target_fps: int = 20,
        jpeg_quality: int = 60,
        long_edge: int = 720,
        device_mjpeg_port: int = 9100,
        wda_local_port_for_settings: Optional[int] = None,
        log_tag: str = "ios-mjpeg-pt",
    ) -> None:
        """
        Args:
            serial: udid
            on_jpeg: 收到一帧 JPEG 时的回调 ``(jpeg_bytes, width, height)``
                width/height 在头解析失败时会传 0，回调实现需要自己兜底
            target_fps / jpeg_quality / long_edge / device_mjpeg_port /
                wda_local_port_for_settings: 同父类
            log_tag: 日志 tag 前缀
        """
        # 父类要求 on_init / on_segment 回调（fmp4 管线的），给它一对空 callable
        # 搪塞。因为我们 override 了 _consume_jpeg，父类的 self._fmp4 永远不会
        # 被喂数据，进而 on_init / on_segment 永远不会被调。为了 100% 安全，
        # stop() 里显式干掉 fmp4 子进程。
        super().__init__(
            serial=serial,
            on_init=lambda _init: None,
            on_segment=lambda _seg: None,
            target_fps=target_fps,
            jpeg_quality=jpeg_quality,
            long_edge=long_edge,
            device_mjpeg_port=device_mjpeg_port,
            wda_local_port_for_settings=wda_local_port_for_settings,
            log_tag=log_tag,
        )
        self._on_jpeg = on_jpeg
        # passthrough 不需要 ffmpeg。父类 __init__ 会实例化一个 FMp4Streamer，
        # 但它要到 start()/feed() 才真正 spawn 子进程。我们不喂 feed，所以
        # ffmpeg 子进程根本不会起来；只是 self._fmp4 对象本身留着，stop() 仍
        # 会调 self._fmp4.stop() 作为兜底。零额外成本。

    # ------------------------------------------------------------------
    # 核心：接管父类的 per-frame 消费路径，绕开 fmp4
    # ------------------------------------------------------------------
    def _consume_jpeg(
        self, jpeg: bytes, size: Optional[Tuple[int, int]]
    ) -> None:
        w, h = (size or (0, 0))
        # **不**像父类那样在尺寸变化时 restart：passthrough 每帧独立，浏览器
        # 下一次重设 img.src 就自动按新帧尺寸/比例绘制，无状态。
        if (
            size is not None
            and self._last_size is not None
            and size != self._last_size
        ):
            logger.info(
                "[{}] mjpeg 尺寸变化 {}→{}（passthrough 无需重启，下一帧即生效）",
                self._log_tag, self._last_size, size,
            )
        if size is not None:
            self._last_size = size
        try:
            self._on_jpeg(jpeg, w, h)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] on_jpeg 回调异常: {}", self._log_tag, exc)

    def restart(self) -> None:
        # passthrough 无 init segment 概念；每帧独立。no-op 兼容父类 API。
        self._last_size = None
