"""Server orchestration helpers for managed HarmonyOS Emulator instances."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
from ..models import AndroidVmInstance, DeviceAlias
from .models import HarmonyVmInstance, HarmonyVmPortLease


HDC_PORT_MIN = 10000
HDC_PORT_MAX = 16555
ACTIVE_STATES = {
    "probing",
    "dispatching",
    "creating",
    "starting",
    "connecting_hdc",
    "checking_driver",
    "running",
    "stopping",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_abi(value: str) -> str:
    raw = (value or "auto").strip().lower()
    return "arm64" if raw == "arm64-v8a" else raw


def placeholder_serial(vm_id: str) -> str:
    return f"harmony-vm:{vm_id}"


def serial_for_port(port: int) -> str:
    return f"127.0.0.1:{port}"


def vm_payload(vm: HarmonyVmInstance, *, request_id: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "vm_id": vm.id,
        "name": vm.name,
        "alias": vm.alias,
        "device_type": vm.device_type,
        "os_version": vm.os_version,
        "api_version": vm.api_version,
        "abi": vm.abi,
        "image_id": vm.image_id,
        "screen_profile": vm.screen_profile,
        "screen_width": vm.screen_width,
        "screen_height": vm.screen_height,
        "density": vm.density,
        "screen_size_in": vm.screen_size_in,
        "memory_gb": vm.memory_gb,
        "storage_gb": vm.storage_gb,
        "boot_mode": vm.boot_mode,
        "config_json": vm.config_json or {},
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


async def get_vm_or_404(session: AsyncSession, vm_id: str) -> HarmonyVmInstance:
    vm = await session.get(HarmonyVmInstance, vm_id, populate_existing=True)
    if vm is None:
        raise LookupError("harmony vm not found")
    return vm


async def ensure_alias_available(
    session: AsyncSession,
    *,
    alias: str,
    exclude_vm_id: str = "",
) -> None:
    """Check both Android drafts and the shared alias table.

    Android draft aliases are not inserted into ``DeviceAlias`` until running,
    so checking only the shared table would let Harmony break a later Android
    start.
    """
    normalized = (alias or "").strip()
    if not normalized:
        return
    android = await session.execute(
        select(AndroidVmInstance.id).where(AndroidVmInstance.alias == normalized)
    )
    if android.scalar_one_or_none() is not None:
        raise ValueError("alias_conflict")

    row = (
        await session.execute(select(DeviceAlias).where(DeviceAlias.alias == normalized))
    ).scalar_one_or_none()
    if row is None:
        return
    if exclude_vm_id:
        vm = await session.get(HarmonyVmInstance, exclude_vm_id)
        owned = {placeholder_serial(exclude_vm_id)}
        # An HDC port is reusable.  It only represents this VM's legacy alias
        # while the requested name is still the VM's current name.
        if vm and vm.hdc_serial and vm.alias == normalized:
            owned.add(vm.hdc_serial)
        if row.serial in owned:
            return
    raise ValueError("alias_conflict")


async def reserve_placeholder_alias(session: AsyncSession, vm: HarmonyVmInstance) -> None:
    """Keep the alias on the VM identity in both stopped and running states."""
    alias = (vm.alias or vm.name or "").strip()
    if not alias:
        return
    await _replace_owned_alias(session, vm, alias)


async def _replace_owned_alias(
    session: AsyncSession,
    vm: HarmonyVmInstance,
    target_alias: str,
) -> None:
    alias = (target_alias or "").strip()
    if not alias:
        return
    serial = placeholder_serial(vm.id)
    by_alias = (
        await session.execute(select(DeviceAlias).where(DeviceAlias.alias == alias))
    ).scalar_one_or_none()
    owned_serials = {placeholder_serial(vm.id)}
    # Migrate an existing running VM's old HDC-keyed alias only when its name
    # matches this VM.  Never treat an arbitrary alias on a reused port as
    # owned by the new VM.
    if vm.hdc_serial:
        legacy = await session.get(DeviceAlias, vm.hdc_serial)
        if legacy is not None and legacy.alias == alias:
            owned_serials.add(vm.hdc_serial)
    if by_alias is not None and by_alias.serial not in owned_serials:
        raise ValueError("alias_conflict")

    by_serial = await session.get(DeviceAlias, serial)
    if by_serial is not None and by_serial.alias != alias:
        raise ValueError("serial_alias_conflict")

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


async def delete_owned_alias(session: AsyncSession, vm: HarmonyVmInstance) -> int:
    deleted = 0
    serials = {placeholder_serial(vm.id)}
    if vm.hdc_serial:
        serials.add(vm.hdc_serial)
    for serial in serials:
        row = await session.get(DeviceAlias, serial)
        if row is not None and row.alias == vm.alias:
            await session.delete(row)
            deleted += 1
    await session.flush()
    return deleted


async def allocate_port_lease(
    session: AsyncSession,
    vm: HarmonyVmInstance,
    *,
    agent_id: str,
    excluded_ports: Iterable[int] = (),
) -> HarmonyVmPortLease:
    existing = (
        await session.execute(
            select(HarmonyVmPortLease).where(HarmonyVmPortLease.vm_id == vm.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # A new dispatch must never inherit a previous attempt's port/token.
        # Normal stop has removed the lease; an existing row means cleanup is
        # incomplete or the VM row and lease table have drifted apart.
        raise RuntimeError("harmony_vm_previous_port_lease_not_released")

    excluded = {
        int(port)
        for port in excluded_ports
        if HDC_PORT_MIN <= int(port) <= HDC_PORT_MAX
    }
    # A capability scan cannot prove an old process has released a port.
    # Quarantined leases are released only by confirmed stop/delete replies.
    # 先跳过当前事务已经加载/占用的端口，避免为每个已用端口制造一次
    # 主键冲突；并发请求之间仍由下面的嵌套事务 + PK 唯一约束兜底仲裁。
    occupied = set(
        (
            await session.execute(select(HarmonyVmPortLease.port))
        ).scalars().all()
    )
    for port in range(HDC_PORT_MIN, HDC_PORT_MAX + 1):
        if port in excluded or port in occupied:
            continue
        lease = HarmonyVmPortLease(
            port=port,
            vm_id=vm.id,
            agent_id=agent_id,
            lease_token=uuid.uuid4().hex,
            state="reserved",
        )
        try:
            async with session.begin_nested():
                session.add(lease)
                await session.flush()
        except IntegrityError:
            continue
        vm.hdc_port = port
        vm.hdc_serial = serial_for_port(port)
        vm.lease_token = lease.lease_token
        return lease
    raise RuntimeError("harmony_hdc_port_pool_exhausted")


async def release_port_lease(
    session: AsyncSession,
    vm: HarmonyVmInstance,
    *,
    reason: str,
) -> None:
    lease = None
    if vm.lease_token:
        lease = (
            await session.execute(
                select(HarmonyVmPortLease).where(
                    HarmonyVmPortLease.lease_token == vm.lease_token
                )
            )
        ).scalar_one_or_none()
    history = list((vm.runtime or {}).get("lease_history") or [])
    if vm.lease_token or vm.hdc_port:
        history.append({
            "lease_token": vm.lease_token or "",
            "port": vm.hdc_port,
            "released_at": now_utc().isoformat(),
            "reason": reason,
        })
        vm.runtime = {**(vm.runtime or {}), "lease_history": history[-20:]}
    vm.hdc_port = None
    vm.hdc_serial = None
    vm.lease_token = None
    if lease is not None:
        await session.delete(lease)
    await session.flush()


async def quarantine_current_lease(
    session: AsyncSession,
    vm: HarmonyVmInstance,
    *,
    reason: str,
    error: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    if not vm.lease_token:
        return
    lease = (
        await session.execute(
            select(HarmonyVmPortLease).where(
                HarmonyVmPortLease.lease_token == vm.lease_token
            )
        )
    ).scalar_one_or_none()
    if lease is None:
        return
    lease.state = "quarantined"
    lease.last_error = error[:4000]
    lease.quarantine_reason = reason[:128]
    lease.quarantine_details_json = details or {}
    # Keep the VM link while cleanup is outstanding.


async def mark_agent_vms_offline(agent_id: str, *, hub: Optional[Hub] = None) -> int:
    # Probe invalidation is synchronous in the exact-socket disconnect hook;
    # an old DB cleanup must not erase a reconnected session's fresh result.
    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(select(HarmonyVmInstance).where(
            (HarmonyVmInstance.assigned_agent_id == agent_id) |
            (HarmonyVmInstance.runtime["operation"]["target_agent_id"].as_string() == agent_id)
        ))).scalars().all())
        changed = 0
        for vm in sorted(rows, key=lambda row: row.id):
            await transaction_lock(session, f"harmony-vm:{vm.id}")
            vm = await session.get(HarmonyVmInstance, vm.id, populate_existing=True)
            if vm is None:
                continue
            op = lifecycle_runtime(vm).get("operation") or {}
            # The old socket's DB tail cannot fail a reconnected session's
            # newer operation after acquiring this same per-VM lock.
            if hub is not None and hub.has_agent(agent_id):
                continue
            if op.get("pending") and op.get("target_agent_id") == agent_id:
                fail_attempt(vm, "agent_disconnected", "Agent 断开，本次操作已结束；不会自动重试")
            elif vm.assigned_agent_id == agent_id:
                vm.state, vm.error_code, vm.error_message = "agent_offline", "agent_offline", f"agent offline: {agent_id}"
            else:
                continue
            changed += 1
        if rows:
            await session.commit()
        return changed

async def filter_managed_devices_for_agent(
    agent_id: str, devices: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Accept only the current VM lease under its stable logical identity.

    The HDC serial is a connection address, and can be reused by another VM.
    A managed device therefore enters Device, Hub, and readiness only as
    ``harmony-vm:<vm_id>``.  An untagged report on the *same Agent's* leased
    HDC address is rejected too, so an old rescan cannot recreate the legacy
    physical-keyed device.  Other Agents' ordinary Harmony devices are not
    affected by this local-address check.
    """
    tagged: list[tuple[Dict[str, Any], str, Dict[str, Any]]] = []
    untagged_hdc: set[str] = set()
    for device in devices:
        if str(device.get("platform") or "").strip().lower() != "harmony":
            continue
        extra = device.get("extra") if isinstance(device.get("extra"), dict) else {}
        vm_id = str(extra.get("vm_instance_id") or "").strip()
        serial = str(device.get("serial") or "").strip()
        if vm_id or serial.startswith("harmony-vm:"):
            tagged.append((device, vm_id, extra))
        elif serial.startswith("127.0.0.1:"):
            untagged_hdc.add(serial)
    if not tagged and not untagged_hdc:
        return devices

    accepted: dict[int, Dict[str, Any]] = {}
    tagged_ids = {id(device) for device, _, _ in tagged}
    blocked_hdc: set[str] = set()
    factory = get_session_factory()
    try:
        async with factory() as session:
            for device, vm_id, extra in tagged:
                serial = str(device.get("serial") or "").strip()
                vm = await session.get(HarmonyVmInstance, vm_id) if vm_id else None
                lease = (
                    await session.get(HarmonyVmPortLease, vm.hdc_port)
                    if vm is not None and vm.hdc_port is not None
                    else None
                )
                hdc_serial = str(extra.get("hdc_serial") or "").strip()
                token = str(extra.get("lease_token") or "").strip()
                if (
                    vm is not None
                    and lease is not None
                    and serial == placeholder_serial(vm_id)
                    and hdc_serial == vm.hdc_serial == serial_for_port(lease.port)
                    and token
                    and token == vm.lease_token == lease.lease_token
                    and (vm.assigned_agent_id or "") == agent_id
                    and (lease.agent_id or "") == agent_id
                    and lease.vm_id == vm_id
                    and lease.state in {"reserved", "active"}
                    and vm.state == "running"
                ):
                    # lease_token is an internal authority credential.  Device
                    # extra is exposed by public device-list endpoints.
                    public_extra = {k: v for k, v in extra.items() if k != "lease_token"}
                    accepted[id(device)] = {**device, "extra": public_extra}
                    continue
                logger.error(
                    "拒绝无当前租约的受管 Harmony VM 设备上报：agent={} vm_id={} serial={}",
                    agent_id,
                    vm_id,
                    serial,
                )

            if untagged_hdc:
                rows = (
                    await session.execute(
                        select(HarmonyVmInstance).where(
                            HarmonyVmInstance.assigned_agent_id == agent_id,
                            HarmonyVmInstance.hdc_serial.in_(untagged_hdc),
                        )
                    )
                ).scalars().all()
                for vm in rows:
                    if vm.hdc_port is None or not vm.lease_token:
                        continue
                    lease = await session.get(HarmonyVmPortLease, vm.hdc_port)
                    if (
                        lease is not None
                        and lease.vm_id == vm.id
                        and lease.agent_id == agent_id
                        and lease.lease_token == vm.lease_token
                        and lease.state in {"reserved", "active"}
                        and vm.hdc_serial == serial_for_port(lease.port)
                    ):
                        blocked_hdc.add(vm.hdc_serial)
    except SQLAlchemyError:
        # Missing optional Harmony tables fail closed for tagged VM reports,
        # without disconnecting Android, iOS, or Harmony real devices.
        logger.exception(
            "Harmony VM 身份表不可用：拒绝本次受管 VM 上报，保留普通设备 agent={}",
            agent_id,
        )
        accepted.clear()

    result: list[Dict[str, Any]] = []
    for device in devices:
        serial = str(device.get("serial") or "").strip()
        if id(device) in accepted:
            result.append(accepted[id(device)])
        elif id(device) in tagged_ids:
            continue
        elif (
            str(device.get("platform") or "").strip().lower() == "harmony"
            and serial in blocked_hdc
        ):
            logger.warning(
                "拒绝当前受管 Harmony VM HDC 地址的未标记旁路上报：agent={} serial={}",
                agent_id,
                serial,
            )
        else:
            result.append(device)
    return result


