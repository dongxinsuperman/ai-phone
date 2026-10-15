"""Shared lifecycle acceptance on isolated DB/fake Agents; never host devices."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ai_phone.server import db
from ai_phone.server.android_vm import api as android_api, service as android_service
from ai_phone.server.harmony_vm import api as harmony_api, service as harmony_service
from ai_phone.server.ios_sim import api as ios_api, service as ios_service
from ai_phone.server.harmony_vm.models import HarmonyVmInstance
from ai_phone.server.ios_sim.models import IosSimVmInstance
from ai_phone.server.models import AndroidVmInstance
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.vm_lifecycle import runtime, update_runtime
from ai_phone.shared import protocol as P


class ContractHub(Hub):
    def __init__(self, online=("old", "new")):
        super().__init__()
        self.online = set(online)
        self.sent = []
        self.vm_lock_store = DeviceLockStore()
        self.hosts = {"old": "host-one", "new": "host-two", "restarted": "host-one"}

    def has_agent(self, agent_id):
        return agent_id in self.online

    def agent_host_id(self, agent_id):
        return self.hosts.get(agent_id, "")

    def snapshot(self):
        return {"agents": [{"agent_id": a, "host_os": "Darwin"} for a in self.online]}

    async def send_to_agent(self, agent_id, payload):
        self.sent.append((agent_id, dict(payload)))
        return self.has_agent(agent_id)


@pytest.fixture(params=("android", "ios", "harmony"))
def platform(request):
    return {
        "android": SimpleNamespace(
            name="android",
            api=android_api,
            svc=android_service,
            model=AndroidVmInstance,
            serial="adb_serial",
            value="emulator-5554",
            delete=P.MSG_VM_DELETE,
        ),
        "ios": SimpleNamespace(
            name="ios",
            api=ios_api,
            svc=ios_service,
            model=IosSimVmInstance,
            serial="udid",
            value="SIM-UDID-1",
            delete=P.MSG_IOS_SIM_VM_DELETE,
        ),
        "harmony": SimpleNamespace(
            name="harmony",
            api=harmony_api,
            svc=harmony_service,
            model=HarmonyVmInstance,
            serial="hdc_serial",
            value=None,
            delete=P.MSG_HARMONY_VM_DELETE,
        ),
    }[request.param]


async def make_vm(session, platform, state="stopped", owner="old"):
    fields = dict(name="contract-vm", alias="contract-vm", state=state, assigned_agent_id=owner)
    if platform.name == "android":
        fields.update(api_level=33, abi="arm64", adb_serial=platform.value)
    elif platform.name == "ios":
        fields.update(udid=platform.value)
    vm = platform.model(**fields)
    session.add(vm)
    await session.flush()
    update_runtime(vm, owner_host_id="host-one", instance_created=True)
    await session.commit()
    return vm


async def status(platform, hub, vm_id, request_id="", *, ok=True, state="stopped", reason="deleted", **extra):
    msg = {
        "vm_id": vm_id,
        "request_id": request_id,
        "ok": ok,
        "state": state,
        "reason": reason,
        "details": {"cleanup_confirmed": ok},
        **extra,
    }
    await platform.svc.handle_vm_status("old", msg, hub)


@pytest.mark.asyncio
async def test_starting_blocks_stop_delete_and_edit(session, platform):
    vm = await make_vm(session, platform, state="starting")
    hub = ContractHub()
    stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
    for operation in (stop, platform.api.delete_instance):
        with pytest.raises(HTTPException) as exc:
            await operation(vm_id=vm.id, session=session, hub=hub)
        assert exc.value.status_code == 409
    assert vm.state == "starting" and not hub.sent


@pytest.mark.asyncio
async def test_task_occupancy_prevents_stop_without_changing_state(session, platform):
    vm = await make_vm(session, platform, state="running")
    hub = ContractHub()
    serial = f"harmony-vm:{vm.id}" if platform.name == "harmony" else platform.value
    await hub.vm_lock_store.acquire(serial, "run-1", "job")
    stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
    with pytest.raises(HTTPException) as exc:
        await stop(vm_id=vm.id, session=session, hub=hub)
    assert "device_busy" in str(exc.value.detail)
    assert vm.state == "running" and not hub.sent


@pytest.mark.asyncio
async def test_foreign_owner_cannot_steal_or_delete_valid_instance(session, platform):
    vm = await make_vm(session, platform, state="agent_offline")
    hub = ContractHub(online=("new",))
    msg = {
        "vm_id": vm.id,
        "state": "running",
        "ok": True,
        "reason": "reclaimed",
        platform.serial: platform.value,
    }
    await platform.svc.handle_vm_status("new", msg, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "old" and vm.state == "agent_offline"
    assert not hub.sent


@pytest.mark.asyncio
async def test_unknown_inventory_does_not_claim_missing_or_stopped(session, platform):
    vm = await make_vm(session, platform, state="running")
    hub = ContractHub()
    inventory = (
        {"instances": [], "inventory_complete": False}
        if platform.name == "harmony"
        else {"vm_ids": [], "stopped_vm_ids": [], "inventory_complete": False}
    )
    await platform.svc.handle_vm_reconcile("old", inventory, hub)
    await session.refresh(vm)
    assert vm.state == "running" and vm.assigned_agent_id == "old"


@pytest.mark.asyncio
async def test_android_port_reservation_is_atomic_across_sessions(session):
    vms = [AndroidVmInstance(name=f"vm-{i}", alias=f"vm-{i}", api_level=33, abi="arm64") for i in range(2)]
    session.add_all(vms)
    await session.commit()
    first_reserved, allow_commit = asyncio.Event(), asyncio.Event()
    factory = db.get_session_factory()

    async def reserve(vm_id, first=False):
        async with factory() as other:
            vm = await other.get(AndroidVmInstance, vm_id)
            port, _ = await android_service.assign_emulator_port(other, vm)
            if first:
                first_reserved.set()
                await allow_commit.wait()
            await other.commit()
            return port

    first = asyncio.create_task(reserve(vms[0].id, True))
    await first_reserved.wait()
    second = asyncio.create_task(reserve(vms[1].id))
    await asyncio.sleep(0.02)
    assert not second.done()
    allow_commit.set()
    assert await first != await second


@pytest.mark.asyncio
async def test_all_failed_probe_sends_complete_without_waiting(session, platform):
    vm = await make_vm(session, platform)
    hub = ContractHub()

    async def fail(*args):
        return False

    hub.send_to_agent = fail
    waiter = {
        "android": android_service.VmCapabilityWaiter,
        "ios": ios_service.IosSimCapabilityWaiter,
        "harmony": harmony_service.HarmonyVmCapabilityWaiter,
    }[platform.name]()
    result = await asyncio.wait_for(waiter.probe(hub=hub, vm=vm, timeout_sec=60), timeout=0.2)
    assert len(result["agents"]) == 2 and not any(row["ok"] for row in result["agents"])


@pytest.mark.asyncio
async def test_stopped_legacy_instance_adopts_verified_restarted_host_once(session, platform):
    vm = await make_vm(session, platform, state="agent_offline")
    field = "runtime_state" if platform.name == "ios" else "runtime"
    setattr(vm, field, {"instance_created": True})
    await session.commit()
    hub = ContractHub(online=("restarted",))
    if platform.name == "harmony":
        inventory = {
            "instances": [
                {
                    "vm_id": vm.id,
                    "state": "stopped",
                    "details": {
                        "instance_name": f"aiphone_harmony_{vm.id}",
                        "managed_instance_present": True,
                    },
                }
            ],
            "inventory_complete": True,
        }
    else:
        proof = (
            {"avd_name": f"aiphone_vm_{vm.id}"}
            if platform.name == "android"
            else {"udid": vm.udid, "name": f"aiphone_sim_{vm.id}", "state": "Shutdown"}
        )
        inventory = {
            "vm_ids": [vm.id],
            "stopped_vm_ids": [vm.id],
            "inventory_complete": True,
            "instance_proofs": {vm.id: proof},
        }
    await platform.svc.handle_vm_reconcile("restarted", inventory, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "restarted" and vm.state == "stopped"
    assert runtime(vm)["owner_host_id"] == "host-one"
    # A second, different host cannot reuse legacy adoption after the first.
    hub.online = {"new"}
    await platform.svc.handle_vm_reconcile("new", inventory, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "restarted"


def test_observation_from_previous_operation_cannot_restore_running():
    from ai_phone.server.vm_lifecycle import accept_status

    vm = SimpleNamespace(
        runtime={"operation": {"request_id": "stop-new", "action": "stop", "pending": False}}
    )
    assert not accept_status(
        vm, {"state": "running", "reason": "transport_recovered", "observed_request_id": "start-old"}
    )


@pytest.mark.asyncio
async def test_unbound_failed_configuration_can_be_deleted(session, platform):
    vm = await make_vm(session, platform, state="error", owner=None)
    setattr(vm, platform.serial, None)
    await session.commit()
    vm_id = vm.id
    result = await platform.api.delete_instance(vm_id=vm_id, session=session, hub=ContractHub())
    assert result["deleted"] is True
    async with db.get_session_factory()() as check:
        assert await check.get(platform.model, vm_id) is None


@pytest.mark.asyncio
async def test_probe_does_not_hold_vm_lock_needed_by_agent_status(session, platform, monkeypatch):
    if platform.name == "android":
        pytest.skip("Android performs its capability check inside Agent startup")
    vm = await make_vm(session, platform)
    hub = ContractHub()

    async def probe_agent(**_kwargs):
        # Simulates a preceding status/reconcile on the sole Agent receive loop;
        # that handler must finish before the capability reply can arrive.
        inventory = (
            {"instances": [], "inventory_complete": False}
            if platform.name == "harmony"
            else {"vm_ids": [], "inventory_complete": False}
        )
        await asyncio.wait_for(platform.svc.handle_vm_reconcile("old", inventory, hub), timeout=0.2)
        return {"ok": True}

    monkeypatch.setattr(
        platform.api,
        "get_capability_waiter",
        lambda: SimpleNamespace(probe_agent=probe_agent, excluded_ports_for=lambda _agent: []),
    )
    kwargs = {"probe": True} if platform.name == "ios" else {}
    result = await platform.api._send_start(vm.id, "old", session, hub, **kwargs)
    assert result["sent"] and result["instance"]["state"] == "starting"


@pytest.mark.asyncio
async def test_probe_rechecks_operation_state_before_dispatch(session, platform, monkeypatch):
    if platform.name == "android":
        pytest.skip("Android has no asynchronous Server capability wait")
    vm = await make_vm(session, platform)
    hub = ContractHub()

    async def probe_agent(**_kwargs):
        async with db.get_session_factory()() as other:
            current = await other.get(platform.model, vm.id)
            current.state = "starting"
            await other.commit()
        return {"ok": True}

    monkeypatch.setattr(
        platform.api,
        "get_capability_waiter",
        lambda: SimpleNamespace(probe_agent=probe_agent, excluded_ports_for=lambda _agent: []),
    )
    kwargs = {"probe": True} if platform.name == "ios" else {}
    with pytest.raises(HTTPException) as exc:
        await platform.api._send_start(vm.id, "old", session, hub, **kwargs)
    assert exc.value.status_code == 409 and not hub.sent


@pytest.mark.asyncio
async def test_stopped_ack_keeps_late_readiness_out_of_device_pool(session, platform):
    vm = await make_vm(session, platform, state="running")
    hub = ContractHub()
    if platform.name == "harmony":
        await harmony_service.allocate_port_lease(session, vm, agent_id="old")
        await session.commit()
    serial = f"harmony-vm:{vm.id}" if platform.name == "harmony" else platform.value
    stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
    await stop(vm_id=vm.id, session=session, hub=hub)
    msg = {
        "vm_id": vm.id,
        "request_id": runtime(vm)["operation"]["request_id"],
        "state": "stopped",
        "reason": "stopped",
        "ok": True,
        "details": {"cleanup_confirmed": True},
    }
    if platform.name == "harmony":
        msg["lease_token"] = vm.lease_token
        msg["hdc_serial"] = vm.hdc_serial
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.state == "stopped"
    hub.set_device_readiness(serial, {"ready": True})
    extra = hub.get_device_extra(serial)
    assert extra["readiness"]["ready"] is False
    assert extra["readiness"]["not_ready_reason"] == "vm_maintenance"


@pytest.mark.asyncio
async def test_harmony_bound_hardware_patch_fails_before_saving(session):
    from ai_phone.server.harmony_vm.schemas import HarmonyVmPatchReq

    vm = HarmonyVmInstance(
        name="fixed-config", alias="fixed-config", memory_gb=4, state="stopped", assigned_agent_id="old"
    )
    session.add(vm)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await harmony_api.patch_instance(HarmonyVmPatchReq(memory_gb=8), vm_id=vm.id, session=session)
    assert exc.value.status_code == 409 and vm.memory_gb == 4
    assert exc.value.detail["reason"] == "bound_configuration_immutable"


@pytest.mark.asyncio
async def test_host_identity_conflict_preserves_authoritative_and_native_instances(session, platform):
    vm = await make_vm(session, platform, state="stopped")
    hub = ContractHub()
    original_serial = getattr(vm, platform.serial)
    if platform.name == "harmony":
        inventory = {
            "instances": [
                {
                    "vm_id": vm.id,
                    "state": "stopped",
                    "lease_token": "copy-token",
                    "hdc_serial": "127.0.0.1:11011",
                    "details": {
                        "instance_name": f"aiphone_harmony_{vm.id}",
                        "managed_instance_present": True,
                    },
                }
            ],
            "inventory_complete": True,
        }
    else:
        proof = (
            {"avd_name": f"aiphone_vm_{vm.id}"}
            if platform.name == "android"
            else {"udid": "copy-udid", "name": f"aiphone_sim_{vm.id}", "state": "Shutdown"}
        )
        inventory = {
            "vm_ids": [vm.id],
            "stopped_vm_ids": [vm.id],
            "inventory_complete": True,
            "instance_proofs": {vm.id: proof},
        }
    await platform.svc.handle_vm_reconcile("new", inventory, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "old" and vm.state == "stopped"
    assert getattr(vm, platform.serial) == original_serial
    assert not hub.sent, "different identity files do not prove separate native data"
    # A successful reply to copy cleanup must not retire the authoritative row.
    await platform.svc.handle_vm_status(
        "new",
        {
            "vm_id": vm.id,
            "request_id": "unrequested-foreign-cleanup",
            "state": "stopped",
            "reason": "deleted",
            "ok": True,
            "details": {"cleanup_confirmed": True},
        },
        hub,
    )
    await session.refresh(vm)
    assert vm.assigned_agent_id == "old" and vm.state == "stopped"


@pytest.mark.asyncio
async def test_unknown_legacy_host_does_not_authorize_foreign_copy_deletion(session, platform):
    from ai_phone.server.vm_lifecycle import report_owner_conflict

    vm = await make_vm(session, platform)
    setattr(vm, "runtime_state" if platform.name == "ios" else "runtime", {})
    await session.commit()
    proof = {
        "avd_name": f"aiphone_vm_{vm.id}",
        "name": f"aiphone_sim_{vm.id}",
        "udid": "copy-udid",
        "instance_name": f"aiphone_harmony_{vm.id}",
        "managed_instance_present": True,
    }
    hub = ContractHub()
    assert not await report_owner_conflict(hub, vm, "new", proof)
    assert not hub.sent and vm.assigned_agent_id == "old"


@pytest.mark.asyncio
async def test_reused_agent_id_on_wrong_host_cannot_receive_stop(session, platform):
    vm = await make_vm(session, platform, state="running")
    hub = ContractHub()
    hub.hosts["old"] = "different-computer"
    stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
    with pytest.raises(HTTPException) as exc:
        await stop(vm_id=vm.id, session=session, hub=hub)
    assert exc.value.status_code == 409 and not hub.sent
    assert vm.state == "running" and vm.assigned_agent_id == "old"


async def start_first(session, platform, hub, monkeypatch):
    vm = platform.model(
        name="first-attempt",
        alias="first-attempt",
        state="draft",
        **({"api_level": 33, "abi": "arm64"} if platform.name == "android" else {}),
    )
    session.add(vm)
    await session.commit()

    async def available(**_kwargs):
        return {"ok": True}

    if platform.name != "android":
        monkeypatch.setattr(
            platform.api,
            "get_capability_waiter",
            lambda: SimpleNamespace(probe_agent=available, excluded_ports_for=lambda _agent: []),
        )
    if platform.name == "android":
        result = await platform.api._send_start(vm_id=vm.id, agent_id="old", session=session, hub=hub)
    else:
        result = await platform.api._send_start(vm.id, "old", session, hub)
    return vm, result


@pytest.mark.asyncio
async def test_first_binding_confirms_only_exact_success_ack(session, platform, monkeypatch):
    hub = ContractHub()
    vm, response = await start_first(session, platform, hub, monkeypatch)
    assert response["sent"] and vm.state == "starting" and vm.assigned_agent_id is None
    op = runtime(vm)["operation"]
    assert op["pending"] and op["target_agent_id"] == "old" and op["first_bind"]
    assert hub.sent[-1][1]["rollback_new_instance"] is True
    assert hub.sent[-1][1]["operation_timeout_sec"] > 0
    msg = {
        "vm_id": vm.id,
        "state": "running",
        "ok": True,
        "reason": "started",
        "request_id": "older-attempt",
        platform.serial: getattr(vm, platform.serial) or platform.value or "",
    }
    if platform.name == "ios":
        msg["udid"] = "new-owned-udid"
    if platform.name == "harmony":
        msg["lease_token"] = vm.lease_token
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id is None and vm.state == "starting"
    msg["request_id"] = op["request_id"]
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "old" and vm.state == "running"
    assert runtime(vm)["operation"]["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_failed_first_attempt_rolls_back_binding_and_never_resumes(session, platform, monkeypatch):
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    request_id = runtime(vm)["operation"]["request_id"]
    msg = {
        "vm_id": vm.id,
        "request_id": request_id,
        "state": "error",
        "ok": False,
        "reason": "startup_failed",
        "details": {
            "cleanup_confirmed": True,
            "instance_deleted": True,
            "instance_created": False,
            "rollback_new_instance": True,
        },
    }
    if platform.name == "ios":
        msg["udid"] = "deleted-after-boot"
    if platform.name == "harmony":
        msg["lease_token"] = vm.lease_token
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert not runtime(vm)["operation"]["pending"]
    assert not runtime(vm).get("instance_created")
    assert getattr(vm, platform.serial) is None
    # A duplicated success cannot reopen the failed transaction or bind it.
    await platform.svc.handle_vm_status(
        "old", {**msg, "state": "running", "ok": True, "reason": "started"}, hub
    )
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert len([payload for _, payload in hub.sent if payload["type"].endswith("vm_start")]) == 1


@pytest.mark.asyncio
async def test_one_attempt_timeout_is_terminal_and_does_not_retry(session, platform, monkeypatch):
    from ai_phone.server import vm_lifecycle

    monkeypatch.setattr(vm_lifecycle, "operation_timeout_seconds", lambda *_args: 0.03)
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    await asyncio.sleep(0.08)
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert runtime(vm)["operation"]["failure_reason"] == "operation_timeout"
    assert not runtime(vm)["operation"]["pending"]
    assert len(hub.sent) == 1  # no hidden retry, resume, or dispatch to another Agent


@pytest.mark.asyncio
async def test_server_restart_ends_unfinished_attempt_without_confirming_owner(
    session, platform, monkeypatch
):
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    await platform.svc.reset_vm_states_on_startup(session)
    await session.commit()
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert runtime(vm)["operation"]["failure_reason"] == "server_restarted"
    assert not runtime(vm)["operation"]["pending"]
    assert len(hub.sent) == 1


@pytest.mark.asyncio
async def test_offline_delete_is_immediate_and_only_orphan_inventory_cleans_residue(session, platform):
    vm = await make_vm(session, platform, state="agent_offline")
    vm_id = vm.id
    hub = ContractHub(online=())
    deleted = await platform.api.delete_instance(vm_id=vm_id, session=session, hub=hub)
    assert deleted["deleted"] is True and not deleted["cleanup_sent"]
    async with db.get_session_factory()() as check:
        assert await check.get(platform.model, vm_id) is None
    hub.online.add("old")
    inventory = (
        {"instances": [{"vm_id": vm_id, "state": "stopped"}]}
        if platform.name == "harmony"
        else {"vm_ids": [vm_id], "stopped_vm_ids": [vm_id], "inventory_complete": True}
    )
    await platform.svc.handle_vm_reconcile("old", inventory, hub)
    assert hub.sent[-1][1]["type"] == platform.delete and hub.sent[-1][1]["vm_id"] == vm_id
    async with db.get_session_factory()() as check:
        assert await check.get(platform.model, vm_id) is None


@pytest.mark.asyncio
async def test_bound_restart_failure_preserves_confirmed_identity_and_data(session, platform, monkeypatch):
    vm = await make_vm(session, platform)
    hub = ContractHub()

    async def available(**_kwargs):
        return {"ok": True}

    if platform.name != "android":
        monkeypatch.setattr(
            platform.api,
            "get_capability_waiter",
            lambda: SimpleNamespace(probe_agent=available, excluded_ports_for=lambda _agent: []),
        )
    if platform.name == "android":
        await platform.api._send_start(vm_id=vm.id, agent_id="old", session=session, hub=hub)
    else:
        await platform.api._send_start(vm.id, "old", session, hub)
    assert hub.sent[-1][1]["rollback_new_instance"] is False
    assert hub.sent[-1][1]["require_existing_instance"] is True
    msg = {
        "vm_id": vm.id,
        "request_id": runtime(vm)["operation"]["request_id"],
        "state": "error",
        "ok": False,
        "reason": "boot_failed",
        "details": {"cleanup_confirmed": True, "instance_created": True},
    }
    if platform.name == "harmony":
        msg["lease_token"] = vm.lease_token
    if platform.name == "ios":
        msg["udid"] = vm.udid
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id == "old"
    assert runtime(vm)["instance_created"] is True
    if platform.name == "ios":
        assert vm.udid == platform.value
    assert len(hub.sent) == 1


@pytest.mark.asyncio
async def test_health_failure_after_successful_first_bind_keeps_confirmed_owner(
    session, platform, monkeypatch
):
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    req = runtime(vm)["operation"]["request_id"]
    msg = {"vm_id": vm.id, "request_id": req, "state": "running", "ok": True, "reason": "started"}
    if platform.name == "android":
        msg["adb_serial"] = vm.adb_serial
    elif platform.name == "ios":
        msg["udid"] = "stable-owned-udid"
    else:
        msg["lease_token"], msg["hdc_serial"] = vm.lease_token, vm.hdc_serial
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.assigned_agent_id == "old"
    await platform.svc.handle_vm_status(
        "old",
        {
            **msg,
            "request_id": "",
            "observed_request_id": req,
            "state": "error",
            "ok": False,
            "reason": "transport_unavailable",
            "details": {"cleanup_confirmed": False},
        },
        hub,
    )
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id == "old"
    assert runtime(vm)["operation"]["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_late_first_start_after_timeout_is_cleaned_once_without_binding_or_retry(
    session, platform, monkeypatch
):
    from ai_phone.server import vm_lifecycle

    monkeypatch.setattr(vm_lifecycle, "operation_timeout_seconds", lambda *_args: 0.02)
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    req = runtime(vm)["operation"]["request_id"]
    await asyncio.sleep(0.05)
    await session.refresh(vm)
    proof = (
        {"avd_name": f"aiphone_vm_{vm.id}"}
        if platform.name == "android"
        else {"sim_name": f"aiphone_sim_{vm.id}"}
        if platform.name == "ios"
        else {"instance_name": f"aiphone_harmony_{vm.id}"}
    )
    proof["created_in_attempt"] = True
    late = {
        "vm_id": vm.id,
        "request_id": req,
        "state": "running",
        "ok": True,
        "reason": "started",
        "details": proof,
    }
    if platform.name == "android":
        late["adb_serial"] = vm.adb_serial
    elif platform.name == "ios":
        late["udid"] = "late-created-udid"
    else:
        late["lease_token"], late["hdc_serial"] = vm.lease_token, vm.hdc_serial
    await platform.svc.handle_vm_status("old", late, hub)
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert len(hub.sent) == 2 and hub.sent[-1][1]["type"] == platform.delete
    cleanup_request = hub.sent[-1][1]["request_id"]
    await platform.svc.handle_vm_status("old", late, hub)
    assert len(hub.sent) == 2
    await platform.svc.handle_vm_status(
        "old",
        {
            "vm_id": vm.id,
            "request_id": cleanup_request,
            "state": "stopped",
            "ok": True,
            "reason": "deleted",
            "details": {"cleanup_confirmed": True},
        },
        hub,
    )
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert runtime(vm)["operation"]["outcome"] == "failed"
    assert runtime(vm)["details"]["cleanup_confirmed"]
    assert getattr(vm, platform.serial) is None
    assert len(hub.sent) == 2


def test_timeout_does_not_reuse_cleanup_proof_from_previous_attempt():
    from ai_phone.server.vm_lifecycle import fail_attempt, require_startable

    vm = SimpleNamespace(
        id="no-live-task",
        state="starting",
        assigned_agent_id=None,
        error_message="",
        runtime={
            "operation": {
                "request_id": "new-attempt",
                "action": "start",
                "first_bind": True,
                "pending": True,
                "outcome": "pending",
            },
            "last_status": {"details": {"cleanup_confirmed": True, "instance_created": False}},
        },
    )
    fail_attempt(vm, "operation_timeout", "timed out")
    assert runtime(vm)["last_status"]["details"]["cleanup_confirmed"] is False
    with pytest.raises(HTTPException):
        require_startable(vm)


@pytest.mark.asyncio
async def test_expired_deadline_rejects_success_even_if_watchdog_has_not_run(session, platform, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from ai_phone.server.vm_lifecycle import cancel_operation_timeout

    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    request_id = runtime(vm)["operation"]["request_id"]
    # Deterministic delayed-timer scenario: cancel its scheduling and persist a
    # deadline in the past, leaving pending=True until the reply handler runs.
    cancel_operation_timeout(vm)
    update_runtime(
        vm,
        operation={
            **runtime(vm)["operation"],
            "deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        },
    )
    await session.commit()
    proof = (
        {"avd_name": f"aiphone_vm_{vm.id}"}
        if platform.name == "android"
        else {"sim_name": f"aiphone_sim_{vm.id}"}
        if platform.name == "ios"
        else {"instance_name": f"aiphone_harmony_{vm.id}"}
    )
    proof["created_in_attempt"] = True
    msg = {
        "vm_id": vm.id,
        "request_id": request_id,
        "state": "running",
        "ok": True,
        "reason": "started",
        "details": proof,
    }
    if platform.name == "android":
        msg["adb_serial"] = vm.adb_serial
    elif platform.name == "ios":
        msg["udid"] = "created-after-deadline"
    else:
        msg["lease_token"], msg["hdc_serial"] = vm.lease_token, vm.hdc_serial
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    op = runtime(vm)["operation"]
    assert op["outcome"] == "failed" and op["failure_reason"] == "operation_timeout"
    assert not op["pending"]
    assert len(hub.sent) == 2 and hub.sent[-1][1]["type"] == platform.delete
    await platform.svc.handle_vm_status("old", msg, hub)
    assert len(hub.sent) == 2


@pytest.mark.asyncio
async def test_late_verified_stop_records_fact_without_turning_timeout_into_success(session, platform):
    from datetime import datetime, timedelta, timezone
    from ai_phone.server.vm_lifecycle import cancel_operation_timeout

    vm = await make_vm(session, platform, state="running")
    hub = ContractHub()
    if platform.name == "harmony":
        await harmony_service.allocate_port_lease(session, vm, agent_id="old")
        await session.commit()
    stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
    await stop(vm_id=vm.id, session=session, hub=hub)
    cancel_operation_timeout(vm)
    update_runtime(
        vm,
        operation={
            **runtime(vm)["operation"],
            "deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        },
    )
    await session.commit()
    msg = {
        "vm_id": vm.id,
        "request_id": runtime(vm)["operation"]["request_id"],
        "state": "stopped",
        "ok": True,
        "reason": "stopped",
        "details": {"cleanup_confirmed": True},
    }
    if platform.name == "harmony":
        msg["lease_token"], msg["hdc_serial"] = vm.lease_token, vm.hdc_serial
    await platform.svc.handle_vm_status("old", msg, hub)
    await session.refresh(vm)
    assert vm.state == "stopped" and vm.assigned_agent_id == "old"
    assert runtime(vm)["operation"]["outcome"] == "failed"
    assert runtime(vm)["operation"]["failure_reason"] == "operation_timeout"
    assert len(hub.sent) == 1


@pytest.mark.asyncio
async def test_failed_first_bind_never_deletes_native_data_it_did_not_create(session, platform, monkeypatch):
    hub = ContractHub()
    vm, _ = await start_first(session, platform, hub, monkeypatch)
    vm_id = vm.id
    req = runtime(vm)["operation"]["request_id"]
    proof = (
        {"avd_name": f"aiphone_vm_{vm_id}"}
        if platform.name == "android"
        else {"sim_name": f"aiphone_sim_{vm_id}"}
        if platform.name == "ios"
        else {"instance_name": f"aiphone_harmony_{vm_id}", "managed_instance_present": True}
    )
    failed = {
        "vm_id": vm_id,
        "request_id": req,
        "state": "error",
        "ok": False,
        "reason": "instance_preexisting",
        "details": {
            **proof,
            "created_in_attempt": False,
            "instance_created": True,
            "instance_deleted": False,
            "cleanup_confirmed": True,
        },
    }
    if platform.name == "ios":
        failed["udid"] = "OLD-DATA"
    elif platform.name == "harmony":
        failed["lease_token"], failed["hdc_serial"] = vm.lease_token, vm.hdc_serial
    await platform.svc.handle_vm_status("old", failed, hub)
    await session.refresh(vm)
    assert vm.state == "error" and vm.assigned_agent_id is None
    assert runtime(vm)["operation"]["created_in_attempt"] is False
    if platform.name == "harmony":
        inventory = {
            "instances": [{"vm_id": vm_id, "state": "stopped", "details": proof}],
            "inventory_complete": True,
        }
    else:
        inventory_proof = (
            {"avd_name": proof["avd_name"]}
            if platform.name == "android"
            else {"name": proof["sim_name"], "udid": "OLD-DATA", "state": "Shutdown"}
        )
        inventory = {
            "vm_ids": [vm_id],
            "stopped_vm_ids": [vm_id],
            "inventory_complete": True,
            "instance_proofs": {vm_id: inventory_proof},
        }
    await platform.svc.handle_vm_reconcile("old", inventory, hub)
    assert len(hub.sent) == 1  # native instance remains untouched by reconciliation
    # Even contradictory late success cannot override explicit no-create proof.
    await platform.svc.handle_vm_status(
        "old",
        {
            **failed,
            "state": "running",
            "ok": True,
            "reason": "started",
            "details": {**proof, "created_in_attempt": True},
        },
        hub,
    )
    assert len(hub.sent) == 1
    await session.refresh(vm)
    assert vm.assigned_agent_id is None and vm.state == "error"
    # An explicit user DELETE is a separate authority: retire the ledger and
    # clean this managed VM ID, even though the failed attempt did not create it.
    deleted = await platform.api.delete_instance(vm_id=vm_id, session=session, hub=hub)
    assert deleted["deleted"] is True and hub.sent[-1][1]["type"] == platform.delete
    async with db.get_session_factory()() as check:
        assert await check.get(platform.model, vm_id) is None
