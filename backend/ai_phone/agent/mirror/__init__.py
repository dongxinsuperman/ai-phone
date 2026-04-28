"""Mirror 推流相关模块。

当前唯一成员：``fmp4.FMp4Streamer`` —— 把 scrcpy 出来的原始 H.264 (annex-B) 字节
喂给 ffmpeg 子进程，再从 stdout 拿 fragmented MP4 (init + media segments)，
直接送给浏览器 MSE/<video> 播放（无需 server 端 H.264→JPEG 二次编码）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .fmp4 import (
    FMp4Streamer,
    extract_codec_string_from_moov,
    extract_resolution_from_moov,
    extract_sps_nal,
)

# IosScreenStreamer / IosMjpegStreamer 走 lazy import：iOS 是可选 extras，
# pymobiledevice3 / httpx 没装时导入也不应该报错（只有真正用到才会触发）。
# 所以不在这里直接 re-export，避免 ``from ai_phone.agent.mirror import IosScreenStreamer``
# 在没装 ios extras 时炸。


def build_ios_streamer(
    *,
    serial: str,
    # --- fmp4 / MSE 路径专用（wda_mjpeg / dvt_screenshot）---
    on_init: Callable[[bytes], None],
    on_segment: Callable[[bytes], None],
    target_fps: int,
    frag_ms: int,
    gop_sec: int,
    # --- mjpeg_passthrough 专用 ---
    on_jpeg: Optional[Callable[[bytes, int, int], None]] = None,
    # --- 通用 ---
    log_tag: str,
    wda_local_port: Optional[int] = None,
) -> Any:
    """按 ``settings.ios_mirror_backend`` 决定用哪个 iOS 镜像后端。

    返回值的 API 必须与 ``IosScreenStreamer`` 等价（``start`` / ``stop`` /
    ``restart`` / ``is_alive``），让 ``_IosMirrorSession`` 不感知后端差异。

    三个后端（env ``AI_PHONE_IOS_MIRROR_BACKEND``）：

    - ``mjpeg_passthrough``（**默认**）：WDA mjpeg → JPEG 直通浏览器
      *必须*传 ``on_jpeg``；``on_init`` / ``on_segment`` 会被忽略
    - ``wda_mjpeg``：WDA mjpeg → ffmpeg H.264 → fmp4 → MSE
      使用 ``on_init`` / ``on_segment``
    - ``dvt_screenshot``：pmd3 DVT 轮询 PNG → fmp4 → MSE（保底）

    Args:
        wda_local_port: 仅 mjpeg 两路用，传 0 / None 表示不下发 appium settings
        on_jpeg: 仅 passthrough 用；签名 ``(jpeg_bytes, width, height) -> None``
    """
    from ai_phone.config import get_settings  # noqa: PLC0415

    s = get_settings()
    backend = (s.ios_mirror_backend or "mjpeg_passthrough").strip().lower()

    if backend == "mjpeg_passthrough":
        if on_jpeg is None:
            raise ValueError(
                "mjpeg_passthrough 后端需要 on_jpeg 回调；请让 _IosMirrorSession "
                "传入 self._on_mirror_jpeg"
            )
        from .ios_capture_mjpeg_passthrough import (  # noqa: PLC0415
            IosMjpegPassthroughStreamer,
        )

        return IosMjpegPassthroughStreamer(
            serial=serial,
            on_jpeg=on_jpeg,
            target_fps=int(s.wda_mjpeg_fps or target_fps),
            jpeg_quality=int(s.wda_mjpeg_quality),
            long_edge=int(s.wda_mjpeg_long_edge),
            device_mjpeg_port=int(s.wda_mjpeg_device_port),
            wda_local_port_for_settings=wda_local_port,
            log_tag=log_tag.replace("ios-fmp4", "ios-mjpeg-pt"),
        )

    if backend == "wda_mjpeg":
        from .ios_capture_mjpeg import IosMjpegStreamer  # noqa: PLC0415

        return IosMjpegStreamer(
            serial=serial,
            on_init=on_init,
            on_segment=on_segment,
            target_fps=int(s.wda_mjpeg_fps or target_fps),
            jpeg_quality=int(s.wda_mjpeg_quality),
            long_edge=int(s.wda_mjpeg_long_edge),
            device_mjpeg_port=int(s.wda_mjpeg_device_port),
            wda_local_port_for_settings=wda_local_port,
            frag_ms=frag_ms,
            gop_sec=gop_sec,
            log_tag=log_tag.replace("ios-fmp4", "ios-mjpeg"),
        )

    # dvt_screenshot（保底）
    from .ios_capture import IosScreenStreamer  # noqa: PLC0415

    return IosScreenStreamer(
        serial=serial,
        on_init=on_init,
        on_segment=on_segment,
        target_fps=target_fps,
        frag_ms=frag_ms,
        gop_sec=gop_sec,
        log_tag=log_tag,
    )


def build_harmony_streamer(
    *,
    serial: str,
    driver: Any,
    on_jpeg: Callable[[bytes, int, int], None],
    log_tag: str,
) -> Any:
    """鸿蒙镜像 streamer 工厂。返回值 API 与 ``IosMjpegPassthroughStreamer`` 等价
    （``start`` / ``stop`` / ``restart`` / ``is_alive``），让上层不感知后端差异。

    两个后端（env ``AI_PHONE_HARMONY_MIRROR_BACKEND``）：

    - ``screenshot``（**默认/兜底**）：``HarmonyScreenshotStreamer``，hmdriver2
      截图轮询，~8-10fps 上限。稳定但卡。
    - ``hypium``：``HarmonyHypiumStreamer``，hypium Captures MJPEG 协议，
      设备硬编码 + socket 长连主动 push，~30fps，<100ms 延迟。

    Args:
        driver: 已就绪的 ``HarmonyDriver``。screenshot 后端用它截图；hypium
            后端不直接用 driver（独立 socket），但保留参数让 supervisor 一视同仁。
    """
    from ai_phone.config import get_settings  # noqa: PLC0415

    s = get_settings()
    backend = (s.harmony_mirror_backend or "screenshot").strip().lower()

    if backend == "hypium":
        from .harmony_capture_hypium import HarmonyHypiumStreamer  # noqa: PLC0415

        return HarmonyHypiumStreamer(
            serial=serial,
            on_jpeg=on_jpeg,
            log_tag=log_tag.replace("hm-shot", "hm-hypium"),
        )

    # screenshot（默认/兜底）
    from .harmony_capture import HarmonyScreenshotStreamer  # noqa: PLC0415

    return HarmonyScreenshotStreamer(
        serial=serial,
        driver=driver,
        on_jpeg=on_jpeg,
        target_fps=int(s.harmony_mirror_fps),
        jpeg_quality=int(s.harmony_mirror_jpeg_quality),
        long_edge=int(s.harmony_mirror_long_edge),
        log_tag=log_tag,
    )


__all__ = [
    "FMp4Streamer",
    "extract_resolution_from_moov",
    "extract_codec_string_from_moov",
    "extract_sps_nal",
    "build_ios_streamer",
    "build_harmony_streamer",
]
