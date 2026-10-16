"""Capability cache contracts using real probe lifetimes and in-memory Hubs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from ai_phone.server.harmony_vm import api as harmony_api
from ai_phone.server.harmony_vm import service as harmony_service
from ai_phone.server.harmony_vm.models import HarmonyVmInstance, HarmonyVmPortLease
from ai_phone.server.hub import Hub
from ai_phone.server.ios_sim import service as ios_service
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.vm_lifecycle import cancel_operation_timeout
from ai_phone.shared import protocol as P


pytestmark = pytest.mark.asyncio


class ProbeHub:
    def __init__(self, agent_ids=("agent-a",)):
        self.agent_ids = tuple(agent_ids)
        self.online = set(agent_ids)
        self.sent = asyncio.Queue()

    def snapshot(self):
        return {"agents": [
            {"agent_id": agent_id, "agent_name": agent_id, "host_os": "Darwin"}
            for agent_id in self.agent_ids if agent_id in self.online
        ]}

    def has_agent(self, agent_id):
        return agent_id in self.online

    async def send_to_agent(self, agent_id, payload):
        await self.sent.put((agent_id, dict(payload)))
        return self.has_agent(agent_id)


@pytest.fixture(params=("ios", "harmony"))
def cache_case(request):
    common = {"id": f"cache-{request.param}", "name": "cache-vm", "alias": "cache-vm", "config_json": {}}
    if request.param == "ios":
        vm = SimpleNamespace(
            **common, device_type="iPhone-17", device_type_name="iPhone 17",
            runtime="iOS-26", runtime_name="iOS 26", os_version="26.0",
        )
        service = ios_service
        waiter = service.IosSimCapabilityWaiter()
    else:
        vm = SimpleNamespace(
            **common, device_type="Phone", os_version="HarmonyOS 6.0", api_version="20",
            abi="arm64", image_id="Phone|HarmonyOS 6.0|arm64", screen_profile="Default Phone",
            screen_width=1080, screen_height=2340, density=420, screen_size_in="6.0",
            memory_gb=4, storage_gb=8, boot_mode="cold",
        )
        service = harmony_service
        waiter = service.HarmonyVmCapabilityWaiter()
    return SimpleNamespace(waiter=waiter, vm=vm, platform=request.param, service=service)


def native_reply(request_id, revision, *, ok=True):
    return {
        "request_id": request_id,
        "ok": ok,
        "reason": f"native-result-{revision}",
        "warning": "native soft limit",
        "details": {
            "revision": revision,
            "hdc_target_ports": [10001 + revision],
            "fport_ports": [16557 + revision],
            "tcp_listener_ports": [17000 + revision],
            "native_nested": {"model": "exact-model", "supported": ["arm64"]},
        },
        # These fields are deliberately outside the streamed row schema.
        "resolved_abi": "arm64",
        "native_tool_version": "native-test-version",
        "native_create_method": {"mode": "default", "arguments": []},
    }


async def answer_single_probe(cache_case, hub, revision, *, ok=True, agent_id="agent-a"):
    task = asyncio.create_task(cache_case.waiter.probe_agent(
        hub=hub, vm=cache_case.vm, agent_id=agent_id, timeout_sec=5,
    ))
    try:
        target, command = await asyncio.wait_for(hub.sent.get(), timeout=1)
        assert target == agent_id
        reply = native_reply(command["request_id"], revision, ok=ok)
        assert cache_case.waiter.resolve(agent_id, reply) is True
        result = await asyncio.wait_for(task, timeout=1)
        return reply, result
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def open_stream(cache_case, hub):
    events = cache_case.waiter.probe_stream(hub=hub, vm=cache_case.vm, timeout_sec=5)
    next_update = None
    try:
        snapshot = await anext(events)
        assert snapshot["type"] == "snapshot"
        assert all(row["probe_state"] == "probing" for row in snapshot["agents"])
        next_update = asyncio.create_task(anext(events))
        expected = {row["agent_id"] for row in snapshot["agents"]}
        sent = set()
        for _ in expected:
            target, command = await asyncio.wait_for(hub.sent.get(), timeout=1)
            assert command["request_id"] == snapshot["request_id"]
            sent.add(target)
        assert sent == expected
        yield SimpleNamespace(events=events, snapshot=snapshot, next_update=next_update)
    finally:
        if next_update is not None:
            if not next_update.done():
                next_update.cancel()
            await asyncio.gather(next_update, return_exceptions=True)
        await events.aclose()


@pytest.mark.parametrize("ok", [True, False])
async def test_single_agent_probe_preserves_every_raw_field(cache_case, ok):
    hub = ProbeHub()
    reply, result = await answer_single_probe(cache_case, hub, 1, ok=ok)
    assert result == reply
    assert cache_case.waiter.latest_for("agent-a") == reply
    result["native_tool_version"] = "caller changed its own result"
    assert cache_case.waiter.latest_for("agent-a")["native_tool_version"] == "native-test-version"


async def test_earlier_batch_reply_cannot_overwrite_fresh_single_probe_cache(cache_case):
    hub = ProbeHub()
    async with open_stream(cache_case, hub) as older:
        fresh_reply, fresh_result = await answer_single_probe(cache_case, hub, 2)
        assert fresh_reply["request_id"] != older.snapshot["request_id"]
        assert fresh_result == fresh_reply
        old_reply = native_reply(older.snapshot["request_id"], 1)
        assert cache_case.waiter.resolve("agent-a", old_reply) is True
        old_event = await asyncio.wait_for(older.next_update, timeout=1)
        assert old_event["agent"]["details"]["revision"] == 1
        # The old request still gets its own answer; only the shared cache is
        # fenced by probe order, rather than whichever reply arrives last.
        assert cache_case.waiter.latest_for("agent-a") == fresh_reply


async def test_closing_stream_rejects_late_reply_without_erasing_previous_cache(cache_case):
    hub = ProbeHub(("agent-a", "agent-b"))
    previous, _ = await answer_single_probe(cache_case, hub, 1, agent_id="agent-b")
    async with open_stream(cache_case, hub) as batch:
        request_id = batch.snapshot["request_id"]
        accepted = native_reply(request_id, 2)
        assert cache_case.waiter.resolve("agent-a", accepted) is True
        event = await asyncio.wait_for(batch.next_update, timeout=1)
        assert event["agent"]["agent_id"] == "agent-a"
        # Agent B is still pending, but no __anext__ call is active. Closing
        # here exercises stream closure itself, not an artificial expired ID.
    assert cache_case.waiter.resolve("agent-b", native_reply(request_id, 99)) is False
    assert cache_case.waiter.latest_for("agent-b") == previous
    assert cache_case.waiter.latest_for("agent-a") == accepted


async def test_missing_unknown_unexpected_and_terminal_replies_do_not_seed_cache(cache_case):
    hub = ProbeHub()
    waiter = cache_case.waiter
    async with open_stream(cache_case, hub) as batch:
        request_id = batch.snapshot["request_id"]
        hub.online.add("unexpected-agent")
        assert waiter.resolve("unexpected-agent", native_reply(request_id, 1)) is False
        assert waiter.latest_for("unexpected-agent") == {}
        assert waiter.resolve("agent-a", native_reply("", 1)) is False
        assert waiter.resolve("agent-a", native_reply("unknown-request", 1)) is False
        assert waiter.latest_for("agent-a") == {}

        first = native_reply(request_id, 2)
        assert waiter.resolve("agent-a", first) is True
        event = await asyncio.wait_for(batch.next_update, timeout=1)
        assert event["agent"]["probe_state"] == "available"
        assert waiter.resolve("agent-a", native_reply(request_id, 99, ok=False)) is False
        assert waiter.latest_for("agent-a") == first


async def test_expired_current_request_cannot_replace_previous_cache(cache_case):
    hub = ProbeHub()
    previous, _ = await answer_single_probe(cache_case, hub, 1)
    events = cache_case.waiter.probe_stream(hub=hub, vm=cache_case.vm, timeout_sec=0)
    try:
        snapshot = await anext(events)
        assert cache_case.waiter.resolve("agent-a", native_reply(snapshot["request_id"], 99)) is False
        updates = [event async for event in events]
    finally:
        await events.aclose()
    rows = [event["agent"] for event in updates if event["type"] == "agent"]
    assert len(rows) == 1 and rows[0]["probe_state"] == "timeout"
    assert updates[-1]["type"] == "done"
    assert cache_case.waiter.latest_for("agent-a") == previous
    assert hub.sent.empty()


async def test_cancelled_single_probe_late_reply_cannot_replace_previous_cache(cache_case):
    hub = ProbeHub()
    previous, _ = await answer_single_probe(cache_case, hub, 1)
    task = asyncio.create_task(cache_case.waiter.probe_agent(
        hub=hub, vm=cache_case.vm, agent_id="agent-a", timeout_sec=5,
    ))
    try:
        target, command = await asyncio.wait_for(hub.sent.get(), timeout=1)
        assert target == "agent-a"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cache_case.waiter.resolve("agent-a", native_reply(command["request_id"], 99)) is False
        assert cache_case.waiter.latest_for("agent-a") == previous
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_late_offline_database_tail_does_not_erase_reconnected_probe_cache(cache_case, monkeypatch):
    hub = ProbeHub()
    waiter = cache_case.waiter
    await answer_single_probe(cache_case, hub, 1)
    hub.online.remove("agent-a")
    waiter.discard_agent("agent-a")
    assert waiter.latest_for("agent-a") == {}

    database_reads = []

    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def execute(self, statement):
            database_reads.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        async def commit(self):
            return None

    monkeypatch.setattr(cache_case.service, "get_session_factory", lambda: EmptySession)
    monkeypatch.setattr(cache_case.service, "get_capability_waiter", lambda: waiter)
    old_database_waiting, release_old_database = asyncio.Event(), asyncio.Event()

    async def old_disconnect_database_tail():
        # The old connection already discarded its cache. Its preceding DB
        # cleanup is suspended before the platform-specific offline callback.
        old_database_waiting.set()
        await release_old_database.wait()
        return await cache_case.service.mark_agent_vms_offline("agent-a")

    old_tail = asyncio.create_task(old_disconnect_database_tail())
    try:
        await asyncio.wait_for(old_database_waiting.wait(), timeout=1)
        hub.online.add("agent-a")
        fresh, result = await answer_single_probe(cache_case, hub, 2)
        assert result == fresh
        fresh_sequence = waiter._latest_sequence["agent-a"]  # noqa: SLF001

        release_old_database.set()
        assert await asyncio.wait_for(old_tail, timeout=1) == 0
        assert len(database_reads) == 1
        assert waiter.latest_for("agent-a") == fresh
        assert waiter._latest_sequence["agent-a"] == fresh_sequence  # noqa: SLF001
        if cache_case.platform == "harmony":
            assert waiter.excluded_ports_for("agent-a") == {10003, 16559, 17002}
    finally:
        release_old_database.set()
        if not old_tail.done():
            old_tail.cancel()
        await asyncio.gather(old_tail, return_exceptions=True)


@pytest.mark.parametrize("port_field", [
    "hdc_target_ports", "fport_ports", "tcp_listener_ports", "local_excluded_ports",
])
async def test_harmony_dispatch_keeps_its_live_port_evidence_when_newer_batch_owns_cache(
    session, monkeypatch, port_field
):
    """A newer cache revision cannot erase this operation's live exclusions."""
    waiter = harmony_service.HarmonyVmCapabilityWaiter()
    monkeypatch.setattr(harmony_api, "get_capability_waiter", lambda: waiter)
    target_vm = HarmonyVmInstance(name="port-safe-dispatch", alias="port-safe-dispatch")
    other_vm = HarmonyVmInstance(name="other-requirement", alias="other-requirement")
    session.add_all([target_vm, other_vm])
    await session.commit()
    target_id = target_vm.id

    # Exercise the real Hub/dispatch/lease path with an in-memory socket only.
    hub = Hub()
    hub.vm_lock_store = DeviceLockStore()
    hub.sent = asyncio.Queue()

    class AgentSocket:
        async def send_json(self, payload):
            await hub.sent.put(("agent-a", dict(payload)))

    await hub.register_agent("agent-a", "Agent A", "Darwin", AgentSocket(), host_id="host-a")
    target_case = SimpleNamespace(waiter=waiter, vm=target_vm)
    await answer_single_probe(target_case, hub, 1)
    assert waiter._latest_sequence["agent-a"] == 1  # noqa: SLF001

    dispatch = asyncio.create_task(harmony_api._send_start(target_id, "agent-a", session, hub))
    try:
        target, dispatch_probe = await asyncio.wait_for(hub.sent.get(), timeout=2)
        assert target == "agent-a" and dispatch_probe["type"] == P.MSG_HARMONY_VM_CAPABILITY_PROBE
        assert dispatch_probe["vm_id"] == target_id
        # The dispatch's sequence-2 probe is deliberately still awaiting its
        # answer when another VM's sequence-3 candidate query completes first.
        other_case = SimpleNamespace(waiter=waiter, vm=other_vm)
        async with open_stream(other_case, hub) as newer:
            newer_reply = {
                "request_id": newer.snapshot["request_id"], "ok": False,
                "reason": "another VM requirement is unavailable", "details": {},
            }
            assert newer_reply["request_id"] != dispatch_probe["request_id"]
            assert waiter.resolve("agent-a", newer_reply) is True
            newer_event = await asyncio.wait_for(newer.next_update, timeout=1)
            assert newer_event["agent"]["probe_state"] == "unavailable"
            assert waiter.latest_for("agent-a") == newer_reply
            assert waiter._latest_sequence["agent-a"] == 3  # noqa: SLF001
            assert waiter.excluded_ports_for("agent-a") == set()

            live_reply = {
                "request_id": dispatch_probe["request_id"], "ok": True,
                "reason": "dispatch requirement is available", "details": {port_field: [10000]},
            }
            assert waiter.resolve("agent-a", live_reply) is True
            response = await asyncio.wait_for(dispatch, timeout=5)
            assert response["sent"] is True
            assert response["instance"]["id"] == target_id
            assert response["instance"]["state"] == "starting"
            assert response["instance"]["hdc_port"] == 10001
            target, start = await asyncio.wait_for(hub.sent.get(), timeout=1)
            assert target == "agent-a" and start["type"] == P.MSG_HARMONY_VM_START
            assert start["vm_id"] == target_id and start["assigned_port"] == 10001

            await session.refresh(target_vm)
            assert target_vm.hdc_port == 10001 and target_vm.hdc_serial == "127.0.0.1:10001"
            lease = await session.get(HarmonyVmPortLease, 10001)
            assert lease is not None and lease.vm_id == target_id
            assert await session.get(HarmonyVmPortLease, 10000) is None
            # Do not "fix" allocation by rolling the global cache back to the
            # older successful reply: other consumers still need revision 3.
            assert waiter.latest_for("agent-a") == newer_reply
            assert waiter._latest_sequence["agent-a"] == 3  # noqa: SLF001
            assert waiter.excluded_ports_for("agent-a") == set()
    finally:
        if not dispatch.done():
            dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)
        cancel_operation_timeout(target_vm)
