"""轨迹缓存 mode 决议。

env 只表示服务端是否开放缓存能力；payload 的 ``cacheMode`` 表示本次 Run
想走 off/v1/v2/v3 哪个模式。非法值和总开关关闭都对齐到 off。
"""

from __future__ import annotations

from typing import Any, Optional

CACHE_MODE_OFF = "off"
CACHE_MODE_V1 = "v1"
CACHE_MODE_V2 = "v2"
CACHE_MODE_V3 = "v3"
CACHE_MODES = {CACHE_MODE_OFF, CACHE_MODE_V1, CACHE_MODE_V2, CACHE_MODE_V3}


def normalize_requested_cache_mode(value: Any) -> str:
    if value is None:
        return CACHE_MODE_OFF
    text = str(value).strip().lower()
    return text if text in CACHE_MODES else CACHE_MODE_OFF


def resolve_effective_cache_mode(
    *,
    env_cache_enabled: bool,
    requested_cache_mode: Optional[str],
) -> str:
    requested = normalize_requested_cache_mode(requested_cache_mode)
    if requested == CACHE_MODE_OFF:
        return CACHE_MODE_OFF
    if not env_cache_enabled:
        return CACHE_MODE_OFF
    return requested


__all__ = [
    "CACHE_MODE_OFF",
    "CACHE_MODE_V1",
    "CACHE_MODE_V2",
    "CACHE_MODE_V3",
    "CACHE_MODES",
    "normalize_requested_cache_mode",
    "resolve_effective_cache_mode",
]
