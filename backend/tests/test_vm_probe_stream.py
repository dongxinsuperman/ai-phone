"""Capability batching uses fake Agent transports, never devices or sockets."""
from __future__ import annotations

import asyncio
import time

import pytest

from ai_phone.server.android_vm.service import VmCapabilityWaiter
from ai_phone.server.ios_sim.service import IosSimCapabilityWaiter
from ai_phone.server.harmony_vm.service import HarmonyVmCapabilityWaiter


@pytest.fixture(params=[VmCapabilityWaiter, IosSimCapabilityWaiter, HarmonyVmCapabilityWaiter])
def waiter(request):
    value = request.param()
    value._payload = lambda vm, request_id: {"vm_id": vm, "request_id": request_id}
    return value


class ProbeHub:
    def __init__(self, agents):
        self.online = set(agents)
        self.agents = list(agents)
        self.started, self.active, self.cancelled = set(), set(), set()
        self.all_started = asyncio.Event()
        self.behaviors = {}
        self.messages = {}

    def snapshot(self):
        return {"agents": [{"agent_id": agent, "agent_name": agent, "host_os": "test"}
                           for agent in self.agents if agent in self.online]}

    def has_agent(self, agent_id):
        return agent_id in self.online

    async def send_to_agent(self, agent_id, payload):
        self.started.add(agent_id)
        self.active.add(agent_id)
        self.messages[agent_id] = dict(payload)
        if len(self.started) == len(set(self.agents)):
            self.all_started.set()
        try:
            behavior = self.behaviors.get(agent_id)
            return await behavior(payload) if behavior else True
        except asyncio.CancelledError:
            self.cancelled.add(agent_id)
            raise
        finally:
            self.active.discard(agent_id)


async def never(_payload):
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_fast_reply_streams_before_slow_send_or_reply_and_close_cleans_tasks(waiter):
    hub = ProbeHub(["slow-send", "fast", "slow-reply"])
    hub.behaviors["slow-send"] = never

    async def answer(payload):
        await hub.all_started.wait()
        assert waiter.resolve("fast", {"request_id": payload["request_id"], "ok": True,
                                       "details": {"proof": "fast"}})
        return True

    hub.behaviors["fast"] = answer
    events = waiter.probe_stream(hub=hub, vm="vm", timeout_sec=1)
    snapshot = await anext(events)
    assert [row["agent_id"] for row in snapshot["agents"]] == hub.agents
    assert all(row["probe_state"] == "probing" and not row["ok"] for row in snapshot["agents"])
    assert not hub.started, "snapshot must precede all sends"
    try:
        first = await asyncio.wait_for(anext(events), 0.2)
        assert first["type"] == "agent" and first["agent"]["agent_id"] == "fast"
        assert first["agent"]["probe_state"] == "available"
        assert first["agent"]["details"] == {"proof": "fast"}
        assert hub.started == set(hub.agents)
        assert "slow-send" in hub.active
    finally:
        await events.aclose()
    assert not waiter._pending and not hub.active
    assert "slow-send" in hub.cancelled
    assert not waiter.resolve("slow-reply", {"request_id": snapshot["request_id"], "ok": True})


@pytest.mark.asyncio
async def test_one_batch_deadline_includes_sends_and_reply_wait(waiter):
    hub = ProbeHub(["hang-one", "hang-two", "no-reply"])
    hub.behaviors.update({"hang-one": never, "hang-two": never})
    started = time.monotonic()
    rows = [event async for event in waiter.probe_stream(hub=hub, vm="vm", timeout_sec=0.04)]
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, "three Agents must not consume three separate deadlines"
    assert rows[0]["type"] == "snapshot" and rows[-1]["type"] == "done"
    terminal = [row["agent"] for row in rows if row["type"] == "agent"]
    assert len(terminal) == 3 and all(row["probe_state"] == "timeout" for row in terminal)
    assert hub.cancelled == {"hang-one", "hang-two"}
    assert not waiter._pending and not hub.active


@pytest.mark.asyncio
async def test_false_exception_and_bounded_stuck_send_finish_without_reply_deadline(waiter, monkeypatch):
    import ai_phone.server.vm_probe as probe_module

    monkeypatch.setattr(probe_module, "OPERATION_SEND_TIMEOUT_SEC", 0.02)
    hub = ProbeHub(["false", "exception", "stuck"])
    async def false(_payload):
        return False
    async def failure(_payload):
        raise ConnectionError("fake failed socket")
    hub.behaviors.update({"false": false, "exception": failure, "stuck": never})
    started = time.monotonic()
    events = [event async for event in waiter.probe_stream(hub=hub, vm="vm", timeout_sec=5)]
    assert time.monotonic() - started < 0.2
    assert [event["type"] for event in events] == ["snapshot", "agent", "agent", "agent", "done"]
    assert all(event["agent"]["probe_state"] == "send_failed" for event in events[1:-1])
    assert "stuck" in hub.cancelled and not hub.active and not waiter._pending


