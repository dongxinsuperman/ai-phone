"""Ordinary starts use one current observation, with no native/network I/O."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from ai_phone.server import db
from ai_phone.server.harmony_vm.models import HarmonyVmPortLease
from ai_phone.server.vm_lifecycle import (
    operation_lock_key, runtime, transaction_lock, update_runtime,
)
from tests.test_vm_lifecycle_view_api import (
    AUTH, assert_lifecycle, create_vm, last_command, read_vm,
    view_context, view_platform,  # noqa: F401 -- imported pytest fixtures
)


async def bound_unknown(context):
    vm_id = (await create_vm(context))["id"]
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        vm.assigned_agent_id, vm.state = "agent-1", "error"
        vm.error_message = "此前操作失败"
        update_runtime(vm, owner_host_id="fixture-host", last_failure={"message": "此前操作失败"})
        if context.platform.name == "android":
            vm.adb_serial = "emulator-5554"
        elif context.platform.name == "ios":
            vm.udid = f"existing-{vm_id}"
        else:
            await context.platform.svc.allocate_port_lease(session, vm, agent_id="agent-1")
        await session.commit()
    return vm_id


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["valid", "absent", "wrong_token", "wrong_vm", "occupied"])
@pytest.mark.parametrize("cleanup_history", ["confirmed_reassigned", "unconfirmed", "missing", "pending"])
async def test_harmony_failed_reservation_recovers_through_real_agent_inspection(
    view_context, monkeypatch, tmp_path, proof, cleanup_history,
):
    context = view_context
    if context.platform.name != "harmony":
        pytest.skip("Harmony lease recovery only")
    import psutil
    from ai_phone.agent.harmony_vm import manager as agent_module
    from ai_phone.agent.harmony_vm.registry import register_managed_vm, unregister_managed_vm, managed_vm_owner

    vm_id = await bound_unknown(context)
    manager = agent_module.HarmonyVmManager(runtime_dir=tmp_path)
    known = {"instance_name": "aiphone_harmony_" + vm_id, "instance_path": str(manager.instance_path),
             "hdc_serial": "127.0.0.1:10007", "hdc_port": 10007, "lease_token": "retired-lease",
             "cleanup_confirmed": True, "cleanup_pending": False, "created": True, "ready": False}
    if cleanup_history == "unconfirmed":
        known["cleanup_confirmed"] = False
    elif cleanup_history == "missing":
        known.pop("cleanup_confirmed")
    elif cleanup_history == "pending":
        known.update(cleanup_confirmed=False, cleanup_pending=True)
    manager._known[vm_id] = known
    config = manager.instance_path / known["instance_name"] / "config.ini"
    config.parent.mkdir(parents=True); config.write_text("preserved-user-data")
    manager._save_registry()
    if cleanup_history == "confirmed_reassigned":
        register_managed_vm("unrelated-running-B", known["hdc_serial"], "B-lease")
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter(()))
    monkeypatch.setattr(agent_module, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(manager, "_port_is_free", lambda port: proof != "occupied")
    install_inspection(context, monkeypatch)
    waiter = context.platform.api.get_capability_waiter()

    async def inspect(**kwargs):
        vm = kwargs["vm"]
        observation = manager.inspect_start_reservation(
            {"vm_id": vm.id, "hdc_serial": vm.hdc_serial, "lease_token": vm.lease_token}, None)
        if proof == "absent": observation.pop("unused_reservation", None)
        if proof == "wrong_token": observation["unused_reservation"]["lease_token"] = "wrong-token"
        if proof == "wrong_vm": observation["vm_id"] = "another-instance"
        return {"ok": True, "request_id": "fresh-inspection", "details": {"instance_observation": observation}}

    waiter.inspect_agent = inspect
    try:
        response = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
        assert response.status_code == (200 if proof == "valid" else 409), response.text
        expected_owner = ("unrelated-running-B", "B-lease") if cleanup_history == "confirmed_reassigned" else None
        assert managed_vm_owner(known["hdc_serial"]) == expected_owner
        assert config.read_text() == "preserved-user-data"
        if proof == "valid":
            command = last_command(context, context.platform.start)
            assert command["require_existing_instance"] is True
            assert command["lease_token"] != "retired-lease"
            async with db.get_session_factory()() as session:
                vm = await session.get(context.platform.model, vm_id)
                assert runtime(vm)["lease_history"][-1]["reason"] == "verified_stopped_observation"
            # Exercise the forward path too: another failure before adopting
            # the new reservation must not recreate today's quarantined lease.
            monkeypatch.setattr(manager, "probe", lambda _msg: pytest.fail("expired start reached capability probe"))
            async def send_status(message):
                await context.platform.svc.handle_vm_status("agent-1", message, context.hub)
            expired = {**command, "_operation_deadline": time.monotonic() - 1.0}
            await manager._start_and_report(SimpleNamespace(send=send_status), expired)
            async with db.get_session_factory()() as session:
                vm = await session.get(context.platform.model, vm_id)
                assert vm.lease_token is None and vm.hdc_port is None
                assert await session.get(HarmonyVmPortLease, command["assigned_port"]) is None
            context.hub.sent.clear()
            again = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
            assert again.status_code == 200, again.text
            assert last_command(context, context.platform.start)["lease_token"] != command["lease_token"]
        else:
            assert context.hub.sent == []
    finally:
        unregister_managed_vm("unrelated-running-B", "B-lease")


def observation_for(vm, state="stopped", released=True):
    if hasattr(vm, "udid"):
        identity = {"sim_name": f"aiphone_sim_{vm.id}", "udid": vm.udid}
    elif hasattr(vm, "hdc_serial"):
        identity = {"instance_name": f"aiphone_harmony_{vm.id}", "instance_path": "/fixture-only/instances",
                    "lease_token": vm.lease_token, "hdc_serial": vm.hdc_serial}
    else:
        identity = {"avd_name": f"aiphone_vm_{vm.id}"}
    return {"vm_id": vm.id, "state": state, "identity": identity,
            "resources_released": released, "reason": "fixture-current-native-fact"}


def install_inspection(context, monkeypatch, *, state="stopped", released=True, change=None, malformed=False):
    calls = []

    async def inspect(**kwargs):
        assert kwargs["timeout_sec"] == 10.0
        calls.append(kwargs)
        vm = kwargs["vm"]
        # This must acquire the same lock on a separate session while HTTP is
        # awaiting the response: capability traffic cannot hold that lock.
        async with db.get_session_factory()() as session:
            await asyncio.wait_for(transaction_lock(session, operation_lock_key(vm)), 0.5)
            current = await session.get(context.platform.model, vm.id)
            observation = observation_for(current, state, released)
            if malformed:
                observation["identity"] = {"name": "a different machine"}
            if change:
                change(current)
            await session.commit()
        return {"ok": state in {"stopped", "running"}, "request_id": "inspect-current",
                "details": {"instance_observation": observation}}

    async def capability(**_kwargs):
        return {"ok": True, "details": {}}

    waiter = SimpleNamespace(inspect_agent=inspect, probe_agent=capability, excluded_ports_for=lambda _a: [])
    monkeypatch.setattr(context.platform.api, "get_capability_waiter", lambda: waiter)
    return calls


@pytest.mark.asyncio
async def test_unknown_start_inspects_outside_lock_then_starts_existing_instance_once(view_context, monkeypatch):
    context = view_context
    vm_id = await bound_unknown(context)
    calls = install_inspection(context, monkeypatch)
    response = await asyncio.wait_for(context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH), 2)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert assert_lifecycle(response.json()["instance"], state="starting")["error"] == ""
    commands = [payload for _, payload in context.hub.sent]
    assert len(commands) == 1 and commands[0]["type"] == context.platform.start
    assert commands[0]["require_existing_instance"] is True
    assert commands[0]["rollback_new_instance"] is False
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        assert vm.assigned_agent_id == "agent-1"
        if context.platform.name == "harmony":
            assert vm.lease_token != calls[0]["vm"].lease_token
            assert runtime(vm)["lease_history"][-1]["reason"] == "verified_stopped_observation"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_state", ["running", "missing", "unknown"])
async def test_unknown_start_refuses_non_stopped_without_native_commands(view_context, monkeypatch, native_state):
    context = view_context
    vm_id = await bound_unknown(context)
    install_inspection(context, monkeypatch, state=native_state)
    response = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    assert response.status_code == 409, response.text
    value = assert_lifecycle(response.json()["detail"]["instance"], state="running" if native_state == "running" else None)
    assert value["error"] and context.hub.sent == []
    if native_state == "running":
        assert value["error"] == "原实例仍在运行，未重复启动"
    assert assert_lifecycle(await read_vm(context, vm_id))["error"] == value["error"]
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        assert vm.assigned_agent_id == "agent-1" and not runtime(vm).get("operation")
        if context.platform.name == "harmony":
            assert await session.get(HarmonyVmPortLease, vm.hdc_port) is not None


@pytest.mark.asyncio
async def test_wrong_identity_observation_never_starts_or_releases_lease(view_context, monkeypatch):
    context = view_context
    vm_id = await bound_unknown(context)
    install_inspection(context, monkeypatch, malformed=True)
    response = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    assert response.status_code == 409 and context.hub.sent == []
    assert assert_lifecycle(response.json()["detail"]["instance"], state=None)["error"]
    if context.platform.name == "harmony":
        async with db.get_session_factory()() as session:
            vm = await session.get(context.platform.model, vm_id)
            assert await session.get(HarmonyVmPortLease, vm.hdc_port) is not None


@pytest.mark.asyncio
async def test_resource_release_is_platform_specific_not_a_generic_stopped_gate(view_context, monkeypatch):
    context = view_context
    vm_id = await bound_unknown(context)
    install_inspection(context, monkeypatch, released=False)
    response = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    if context.platform.name == "harmony":
        assert response.status_code == 409 and context.hub.sent == []
        value = assert_lifecycle(response.json()["detail"]["instance"], state="stopped")
        assert value["actions"]["stop"] is False and value["error"]
        async with db.get_session_factory()() as session:
            vm = await session.get(context.platform.model, vm_id)
            assert await session.get(HarmonyVmPortLease, vm.hdc_port) is not None
    else:
        assert response.status_code == 200, response.text
        assert last_command(context, context.platform.start)["require_existing_instance"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "config", "operation"])
async def test_inspect_reply_is_fenced_against_concurrent_changes(view_context, monkeypatch, change):
    context = view_context
    vm_id = await bound_unknown(context)

    def mutate(vm):
        if change == "owner":
            vm.assigned_agent_id = "another-owner"
        elif change == "config":
            vm.name = "changed-during-inspect"
        else:
            update_runtime(vm, operation={"request_id": "newer-operation", "action": "stop", "pending": True})
            vm.state = "stopping"

    install_inspection(context, monkeypatch, change=mutate)
    response = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    assert response.status_code == 409 and context.hub.sent == []
    assert "核验期间" in response.json()["detail"]["message"]
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        assert runtime(vm)["last_failure"]["message"] == "此前操作失败"
        if change == "owner":
            assert vm.assigned_agent_id == "another-owner"
        elif change == "config":
            assert vm.name == "changed-during-inspect"
        else:
            assert runtime(vm)["operation"]["request_id"] == "newer-operation" and vm.state == "stopping"


@pytest.mark.asyncio
async def test_same_agent_reconnect_cannot_reuse_old_stopped_observation(view_context, monkeypatch):
    context = view_context
    vm_id = await bound_unknown(context)
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        update_runtime(vm, instance_observation=observation_for(vm))
        vm.state = "stopped"
        await session.commit()
    assert_lifecycle(await read_vm(context, vm_id), state="stopped")
    context.hub.online.clear()
    if context.platform.name == "android":
        await context.platform.svc.mark_agent_vms_unavailable("agent-1", hub=context.hub)
    else:
        await context.platform.svc.mark_agent_vms_offline("agent-1", hub=context.hub)
    context.hub.online.add("agent-1")
    assert_lifecycle(await read_vm(context, vm_id), state=None)
    calls = install_inspection(context, monkeypatch, state="unknown")
    result = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    assert result.status_code == 409 and len(calls) == 1 and context.hub.sent == []


@pytest.mark.asyncio
async def test_inspection_does_not_poison_candidate_capability_cache(view_context, monkeypatch):
    context = view_context
    vm_id = await bound_unknown(context)
    waiter = context.platform.svc.get_capability_waiter().__class__()
    waiter._latest["agent-1"] = {"ok": True, "details": {"local_excluded_ports": [10000]}}
    before = dict(waiter._latest)
    sent = []

    async def send(agent_id, payload):
        sent.append(payload)
        assert payload["inspect_instance"] is True and payload["operation_timeout_sec"] == 5.0
        waiter.resolve(agent_id, {"request_id": payload["request_id"], "ok": True,
                                 "details": {"instance_observation": {"state": "stopped"}}})
        return True

    monkeypatch.setattr(context.hub, "send_to_agent", send)
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        result = await waiter.inspect_agent(hub=context.hub, vm=vm, agent_id="agent-1", timeout_sec=0.5)
    assert result["request_id"] == sent[0]["request_id"]
    assert waiter._latest == before and waiter._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("view_platform", ["android", "ios"], indirect=True)
async def test_complete_stopped_inventory_replaces_old_unknown_but_keeps_failure(view_context):
    context = view_context
    vm_id = await bound_unknown(context)
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        update_runtime(vm, last_status={"state": "error", "ok": False,
                       "details": {"cleanup_confirmed": False,
                                   "instance_observation": observation_for(vm, "unknown", False)}})
        if context.platform.name == "android":
            proof = {"avd_name": f"aiphone_vm_{vm_id}"}
        else:
            proof = {"name": f"aiphone_sim_{vm_id}", "udid": vm.udid, "state": "Shutdown"}
        await session.commit()
    await context.platform.svc.handle_vm_reconcile("agent-1", {
        "inventory_complete": True, "vm_ids": [vm_id], "stopped_vm_ids": [vm_id],
        "instance_proofs": {vm_id: proof},
    }, context.hub)
    value = assert_lifecycle(await read_vm(context, vm_id), state="stopped")
    assert value["error"] == "此前操作失败" and value["actions"]["start"] is True
    assert context.hub.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("view_platform", ["harmony"], indirect=True)
async def test_inspection_cannot_release_a_lease_changed_while_waiting(view_context, monkeypatch):
    context = view_context
    vm_id = await bound_unknown(context)
    calls = install_inspection(context, monkeypatch,
                               change=lambda vm: setattr(vm, "lease_token", "a-newer-lease-token"))
    result = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    assert result.status_code == 409 and context.hub.sent == [] and len(calls) == 1
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        assert vm.lease_token == "a-newer-lease-token"
        assert await session.get(HarmonyVmPortLease, vm.hdc_port) is not None


@pytest.mark.asyncio
async def test_explicit_stop_of_proven_stopped_instance_keeps_one_real_command(view_context):
    context = view_context
    vm_id = await bound_unknown(context)
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        update_runtime(vm, instance_observation=observation_for(vm))
        vm.state = "stopped"
        await session.commit()
    value = assert_lifecycle(await read_vm(context, vm_id), state="stopped")
    assert value["actions"]["stop"] is False
    result = await context.client.post(f"{context.platform.base}/{vm_id}/stop", headers=AUTH)
    assert result.status_code == 200, result.text
    assert result.json()["sent"] is True
    assert [payload["type"] for _, payload in context.hub.sent] == [context.platform.stop]
