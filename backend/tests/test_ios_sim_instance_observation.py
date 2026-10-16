"""Read-only instance observations and ordinary start/stop, with no native I/O."""
import asyncio
import importlib.util
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from ai_phone.agent.ios_sim import manager as module
from ai_phone.agent.drivers import ios_simulator_wda as wda


_REAL_PROBE = module.IosSimVmManager.probe
_REAL_LIST = module.IosSimVmManager._list_all_instances_strict
spec = importlib.util.spec_from_file_location(
    "ios_observation_fixture", Path(__file__).with_name("test_ios_sim_manager.py")
)
fixture = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(fixture)
env = fixture.env
VM, UDID = fixture._VM, fixture._UDID
msg = fixture._msg
FakeLauncher, Client = fixture.FakeLauncher, fixture.RecordingClient
Manager = module.IosSimVmManager


def existing(env, state="Shutdown"):
    env["instances"][UDID] = {
        "name": module.managed_sim_name(VM), "state": state, "runtime": fixture._RT,
        "device_type": fixture._DT, "is_available": True, "private_data": "keep",
    }


@pytest.mark.parametrize("state,expected,released", [
    ("Booted", "running", False), ("Shutdown", "stopped", True),
    ("Booting", "unknown", False), ("Shutting Down", "unknown", False),
])
def test_observation_reports_guest_fact_without_native_mutation(env, state, expected, released):
    existing(env, state)
    manager = Manager()
    observation = manager.observe_instance(VM)
    assert observation["vm_id"] == VM
    assert observation["state"] == expected
    assert observation["resources_released"] is released
    assert observation["identity"] == {"sim_name": module.managed_sim_name(VM), "udid": UDID}
    assert env["calls"] == [] and FakeLauncher.instances == [] and manager._runtimes == {}


def test_inspect_probe_uses_local_identity_and_never_sdk_candidates(env, monkeypatch):
    existing(env)
    manager = Manager()
    monkeypatch.setattr(Manager, "probe", _REAL_PROBE)
    monkeypatch.setattr(module, "probe_ios_sim_capability", lambda *a, **k: pytest.fail("no SDK probe"))
    result = manager.probe({"inspect_instance": True, "vm_id": VM, "udid": "REQUEST-ONLY-ID",
                            "_operation_deadline": -1, "_monotonic": -1})
    assert result["ok"] is True
    assert result["details"]["instance_observation"]["identity"]["udid"] == UDID
    assert env["calls"] == [] and FakeLauncher.instances == []


def test_missing_inspection_never_echoes_requested_udid(env, monkeypatch):
    monkeypatch.setattr(Manager, "probe", _REAL_PROBE)
    result = Manager().probe({"inspect_instance": True, "vm_id": VM, "udid": "UNVERIFIED"})
    observation = result["details"]["instance_observation"]
    assert observation["state"] == "missing"
    assert observation["identity"]["udid"] == ""
    assert observation["resources_released"] is False
    assert env["calls"] == []


def test_normal_capability_probe_still_uses_existing_sdk_path(env, monkeypatch):
    calls = []
    monkeypatch.setattr(Manager, "probe", _REAL_PROBE)
    monkeypatch.setattr(module, "probe_ios_sim_capability", lambda req, **kw: calls.append(req) or {"ok": True})
    assert Manager().probe({"device_type": "fixture"}) == {"ok": True}
    assert calls == [{"device_type": "fixture"}]


def test_unreadable_inventory_is_unknown_not_stopped_and_has_no_id_proof(env):
    existing(env)
    env["list_fails"] = True
    observation = Manager().observe_instance(VM)
    assert observation["state"] == "unknown"
    assert observation["identity"]["udid"] == ""
    assert observation["resources_released"] is False
    assert observation["reason"].startswith("instance_list_failed")


@pytest.mark.parametrize("raw", ["{}", '{"devices": {"ios": [{"udid": "U", "name": "aiphone_sim_abc123"}]}}'])
def test_incomplete_native_snapshot_is_unknown(env, monkeypatch, raw):
    monkeypatch.setattr(Manager, "_list_all_instances_strict", _REAL_LIST)
    calls = []
    def read(*args, **kwargs):
        calls.append(args)
        assert args == ("list", "devices", "-j")
        return raw
    monkeypatch.setattr(module, "simctl_run", read)
    observation = Manager().observe_instance(VM)
    assert observation["state"] == "unknown"
    assert calls == [("list", "devices", "-j")]


def test_duplicate_native_identity_is_unknown(env):
    existing(env)
    env["instances"]["DUPLICATE"] = dict(env["instances"][UDID])
    observation = Manager().observe_instance(VM)
    assert observation["state"] == "unknown"
    assert observation["identity"]["udid"] == ""


