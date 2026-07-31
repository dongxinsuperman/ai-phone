"""Server orchestration helpers for managed HarmonyOS Emulator instances."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..hub import Hub
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
    vm = await session.get(HarmonyVmInstance, vm_id)
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
        if vm and vm.hdc_serial:
            owned.add(vm.hdc_serial)
        if row.serial in owned:
            return
    raise ValueError("alias_conflict")


async def reserve_placeholder_alias(session: AsyncSession, vm: HarmonyVmInstance) -> None:
    alias = (vm.alias or vm.name or "").strip()
    if not alias:
        return
    await _replace_owned_alias(session, vm, placeholder_serial(vm.id), alias)


async def point_alias_to_runtime(
    session: AsyncSession, vm: HarmonyVmInstance, serial: str
) -> None:
    await _replace_owned_alias(session, vm, serial, vm.alias)


async def _replace_owned_alias(
    session: AsyncSession,
    vm: HarmonyVmInstance,
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
    if vm.hdc_serial:
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
        return existing

    excluded = {
        int(port)
        for port in excluded_ports
        if HDC_PORT_MIN <= int(port) <= HDC_PORT_MAX
    }
    # 隔离端口的自动回收：必须同时满足两个条件，缺一不可。
    #
    #   1) 该端口原持有者就是本次探查的这台 Agent
    #   2) 这台 Agent 的实时探查里确实没有该端口
    #
    # 只看条件 2 是不够的——那等于用"一次探查没看见"去替代"确认端口已释放"这个
    # 更强的证据。反例：旧 Agent 已离线但它上面的 VM 仍在运行，本次探查（来自另
    # 一台 Agent）自然看不见那个端口，于是被误判空闲并重新分配，两台 VM 抢同一
    # 个 serial，正是 4.3 节要防的跨机串台。
    #
    # 收紧后：Agent 在线就能自愈（不会死锁），Agent 离线则一直隔离（不会误抢）。
    reclaimable = (
        await session.execute(
            select(HarmonyVmPortLease).where(
                HarmonyVmPortLease.state == "quarantined",
                HarmonyVmPortLease.agent_id == agent_id,
                HarmonyVmPortLease.port.notin_(excluded or {-1}),
            )
        )
    ).scalars().all()
    for stale in reclaimable:
        logger.info(
            "隔离端口 {} 由原持有 Agent {} 的实时探查确认空闲，放回可用池（原因：{}）",
            stale.port,
            agent_id,
            stale.quarantine_reason or "-",
        )
        await session.delete(stale)
    if reclaimable:
        await session.flush()

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
    lease.vm_id = None


async def mark_agent_vms_offline(agent_id: str) -> int:
    # 与 Android 对齐：Android 的端口占用每次实时查库算出，Agent 断线时
    # adb_serial 被清空、端口自动回到可用池。鸿蒙的探查结果缓存在进程内存里，
    # 断线不清就会让该 Agent 上报过的端口被永久排除。
    get_capability_waiter().forget_agent(agent_id)
    factory = get_session_factory()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(HarmonyVmInstance).where(
                        HarmonyVmInstance.assigned_agent_id == agent_id
                    )
                )
            ).scalars().all()
        )
        for vm in rows:
            vm.state = "agent_offline"
            vm.error_code = "agent_offline"
            vm.error_message = f"agent offline: {agent_id}"
        if rows:
            await session.commit()
        return len(rows)


async def filter_managed_devices_for_agent(
    agent_id: str, devices: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Reject a managed Harmony serial reported by any non-owner Agent.

    Hub serial routing is global.  Without this guard, two hosts both exposing
    ``127.0.0.1:10000`` would make the latest hello silently steal the route.
    Real-device reporting is untouched; this function only recognizes devices
    carrying the managed VM identity tag.
    """
    managed = []
    for device in devices:
        extra = device.get("extra") if isinstance(device.get("extra"), dict) else {}
        vm_id = str(extra.get("vm_instance_id") or "").strip()
        platform_name = str(device.get("platform") or "").strip().lower()
        if vm_id and platform_name == "harmony":
            managed.append((device, vm_id))
    if not managed:
        return devices

    factory = get_session_factory()
    accepted_ids: set[int] = set()
    try:
        async with factory() as session:
            for device, vm_id in managed:
                vm = await session.get(HarmonyVmInstance, vm_id)
                serial = str(device.get("serial") or "").strip()
                if (
                    vm is not None
                    and (vm.assigned_agent_id or "") == agent_id
                    and (vm.hdc_serial or "") == serial
                    and vm.state in ACTIVE_STATES
                ):
                    accepted_ids.add(id(device))
                    continue
                logger.error(
                    "拒绝非租约持有者上报受管 Harmony VM：agent={} vm_id={} serial={}",
                    agent_id,
                    vm_id,
                    serial,
                )
    except SQLAlchemyError:
        # 新表不可用时 fail-closed 地摘掉受管 VM，但保留同一次 hello 中的
        # Android/iOS/鸿蒙真机，不能让可选能力拖断整条 Agent 连接。
        logger.exception(
            "Harmony VM 身份表不可用：拒绝本次受管 VM 上报，保留普通设备 agent={}",
            agent_id,
        )
    return [
        device
        for device in devices
        if not (
            str(device.get("platform") or "").strip().lower() == "harmony"
            and str(
                (
                    device.get("extra")
                    if isinstance(device.get("extra"), dict)
                    else {}
                ).get("vm_instance_id")
                or ""
            ).strip()
            and id(device) not in accepted_ids
        )
    ]


