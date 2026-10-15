"""Android VM server-side orchestration helpers.

This module keeps VM-specific runtime concerns out of the generic Agent WS,
runner, scheduler, and device APIs.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..hub import Hub
from ..vm_lifecycle import (
    record_creation_provenance, proves_live_managed_instance, send_operation_once,
    expire_operation_if_due,
    is_abandoned_cleanup_receipt, record_abandoned_cleanup_receipt,
    accept_owner, accept_status, bind_owner, cleanup_foreign_copy, cleanup_abandoned_first_bind,
    finish_operation, maintenance, runtime as lifecycle_runtime,
    transaction_lock, update_runtime, host_id as agent_host_id, fail_attempt,
)
from ..models import AndroidVmInstance, DeviceAlias


TERMINAL_STATES = {"draft", "stopped", "unavailable", "error"}
# 过渡/在跑态：reconcile 认领时不把这些降级为 stopped（running 由 vm_status 维护）。
_ACTIVE_STATES = {"starting", "running", "stopping"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Emulator 控制台端口区间（与 Agent ``_choose_port`` 的 ``range(5554, 5684, 2)`` 对齐）：
# 5554~5682 偶数，共 65 个槽。emulator serial 形如 ``emulator-<port>`` 本机唯一、跨机不唯一，
# 故由 Server 在这个全局池里统一分配，保证全网 serial 不撞（堵死跨机器串台）。
_EMULATOR_PORT_MIN = 5554
_EMULATOR_PORT_MAX = 5682


def _port_from_serial(serial: Optional[str]) -> Optional[int]:
    s = (serial or "").strip()
    if not s.startswith("emulator-"):
        return None
    try:
        return int(s.rsplit("-", 1)[1])
    except ValueError:
        return None


async def used_emulator_ports(
    session: AsyncSession, *, exclude_vm_id: Optional[str] = None
) -> set[int]:
    """从所有 VM 的 ``adb_serial`` 反解出全局已占用的 emulator 端口集合。

    ``exclude_vm_id``：排除某台 VM 自身（给它重新分配端口时，别把它旧端口算进冲突）。
    """
    res = await session.execute(
        select(AndroidVmInstance.id, AndroidVmInstance.adb_serial).where(
            AndroidVmInstance.adb_serial.is_not(None)
        )
    )
    ports: set[int] = set()
    for vm_id, serial in res.all():
        if exclude_vm_id and vm_id == exclude_vm_id:
            continue
        port = _port_from_serial(serial)
        if port is not None:
            ports.add(port)
    return ports


def _pick_free_port(used: set[int]) -> Optional[int]:
    for port in range(_EMULATOR_PORT_MIN, _EMULATOR_PORT_MAX + 1, 2):
        if port not in used:
            return port
    return None


async def assign_emulator_port(session: AsyncSession, vm: AndroidVmInstance, hub: Optional[Hub] = None) -> tuple[Optional[int], list[int]]:
    """Reserve a global serial atomically until the caller commits or rolls back."""
    await transaction_lock(session, "android-vm-ports")
    used = await used_emulator_ports(session, exclude_vm_id=vm.id)
    if hub is not None and not vm.assigned_agent_id and not lifecycle_runtime(vm).get("instance_created"):
        from ..lockstore import get_default_lock_store
        store = getattr(hub, "vm_lock_store", None) or get_default_lock_store()
        for serial in store.snapshot():
            occupied = _port_from_serial(serial)
            if occupied is not None:
                used.add(occupied)
        for candidate in range(_EMULATOR_PORT_MIN, _EMULATOR_PORT_MAX + 1, 2):
            if hub.subscriber_count(f"emulator-{candidate}"):
                used.add(candidate)
    previous = _port_from_serial(vm.adb_serial)
    port = previous if previous is not None and previous not in used else _pick_free_port(used)
    if port is None:
        raise RuntimeError("android_emulator_port_pool_exhausted")
    vm.adb_serial = f"emulator-{port}"
    await session.flush()
    return port, sorted(used)


def vm_payload(vm: AndroidVmInstance, *, request_id: Optional[str] = None) -> Dict[str, Any]:
    config = vm.config_json or {}
    payload: Dict[str, Any] = {
        "vm_id": vm.id,
        "name": vm.name,
        "alias": vm.alias or vm.name,
        "profile_ref_type": vm.profile_ref_type or "custom",
        "profile_ref_id": vm.profile_ref_id or "",
        "profile_id": vm.profile_id or "",
        "profile_name": vm.profile_name or "",
        "config_version": int(vm.config_version or 1),
        "config_json": config,
        "capability_marks": vm.capability_marks or {},
        "api_level": int(vm.api_level),
        "abi": vm.abi or "auto",
        "system_type": vm.system_type or "google_apis",
        "system_image": vm.system_image or "",
        "screen_width": int(vm.screen_width or 1080),
        "screen_height": int(vm.screen_height or 2400),
        "density": int(vm.density or 420),
        "orientation": vm.orientation or "portrait",
    }
    payload.update(_agent_config_fields(config))
    if request_id:
        payload["request_id"] = request_id
    return payload


def _agent_config_fields(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "screen_size_in": _cfg(config, "display", "screen_size_in", default=""),
        "ram_mb": _cfg(config, "performance", "ram_mb", default=None),
        "cpu_cores": _cfg(config, "performance", "cpu_cores", default=None),
        "vm_heap_mb": _cfg(config, "performance", "vm_heap_mb", default=None),
        "gpu_mode": _cfg(config, "performance", "gpu_mode", default="auto"),
        "internal_storage_mb": _cfg(config, "storage", "internal_storage_mb", default=None),
        "sdcard_mb": _cfg(config, "storage", "sdcard_mb", default=None),
        "wipe_data": bool(_cfg(config, "storage", "wipe_data", default=False)),
        "snapshot_policy": _cfg(config, "storage", "snapshot_policy", default="discard_changes"),
        "network_speed": _cfg(config, "network", "speed", default="full"),
        "network_delay": _cfg(config, "network", "delay", default="none"),
        "dns_server": _cfg(config, "network", "dns_server", default=""),
        "http_proxy": _cfg(config, "network", "http_proxy", default=""),
        "back_camera": _cfg(config, "hardware", "back_camera", default="emulated"),
        "front_camera": _cfg(config, "hardware", "front_camera", default="none"),
        "gps": bool(_cfg(config, "hardware", "gps", default=True)),
        "accelerometer": bool(_cfg(config, "hardware", "accelerometer", default=True)),
        "gyroscope": bool(_cfg(config, "hardware", "gyroscope", default=True)),
        "proximity": bool(_cfg(config, "hardware", "proximity", default=False)),
        "hardware_keyboard": bool(_cfg(config, "hardware", "hardware_keyboard", default=False)),
        "navigation_style": _cfg(config, "hardware", "navigation_style", default="none"),
        "no_window": bool(_cfg(config, "startup", "no_window", default=True)),
        "no_audio": bool(_cfg(config, "startup", "no_audio", default=True)),
        "no_boot_anim": bool(_cfg(config, "startup", "no_boot_anim", default=True)),
        "writable_system": bool(_cfg(config, "startup", "writable_system", default=False)),
    }


def _cfg(config: Dict[str, Any], group: str, key: str, *, default: Any) -> Any:
    value = config.get(group)
    if not isinstance(value, dict):
        return default
    return value.get(key, default)


async def get_vm_or_404(session: AsyncSession, vm_id: str) -> AndroidVmInstance:
    vm = await session.get(AndroidVmInstance, vm_id, populate_existing=True)
    if vm is None:
        raise LookupError("android vm not found")
    return vm


def apply_vm_patch(vm: AndroidVmInstance, patch: Dict[str, Any]) -> None:
    for key in (
        "name",
        "alias",  # 别名可改（唯一性/映射同步在 patch 接口里已处理）
        "profile_ref_type",
        "profile_ref_id",
        "profile_id",
        "profile_name",
        "config_version",
        "config_json",
        "capability_marks",
        "api_level",
        "abi",
        "system_type",
        "system_image",
        "screen_width",
        "screen_height",
        "density",
        "orientation",
    ):
        if key in patch and patch[key] is not None:
            setattr(vm, key, patch[key])


async def mark_agent_vms_unavailable(
    agent_id: str, *, session: Optional[AsyncSession] = None
) -> int:
    """Mark all VMs assigned to a disconnected Agent as unavailable."""
    if session is not None:
        return await _mark_agent_vms_unavailable(session, agent_id)
    session_factory = get_session_factory()
    async with session_factory() as session:
        return await _mark_agent_vms_unavailable(session, agent_id)


async def _mark_agent_vms_unavailable(session: AsyncSession, agent_id: str) -> int:
    rows = list((await session.execute(select(AndroidVmInstance).where(
        (AndroidVmInstance.assigned_agent_id == agent_id) |
        (AndroidVmInstance.runtime["operation"]["target_agent_id"].as_string() == agent_id)
    ))).scalars().all())
    for vm in sorted(rows, key=lambda row: row.id):
        await transaction_lock(session, f"android-vm:{vm.id}")
        vm = await session.get(AndroidVmInstance, vm.id, populate_existing=True)
        if vm is None:
            continue
        op = lifecycle_runtime(vm).get("operation") or {}
        if op.get("pending") and op.get("target_agent_id") == agent_id:
            fail_attempt(vm, "agent_disconnected", "Agent 断开，本次操作已结束；不会自动重试")
        elif vm.assigned_agent_id == agent_id:
            vm.state, vm.error_message = "agent_offline", f"agent offline: {agent_id}"
        update_runtime(vm, agent_offline_at=now_utc().isoformat(), last_known_adb_serial=vm.adb_serial or "")
    if rows:
        await session.commit()
    return len(rows)


async def reset_vm_states_on_startup(session: AsyncSession) -> int:
    rows = list((await session.execute(select(AndroidVmInstance).where(AndroidVmInstance.state != "draft"))).scalars().all())
    for vm in rows:
        op = lifecycle_runtime(vm).get("operation") or {}
        if op.get("pending"):
            fail_attempt(vm, "server_restarted", "服务重启，本次未完成操作已结束；不会自动重试")
        elif vm.assigned_agent_id:
            vm.state = "agent_offline"
    return len(rows)


async def handle_vm_status(
    agent_id: str,
    msg: Dict[str, Any],
    hub: Optional[Hub] = None,
    *,
    session: Optional[AsyncSession] = None,
) -> None:
    """Persist Agent VM lifecycle updates."""
    vm_id = str(msg.get("vm_id") or "")
    if not vm_id:
        return
    if session is not None:
        await _handle_vm_status(session, agent_id, msg, vm_id, hub)
        return
    session_factory = get_session_factory()
    async with session_factory() as session:
        await _handle_vm_status(session, agent_id, msg, vm_id, hub)


async def _cleanup_remote_avd(hub: Optional[Hub], agent_id: str, vm_id: str) -> None:
    if hub is None or not agent_id or not vm_id:
        return
    await send_operation_once(hub,
        agent_id,
        {
            "type": P.MSG_VM_DELETE,
            "request_id": uuid.uuid4().hex[:16],
            "vm_id": vm_id,
            "adb_serial": "",
        },
    )


async def handle_vm_reconcile(
    agent_id: str, msg: Dict[str, Any], hub: Hub, *, session: Optional[AsyncSession] = None
) -> None:
    """Agent reports inventory; Server preserves recorded ownership and cleanup intent."""
    def _clean(key: str) -> list[str]:
        return [str(v).strip() for v in (msg.get(key) or []) if str(v).strip()]

    running_ids = _clean("running_vm_ids")
    stopped_ids = _clean("stopped_vm_ids")
    # 全集：优先 vm_ids（兼容旧 Agent），否则由 running ∪ stopped 合成
    vm_ids = _clean("vm_ids") or sorted(set(running_ids) | set(stopped_ids))
    # 注意：空清单也要处理——代表"该 Agent 本机一台受管 VM 都没有"，需对其名下 DB VM 做差集收敛。
    if session is not None:
        await _handle_vm_reconcile(session, agent_id, vm_ids, running_ids, stopped_ids, hub, inventory_complete=msg.get("inventory_complete", True), unknown_ids=msg.get("unknown_vm_ids") or [], instance_proofs=msg.get("instance_proofs") or {})
        return
    session_factory = get_session_factory()
    async with session_factory() as session:
        await _handle_vm_reconcile(session, agent_id, vm_ids, running_ids, stopped_ids, hub, inventory_complete=msg.get("inventory_complete", True), unknown_ids=msg.get("unknown_vm_ids") or [], instance_proofs=msg.get("instance_proofs") or {})


async def _handle_vm_reconcile(
    session: AsyncSession, agent_id: str, vm_ids: list[str], running_ids: list[str],
    stopped_ids: list[str], hub: Hub, *, inventory_complete: bool = True, unknown_ids: list[str] = (),
    instance_proofs: Optional[dict] = None,
) -> None:
    reported, stopped, unknown = set(vm_ids), set(stopped_ids), set(unknown_ids)
    reporter_host = agent_host_id(hub, agent_id)
    owner_query = (AndroidVmInstance.assigned_agent_id == agent_id) | AndroidVmInstance.id.in_(reported)
    if reporter_host:
        owner_query |= AndroidVmInstance.runtime["owner_host_id"].as_string() == reporter_host
    rows = list((await session.execute(select(AndroidVmInstance).where(owner_query))).scalars().all())
    known = {vm.id for vm in rows}
    for vm in sorted(rows, key=lambda row: row.id):
        await transaction_lock(session, f"android-vm:{vm.id}")
        vm = await session.get(AndroidVmInstance, vm.id, populate_existing=True)
        if vm is None:
            continue
        proof = (instance_proofs or {}).get(vm.id) or {}
        op = lifecycle_runtime(vm).get("operation") or {}
        if not vm.assigned_agent_id and op.get("first_bind") and not op.get("pending"):
            await cleanup_abandoned_first_bind(hub, vm, agent_id, proof)
            continue
        legacy = bool(inventory_complete and vm.id in reported and
                      proof.get("avd_name") == f"aiphone_vm_{vm.id}")
        if not accept_owner(vm, agent_id, hub, legacy_evidence=legacy):
            if inventory_complete:
                await cleanup_foreign_copy(hub, vm, agent_id, proof)
            continue  # cleanup addresses the copy, never rewrites the ledger
        if vm.id in reported:
            update_runtime(vm, instance_created=True)
            if vm.id in stopped and vm.id not in unknown and vm.state not in {"starting", "stopping"} and not (vm.state == "running" and lifecycle_runtime(vm).get("operation")):
                vm.state = "stopped"
                vm.error_message = ""
                vm.stopped_at = vm.stopped_at or now_utc()
                finish_operation(vm, {"state": "stopped"})
                maintenance(hub, vm, True)
        elif inventory_complete and vm.id not in unknown and vm.state not in {"draft", "starting", "stopping"}:
            vm.state = "error"
            vm.error_message = "instance_missing: Agent 未发现原实例；未自动重建"
    await session.commit()
    for vm_id in reported - known:
        await _cleanup_remote_avd(hub, agent_id, vm_id)


async def _handle_vm_status(
    session: AsyncSession,
    agent_id: str,
    msg: Dict[str, Any],
    vm_id: str,
    hub: Optional[Hub] = None,
) -> None:
    await transaction_lock(session, f"android-vm:{vm_id}")
    reason = str(msg.get("reason") or "")
    vm = await session.get(AndroidVmInstance, vm_id)
    if vm is None:
        if reason == "reclaimed" or proves_live_managed_instance(vm_id, msg, "android"):
            await _cleanup_remote_avd(hub, agent_id, vm_id)
        return
    state = str(msg.get("state") or vm.state or "draft")
    ok = bool(msg.get("ok", True))
    adb_serial = str(msg.get("adb_serial") or "").strip()
    error = str(msg.get("error") or msg.get("reason") or "")
    details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
    legacy = (reason == "reclaimed" and details.get("avd_name") == f"aiphone_vm_{vm.id}"
              and (not vm.adb_serial or vm.adb_serial == adb_serial))
    if not accept_owner(vm, agent_id, hub, legacy_evidence=legacy):
        if reason == "reclaimed":
            await cleanup_foreign_copy(hub, vm, agent_id, details, msg)
        return
    record_creation_provenance(vm, msg)
    expired = expire_operation_if_due(vm, hub)
    if is_abandoned_cleanup_receipt(vm, msg):
        maintenance(hub, vm, False)
        vm.adb_serial = None
        record_abandoned_cleanup_receipt(vm)
        await session.commit()
        return
    if not accept_status(vm, msg):
        expired = expire_operation_if_due(vm, hub) or expired
        cleaned = await cleanup_abandoned_first_bind(hub, vm, agent_id, details, msg)
        if expired or cleaned:
            await session.commit()
        return
    if state == "stopped" and not ok:
        state = "error"
    if adb_serial and vm.adb_serial and adb_serial != vm.adb_serial:
        # The Agent may not silently select a different globally reserved port.
        state, ok, error = "error", False, f"adb_serial_mismatch: expected {vm.adb_serial}, got {adb_serial}"
        adb_serial = ""
    if state == "running" and ok:
        bind_owner(vm, agent_id, hub)
    if (state == "running" and ok) or details.get("instance_created") is True:
        update_runtime(vm, instance_created=True)
    finish_operation(vm, {**msg, "state": state})
    maintenance(hub, vm, not (state == "running" and ok))
    vm.state = state
    vm.error_message = "" if ok else error[:4000]
    vm.runtime = {
        **(vm.runtime or {}),
        "last_status": {
            "state": state,
            "ok": ok,
            "reason": msg.get("reason") or "",
            "details": details,
            "ts": now_utc().isoformat(),
        },
    }
    if adb_serial:
        vm.adb_serial = adb_serial
    if state == "running":
        vm.started_at = vm.started_at or now_utc()
        vm.stopped_at = None
        if adb_serial:
            try:
                await sync_vm_alias(session, vm, adb_serial)
            except VmAliasConflict as exc:
                vm.error_message = str(exc)[:4000]
                vm.runtime = {
                    **(vm.runtime or {}),
                    "alias_sync_error": {
                        "alias": exc.alias,
                        "conflict_serial": exc.conflict_serial,
                        "ts": now_utc().isoformat(),
                    },
                }
    if state == "stopped" and ok:
        vm.stopped_at = now_utc()
    # Stop/offline/error preserve this identity. Ledger deletion retires the
    # reservation; reconnect orphan reconciliation removes old host residues.
    op = lifecycle_runtime(vm).get("operation") or {}
    if op.get("first_bind") and op.get("outcome") == "failed" and not ok:
        vm.assigned_agent_id = None
        if details.get("instance_deleted") or (details.get("cleanup_confirmed") and details.get("instance_created") is False):
            maintenance(hub, vm, False)
            vm.adb_serial = None
            update_runtime(vm, instance_created=False)
    await session.commit()


class VmAliasConflict(RuntimeError):
    def __init__(self, *, alias: str, conflict_serial: str) -> None:
        super().__init__(
            f"vm_alias_conflict: alias {alias!r} already bound to {conflict_serial}"
        )
        self.alias = alias
        self.conflict_serial = conflict_serial


async def sync_vm_alias(
    session: AsyncSession, vm: AndroidVmInstance, adb_serial: str
) -> None:
    """Point the VM's frozen alias at the current emulator serial."""
    alias = (vm.alias or vm.name or "").strip()
    serial = (adb_serial or "").strip()
    if not alias or not serial:
        return

    res = await session.execute(select(DeviceAlias).where(DeviceAlias.alias == alias))
    by_alias = res.scalar_one_or_none()
    by_serial = await session.get(DeviceAlias, serial)

    if by_serial is not None and by_serial.alias != alias:
        res_vm = await session.execute(
            select(AndroidVmInstance).where(AndroidVmInstance.alias == by_serial.alias)
        )
        stale_vm = res_vm.scalar_one_or_none()
        if stale_vm is None:
            raise VmAliasConflict(alias=alias, conflict_serial=serial)
        await session.delete(by_serial)
        await session.flush()

    if by_alias is not None:
        if by_alias.serial == serial:
            by_alias.note = ""
            await session.flush()
            return
        if not by_alias.serial.startswith("emulator-"):
            raise VmAliasConflict(alias=alias, conflict_serial=by_alias.serial)
        await session.delete(by_alias)
        await session.flush()

    by_serial = await session.get(DeviceAlias, serial)
    if by_serial is not None:
        if by_serial.alias != alias:
            raise VmAliasConflict(alias=alias, conflict_serial=serial)
        by_serial.note = ""
        await session.flush()
        return

    session.add(DeviceAlias(serial=serial, alias=alias, note=""))
    await session.flush()


