"""An unaccepted replacement start may be submitted once by a later user action.

All HTTP traffic uses ASGITransport and all Agent traffic uses FakeHub. No
native operation, network socket, installed profile, or live Server is used.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ai_phone.server import db
from ai_phone.server.vm_lifecycle import runtime, update_runtime
from tests.test_vm_lifecycle_view_api import (
    AUTH, assert_lifecycle, create_vm, read_vm,
    view_context as view_context_fixture, view_platform as view_platform_fixture,
)

view_context = view_context_fixture
view_platform = view_platform_fixture


def _evidence(name, value):
    directory = os.environ.get("VM_SWITCH_REVIEW_EVIDENCE_DIR")
    if directory:
        (Path(directory) / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n")


async def _switch_with_target_disconnect(context, monkeypatch):
    old_id = (await create_vm(context))["id"]
    async with db.get_session_factory()() as session:
        old = await session.get(context.platform.model, old_id)
        old.assigned_agent_id, old.state = "agent-1", "stopped"
        update_runtime(old, owner_host_id="fixture-host", instance_created=True,
                       last_status={"state": "stopped", "ok": True,
                                    "details": {"cleanup_confirmed": True}})
        await session.commit()
    context.hub.online.add("agent-2")
    original_send = context.hub.send_to_agent
    monkeypatch.setattr(context.hub, "agent_host_id",
                        lambda agent: {"agent-1": "fixture-host", "agent-2": "fixture-target"}.get(agent, ""))

    async def disconnect_before_first_target_send(agent_id, payload):
        if payload.get("vm_id") == old_id and payload["type"].endswith("_delete"):
            context.hub.online.discard("agent-2")
        return await original_send(agent_id, payload)

    monkeypatch.setattr(context.hub, "send_to_agent", disconnect_before_first_target_send)
    first = await context.client.post(f"{context.platform.base}/{old_id}/dispatch",
                                      json={"agent_id": "agent-2"}, headers=AUTH)
    assert first.status_code == 409, first.text
    detail = first.json()["detail"]
    assert detail["switched"] is True and detail["old_vm_id"] == old_id
    assert detail["vm_id"] != old_id and detail["instance"]["assigned_agent_id"] is None
    assert detail["sent"] is False and detail["request_id"] == ""
    assert not [payload for _, payload in context.hub.sent if payload["type"] == context.platform.start]
    old_read = await context.client.get(f"{context.platform.base}/{old_id}", headers=AUTH)
    assert old_read.status_code == 404
    return old_id, detail["vm_id"], detail


async def _complete_first_binding(context, vm_id, command):
    platform = context.platform
    async with db.get_session_factory()() as session:
        vm = await session.get(platform.model, vm_id)
        if platform.name == "android":
            extra = {"adb_serial": vm.adb_serial}
            identity = {"avd_name": f"aiphone_vm_{vm_id}"}
        elif platform.name == "ios":
            extra = {"udid": f"new-only-{vm_id}"}
            identity = {"sim_name": f"aiphone_sim_{vm_id}", **extra}
        else:
            extra = {"lease_token": vm.lease_token, "hdc_serial": vm.hdc_serial}
            identity = {"instance_name": f"aiphone_harmony_{vm_id}",
                        "instance_path": "/fixture-only/instances", **extra}
    observation = {"vm_id": vm_id, "state": "running", "identity": identity,
                   "resources_released": False, "reason": "fixture_process_running"}
    await platform.svc.handle_vm_status("agent-2", {
        "vm_id": vm_id, "request_id": command["request_id"], "state": "running", "ok": True,
        "reason": "running", **extra,
        "details": {**identity, "instance_created": True, "created_in_attempt": True,
                    "instance_observation": observation},
    }, context.hub)


@pytest.mark.asyncio
async def test_failed_unsent_switch_allows_one_later_manual_dispatch_and_success(view_context, monkeypatch):
    context = view_context
    old_id, new_id, failure = await _switch_with_target_disconnect(context, monkeypatch)
    failed_view = assert_lifecycle(failure["instance"], state="draft")
    assert failed_view["actions"]["dispatch"] is True and failed_view["error"]
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, new_id)
        assert not runtime(vm).get("operation") and not runtime(vm).get("instance_created")
        assert runtime(vm)["last_failure"]["native_command_sent"] is False
        assert all(not getattr(vm, field, None) for field in ("adb_serial", "udid", "hdc_serial", "lease_token"))

    # A reconnect/read only exposes the same failed draft. It must never
    # resume the old switch attempt or dispatch a command in the background.
    context.hub.online.add("agent-2")
    await asyncio.sleep(0)
    readback = await read_vm(context, new_id)
    assert assert_lifecycle(readback, state="draft")["error"] == failed_view["error"]
    listed = await context.client.get(context.platform.base, headers=AUTH)
    assert [row["id"] for row in listed.json()] == [new_id]
    assert not [payload for _, payload in context.hub.sent if payload["type"] == context.platform.start]

    accepted = await context.client.post(f"{context.platform.base}/{new_id}/dispatch",
                                         json={"agent_id": "agent-2"}, headers=AUTH)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["sent"] is True
    assert assert_lifecycle(accepted.json()["instance"], state="starting")["error"] == ""
    starts = [payload for _, payload in context.hub.sent if payload["type"] == context.platform.start]
    assert len(starts) == 1 and starts[0]["vm_id"] == new_id
    assert starts[0]["require_existing_instance"] is False and starts[0]["rollback_new_instance"] is True
    await _complete_first_binding(context, new_id, starts[0])
    running = await read_vm(context, new_id)
    assert assert_lifecycle(running, state="running")["error"] == ""
    assert running["assigned_agent_id"] == "agent-2"
    async with db.get_session_factory()() as session:
        assert await session.get(context.platform.model, old_id) is None
        vm = await session.get(context.platform.model, new_id)
        assert runtime(vm)["operation"]["request_id"] == starts[0]["request_id"]
        assert runtime(vm)["operation"]["outcome"] == "succeeded"
        assert runtime(vm)["operation"]["pending"] is False
    assert len([payload for _, payload in context.hub.sent if payload["type"] == context.platform.start]) == 1
    _evidence(f"manual-dispatch-{context.platform.name}", {
        "first_http": 409, "first_detail": failure, "old_id": old_id, "replacement_id": new_id,
        "read_after_target_reconnect": readback, "second_http": accepted.status_code,
        "second_body": accepted.json(), "final_instance": running,
        "native_start_commands": starts, "old_ledger_present": False,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("provenance", ["created", "unknown", "unconfirmed_operation"])
async def test_created_or_unproven_replacement_is_not_made_dispatchable(view_context, monkeypatch, provenance):
    context = view_context
    _old_id, new_id, _failure = await _switch_with_target_disconnect(context, monkeypatch)
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, new_id)
        if provenance == "created":
            update_runtime(vm, instance_created=True,
                           last_status={"state": "error", "ok": False,
                                        "details": {"instance_created": True, "cleanup_confirmed": False}})
        elif provenance == "unknown":
            # An older error row without explicit not-sent provenance stays
            # unknown; the fix must not globally treat unbound error as draft.
            update_runtime(vm, last_failure=None)
        else:
            update_runtime(vm, operation={"request_id": "another-unconfirmed-attempt", "action": "start",
                           "first_bind": True, "pending": False, "outcome": "failed"},
                           last_status={"state": "error", "ok": False, "details": {"cleanup_confirmed": False}})
        await session.commit()
    context.hub.online.add("agent-2")
    before = await read_vm(context, new_id)
    assert assert_lifecycle(before, state=None)["actions"]["dispatch"] is False
    rejected = await context.client.post(f"{context.platform.base}/{new_id}/dispatch",
                                         json={"agent_id": "agent-2"}, headers=AUTH)
    assert rejected.status_code == 409
    assert not [payload for _, payload in context.hub.sent if payload["type"] == context.platform.start]
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, new_id)
        assert vm.assigned_agent_id is None
        if provenance == "unconfirmed_operation":
            assert runtime(vm)["operation"]["request_id"] == "another-unconfirmed-attempt"
        else:
            assert not runtime(vm).get("operation")
        if provenance == "created":
            assert runtime(vm)["instance_created"] is True


@pytest.mark.asyncio
async def test_stop_payload_uses_this_operations_recorded_budget(view_context, monkeypatch):
    from ai_phone.server import vm_lifecycle

    context = view_context
    vm_id = (await create_vm(context))["id"]
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        vm.assigned_agent_id, vm.state = "agent-1", "running"
        update_runtime(vm, owner_host_id="fixture-host", instance_created=True)
        if context.platform.name == "android":
            vm.adb_serial = "emulator-5554"
        elif context.platform.name == "ios":
            vm.udid = f"budget-owned-{vm_id}"
        else:
            await context.platform.svc.allocate_port_lease(session, vm, agent_id="agent-1")
        await session.commit()
    # A nondefault budget proves the payload uses this operation's recorded
    # bound instead of silently creating a fresh Agent-side default timeout.
    monkeypatch.setattr(vm_lifecycle, "operation_timeout_seconds", lambda *_args: 7.25)
    response = await context.client.post(f"{context.platform.base}/{vm_id}/stop", headers=AUTH)
    assert response.status_code == 200 and response.json()["sent"] is True
    stops = [payload for _, payload in context.hub.sent if payload["type"] == context.platform.stop]
    assert len(stops) == 1
    async with db.get_session_factory()() as session:
        vm = await session.get(context.platform.model, vm_id)
        operation = runtime(vm)["operation"]
        assert operation["pending"] is True and operation["action"] == "stop"
        assert operation["request_id"] == stops[0]["request_id"]
        assert stops[0]["operation_timeout_sec"] == operation["timeout_sec"] == 7.25