async def reset_vm_states_on_startup(session: AsyncSession) -> int:
    result = await session.execute(
        update(HarmonyVmInstance)
        .where(HarmonyVmInstance.state != "draft")
        .values(state="agent_offline", error_code="server_restarted")
    )
    return result.rowcount or 0


async def handle_vm_status(
    agent_id: str,
    msg: Dict[str, Any],
    hub: Optional[Hub] = None,
) -> None:
    vm_id = str(msg.get("vm_id") or "").strip()
    if not vm_id:
        return
    factory = get_session_factory()
    async with factory() as session:
        vm = await session.get(HarmonyVmInstance, vm_id)
        if vm is None:
            # 与 Android 相同：只有重连认领发现“本地有、DB 已无”的孤儿
            # 才回发删除。普通 delete ack 不回发，避免 delete/status 循环。
            if hub is not None and str(msg.get("reason") or "") == "reclaimed":
                await hub.send_to_agent(agent_id, {
                    "type": P.MSG_HARMONY_VM_DELETE,
                    "request_id": uuid.uuid4().hex[:16],
                    "vm_id": vm_id,
                    "hdc_serial": str(msg.get("hdc_serial") or ""),
                    "lease_token": str(msg.get("lease_token") or ""),
                })
            else:
                logger.warning(
                    "收到未知 Harmony VM 状态 vm_id={} agent={}",
                    vm_id,
                    agent_id,
                )
            return
        token = str(msg.get("lease_token") or "")
        if not token or token != (vm.lease_token or ""):
            logger.warning(
                "忽略过期 Harmony VM 状态 vm_id={} agent={} token={}",
                vm_id,
                agent_id,
                token[:8],
            )
            return
        lease = (
            await session.execute(
                select(HarmonyVmPortLease).where(
                    HarmonyVmPortLease.lease_token == token
                )
            )
        ).scalar_one_or_none()
        if lease is None:
            logger.warning("忽略无有效租约的 Harmony VM 状态 vm_id={} agent={}", vm_id, agent_id)
            return

        state = str(msg.get("state") or vm.state)
        ok = bool(msg.get("ok", True))
        error_code = str(msg.get("error_code") or ("" if ok else msg.get("reason") or "error"))
        error_message = str(msg.get("error") or msg.get("reason") or "")
        details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
        serial = str(msg.get("hdc_serial") or "").strip()
        expected_serial = serial_for_port(lease.port)
        old_owners = {
            owner
            for owner in (
                str(vm.assigned_agent_id or "").strip(),
                str(lease.agent_id or "").strip(),
            )
            if owner and owner != agent_id
        }
        if old_owners:
            # 与 Android 一致：所有权跟随物理持有者。Harmony 比 Android
            # 多出的唯一准入是上方已经通过的 lease_token；能带有效 token
            # 上报本机实例状态，就把 VM 与端口租约一起绑定到上报者。
            logger.info(
                "Harmony VM 按物理持有者重新绑定 Agent "
                "vm_id={} owners={} reporter={}",
                vm_id,
                sorted(old_owners),
                agent_id,
            )
            lease.agent_id = agent_id
        if serial and serial != expected_serial:
            ok = False
            state = "error"
            error_code = "hdc_serial_mismatch"
            error_message = f"expected {expected_serial}, got {serial}"

        vm.assigned_agent_id = agent_id
        vm.state = state
        vm.error_code = "" if ok else error_code[:128]
        vm.error_message = "" if ok else error_message[:4000]
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
        if state == "running" and ok:
            lease.state = "active"
            lease.activated_at = lease.activated_at or now_utc()
            vm.hdc_port = lease.port
            vm.hdc_serial = expected_serial
            vm.started_at = vm.started_at or now_utc()
            vm.stopped_at = None
            await point_alias_to_runtime(session, vm, expected_serial)
        elif state == "stopped":
            await reserve_placeholder_alias(session, vm)
            await release_port_lease(session, vm, reason="stopped")
            vm.stopped_at = now_utc()
        elif state in {"error", "unavailable"}:
            vm.stopped_at = now_utc()
            if bool(details.get("cleanup_confirmed")):
                await reserve_placeholder_alias(session, vm)
                await release_port_lease(session, vm, reason=error_code or state)
            else:
                # 即使端口被隔离，别名也必须先退回稳定的 vm_id 占位；
                # 否则下一次换新端口启动时，旧 serial 映射会反向制造别名冲突。
                await reserve_placeholder_alias(session, vm)
                await quarantine_current_lease(
                    session,
                    vm,
                    reason=error_code or state,
                    error=error_message,
                    details=details,
                )
        await session.commit()


