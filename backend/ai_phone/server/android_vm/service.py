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

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..hub import Hub
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


async def assign_emulator_port(
    session: AsyncSession, vm: AndroidVmInstance
) -> tuple[Optional[int], list[int]]:
    """给待启动 VM 在全局端口池里分配一个不与其它 VM 冲突的端口。

    返回 ``(assigned_port, exclude_ports)``：
    - ``assigned_port``：Server 钦定的端口（池满返回 ``None``，退回 Agent 自选）。
    - ``exclude_ports``：全局已占端口列表，随 ``vm_start`` 下发给 Agent 作兜底避让。

    **副作用（轻量预占）**：把 ``assigned_port`` 写进 ``vm.adb_serial`` 占坑，让随后并发
    发起的另一台 VM 启动在计算 ``used`` 时就能看见它、不会撞同一端口。本机该端口若被其它
    进程占用，由 Agent 侧 ``_choose_port`` 的本机探测兜底改选，并经 ``vm_status`` 回填真实 serial。
    """
    used = await used_emulator_ports(session, exclude_vm_id=vm.id)
    port = _pick_free_port(used)
    if port is not None:
        vm.adb_serial = f"emulator-{port}"
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
    vm = await session.get(AndroidVmInstance, vm_id)
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
    res = await session.execute(
        select(AndroidVmInstance).where(AndroidVmInstance.assigned_agent_id == agent_id)
    )
    rows = list(res.scalars().all())
    for vm in rows:
        vm.state = "agent_offline"
        vm.error_message = f"agent_offline_waiting_reclaim: {agent_id}"
        vm.runtime = {
            **(vm.runtime or {}),
            "agent_offline_at": now_utc().isoformat(),
            "last_known_adb_serial": vm.adb_serial or "",
        }
    if rows:
        await session.commit()
    return len(rows)


