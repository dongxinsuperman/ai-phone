"""Upgrade/compatibility audit regressions; isolated DB and fake Agent only."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from ai_phone.server import db
from tests.test_vm_lifecycle_contract import ContractHub, make_vm, platform as platform_fixture

platform = platform_fixture


@pytest.mark.asyncio
async def test_start_cannot_turn_same_host_reclaim_race_into_destructive_switch(session, platform, monkeypatch):
    old = await make_vm(session, platform, state="stopped", owner="old")
    old_id = old.id
    original_identity = getattr(old, platform.serial)
    hub = ContractHub(online=("restarted",))
    original_send_start = platform.api._send_start
    changed = False

    async def reclaim_between_route_read_and_locked_start(*args, **kwargs):
        nonlocal changed
        request_vm_id = kwargs.get("vm_id") or args[0]
        if request_vm_id == old_id and not changed:
            changed = True
            async with db.get_session_factory()() as reclaimed:
                current = await reclaimed.get(platform.model, old_id)
                current.assigned_agent_id = "restarted"
                await reclaimed.commit()
        return await original_send_start(*args, **kwargs)

    monkeypatch.setattr(platform.api, "_send_start", reclaim_between_route_read_and_locked_start)
    start = platform.api.start if platform.name == "ios" else platform.api.start_instance
    failure = None
    response = None
    try:
        response = await start(vm_id=old_id, session=session, hub=hub)
    except HTTPException as exc:
        failure = exc
    async with db.get_session_factory()() as check:
        rows = list((await check.execute(select(platform.model))).scalars())
        assert [row.id for row in rows] == [old_id], f"POST /start retired the original VM: {response!r}"
        assert rows[0].assigned_agent_id == "restarted"
        assert getattr(rows[0], platform.serial) == original_identity
        assert rows[0].state == "stopped"
    assert failure is not None and failure.status_code == 409
    assert not hub.sent, "a stale start target must send neither DELETE nor START"


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [None, False, "true"])
async def test_legacy_or_nonboolean_empty_inventory_is_not_absence_evidence(session, platform, completion):
    vm = await make_vm(session, platform, state="running")
    identity = getattr(vm, platform.serial)
    hub = ContractHub()
    inventory = {"instances": []} if platform.name == "harmony" else {"vm_ids": [], "stopped_vm_ids": []}
    if completion is not None:
        inventory["inventory_complete"] = completion
    await platform.svc.handle_vm_reconcile("old", inventory, hub)
    await session.refresh(vm)
    assert vm.state == "running" and vm.assigned_agent_id == "old"
    assert getattr(vm, platform.serial) == identity
    assert not hub.sent


@pytest.mark.asyncio
async def test_legacy_stopped_inventory_without_complete_flag_does_not_release_runtime(session, platform):
    vm = await make_vm(session, platform, state="running")
    hub = ContractHub()
    if platform.name == "harmony":
        await platform.svc.allocate_port_lease(session, vm, agent_id="old")
        await session.commit()
        inventory = {"instances": [{"vm_id": vm.id, "state": "stopped", "lease_token": vm.lease_token,
                                    "hdc_serial": vm.hdc_serial, "details": {"cleanup_confirmed": True}}]}
    else:
        inventory = {"vm_ids": [vm.id], "stopped_vm_ids": [vm.id]}
    identity = getattr(vm, platform.serial)
    await platform.svc.handle_vm_reconcile("old", inventory, hub)
    await session.refresh(vm)
    assert vm.state == "running" and vm.assigned_agent_id == "old"
    assert getattr(vm, platform.serial) == identity
    assert not hub.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/api/internal/vm", "/api/internal/harmony-vm"])
async def test_delete_keeps_legacy_vm_id_path_validation(client, prefix):
    response = await client.delete(f"{prefix}/instances/{'a' * 65}", headers={"Authorization": "Bearer dev"})
    assert response.status_code == 422
    assert any(item["loc"] == ["path", "vm_id"] for item in response.json()["detail"])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "stop", "dispatch"])
async def test_real_http_send_failure_is_non_2xx_with_exact_operation_evidence(
    client, app, session, platform, monkeypatch, action
):
    from types import SimpleNamespace
    from ai_phone.server.vm_lifecycle import runtime
    state = "running" if action == "stop" else "draft" if action == "dispatch" else "stopped"
    vm = await make_vm(session, platform, state=state, owner=None if action == "dispatch" else "old")
    hub = ContractHub()
    async def fail_send(agent_id, payload):
        hub.sent.append((agent_id, payload))
        return False
    hub.send_to_agent = fail_send
    app.state.hub = hub
    async def available(**_kwargs):
        return {"ok": True}
    if platform.name != "android":
        monkeypatch.setattr(platform.api, "get_capability_waiter", lambda: SimpleNamespace(
            probe_agent=available, excluded_ports_for=lambda _agent: []))
    prefix = {"android": "/api/internal/vm", "ios": "/api/internal/ios-sim",
              "harmony": "/api/internal/harmony-vm"}[platform.name]
    response = await client.post(f"{prefix}/instances/{vm.id}/{action}",
        headers={"Authorization": "Bearer dev"}, json={"agent_id": "old"} if action == "dispatch" else None)
    assert response.status_code == (502 if platform.name == "ios" else 409), response.text
    detail = response.json()["detail"]
    await session.refresh(vm)
    assert detail["sent"] is False and detail["vm_id"] == vm.id
    assert detail["instance"]["id"] == vm.id and detail["instance"]["state"] == "error"
    assert detail["request_id"] == runtime(vm)["operation"]["request_id"]
    assert runtime(vm)["operation"]["outcome"] == "failed"
    assert vm.assigned_agent_id == (None if action == "dispatch" else "old")
    assert len(hub.sent) == 1


@pytest.mark.asyncio
async def test_real_http_switch_send_failure_exposes_new_id_and_retired_old_id(
    client, app, session, platform, monkeypatch
):
    from types import SimpleNamespace
    old = await make_vm(session, platform)
    old_id = old.id
    hub = ContractHub()
    async def selected_failure(agent_id, payload):
        hub.sent.append((agent_id, payload))
        return not (agent_id == "new" and payload["type"].endswith("vm_start"))
    hub.send_to_agent = selected_failure
    app.state.hub = hub
    async def available(**_kwargs):
        return {"ok": True}
    if platform.name != "android":
        monkeypatch.setattr(platform.api, "get_capability_waiter", lambda: SimpleNamespace(
            probe_agent=available, excluded_ports_for=lambda _agent: []))
    prefix = {"android": "/api/internal/vm", "ios": "/api/internal/ios-sim",
              "harmony": "/api/internal/harmony-vm"}[platform.name]
    response = await client.post(f"{prefix}/instances/{old_id}/dispatch",
        headers={"Authorization": "Bearer dev"}, json={"agent_id": "new"})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["sent"] is False and detail["switched"] is True and detail["old_vm_id"] == old_id
    new_id = detail["instance"]["id"]
    assert new_id != old_id and detail["vm_id"] == new_id and detail["request_id"]
    async with db.get_session_factory()() as check:
        rows = list((await check.execute(select(platform.model))).scalars())
        assert [row.id for row in rows] == [new_id]
        assert rows[0].state == "error" and rows[0].assigned_agent_id is None