async def handle_vm_reconcile(
    agent_id: str,
    msg: Dict[str, Any],
    hub: Optional[Hub] = None,
) -> None:
    rows = msg.get("instances") if isinstance(msg.get("instances"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("state") or "").strip() == "stopped":
            await _handle_stopped_vm_reconcile(agent_id, item, hub)
            continue
        await handle_vm_status(
            agent_id,
            {
                "type": P.MSG_HARMONY_VM_STATUS,
                "reason": "reclaimed",
                "ok": True,
                **item,
            },
            hub,
        )


async def _handle_stopped_vm_reconcile(
    agent_id: str,
    item: Dict[str, Any],
    hub: Optional[Hub],
) -> None:
    """Restore a lease-free stopped VM after Server/Agent reconnection.

    A stopped VM has already released its HDC port lease, so routing it through
    ``handle_vm_status`` can never succeed: that handler deliberately requires
    a live lease for runtime updates. Android reconciliation has the same
    running/stopped split. Ownership follows the Agent that reports the locally
    existing managed instance, regardless of the previous process-scoped id.
    """
    vm_id = str(item.get("vm_id") or "").strip()
    if not vm_id:
        return
    factory = get_session_factory()
    async with factory() as session:
        vm = await session.get(HarmonyVmInstance, vm_id)
        if vm is None:
            # Agent still has a local instance whose Server configuration was
            # deleted.  Reuse the existing orphan cleanup command.
            if hub is not None:
                await hub.send_to_agent(
                    agent_id,
                    {
                        "type": P.MSG_HARMONY_VM_DELETE,
                        "request_id": uuid.uuid4().hex[:16],
                        "vm_id": vm_id,
                        "hdc_serial": str(item.get("hdc_serial") or ""),
                        "lease_token": str(item.get("lease_token") or ""),
                    },
                )
            return

        # No guessing: if Server still has any runtime/lease evidence, a
        # lease-free stopped report is not authoritative.  The Agent must
        # reclaim it through the normal running+lease path or leave it offline
        # for explicit investigation.
        if (
            vm.state in ACTIVE_STATES
            or vm.lease_token
            or vm.hdc_port is not None
            or vm.hdc_serial
        ):
            logger.warning(
                "忽略仍有活动证据的 stopped Harmony VM 对账 "
                "vm_id={} agent={} state={} port={} serial={}",
                vm_id,
                agent_id,
                vm.state,
                vm.hdc_port,
                vm.hdc_serial or "",
            )
            return

        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        old_owner = str(vm.assigned_agent_id or "").strip()
        if old_owner and old_owner != agent_id:
            logger.info(
                "已停止 Harmony VM 按物理持有者重新绑定 Agent "
                "vm_id={} owner={} reporter={}",
                vm_id,
                old_owner,
                agent_id,
            )
        vm.assigned_agent_id = agent_id
        vm.state = "stopped"
        vm.error_code = ""
        vm.error_message = ""
        vm.stopped_at = vm.stopped_at or now_utc()
        vm.runtime = {
            **(vm.runtime or {}),
            "last_status": {
                "state": "stopped",
                "ok": True,
                "reason": "reclaimed",
                "details": details,
                "ts": now_utc().isoformat(),
            },
        }
        await reserve_placeholder_alias(session, vm)
        await session.commit()


@dataclass
class _ProbeState:
    expected: set[str]
    responses: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    failures: Dict[str, str] = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)