async def reset_vm_states_on_startup(session: AsyncSession) -> int:
    """Server 启动重置：把所有非 draft 的 VM 运行态归零为 ``agent_offline``（绑定关系保留）。

    **必须在 Server 开始接收 Agent 上报之前执行**（放在 lifespan startup、yield 之前）——
    那时还没有任何 Agent 能连上来，从根上避免"Agent 先标 running、重置又把它打回"的竞态。
    之后 Agent 重连各自认领回真实 running/stopped；永不回来的就正确停在 agent_offline。
    """
    res = await session.execute(
        update(AndroidVmInstance)
        .where(AndroidVmInstance.state != "draft")
        .values(state="agent_offline", adb_serial=None)
    )
    return res.rowcount or 0


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
    await hub.send_to_agent(
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
    """认领 + 孤儿对账（所有权 = 物理占有）：

    Agent 上报本机受管 AVD 的 vm_id 清单（区分 running_vm_ids / stopped_vm_ids）。Server：
    - vm_id **库里有**：归属改为上报者（谁报谁绑）；按实测落态——在 stopped 集 → stopped；
      在 running 集 → 不降级（由 vm_status reclaimed 置 running + serial）。
      旧 Agent 只发 vm_ids（未分类）时退回保守口径：非 active → stopped。
    - vm_id **库里没有** → 孤儿（删除残留 / 换 Agent 后的旧 vmid）→ 回发删除指令。
    - 差集收敛：DB 里归本 Agent、却**不在本轮清单**的 → 它已不在本机 → 置 agent_offline（不删）。
    """
    def _clean(key: str) -> list[str]:
        return [str(v).strip() for v in (msg.get(key) or []) if str(v).strip()]

    running_ids = _clean("running_vm_ids")
    stopped_ids = _clean("stopped_vm_ids")
    # 全集：优先 vm_ids（兼容旧 Agent），否则由 running ∪ stopped 合成
    vm_ids = _clean("vm_ids") or sorted(set(running_ids) | set(stopped_ids))
    # 注意：空清单也要处理——代表"该 Agent 本机一台受管 VM 都没有"，需对其名下 DB VM 做差集收敛。
    if session is not None:
        await _handle_vm_reconcile(session, agent_id, vm_ids, running_ids, stopped_ids, hub)
        return
    session_factory = get_session_factory()
    async with session_factory() as session:
        await _handle_vm_reconcile(session, agent_id, vm_ids, running_ids, stopped_ids, hub)


async def _handle_vm_reconcile(
    session: AsyncSession,
    agent_id: str,
    vm_ids: list[str],
    running_ids: list[str],
    stopped_ids: list[str],
    hub: Hub,
) -> None:
    res = await session.execute(
        select(AndroidVmInstance).where(AndroidVmInstance.id.in_(vm_ids))
    )
    rows = {vm.id: vm for vm in res.scalars().all()}
    orphans = [v for v in vm_ids if v not in rows]
    running_set = set(running_ids)
    stopped_set = set(stopped_ids)
    classified = bool(running_set or stopped_set)
    # 谁报谁绑：库里有的，归属一律改为上报者；状态按 Agent 实测的在跑/没跑精确落。
    for vm in rows.values():
        vm.assigned_agent_id = agent_id
        if classified:
            if vm.id in stopped_set:
                # 实测没跑 → 明确 stopped（修正"DB 旧 running 但进程已没"的假 running）
                vm.state = "stopped"
                vm.adb_serial = None
            # 在跑的（running_set）不在此降级——由 vm_status(reclaimed) 置 running + serial
        elif vm.state not in _ACTIVE_STATES:
            # 旧 Agent 未分类清单：退回保守口径（非 active → stopped）
            vm.state = "stopped"
            vm.adb_serial = None
    # 差集收敛：DB 里归本 Agent、但本轮没上报的 → 已不在本机 → agent_offline（不删，等下次认领）。
    # 覆盖"Server 重启后未经断线 handler、且某 VM 的 AVD 已不在 Agent 上"的滞留假 running。
    # 只跳过在途指令态（starting/stopping，AVD 可能还没建好）；running 的若不在清单必为滞留
    # （在跑的一定在 list_managed_avd_vmids 里），照样收敛。
    stale_stmt = select(AndroidVmInstance).where(
        AndroidVmInstance.assigned_agent_id == agent_id
    )
    if vm_ids:
        stale_stmt = stale_stmt.where(AndroidVmInstance.id.not_in(vm_ids))
    res_stale = await session.execute(stale_stmt)
    for vm in res_stale.scalars().all():
        if vm.state in ("starting", "stopping", "agent_offline"):
            continue
        vm.state = "agent_offline"
        vm.adb_serial = None
    await session.commit()
    if orphans:
        logger.info(
            "Agent {} 上报受管 VM {} 个：认领 {} 个、孤儿 {} 个，下发清理：{}",
            agent_id, len(vm_ids), len(rows), len(orphans), orphans,
        )
    for vm_id in orphans:
        await hub.send_to_agent(
            agent_id,
            {
                "type": P.MSG_VM_DELETE,
                "request_id": uuid.uuid4().hex[:16],
                "vm_id": vm_id,
                "adb_serial": "",
            },
        )


async def _handle_vm_status(
    session: AsyncSession,
    agent_id: str,
    msg: Dict[str, Any],
    vm_id: str,
    hub: Optional[Hub] = None,
) -> None:
    reason = str(msg.get("reason") or "")
    vm = await session.get(AndroidVmInstance, vm_id)
    if vm is None:
        # 认领上报但 DB 已无此 vm = 删除残留（删除时 Agent 离线漏了指令），回发清理
        if reason == "reclaimed":
            await _cleanup_remote_avd(hub, agent_id, vm_id)
            logger.info("Agent {} 认领 DB 已删除的 VM {}，下发清理", agent_id, vm_id)
        else:
            logger.warning("收到未知 VM 状态 vm_id={} agent={}", vm_id, agent_id)
        return
    # 所有权 = 物理占有：谁上报谁认领。vm_id 全局唯一，能上报 reclaimed 就代表该 Agent 本机
    # 物理有这台 AVD → 直接把归属落到上报者（即便 DB 里还记着旧 Agent ID，重启换名也认）。
    # 旧"agent_id 不一致就当抢占、下发清理"的判定已废除——换 Agent 走"删旧 vmid"路径，
    # 旧机回来报的旧 vmid 会因"库里没有"被 reconcile 清掉，不会落到这里。
    state = str(msg.get("state") or vm.state or "draft")
    ok = bool(msg.get("ok", True))
    adb_serial = str(msg.get("adb_serial") or "").strip()
    error = str(msg.get("error") or msg.get("reason") or "")
    details = msg.get("details") if isinstance(msg.get("details"), dict) else {}

    vm.assigned_agent_id = agent_id
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
    if state in ("stopped", "unavailable", "error"):
        vm.stopped_at = now_utc()
        if state != "running":
            vm.adb_serial = adb_serial or None
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
                sent = await hub.send_to_agent(agent_id, payload)
                if not sent:
                    state.sent_failures[agent_id] = "send_failed"
            if state.expected and len(state.responses) < len(state.expected):
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
