"""Exact-socket disconnect notifies every probe before slow DB persistence."""
import asyncio
import importlib

import pytest

from ai_phone.server.android_vm.service import VmCapabilityWaiter
from ai_phone.server.harmony_vm.service import HarmonyVmCapabilityWaiter
from ai_phone.server.ios_sim.service import IosSimCapabilityWaiter
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore


class FakeSocket:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def send_json(self, payload):
        await self.messages.put(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced_socket", [False, True])
async def test_disconnect_notifies_all_probe_streams_before_db_and_keeps_replacement(
    monkeypatch, replaced_socket,
):
    module = importlib.import_module("ai_phone.server.ws.agent_ws")
    hub, old_socket = Hub(), FakeSocket()
    await hub.register_agent("agent", "Test", "test", old_socket)
    waiters = [VmCapabilityWaiter(), IosSimCapabilityWaiter(), HarmonyVmCapabilityWaiter()]
    for waiter in waiters:
        waiter._payload = lambda vm, request_id: {"vm_id": vm, "request_id": request_id}
    for getter, waiter in zip(
        ["get_capability_waiter", "get_ios_sim_capability_waiter", "get_harmony_capability_waiter"], waiters,
    ):
        monkeypatch.setattr(module, getter, lambda value=waiter: value)
    database_entered, release_database = asyncio.Event(), asyncio.Event()
    async def blocked_database(*_args):
        database_entered.set()
        await release_database.wait()
    async def mark_offline(*_args, **_kwargs):
        return 0
    monkeypatch.setattr(module, "_offline_devices", blocked_database)
    for name in ["mark_agent_vms_unavailable", "mark_agent_harmony_vms_offline", "mark_agent_ios_sim_vms_offline"]:
        monkeypatch.setattr(module, name, mark_offline)
    streams = [waiter.probe_stream(hub=hub, vm="vm", timeout_sec=10) for waiter in waiters]
    snapshots = [await anext(stream) for stream in streams]
    pending = [asyncio.create_task(anext(stream)) for stream in streams]
    disconnect = None
    try:
        for _ in streams:
            await asyncio.wait_for(old_socket.messages.get(), 0.2)
        if replaced_socket:
            await hub.register_agent("agent", "New", "test", FakeSocket())
        disconnect = asyncio.create_task(module._on_disconnect(hub, DeviceLockStore(), "agent", set(), ws=old_socket))
        if replaced_socket:
            await asyncio.wait_for(disconnect, 0.2)
            assert not database_entered.is_set()
            for waiter, snapshot in zip(waiters, snapshots):
                assert waiter.resolve("agent", {"request_id": snapshot["request_id"], "ok": True})
        else:
            await asyncio.wait_for(database_entered.wait(), 0.2)
            assert not disconnect.done(), "the DB tail should still be held"
        results = await asyncio.wait_for(asyncio.gather(*pending), 0.2)
        assert all(result["agent"]["probe_state"] == (
            "available" if replaced_socket else "offline") for result in results)
        for stream in streams:
            assert (await anext(stream))["type"] == "done"
        assert all(not waiter._pending for waiter in waiters)
    finally:
        release_database.set()
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if disconnect is not None:
            await disconnect
        for stream in streams:
            await stream.aclose()
