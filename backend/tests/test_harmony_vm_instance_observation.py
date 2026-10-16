"""Current Harmony instance facts; native processes, HDC and ports are fakes."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import threading

import psutil
import pytest

from ai_phone.agent.harmony_vm import capability as cap
from ai_phone.agent.harmony_vm import manager as mod
from ai_phone.agent.harmony_vm.registry import (
    managed_vm_owner, register_managed_vm, unregister_managed_serial, unregister_managed_vm,
)


@pytest.fixture
def local_vm(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("observation must not invoke a native mutation or SDK probe")

    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod, "hdc_run", forbidden)
    monkeypatch.setattr(mod, "open_harmony_driver", forbidden)
    monkeypatch.setattr(mod, "find_harmony_tools", forbidden)
    monkeypatch.setattr(mod, "probe_harmony_vm_capability", forbidden)
    monkeypatch.setattr(mod, "_spawn_harmony_emulator", forbidden)
    monkeypatch.setattr(mod.HarmonyVmManager, "orphan_cleanup", property(lambda _self: True))
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter(()))
    manager = mod.HarmonyVmManager(runtime_dir=tmp_path)
    known = {
        "instance_name": "aiphone_harmony_observe_vm", "instance_path": str(manager.instance_path),
        "hdc_serial": "127.0.0.1:10007", "hdc_port": 10007, "lease_token": "old-local-lease",
        "ready": False, "cleanup_confirmed": False, "created": True,
    }
    config = manager.instance_path / known["instance_name"] / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("private-userdata")
    manager._known["observe-vm"] = known
    manager._save_registry()
    monkeypatch.setattr(mod, "hdc_list_targets", lambda: [SimpleNamespace(serial=known["hdc_serial"], status="Offline")])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(manager, "_create_instance", forbidden)
    monkeypatch.setattr(manager, "_delete_instance_definition", forbidden)
    yield manager, known, config
    for runtime in manager._runtimes.values():
        if runtime.log_file:
            runtime.log_file.close()
    for vm_id in ("observe-vm", "other-vm"):
        for token in ("old-local-lease", "new-lease", "other-lease"):
            unregister_managed_vm(vm_id, token)
    for port in (10007, 10008):
        unregister_managed_serial(f"127.0.0.1:{port}")


def native_process(known):
    return SimpleNamespace(info={"name": "Emulator", "cmdline": [
        "/fake/Emulator", "-start", known["instance_name"],
        "-instancePath", known["instance_path"], "-hdcPort", str(known["hdc_port"]),
    ]})


def test_legacy_false_flag_is_replaced_by_current_readonly_facts(local_vm, monkeypatch):
    manager, known, config = local_vm
    registry_before, data_before = manager.registry_path.read_bytes(), config.read_bytes()
    monkeypatch.setattr(manager, "_save_registry", lambda: pytest.fail("readonly observation wrote registry"))
    result = manager.observe_instance("observe-vm")
    assert result == {
        "vm_id": "observe-vm", "state": "stopped", "resources_released": True,
        "identity": {key: known[key] for key in ("instance_name", "instance_path", "lease_token", "hdc_serial")},
        "reason": "instance_process_absent_resources_released",
    }
    assert manager.registry_path.read_bytes() == registry_before and config.read_bytes() == data_before
    assert manager._known["observe-vm"]["cleanup_confirmed"] is False


def test_inspect_skips_sdk_and_ignores_echoed_identity_or_monotonic(local_vm):
    manager, known, _config = local_vm
    result = manager.probe({"inspect_instance": True, "vm_id": "observe-vm",
                            "lease_token": "request-is-not-proof", "hdc_serial": "127.0.0.1:12345",
                            "_operation_deadline": -1, "operation_timeout_sec": 5})
    assert result["ok"] is True
    observation = result["details"]["instance_observation"]
    assert observation["state"] == "stopped"
    assert observation["identity"]["lease_token"] == known["lease_token"]
    assert observation["identity"]["hdc_serial"] == known["hdc_serial"]
    assert set(result["details"]) == {"instance_observation"}


def test_no_local_record_is_unknown_even_when_generated_path_is_absent(local_vm):
    manager, _known, _config = local_vm
    observation = manager.observe_instance("never-recorded")
    assert observation["state"] == "unknown" and not observation["resources_released"]
    assert set(observation["identity"].values()) == {""}


@pytest.mark.parametrize("directory_present", [False, True])
def test_missing_requires_known_identity_absent_directory_and_absent_process(local_vm, directory_present):
    manager, _known, config = local_vm
    config.unlink()
    if not directory_present:
        config.parent.rmdir()
    observation = manager.observe_instance("observe-vm")
    assert observation["state"] == ("stopped" if directory_present else "missing")
    assert observation["resources_released"] is (not directory_present)


def test_current_exit_sample_keeps_stopped_fact_without_certifying_incomplete_definition(local_vm):
    manager, known, config = local_vm
    config.unlink()
    observation = manager._observation_from_exit(
        "observe-vm", known, {"instance_process_status": "absent"},
        resources_released=True, reason="removed",
    )
    assert observation["state"] == "stopped" and observation["resources_released"] is False
    assert observation["reason"] == "instance_definition_unverified"


@pytest.mark.parametrize("cleanup_pending", [False, True])
def test_stopped_incomplete_definition_cannot_clear_pending_resources_or_start(local_vm, cleanup_pending):
    manager, known, config = local_vm
    known["cleanup_pending"] = cleanup_pending
    config.unlink()
    with pytest.raises(RuntimeError, match="instance_definition_unverified") as caught:
        manager.start_sync({"vm_id": "observe-vm", "assigned_port": 10008, "lease_token": "new-lease",
                            "require_existing_instance": True, "rollback_new_instance": False})
    observation = caught.value.cleanup_details["instance_observation"]
    assert observation["state"] == "stopped" and observation["resources_released"] is False
    assert known["cleanup_pending"] is cleanup_pending and not config.exists()


def test_mismatched_registered_identity_remains_unknown(local_vm, monkeypatch):
    manager, known, _config = local_vm
    known["instance_name"] = "aiphone_harmony_another_vm"
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: pytest.fail("invalid identity scanned"))
    observation = manager.observe_instance("observe-vm")
    assert observation["state"] == "unknown" and observation["resources_released"] is False
    assert observation["reason"] == "instance_identity_unverified"


@pytest.mark.parametrize("status", ["running", "unknown"])
def test_native_process_facts_override_old_stop_flags(local_vm, monkeypatch, status):
    manager, known, _config = local_vm
    known.update(cleanup_confirmed=True, cleanup_pending=True, ready=False)
    def process_iter(_attrs):
        if status == "unknown":
            raise PermissionError("fixture process query denied")
        return iter([native_process(known)])
    monkeypatch.setattr(psutil, "process_iter", process_iter)
    monkeypatch.setattr(mod, "hdc_list_targets", lambda: pytest.fail("process already settles this observation"))
    result = manager.observe_instance("observe-vm")
    assert result["state"] == status and result["resources_released"] is False


@pytest.mark.parametrize("name", ["Emulator", "Emulator.exe"])
@pytest.mark.parametrize("args", [None, []])
def test_visible_native_process_with_unreadable_argv_cannot_prove_absence(local_vm, monkeypatch, name, args):
    manager, _known, _config = local_vm
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter([
        SimpleNamespace(info={"name": name, "cmdline": args}),
    ]))
    monkeypatch.setattr(mod, "hdc_list_targets", lambda: pytest.fail("unknown process must not release resources"))
    observation = manager.observe_instance("observe-vm")
    assert observation["state"] == "unknown"
    assert observation["resources_released"] is False
    assert observation["reason"] == "instance_process_unknown"


def test_unrelated_program_without_argv_does_not_add_a_permission_requirement(local_vm, monkeypatch):
    manager, _known, _config = local_vm
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter([
        SimpleNamespace(info={"name": "other-program", "cmdline": []}),
    ]))
    observation = manager.observe_instance("observe-vm")
    assert observation["state"] == "stopped" and observation["resources_released"] is True


def test_other_owner_port_is_not_queried_or_unregistered(local_vm, monkeypatch):
    manager, known, _config = local_vm
    register_managed_vm("other-vm", known["hdc_serial"], "other-lease")
    monkeypatch.setattr(mod, "hdc_list_targets", lambda: pytest.fail("B channel queried"))
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: pytest.fail("B port probed"))
    result = manager.observe_instance("observe-vm")
    assert result["state"] == "stopped" and result["resources_released"] is False
    assert result["reason"] == "transport_owned_by_another_vm"
    assert managed_vm_owner(known["hdc_serial"]) == ("other-vm", "other-lease")


@pytest.mark.parametrize("blocked", ["connected", "unknown_hdc", "hdc_error", "port", "fport"])
def test_process_stopped_does_not_mean_its_resources_are_released(local_vm, monkeypatch, blocked):
    manager, known, _config = local_vm
    if blocked in {"connected", "unknown_hdc"}:
        monkeypatch.setattr(mod, "hdc_list_targets", lambda: [SimpleNamespace(serial=known["hdc_serial"], status=blocked)])
    elif blocked == "hdc_error":
        def fail():
            raise PermissionError("fixture HDC query denied")
        monkeypatch.setattr(mod, "hdc_list_targets", fail)
    else:
        known["driver_fport_port"] = 16556
        monkeypatch.setattr(manager, "_port_is_free", lambda port: port != (10007 if blocked == "port" else 16556))
    result = manager.observe_instance("observe-vm")
    assert result["state"] == "stopped" and result["resources_released"] is False


def test_inflight_vm_operation_prevents_observation(local_vm, monkeypatch):
    manager, _known, _config = local_vm
    entered, release = threading.Event(), threading.Event()
    def operation():
        with manager._vm_lock("observe-vm"):
            entered.set()
            assert release.wait(2)
    thread = threading.Thread(target=operation)
    thread.start()
    assert entered.wait(2)
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: pytest.fail("inflight operation inspected"))
    try:
        result = manager.observe_instance("observe-vm")
        assert result["state"] == "unknown" and result["reason"] == "operation_in_progress"
    finally:
        release.set()
        thread.join(2)


def test_elapsed_inspection_budget_cannot_report_a_late_running_fact(local_vm, monkeypatch):
    manager, known, _config = local_vm
    clock = [1.0]
    monkeypatch.setattr(cap, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def slow_inspect(*_args):
        clock[0] = 3.0
        return known["hdc_serial"]
    monkeypatch.setattr(manager, "_active_instance_serial", slow_inspect)
    result = manager.observe_instance("observe-vm", deadline=2.0)
    assert result["state"] == "unknown" and result["reason"] == "observation_deadline"


@pytest.mark.parametrize("budget", [0, -1, "bad", float("inf")])
def test_invalid_inspection_budget_never_scans(local_vm, monkeypatch, budget):
    manager, _known, _config = local_vm
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: pytest.fail("expired probe inspected native process"))
    result = manager.probe({"vm_id": "observe-vm", "inspect_instance": True, "operation_timeout_sec": budget})
    assert result["details"]["instance_observation"]["state"] == "unknown"


async def test_inspect_budget_includes_worker_queue_time(local_vm, monkeypatch):
    manager, _known, _config = local_vm
    clock = [1.0]
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cap, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: pytest.fail("expired queued probe inspected process"))
    async def queued_worker(function, *args, **kwargs):
        clock[0] += 6.0
        return function(*args, **kwargs)
    monkeypatch.setattr(mod.asyncio, "to_thread", queued_worker)
    messages = []
    async def send(message):
        messages.append(message)
    await manager.handle_capability_probe(SimpleNamespace(agent_id="fixture", send=send), {
        "vm_id": "observe-vm", "inspect_instance": True, "request_id": "fresh-probe",
        "_operation_deadline": 999999.0,
    })
    assert messages[0]["request_id"] == "fresh-probe"
    observation = messages[0]["details"]["instance_observation"]
    assert observation["state"] == "unknown" and observation["reason"] == "observation_deadline"


@pytest.mark.parametrize("ready,pending", [(False, False), (False, True), (True, False), (True, True)])
async def test_inventory_observes_before_using_old_flags(local_vm, ready, pending):
    manager, known, config = local_vm
    known.update(ready=ready, cleanup_pending=pending)
    snapshot = deepcopy(manager._known)
    registry_before, data_before = manager.registry_path.read_bytes(), config.read_bytes()
    messages = []
    async def send(message):
        messages.append(message)
    assert await manager.report_orphan_reconcile(SimpleNamespace(agent_id="fixture", send=send)) == 1
    row = messages[0]["instances"][0]
    assert row["state"] == "stopped" and row["details"]["cleanup_confirmed"] is True
    assert row["details"]["instance_observation"]["state"] == "stopped"
    assert messages[0]["unknown_vm_ids"] == []
    assert manager._known == snapshot
    assert manager.registry_path.read_bytes() == registry_before and config.read_bytes() == data_before


@pytest.mark.parametrize("observed", ["running", "unknown"])
async def test_inventory_preserves_live_or_unknown_instance_despite_cleanup_flag(local_vm, monkeypatch, observed):
    manager, known, _config = local_vm
    known.update(ready=False, cleanup_pending=True)
    def inspect(_attrs):
        if observed == "unknown":
            raise PermissionError("fixture")
        return iter([native_process(known)])
    monkeypatch.setattr(psutil, "process_iter", inspect)
    messages = []
    async def send(message):
        messages.append(message)
    await manager.report_orphan_reconcile(SimpleNamespace(agent_id="fixture", send=send))
    row = messages[0]["instances"][0]
    assert row["state"] == ("running" if observed == "running" else "error")
    assert row["details"]["instance_observation"]["state"] == observed
    assert row["details"]["cleanup_confirmed"] is False
    assert messages[0]["unknown_vm_ids"] == (["observe-vm"] if observed == "unknown" else [])


@pytest.mark.parametrize("action", ["start", "stop", "delete"])
async def test_failure_reports_current_facts_instead_of_old_saved_exit_evidence(local_vm, monkeypatch, action):
    manager, known, _config = local_vm
    known["exit_evidence"] = {"instance_process_status": "absent", "process_return_code": 0}
    known["cleanup_confirmed"] = True
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter([native_process(known)]))
    def failure(*_args):
        raise RuntimeError("this operation failed")
    monkeypatch.setattr(manager, f"{action}_sync", failure)
    messages = []
    async def send(message):
        messages.append(message)
    async def refresh():
        pass
    client = SimpleNamespace(agent_id="fixture", send=send, refresh_devices=refresh)
    await getattr(manager, f"handle_{action}")(client, {"vm_id": "observe-vm", "request_id": "new-operation"})
    terminal = messages[-1]
    assert terminal["ok"] is False and terminal["reason"].endswith("failed")
    assert terminal["details"]["instance_observation"]["state"] == "running"
    assert terminal["details"]["instance_observation"]["resources_released"] is False
    assert terminal["request_id"] == "new-operation"


@pytest.mark.parametrize("native_state", ["running", "unknown"])
def test_explicit_start_keeps_pending_record_when_exit_is_not_proven(local_vm, monkeypatch, native_state):
    manager, known, config = local_vm
    known["cleanup_pending"] = True
    before = config.read_bytes()
    monkeypatch.setattr(manager, "_active_instance_serial", lambda *_a: known["hdc_serial"] if native_state == "running" else None)
    with pytest.raises(RuntimeError, match="cleanup_required") as caught:
        manager.start_sync({"vm_id": "observe-vm", "lease_token": "new-lease", "assigned_port": 10008})
    assert caught.value.cleanup_details["instance_observation"]["state"] == native_state
    assert known["cleanup_pending"] is True and config.read_bytes() == before


@pytest.mark.parametrize("native_port,expected", [(10007, "running"), (10009, "unknown")])
def test_clean_bound_start_rechecks_old_native_process_before_using_a_new_port(local_vm, monkeypatch, native_port, expected):
    manager, known, config = local_vm
    known.update(cleanup_pending=False, cleanup_confirmed=True, ready=False)
    manager._save_registry()
    before, registry_before = config.read_bytes(), manager.registry_path.read_bytes()
    process = native_process({**known, "hdc_port": native_port})
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter([process]))
    # The newly assigned port is free. It says nothing about whether this same
    # named instance is already running at its old/different HDC address.
    monkeypatch.setattr(manager, "_port_conflict", lambda *_a: "")
    assert manager._runtimes == {}
    with pytest.raises(RuntimeError, match="cleanup_required") as caught:
        manager.start_sync({"vm_id": "observe-vm", "lease_token": "new-lease", "assigned_port": 10008,
                            "require_existing_instance": True, "rollback_new_instance": False})
    assert caught.value.cleanup_details["instance_observation"]["state"] == expected
    assert manager._runtimes == {} and managed_vm_owner("127.0.0.1:10008") is None
    assert config.read_bytes() == before and manager.registry_path.read_bytes() == registry_before


@pytest.mark.parametrize("recorded", [False, True])
def test_bound_start_never_recreates_missing_or_unowned_definition(local_vm, recorded):
    manager, known, config = local_vm
    config.unlink()
    config.parent.rmdir()
    if not recorded:
        manager._known.pop("observe-vm")
    with pytest.raises(RuntimeError) as caught:
        manager.start_sync({"vm_id": "observe-vm", "lease_token": "new-lease", "assigned_port": 10008,
                            "require_existing_instance": True, "rollback_new_instance": False})
    observation = caught.value.cleanup_details["instance_observation"]
    assert observation["state"] == ("missing" if recorded else "unknown")
    assert not config.parent.exists() and manager._runtimes == {}


@pytest.mark.parametrize("protection", ["rollback_pending", "first_bind"])
def test_explicit_start_does_not_adopt_old_first_bind_data(local_vm, protection):
    manager, known, config = local_vm
    known["cleanup_pending"] = True
    known["rollback_pending"] = protection == "rollback_pending"
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match="cleanup_required"):
        manager.start_sync({"vm_id": "observe-vm", "lease_token": "new-lease", "assigned_port": 10008,
                            "rollback_new_instance": protection == "first_bind"})
    assert known["cleanup_pending"] is True and config.read_bytes() == before


@pytest.mark.parametrize("cache_state", ["pending", "stale", "clean"])
@pytest.mark.parametrize("no_window", [False, True])
def test_explicit_bound_start_clears_expired_bookkeeping_and_reuses_exact_definition(local_vm, monkeypatch, cache_state, no_window):
    manager, known, config = local_vm
    known["cleanup_pending"] = cache_state == "pending"
    if cache_state == "stale":
        manager._runtimes["observe-vm"] = manager._runtime_from_known("observe-vm", known, known["hdc_serial"])
    register_managed_vm("observe-vm", known["hdc_serial"], known["lease_token"])
    before = config.read_bytes()
    spawned = []
    monkeypatch.setattr(manager, "probe", lambda _msg: {"ok": True, "details": {"tools": {"emulator": "/fake/Emulator"}, "no_window_supported": no_window}})
    monkeypatch.setattr(manager, "_resolve_image_root", lambda _msg: "")
    monkeypatch.setattr(manager, "_port_conflict", lambda *_a: "")
    monkeypatch.setattr(mod, "_spawn_harmony_emulator", lambda args, *_a, **_k: spawned.append(args) or SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(manager, "_wait_hdc", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "_wait_driver", lambda *_a, **_k: SimpleNamespace(get_raw_driver=lambda: SimpleNamespace(_client=SimpleNamespace(local_port=16556))))
    monkeypatch.setattr(manager, "_apply_fold_state", lambda *_a: None)
    result = manager.start_sync({"vm_id": "observe-vm", "lease_token": "new-lease", "assigned_port": 10008,
                                 "require_existing_instance": True, "rollback_new_instance": False})
    assert len(spawned) == 1 and spawned[0][1:3] == ["-start", known["instance_name"]]
    assert ("-noWindow" in spawned[0]) is no_window
    assert result["details"]["instance_observation"]["state"] == "running"
    assert result["details"]["cleanup_confirmed"] is False
    assert result["details"]["created_in_attempt"] is False
    assert not manager._known["observe-vm"].get("cleanup_pending")
    assert managed_vm_owner(known["hdc_serial"]) is None
    assert managed_vm_owner("127.0.0.1:10008") == ("observe-vm", "new-lease")
    assert config.read_bytes() == before
