"""device_aliases 表的纯 CRUD，不做业务校验。

所有方法只接 :class:`AsyncSession`，不自己起事务；commit 由调用方决定。这样和
``scheduler.service`` / ``api.device_aliases`` 可以共享同一个请求级 Session。
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DeviceAlias


async def list_aliases(session: AsyncSession) -> List[DeviceAlias]:
    """全量别名，按 alias 字母序。规模很小（< 200 台），无分页。"""
    res = await session.execute(select(DeviceAlias).order_by(DeviceAlias.alias.asc()))
    return list(res.scalars().all())


async def get_alias_by_serial(session: AsyncSession, serial: str) -> Optional[DeviceAlias]:
    return await session.get(DeviceAlias, serial)


async def get_alias_by_alias(session: AsyncSession, alias: str) -> Optional[DeviceAlias]:
    """按友好名反查。名字全局唯一，最多一条。"""
    res = await session.execute(
        select(DeviceAlias).where(DeviceAlias.alias == alias)
    )
    return res.scalar_one_or_none()


async def create_or_update_alias(
    session: AsyncSession,
    *,
    serial: str,
    alias: str,
    note: str = "",
) -> DeviceAlias:
    """upsert：同 serial 则就地改名 + 改 note；否则新建。

    ``alias`` 冲突（别的 serial 已占用）由调用方先查一次做 409 返回；本函数单纯写。
    """
    existing = await session.get(DeviceAlias, serial)
    if existing is None:
        existing = DeviceAlias(serial=serial, alias=alias, note=note)
        session.add(existing)
    else:
        existing.alias = alias
        existing.note = note
    await session.flush()
    return existing


async def delete_alias(session: AsyncSession, serial: str) -> bool:
    """按 serial 删；返回是否真的删了一条。"""
    existing = await session.get(DeviceAlias, serial)
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True
