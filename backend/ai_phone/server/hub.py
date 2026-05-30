"""WebSocket Hub：Agent 与 Browser 的内存路由表。

职责：
- 维护当前在线 Agent 连接，以及每个 Agent 管辖的设备 serial
- 维护 Browser 订阅：``serial -> set[browser_ws]``
- 提供「按 serial 找到 agent」「按 run_id 找到 agent」两种派发口
- 广播 Agent 上行事件到订阅该 serial 的所有浏览器

并发模型：
- 单进程单 asyncio loop；所有 mutation 段内都没有 await
- 因此不叠 ``asyncio.Lock``（其绑定 loop 的特性在测试/重连场景下会带来跨 loop 报错）
- 真要做多 Server 副本，应该换 Redis pub/sub，届时 locking 由后端存储保证
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket
from loguru import logger


@dataclass
class AgentConn:
    agent_id: str
    agent_name: str
    host_os: str
    ws: WebSocket
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    serials: Set[str] = field(default_factory=set)
    run_ids: Set[str] = field(default_factory=set)


class Hub:
    def __init__(self) -> None:
        self._agents: Dict[str, AgentConn] = {}
        self._serial_to_agent: Dict[str, str] = {}
        self._run_to_agent: Dict[str, str] = {}
        self._subs: Dict[str, Set[WebSocket]] = {}
        # serial -> agent 上报的非持久化元信息（如 unauthorized 原因）。
        # 不落库（避免加 schema migration），下次 hello 来全量刷新。
        self._device_extra: Dict[str, Dict[str, Any]] = {}
        # serial -> WDA 启动阶段（由 MSG_DEVICE_STATUS 单独维护，避免 rescan
        # 全量覆盖 extra 时把 stage 冲掉）。``get_device_extra`` 会合并输出。
        self._device_stage: Dict[str, Dict[str, Any]] = {}
        # serial -> readiness 快照（由 MSG_DEVICE_READINESS 单独维护）。结构约定：
        # ``{"ready": bool, "not_ready_reason": Optional[str], "hint": str,``
        # `` "fail_streak": int, "ts": float, "platform": str}``
        # ready=True 的设备在这里保留最新一条（便于前端判断"曾经 ready 过"），
        # 也可用于 API 层直接渲染"空闲/未就绪"。
        self._device_readiness: Dict[str, Dict[str, Any]] = {}

    def set_device_extra(self, serial: str, extra: Optional[Dict[str, Any]]) -> None:
        """覆盖设备的 rescan 元信息；空 dict / None 会清空该 serial 的条目。

        不影响 ``_device_stage``——rescan 和 MSG_DEVICE_STATUS 两条通路互不覆盖。
        """
        if not extra:
            self._device_extra.pop(serial, None)
        else:
            self._device_extra[serial] = dict(extra)

    def set_device_stage(self, serial: str, stage: Optional[Dict[str, Any]]) -> None:
        """写入设备的 WDA 启动阶段（由 MSG_DEVICE_STATUS 触发）。

        ``stage`` 结构（约定）：
        ``{"stage": "compiling", "title": "...", "hint": "...",``
        `` "elapsed_ms": 1234, "ts": 1700000000.0}``

        ``stage == "ready"`` 会直接清除（既然就绪了就别再在 UI 上提示了）。
        """
        if not stage:
            self._device_stage.pop(serial, None)
            return
        if str(stage.get("stage") or "") == "ready":
            self._device_stage.pop(serial, None)
            return
        self._device_stage[serial] = dict(stage)

    def set_device_readiness(self, serial: str, payload: Optional[Dict[str, Any]]) -> None:
        """写入 readiness 快照；``payload`` 为空视作清除。

        Agent 的 ``ReadinessSupervisor`` 在状态跳变时才发一次 WS 消息；Hub 只
        要每收一次就覆盖存储。拔线时由 ``clear_device_extra`` 一起清掉。
        """
        if not payload:
            self._device_readiness.pop(serial, None)
            return
        self._device_readiness[serial] = dict(payload)

    def get_device_extra(self, serial: str) -> Dict[str, Any]:
        """合并 rescan 元信息 + WDA 启动阶段 + readiness 后返回。

        输出形如 ``{"reason": "...", "wda_stage": {...}, "readiness": {...}}``；
        三路都没东西就返回 ``{}``。API 层把整块塞进 device dict 的 ``extra`` 字段。
        """
        out = dict(self._device_extra.get(serial) or {})
        stage = self._device_stage.get(serial)
        if stage:
            out["wda_stage"] = dict(stage)
        readiness = self._device_readiness.get(serial)
        if readiness:
            out["readiness"] = dict(readiness)
        return out

    def clear_device_extra(self, serials: Set[str]) -> None:
        """拔线 / 删除设备时调用：rescan 元信息和 stage / readiness 一起摘掉。"""
        for s in serials:
            self._device_extra.pop(s, None)
            self._device_stage.pop(s, None)
            self._device_readiness.pop(s, None)

    # ------------------------------------------------------------------
    # Agent 注册 / 注销
    # ------------------------------------------------------------------
    async def register_agent(
        self, agent_id: str, agent_name: str, host_os: str, ws: WebSocket
    ) -> AgentConn:
        old = self._agents.pop(agent_id, None)
        if old is not None:
            logger.warning("Agent {} 重复注册，旧连接将被替换", agent_id)
            for s in list(old.serials):
                self._serial_to_agent.pop(s, None)
            for r in list(old.run_ids):
                self._run_to_agent.pop(r, None)
            try:
                await old.ws.close(code=4000, reason="replaced by new connection")
            except Exception:  # noqa: BLE001
                pass

        conn = AgentConn(agent_id=agent_id, agent_name=agent_name, host_os=host_os, ws=ws)
        self._agents[agent_id] = conn
        logger.info("Agent 上线 | id={} name={} host_os={}", agent_id, agent_name, host_os)
        return conn

    async def unregister_agent(self, agent_id: str) -> Optional[AgentConn]:
        conn = self._agents.pop(agent_id, None)
        if conn is None:
            return None
        for s in list(conn.serials):
            self._serial_to_agent.pop(s, None)
        for r in list(conn.run_ids):
            self._run_to_agent.pop(r, None)
        logger.info("Agent 下线 | id={} serials={}", agent_id, sorted(conn.serials))
        return conn

    async def attach_device(self, agent_id: str, serial: str) -> None:
        conn = self._agents.get(agent_id)
        if conn is None:
            return
        conn.serials.add(serial)
        self._serial_to_agent[serial] = agent_id

    async def detach_device(self, agent_id: str, serial: str) -> None:
        conn = self._agents.get(agent_id)
        if conn is None:
            return
        conn.serials.discard(serial)
        if self._serial_to_agent.get(serial) == agent_id:
            self._serial_to_agent.pop(serial, None)

    async def set_devices(self, agent_id: str, serials: Set[str]) -> None:
        """全量替换一个 Agent 的设备集合，做差分后更新反查表。"""
        conn = self._agents.get(agent_id)
        if conn is None:
            return
        conn.last_seen_at = time.time()
        old = conn.serials
        to_remove = old - serials
        to_add = serials - old
        for s in to_remove:
            if self._serial_to_agent.get(s) == agent_id:
                self._serial_to_agent.pop(s, None)
        for s in to_add:
            self._serial_to_agent[s] = agent_id
        conn.serials = set(serials)

    def touch_agent(self, agent_id: str) -> None:
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.last_seen_at = time.time()

    # ------------------------------------------------------------------
    # Run 路由
    # ------------------------------------------------------------------
    async def bind_run(self, run_id: str, agent_id: str) -> None:
        self._run_to_agent[run_id] = agent_id
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.run_ids.add(run_id)

    async def unbind_run(self, run_id: str) -> None:
        agent_id = self._run_to_agent.pop(run_id, None)
        if agent_id is None:
            return
        conn = self._agents.get(agent_id)
        if conn is not None:
            conn.run_ids.discard(run_id)

    def agent_id_for_serial(self, serial: str) -> Optional[str]:
        return self._serial_to_agent.get(serial)

    def agent_id_for_run(self, run_id: str) -> Optional[str]:
        return self._run_to_agent.get(run_id)

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------
    async def send_to_agent(self, agent_id: str, payload: Dict[str, Any]) -> bool:
        conn = self._agents.get(agent_id)
        if conn is None:
            return False
        try:
            await conn.ws.send_json(payload)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送到 Agent {} 失败：{}", agent_id, exc)
            return False

    async def send_to_serial(self, serial: str, payload: Dict[str, Any]) -> bool:
        aid = self._serial_to_agent.get(serial)
        if aid is None:
            return False
        return await self.send_to_agent(aid, payload)

    def agent_for_serial(self, serial: str) -> Optional[str]:
        """返回当前持有该 serial 的 agent_id（不在线返回 None）。

        next/server-brain 的 RemoteDriver 在构造时需要把 agent_id 快照进
        ``runs.agent_id_at_start``；这是给那条路用的薄 accessor，避免外部直
        接读 ``_serial_to_agent`` 私有字典。
        """
        return self._serial_to_agent.get(serial)

    async def send_to_run(self, run_id: str, payload: Dict[str, Any]) -> bool:
        aid = self._run_to_agent.get(run_id)
        if aid is None:
            return False
        return await self.send_to_agent(aid, payload)

    # ------------------------------------------------------------------
    # 浏览器订阅
    # ------------------------------------------------------------------
    async def subscribe(self, serial: str, ws: WebSocket) -> int:
        """订阅后返回当前订阅人数。调用方可据此判断是否需要触发 mirror 启停。"""
        subs = self._subs.setdefault(serial, set())
        subs.add(ws)
        n = len(subs)
        logger.debug("浏览器订阅 serial={} 订阅数={}", serial, n)
        return n

    async def unsubscribe(self, serial: str, ws: WebSocket) -> int:
        """取消订阅后返回剩余订阅人数（0 表示该 serial 已没人在看）。"""
        subs = self._subs.get(serial)
        if subs is None:
            return 0
        subs.discard(ws)
        n = len(subs)
        if n == 0:
            self._subs.pop(serial, None)
        return n

    def subscriber_count(self, serial: str) -> int:
        return len(self._subs.get(serial, ()))

    async def broadcast_to_serial(self, serial: str, payload: Dict[str, Any]) -> int:
        """推送给所有订阅该 serial 的浏览器，返回成功条数。"""
        subs = list(self._subs.get(serial, ()))
        if not subs:
            return 0

        async def _send(ws: WebSocket) -> bool:
            try:
                await ws.send_json(payload)
                return True
            except Exception:  # noqa: BLE001
                return False

        results = await asyncio.gather(*[_send(w) for w in subs], return_exceptions=False)
        return sum(1 for r in results if r)

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "agents": [
                {
                    "agent_id": c.agent_id,
                    "agent_name": c.agent_name,
                    "host_os": c.host_os,
                    "connected_at": c.connected_at,
                    "last_seen_at": c.last_seen_at,
                    "serials": sorted(c.serials),
                    "run_ids": sorted(c.run_ids),
                }
                for c in self._agents.values()
            ],
            "subscribers": {s: len(subs) for s, subs in self._subs.items()},
        }
