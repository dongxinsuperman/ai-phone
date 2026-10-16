"""One bounded capability batch, exposed as events or the existing JSON result."""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import anyio
from starlette.responses import StreamingResponse

from .vm_lifecycle import OPERATION_SEND_TIMEOUT_SEC, send_operation_once


@dataclass
class _ProbeState:
    request_id: str
    agents: dict[str, dict]
    hub: Any
    deadline: float
    sequence: int
    terminal: dict[str, str] = field(default_factory=dict)
    responses: dict[str, dict] = field(default_factory=dict)
    updates: asyncio.Queue = field(default_factory=asyncio.Queue)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)


class CapabilityProbeWaiter:
    def __init__(
        self, message_type: str, payload: Callable, *, sort_rows: bool = True,
        send_failure_reason: str = "下发失败", timeout_reason: str = "探查超时",
        failure_warning: bool = False, cache_latest: bool = False,
    ) -> None:
        self._message_type, self._payload = message_type, payload
        self._sort_rows = sort_rows
        self._failure_reasons = {
            "probing": "探查中…", "send_failed": send_failure_reason,
            "timeout": timeout_reason, "offline": "Agent 已断开",
        }
        self._failure_warning, self._cache_latest = failure_warning, cache_latest
        self._pending: dict[str, _ProbeState] = {}
        self._latest: dict[str, dict] = {}
        self._latest_sequence: dict[str, int] = {}
        self._sequence = 0

    def _prepare(self, hub, vm, agents, timeout_sec):
        # Prepare before StreamingResponse sends headers. Only Hub's online
        # snapshot is used; a missing Agent directory is not invented here.
        unique = {}
        for agent in agents:
            agent_id = str(agent.get("agent_id") or "").strip()
            if agent_id and agent_id not in unique:
                unique[agent_id] = {**agent, "agent_id": agent_id}
        self._sequence += 1
        request_id = uuid.uuid4().hex[:16]
        payload = {"type": self._message_type, **self._payload(vm, request_id=request_id)}
        state = _ProbeState(request_id, unique, hub,
                            asyncio.get_running_loop().time() + max(0.0, timeout_sec), self._sequence)
        return state, payload

    def probe_stream(self, *, hub, vm, timeout_sec: float = 60.0) -> AsyncIterator[dict]:
        state, payload = self._prepare(hub, vm, hub.snapshot().get("agents") or [], timeout_sec)
        return self._events(state, payload)

    async def probe(self, *, hub, vm, timeout_sec: float = 60.0) -> dict:
        return await self.collect_probe(self.probe_stream(hub=hub, vm=vm, timeout_sec=timeout_sec))

    async def collect_probe(self, events: AsyncIterator[dict]) -> dict:
        rows, request_id = {}, ""
        async with aclosing(events):
            async for event in events:
                request_id = event["request_id"]
                if event["type"] == "snapshot":
                    rows = {row["agent_id"]: row for row in event["agents"]}
                elif event["type"] == "agent":
                    rows[event["agent"]["agent_id"]] = event["agent"]
        result = []
        for row in rows.values():
            legacy = {key: value for key, value in row.items() if key != "probe_state"}
            if not self._failure_warning and row["probe_state"] not in {"available", "unavailable"}:
                legacy.pop("warning", None)
            result.append(legacy)
        if self._sort_rows:
            result.sort(key=lambda row: (not row["ok"], str(row.get("agent_name") or row["agent_id"])))
        return {"request_id": request_id, "agents": result}

    async def probe_agent(self, *, hub, vm, agent_id: str, timeout_sec: float = 60.0) -> dict:
        agent = next((a for a in hub.snapshot().get("agents") or [] if a.get("agent_id") == agent_id),
                     {"agent_id": agent_id})
        state, payload = self._prepare(hub, vm, [agent], timeout_sec)
        async with aclosing(self._events(state, payload)) as events:
            async for _event in events:
                pass
        if state.terminal.get(agent_id) in {"available", "unavailable"}:
            return dict(state.responses[agent_id])
        row = self._row(state, agent_id)
        result = {"ok": False, "reason": row["reason"], "details": {}}
        if self._failure_warning:
            result["warning"] = ""
        return result

    def _row(self, state: _ProbeState, agent_id: str) -> dict:
        agent, outcome = state.agents[agent_id], state.terminal.get(agent_id, "probing")
        response = state.responses.get(agent_id, {}) if outcome in {"available", "unavailable"} else {}
        ok = outcome == "available"
        return {
            "agent_id": agent_id, "agent_name": agent.get("agent_name") or "",
            "host_os": agent.get("host_os") or "", "probe_state": outcome, "ok": ok,
            "reason": str(response.get("reason") or (
                ("可用" if ok else "不可用") if response else self._failure_reasons[outcome])),
            "warning": str(response.get("warning") or ""),
            "details": response.get("details") if isinstance(response.get("details"), dict) else {},
        }

    def _finish(self, state, agent_id, outcome, response=None, *, disconnected=False) -> bool:
        if agent_id not in state.agents or (
            agent_id in state.terminal and not disconnected
        ) or state.terminal.get(agent_id) == "offline":
            return False
        # A disconnect is a new transport fact, not a duplicate capability
        # reply: it can invalidate an available row while this batch is open.
        state.terminal[agent_id] = outcome
        if response is not None:
            state.responses[agent_id] = dict(response)
        state.updates.put_nowait({"type": "agent", "request_id": state.request_id,
                                 "agent": self._row(state, agent_id)})
        return True

    def resolve(self, agent_id: str, msg: dict) -> bool:
        state = self._pending.get(str(msg.get("request_id") or ""))
        if state is None or agent_id not in state.agents or agent_id in state.terminal:
            return False
        if not state.hub.has_agent(agent_id):
            self._finish(state, agent_id, "offline")
            return False
        if asyncio.get_running_loop().time() >= state.deadline:
            self._finish(state, agent_id, "timeout")
            return False
        accepted = self._finish(state, agent_id, "available" if msg.get("ok") else "unavailable", msg)
        if accepted and self._cache_latest and state.sequence >= self._latest_sequence.get(agent_id, -1):
            self._latest[agent_id] = dict(msg)
            self._latest_sequence[agent_id] = state.sequence
        return accepted

    def discard_agent(self, agent_id: str) -> None:
        self.forget_agent(agent_id)
        for state in self._pending.values():
            self._finish(state, agent_id, "offline", disconnected=True)
            task = state.tasks.get(agent_id)
            if task is not None and not task.done():
                task.cancel()

    def forget_agent(self, agent_id: str) -> None:
        self._latest.pop(agent_id, None)
        self._latest_sequence.pop(agent_id, None)

    def latest_for(self, agent_id: str) -> dict:
        return dict(self._latest.get(agent_id) or {})

    async def _send(self, state, agent_id, payload):
        if agent_id in state.terminal:
            return
        if not state.hub.has_agent(agent_id):
            self._finish(state, agent_id, "offline")
            return
        remaining = state.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            self._finish(state, agent_id, "timeout")
            return
        sent = await send_operation_once(state.hub, agent_id, payload,
            timeout_sec=min(OPERATION_SEND_TIMEOUT_SEC, remaining))
        if not sent:
            outcome = "offline" if not state.hub.has_agent(agent_id) else (
                "timeout" if asyncio.get_running_loop().time() >= state.deadline else "send_failed")
            self._finish(state, agent_id, outcome)

    async def _events(self, state, payload):
        self._pending[state.request_id] = state
        try:
            # Nothing has been sent or awaited when the initial rows appear.
            yield {"type": "snapshot", "request_id": state.request_id,
                   "agents": [self._row(state, agent_id) for agent_id in state.agents]}
            state.tasks = {agent_id: asyncio.create_task(self._send(state, agent_id, payload),
                name=f"vm-probe-send:{state.request_id}:{agent_id}") for agent_id in state.agents}
            while len(state.terminal) < len(state.agents) or not state.updates.empty():
                if not state.updates.empty():
                    yield state.updates.get_nowait()
                    continue
                remaining = state.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    for agent_id in state.agents:
                        self._finish(state, agent_id, "timeout")
                    continue
                try:
                    yield await asyncio.wait_for(state.updates.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    for agent_id in state.agents:
                        self._finish(state, agent_id, "timeout")
        finally:
            self._pending.pop(state.request_id, None)
            for task in state.tasks.values():
                if not task.done():
                    task.cancel()
            if state.tasks:
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(*state.tasks.values(), return_exceptions=True)
        yield {"type": "done", "request_id": state.request_id}


class _ProbeStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Disconnect can happen while ASGI sends a yielded body chunk,
            # when async-for itself would not close the suspended iterator.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


def probe_stream_response(events: AsyncIterator[dict]) -> StreamingResponse:
    async def ndjson():
        async with aclosing(events):
            async for event in events:
                yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    return _ProbeStreamingResponse(ndjson(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
