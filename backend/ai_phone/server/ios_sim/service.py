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

状态机与 Android / 鸿蒙保持一致的对外语义：

```text
draft ──dispatch/start──▶ starting ──Agent 上报 running──▶ running
                            │                                │
                            └──失败──▶ error                  ├──stop──▶ stopping ──▶ stopped
                                                             └──消失──▶ stopped
任意活动态 ──Agent 断线──▶ agent_offline ──重连认领──▶ running / stopped
Server 重启 ──▶ 非 draft 一律归 agent_offline（等 Agent 重连重新认领）
```
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..hub import Hub
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
    vm = await session.get(IosSimVmInstance, vm_id)
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
    """Server 重启后把所有非 draft 实例归为 agent_offline。

    与 Android / 鸿蒙一致：Server 内存里的 Agent 连接全丢了，任何"运行中"的记录
    都不再可信，必须等 Agent 重连上报对账清单后重新认领。

    与 Android 的一处差异：Android 会顺手清空 ``adb_serial`` 把端口还回池子；
    我们**保留 udid**——它是虚拟机的持久身份，不是端口，清掉反而丢失了"这条记录
    对应哪台实例"的线索。
    """
    result = await session.execute(
        update(IosSimVmInstance)
        .where(IosSimVmInstance.state != "draft")
        .values(state="agent_offline", error_code="server_restarted")
    )
    return result.rowcount or 0


