"""Offline delete exceptions on isolated DB/fake Agents, never host devices."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ai_phone.server import db
from ai_phone.server.android_vm import api as android_api, service as android_service
from ai_phone.server.models import AndroidVmInstance, Run
from ai_phone.server.vm_lifecycle import runtime
from ai_phone.shared import protocol as P
from tests.test_vm_lifecycle_contract import ContractHub, make_vm, start_first
from tests.test_vm_lifecycle_contract import platform as platform


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["stopped", "agent_offline"])
@pytest.mark.parametrize("owner_online", [False, True])
async def test_delete_distinguishes_offline_residue_from_live_occupancy(
    session, platform, state, owner_online
):
    vm = await make_vm(session, platform, state=state)
    vm_id = vm.id
    if platform.name == "harmony":
        await platform.svc.allocate_port_lease(session, vm, agent_id="old")
        await session.commit()
    serial = f"harmony-vm:{vm_id}" if platform.name == "harmony" else platform.value
    hub = ContractHub(online=("old",) if owner_online else ())
    old_lock = await hub.vm_lock_store.acquire(
        serial, "old-run", "job", ttl_seconds=600, meta={"auto_acquired": True}
    )
    await hub.subscribe(serial, object())

    if owner_online:
        with pytest.raises(HTTPException) as exc:
            await platform.api.delete_instance(vm_id=vm_id, session=session, hub=hub)
        assert exc.value.status_code == 409
        assert "device_busy" in str(exc.value.detail)
        await session.refresh(vm)
        assert vm.state == state and vm.assigned_agent_id == "old"
        assert not hub.sent
    else:
        deleted = await platform.api.delete_instance(vm_id=vm_id, session=session, hub=hub)
        assert deleted["deleted"] is True and deleted["cleanup_sent"] is False
        async with db.get_session_factory()() as check:
            assert await check.get(platform.model, vm_id) is None

    # Deleting a VM configuration must never claim ownership of a Run's lock.
    preserved = hub.vm_lock_store.peek(serial)
    assert preserved is not None and preserved.token == old_lock.token
    assert preserved.holder == "old-run"


@pytest.mark.asyncio
async def test_deleted_failed_first_start_cleans_only_exact_late_reporter(
    session, platform, monkeypatch
):
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    vm_id = vm.id
    request_id = runtime(vm)["operation"]["request_id"]
    if platform.name == "android":
        proof = {"avd_name": f"aiphone_vm_{vm_id}"}
        transport = {"adb_serial": vm.adb_serial}
    elif platform.name == "ios":
        proof = {"sim_name": f"aiphone_sim_{vm_id}"}
        transport = {"udid": "late-deleted-simulator-udid"}
    else:
        proof = {"instance_name": f"aiphone_harmony_{vm_id}"}
        transport = {"hdc_serial": vm.hdc_serial, "lease_token": vm.lease_token}

    hub.online.remove("old")
    mark_offline = (
        platform.svc.mark_agent_vms_unavailable
        if platform.name == "android"
        else platform.svc.mark_agent_vms_offline
    )
    await mark_offline("old")
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert runtime(vm)["operation"]["failure_reason"] == "agent_disconnected"
    assert runtime(vm)["operation"]["pending"] is False
    deleted = await platform.api.delete_instance(vm_id=vm_id, session=session, hub=hub)
    assert deleted["deleted"] is True and deleted["cleanup_sent"] is False
    async with db.get_session_factory()() as check:
        assert await check.get(platform.model, vm_id) is None

    hub.online.add("old")
    before_reconnect = len(hub.sent)
    inventory = (
        {"instances": [], "inventory_complete": True}
        if platform.name == "harmony"
        else {"vm_ids": [], "stopped_vm_ids": [], "inventory_complete": True}
    )
    await platform.svc.handle_vm_reconcile("old", inventory, hub)
    assert len(hub.sent) == before_reconnect

    # The ledger was explicitly deleted. A provably managed late instance must
    # be removed even without created_in_attempt provenance from the old Agent.
    late = {
        "vm_id": vm_id,
        "request_id": request_id,
        "state": "running",
        "ok": True,
        "reason": "started",
        "details": proof,
        **transport,
    }
    for invalid_proof in ({}, {key: "unmanaged-instance" for key in proof}):
        await platform.svc.handle_vm_status("old", {**late, "details": invalid_proof}, hub)
    assert len(hub.sent) == before_reconnect
    await platform.svc.handle_vm_status("old", late, hub)
    assert len(hub.sent) == before_reconnect + 1
    target, command = hub.sent[-1]
    assert target == "old"
    assert command["type"] == platform.delete and command["vm_id"] == vm_id
    assert command["request_id"] != request_id
    if platform.name == "android":
        # AVD identity comes from vm_id. A delayed reusable ADB address must
        # not be treated as authority for stopping whatever now owns that port.
        assert command["adb_serial"] == ""
    else:
        for key, value in transport.items():
            assert command[key] == value

    # Terminal deletion replies may include the same identity proof. Neither
    # success nor failure may recursively trigger another unknown-VM deletion.
    sent_after_cleanup = len(hub.sent)
    for reason in ("deleted", "delete_failed"):
        ok = reason == "deleted"
        await platform.svc.handle_vm_status(
            "old",
            {
                **late,
                "request_id": command["request_id"],
                "state": "stopped" if ok else "error",
                "ok": ok,
                "reason": reason,
                "details": {**proof, "cleanup_confirmed": ok},
            },
            hub,
        )
    assert len(hub.sent) == sent_after_cleanup
    assert len([msg for _, msg in hub.sent if msg["type"].endswith("vm_start")]) == 1
    async with db.get_session_factory()() as check:
        assert await check.get(platform.model, vm_id) is None


@pytest.mark.asyncio
async def test_android_new_configuration_avoids_old_run_slot_and_late_stop_preserves_new_locks(
    client, app, session, monkeypatch
):
    android = SimpleNamespace(
        name="android", api=android_api, svc=android_service, model=AndroidVmInstance,
        serial="adb_serial", value="emulator-5554", delete=P.MSG_VM_DELETE,
    )
    hub = ContractHub(online=())
    app.state.hub = hub
    app.state.lock_store = hub.vm_lock_store
    old = await make_vm(session, android, state="agent_offline")
    old_id, old_serial = old.id, old.adb_serial
    old_run = Run(device_serial=old_serial, agent_id="old", goal="old VM run", status="running")
    session.add(old_run)
    await session.commit()
    old_run_id = old_run.id
    old_lock = await hub.vm_lock_store.acquire(
        old_serial, old_run_id, "job", ttl_seconds=600, meta={"auto_acquired": True}
    )
    deleted = await android_api.delete_instance(vm_id=old_id, session=session, hub=hub)
    assert deleted["deleted"] is True
    assert hub.vm_lock_store.peek(old_serial).token == old_lock.token

    hub.online.add("old")
    new_vm, response = await start_first(session, android, hub, monkeypatch)
    new_id, new_serial = new_vm.id, new_vm.adb_serial
    assert response["sent"] is True and new_id != old_id
    assert new_serial != old_serial
    assert new_serial == "emulator-5556"
    request_id = runtime(new_vm)["operation"]["request_id"]
    await android_service.handle_vm_status(
        "old",
        {"vm_id": new_id, "request_id": request_id, "state": "running", "ok": True,
         "reason": "started", "adb_serial": new_serial},
        hub,
    )
    await session.refresh(new_vm)
    assert new_vm.state == "running" and new_vm.assigned_agent_id == "old"
    new_run = Run(device_serial=new_serial, agent_id="old", goal="new VM run", status="running")
    session.add(new_run)
    await session.commit()
    new_lock = await hub.vm_lock_store.acquire(
        new_serial, new_run.id, "job", ttl_seconds=600, meta={"auto_acquired": True}
    )

    stopped = await client.post(f"/api/runs/{old_run_id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert hub.vm_lock_store.peek(old_serial) is None
    assert hub.vm_lock_store.peek(new_serial).token == new_lock.token

    # Once the old lock is legitimately released, a different Run may take its
    # serial. Repeating old Run cleanup must compare holders, not just serials.
    later_lock = await hub.vm_lock_store.acquire(
        old_serial, "later-run-on-reused-slot", "job", ttl_seconds=600,
        meta={"auto_acquired": True},
    )
    repeated = await client.post(f"/api/runs/{old_run_id}/stop")
    assert repeated.status_code == 200, repeated.text
    assert hub.vm_lock_store.peek(old_serial).token == later_lock.token
    assert hub.vm_lock_store.peek(new_serial).token == new_lock.token
    await session.refresh(new_vm)
    assert new_vm.state == "running" and new_vm.adb_serial == new_serial
