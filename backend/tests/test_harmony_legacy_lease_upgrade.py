"""Legacy stopped registry upgrade through real payloads; no native commands."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from ai_phone.agent.harmony_vm.manager import HarmonyVmManager
from ai_phone.server.harmony_vm import api, service
from ai_phone.server.harmony_vm.models import HarmonyVmInstance, HarmonyVmPortLease
from ai_phone.server.vm_lifecycle import cancel_operation_timeout
from ai_phone.shared import protocol as P
from tests.test_vm_lifecycle_contract import ContractHub


class RecordingClient:
    agent_id = "restarted"

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(copy.deepcopy(payload))

    async def refresh_devices(self):
        return None


async def legacy_setup(session, tmp_path, *, owner_host_id=None):
    vm = HarmonyVmInstance(
        id="abc012def345", name="legacy", alias="legacy", state="agent_offline",
        assigned_agent_id="old", error_code="legacy_stop_unconfirmed", error_message="old stop was not confirmed",
        runtime={"instance_created": True, "last_status": {"state": "error", "details": {"cleanup_confirmed": False}}},
    )
    if owner_host_id:
        vm.runtime = {**vm.runtime, "owner_host_id": owner_host_id}
    session.add(vm)
    await session.flush()
    lease = await service.allocate_port_lease(session, vm, agent_id="old")
    lease.state = "active"
    await session.commit()
    manager = HarmonyVmManager(runtime_dir=tmp_path / "agent-runtime")
    name = f"aiphone_harmony_{vm.id}"
    directory = manager.instance_path / name
    directory.mkdir(parents=True)
    (directory / "config.ini").write_text("[instance]\nname=legacy\n")
    (directory / "userdata.marker").write_text("preserve original user data")
    manager._known[vm.id] = {
        "vm_id": vm.id, "name": vm.name, "instance_name": name,
        "instance_path": str(manager.instance_path), "created": True,
        "ready": False, "cleanup_pending": False, "hdc_port": vm.hdc_port,
        "hdc_serial": vm.hdc_serial, "lease_token": vm.lease_token,
    }
    manager._save_registry()
    # Read the old on-disk format in a fresh manager instead of hand-writing
    # the reconcile message or seeding a new-format in-memory runtime.
    manager = HarmonyVmManager(runtime_dir=manager.runtime_dir)
    client = RecordingClient()
    assert await manager.report_orphan_reconcile(client) == 1
    payload = client.sent[-1]
    assert payload["inventory_complete"] is True
    assert payload["instances"][0]["details"]["managed_instance_present"] is True
    assert payload["instances"][0]["details"]["cleanup_confirmed"] is False
    assert "cleanup_confirmed" not in manager._known[vm.id]
    return vm, manager, client, payload, directory


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_host_id", [None, "host-one"])
async def test_legacy_unconfirmed_lease_reclaims_session_then_explicit_stop_checks_absence(
    session, tmp_path, monkeypatch, owner_host_id,
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    vm, manager, client, payload, directory = await legacy_setup(
        session, tmp_path, owner_host_id=owner_host_id,
    )
    identity = (vm.hdc_port, vm.hdc_serial, vm.lease_token)
    hub = ContractHub(online=("restarted",))
    await service.handle_vm_reconcile("restarted", payload, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "restarted"
    assert vm.runtime["owner_host_id"] == "host-one"
    assert vm.state == "agent_offline"
    assert vm.error_code == "legacy_stop_unconfirmed" and vm.error_message == "old stop was not confirmed"
    assert vm.runtime["last_status"]["details"]["cleanup_confirmed"] is False
    assert (vm.hdc_port, vm.hdc_serial, vm.lease_token) == identity
    lease = await session.get(HarmonyVmPortLease, identity[0], populate_existing=True)
    assert lease.lease_token == identity[2] and lease.state == "active"
    assert not hub.sent, "inventory only observes; it must not issue a stop or deletion"

    inspected = []
    monkeypatch.setattr(manager, "_active_instance_serial", lambda vm_id, _known: inspected.append(vm_id) or "")
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    monkeypatch.setattr(manager_module, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(manager_module, "managed_vm_owner", lambda _serial: None)
    result = await api.stop_instance(vm_id=vm.id, session=session, hub=hub)
    assert result["sent"] is True
    assert hub.sent[-1][0] == "restarted"
    command = hub.sent[-1][1]
    assert command["type"] == P.MSG_HARMONY_VM_STOP
    assert (command["hdc_serial"], command["lease_token"]) == identity[1:]
    try:
        await manager.handle_stop(client, command)
        receipt = client.sent[-1]
        assert inspected == [vm.id], "legacy ready=False alone is not fresh stop evidence"
        assert receipt["details"]["exit_evidence"]["instance_process_status"] == "absent"
        assert receipt["details"]["cleanup_confirmed"] is True
        await service.handle_vm_status("restarted", receipt, hub)
        await session.refresh(vm)
        assert vm.state == "stopped" and vm.assigned_agent_id == "restarted"
        assert vm.lease_token is None and vm.hdc_port is None and vm.hdc_serial is None
        assert await session.get(HarmonyVmPortLease, identity[0], populate_existing=True) is None
        assert (directory / "userdata.marker").read_text() == "preserve original user data"
        assert (directory / "config.ini").is_file()
    finally:
        cancel_operation_timeout(vm)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [
    "complete", "name", "path", "managed", "token", "serial", "old_online", "host_conflict",
    "lease_row", "foreign_lease",
])
async def test_legacy_session_reclaim_rejects_incomplete_or_conflicting_proof(
    session, tmp_path, missing,
):
    vm, _manager, _client, payload, _directory = await legacy_setup(
        session, tmp_path, owner_host_id="original-host" if missing == "host_conflict" else None,
    )
    identity = (vm.hdc_port, vm.hdc_serial, vm.lease_token)
    row = payload["instances"][0]
    if missing == "complete":
        payload["inventory_complete"] = False
    elif missing == "name":
        row["details"]["instance_name"] = "wrong-instance"
    elif missing == "path":
        row["details"].pop("instance_path")
    elif missing == "managed":
        row["details"].pop("managed_instance_present")
    elif missing == "token":
        row["lease_token"] = "wrong-token"
    elif missing == "serial":
        row["hdc_serial"] = "127.0.0.1:10008"
    elif missing in {"lease_row", "foreign_lease"}:
        lease = await session.get(HarmonyVmPortLease, vm.hdc_port)
        if missing == "lease_row":
            await session.delete(lease)
        else:
            lease.vm_id = "another-vm"
        await session.commit()
    hub = ContractHub(online=("old", "restarted") if missing == "old_online" else ("restarted",))
    await service.handle_vm_reconcile("restarted", payload, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "old" and vm.state == "agent_offline"
    assert (vm.hdc_port, vm.hdc_serial, vm.lease_token) == identity
    assert not hub.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("unconfirmed", ["connected", "owner_unknown", "port_busy"])
async def test_legacy_stop_without_absence_evidence_does_not_claim_cleanup(session, tmp_path, monkeypatch, unconfirmed):
    import ai_phone.agent.harmony_vm.manager as manager_module

    vm, manager, _client, _payload, _directory = await legacy_setup(session, tmp_path)
    inspected = []
    def inspect(vm_id, _known):
        inspected.append(vm_id)
        return None if unconfirmed == "owner_unknown" else ""
    monkeypatch.setattr(manager, "_active_instance_serial", inspect)
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: unconfirmed != "port_busy")
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    monkeypatch.setattr(manager_module, "managed_vm_owner", lambda _serial: None)
    monkeypatch.setattr(manager_module, "hdc_list_targets", lambda: [SimpleNamespace(serial=vm.hdc_serial, status="connected")] if unconfirmed == "connected" else [])
    result = manager.stop_sync(vm.id, vm.hdc_serial, vm.lease_token)
    assert inspected == [vm.id]
    assert result["ok"] is False and result["details"]["cleanup_confirmed"] is False
    assert result["reason"] == ("instance_owner_unverified" if unconfirmed == "owner_unknown" else "instance_absence_unconfirmed")