@pytest.mark.asyncio
async def test_disconnect_finishes_waiting_send_and_invalidates_available_row(waiter):
    hub = ProbeHub(["ready", "waiting"])
    hub.behaviors["waiting"] = never
    async def answer(payload):
        await hub.all_started.wait()
        waiter.resolve("ready", {"request_id": payload["request_id"], "ok": True})
        return True
    hub.behaviors["ready"] = answer
    events = waiter.probe_stream(hub=hub, vm="vm", timeout_sec=10)
    snapshot = await anext(events)
    assert (await asyncio.wait_for(anext(events), 0.2))["agent"]["probe_state"] == "available"
    hub.online.clear()
    waiter.discard_agent("ready")
    waiter.discard_agent("waiting")
    waiter.discard_agent("waiting")
    assert not waiter.resolve("ready", {"request_id": snapshot["request_id"], "ok": True})
    remaining = [event async for event in events]
    assert [event["agent"]["probe_state"] for event in remaining[:-1]] == ["offline", "offline"]
    assert {event["agent"]["agent_id"] for event in remaining[:-1]} == {"ready", "waiting"}
    assert remaining[-1]["type"] == "done"
    assert not waiter._pending and not hub.active


@pytest.mark.asyncio
async def test_wrong_request_foreign_agent_and_duplicate_reply_cannot_change_terminal(waiter):
    hub = ProbeHub(["expected", "later"])
    events = waiter.probe_stream(hub=hub, vm="vm", timeout_sec=1)
    snapshot = await anext(events)
    next_event = asyncio.create_task(anext(events))
    await asyncio.wait_for(hub.all_started.wait(), 0.2)
    assert not waiter.resolve("expected", {"request_id": "other-request", "ok": True})
    assert not waiter.resolve("foreign", {"request_id": snapshot["request_id"], "ok": True})
    assert waiter.resolve("expected", {"request_id": snapshot["request_id"], "ok": False, "reason": "missing image"})
    assert not waiter.resolve("expected", {"request_id": snapshot["request_id"], "ok": True})
    result = await next_event
    assert result["agent"]["probe_state"] == "unavailable" and result["agent"]["reason"] == "missing image"
    await events.aclose()
    assert not waiter.resolve("later", {"request_id": snapshot["request_id"], "ok": True})
    assert not waiter._pending


@pytest.mark.asyncio
async def test_deadline_checked_when_receiving_reply_even_before_timer_runs(waiter):
    hub = ProbeHub(["agent"])
    events = waiter.probe_stream(hub=hub, vm="vm", timeout_sec=10)
    snapshot = await anext(events)
    next_event = asyncio.create_task(anext(events))
    await asyncio.wait_for(hub.all_started.wait(), 0.2)
    waiter._pending[snapshot["request_id"]].deadline = asyncio.get_running_loop().time() - 1
    assert not waiter.resolve("agent", {"request_id": snapshot["request_id"], "ok": True})
    assert (await next_event)["agent"]["probe_state"] == "timeout"
    assert (await anext(events))["type"] == "done"
    await events.aclose()
    assert not waiter._pending


@pytest.mark.asyncio
async def test_cancel_waiting_consumer_immediately_removes_pending_and_send_task(waiter):
    hub = ProbeHub(["agent"])
    hub.behaviors["agent"] = never
    events = waiter.probe_stream(hub=hub, vm="vm", timeout_sec=60)
    await anext(events)
    consumer = asyncio.create_task(anext(events))
    await asyncio.wait_for(hub.all_started.wait(), 0.2)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert not waiter._pending and not hub.active and hub.cancelled == {"agent"}
    await events.aclose()


@pytest.mark.asyncio
async def test_closing_at_snapshot_never_sends_and_deduplicates_online_rows(waiter):
    hub = ProbeHub(["same", "same", ""])
    events = waiter.probe_stream(hub=hub, vm="vm", timeout_sec=1)
    snapshot = await anext(events)
    assert [row["agent_id"] for row in snapshot["agents"]] == ["same"]
    await events.aclose()
    assert not hub.started and not waiter._pending
