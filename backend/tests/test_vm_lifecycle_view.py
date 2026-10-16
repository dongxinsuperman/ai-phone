"""Pure lifecycle presentation contracts: no DB, Agent, native tool, or network."""

from __future__ import annotations

from copy import deepcopy
import re

import pytest

from ai_phone.server import vm_lifecycle as lifecycle
from ai_phone.server.harmony_vm.models import HarmonyVmInstance
from ai_phone.server.hub import Hub
from ai_phone.server.ios_sim.models import IosSimVmInstance
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import AndroidVmInstance


_STATES = {None, "draft", "stopped", "starting", "running", "stopping"}
_ACTIONS = {"start", "stop", "dispatch", "delete", "edit", "copy"}


class ViewHub(Hub):
    def __init__(self, *, online=True):
        super().__init__()
        self.online = {"agent-a"} if online else set()
        self.vm_lock_store = DeviceLockStore()

    def has_agent(self, agent_id):
        return agent_id in self.online

    def agent_host_id(self, agent_id):
        return "host-a" if agent_id == "agent-a" else ""

    def subscriber_count(self, _serial):
        return 0

    def snapshot(self):
        return {"agents": [{"agent_id": agent_id} for agent_id in sorted(self.online)]}


@pytest.fixture(params=("android", "ios", "harmony"))
def vm_kind(request):
    return request.param


def make_vm(kind, *, state="error", owner="agent-a"):
    fields = {
        "id": "vmview001", "name": "lifecycle-view", "alias": "lifecycle-view",
        "state": state, "assigned_agent_id": owner, "error_message": "",
    }
    if kind == "android":
        vm = AndroidVmInstance(
            **fields, api_level=33, abi="arm64", runtime={},
            adb_serial="emulator-5554" if owner else None,
        )
    elif kind == "ios":
        vm = IosSimVmInstance(
            **fields, runtime="iOS-26", runtime_state={},
            udid="SIM-VMVIEW001" if owner else None,
        )
    else:
        vm = HarmonyVmInstance(
            **fields, runtime={}, hdc_port=10000 if owner else None,
            hdc_serial="127.0.0.1:10000" if owner else None,
            lease_token="view-lease-token" if owner else None,
        )
    lifecycle.update_runtime(vm, owner_host_id="host-a" if owner else "", instance_created=bool(owner))
    return vm


def store_details(kind, vm, details, *, state="error", reason="observation", ok=False):
    # Preserve each platform's actual stored envelope rather than placing the
    # same evidence in several locations and masking a resolver lookup bug.
    if kind == "ios":
        lifecycle.update_runtime(vm, details=details, last_state=state, last_reason=reason)
    else:
        lifecycle.update_runtime(vm, last_status={
            "state": state, "ok": ok, "reason": reason, "details": details,
        })


def checked_view(vm, hub):
    result = lifecycle.lifecycle_view(vm, hub)
    assert result["state"] in _STATES
    assert type(result["agent_online"]) is bool
    assert set(result["actions"]) == _ACTIONS
    assert all(type(value) is bool for value in result["actions"].values())
    assert isinstance(result["notice"], str)
    assert isinstance(result["error"], str)
    return result


def instance_observation(kind, vm, state):
    if kind == "android":
        identity = {"avd_name": f"aiphone_vm_{vm.id}"}
    elif kind == "ios":
        identity = {"sim_name": f"aiphone_sim_{vm.id}", "udid": vm.udid}
    else:
        safe_id = re.sub(r"[^A-Za-z0-9_]+", "_", vm.id)[:32]
        identity = {
            "instance_name": f"aiphone_harmony_{safe_id}",
            "instance_path": "/fixture/managed/harmony/instances",
            "lease_token": vm.lease_token,
            "hdc_serial": vm.hdc_serial,
        }
    return {
        "vm_id": vm.id, "state": state, "identity": identity,
        "resources_released": state == "stopped", "reason": "verified_native_observation",
    }


def store_observation(kind, vm, observation, location):
    if location == "normalized":
        lifecycle.update_runtime(vm, instance_observation=observation)
    else:
        store_details(kind, vm, {"instance_observation": observation})


def clear_stopped_harmony_lease(kind, vm):
    if kind == "harmony":
        vm.hdc_port = vm.hdc_serial = vm.lease_token = None


def test_vm_view_adds_lifecycle_without_replacing_raw_state(vm_kind):
    vm = make_vm(vm_kind, state="error")
    hub = ViewHub()
    expected = checked_view(vm, hub)
    payload = lifecycle.vm_view(vm, hub)
    assert payload["id"] == vm.id
    assert payload["state"] == "error"
    assert payload["lifecycle"] == expected
    assert vm.state == "error"


