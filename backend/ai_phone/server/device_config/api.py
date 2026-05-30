from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Path, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from ..api._deps import DBSession
from .schemas import (
    ALLOWED_WAKE_POLICY_PLATFORMS,
    DeviceWakePolicyPatch,
    DeviceWakePolicyUpsert,
    normalize_platform,
)
from .service import (
    delete_wake_policy,
    get_wake_policy,
    list_wake_policies,
    upsert_wake_policy,
)


router = APIRouter(prefix="/api/device-wake-policies", tags=["device-wake-policies"])


def _reject_invalid_platform(platform: str) -> None:
    normalized = normalize_platform(platform)
    if normalized == "android":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "android_not_configurable",
                "message": "Android 固定走 KEYCODE_WAKEUP + dismiss-keyguard，无需按设备配置上滑",
            },
        )
    if normalized == "ios":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "ios_not_configurable",
                "message": "iOS 无需按设备配置，所有 iOS 设备统一走 wda.unlock",
            },
        )
    if normalized not in ALLOWED_WAKE_POLICY_PLATFORMS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "invalid_platform",
                "message": "platform 必须是 harmony",
            },
        )


@router.get("")
async def list_all(
    platform: Optional[str] = Query(default=None, max_length=16),
    session: AsyncSession = DBSession,
) -> List[Dict[str, Any]]:
    normalized = normalize_platform(platform or "")
    if normalized:
        _reject_invalid_platform(normalized)
    rows = await list_wake_policies(session, platform=normalized or "harmony")
    return [row.to_dict() for row in rows]


@router.post("")
async def upsert(
    body: DeviceWakePolicyUpsert,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    _reject_invalid_platform(body.platform)
    row = await upsert_wake_policy(
        session,
        serial=body.serial,
        platform=body.platform,
        wake_swipe=body.wake_swipe,
        remark=body.remark,
    )
    await session.commit()
    await session.refresh(row)
    logger.info(
        "device_wake_policy.upsert serial={} platform={} wake_swipe={}",
        row.serial,
        row.platform,
        row.wake_swipe,
    )
    return row.to_dict()


@router.patch("/{serial}")
async def patch_one(
    body: DeviceWakePolicyPatch,
    serial: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    cleaned_serial = str(serial or "").strip()
    row = await get_wake_policy(session, cleaned_serial)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "serial": cleaned_serial},
        )
    _reject_invalid_platform(row.platform)

    patch = body.model_dump(exclude_unset=True)
    if "platform" in patch and patch["platform"] is not None:
        _reject_invalid_platform(patch["platform"])
        row.platform = patch["platform"]
    if "wake_swipe" in patch and patch["wake_swipe"] is not None:
        row.wake_swipe = bool(patch["wake_swipe"])
    if "remark" in patch and patch["remark"] is not None:
        row.remark = patch["remark"]

    await session.commit()
    await session.refresh(row)
    logger.info("device_wake_policy.patch serial={}", row.serial)
    return row.to_dict()


@router.delete("/{serial}")
async def delete_one(
    serial: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    cleaned_serial = str(serial or "").strip()
    removed = await delete_wake_policy(session, cleaned_serial)
    await session.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "serial": cleaned_serial},
        )
    logger.info("device_wake_policy.delete serial={}", cleaned_serial)
    return {"serial": cleaned_serial, "deleted": True}
