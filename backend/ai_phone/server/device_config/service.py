from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DeviceWakePolicy


async def list_wake_policies(
    session: AsyncSession,
    *,
    platform: Optional[str] = None,
) -> List[DeviceWakePolicy]:
    stmt = select(DeviceWakePolicy).order_by(
        DeviceWakePolicy.platform.asc(),
        DeviceWakePolicy.serial.asc(),
    )
    if platform:
        stmt = stmt.where(DeviceWakePolicy.platform == platform)
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_wake_policy(
    session: AsyncSession,
    serial: str,
) -> Optional[DeviceWakePolicy]:
    return await session.get(DeviceWakePolicy, serial)


async def upsert_wake_policy(
    session: AsyncSession,
    *,
    serial: str,
    platform: str,
    wake_swipe: bool,
    remark: str = "",
) -> DeviceWakePolicy:
    row = await get_wake_policy(session, serial)
    if row is None:
        row = DeviceWakePolicy(
            serial=serial,
            platform=platform,
            wake_swipe=bool(wake_swipe),
            remark=remark or "",
        )
        session.add(row)
        return row
    row.platform = platform
    row.wake_swipe = bool(wake_swipe)
    row.remark = remark or ""
    return row


async def delete_wake_policy(session: AsyncSession, serial: str) -> bool:
    row = await get_wake_policy(session, serial)
    if row is None:
        return False
    await session.delete(row)
    return True
