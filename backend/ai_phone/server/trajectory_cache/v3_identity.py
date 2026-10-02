"""V3 平台共享标识；不改变 V1/V2 的设备 key。"""
from __future__ import annotations

import hashlib
import json
from typing import Tuple

from ai_phone.shared.protocol import platform_family
from .service import build_cache_key


async def resolve_v3_platform(session, *, device_code: str, hint: str = "") -> str:
    from ai_phone.server.models import Device

    device = await session.get(Device, device_code)
    # 已登记设备以 Server 记录为准；未知平台不猜为 Android。
    return v3_platform(device.platform if device is not None else hint)


def v3_platform(platform: str) -> str:
    family = platform_family(str(platform or "").strip().lower())
    return family if family in {"android", "ios", "harmony"} else ""


def build_v3_platform_cache_key(*, platform: str, run_semantic_text: str) -> Tuple[str, str, str]:
    family = v3_platform(platform)
    if not family:
        raise ValueError("V3 platform cache requires a known platform")
    # 复用既有 Case 原文规范化与哈希口径；平台命名空间不与旧设备 key 重合。
    _legacy, normalized, semantic_hash = build_cache_key(
        device_code="", run_semantic_text=run_semantic_text, schema_version=3,
    )
    material = json.dumps(["v3-platform", family, semantic_hash], separators=(",", ":"))
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return key, normalized, semantic_hash