async def reset_vm_states_on_startup(session: AsyncSession) -> int:
    rows = list((await session.execute(select(HarmonyVmInstance).where(HarmonyVmInstance.state != "draft"))).scalars().all())
    for vm in rows:
        op = lifecycle_runtime(vm).get("operation") or {}
        if op.get("pending"):
            fail_attempt(vm, "server_restarted", "服务重启，本次未完成操作已结束；不会自动重试")
        elif vm.assigned_agent_id:
            vm.state = "agent_offline"
    return len(rows)

async def handle_vm_status(agent_id: str, msg: Dict[str, Any], hub: Optional[Hub] = None) -> None:
    vm_id = str(msg.get("vm_id") or "")
    if not vm_id:
        return
    factory = get_session_factory()
    async with factory() as session:
        await transaction_lock(session, f"harmony-vm:{vm_id}")
        vm = await session.get(HarmonyVmInstance, vm_id)
        reason = str(msg.get("reason") or "")
        token = str(msg.get("lease_token") or "")
        serial = str(msg.get("hdc_serial") or "")
        if vm is None:
            if hub and (reason == "reclaimed" or proves_live_managed_instance(vm_id, msg, "harmony")):
                await send_operation_once(hub, agent_id, {"type": P.MSG_HARMONY_VM_DELETE,
                    "request_id": uuid.uuid4().hex[:16], "vm_id": vm_id,
                    "hdc_serial": serial, "lease_token": token})
            return
        details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
        legacy = bool(reason == "reclaimed" and token and token == vm.lease_token
                      and serial and serial == vm.hdc_serial)
        if not accept_owner(vm, agent_id, hub, legacy_evidence=legacy):
            if reason == "reclaimed":
                await report_owner_conflict(hub, vm, agent_id, details, msg)
            return
        record_creation_provenance(vm, msg)
        expired = expire_operation_if_due(vm, hub)
        if is_abandoned_cleanup_receipt(vm, msg):
            maintenance(hub, vm, False)
            await release_port_lease(session, vm, reason="abandoned_first_bind_deleted")
            record_abandoned_cleanup_receipt(vm)
            await session.commit()
            return
        if not accept_status(vm, msg):
            expired = expire_operation_if_due(vm, hub) or expired
            cleaned = await cleanup_abandoned_first_bind(hub, vm, agent_id, details, msg)
            if expired or cleaned:
                await session.commit()
            return
        state = str(msg.get("state") or vm.state)
        ok = bool(msg.get("ok", True))
        details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
        lease = None
        if vm.lease_token:
            if token != vm.lease_token:
                return
            lease = (await session.execute(select(HarmonyVmPortLease).where(
                HarmonyVmPortLease.lease_token == token))).scalar_one_or_none()
            if lease is None:
                return
        else:
            op = lifecycle_runtime(vm).get("operation") or {}
            if token or op.get("action") != "stop" or msg.get("request_id") != op.get("request_id"):
                return
        if state == "running" and lease is None:
            return
        expected_serial = serial_for_port(lease.port) if lease is not None else ""
        error_code = str(msg.get("error_code") or ("" if ok else reason or "error"))
        error_message = str(msg.get("error") or reason)
        if serial and expected_serial and serial != expected_serial:
            state, ok, error_code = "error", False, "hdc_serial_mismatch"
            error_message = f"expected {expected_serial}, got {serial}"
        if not ok:
            state = "error"
        if state not in {"starting", "running", "stopping", "stopped", "error", "unavailable"}:
            return
        if lease is not None:
            lease.agent_id = agent_id
        vm.state = state
        vm.error_code = "" if ok else error_code[:128]
        vm.error_message = "" if ok else error_message[:4000]
        update_runtime(vm, last_status={"state": state, "ok": ok, "reason": reason,
                       "details": details, "ts": now_utc().isoformat()})
        if details.get("instance_created") is True:
            update_runtime(vm, instance_created=True)
        if state == "running" and ok:
            bind_owner(vm, agent_id, hub)
        if state == "running" and ok:
            lease.state = "active"
            lease.vm_id = vm.id
            lease.activated_at = lease.activated_at or now_utc()
            vm.hdc_port, vm.hdc_serial = lease.port, expected_serial
            vm.started_at, vm.stopped_at = vm.started_at or now_utc(), None
            update_runtime(vm, instance_created=True)
            await reserve_placeholder_alias(session, vm)
        elif state == "stopped" and ok:
            await reserve_placeholder_alias(session, vm)
            await release_port_lease(session, vm, reason="stopped")
            vm.stopped_at = now_utc()
        elif state in {"error", "unavailable"}:
            await reserve_placeholder_alias(session, vm)
            if details.get("cleanup_confirmed"):
                await release_port_lease(session, vm, reason=error_code or state)
            else:
                await quarantine_current_lease(session, vm, reason=error_code or state,
                                               error=error_message, details=details)
        finish_operation(vm, {**msg, "state": state})
        maintenance(hub, vm, not (state == "running" and ok))
        op = lifecycle_runtime(vm).get("operation") or {}
        if op.get("first_bind") and op.get("outcome") == "failed" and not ok:
            vm.assigned_agent_id = None
            if details.get("instance_deleted") or (details.get("cleanup_confirmed") and details.get("instance_created") is False):
                maintenance(hub, vm, False)
                pass
                update_runtime(vm, instance_created=False)
        await session.commit()


