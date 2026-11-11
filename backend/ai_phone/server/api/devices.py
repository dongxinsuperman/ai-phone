"""/api/devices & 设备占用锁接口。

设计：
- GET /api/devices                              列设备（含 lock 状态叠加）
- GET /api/devices/{serial}                     单设备详情
- POST /api/devices/{serial}/lock               占用锁（body: holder, holder_type）
- POST /api/devices/{serial}/heartbeat          心跳续期（body: token）
- DELETE /api/devices/{serial}/lock             释放锁（body: token 或 force）
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Literal, Optional, get_args

from fastapi import APIRouter, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..android_vm.schemas import AndroidDisplayOrientation
from ..android_vm.service import request_vm_display, save_vm_display, vm_display_lock
from ..app_uninstall import (
    AppUninstallDispatchError,
    AppUninstallTimeoutError,
    get_app_uninstall_waiter,
)
from ..hub import Hub
from ..harmony_vm.models import HarmonyVmInstance
from ..lockstore import BadToken, DeviceLockStore, LockConflict, LockNotFound
from ..models import AndroidVmInstance, Device, DeviceAlias
from ..vm_lifecycle import require_idle, require_owner_host, runtime as vm_runtime, send_operation_once, transaction_lock
from ._deps import DBSession, HubDep, LockStoreDep

router = APIRouter(prefix="/api/devices", tags=["devices"])

APP_UNINSTALL_TIMEOUT_SEC = 60.0
APP_UNINSTALL_RESPONSE_TIMEOUT_SEC = 70.0


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


class InputFrame(BaseModel):
    stream_id: str = Field(..., min_length=1, max_length=64)
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)


class InputReq(BaseModel):
    kind: str = Field(
        ...,
        pattern="^(tap|swipe|long_press|type|press_home|press_back|keycode)$",
    )
    params: Optional[Dict[str, Any]] = None
    lock_token: str = Field(..., description="调用方持有的设备锁 token")
    frame: Optional[InputFrame] = None


class AndroidDisplayReq(BaseModel):
    vm_id: str = Field(..., min_length=1, max_length=32)
    lock_token: str
    orientation: Optional[AndroidDisplayOrientation] = None
    rotation: Optional[Literal["left", "right"]] = None
    fold_state: Optional[Literal["folded", "unfolded"]] = None


class NativeVmDisplayReq(AndroidDisplayReq):
    platform: Literal["harmony"]
    rotation_locked: Optional[bool] = None


class AppUninstallReq(BaseModel):
    package_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$",
        description="App 精确包名（Android/Harmony）或 Bundle ID（iOS）",
    )


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
# 对外匿名：GET /api/devices/statuses / available
# ---------------------------------------------------------------------------
@router.get("/statuses")
async def list_device_statuses(
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> List[Dict[str, Any]]:
    """匿名：全量设备状态清单，响应结构与 ``GET /api/devices`` 等价。

    这个入口用于对外暴露当前工作台已经消费的设备状态模型，避免外部系统继续
    依赖内部路径；字段名和嵌套结构保持一致（如 ``effective_status``、
    ``screen_width``、``lock.holder_type``）。

    唯一与 ``GET /api/devices`` 取值不同的是 ``platform``：这里报对外平台，
    iOS 虚拟机报 ``ios``。内部通道（``ios_sim``）只在工作台用来路由到虚拟机
    管理接口，对外没有意义，而提交时 ``platforms`` 也只认三端。
    「这台是不是虚拟机」由 ``extra.is_virtual`` / ``extra.vm_platform`` 表达，
    不靠 ``platform`` 承载，所以这里换口径不丢信息。
    """
    res = await session.execute(select(Device).order_by(Device.serial))
    alias_map = await _load_alias_map(session)
    items: List[Dict[str, Any]] = []
    for dev in res.scalars().all():
        dd = dev.to_dict()
        dd["alias"] = alias_map.get(dev.serial, "")
        dd["platform"] = P.platform_family(dev.platform)
        items.append(await _merge_lock_into(dd, store, hub))
    return items


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

    ``platform`` 报的是**对外平台**（``android`` / ``ios`` / ``harmony``），不是内部
    通道——iOS 虚拟机在这里报 ``ios``。这个接口的用途就是「提交前看看谁能接单」，
    而提交时 ``platforms`` 只认这三个值；两处口径必须一致，否则照着设备列表写脚本
    的人会拿 ``ios_sim`` 去投递，直接吃 ``invalid_platform``。
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
            "platform": P.platform_family(dev.platform),
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
# 同步 App 卸载
# ---------------------------------------------------------------------------
@router.post("/{serial}/apps/uninstall")
async def uninstall_app(
    serial: str,
    body: AppUninstallReq,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    """按设备 serial + 精确包名同步卸载 App。

    HTTP 请求会等待 Agent 完成「查询存在→卸载→再查询确认不存在」。
    本接口只返回卸载结果，不创建异步任务或修改应用分发记录。
    """
    dev = await session.get(Device, serial)
    if dev is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "DEVICE_NOT_FOUND", "message": "无法找到对应的设备"},
        )
    if dev.status != "online":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DEVICE_NOT_READY",
                "message": f"设备当前状态为 {dev.status}，不能卸载 App",
            },
        )

    readiness = (hub.get_device_extra(serial) or {}).get("readiness") or {}
    if not readiness.get("ready"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DEVICE_NOT_READY",
                "message": str(readiness.get("hint") or "设备尚未就绪"),
            },
        )
    if hub.agent_id_for_serial(serial) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AGENT_UNAVAILABLE", "message": "设备所属 Agent 不在线"},
        )

    request_id = uuid.uuid4().hex[:16]
    holder = f"app-uninstall:{request_id}"
    try:
        lock = await store.acquire(
            serial,
            holder=holder,
            holder_type="job",
            ttl_seconds=APP_UNINSTALL_RESPONSE_TIMEOUT_SEC + 5,
            meta={"action": "app_uninstall", "package_name": body.package_name},
        )
    except LockConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DEVICE_BUSY", "message": str(exc)},
        ) from exc

    try:
        result = await get_app_uninstall_waiter().request(
            hub=hub,
            request_id=request_id,
            serial=serial,
            platform=dev.platform,
            package_name=body.package_name,
            timeout_sec=APP_UNINSTALL_TIMEOUT_SEC,
            wait_timeout_sec=APP_UNINSTALL_RESPONSE_TIMEOUT_SEC,
        )
    except AppUninstallDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AGENT_UNAVAILABLE", "message": str(exc)},
        ) from exc
    except AppUninstallTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "UNINSTALL_TIMEOUT", "message": str(exc)},
        ) from exc
    finally:
        try:
            await store.release(serial, token=lock.token)
        except BadToken:
            # 超时期间若被管理员强制解锁/换锁，不要用清理异常覆盖真实卸载结果。
            logger.warning(
                "app_uninstall release lock token mismatch request_id={} serial={}",
                request_id,
                serial,
            )

    if bool(result.get("success")):
        return {
            "success": True,
            "serial": serial,
            "platform": P.platform_family(dev.platform),
            "package_name": body.package_name,
            "message": str(result.get("message") or "卸载成功"),
        }

    reason = str(result.get("reason") or "uninstall_failed").strip().lower()
    message = str(result.get("message") or "卸载失败")
    if reason == "timeout":
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": "UNINSTALL_TIMEOUT", "message": message},
        )
    code = "APP_NOT_FOUND" if reason == "app_not_found" else "UNINSTALL_FAILED"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": code, "message": message},
    )


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
def _require_device_lock(store: DeviceLockStore, serial: str, token: str) -> None:
    lock = store.peek(serial)
    if lock is None:
        raise HTTPException(409, "设备未被占用，请先 acquire 锁")
    if lock.token != token:
        raise HTTPException(403, "lock_token 不匹配，你不是当前持有者")


def _android_display_target(hub: Hub, vm: Optional[AndroidVmInstance], serial: str) -> tuple[str, bool]:
    extra = hub.get_device_extra(serial)
    capability = extra.get("android_vm_display")
    agent_id = hub.agent_id_for_serial(serial)
    if (vm is None or vm.adb_serial != serial or vm.state != "running" or not agent_id
            or vm.assigned_agent_id != agent_id or extra.get("vm_instance_id") != vm.id
            or not isinstance(capability, dict) or not hub.agent_supports(agent_id, P.CAP_ANDROID_VM_DISPLAY)):
        raise HTTPException(409, "当前设备不支持 Android 虚拟机屏幕控制")
    require_owner_host(vm, hub)
    require_idle(vm, allow_running=True)
    return agent_id, bool(capability.get("can_fold") and (vm.config_json or {}).get("fold"))


def _native_display_target(
    hub: Hub, vm: HarmonyVmInstance | None, serial: str,
) -> str:
    extra = hub.get_device_extra(serial)
    agent_id = hub.agent_id_for_serial(serial)
    if (vm is None or serial != f"harmony-vm:{vm.id}" or extra.get("hdc_serial") != vm.hdc_serial
            or vm.state != "running" or not agent_id
            or vm.assigned_agent_id != agent_id or extra.get("vm_instance_id") != vm.id
            or not hub.agent_supports(agent_id, P.CAP_NATIVE_VM_DISPLAY)):
        raise HTTPException(409, "当前设备不支持原生虚拟机屏幕控制")
    require_owner_host(vm, hub)
    require_idle(vm, allow_running=True)
    return agent_id


@router.post("/{serial}/native-display")
async def native_vm_display(
    serial: str,
    body: NativeVmDisplayReq,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> dict:
    lock_key = f"harmony-vm:{body.vm_id}"
    changes = sum(value is not None for value in (body.rotation, body.fold_state, body.rotation_locked))
    if changes > 1 or body.orientation is not None:
        raise HTTPException(422, "请选择该模拟器支持的一个原生操作")
    async with vm_display_lock(lock_key):
        _require_device_lock(store, serial, body.lock_token)
        vm = await session.get(HarmonyVmInstance, body.vm_id)
        agent_id = _native_display_target(hub, vm, serial)
        operation_id = (vm_runtime(vm).get("operation") or {}).get("request_id")
        await session.rollback()
        result = await request_vm_display(
            hub, agent_id=agent_id, vm_id=body.vm_id, serial=serial,
            orientation=None, fold_state=body.fold_state,
            rotation=body.rotation, platform=body.platform, rotation_locked=body.rotation_locked,
        )
        display = result.get("display") if result.get("ok") else None
        body_rotation = display.get("body_rotation") if isinstance(display, dict) else None
        if (not isinstance(display, dict)
                or (body.rotation and (not display.get("can_rotate")
                                       or type(body_rotation) is not int or body_rotation not in range(4)))
                or (body.fold_state and display.get("fold_state") != body.fold_state)
                or (body.rotation_locked is not None and display.get("rotation_locked") is not body.rotation_locked)):
            raise HTTPException(409, result.get("error") or "模拟器未确认屏幕操作")
        if changes:
            await transaction_lock(session, lock_key)
            vm = await session.get(HarmonyVmInstance, body.vm_id, populate_existing=True)
            if (_native_display_target(hub, vm, serial) != agent_id
                    or (vm_runtime(vm).get("operation") or {}).get("request_id") != operation_id):
                raise HTTPException(409, "虚拟机已重新启动，请刷新后重试")
            if body.rotation_locked is not None:
                await session.rollback()
                return display
            config = dict(vm.config_json or {})
            if body.rotation:
                config["initial_rotation"] = body_rotation
            if body.fold_state:
                config["fold"] = {**(config.get("fold") or {}), "initial_state": display["fold_state"]}
            vm.config_json = config
            await session.commit()
        return display


@router.post("/{serial}/android-display")
async def android_display(
    serial: str,
    body: AndroidDisplayReq,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> dict:
    if body.orientation is not None and body.rotation is not None:
        raise HTTPException(422, "指定方向和左右转只能选一项")
    async with vm_display_lock(body.vm_id):
        _require_device_lock(store, serial, body.lock_token)
        vm = await session.get(AndroidVmInstance, body.vm_id)
        agent_id, can_fold = _android_display_target(hub, vm, serial)
        if body.fold_state and not can_fold:
            raise HTTPException(409, "当前 Android 虚拟机不支持折叠")
        operation_id = (vm_runtime(vm).get("operation") or {}).get("request_id")
        # Agent reconnect reconciliation must be able to finish before its screen reply.
        await session.rollback()
        changing = any(value is not None for value in (body.orientation, body.fold_state, body.rotation))
        result = await request_vm_display(
            hub, agent_id=agent_id, vm_id=body.vm_id, serial=serial,
            orientation=body.orientation, fold_state=body.fold_state, rotation=body.rotation,
        )
        display = result.get("display") if result.get("ok") else None
        confirmed = (
            isinstance(display, dict)
            and "orientation" in display
            and display.get("orientation") in (None, *get_args(AndroidDisplayOrientation))
            and (body.rotation is None or display["orientation"] is not None)
            and display.get("fold_state") in ({"folded", "unfolded"} if can_fold else {""})
            and (body.orientation is None or display["orientation"] == body.orientation
                 or (body.orientation == "landscape" and display["orientation"] in ("landscape_left", "landscape_right")))
            and (body.fold_state is None or display["fold_state"] == body.fold_state)
            and all(isinstance(display.get(axis), int) and display[axis] > 0 for axis in ("width", "height"))
        )
        if changing and confirmed:
            await transaction_lock(session, f"android-vm:{body.vm_id}")
            vm = await session.get(AndroidVmInstance, body.vm_id, populate_existing=True)
            if (_android_display_target(hub, vm, serial) != (agent_id, can_fold)
                    or (vm_runtime(vm).get("operation") or {}).get("request_id") != operation_id):
                raise HTTPException(409, "Android 虚拟机已重新启动，请刷新后重试")
            save_vm_display(vm, display)
            await session.commit()
        stream_id = ""
        if (changing and (confirmed or result.get("reason") == "display_failed")
                and hub.agent_id_for_serial(serial) == agent_id and hub.subscriber_count(serial)):
            next_stream_id = result["request_id"]
            sent = await send_operation_once(hub, agent_id, {
                "type": P.MSG_START_MIRROR, "serial": serial, "vm_id": body.vm_id,
                "refresh_stream_id": next_stream_id,
            })
            if sent:
                stream_id = next_stream_id
        if not confirmed:
            raise HTTPException(409, {
                "message": result.get("error") or "AVD 未确认目标机身姿态",
                "stream_id": stream_id,
            })
        return {**display, "stream_id": stream_id}


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
    _require_device_lock(store, serial, body.lock_token)
    ok = await _hub.send_to_serial(
        serial,
        {
            "type": P.MSG_INPUT,
            "serial": serial,
            "kind": body.kind,
            "params": body.params or {},
            **({"frame": body.frame.model_dump()} if body.frame is not None else {}),
        },
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="派发到 Agent 失败",
        )
    return {"dispatched": True}