@pytest.mark.parametrize("last_state,reason,ok", [
    ("running", "started", True),
    ("error", "operation_not_started", False),
])
def test_cleanup_confirmed_alone_is_not_stopped_evidence(vm_kind, last_state, reason, ok):
    vm = make_vm(vm_kind)
    store_details(vm_kind, vm, {"cleanup_confirmed": True}, state=last_state, reason=reason, ok=ok)
    assert checked_view(vm, ViewHub())["state"] != "stopped"


def test_legacy_harmony_stopped_without_lease_is_unknown_when_cleanup_was_unconfirmed():
    vm = make_vm("harmony", state="stopped")
    vm.hdc_port = vm.hdc_serial = vm.lease_token = None
    store_details("harmony", vm, {"cleanup_confirmed": False}, state="stopped", reason="stop_unconfirmed")
    view = checked_view(vm, ViewHub())
    assert view["state"] is None
    assert view["actions"]["start"] is True
    assert view["actions"]["stop"] is True


def test_stopped_inventory_with_unobserved_resources_is_not_a_failed_action(vm_kind):
    vm = make_vm(vm_kind, state="stopped")
    observation = instance_observation(vm_kind, vm, "stopped")
    observation.update(resources_released=False, reason="ports_not_observed")
    lifecycle.update_runtime(vm, instance_observation=observation)
    view = checked_view(vm, ViewHub())
    assert view["state"] == "stopped" and view["notice"] == "" and view["error"] == ""
    assert view["actions"]["stop"] is False


@pytest.mark.parametrize("online", [True, False])
def test_unknown_bound_instance_offers_start_stop_only_when_owner_is_online(vm_kind, online):
    vm = make_vm(vm_kind)
    view = checked_view(vm, ViewHub(online=online))
    assert view["state"] is None
    assert view["agent_online"] is online
    assert view["actions"]["start"] is online
    assert view["actions"]["stop"] is online


def test_unbound_draft_uses_dispatch_instead_of_start(vm_kind):
    vm = make_vm(vm_kind, state="draft", owner=None)
    view = checked_view(vm, ViewHub())
    assert view["state"] == "draft"
    assert view["actions"]["start"] is False
    assert view["actions"]["dispatch"] is True


@pytest.mark.parametrize("created_here", [False, True])
def test_unbound_failed_attempt_with_existing_resources_is_not_dispatchable(vm_kind, created_here):
    from fastapi import HTTPException

    vm = make_vm(vm_kind, state="error", owner=None)
    lifecycle.update_runtime(vm, instance_created=True, operation={
        "request_id": "failed-first-bind", "first_bind": True, "created_in_attempt": created_here,
        "action": "start", "pending": False, "outcome": "failed",
    })
    store_details(vm_kind, vm, {"instance_created": True, "cleanup_confirmed": False})
    view = checked_view(vm, ViewHub())
    assert view["state"] is None and view["actions"]["dispatch"] is False
    with pytest.raises(HTTPException) as failure:
        lifecycle.require_startable(vm, ViewHub())
    assert failure.value.status_code == 409


@pytest.mark.parametrize("action,state", [("start", "starting"), ("stop", "stopping")])
def test_pending_operation_blocks_conflicting_lifecycle_actions(vm_kind, action, state):
    vm = make_vm(vm_kind, state=state)
    lifecycle.begin_operation(
        vm, action, request_id="current-operation", target_agent_id="agent-a",
        target_host_id="host-a", timeout_sec=60,
    )
    view = checked_view(vm, ViewHub())
    assert view["state"] == state
    for name in ("start", "stop", "dispatch", "delete"):
        assert view["actions"][name] is False
    # edit/copy remain boolean DTO fields; their enablement is defined separately.


@pytest.mark.parametrize("reason", ["health_recovered", "reclaimed"])
def test_health_or_reconnect_without_pending_operation_preserves_last_failure(vm_kind, reason):
    vm = make_vm(vm_kind)
    message = "此前停止失败，尚未确认清理"
    lifecycle.record_failure(vm, message, reason="stop_unconfirmed", action="stop", request_id="failed-stop")
    failure = deepcopy(lifecycle.runtime(vm)["last_failure"])
    lifecycle.update_runtime(vm, operation={
        "action": "stop", "request_id": "failed-stop", "pending": False, "outcome": "failed",
    })
    assert checked_view(vm, ViewHub())["error"] == message

    # Legacy handlers may clear their transient error field on a healthy
    # observation. That cannot erase the persistent operation failure.
    vm.error_message = ""
    vm.state = "running"
    store_details(vm_kind, vm, {"cleanup_confirmed": True}, state="running", reason=reason, ok=True)
    lifecycle.finish_operation(vm, {"state": "running", "ok": True, "reason": reason})
    assert lifecycle.runtime(vm)["last_failure"] == failure
    assert checked_view(vm, ViewHub())["error"] == message