async def handle_vm_reconcile(agent_id: str, msg: Dict[str, Any], hub: Optional[Hub] = None) -> None:
    rows = msg.get("instances") if isinstance(msg.get("instances"), list) else []
    reported = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        reported.add(str(item.get("vm_id") or ""))
        if item.get("state") == "stopped":
            await _handle_stopped_vm_reconcile(agent_id, item, hub, inventory_complete=msg.get("inventory_complete") is True)
        else:
            await handle_vm_status(agent_id, {"type": P.MSG_HARMONY_VM_STATUS,
                "reason": "reclaimed", "ok": item.get("state") == "running", **item}, hub)
    factory = get_session_factory()
    async with factory() as session:
        reporter_host = agent_host_id(hub, agent_id)
        owner_query = HarmonyVmInstance.assigned_agent_id == agent_id
        if reporter_host:
            owner_query |= HarmonyVmInstance.runtime["owner_host_id"].as_string() == reporter_host
        owned = list((await session.execute(select(HarmonyVmInstance).where(owner_query))).scalars().all())
        unknown = set(msg.get("unknown_vm_ids") or [])
        for vm in sorted(owned, key=lambda row: row.id):
            await transaction_lock(session, f"harmony-vm:{vm.id}")
            vm = await session.get(HarmonyVmInstance, vm.id, populate_existing=True)
            if vm is None:
                continue
            if not accept_owner(vm, agent_id, hub):
                continue
            if (msg.get("inventory_complete") is True and vm.id not in reported | unknown
                  and vm.state not in {"draft", "starting", "stopping"}):
                vm.state, vm.error_code = "error", "instance_missing"
                vm.error_message = "Agent 未发现原实例；未自动重建"
        await session.commit()


