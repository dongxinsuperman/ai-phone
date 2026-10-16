"""iOS 虚拟机的内部 REST 接口。

路由前缀独立（``/api/internal/ios-sim``），与 Android 的 ``/api/internal/vm``、
鸿蒙的 ``/api/internal/harmony-vm`` 并列，互不复用（方案 §0.3 的隔离原则）。

暴露的操作严格对齐三端右侧卡片那六个，不多不少（方案 §6.5.1）：

```text
探查   POST /instances/{id}/dispatch-candidates
下发   POST /instances/{id}/dispatch
启动   POST /instances/{id}/start
停止   POST /instances/{id}/stop
复制   POST /instances/{id}/copy
删除   DELETE /instances/{id}
```

**没有** erase / wipe 端点——三端统一，不为 iOS 搞特殊化。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..api._deps import DBSession, HubDep
from ..api.submissions import RequireBearer
from ..hub import Hub
from ..vm_probe import probe_stream_response
from ..vm_lifecycle import (
    vm_view, require_action, require_editable,
    ensure_start_observation, start_preflight_snapshot, reject_start_preflight,
    operation_failure_detail,
    is_untouched_draft, record_failure,
    require_retirement_allowed, send_operation_once,
    begin_operation, maintenance, require_startable,
    require_device_idle, require_owner_host, runtime as lifecycle_runtime,
    transaction_lock, record_send_failure, delete_device_record, forget_device_route,
    cleanup_command, send_cleanup_once, cancel_operation_timeout, arm_operation_timeout,
    host_id as agent_host_id, update_runtime,
)
from . import catalog as cat
from .models import IosSimCatalogSnapshot, IosSimVmInstance
from .schemas import IosSimVmCreateReq, IosSimVmDispatchReq, IosSimVmPatchReq
from .service import (
    delete_owned_alias,
    ensure_alias_available,
    get_capability_waiter,
    get_vm_or_404,
    point_alias_to_runtime,
    reserve_placeholder_alias,
    vm_payload,
)


def _require_ios_sim_db(request: Request) -> None:
    """DDL 失败时明确 503，而不是抛一堆 SQL 错误。

    与鸿蒙同构：这项能力的表是隔离创建的，失败只影响自己。
    """
    if getattr(request.app.state, "ios_sim_db_ready", True) is False:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="iOS 虚拟机数据表初始化失败；现有设备与 Android / 鸿蒙链路不受影响",
        )


router = APIRouter(
    prefix="/api/internal/ios-sim/instances",
    tags=["internal-ios-sim"],
    dependencies=[RequireBearer, Depends(_require_ios_sim_db)],
)
catalog_router = APIRouter(
    prefix="/api/internal/ios-sim",
    tags=["internal-ios-sim"],
    dependencies=[RequireBearer, Depends(_require_ios_sim_db)],
)

REDISPATCH_REQUIRED = "所属 Agent 已离线，请重新探查并下发"


def _request_id() -> str:
    return uuid.uuid4().hex[:16]


async def _resolve_names(
    session: AsyncSession, device_type: str, runtime: str
) -> Dict[str, str]:
    """把 identifier 补成可读名，并做 Server 侧的组合预校验。

    **这只是预校验**，用于在下发前挡掉明显不可能的组合、省一次无效往返。
    最终判据是 Agent 探查——那边用 runtime 自带的 ``supportedDeviceTypes``，
    是苹果直给的权威表（方案 §6.5.4）。两者冲突时以 Agent 为准。
    """
    row = await session.get(IosSimCatalogSnapshot, "official")
    payload = row.payload if row is not None else None
    dt = cat.find_device_type(device_type, payload)
    if dt is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"官方目录里没有该机型：{device_type}",
        )
    # runtime identifier 形如 ...SimRuntime.iOS-26-0 → 版本串 26.0
    tail = runtime.rsplit(".", 1)[-1]
    parts = tail.split("-")
    version = ".".join(parts[1:]) if len(parts) >= 2 else ""
    runtime_name = f"{parts[0]} {version}" if version else tail
    if version and not cat.supports_version(dt, version):
        # 苹果用 65535.255.255 表示「没有上限」，不是空值——直接拼进提示会变成
        # 一串没人看得懂的数字。
        max_str = str(dt.get("max_runtime_version_string") or "")
        max_label = "无上限" if not max_str or max_str.startswith("65535.") else max_str
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                f"机型「{dt.get('name')}」不支持 {runtime_name}"
                f"（官方支持区间 {dt.get('min_runtime_version_string')} ~ {max_label}）"
            ),
        )
    return {
        "device_type_name": str(dt.get("name") or ""),
        "runtime_name": runtime_name,
        "os_version": version,
    }


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
@catalog_router.get("/catalog")
async def get_catalog(session: AsyncSession = DBSession) -> Dict[str, Any]:
    """官方机型目录。前端左侧选择器用。

    只回答「有哪些机型、各自支持哪些系统版本」。**「这台 Agent 装了哪些 runtime」
    不在这里**——那要靠探查（方案 §6.5.4.1），因为每台 Agent 装的 Xcode 与 runtime
    都不同，Server 存快照反而会失真。
    """
    row = await session.get(IosSimCatalogSnapshot, "official")
    if row is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="官方机型目录尚未导入；请重启 Server 或检查 official_catalog.json",
        )
    return row.to_dict()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@catalog_router.get("/health")
async def health() -> Dict[str, Any]:
    summary = cat.catalog_summary()
    return {"ok": True, "catalog": summary}


@router.get("")
async def list_instances(session: AsyncSession = DBSession, hub: Hub = HubDep) -> List[Dict[str, Any]]:
    rows = (
        await session.execute(
            select(IosSimVmInstance).order_by(IosSimVmInstance.created_at.desc())
        )
    ).scalars().all()
    return [vm_view(row, hub) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_instance(
    body: IosSimVmCreateReq, session: AsyncSession = DBSession, hub: Hub = HubDep
) -> Dict[str, Any]:
    alias = body.alias.strip()
    try:
        await ensure_alias_available(session, alias)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    names = await _resolve_names(session, body.device_type, body.runtime)
    vm = IosSimVmInstance(
        name=(body.name or alias).strip(),
        alias=alias,
        device_type=body.device_type,
        runtime=body.runtime,
        config_json=body.config_json or {},
        state="draft",
        **names,
    )
    session.add(vm)
    await session.flush()
    # 草稿态还没有 UDID，先用占位 serial 占住别名，避免被别人抢走
    await reserve_placeholder_alias(session, vm)
    await session.commit()
    await session.refresh(vm)
    logger.info(
        "已创建 iOS 虚拟机配置 vm_id={} alias={} 机型={} 系统={}",
        vm.id, vm.alias, vm.device_type_name, vm.runtime_name,
    )
    return vm_view(vm, hub)


@router.get("/{vm_id}")
async def get_instance(vm_id: str, session: AsyncSession = DBSession, hub: Hub = HubDep) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    return vm_view(vm, hub)


@router.patch("/{vm_id}")
async def patch_instance(
    vm_id: str, body: IosSimVmPatchReq, session: AsyncSession = DBSession, hub: Hub = HubDep
) -> Dict[str, Any]:
    await transaction_lock(session, f"ios-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    require_action(vm, "edit", hub)
    patch = body.model_dump(exclude_unset=True)
    # 别名只是调度与展示身份，运行中可安全更新；机型、系统和其他配置仍要求先停止。
    # 与 Android / 鸿蒙虚拟机保持一致，避免设备明明空闲却无法改名。
    alias_only = set(patch).issubset({"alias"})
    require_editable(vm, hub, alias_only=alias_only)

    # 机型与系统版本由官方目录锁定，不能改。与鸿蒙 CATALOG_LOCKED_FIELDS 一致：
    # 允许在这里改等于绕过整套兼容校验（机型 × 版本区间），能改出一台起不来的配置，
    # 且直到下发才暴露。要换配置就新建一台，不在 PATCH 里放暗道。
    conflicting = [
        key
        for key, incoming in (
            ("device_type", body.device_type),
            ("runtime", body.runtime),
        )
        if incoming is not None and incoming != getattr(vm, key)
    ]
    if conflicting:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "catalog_locked_fields_immutable",
                "message": (
                    "机型与系统版本由官方目录锁定，不能修改："
                    + "、".join(conflicting)
                    + "。请新建一台配置。"
                ),
            },
        )

    if body.alias is not None:
        alias = body.alias.strip()
        try:
            await ensure_alias_available(session, alias, exclude_vm_id=vm_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # 先摘旧别名行再改字段：别名表按 serial 存，旧行认的是旧别名，
        # 顺序反过来会摘不掉，留下一条孤儿映射把新别名卡住。
        await delete_owned_alias(session, vm)
        vm.alias = alias
        vm.name = alias
        try:
            if vm.udid:
                await point_alias_to_runtime(session, vm, vm.udid)
            else:
                await reserve_placeholder_alias(session, vm)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if body.name is not None:
        vm.name = body.name.strip() or vm.alias
    if body.config_json is not None:
        vm.config_json = body.config_json

    await session.commit()
    await session.refresh(vm)
    return vm_view(vm, hub)


@router.delete("/{vm_id}")
async def delete_instance(vm_id: str, session: AsyncSession = DBSession, hub: Hub = HubDep) -> Dict[str, Any]:
    await transaction_lock(session, f"ios-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "实例不存在")
    require_retirement_allowed(hub, vm)
    target, command, alias_deleted = await _retire_configuration(session, vm, hub)
    await session.commit()
    await forget_device_route(hub, vm)
    sent = await send_cleanup_once(hub, vm, target, command)
    return {"id": vm_id, "deleted": True, "alias_deleted": alias_deleted,
            "cleanup_sent": sent, "avd_cleanup_sent": sent}


async def _retire_configuration(session: AsyncSession, vm: IosSimVmInstance, hub: Hub):
    """Remove only ledger state; the caller commits before any network send."""
    target, command = cleanup_command(vm, P.MSG_IOS_SIM_VM_DELETE)
    alias_deleted = await delete_owned_alias(session, vm)
    cancel_operation_timeout(vm)
    maintenance(hub, vm, True)
    await delete_device_record(session, vm)
    await session.delete(vm)
    await session.flush()
    return target, command, alias_deleted

@router.post("/{vm_id}/copy", status_code=status.HTTP_201_CREATED)
async def copy_instance(
    vm_id: str, body: IosSimVmCreateReq, session: AsyncSession = DBSession, hub: Hub = HubDep
) -> Dict[str, Any]:
    """按现有实例的配置新建一台（对齐三端卡片的「复制配置」）。

    只复制配置，**不复制数据**——新实例是一台全新的虚拟机，UDID 与数据目录都是新的。
    """
    try:
        source = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="源实例不存在") from exc
    require_action(source, "copy", hub)
    alias = body.alias.strip()
    try:
        await ensure_alias_available(session, alias)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    vm = IosSimVmInstance(
        name=(body.name or alias).strip(),
        alias=alias,
        device_type=source.device_type,
        device_type_name=source.device_type_name,
        runtime=source.runtime,
        runtime_name=source.runtime_name,
        os_version=source.os_version,
        config_json=dict(source.config_json or {}),
        state="draft",
    )
    session.add(vm)
    await session.flush()
    await reserve_placeholder_alias(session, vm)
    await session.commit()
    await session.refresh(vm)
    logger.info("已复制 iOS 虚拟机配置 {} → {}", vm_id, vm.id)
    return vm_view(vm, hub)


# ---------------------------------------------------------------------------
# 探查 / 下发 / 启停
# ---------------------------------------------------------------------------
@router.post("/{vm_id}/dispatch-candidates")
async def dispatch_candidates(
    vm_id: str,
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
    stream: bool = False,
) -> Dict[str, Any]:
    """向所有在线 Agent 广播探查，返回各自能否承接。

    与 Android / 鸿蒙一致：**Server 不自动选 Agent**，把候选列表给用户选。
    """
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    waiter = get_capability_waiter()
    events = waiter.probe_stream(hub=hub, vm=vm)
    # Payload/snapshot are plain data now; do not pin a DB connection while
    # slow Agents are being probed (including the legacy JSON collector).
    await session.close()
    if stream:
        return probe_stream_response(events)
    return await waiter.collect_probe(events)


@router.post("/{vm_id}/dispatch")
async def dispatch(vm_id: str, body: IosSimVmDispatchReq,
                   session: AsyncSession = DBSession, hub: Hub = HubDep) -> Dict[str, Any]:
    return await _send_start(vm_id, body.agent_id.strip(), session, hub, probe=True)


async def _send_start(vm_id: str, agent_id: str, session: AsyncSession, hub: Hub, *, probe: bool = False, expected_draft: bool = False, allow_switch: bool = True) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
        await session.refresh(vm)
    except LookupError:
        raise HTTPException(404, "实例不存在")
    if expected_draft and not is_untouched_draft(vm):
        raise HTTPException(409, "新配置已被另一操作接管，本次换机不会重复启动")
    require_action(vm, "edit", hub)
    old_agent = str(vm.assigned_agent_id or "")
    if old_agent and old_agent != agent_id:
        if not allow_switch:
            raise HTTPException(409, "所属 Agent 已变化，本次未启动；请刷新后操作")
        return await _switch_agent(vm=vm, new_agent_id=agent_id, session=session, hub=hub)
    require_startable(vm, hub)
    if not hub.has_agent(agent_id):
        raise HTTPException(409, "所属 Agent 离线，请等待连接")
    expected_preflight = start_preflight_snapshot(vm)
    if probe:
        fresh = await get_capability_waiter().probe_agent(hub=hub, vm=vm, agent_id=agent_id)
        if not fresh.get("ok"):
            await transaction_lock(session, f"ios-vm:{vm_id}")
            vm = await get_vm_or_404(session, vm_id)
            if start_preflight_snapshot(vm) != expected_preflight:
                raise HTTPException(409, operation_failure_detail(vm, "探查期间实例已变化，本次未启动", hub=hub))
            await reject_start_preflight(session, vm, hub, f"该 Agent 当前无法承接：{fresh.get('reason') or '不可用'}", reason="agent_capability_unavailable")
    # Capability replies use the Agent's same receive loop as lifecycle status.
    # Never hold the VM transaction lock while waiting for that reply.
    await transaction_lock(session, f"ios-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "实例已删除")
    if expected_draft and not is_untouched_draft(vm):
        raise HTTPException(409, "新配置已被另一操作接管，本次换机不会重复启动")
    require_startable(vm, hub)
    if start_preflight_snapshot(vm) != expected_preflight:
        raise HTTPException(409, operation_failure_detail(vm, "探查期间配置、归属或操作已变化，本次未启动", hub=hub))
    old_agent = (vm.assigned_agent_id or "").strip()
    if old_agent and old_agent != agent_id:
        if not allow_switch:
            raise HTTPException(409, "所属 Agent 已变化，本次未启动；请刷新后操作")
        return await _switch_agent(vm=vm, new_agent_id=agent_id, session=session, hub=hub)
    require_owner_host(vm, hub)
    vm = await ensure_start_observation(session, vm, hub, get_capability_waiter())
    require_device_idle(hub, vm)
    first_bind = not bool(vm.assigned_agent_id)
    request_id = begin_operation(vm, "start", target_agent_id=agent_id,
        target_host_id=agent_host_id(hub, agent_id), first_bind=first_bind)
    vm.state, vm.error_code, vm.error_message = "starting", "", ""
    payload = {"type": P.MSG_IOS_SIM_VM_START, **vm_payload(vm, request_id=request_id),
               "udid": vm.udid or "", "require_existing_instance": bool(vm.assigned_agent_id or vm.udid or lifecycle_runtime(vm).get("instance_created")),
        "rollback_new_instance": first_bind,
        "operation_timeout_sec": lifecycle_runtime(vm)["operation"]["timeout_sec"],
    }
    maintenance(hub, vm, True)
    await session.commit()
    arm_operation_timeout(vm, hub)
    sent = await send_operation_once(hub, agent_id, payload)
    if not sent:
        await record_send_failure(session, vm, request_id, "下发失败，本次操作已结束")
        current_operation = lifecycle_runtime(vm).get("operation") or {}
        if current_operation.get("request_id") == request_id and current_operation.get("outcome") == "succeeded":
            sent = True
        else:
            raise HTTPException(502, detail=operation_failure_detail(vm, "启动下发失败，本次操作已结束；请查询当前实例与操作结果", hub=hub, request_id=request_id))
    await session.refresh(vm)
    return {"sent": sent, "instance": vm_view(vm, hub)}


async def _switch_agent(*, vm: IosSimVmInstance, new_agent_id: str, session: AsyncSession, hub: Hub) -> Dict[str, Any]:
    await transaction_lock(session, f"ios-vm:{vm.id}")
    try:
        vm = await get_vm_or_404(session, vm.id)
    except LookupError:
        raise HTTPException(404, "实例已删除")
    require_retirement_allowed(hub, vm)
    inherited = {key: getattr(vm, key) for key in ('name', 'alias', 'device_type', 'device_type_name', 'runtime', 'runtime_name', 'os_version', 'config_json')}
    old_vm_id = vm.id
    old_target, old_command, _ = await _retire_configuration(session, vm, hub)
    new_vm = IosSimVmInstance(state="draft", **inherited)
    session.add(new_vm)
    await session.flush()
    await reserve_placeholder_alias(session, new_vm)
    # Retire old ID and establish the replacement configuration atomically.
    # An unreachable old WebSocket cannot leave the ledger with neither row.
    new_vm_id = new_vm.id
    await session.commit()
    await forget_device_route(hub, vm)
    cleanup_sent = await send_cleanup_once(hub, vm, old_target, old_command)
    try:
        result = await _send_start(new_vm_id, new_agent_id, session, hub, expected_draft=True)
        sent = bool(result.get("sent"))
        instance = result.get("instance") or vm_view(new_vm, hub)
    except HTTPException as exc:
        await transaction_lock(session, f"ios-vm:{new_vm_id}")
        current = await session.get(IosSimVmInstance, new_vm_id, populate_existing=True)
        if current is None:
            await session.commit()
            raise HTTPException(409, detail={"message": "新配置已被其他操作删除", "sent": False,
                "switched": True, "old_vm_id": old_vm_id, "vm_id": new_vm_id,
                "instance": None, "request_id": "", "old_cleanup_sent": cleanup_sent}) from exc
        # A different caller may have started/stopped/deleted the visible new
        # configuration while old-host cleanup was in flight. Never overwrite
        # its operation or result with this switch call's stale exception.
        if is_untouched_draft(current):
            current.state = "error"
            current.error_message = f"新配置启动失败：{exc.detail}"
            # The locked replacement is still untouched: no native command
            # was accepted. Keep the failure visible and permit a later
            # explicit dispatch, without resuming this failed switch.
            record_failure(current, current.error_message, reason="switch_target_not_dispatched",
                           action="start", preflight=True)
            update_runtime(current, last_status={"state": "error", "ok": False,
                           "details": {"cleanup_confirmed": True, "instance_created": False}},
                           details={"cleanup_confirmed": True, "instance_created": False})
        raw_detail = exc.detail if isinstance(exc.detail, dict) else {}
        message = str(raw_detail.get("message") or exc.detail)
        detail = operation_failure_detail(current, message, hub=hub, request_id=str(raw_detail.get("request_id") or ""), switched=True,
            old_vm_id=old_vm_id, old_cleanup_sent=cleanup_sent)
        await session.commit()
        # All three legacy switch paths report a conflict when the target
        # cannot be started. This is a partial-effect failure, never HTTP 200.
        raise HTTPException(409, detail=detail) from exc
    return {"sent": sent, "switched": True, "old_vm_id": old_vm_id,
            "old_cleanup_sent": cleanup_sent, "instance": instance}

@router.post("/{vm_id}/start")
async def start(vm_id: str, session: AsyncSession = DBSession, hub: Hub = HubDep) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "实例不存在")
    agent_id = str(vm.assigned_agent_id or "")
    if not agent_id:
        raise HTTPException(409, "实例尚未绑定 Agent，请先探查并下发")
    return await _send_start(vm_id, agent_id, session, hub, allow_switch=False)


@router.post("/{vm_id}/stop")
async def stop(vm_id: str, session: AsyncSession = DBSession, hub: Hub = HubDep) -> Dict[str, Any]:
    await transaction_lock(session, f"ios-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "实例不存在")
    require_action(vm, "stop", hub, idempotent_stop=True)
    agent_id = str(vm.assigned_agent_id or "")
    if not agent_id:
        raise HTTPException(409, "实例尚未绑定 Agent")
    require_owner_host(vm, hub)
    require_device_idle(hub, vm)
    request_id = begin_operation(vm, "stop", target_agent_id=vm.assigned_agent_id,
                                 target_host_id=agent_host_id(hub, vm.assigned_agent_id))
    vm.state = "stopping"
    maintenance(hub, vm, True)
    await session.commit()
    arm_operation_timeout(vm, hub)
    sent = await send_operation_once(hub, agent_id, {"type": P.MSG_IOS_SIM_VM_STOP,
        "request_id": request_id, "vm_id": vm.id, "udid": vm.udid or "",
        "operation_timeout_sec": lifecycle_runtime(vm)["operation"]["timeout_sec"]})
    if not sent:
        await record_send_failure(session, vm, request_id, "所属 Agent 离线，尚未确认停止")
        current_operation = lifecycle_runtime(vm).get("operation") or {}
        if current_operation.get("request_id") == request_id and current_operation.get("outcome") == "succeeded":
            sent = True
        else:
            raise HTTPException(502, detail=operation_failure_detail(vm, "停止下发失败，本次操作已结束；尚未确认实例停止", hub=hub, request_id=request_id))
    await session.refresh(vm)
    return {"sent": sent, "instance": vm_view(vm, hub)}
