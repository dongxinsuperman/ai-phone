"""/api/devices & 设备占用锁接口。

设计：
- GET /api/devices                              列设备（含 lock 状态叠加）
- GET /api/devices/{serial}                     单设备详情
- POST /api/devices/{serial}/lock               占用锁（body: holder, holder_type）
- POST /api/devices/{serial}/heartbeat          心跳续期（body: token）
- DELETE /api/devices/{serial}/lock             释放锁（body: token 或 force）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..hub import Hub
from ..lockstore import BadToken, DeviceLockStore, LockConflict, LockNotFound
from ..models import Device, DeviceAlias
from ._deps import DBSession, HubDep, LockStoreDep

router = APIRouter(prefix="/api/devices", tags=["devices"])


# ---------------------------------------------------------------------------
# Pydantic 入参 / 出参
# ---------------------------------------------------------------------------
class LockAcquireReq(BaseModel):
    holder: str = Field(..., description="持有者标识（浏览器 session id / job id / webhook id）")
    # holder_type 仅作元数据展示；不参与互斥判断
    holder_type: str = Field("session", pattern="^(session|job|webhook|manual|auto)$")
    ttl_seconds: Optional[float] = Field(None, ge=3.0, le=300.0)
    meta: Optional[Dict[str, Any]] = None


class LockTokenReq(BaseModel):
    token: str


class LockForceReleaseReq(BaseModel):
    token: Optional[str] = None
    force: bool = False


class InputReq(BaseModel):
    kind: str = Field(
        ...,
        pattern="^(tap|swipe|long_press|type|press_home|press_back|keycode)$",
    )
    params: Optional[Dict[str, Any]] = None
    lock_token: str = Field(..., description="调用方持有的设备锁 token")


# ---------------------------------------------------------------------------
# 列表 / 详情
# ---------------------------------------------------------------------------
async def _merge_lock_into(
    dev_dict: Dict[str, Any],
    store: DeviceLockStore,
    hub: Optional[Hub] = None,
) -> Dict[str, Any]:
    lock = store.peek(dev_dict["serial"])
    dev_dict["lock"] = lock.to_dict() if lock else None
    # 派生状态：在线设备被锁视为 busy，前端总览一眼就能看出来
    if lock and dev_dict.get("status") == "online":
        dev_dict["effective_status"] = "busy"
    else:
        dev_dict["effective_status"] = dev_dict.get("status", "offline")
    # 合并 agent 上报的易变元信息（如 unauthorized 的 reason 提示）
    if hub is not None:
        extra = hub.get_device_extra(dev_dict["serial"])
        if extra:
            dev_dict["extra"] = extra
    return dev_dict


@router.get("")
async def list_devices(
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> List[Dict[str, Any]]:
    res = await session.execute(select(Device).order_by(Device.serial))
    alias_map = await _load_alias_map(session)
    items: List[Dict[str, Any]] = []
    for d in res.scalars().all():
        dd = d.to_dict()
        dd["alias"] = alias_map.get(d.serial, "")
        items.append(await _merge_lock_into(dd, store, hub))
    return items


async def _load_alias_map(session: AsyncSession) -> Dict[str, str]:
    """一次捞全量 alias → 内存 dict。表规模很小（< 200 台），不做分页。"""
    res = await session.execute(select(DeviceAlias.serial, DeviceAlias.alias))
    return {row.serial: row.alias for row in res.all()}


# ---------------------------------------------------------------------------
# 对外匿名：GET /api/devices/available
# ---------------------------------------------------------------------------
# 语义："外部调用方想提交任务前，先看看平台现在哪台能接"。只返回 **此刻就能调度** 的
# 设备：online + readiness.ready + 未被锁 + agent WS 在线。没绑别名的设备照样出现，
# alias 字段空串，提交时只能走"不指定 alias"的随机派发。
@router.get("/available")
async def list_available(
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> List[Dict[str, Any]]:
    """匿名：当前可接单的设备清单（含 alias，alias 可能为空串）。

    返回字段按外部调用方需要裁剪：

    - ``serial`` 设备唯一码（全文）
    - ``alias``  友好名，未绑定时为 ``""``
    - ``platform`` / ``brand`` / ``model`` / ``osVersion``
    - ``screenWidth`` / ``screenHeight``
    - ``lastSeenAt``

    不暴露内部字段（agent_id、锁 token、readiness 原始 payload 等）。
    """
    res = await session.execute(
        select(Device).where(Device.status == "online").order_by(Device.serial)
    )
    alias_map = await _load_alias_map(session)
    out: List[Dict[str, Any]] = []
    for dev in res.scalars().all():
        # 未 ready / 被锁 / 没有活 agent → 跳过
        extra = hub.get_device_extra(dev.serial) or {}
        readiness = extra.get("readiness") or {}
        if not readiness.get("ready"):
            continue
        if store.peek(dev.serial) is not None:
            continue
        if hub.agent_id_for_serial(dev.serial) is None:
            continue
        out.append({
            "serial": dev.serial,
            "alias": alias_map.get(dev.serial, ""),
            "platform": dev.platform,
            "brand": dev.brand or "",
            "model": dev.model or "",
            "osVersion": dev.os_version or "",
            "screenWidth": dev.screen_width or 0,
            "screenHeight": dev.screen_height or 0,
            "lastSeenAt": dev.last_seen_at.isoformat() if dev.last_seen_at else None,
        })
    return out


@router.get("/{serial}")
async def get_device(
    serial: str,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    dev = await session.get(Device, serial)
    if dev is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    return await _merge_lock_into(dev.to_dict(), store, hub)


# ---------------------------------------------------------------------------
# 占用锁
# ---------------------------------------------------------------------------
@router.post("/{serial}/lock", status_code=status.HTTP_201_CREATED)
async def acquire_lock(
    serial: str,
    body: LockAcquireReq,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
) -> Dict[str, Any]:
    dev = await session.get(Device, serial)
    if dev is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    if dev.status != "online":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"device status={dev.status}, 必须 online 才能占用",
        )
    try:
        info = await store.acquire(
            serial,
            holder=body.holder,
            holder_type=body.holder_type,
            ttl_seconds=body.ttl_seconds,
            meta=body.meta,
        )
    except LockConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    # token 只在这里返给占用方；后续心跳 / 释放都要带
    payload = info.to_dict()
    payload["token"] = info.token
    return payload


@router.post("/{serial}/heartbeat")
async def heartbeat_lock(
    serial: str,
    body: LockTokenReq,
    store: DeviceLockStore = LockStoreDep,
) -> Dict[str, Any]:
    try:
        info = await store.heartbeat(serial, body.token)
    except LockNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except BadToken as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return info.to_dict()


@router.delete("/{serial}/lock")
async def release_lock(
    serial: str,
    body: LockForceReleaseReq,
    store: DeviceLockStore = LockStoreDep,
) -> Dict[str, Any]:
    try:
        removed = await store.release(
            serial, token=body.token or "", force=body.force
        )
    except BadToken as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return {"released": removed}


# ---------------------------------------------------------------------------
# 手动输入（浏览器持 manual 锁时派发给 Agent）
# ---------------------------------------------------------------------------
@router.post("/{serial}/input")
async def send_input(
    serial: str,
    body: InputReq,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    _hub: Hub = HubDep,
) -> Dict[str, Any]:
    dev = await session.get(Device, serial)
    if dev is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    if dev.status != "online":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"device status={dev.status}, 必须 online",
        )
    # 新锁模型：谁持有有效 token 谁就能派发输入，不区分 manual/auto 角色。
    # VLM Run 沿用发起方的 token，同一 token 下自动和手动可以交织。
    lock = store.peek(serial)
    if lock is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="设备未被占用，请先 acquire 锁",
        )
    if lock.token != body.lock_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="lock_token 不匹配，你不是当前持有者",
        )
    ok = await _hub.send_to_serial(
        serial,
        {
            "type": P.MSG_INPUT,
            "serial": serial,
            "kind": body.kind,
            "params": body.params or {},
        },
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="派发到 Agent 失败",
        )
    return {"dispatched": True}
