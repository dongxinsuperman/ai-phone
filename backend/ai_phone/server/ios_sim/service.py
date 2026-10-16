"""iOS 虚拟机的 Server 侧编排：状态机、Agent 探查等待器、状态与对账处理。

对标 ``harmony_vm/service.py``，但**明显更薄**——鸿蒙那边最重的三块这里都不需要
（方案 §6.5.5）：

```text
鸿蒙有                             iOS 虚拟机
────────────────────────────────────────────────────────────
HDC 端口池 + 租约表 + lease_token   不需要：serial 是 UDID 天然全局唯一，
  + quarantine 隔离                端口是 Agent 本机事务
serial_for_port(port) 映射          不需要：UDID 由 simctl 生成
excluded_ports_for(agent)          不需要：Server 不分配端口
按 lease_token 校验状态上报          不需要：按 vm_id + assigned_agent_id 校验即可
```

首次下发只记录临时目标；收到对应 running 回包后才建立绑定。
启动、停止各执行一次原生操作，有固定截止时间；失败与重启不会自动续做。
已绑定实例保留 UDID 与私有数据；离线仅表示事实待核对。
删除立即退役 Server 配置，在线宿主尽力清理，离线残留由下次孤儿对账处理。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..hub import Hub
from ..vm_probe import CapabilityProbeWaiter
from ..vm_lifecycle import (
    record_creation_provenance, proves_live_managed_instance, send_operation_once,
    expire_operation_if_due,
    is_abandoned_cleanup_receipt, record_abandoned_cleanup_receipt,
    accept_owner, accept_status, bind_owner, report_owner_conflict, cleanup_abandoned_first_bind,
    finish_operation, maintenance, runtime as lifecycle_runtime,
    transaction_lock, update_runtime, host_id as agent_host_id, fail_attempt,
)
from ..models import DeviceAlias
from .models import IosSimVmInstance


# 与 Android 的 ACTIVE_STATES 对齐。刻意**不**照抄鸿蒙那份含 probing / creating /
# connecting_hdc 的长枚举——鸿蒙那些值在代码里从未被赋值，属于预留/文档态；
# 只保留 Agent 实际会上报的状态，避免出现"永远不会出现的状态"。
ACTIVE_STATES = {"starting", "running", "stopping"}
TERMINAL_STATES = {"draft", "stopped", "error", "unavailable"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def vm_payload(vm: IosSimVmInstance, *, request_id: str = "") -> Dict[str, Any]:
    """构造下发给 Agent 的载荷。

    只带 Agent 真正需要的字段——机型与 runtime 的 identifier。**不下发端口**：
    端口由 Agent 本机分配（方案 §6.5.5），Server 下发端口只会造成两边不一致。
    """
    payload: Dict[str, Any] = {
        "vm_id": vm.id,
        "name": vm.name,
        "alias": vm.alias,
        "device_type": vm.device_type,
        "device_type_name": vm.device_type_name,
        "runtime": vm.runtime,
        "runtime_name": vm.runtime_name,
        "os_version": vm.os_version,
        "config_json": vm.config_json or {},
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


async def get_vm_or_404(session: AsyncSession, vm_id: str) -> IosSimVmInstance:
    vm = await session.get(IosSimVmInstance, vm_id, populate_existing=True)
    if vm is None:
        raise LookupError("ios simulator vm not found")
    return vm


def placeholder_serial(vm_id: str) -> str:
    """实例没在跑时，用这个合成 serial 占住别名。

    与鸿蒙 ``harmony-vm:{vm_id}`` 同构。为什么需要占位：别名表是按 serial 存的，
    而虚拟机只有启动后才有 UDID。没有占位符，草稿态的别名就无处安放，别人能把它
    抢走，等这台真启动时才发现撞名。
    """
    return f"ios-sim:{vm_id}"


async def ensure_alias_available(
    session: AsyncSession, alias: str, *, exclude_vm_id: str = ""
) -> None:
    """别名必须**全平台**唯一，不只是 iOS 虚拟机内部唯一。

    与鸿蒙同构，检查两处：

    1. 共享的 ``device_aliases`` 表——真机、Android 虚拟机、鸿蒙虚拟机运行时都写这里
    2. 其它 iOS 虚拟机实例（草稿态还没写进共享表，只查共享表会漏）

    别名是人给设备起的名字，跨平台重名会让调度和人工识别都出错。
    """
    normalized = (alias or "").strip()
    if not normalized:
        raise ValueError("alias is required")

    stmt = select(IosSimVmInstance).where(IosSimVmInstance.alias == normalized)
    if exclude_vm_id:
        stmt = stmt.where(IosSimVmInstance.id != exclude_vm_id)
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        raise ValueError(f"别名已被占用：{normalized}（vm_id={existing.id}）")

    row = (
        await session.execute(select(DeviceAlias).where(DeviceAlias.alias == normalized))
    ).scalar_one_or_none()
    if row is None:
        return
    if exclude_vm_id:
        vm = await session.get(IosSimVmInstance, exclude_vm_id)
        owned = {placeholder_serial(exclude_vm_id)}
        if vm is not None and vm.udid:
            owned.add(vm.udid)
        if row.serial in owned:
            return
    raise ValueError(f"别名已被占用：{normalized}（已绑定设备 {row.serial}）")


# ---------------------------------------------------------------------------
# 别名与设备的绑定
# ---------------------------------------------------------------------------
# 别名要在设备总览的卡片上显示，就必须写进共享的 ``device_aliases`` 表——那张表是
# 按 serial 索引的。虚拟机的 serial 是 UDID，而 UDID 只有启动后才存在，所以沿用
# 鸿蒙那套两段式：没跑时挂在占位 serial 上，跑起来后移到真 UDID 上。
async def reserve_placeholder_alias(session: AsyncSession, vm: IosSimVmInstance) -> None:
    """把别名挂到占位 serial 上（创建、停止、出错时）。"""
    alias = (vm.alias or vm.name or "").strip()
    if not alias:
        return
    await _replace_owned_alias(session, vm, placeholder_serial(vm.id), alias)


async def point_alias_to_runtime(
    session: AsyncSession, vm: IosSimVmInstance, udid: str
) -> None:
    """实例跑起来后，把别名从占位符移到真实 UDID 上。"""
    await _replace_owned_alias(session, vm, udid, vm.alias)


async def _replace_owned_alias(
    session: AsyncSession,
    vm: IosSimVmInstance,
    target_serial: str,
    target_alias: str,
) -> None:
    alias = (target_alias or "").strip()
    serial = (target_serial or "").strip()
    if not alias or not serial:
        return
    by_alias = (
        await session.execute(select(DeviceAlias).where(DeviceAlias.alias == alias))
    ).scalar_one_or_none()
    owned_serials = {placeholder_serial(vm.id)}
    if vm.udid:
        owned_serials.add(vm.udid)
    if by_alias is not None and by_alias.serial not in owned_serials:
        raise ValueError(f"别名已被占用：{alias}（已绑定设备 {by_alias.serial}）")

    by_serial = await session.get(DeviceAlias, serial)
    if by_serial is not None and by_serial.alias != alias:
        raise ValueError(f"设备 {serial} 已有别名：{by_serial.alias}")

    for owned in owned_serials:
        row = await session.get(DeviceAlias, owned)
        if row is not None and row.serial != serial:
            await session.delete(row)
    await session.flush()
    current = await session.get(DeviceAlias, serial)
    if current is None:
        session.add(DeviceAlias(serial=serial, alias=alias, note=""))
    else:
        current.alias = alias
        current.note = ""
    await session.flush()


async def _try_bind_alias(
    session: AsyncSession, vm: IosSimVmInstance, target_serial: str
) -> None:
    """状态上报路径专用：绑定失败只记日志，不打断状态机。

    别名冲突是配置问题（比如别人抢了同名），不该让一台已经跑起来的虚拟机
    因此卡在错误状态——那会让用户完全看不懂发生了什么。
    """
    try:
        await _replace_owned_alias(session, vm, target_serial, vm.alias)
    except ValueError as exc:
        logger.warning(
            "iOS 虚拟机别名绑定失败 vm_id={} serial={}（不影响实例状态）：{}",
            vm.id, target_serial, exc,
        )


async def delete_owned_alias(session: AsyncSession, vm: IosSimVmInstance) -> int:
    """删除本实例名下的别名行（占位与真实 UDID 两处都清）。"""
    deleted = 0
    for serial in {placeholder_serial(vm.id), *( [vm.udid] if vm.udid else [] )}:
        row = await session.get(DeviceAlias, serial)
        if row is not None and row.alias == vm.alias:
            await session.delete(row)
            deleted += 1
    await session.flush()
    return deleted


async def reset_vm_states_on_startup(session: AsyncSession) -> int:
    rows = list((await session.execute(select(IosSimVmInstance).where(IosSimVmInstance.state != "draft"))).scalars().all())
    for vm in rows:
        op = lifecycle_runtime(vm).get("operation") or {}
        if op.get("pending"):
            fail_attempt(vm, "server_restarted", "服务重启，本次未完成操作已结束；不会自动重试")
        elif vm.assigned_agent_id:
            vm.state = "agent_offline"
    return len(rows)

async def mark_agent_vms_offline(agent_id: str, *, hub: Optional[Hub] = None) -> int:
    # Probe invalidation is synchronous in the exact-socket disconnect hook;
    # an old DB cleanup must not erase a reconnected session's fresh result.
    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(select(IosSimVmInstance).where(
            (IosSimVmInstance.assigned_agent_id == agent_id) |
            (IosSimVmInstance.runtime_state["operation"]["target_agent_id"].as_string() == agent_id)
        ))).scalars().all())
        changed = 0
        for vm in sorted(rows, key=lambda row: row.id):
            await transaction_lock(session, f"ios-vm:{vm.id}")
            vm = await session.get(IosSimVmInstance, vm.id, populate_existing=True)
            if vm is None:
                continue
            op = lifecycle_runtime(vm).get("operation") or {}
            # Same-ID reconnect can happen during the earlier DB awaits.
            # Keep this guard under the same VM lock used by start/status.
            if hub is not None and hub.has_agent(agent_id):
                continue
            if op.get("pending") and op.get("target_agent_id") == agent_id:
                fail_attempt(vm, "agent_disconnected", "Agent 断开，本次操作已结束；不会自动重试")
            elif vm.assigned_agent_id == agent_id:
                vm.state, vm.error_code, vm.error_message = "agent_offline", "agent_offline", f"agent offline: {agent_id}"
            else:
                continue
            await _try_bind_alias(session, vm, placeholder_serial(vm.id))
            changed += 1
        if rows:
            await session.commit()
        return changed

async def handle_vm_status(agent_id: str, msg: Dict[str, Any], hub: Optional[Hub] = None) -> None:
    vm_id = str(msg.get("vm_id") or "")
    if not vm_id:
        return
    state, reason = str(msg.get("state") or ""), str(msg.get("reason") or "")
    udid = str(msg.get("udid") or "")
    details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
    factory = get_session_factory()
    async with factory() as session:
        await transaction_lock(session, f"ios-vm:{vm_id}")
        vm = await session.get(IosSimVmInstance, vm_id)
        if vm is None:
            if hub and (reason == "reclaimed" or proves_live_managed_instance(vm_id, msg, "ios")):
                await send_operation_once(hub, agent_id, {"type": P.MSG_IOS_SIM_VM_DELETE,
                    "request_id": uuid.uuid4().hex[:16], "vm_id": vm_id, "udid": udid})
            return
        legacy = bool(reason in {"reclaimed", "wda_not_ready"} and udid and vm.udid == udid)
        if not accept_owner(vm, agent_id, hub, legacy_evidence=legacy):
            if reason in {"reclaimed", "wda_not_ready"}:
                await report_owner_conflict(hub, vm, agent_id, details, msg)
            return
        record_creation_provenance(vm, msg)
        expired = expire_operation_if_due(vm, hub)
        if is_abandoned_cleanup_receipt(vm, msg):
            maintenance(hub, vm, False)
            vm.udid, vm.wda_port, vm.mjpeg_port = None, None, None
            record_abandoned_cleanup_receipt(vm)
            await session.commit()
            return
        if not accept_status(vm, msg):
            expired = expire_operation_if_due(vm, hub) or expired
            cleaned = await cleanup_abandoned_first_bind(hub, vm, agent_id, details, msg)
            if expired or cleaned:
                await session.commit()
            return
        ok = bool(msg.get("ok", True))
        if udid and vm.udid and udid != vm.udid:
            state, ok, reason = "error", False, "udid_mismatch"
        elif udid:
            vm.udid = udid
            update_runtime(vm, instance_created=True)
        if not ok:
            state = "error"
        if state not in {"running", "stopped", "starting", "stopping", "error", "unavailable"}:
            return
        vm.state = state
        vm.error_code = "" if ok else reason
        vm.error_message = "" if ok else str(msg.get("error") or reason)
        if state == "running" and ok:
            bind_owner(vm, agent_id, hub)
        if state == "running" and ok:
            update_runtime(vm, instance_created=True)
            vm.started_at = vm.started_at or now_utc()
            vm.stopped_at = None
            for key in ("wda_port", "mjpeg_port"):
                if isinstance(details.get(key), int):
                    setattr(vm, key, details[key])
            if vm.udid:
                await _try_bind_alias(session, vm, vm.udid)
        elif state == "stopped" and ok:
            vm.wda_port, vm.mjpeg_port = None, None
            vm.stopped_at = now_utc()
            await _try_bind_alias(session, vm, placeholder_serial(vm.id))
        update_runtime(vm, last_reason=reason, last_state=state, last_agent_id=agent_id,
                       details=details, updated_at=now_utc().isoformat())
        finish_operation(vm, {**msg, "state": state})
        maintenance(hub, vm, not (state == "running" and ok))
        op = lifecycle_runtime(vm).get("operation") or {}
        if op.get("first_bind") and op.get("outcome") == "failed" and not ok:
            vm.assigned_agent_id = None
            if details.get("instance_deleted") or (details.get("cleanup_confirmed") and details.get("instance_created") is False):
                maintenance(hub, vm, False)
                vm.udid, vm.wda_port, vm.mjpeg_port = None, None, None
                update_runtime(vm, instance_created=False)
        await session.commit()


async def handle_vm_reconcile(agent_id: str, msg: Dict[str, Any], hub: Optional[Hub] = None) -> None:
    reported = {str(x) for x in (msg.get("vm_ids") or []) if str(x)}
    running = {str(x) for x in (msg.get("running_vm_ids") or []) if str(x)}
    stopped = {str(x) for x in (msg.get("stopped_vm_ids") or []) if str(x)}
    reported |= running | stopped
    unknown = set(msg.get("unknown_vm_ids") or [])
    factory = get_session_factory()
    async with factory() as session:
        reporter_host = agent_host_id(hub, agent_id)
        owner_query = (IosSimVmInstance.assigned_agent_id == agent_id) | IosSimVmInstance.id.in_(reported)
        if reporter_host:
            owner_query |= IosSimVmInstance.runtime_state["owner_host_id"].as_string() == reporter_host
        rows = list((await session.execute(select(IosSimVmInstance).where(owner_query))).scalars().all())
        known = {vm.id for vm in rows}
        for vm in sorted(rows, key=lambda row: row.id):
            await transaction_lock(session, f"ios-vm:{vm.id}")
            vm = await session.get(IosSimVmInstance, vm.id, populate_existing=True)
            if vm is None:
                continue
            proof = (msg.get("instance_proofs") or {}).get(vm.id) or {}
            op = lifecycle_runtime(vm).get("operation") or {}
            if not vm.assigned_agent_id and op.get("first_bind") and not op.get("pending"):
                await cleanup_abandoned_first_bind(hub, vm, agent_id, proof)
                continue
            legacy = bool(msg.get("inventory_complete") and vm.id in reported and proof.get("udid")
                          and proof.get("name") == f"aiphone_sim_{vm.id}"
                          and (not vm.udid or vm.udid == proof.get("udid")))
            if not accept_owner(vm, agent_id, hub, legacy_evidence=legacy):
                if msg.get("inventory_complete"):
                    await report_owner_conflict(hub, vm, agent_id, proof)
                continue
            if legacy and not vm.udid:
                vm.udid = proof["udid"]
            if vm.id in reported:
                update_runtime(vm, instance_created=True)
                if msg.get("inventory_complete") is True and vm.id in stopped and vm.id not in unknown and vm.state not in {"starting", "stopping"} and not (vm.state == "running" and lifecycle_runtime(vm).get("operation")):
                    vm.state, vm.error_code, vm.error_message = "stopped", "", ""
                    vm.wda_port, vm.mjpeg_port = None, None
                    vm.stopped_at = vm.stopped_at or now_utc()
                    await _try_bind_alias(session, vm, placeholder_serial(vm.id))
                    finish_operation(vm, {"state": "stopped"})
                    maintenance(hub, vm, True)
            elif msg.get("inventory_complete") is True and vm.id not in unknown and vm.state not in {"draft", "starting", "stopping"}:
                vm.state, vm.error_code = "error", "instance_missing"
                vm.error_message = "Agent 未发现原实例；未自动重建"
        await session.commit()
    if hub:
        for vm_id in reported - known:
            await send_operation_once(hub, agent_id, {"type": P.MSG_IOS_SIM_VM_DELETE,
                "request_id": uuid.uuid4().hex[:16], "vm_id": vm_id, "udid": ""})


class IosSimCapabilityWaiter(CapabilityProbeWaiter):
    def __init__(self) -> None:
        super().__init__(P.MSG_IOS_SIM_VM_CAPABILITY_PROBE, vm_payload,
            sort_rows=False, timeout_reason="探查超时，Agent 未回应",
            failure_warning=True, cache_latest=True)


_waiter: Optional[IosSimCapabilityWaiter] = None


def get_capability_waiter() -> IosSimCapabilityWaiter:
    global _waiter
    if _waiter is None:
        _waiter = IosSimCapabilityWaiter()
    return _waiter


def reset_capability_waiter_for_tests() -> None:
    global _waiter
    _waiter = None


__all__ = [
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "IosSimCapabilityWaiter",
    "delete_owned_alias",
    "ensure_alias_available",
    "get_capability_waiter",
    "get_vm_or_404",
    "handle_vm_reconcile",
    "handle_vm_status",
    "mark_agent_vms_offline",
    "now_utc",
    "placeholder_serial",
    "point_alias_to_runtime",
    "reserve_placeholder_alias",
    "reset_capability_waiter_for_tests",
    "reset_vm_states_on_startup",
    "vm_payload",
]