async def _handle_stopped_vm_reconcile(agent_id: str, item: Dict[str, Any], hub: Optional[Hub], *, inventory_complete: bool = False) -> None:
    vm_id = str(item.get("vm_id") or "")
    if not vm_id:
        return
    factory = get_session_factory()
    async with factory() as session:
        await transaction_lock(session, f"harmony-vm:{vm_id}")
        vm = await session.get(HarmonyVmInstance, vm_id)
        if vm is None:
            if hub:
                await send_operation_once(hub, agent_id, {"type": P.MSG_HARMONY_VM_DELETE,
                    "request_id": uuid.uuid4().hex[:16], "vm_id": vm_id,
                    "hdc_serial": str(item.get("hdc_serial") or ""),
                    "lease_token": str(item.get("lease_token") or "")})
            return
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        safe_name = "aiphone_harmony_" + re.sub(r"[^A-Za-z0-9_]+", "_", vm.id)[:32]
        op = lifecycle_runtime(vm).get("operation") or {}
        if not vm.assigned_agent_id and op.get("first_bind") and not op.get("pending"):
            if await cleanup_abandoned_first_bind(hub, vm, agent_id, details, item):
                await session.commit()
            return
        legacy = bool(details.get("managed_instance_present") and details.get("instance_name") == safe_name)
        if not accept_owner(vm, agent_id, hub, legacy_evidence=legacy):
            await report_owner_conflict(hub, vm, agent_id, details, item)
            return
        if not inventory_complete:
            return  # old/partial inventory cannot certify that a guest stopped
        if vm.state in {"starting", "stopping"} or (vm.state == "running" and lifecycle_runtime(vm).get("operation")):
            return
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        if vm.lease_token or vm.hdc_port is not None or vm.hdc_serial:
            reported_token = str(item.get("lease_token") or details.get("released_lease_token") or "")
            reported_serial = str(item.get("hdc_serial") or "")
            if (not details.get("cleanup_confirmed") or not reported_token
                    or reported_token != vm.lease_token
                    or (reported_serial and reported_serial != vm.hdc_serial)):
                # Old registries may prove the exact instance/lease without
                # proving it stopped. Preserve that uncertainty, but persist
                # an evidenced session handoff so an explicit stop reaches
                # this same installation instead of the offline old session.
                if (details.get("managed_instance_present") is True
                        and details.get("instance_name") == safe_name
                        and str(details.get("instance_path") or "").strip()
                        and reported_token and reported_token == vm.lease_token
                        and reported_serial and reported_serial == vm.hdc_serial
                        and vm.hdc_port is not None
                        and reported_serial == serial_for_port(vm.hdc_port)):
                    lease = await session.get(HarmonyVmPortLease, vm.hdc_port)
                    if (lease is not None and lease.vm_id == vm.id
                            and lease.lease_token == reported_token):
                        await session.commit()
                return
            await release_port_lease(session, vm, reason="reconcile_stopped")
        vm.state, vm.error_code, vm.error_message = "stopped", "", ""
        vm.stopped_at = vm.stopped_at or now_utc()
        update_runtime(vm, instance_created=True, last_status={"state": "stopped", "ok": True,
            "reason": "reclaimed", "details": details, "ts": now_utc().isoformat()})
        finish_operation(vm, {"state": "stopped"})
        await reserve_placeholder_alias(session, vm)
        maintenance(hub, vm, True)
        await session.commit()