def test_accepting_new_operation_clears_previous_failure(vm_kind):
    vm = make_vm(vm_kind)
    lifecycle.record_failure(vm, "旧操作失败", reason="old_failure", action="stop", request_id="old-stop")
    assert checked_view(vm, ViewHub())["error"] == "旧操作失败"
    lifecycle.begin_operation(
        vm, "start", request_id="new-start", target_agent_id="agent-a",
        target_host_id="host-a", timeout_sec=60,
    )
    vm.state = "starting"
    assert not lifecycle.runtime(vm).get("last_failure")
    view = checked_view(vm, ViewHub())
    assert view["state"] == "starting"
    assert view["error"] == ""


@pytest.mark.parametrize("location", ["normalized", "legacy"])
@pytest.mark.parametrize("observed_state", ["stopped", "running"])
def test_verified_observation_drives_actions_without_erasing_failure(vm_kind, location, observed_state):
    vm = make_vm(vm_kind)
    lifecycle.record_failure(vm, "先前操作失败仍需展示", reason="prior_failure", action="stop")
    observation = instance_observation(vm_kind, vm, observed_state)
    if observed_state == "stopped":
        clear_stopped_harmony_lease(vm_kind, vm)
    store_observation(vm_kind, vm, observation, location)
    view = checked_view(vm, ViewHub())
    assert view["state"] == observed_state
    assert view["actions"]["start"] is (observed_state == "stopped")
    assert view["error"] == "先前操作失败仍需展示"
    assert vm.state == "error", "展示层不能为了显示真实观察而重写旧raw state"


@pytest.mark.parametrize("corruption", ["wrong_vm_id", "wrong_native_name", "missing_identity"])
def test_invalid_observation_identity_never_confirms_stopped(vm_kind, corruption):
    vm = make_vm(vm_kind)
    observation = instance_observation(vm_kind, vm, "stopped")
    clear_stopped_harmony_lease(vm_kind, vm)
    if corruption == "wrong_vm_id":
        observation["vm_id"] = "a-different-vm"
    elif corruption == "missing_identity":
        observation["identity"] = {}
    else:
        key = {"android": "avd_name", "ios": "sim_name", "harmony": "instance_name"}[vm_kind]
        observation["identity"][key] = "unmanaged-instance"
    store_observation(vm_kind, vm, observation, "normalized")
    assert checked_view(vm, ViewHub())["state"] != "stopped"


def test_ios_simulator_name_without_matching_udid_does_not_confirm_stopped():
    vm = make_vm("ios")
    observation = instance_observation("ios", vm, "stopped")
    observation["identity"]["udid"] = "ANOTHER-SIMULATOR-UDID"
    store_observation("ios", vm, observation, "normalized")
    assert checked_view(vm, ViewHub())["state"] != "stopped"


def test_harmony_observation_uses_its_safe_native_instance_name():
    vm = make_vm("harmony")
    vm.id = "legacy-vm-with-hyphens"
    observation = instance_observation("harmony", vm, "stopped")
    clear_stopped_harmony_lease("harmony", vm)
    assert observation["identity"]["instance_name"] == "aiphone_harmony_legacy_vm_with_hyphens"
    store_observation("harmony", vm, observation, "normalized")
    view = checked_view(vm, ViewHub())
    assert view["state"] == "stopped" and view["actions"]["start"] is True


@pytest.mark.parametrize("observed_state", ["missing", "unknown"])
def test_missing_or_unknown_observation_is_not_a_stopped_machine(vm_kind, observed_state):
    vm = make_vm(vm_kind)
    observation = instance_observation(vm_kind, vm, observed_state)
    store_observation(vm_kind, vm, observation, "normalized")
    assert checked_view(vm, ViewHub())["state"] is None


def test_bare_legacy_stopped_state_is_not_native_stop_evidence(vm_kind):
    vm = make_vm(vm_kind, state="stopped")
    clear_stopped_harmony_lease(vm_kind, vm)
    assert checked_view(vm, ViewHub())["state"] is None


@pytest.mark.parametrize("location", ["normalized", "legacy"])
def test_new_accepted_operation_invalidates_previous_observation(vm_kind, location):
    vm = make_vm(vm_kind, state="stopped")
    observation = instance_observation(vm_kind, vm, "stopped")
    clear_stopped_harmony_lease(vm_kind, vm)
    store_observation(vm_kind, vm, observation, location)
    assert checked_view(vm, ViewHub())["state"] == "stopped"
    lifecycle.begin_operation(
        vm, "start", request_id="next-start", target_agent_id="agent-a",
        target_host_id="host-a", timeout_sec=60,
    )
    vm.state = "starting"
    view = checked_view(vm, ViewHub())
    assert view["state"] == "starting" and view["actions"]["start"] is False
    # Even after progress ends without a new native observation, the previous
    # stopped fact must not reappear through a legacy storage fallback.
    lifecycle.finish_operation(vm, {"state": "error", "ok": False, "reason": "start_failed"})
    vm.state = "error"
    assert checked_view(vm, ViewHub())["state"] is None