async def delete_vm_alias(session: AsyncSession, vm: AndroidVmInstance) -> int:
    """Delete the frozen alias row for a VM configuration."""
    alias = (vm.alias or vm.name or "").strip()
    if not alias:
        return 0
    res = await session.execute(select(DeviceAlias).where(DeviceAlias.alias == alias))
    row = res.scalar_one_or_none()
    if row is None:
        return 0
    if vm.adb_serial and row.serial != vm.adb_serial:
        return 0
    if not vm.adb_serial and not row.serial.startswith("emulator-"):
        return 0
    await session.delete(row)
    await session.flush()
    return 1


@dataclass
class _ProbeState:
    expected: set[str]
    responses: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    sent_failures: Dict[str, str] = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)


class VmCapabilityWaiter:
    """Small in-process rendezvous for VM capability probe responses."""

    def __init__(self) -> None:
        self._pending: Dict[str, _ProbeState] = {}

    async def probe(
        self,
        *,
        hub: Hub,
        vm: AndroidVmInstance,
        timeout_sec: float = 60.0,
    ) -> Dict[str, Any]:
        agents = list(hub.snapshot().get("agents") or [])
        agent_ids = {
            str(agent.get("agent_id") or "").strip()
            for agent in agents
            if str(agent.get("agent_id") or "").strip()
        }
        request_id = uuid.uuid4().hex[:16]
        state = _ProbeState(expected=set(agent_ids))
        self._pending[request_id] = state
        payload = {
            "type": P.MSG_VM_CAPABILITY_PROBE,
            **vm_payload(vm, request_id=request_id),
        }

        try:
            for agent_id in sorted(agent_ids):
                sent = await send_operation_once(hub, agent_id, payload)
                if not sent:
                    state.sent_failures[agent_id] = "send_failed"
            if state.expected - (state.responses.keys() | state.sent_failures.keys()):
                try:
                    await asyncio.wait_for(state.event.wait(), timeout=timeout_sec)
                except asyncio.TimeoutError:
                    pass
            return {
                "request_id": request_id,
                "agents": self._build_rows(agents, state),
            }
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, agent_id: str, msg: Dict[str, Any]) -> bool:
        request_id = str(msg.get("request_id") or "")
        state = self._pending.get(request_id)
        if state is None:
            return False
        state.responses[agent_id] = dict(msg)
        if state.expected.issubset(state.responses.keys() | state.sent_failures.keys()):
            state.event.set()
        return True

    @staticmethod
    def _build_rows(
        agents: Iterable[Dict[str, Any]], state: _ProbeState
    ) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for agent in agents:
            agent_id = str(agent.get("agent_id") or "")
            resp = state.responses.get(agent_id)
            if resp:
                ok = bool(resp.get("ok"))
                rows.append({
                    "agent_id": agent_id,
                    "agent_name": agent.get("agent_name") or "",
                    "host_os": agent.get("host_os") or "",
                    "ok": ok,
                    "reason": str(resp.get("reason") or ("可用" if ok else "不可用")),
                    "warning": str(resp.get("warning") or ""),
                    "details": resp.get("details") if isinstance(resp.get("details"), dict) else {},
                })
                continue
            # 超时不等于"不可用"，而是"Agent 未响应"——区分二者，提示可重试
            reason = (
                "下发通道异常，Agent 未收到探查"
                if state.sent_failures.get(agent_id)
                else "探查超时：Agent 未响应（可重试）"
            )
            rows.append({
                "agent_id": agent_id,
                "agent_name": agent.get("agent_name") or "",
                "host_os": agent.get("host_os") or "",
                "ok": False,
                "reason": reason,
                "details": {},
            })
        rows.sort(key=lambda row: (not row["ok"], str(row.get("agent_name") or row["agent_id"])))
        return rows


_capability_waiter = VmCapabilityWaiter()


def get_capability_waiter() -> VmCapabilityWaiter:
    return _capability_waiter