class HarmonyVmCapabilityWaiter:
    def __init__(self) -> None:
        self._pending: Dict[str, _ProbeState] = {}
        self._latest: Dict[str, Dict[str, Any]] = {}

    async def probe(
        self,
        *,
        hub: Hub,
        vm: HarmonyVmInstance,
        timeout_sec: float = 60.0,
    ) -> Dict[str, Any]:
        agents = list(hub.snapshot().get("agents") or [])
        agent_ids = {
            str(row.get("agent_id") or "").strip()
            for row in agents
            if str(row.get("agent_id") or "").strip()
        }
        request_id = uuid.uuid4().hex[:16]
        state = _ProbeState(expected=agent_ids)
        self._pending[request_id] = state
        payload = {
            "type": P.MSG_HARMONY_VM_CAPABILITY_PROBE,
            **vm_payload(vm, request_id=request_id),
        }
        try:
            for agent_id in sorted(agent_ids):
                if not await hub.send_to_agent(agent_id, payload):
                    state.failures[agent_id] = "send_failed"
            if state.expected:
                try:
                    await asyncio.wait_for(state.event.wait(), timeout=timeout_sec)
                except asyncio.TimeoutError:
                    pass
            return {"request_id": request_id, "agents": self._rows(agents, state)}
        finally:
            self._pending.pop(request_id, None)

    async def probe_agent(
        self,
        *,
        hub: Hub,
        vm: HarmonyVmInstance,
        agent_id: str,
        timeout_sec: float = 60.0,
    ) -> Dict[str, Any]:
        """Refresh one Agent immediately before reserving an HDC port.

        A previous probe can become stale after a crashed Emulator leaves an
        Offline HDC target. Android resolves its local emulator port at start;
        Harmony keeps Server ownership, so Server must refresh this evidence
        before choosing from its independent port pool.
        """
        request_id = uuid.uuid4().hex[:16]
        state = _ProbeState(expected={agent_id})
        self._pending[request_id] = state
        payload = {
            "type": P.MSG_HARMONY_VM_CAPABILITY_PROBE,
            **vm_payload(vm, request_id=request_id),
        }
        try:
            if not await hub.send_to_agent(agent_id, payload):
                state.failures[agent_id] = "send_failed"
            else:
                try:
                    await asyncio.wait_for(
                        state.event.wait(),
                        timeout=timeout_sec,
                    )
                except asyncio.TimeoutError:
                    pass
            response = state.responses.get(agent_id)
            if response is not None:
                return dict(response)
            return {
                "ok": False,
                "reason": (
                    "下发失败"
                    if state.failures.get(agent_id)
                    else "探查超时"
                ),
                "details": {},
            }
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, agent_id: str, msg: Dict[str, Any]) -> bool:
        self._latest[agent_id] = dict(msg)
        request_id = str(msg.get("request_id") or "")
        state = self._pending.get(request_id)
        if state is None:
            return False
        state.responses[agent_id] = dict(msg)
        if state.expected.issubset(state.responses.keys() | state.failures.keys()):
            state.event.set()
        return True

    def discard_agent(self, agent_id: str) -> None:
        """Drop stale port evidence and finish probes waiting on a disconnected Agent."""
        self._latest.pop(agent_id, None)
        for state in self._pending.values():
            if (
                agent_id in state.expected
                and agent_id not in state.responses
                and agent_id not in state.failures
            ):
                state.failures[agent_id] = "agent_disconnected"
            if state.expected.issubset(
                state.responses.keys() | state.failures.keys()
            ):
                state.event.set()

    def forget_agent(self, agent_id: str) -> None:
        """Agent 断线即丢弃其探查快照，避免其上报端口被永久排除。"""
        self._latest.pop(agent_id, None)

    def excluded_ports_for(self, agent_id: str) -> set[int]:
        ports: set[int] = set()
        for aid, response in self._latest.items():
            details = response.get("details") if isinstance(response.get("details"), dict) else {}
            for value in details.get("hdc_target_ports") or []:
                try:
                    ports.add(int(value))
                except (TypeError, ValueError):
                    pass
            if aid != agent_id:
                continue
            for key in ("fport_ports", "tcp_listener_ports", "local_excluded_ports"):
                for value in details.get(key) or []:
                    try:
                        ports.add(int(value))
                    except (TypeError, ValueError):
                        pass
        return ports

    @staticmethod
    def _rows(agents: Iterable[Dict[str, Any]], state: _ProbeState) -> list[Dict[str, Any]]:
        result = []
        for agent in agents:
            agent_id = str(agent.get("agent_id") or "")
            response = state.responses.get(agent_id)
            if response:
                ok = bool(response.get("ok"))
                result.append({
                    "agent_id": agent_id,
                    "agent_name": agent.get("agent_name") or "",
                    "host_os": agent.get("host_os") or "",
                    "ok": ok,
                    "reason": str(response.get("reason") or ("可用" if ok else "不可用")),
                    "warning": str(response.get("warning") or ""),
                    "details": response.get("details") if isinstance(response.get("details"), dict) else {},
                })
            else:
                result.append({
                    "agent_id": agent_id,
                    "agent_name": agent.get("agent_name") or "",
                    "host_os": agent.get("host_os") or "",
                    "ok": False,
                    "reason": "下发失败" if state.failures.get(agent_id) else "探查超时",
                    "details": {},
                })
        result.sort(key=lambda row: (not row["ok"], row["agent_name"] or row["agent_id"]))
        return result


_capability_waiter = HarmonyVmCapabilityWaiter()


def get_capability_waiter() -> HarmonyVmCapabilityWaiter:
    return _capability_waiter


__all__ = [
    "ACTIVE_STATES",
    "HDC_PORT_MAX",
    "HDC_PORT_MIN",
    "allocate_port_lease",
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
