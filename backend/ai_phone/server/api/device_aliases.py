"""设备别名管理 API：``/api/internal/device-aliases/*``（Bearer）。

对外（匿名）视图放在 :mod:`ai_phone.server.api.devices` 里的
``GET /api/devices/available``，本模块只做平台内部的管理面：

- ``GET    /api/internal/device-aliases`` → 全量列表
- ``GET    /api/internal/device-aliases/{serial}`` → 单条
- ``PUT    /api/internal/device-aliases/{serial}`` → upsert（body: ``{alias, note?}``）
- ``DELETE /api/internal/device-aliases/{serial}`` → 删除

冲突处理：``alias`` 全局唯一。upsert 时若新 alias 已被别的 serial 占用，返回
409 ``alias_conflict``；这是唯一会返 409 的场景，其他字段错误走 400。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Path, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from ..aliases import (
    create_or_update_alias,
    delete_alias,
    get_alias_by_alias,
    get_alias_by_serial,
    list_aliases,
)
from ._deps import DBSession
from .submissions import RequireBearer

router = APIRouter(prefix="/api/internal/device-aliases", tags=["internal-device-aliases"])


def _clean(s: Optional[str]) -> str:
    return (s or "").strip()


@router.get("", dependencies=[RequireBearer])
async def list_all(session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    """全量别名，按 alias 升序。规模 < 200 不分页。"""
    rows = await list_aliases(session)
    return [row.to_dict() for row in rows]


@router.get("/{serial}", dependencies=[RequireBearer])
async def get_one(
    serial: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    row = await get_alias_by_serial(session, serial)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "serial": serial},
        )
    return row.to_dict()


@router.put("/{serial}", dependencies=[RequireBearer])
async def put_alias(
    serial: str = Path(..., min_length=1, max_length=128),
    body: Dict[str, Any] = Body(..., description='{"alias": "<=128>", "note": "<=1000>"}'),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    """upsert 一条别名。"""
    alias = _clean(body.get("alias") if isinstance(body, dict) else None)
    note = _clean(body.get("note") if isinstance(body, dict) else None)
    if not alias:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": "invalid_alias", "message": "alias 必填且不能为空"},
        )
    if len(alias) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": "invalid_alias", "message": "alias 长度不能超过 128"},
        )
    if len(note) > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": "invalid_note", "message": "note 长度不能超过 1000"},
        )

    # 唯一性预检：别的 serial 已占用同名 alias → 409
    existing_by_alias = await get_alias_by_alias(session, alias)
    if existing_by_alias is not None and existing_by_alias.serial != serial:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "alias_conflict",
                "alias": alias,
                "conflictSerial": existing_by_alias.serial,
                "message": f"别名 {alias!r} 已绑定到设备 {existing_by_alias.serial}",
            },
        )

    row = await create_or_update_alias(session, serial=serial, alias=alias, note=note)
    await session.commit()
    await session.refresh(row)
    logger.info("device_alias.upsert serial={} alias={!r}", serial, alias)
    return row.to_dict()


@router.delete("/{serial}", dependencies=[RequireBearer])
async def delete_one(
    serial: str = Path(..., min_length=1, max_length=128),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    removed = await delete_alias(session, serial)
    await session.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "not_found", "serial": serial},
        )
    logger.info("device_alias.delete serial={}", serial)
    return {"serial": serial, "deleted": True}