def test_observation_does_not_wait_for_vm_lock(env, monkeypatch):
    manager = Manager()
    entered, release = threading.Event(), threading.Event()
    def hold():
        with manager._vm_lock(VM):
            entered.set()
            assert release.wait(2)
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(1)
        monkeypatch.setattr(manager, "_list_all_instances_strict", lambda **kw: pytest.fail("busy lock must not query"))
        before = time.monotonic()
        observation = manager.observe_instance(VM, before + 0.1)
        assert time.monotonic() - before < 0.1
        assert observation["state"] == "unknown" and observation["reason"] == "operation_in_progress"
    finally:
        release.set()
        worker.join(1)


def test_native_observation_budget_is_checked_after_snapshot(env, monkeypatch):
    existing(env)
    clock = [0.0]
    manager = Manager()
    def delayed(**kwargs):
        assert kwargs["timeout"] == pytest.approx(0.001)
        clock[0] += 0.02
        return dict(env["instances"])
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(manager, "_list_all_instances_strict", delayed)
    observation = manager.observe_instance(VM, deadline=0.001)
    assert observation["state"] == "unknown" and observation["reason"] == "observation_timeout"


@pytest.mark.asyncio
async def test_inspection_executor_queue_consumes_its_local_budget(env, monkeypatch):
    monkeypatch.setattr(Manager, "probe", _REAL_PROBE)
    async def queued(call, *args, **kwargs):
        await asyncio.sleep(0.02)
        return call(*args, **kwargs)
    monkeypatch.setattr(module.asyncio, "to_thread", queued)
    monkeypatch.setattr(Manager, "_list_all_instances_strict", lambda **kw: pytest.fail("already expired"))
    client = Client()
    await Manager().handle_capability_probe(client, {
        "inspect_instance": True, "vm_id": VM, "operation_timeout_sec": 0.001,
        "_operation_deadline": 10**12, "request_id": "inspect-request",
    })
    assert client.sent[-1]["request_id"] == "inspect-request"
    assert client.sent[-1]["ok"] is False
    assert client.sent[-1]["details"]["instance_observation"]["reason"] == "observation_timeout"


def test_shutdown_stale_cleanup_flag_can_start_original_instance_without_shutdown(env):
    manager = Manager()
    manager.start_sync(msg())
    ports = dict(wda._load_reservations())
    env["instances"][UDID].update(state="Shutdown", private_data="keep")
    manager._runtimes[VM].details.update(cleanup_pending=True, cleanup_error="old stop error")
    env["calls"].clear()
    result = manager.start_sync(msg(udid=UDID, require_existing_instance=True))
    assert result["udid"] == UDID
    assert env["instances"][UDID]["private_data"] == "keep"
    assert wda._load_reservations() == ports
    assert not any(call[0] in {"shutdown", "delete", "create"} for call in env["calls"])
    assert result["details"]["instance_observation"]["state"] == "running"


