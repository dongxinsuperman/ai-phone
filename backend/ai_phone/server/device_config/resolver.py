from __future__ import annotations

from typing import Dict

from sqlalchemy.ext.asyncio import AsyncSession

from .schemas import ALLOWED_WAKE_POLICY_PLATFORMS, normalize_platform
from .service import get_wake_policy


async def resolve_wake_decision(
    session: AsyncSession,
    serial: str,
    platform: str,
) -> Dict[str, bool]:
    """Resolve per-device wake policy from DB only.

    V2 only keeps per-device swipe policy for HarmonyOS. Android is handled by
    KEYCODE_WAKEUP + dismiss-keyguard, and iOS uses its own WDA unlock flow.
    HarmonyOS defaults to no swipe when no DB row exists or the stored platform
    no longer matches the live one.
    """

    normalized = normalize_platform(platform)
    if normalized not in ALLOWED_WAKE_POLICY_PLATFORMS:
        return {}

    row = await get_wake_policy(session, str(serial or "").strip())
    if row is None or normalize_platform(row.platform) != normalized:
        return {"wake_swipe": False}
    return {"wake_swipe": bool(row.wake_swipe)}