async def mark_agent_vms_offline(agent_id: str) -> int:
    """Agent 断线：把它名下的实例标 agent_offline，并丢弃其探查快照。

    保留 ``assigned_agent_id``——实例仍然物理存在于那台 Agent 上，重连后要认回去。
    """
    get_capability_waiter().forget_agent(agent_id)
    factory = get_session_factory()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(IosSimVmInstance).where(
                        IosSimVmInstance.assigned_agent_id == agent_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for vm in rows:
            vm.state = "agent_offline"
            vm.error_code = "agent_offline"
            vm.error_message = f"agent offline: {agent_id}"
            # 设备已不在池子里，别名退回占位符；否则那条 UDID 映射会一直挂着，
            # 下次这台重新启动时反过来撞上自己的别名。
            await _try_bind_alias(session, vm, placeholder_serial(vm.id))
        if rows:
            await session.commit()
        return len(rows)


# ---------------------------------------------------------------------------
# 状态上报
# ---------------------------------------------------------------------------
async def handle_vm_status(
    agent_id: str,
    msg: Dict[str, Any],
    hub: Optional[Hub] = None,
) -> None:
    """处理 Agent 的生命周期状态上报。

    校验方式比鸿蒙简单：鸿蒙必须核对 ``lease_token`` 才能确认"这台 VM 归你"，
    因为它的 serial 是可复用的端口；我们的 UDID 天然唯一，按
    ``vm_id`` + ``assigned_agent_id`` 判断归属就够了。
    """
    vm_id = str(msg.get("vm_id") or "").strip()
    if not vm_id:
        return
    state = str(msg.get("state") or "").strip()
    reason = str(msg.get("reason") or "").strip()
    udid = str(msg.get("udid") or "").strip()
    details = msg.get("details") if isinstance(msg.get("details"), dict) else {}

    factory = get_session_factory()
    async with factory() as session:
        vm = await session.get(IosSimVmInstance, vm_id)
        if vm is None:
            # 与 Android / 鸿蒙相同：只有重连认领发现「本机有、DB 已无」的孤儿才
            # 回发删除。普通 delete ack 不回发，避免 delete/status 无限循环。
            if hub is not None and reason == "reclaimed":
                await hub.send_to_agent(
                    agent_id,
                    {
                        "type": P.MSG_IOS_SIM_VM_DELETE,
                        "request_id": uuid.uuid4().hex[:16],
                        "vm_id": vm_id,
                        "udid": udid,
                    },
                )
                logger.info(
                    "iOS 虚拟机孤儿实例，已回发删除 vm_id={} agent={}", vm_id, agent_id
                )
            else:
                logger.warning(
                    "收到未知 iOS 虚拟机状态 vm_id={} agent={} state={}",
                    vm_id, agent_id, state,
                )
            return

        owner = str(vm.assigned_agent_id or "").strip()
        if owner and owner != agent_id and reason != "reclaimed":
            # 非归属 Agent 的普通状态上报一律拒绝，防止两台 Agent 抢同一条记录。
            # reclaimed 是例外：实例物理上就在上报者那里，按物理持有者重新绑定。
            logger.warning(
                "拒绝非归属 Agent 的 iOS 虚拟机状态上报 vm_id={} owner={} reporter={}",
                vm_id, owner, agent_id,
            )
            return

        if reason == "reclaimed" and owner and owner != agent_id:
            logger.info(
                "iOS 虚拟机按物理持有者重新绑定 Agent vm_id={} owner={} reporter={}",
                vm_id, owner, agent_id,
            )

        vm.assigned_agent_id = agent_id
        if udid:
            vm.udid = udid
        wda_port = details.get("wda_port")
        mjpeg_port = details.get("mjpeg_port")
        if isinstance(wda_port, int):
            vm.wda_port = wda_port
        if isinstance(mjpeg_port, int):
            vm.mjpeg_port = mjpeg_port

        ok = bool(msg.get("ok", True))
        if state == "running" and ok:
            vm.state = "running"
            vm.error_code = ""
            vm.error_message = ""
            vm.started_at = vm.started_at or now_utc()
            # 跑起来才有 UDID，此时把别名从占位符移到真设备上，
            # 设备总览的卡片才显示得出用户起的名字。
            if vm.udid:
                await _try_bind_alias(session, vm, vm.udid)
        elif state == "stopped":
            vm.state = "stopped"
            vm.error_code = ""
            vm.error_message = ""
            vm.stopped_at = now_utc()
            # 停机不清 udid：实例还在磁盘上，数据留存（方案 §6.5.1）
            vm.wda_port = None
            vm.mjpeg_port = None
            # 别名退回占位符：设备已不在池子里，留着旧 UDID 映射会让下次
            # 启动时反过来撞上自己的别名。
            await _try_bind_alias(session, vm, placeholder_serial(vm.id))
        elif state == "error" or not ok:
            vm.state = "error"
            vm.error_code = reason or "error"
            vm.error_message = str(msg.get("error") or "")
            await _try_bind_alias(session, vm, placeholder_serial(vm.id))
        elif state in ("starting", "stopping"):
            vm.state = state
        else:
            logger.debug("iOS 虚拟机上报了未识别状态，忽略 state={}", state)

        vm.runtime_state = {
            **(vm.runtime_state or {}),
            "last_reason": reason,
            "last_state": state,
            "last_agent_id": agent_id,
            "details": details,
            "updated_at": now_utc().isoformat(),
        }
        await session.commit()


async def handle_vm_reconcile(
    agent_id: str,
    msg: Dict[str, Any],
    hub: Optional[Hub] = None,
) -> None:
    """处理 Agent (重)连后的受管实例对账清单。

    载荷结构与 **Android 一致**（``vm_ids`` / ``running_vm_ids`` / ``stopped_vm_ids``
    三个 id 列表），不用鸿蒙那种带 lease_token 的完整对象数组——我们没有租约要核。

    三件事：
    1. Agent 报的在跑实例 → 由随后的 ``vm_status reason=reclaimed`` 置态
    2. Agent 报的没跑实例 → 明确置 stopped
    3. **库里归本 Agent、但 Agent 没报** → 置 agent_offline（差集收敛）
    4. **Agent 有、库里没有** → 回发删除清理孤儿
    """
    reported = [str(x) for x in (msg.get("vm_ids") or []) if str(x)]
    running = {str(x) for x in (msg.get("running_vm_ids") or []) if str(x)}
    stopped = {str(x) for x in (msg.get("stopped_vm_ids") or []) if str(x)}

    factory = get_session_factory()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(IosSimVmInstance).where(
                        IosSimVmInstance.assigned_agent_id == agent_id
                    )
                )
            )
            .scalars()
            .all()
        )
        known_ids = {vm.id for vm in rows}

        # 差集收敛：库里归我、但 Agent 说没有 → 实例已不在那台机器上
        for vm in rows:
            if vm.id in reported:
                continue
            if vm.state == "draft":
                continue
            vm.state = "agent_offline"
            vm.error_code = "not_on_agent"
            vm.error_message = f"agent {agent_id} 未报告该实例"
            logger.info("iOS 虚拟机实例已不在 Agent 上，置 agent_offline vm_id={}", vm.id)

        # Agent 说没跑的，明确置 stopped
        for vm_id in sorted(stopped):
            vm = await session.get(IosSimVmInstance, vm_id)
            if vm is None:
                continue
            vm.assigned_agent_id = agent_id
            vm.state = "stopped"
            vm.error_code = ""
            vm.error_message = ""
            vm.wda_port = None
            vm.mjpeg_port = None
            vm.stopped_at = vm.stopped_at or now_utc()

        await session.commit()

    # 孤儿清理：Agent 有、库里没有 → 回发删除（复用删除链路，无需单独消息）
    if hub is not None:
        orphans = [vm_id for vm_id in reported if vm_id not in known_ids]
        for vm_id in orphans:
            await hub.send_to_agent(
                agent_id,
                {
                    "type": P.MSG_IOS_SIM_VM_DELETE,
                    "request_id": uuid.uuid4().hex[:16],
                    "vm_id": vm_id,
                    "udid": "",
                },
            )
        if orphans:
            logger.info(
                "iOS 虚拟机孤儿实例已回发删除 agent={} 共 {} 台", agent_id, len(orphans)
            )

    logger.info(
        "iOS 虚拟机对账完成 agent={} 上报 {} 台（在跑 {} / 没跑 {}）",
        agent_id, len(reported), len(running), len(stopped),
    )