def test_running_unready_cleanup_flag_never_causes_hidden_shutdown_or_restart(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    runtime.ready = False
    runtime.details.update(cleanup_pending=True, cleanup_error="original failure")
    fixture.FakeLauncher.alive_udids.clear()
    env["calls"].clear()
    previous_starts = runtime.launcher.started
    with pytest.raises(module.SimVmStartFailed) as failure:
        manager.start_sync(msg())
    assert failure.value.details["instance_observation"]["state"] == "running"
    assert failure.value.details["cleanup_confirmed"] is False
    assert runtime.launcher.started == previous_starts and runtime.launcher.stopped == 0
    assert env["calls"] == []
    assert runtime.details["cleanup_error"] == "original failure"


def test_unknown_cleanup_flag_never_touches_existing_guest(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    runtime.details["cleanup_pending"] = True
    env["list_fails"] = True
    env["calls"].clear()
    with pytest.raises(module.SimVmStartFailed) as failure:
        manager.start_sync(msg())
    assert failure.value.details["instance_observation"]["state"] == "unknown"
    assert runtime.launcher.stopped == 0 and env["calls"] == []


def test_fresh_agent_running_guest_is_not_implicitly_restarted(env):
    existing(env, "Booted")
    with pytest.raises(module.SimVmStartFailed) as failure:
        Manager().start_sync(msg(udid=UDID, require_existing_instance=True))
    assert failure.value.details["instance_observation"]["state"] == "running"
    assert FakeLauncher.instances == [] and env["calls"] == []


def test_ready_reuse_requires_native_booted_identity(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    env["instances"][UDID]["name"] = "someone-elses-instance"
    env["calls"].clear()
    with pytest.raises(module.SimVmStartFailed) as failure:
        manager.start_sync(msg())
    assert failure.value.details["instance_observation"]["state"] == "unknown"
    assert runtime.launcher.stopped == 0 and env["calls"] == []


def test_ready_reclaimed_instance_reuses_both_ports_without_wda_restart(env):
    original = Manager()
    original.start_sync(msg())
    ports = wda.peek_ports(UDID)
    resumed = Manager()
    resumed.reconcile_running_vms_sync()
    launcher = resumed._runtimes[VM].launcher
    env["calls"].clear()
    result = resumed.start_sync(msg(udid=UDID, require_existing_instance=True))
    assert result["details"]["wda_port"] == ports.wda
    assert result["details"]["mjpeg_port"] == ports.mjpeg
    assert result["details"]["instance_observation"]["state"] == "running"
    assert launcher.started == 0 and launcher.stopped == 0
    assert env["calls"] == []


def test_stopped_guest_does_not_treat_dead_http_as_dead_xcodebuild(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    env["instances"][UDID]["state"] = "Shutdown"
    runtime.launcher._xcodebuild_proc = SimpleNamespace(poll=lambda: None)
    runtime.launcher.is_alive = lambda **kw: pytest.fail("HTTP failure is not process evidence")
    env["calls"].clear()
    observation = manager.observe_instance(VM)
    assert observation["state"] == "stopped"
    assert observation["resources_released"] is False and observation["reason"] == "wda_process_running"
    assert runtime.launcher.stopped == 0 and env["calls"] == []


def test_start_only_cleans_owned_wda_after_confirmed_shutdown(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    env["instances"][UDID]["state"] = "Shutdown"
    runtime.details["cleanup_pending"] = True
    process = SimpleNamespace(poll=lambda: None)
    runtime.launcher._xcodebuild_proc = process
    stopped = []
    def stop_owned():
        stopped.append(runtime.launcher.udid)
        runtime.launcher._xcodebuild_proc = None
    runtime.launcher.stop = stop_owned
    env["calls"].clear()
    assert manager.start_sync(msg(udid=UDID))["udid"] == UDID
    assert stopped == [UDID]
    assert not any(c[0] in {"shutdown", "delete", "create"} for c in env["calls"])


def test_shutdown_owned_wda_cleanup_failure_stays_failed_without_guest_mutation(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    env["instances"][UDID]["state"] = "Shutdown"
    runtime.launcher._xcodebuild_proc = SimpleNamespace(poll=lambda: None)
    calls = []
    def failed_stop():
        calls.append("own-wda-stop")
        raise OSError("own WDA process refused to exit")
    runtime.launcher.stop = failed_stop
    env["calls"].clear()
    with pytest.raises(OSError, match="refused to exit") as failure:
        manager.start_sync(msg(udid=UDID))
    observation = failure.value.details["instance_observation"]
    assert observation["state"] == "stopped" and observation["resources_released"] is False
    assert calls == ["own-wda-stop"] and env["calls"] == []
    assert env["instances"][UDID]["state"] == "Shutdown"


def test_foreign_wda_owner_is_never_cleaned_even_for_shutdown_guest(env):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    env["instances"][UDID]["state"] = "Shutdown"
    runtime.launcher.udid = "ANOTHER-UDID"
    runtime.launcher._xcodebuild_proc = SimpleNamespace(poll=lambda: None)
    env["calls"].clear()
    with pytest.raises(module.InstanceIdentityError):
        manager.start_sync(msg(udid=UDID))
    assert runtime.launcher.stopped == 0 and env["calls"] == []


@pytest.mark.asyncio
async def test_failed_stop_keeps_error_but_reports_confirmed_guest_fact(env, monkeypatch):
    manager = Manager()
    manager.start_sync(msg())
    native = module.simctl_run
    def fail_shutdown(*args, **kwargs):
        if args[0] == "shutdown":
            raise module.SimctlError(list(args), 1, "", "fixture shutdown refused")
        return native(*args, **kwargs)
    monkeypatch.setattr(module, "simctl_run", fail_shutdown)
    client = Client()
    await manager.handle_stop(client, {"vm_id": VM, "udid": UDID, "request_id": "stop-request"})
    result = client.sent[-1]
    assert result["ok"] is False and result["state"] == "error"
    assert "shutdown refused" in result["error"]
    assert result["details"]["instance_observation"]["state"] == "running"
    assert result["details"]["instance_observation"]["resources_released"] is False


@pytest.mark.parametrize("process_udid,released", [(UDID, False), ("OTHER-UDID", True)])
def test_detached_xcodebuild_is_read_only_and_matched_to_native_udid(env, monkeypatch, process_udid, released):
    existing(env)
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter([SimpleNamespace(info={
        "name": "xcodebuild", "cmdline": ["xcodebuild", "-destination", f"platform=iOS Simulator,id={process_udid}"]
    })]))
    observation = Manager().observe_instance(VM)
    assert observation["state"] == "stopped" and observation["resources_released"] is released
    assert FakeLauncher.instances == [] and env["calls"] == []


def test_port_resource_state_does_not_change_confirmed_guest_state(env, monkeypatch):
    manager = Manager()
    manager.start_sync(msg())
    env["instances"][UDID]["state"] = "Shutdown"
    monkeypatch.setattr(Manager, "_local_port_free", staticmethod(lambda _port: False))
    before = dict(wda._load_reservations())
    observation = manager.observe_instance(VM)
    assert observation["state"] == "stopped" and observation["resources_released"] is False
    assert observation["reason"] == "wda_port_in_use"
    assert wda._load_reservations() == before


def test_resource_query_failure_is_separate_from_guest_shutdown(env, monkeypatch):
    existing(env)
    def denied(*args, **kwargs):
        raise PermissionError("fixture process inventory unavailable")
    monkeypatch.setattr(psutil, "process_iter", denied)
    observation = Manager().observe_instance(VM)
    assert observation["state"] == "stopped" and observation["resources_released"] is False
    assert observation["reason"] == "wda_process_unknown"


@pytest.mark.asyncio
async def test_failure_ack_preserves_failure_and_running_observation(env, monkeypatch):
    manager = Manager()
    manager.start_sync(msg())
    env["calls"].clear()
    async def queued(call, *args, **kwargs):
        await asyncio.sleep(0.02)
        return call(*args, **kwargs)
    monkeypatch.setattr(module.asyncio, "to_thread", queued)
    client = Client()
    await manager.handle_start(client, msg(operation_timeout_sec=0.001))
    result = client.sent[-1]
    assert result["ok"] is False and result["state"] == "error"
    assert "deadline exceeded" in result["error"]
    assert result["details"]["cleanup_confirmed"] is False
    assert result["details"]["instance_observation"]["state"] == "running"
    assert result["details"]["instance_observation"]["resources_released"] is False
    assert env["calls"] == []


@pytest.mark.asyncio
async def test_validation_failure_refreshes_fact_instead_of_cached_stopped_claim(env):
    manager = Manager()
    manager.start_sync(msg())
    assert manager._runtimes[VM].details["created_in_attempt"] is True
    manager._runtimes[VM].details.update({
        "cleanup_confirmed": True,
        "instance_observation": {"state": "stopped", "resources_released": True},
    })
    client = Client()
    env["calls"].clear()
    await manager.handle_start(client, msg(operation_timeout_sec=0))
    result = client.sent[-1]
    assert result["ok"] is False and result["state"] == "error"
    assert "positive finite" in result["error"]
    assert result["details"]["instance_observation"]["state"] == "running"
    assert result["details"]["cleanup_confirmed"] is False
    assert result["details"]["created_in_attempt"] is False
    assert env["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,error", [
    ({"udid": "WRONG-UDID"}, "请求 UDID"),
    ({"device_type": fixture._DT + "-Other"}, "配置不一致"),
])
async def test_preflight_failure_never_inherits_previous_creation_proof(env, overrides, error):
    manager = Manager()
    manager.start_sync(msg())
    runtime = manager._runtimes[VM]
    assert runtime.details["created_in_attempt"] is True
    launcher = runtime.launcher
    started = launcher.started
    client = Client()
    env["calls"].clear()

    await manager.handle_start(client, msg(rollback_new_instance=True, **overrides))

    result = client.sent[-1]
    assert result["ok"] is False and result["state"] == "error"
    assert error in result["error"]
    assert result["details"]["created_in_attempt"] is False
    assert result["details"]["cleanup_confirmed"] is False
    observation = result["details"]["instance_observation"]
    assert observation["state"] == "running" and observation["identity"]["udid"] == UDID
    assert manager._runtimes[VM] is runtime and runtime.details["created_in_attempt"] is True
    assert launcher.started == started and launcher.stopped == 0
    assert env["calls"] == [] and env["instances"][UDID]["state"] == "Booted"


def test_bound_missing_instance_is_not_recreated(env):
    with pytest.raises(RuntimeError, match="已缺失") as failure:
        Manager().start_sync(msg(udid=UDID, require_existing_instance=True))
    assert failure.value.details["instance_observation"]["state"] == "missing"
    assert failure.value.details["instance_observation"]["identity"]["udid"] == ""
    assert env["calls"] == []
