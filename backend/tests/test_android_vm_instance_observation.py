"""Explicit VM actions inspect current local facts; never touch host SDKs, services or devices."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.android_vm import manager as mod


@pytest.fixture
def local(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native/SDK operation forbidden in observation tests")

    root = tmp_path / "avds"
    root.mkdir()
    monkeypatch.setenv("ANDROID_AVD_HOME", str(root))
    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod, "probe_android_vm_capability", forbidden)
    monkeypatch.setattr(mod, "find_android_tools", lambda: (None, ["isolated"]))
    monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], True))
    monkeypatch.setattr(mod, "_is_port_free", lambda _: True)
    manager = mod.AndroidVmManager(runtime_dir=tmp_path / "runtime")
    return SimpleNamespace(manager=manager, root=root)


def avd(local, vm_id="a"):
    directory = local.root / f"aiphone_vm_{vm_id}.avd"
    directory.mkdir()
    (directory / "userdata.img").write_bytes(b"old data")
    return directory


def runtime(local, *, alive=False, ready=False, unavailable=False, vm_id="a"):
    proc = SimpleNamespace(poll=lambda: None if alive else 0)
    rt = mod.VmRuntime(vm_id, vm_id, "emulator-5554", 5554, proc, 123,
                       avd_name=f"aiphone_vm_{vm_id}", ready=ready,
                       transport_unavailable=unavailable, observed_request_id="old-request")
    local.manager._runtimes[vm_id] = rt
    return rt


def start_setup(local, monkeypatch):
    tools = mod.AndroidVmTools("/isolated/adb", "/isolated/emulator", "/isolated/avdmanager")
    monkeypatch.setattr(mod, "find_android_tools", lambda: (tools, []))
    monkeypatch.setattr(local.manager, "probe", lambda _: {"ok": True})
    ensure = Mock(return_value=False)
    popen = Mock(return_value=SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(local.manager, "_ensure_avd", ensure)
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(local.manager, "_wait_boot_completed", lambda *_: None)
    monkeypatch.setattr(local.manager, "_provision_device", lambda *_: None)
    monkeypatch.setattr(local.manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a" if popen.called else "")
    monkeypatch.setattr(local.manager, "stop_sync", Mock(side_effect=AssertionError("must not implicitly stop")))
    monkeypatch.setattr(local.manager, "delete_sync", Mock(side_effect=AssertionError("must not implicitly delete")))
    return ensure, popen


def test_observation_uses_current_process_fact_without_mutating_runtime(local):
    rt = runtime(local, alive=True)
    old = {"confirmed": True, "reason": "old-exit"}
    rt.exit_evidence = old
    result = local.manager.observe_instance("a")
    assert result == {"vm_id": "a", "state": "running", "identity": {"avd_name": "aiphone_vm_a"},
                      "resources_released": False, "reason": "owned_process_running"}
    assert rt.exit_evidence is old
    assert not rt.ready and rt.observed_request_id == "old-request"


@pytest.mark.parametrize("blocked", [False, True])
def test_no_owned_process_means_stopped_even_if_port_is_occupied(local, monkeypatch, blocked):
    avd(local)
    runtime(local)
    monkeypatch.setattr(mod, "_is_port_free", lambda port: not (blocked and port == 5554))
    result = local.manager.observe_instance("a")
    assert result["state"] == "stopped"
    assert result["resources_released"] is not blocked
    assert result["reason"] == ("port_unavailable" if blocked else "exit_confirmed")


def test_complete_native_inventory_can_prove_missing_but_cache_cannot(local):
    local.manager._created_vm_ids.add("a")
    runtime(local)
    result = local.manager.observe_instance("a")
    assert result["state"] == "missing"
    assert result["identity"] == {"avd_name": ""}
    assert "a" in local.manager._created_vm_ids and "a" in local.manager._runtimes


def test_other_vm_or_name_prefix_is_not_target_presence(local):
    avd(local, "a_suffix")
    assert local.manager.observe_instance("a")["state"] == "missing"


@pytest.mark.parametrize("failure", ["process_inventory", "directory_denied", "root_missing", "invalid_index", "broken_directory"])
def test_unknown_inventory_never_becomes_missing(local, monkeypatch, failure):
    if failure == "process_inventory":
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    elif failure == "directory_denied":
        monkeypatch.setattr(mod.os, "scandir", Mock(side_effect=PermissionError("isolated denied")))
    elif failure == "root_missing":
        local.root.rmdir()
    elif failure == "invalid_index":
        (local.root / "aiphone_vm_a.ini").write_text("path=\n")
    else:
        (local.root / "aiphone_vm_a.avd").symlink_to(local.root / "absent", target_is_directory=True)
    result = local.manager.observe_instance("a")
    assert result["state"] == "unknown"
    assert result["identity"] == {"avd_name": ""}
    assert result["resources_released"] is False


def test_native_index_custom_directory_is_read_without_rewriting(local, tmp_path):
    custom = tmp_path / "actual-avd"
    custom.mkdir()
    index = local.root / "aiphone_vm_a.ini"
    index.write_text(f"path={custom}\n")
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    result = local.manager.observe_instance("a")
    assert result["state"] == "stopped"
    assert result["identity"] == {"avd_name": "aiphone_vm_a"}
    assert result["resources_released"] is False  # no local port evidence after an Agent restart
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("trusted", [True, False])
def test_directory_unknown_does_not_erase_proven_exit_of_trusted_local_runtime(local, monkeypatch, trusted):
    rt = runtime(local)
    if not trusted:
        rt.process = None  # a stop placeholder alone is not native identity proof
    monkeypatch.setattr(mod.os, "scandir", Mock(side_effect=PermissionError("isolated denied")))
    result = local.manager.observe_instance("a")
    assert result["state"] == ("stopped" if trusted else "unknown")
    assert result["resources_released"] is trusted
    if trusted:
        assert result["reason"] == "native_definition_unverified"
    assert local.manager._runtimes["a"] is rt


def test_native_creation_identity_survives_runtime_removal_without_inventing_port_proof(local, monkeypatch):
    local.manager._created_vm_ids.add("a")
    monkeypatch.setattr(mod.os, "scandir", Mock(side_effect=PermissionError("isolated denied")))
    result = local.manager.observe_instance("a")
    assert result["state"] == "stopped"
    assert result["identity"] == {"avd_name": "aiphone_vm_a"}
    assert result["reason"] == "native_definition_unverified"
    assert result["resources_released"] is False


def test_lock_busy_observation_returns_immediately_without_scanning(local, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def own_lock():
        with local.manager._vm_lock("a"):
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=own_lock)
    worker.start()
    assert entered.wait(2)
    scan = Mock(side_effect=AssertionError("must not scan under another operation"))
    monkeypatch.setattr(local.manager, "_runtime_exit_confirmed", scan)
    try:
        assert local.manager.observe_instance("a")["reason"] == "lifecycle_busy"
        scan.assert_not_called()
    finally:
        release.set()
        worker.join(2)


def test_expired_observation_has_no_native_or_filesystem_query(local, monkeypatch):
    scan = Mock(side_effect=AssertionError("expired inspection must not scan"))
    monkeypatch.setattr(local.manager, "_runtime_exit_confirmed", scan)
    assert local.manager.observe_instance("a", deadline=mod.time.monotonic() - 1)["reason"] == "operation_timeout"
    scan.assert_not_called()


def test_inspect_probe_ignores_incoming_state_identity_and_old_proof(local):
    avd(local)
    result = local.manager.probe({
        "inspect_instance": True, "vm_id": "a", "state": "running", "adb_serial": "emulator-5554",
        "identity": {"avd_name": "unrelated"}, "details": {"instance_observation": {"state": "running"}},
    })
    assert result["ok"] is True
    assert result["details"]["instance_observation"]["state"] == "stopped"
    assert result["details"]["instance_observation"]["identity"] == {"avd_name": "aiphone_vm_a"}


class Client:
    agent_id = "isolated-agent"

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def refresh_devices(self):
        pass


async def test_inspect_budget_includes_executor_queue_and_ignores_remote_monotonic(local, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])

    async def queued(fn, message):
        clock[0] += 0.1
        return fn(message)

    monkeypatch.setattr(asyncio, "to_thread", queued)
    scan = Mock(side_effect=AssertionError("queued deadline expired"))
    monkeypatch.setattr(local.manager, "_runtime_exit_confirmed", scan)
    client = Client()
    await local.manager.handle_capability_probe(client, {
        "vm_id": "a", "inspect_instance": True, "request_id": "inspect-current",
        "operation_timeout_sec": 0.01, "_operation_deadline": 1e10,
    })
    assert client.sent[0]["request_id"] == "inspect-current"
    assert client.sent[0]["details"]["instance_observation"]["reason"] == "operation_timeout"
    scan.assert_not_called()


@pytest.mark.parametrize("ready,unavailable", [(False, False), (True, True), (True, False)])
def test_next_explicit_start_clears_exited_old_flags_and_starts_original_avd_once(local, monkeypatch, ready, unavailable):
    directory = avd(local)
    old = runtime(local, ready=ready, unavailable=unavailable)
    local.manager._created_vm_ids.add("a")
    ensure, popen = start_setup(local, monkeypatch)
    result = local.manager.start_sync({"vm_id": "a", "request_id": "new-start"})
    try:
        assert result["adb_serial"] == old.adb_serial
        assert result["details"]["created_in_attempt"] is False
        assert ensure.call_count == popen.call_count == 1
        assert ensure.call_args.kwargs["require_existing"] is True
        assert ensure.call_args.kwargs["create_only"] is False
        assert local.manager._runtimes["a"] is not old
        assert local.manager._runtimes["a"].observed_request_id == "new-start"
        assert "a" in local.manager._created_vm_ids
        assert (directory / "userdata.img").read_bytes() == b"old data"
    finally:
        local.manager._runtimes["a"].log_file.close()


@pytest.mark.parametrize("state,ready", [("running", False), ("unknown", False), ("missing", False), ("unknown", True)])
def test_live_unknown_or_missing_old_instance_does_not_implicitly_stop_or_create(local, monkeypatch, state, ready):
    if state != "missing":
        avd(local)
    old = runtime(local, alive=state == "running", ready=ready)
    if state == "unknown":
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    ensure, popen = start_setup(local, monkeypatch)
    with pytest.raises(mod.VmStartFailure) as error:
        local.manager.start_sync({"vm_id": "a", "request_id": "new-start"})
    assert error.value.instance_observation["state"] == state
    if state != "missing":
        assert local.manager._runtimes["a"] is old
    assert old.observed_request_id == "old-request"
    ensure.assert_not_called()
    popen.assert_not_called()
    local.manager.stop_sync.assert_not_called()
    local.manager.delete_sync.assert_not_called()


def test_blocked_port_after_old_process_exit_is_stopped_failure_not_running(local, monkeypatch):
    avd(local)
    runtime(local)
    ensure, popen = start_setup(local, monkeypatch)
    monkeypatch.setattr(mod, "_is_port_free", lambda port: port != 5554)
    with pytest.raises(mod.VmStartFailure, match="端口") as error:
        local.manager.start_sync({"vm_id": "a"})
    assert error.value.instance_observation["state"] == "stopped"
    assert error.value.instance_observation["resources_released"] is False
    ensure.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("state", ["running", "unknown", "missing"])
def test_bound_start_without_runtime_requires_current_stopped_fact_before_any_write(local, monkeypatch, state):
    if state != "missing":
        avd(local)
    if state == "running":
        # A Server inspection may be stale: this exact old AVD is now alive on another port.
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([(SimpleNamespace(pid=700), 5556)], True))
    elif state == "unknown":
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    ensure, popen = start_setup(local, monkeypatch)
    choose = Mock(side_effect=AssertionError("do not reserve a new port before proving old guest stopped"))
    monkeypatch.setattr(local.manager, "_choose_port", choose)
    with pytest.raises(mod.VmStartFailure) as error:
        local.manager.start_sync({"vm_id": "a", "require_existing_instance": True, "assigned_port": 5554})
    assert error.value.instance_observation["state"] == state
    assert local.manager._runtimes == {}
    ensure.assert_not_called()
    popen.assert_not_called()
    choose.assert_not_called()
    local.manager.stop_sync.assert_not_called()
    local.manager.delete_sync.assert_not_called()


def test_bound_start_without_runtime_starts_confirmed_stopped_original_avd_once(local, monkeypatch):
    directory = avd(local)
    ensure, popen = start_setup(local, monkeypatch)
    result = local.manager.start_sync({"vm_id": "a", "require_existing_instance": True, "assigned_port": 5554})
    try:
        assert result["adb_serial"] == "emulator-5554"
        assert ensure.call_count == popen.call_count == 1
        assert ensure.call_args.kwargs["require_existing"] is True
        assert ensure.call_args.kwargs["create_only"] is False
        assert (directory / "userdata.img").read_bytes() == b"old data"
        local.manager.stop_sync.assert_not_called()
        local.manager.delete_sync.assert_not_called()
    finally:
        local.manager._runtimes["a"].log_file.close()


async def test_preflight_failure_reports_stopped_fact_for_this_request(local, monkeypatch):
    avd(local)
    local.manager._created_vm_ids.add("a")
    monkeypatch.setattr(local.manager, "probe", lambda _: {"ok": False, "reason": "isolated image unavailable"})
    client = Client()
    await local.manager._start_and_report(client, {"vm_id": "a", "request_id": "new-failed-start", "require_existing_instance": True})
    msg = client.sent[-1]
    assert msg["ok"] is False and msg["reason"] == "start_failed"
    assert msg["request_id"] == "new-failed-start"
    assert msg["details"]["instance_observation"]["state"] == "stopped"
    assert msg["details"]["instance_created"] is True


def test_inspection_flag_cannot_turn_start_into_a_capability_bypass(local, monkeypatch):
    avd(local)
    capability = Mock(return_value={"ok": False, "reason": "missing SDK image"})
    monkeypatch.setattr(mod, "probe_android_vm_capability", capability)
    with pytest.raises(mod.VmStartFailure, match="missing SDK image"):
        local.manager.start_sync({"vm_id": "a", "inspect_instance": True})
    assert capability.call_count == 1
    assert "inspect_instance" not in capability.call_args.args[0]


def test_stop_failure_observes_current_live_instance_not_previous_success(local):
    avd(local)
    rt = runtime(local, alive=True, ready=True)
    rt.exit_evidence = {"confirmed": True, "reason": "old-operation"}
    result = local.manager.stop_sync("a", request_id="current-stop")
    assert result["ok"] is False
    assert result["details"]["instance_observation"]["state"] == "running"
    assert result["details"]["exit_evidence"]["confirmed"] is False
    assert result["details"]["exit_evidence"]["reason"] == "owned_process_running"
    assert local.manager._runtimes["a"] is rt


def test_stop_deadline_failure_drops_old_exit_proof_without_new_query(local, monkeypatch):
    rt = runtime(local)
    rt.exit_evidence = {"confirmed": True, "reason": "old-operation"}
    scan = Mock(side_effect=AssertionError("expired stop must not inspect"))
    monkeypatch.setattr(local.manager, "_runtime_exit_confirmed", scan)
    result = local.manager.stop_sync("a", deadline=mod.time.monotonic() - 1)
    assert result["ok"] is False
    assert result["details"]["exit_evidence"] == {}
    assert result["details"]["instance_observation"]["state"] == "unknown"
    scan.assert_not_called()


@pytest.mark.parametrize("blocker", ["inventory", "port"])
def test_stop_failure_keeps_fresh_exit_diagnostics_instead_of_previous_proof(local, monkeypatch, blocker):
    avd(local)
    rt = runtime(local)
    rt.exit_evidence = {"confirmed": True, "reason": "previous-operation"}
    if blocker == "inventory":
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    else:
        monkeypatch.setattr(mod, "_is_port_free", lambda port: port != 5555)
    result = local.manager.stop_sync("a", request_id="fresh-stop")
    assert result["ok"] is False
    evidence = result["details"]["exit_evidence"]
    assert evidence["confirmed"] is False
    if blocker == "inventory":
        assert evidence["reason"] == "process_inventory_incomplete"
        assert evidence["process_inventory_complete"] is False
        assert result["details"]["instance_observation"]["state"] == "unknown"
    else:
        assert evidence["reason"] == "port_unavailable"
        assert evidence["blocked_ports"] == [5555]
        assert evidence["matching_process_ids"] == []
        assert result["details"]["instance_observation"]["state"] == "stopped"


def test_successful_stop_uses_current_exit_fact_without_another_directory_scan(local, monkeypatch):
    runtime(local)
    local.root.rmdir()
    scan = Mock(side_effect=AssertionError("successful stop must not add another scan"))
    monkeypatch.setattr(local.manager, "_observe_instance_sample", scan)
    result = local.manager.stop_sync("a")
    assert result["ok"] is True
    assert result["details"]["instance_observation"] == {
        "vm_id": "a", "state": "stopped", "identity": {"avd_name": "aiphone_vm_a"},
        "resources_released": True, "reason": "exit_confirmed",
    }
    assert result["details"]["exit_evidence"]["confirmed"] is True
    assert "a" not in local.manager._runtimes
    scan.assert_not_called()


def test_preflight_exhausted_budget_does_not_add_a_fact_query(local, monkeypatch):
    avd(local)
    clock = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])

    def failed_preflight(_requirement):
        clock[0] = 102.0
        return {"ok": False, "reason": "preflight timed out"}

    monkeypatch.setattr(local.manager, "probe", failed_preflight)
    scan = Mock(side_effect=AssertionError("must not scan after preflight used the budget"))
    monkeypatch.setattr(local.manager, "_runtime_exit_confirmed", scan)
    with pytest.raises(mod.VmStartFailure) as error:
        local.manager.start_sync({"vm_id": "a", "operation_timeout_sec": 1.0})
    assert error.value.instance_observation["reason"] == "operation_timeout"
    assert error.value.exit_evidence == {}
    scan.assert_not_called()


def test_first_binding_does_not_adopt_stopped_preexisting_runtime(local):
    avd(local)
    old = runtime(local)
    with pytest.raises(mod.VmStartFailure) as error:
        local.manager.start_sync({"vm_id": "a", "rollback_new_instance": True})
    assert error.value.instance_preexisting is True
    assert error.value.created_in_attempt is False
    assert local.manager._runtimes["a"] is old