# ---------------------------------------------------------------------------
# 能力探查等待器
# ---------------------------------------------------------------------------
@dataclass
class _ProbeState:
    expected: set
    responses: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    failures: Dict[str, str] = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)


class IosSimCapabilityWaiter:
    """向所有在线 Agent 广播探查、收齐回包。

    与鸿蒙的差异：**没有 ``excluded_ports_for``**。鸿蒙需要把各 Agent 上报的已占
    端口汇总起来供 Server 分配端口用；我们不分配端口，探查结果只用于"这台 Agent
    能不能承接"这一个判断（方案 §6.5.5）。
    """

    def __init__(self) -> None:
        self._pending: Dict[str, _ProbeState] = {}
        self._latest: Dict[str, Dict[str, Any]] = {}

    async def probe(
        self,
        *,
        hub: Hub,
        vm: IosSimVmInstance,
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
            "type": P.MSG_IOS_SIM_VM_CAPABILITY_PROBE,
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
        vm: IosSimVmInstance,
        agent_id: str,
        timeout_sec: float = 60.0,
    ) -> Dict[str, Any]:
        """只探一台 Agent。下发前做最后一次确认用。"""
        request_id = uuid.uuid4().hex[:16]
        state = _ProbeState(expected={agent_id})
        self._pending[request_id] = state
        payload = {
            "type": P.MSG_IOS_SIM_VM_CAPABILITY_PROBE,
            **vm_payload(vm, request_id=request_id),
        }
        try:
            if not await hub.send_to_agent(agent_id, payload):
                state.failures[agent_id] = "send_failed"
            else:
                try:
                    await asyncio.wait_for(state.event.wait(), timeout=timeout_sec)
                except asyncio.TimeoutError:
                    pass
            response = state.responses.get(agent_id)
            if response is not None:
                return dict(response)
            return {
                "ok": False,
                "reason": (
                    "下发失败"
                    if state.failures.get(agent_id) == "send_failed"
                    else "探查超时，Agent 未回应"
                ),
                "warning": "",
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
        """Agent 断线时结束正在等它的探查，避免等满超时。"""
        self._latest.pop(agent_id, None)
        for state in self._pending.values():
            if (
                agent_id in state.expected
                and agent_id not in state.responses
                and agent_id not in state.failures
            ):
                state.failures[agent_id] = "agent_disconnected"
            if state.expected.issubset(state.responses.keys() | state.failures.keys()):
                state.event.set()

    def forget_agent(self, agent_id: str) -> None:
        self._latest.pop(agent_id, None)

    def latest_for(self, agent_id: str) -> Dict[str, Any]:
        return dict(self._latest.get(agent_id) or {})

    @staticmethod
    def _rows(agents: Iterable[Dict[str, Any]], state: _ProbeState) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for agent in agents:
            agent_id = str(agent.get("agent_id") or "")
            response = state.responses.get(agent_id)
            if response:
                ok = bool(response.get("ok"))
                result.append(
                    {
                        "agent_id": agent_id,
                        "agent_name": agent.get("agent_name") or "",
                        "host_os": agent.get("host_os") or "",
                        "ok": ok,
                        "reason": str(response.get("reason") or ("可用" if ok else "不可用")),
                        "warning": str(response.get("warning") or ""),
                        "details": response.get("details")
                        if isinstance(response.get("details"), dict)
                        else {},
                    }
                )
                continue
            failure = state.failures.get(agent_id)
            result.append(
                {
                    "agent_id": agent_id,
                    "agent_name": agent.get("agent_name") or "",
                    "host_os": agent.get("host_os") or "",
                    "ok": False,
                    "reason": {
                        "send_failed": "下发失败",
                        "agent_disconnected": "Agent 已断开",
                    }.get(failure or "", "探查超时，Agent 未回应"),
                    "warning": "",
                    "details": {},
                }
            )
        return result


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
