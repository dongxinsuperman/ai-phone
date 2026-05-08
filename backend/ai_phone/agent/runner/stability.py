"""基于 pHash 的页面稳定等待器（迁移自 Groovy `waitPageStablePixelSmart`）。

- 默认总超时 5s、轮询 0.4s、变化率阈值 0.04。
- 支持"复用上一步尾帧作 frame A"：省一次截图。
- 所有异常（截图失败 / PIL 异常）都兜底返回最后可用帧，让上层继续执行，绝
  不把 runner 拍死。

阈值调优历史（2026-04 起）：
    Sonic 线上原配 threshold=0.005 / total_timeout=10s 偏"洁癖"，对**瞬态
    UI**（视频进度条、Toast、自动隐藏的浮层等 3-5s 后自动消失的反馈窗口）
    会等过头——等"完全稳定"时反馈窗口已经消失，VLM 拿到的截图反而失去
    上下文。改为 threshold=0.04 / total_timeout=5s / poll=0.4s 后：
    - 4% 以内的小动画/微抖动直接判稳定，不再死等到 0.5%
    - 5s 兜底比瞬态 UI 自然消失更早返回，VLM 一定能看到反馈
    - trade-off：偶尔会拿到"动画收尾过程中"的截图（4% 内残留差异），
      但 VLM 识别能力足以消化，且大多数页面跳转 1-2s 即稳定
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ai_phone.config import get_settings

from .phash import compute_phash, diff_rate

# 截图函数签名：同步（driver 返回 bytes）或异步都行，runner 里统一 awaitable 包装
ScreenshotFn = Callable[[], Awaitable[bytes]]
# 日志回调（info/warn/error, title, content）
LogFn = Callable[[int, str, str], None]


@dataclass
class StabilityResult:
    bytes_: Optional[bytes]
    stable: bool
    elapsed_ms: int
    checks: int


async def wait_page_stable_pixel(
    screenshot: ScreenshotFn,
    frame_a_bytes: Optional[bytes] = None,
    *,
    total_timeout_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
    threshold: Optional[float] = None,
    use_cache_settings: bool = False,
    log: Optional[LogFn] = None,
) -> StabilityResult:
    """轮询截图直到两帧 pHash 差异率 ≤ threshold 或超总时长。

    返回稳定帧 bytes（兜底返回最后一张）。
    """
    start = time.monotonic()

    def _log(level: int, title: str, content: str) -> None:
        if log is not None:
            log(level, title, content)

    settings = get_settings()
    if use_cache_settings:
        enabled = bool(settings.trajectory_cache_page_stable_enabled)
        default_timeout = float(settings.trajectory_cache_page_stable_timeout_s)
        default_poll = float(settings.trajectory_cache_page_stable_poll_s)
        default_threshold = float(settings.trajectory_cache_page_stable_threshold)
    else:
        enabled = bool(settings.vlm_page_stable_enabled)
        default_timeout = float(settings.vlm_page_stable_timeout_s)
        default_poll = float(settings.vlm_page_stable_poll_s)
        default_threshold = float(settings.vlm_page_stable_threshold)

    total_timeout_s = default_timeout if total_timeout_s is None else float(total_timeout_s)
    poll_interval_s = default_poll if poll_interval_s is None else float(poll_interval_s)
    threshold = default_threshold if threshold is None else float(threshold)
    total_timeout = total_timeout_s
    poll_ms = max(0.1, poll_interval_s)

    if not enabled:
        _log(
            1,
            "页面稳定检测",
            "未开启，直接截图放行"
            + (" | 忽略复用尾帧" if frame_a_bytes is not None else ""),
        )
        try:
            current_bytes = await screenshot()
            return StabilityResult(
                current_bytes,
                False,
                int((time.monotonic() - start) * 1000),
                0,
            )
        except Exception as exc:  # noqa: BLE001
            _log(3, "截图异常", f"错误: {exc} | 返回复用尾帧")
            return StabilityResult(
                frame_a_bytes,
                False,
                int((time.monotonic() - start) * 1000),
                0,
            )

    _log(
        1,
        "页面稳定检测",
        f"策略=像素哈希 | 总超时={total_timeout}s | 轮询={poll_interval_s}s | "
        f"阈值={threshold}"
        + (" | 复用上步尾帧" if frame_a_bytes is not None else ""),
    )

    last_bytes = frame_a_bytes
    if last_bytes is None:
        try:
            last_bytes = await screenshot()
        except Exception as exc:  # noqa: BLE001
            _log(3, "基准截图失败", f"错误: {exc}")
            return StabilityResult(None, False, int((time.monotonic() - start) * 1000), 0)
    last_hash = compute_phash(last_bytes)

    checks = 0
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= total_timeout:
                _log(
                    2,
                    "检测超时",
                    f"已检测{elapsed:.1f}s（{checks}次），返回最后帧继续执行",
                )
                return StabilityResult(
                    last_bytes, False, int(elapsed * 1000), checks
                )

            await asyncio.sleep(poll_ms)
            checks += 1

            try:
                cur_bytes = await screenshot()
            except Exception as exc:  # noqa: BLE001
                _log(3, "截图异常", f"错误: {exc} | 返回最后帧")
                return StabilityResult(
                    last_bytes, False, int((time.monotonic() - start) * 1000), checks
                )

            cur_hash = compute_phash(cur_bytes)
            rate = diff_rate(last_hash, cur_hash)
            if rate <= threshold:
                elapsed = time.monotonic() - start
                _log(
                    1,
                    "截图已稳定",
                    f"变化率={rate:.4f} ≤ {threshold} | 检测{checks}次 | 耗时{elapsed:.1f}s",
                )
                return StabilityResult(cur_bytes, True, int(elapsed * 1000), checks)

            _log(
                1,
                "页面变化中",
                f"变化率={rate:.4f} > {threshold} | 第{checks}次 | 继续等待",
            )
            last_bytes = cur_bytes
            last_hash = cur_hash
    except Exception as exc:  # noqa: BLE001
        _log(3, "检测异常", f"错误: {exc} | 返回最后帧继续执行")
        return StabilityResult(
            last_bytes, False, int((time.monotonic() - start) * 1000), checks
        )
