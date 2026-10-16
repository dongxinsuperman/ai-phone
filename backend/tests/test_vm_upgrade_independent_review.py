"""Independent upgrade review: isolated DB and fake Agents, no native commands.

Current-code tests are data-safety invariants. The explicitly named legacy
replay test instead demonstrates a 452e7d7 upgrade-order hazard; it is not an
acceptance test for that old Server. Its exact function is frozen as a fixture
so the review also works in a shallow checkout without Git history.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_phone.agent.identity import load_host_identity
from ai_phone.server import api as _api_registration  # noqa: F401
from ai_phone.server.android_vm import service as android
from ai_phone.server.harmony_vm import service as harmony
from ai_phone.server.harmony_vm.models import HarmonyVmInstance
from ai_phone.server.hub import Hub
from ai_phone.server.ios_sim import service as ios
from ai_phone.server.ios_sim.models import IosSimVmInstance
from ai_phone.server.models import AndroidVmInstance
from ai_phone.server.vm_lifecycle import runtime, update_runtime
from ai_phone.shared import protocol as P


BASELINE = "452e7d77326f9fc0e5b003ecbf6cec5fc499e4bf"


class UpgradeHub(Hub):
    def __init__(self, *, new_host_id: str, old_online: bool = False):
        super().__init__()
        self.online = {"personal-new-session"}
        if old_online:
            self.online.add("personal-old-session")
        self.new_host_id = new_host_id
        self.sent = []

    def has_agent(self, agent_id):
        return agent_id in self.online

    def agent_host_id(self, agent_id):
        return self.new_host_id if agent_id == "personal-new-session" else ""

    async def send_to_agent(self, agent_id, payload):
        self.sent.append((agent_id, dict(payload)))
        return self.has_agent(agent_id)


@pytest.fixture(params=("android", "ios", "harmony"))
def platform(request):
    return {
        "android": SimpleNamespace(name="android", model=AndroidVmInstance, svc=android, delete=P.MSG_VM_DELETE),
        "ios": SimpleNamespace(name="ios", model=IosSimVmInstance, svc=ios, delete=P.MSG_IOS_SIM_VM_DELETE),
        "harmony": SimpleNamespace(name="harmony", model=HarmonyVmInstance, svc=harmony, delete=P.MSG_HARMONY_VM_DELETE),
    }[request.param]


async def stored_vm(session, platform, *, host_id=""):
    fields = dict(name="personal-upgrade", alias="personal-upgrade", state="agent_offline", assigned_agent_id="personal-old-session")
    if platform.name == "android":
        fields.update(api_level=33, abi="arm64", adb_serial="emulator-5554")
    elif platform.name == "ios":
        fields.update(udid="PERSONAL-PERSISTED-UDID")
    vm = platform.model(**fields)
    session.add(vm)
    await session.flush()
    if host_id:
        update_runtime(vm, owner_host_id=host_id, instance_created=True)
    await session.commit()
    return vm


def stopped_inventory(platform, vm, *, modern=True):
    if platform.name == "harmony":
        # These two fields already existed in the 452e7d7 Agent payload.
        message = {"instances": [{"vm_id": vm.id, "state": "stopped", "details": {
            "instance_name": f"aiphone_harmony_{vm.id}",
            "managed_instance_present": True,
        }}]}
        if modern:
            message["inventory_complete"] = True
            message["instances"][0]["details"].update({
                "instance_path": "/fixture/managed/instances",
                "instance_observation": {
                    "vm_id": vm.id, "state": "stopped", "resources_released": True,
                    "reason": "fixture native process absent and transport released",
                    "identity": {"instance_name": f"aiphone_harmony_{vm.id}",
                                 "instance_path": "/fixture/managed/instances",
                                 "lease_token": getattr(vm, "lease_token", "") or "",
                                 "hdc_serial": getattr(vm, "hdc_serial", "") or ""},
                },
            })
        return message
    message = {"vm_ids": [vm.id], "running_vm_ids": [], "stopped_vm_ids": [vm.id]}
    if modern:
        proof = ({"avd_name": f"aiphone_vm_{vm.id}"} if platform.name == "android" else
                 {"name": f"aiphone_sim_{vm.id}", "udid": vm.udid, "state": "Shutdown"})
        message.update(inventory_complete=True, instance_proofs={vm.id: proof})
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("old_online", [False, True])
async def test_same_native_host_new_storage_identity_must_not_delete_existing_vm(session, platform, tmp_path, old_online):
    """Both storage directories belong to ONE native host, not two VM copies.

    Online old Agent covers overlap/blue-green starts. Its presence cannot
    prove that the new installation sees a separate disposable native copy.
    """
    original_host_id = load_host_identity(tmp_path / "original-installation")
    replacement_host_id = load_host_identity(tmp_path / "new-empty-storage")
    assert original_host_id != replacement_host_id
    vm = await stored_vm(session, platform, host_id=original_host_id)
    hub = UpgradeHub(new_host_id=replacement_host_id, old_online=old_online)
    await platform.svc.handle_vm_reconcile("personal-new-session", stopped_inventory(platform, vm), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-old-session", "do not silently migrate ledger ownership"
    destructive = [message for _target, message in hub.sent if message["type"] == platform.delete]
    assert destructive == [], f"storage identity loss authorized deletion of existing native VM: {destructive}"


@pytest.mark.asyncio
async def test_preserved_installation_identity_reclaims_stopped_vm_without_delete(session, platform, tmp_path):
    host_id = load_host_identity(tmp_path / "preserved-storage")
    assert load_host_identity(tmp_path / "preserved-storage") == host_id
    vm = await stored_vm(session, platform, host_id=host_id)
    hub = UpgradeHub(new_host_id=host_id)
    await platform.svc.handle_vm_reconcile("personal-new-session", stopped_inventory(platform, vm), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-new-session"
    assert vm.state == "stopped"
    assert not hub.sent


@pytest.mark.asyncio
async def test_new_agent_proofs_can_adopt_legacy_row_once(session, platform, tmp_path):
    vm = await stored_vm(session, platform)
    host_id = load_host_identity(tmp_path / "first-upgrade-storage")
    hub = UpgradeHub(new_host_id=host_id)
    await platform.svc.handle_vm_reconcile("personal-new-session", stopped_inventory(platform, vm), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-new-session"
    assert vm.state == "stopped"
    assert runtime(vm)["owner_host_id"] == host_id
    assert not hub.sent


@pytest.mark.asyncio
async def test_legacy_stopped_payload_records_mixed_version_recovery_boundary(session, platform):
    """Old payloads lack the explicit complete-inventory proof now required.

    Harmony has a managed-name proof, but its old payload still cannot certify
    a stopped/missing fact. Preserve the ledger instead of guessing.
    """
    vm = await stored_vm(session, platform)
    hub = UpgradeHub(new_host_id="")  # 452e7d7 Agent has no hello.host_id.
    await platform.svc.handle_vm_reconcile("personal-new-session", stopped_inventory(platform, vm, modern=False), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-old-session"
    assert vm.state == "agent_offline"
    assert not hub.sent


@pytest.mark.asyncio
async def test_server_first_old_same_session_inventory_still_cannot_certify_stopped(session, platform):
    """Server-first is safer ordering, not a promise that old Agents gain new evidence."""
    vm = await stored_vm(session, platform)
    hub = UpgradeHub(new_host_id="", old_online=True)
    await platform.svc.handle_vm_reconcile("personal-old-session", stopped_inventory(platform, vm, modern=False), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-old-session"
    assert vm.state == "agent_offline"
    assert not hub.sent


@pytest.mark.asyncio
async def test_agent_only_downgrade_cannot_reclaim_identity_aware_rows(session, platform):
    """Diagnostic expectation: a legacy hello cannot replace persisted host identity."""
    vm = await stored_vm(session, platform, host_id="preserved-modern-installation")
    hub = UpgradeHub(new_host_id="")
    await platform.svc.handle_vm_reconcile("personal-new-session", stopped_inventory(platform, vm, modern=False), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-old-session"
    assert vm.state == "agent_offline"
    assert not hub.sent


def legacy_ios_reconcile():
    source = (Path(__file__).parent / "fixtures" / "legacy_ios_reconcile_452e7d7.py.txt").read_text()
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_vm_reconcile")
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function,
    ], type_ignores=[])
    namespace = dict(vars(ios))
    exec(compile(ast.fix_missing_locations(module), f"{BASELINE}:ios_sim/service.py", "exec"), namespace)
    return namespace["handle_vm_reconcile"]


@pytest.mark.asyncio
async def test_legacy_server_replay_demonstrates_agent_first_upgrade_delete_hazard(session):
    """Reproduce old code, deliberately asserting its destructive command.

    No command is delivered to a real Agent. Passing means the old-version
    hazard was reproduced, not that an Agent-first rollout is safe.
    """
    platform = SimpleNamespace(name="ios", model=IosSimVmInstance)
    vm = await stored_vm(session, platform)
    hub = UpgradeHub(new_host_id="modern-host")
    await legacy_ios_reconcile()("personal-new-session", stopped_inventory(platform, vm), hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "personal-new-session"
    assert vm.state == "stopped"
    assert any(target == "personal-new-session" and payload["type"] == P.MSG_IOS_SIM_VM_DELETE and payload["vm_id"] == vm.id
               for target, payload in hub.sent)


@pytest.mark.asyncio
async def test_restoring_ledger_without_new_native_ids_authorizes_orphan_delete(session, platform):
    """A stale DB restore is not a code-only rollback: missing native IDs are deleted."""
    vm = SimpleNamespace(id="personal_newer_native", udid="PERSONAL-NEWER-UDID")
    hub = UpgradeHub(new_host_id="preserved-host")
    await platform.svc.handle_vm_reconcile("personal-new-session", stopped_inventory(platform, vm), hub)
    assert any(payload["type"] == platform.delete and payload["vm_id"] == vm.id for _target, payload in hub.sent)