def capability_excluded_ports(response: Dict[str, Any], *, include_local: bool = True) -> set[int]:
    details = response.get("details") if isinstance(response.get("details"), dict) else {}
    keys = ("hdc_target_ports", "fport_ports", "tcp_listener_ports", "local_excluded_ports") if include_local else ("hdc_target_ports",)
    ports: set[int] = set()
    for key in keys:
        for value in details.get(key) or []:
            try:
                ports.add(int(value))
            except (TypeError, ValueError):
                pass
    return ports


class HarmonyVmCapabilityWaiter(CapabilityProbeWaiter):
    def __init__(self) -> None:
        super().__init__(P.MSG_HARMONY_VM_CAPABILITY_PROBE, vm_payload,
            sort_rows=True, cache_latest=True)

    def excluded_ports_for(self, agent_id: str) -> set[int]:
        ports: set[int] = set()
        for aid, response in self._latest.items():
            ports.update(capability_excluded_ports(response, include_local=aid == agent_id))
        return ports


_capability_waiter = HarmonyVmCapabilityWaiter()


def get_capability_waiter() -> HarmonyVmCapabilityWaiter:
    return _capability_waiter


__all__ = [
    "ACTIVE_STATES",
    "HDC_PORT_MAX",
    "HDC_PORT_MIN",
    "allocate_port_lease",
    "capability_excluded_ports",
    "delete_owned_alias",
    "ensure_alias_available",
    "filter_managed_devices_for_agent",
    "get_capability_waiter",
    "get_vm_or_404",
    "handle_vm_reconcile",
    "handle_vm_status",
    "mark_agent_vms_offline",
    "normalize_abi",
    "now_utc",
    "placeholder_serial",
    "release_port_lease",
    "reserve_placeholder_alias",
    "reset_vm_states_on_startup",
    "vm_payload",
]
