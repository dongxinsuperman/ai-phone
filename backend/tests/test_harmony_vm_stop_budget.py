"""One Stop budget across native cleanup; only fake processes/HDC and tmp files."""
from __future__ import annotations

from types import SimpleNamespace
import subprocess
import time

import psutil
import pytest

from ai_phone.agent.drivers import hdc as hdc_module
from ai_phone.agent.harmony_vm import capability as cap
from ai_phone.agent.harmony_vm import manager as mod
from ai_phone.agent.harmony_vm.registry import (
    managed_vm_owner, register_managed_serial, register_managed_vm,
    unregister_managed_serial, unregister_managed_vm,
)


@pytest.fixture
def stop_env(tmp_path, monkeypatch):
    state = SimpleNamespace(now=100.0, delays={}, events=[], native_exits=True, wait_timeout=False)
    clock = SimpleNamespace(monotonic=lambda: state.now, time=time.time,
                            sleep=lambda seconds: setattr(state, "now", state.now + seconds))
    monkeypatch.setattr(mod, "time", clock)
    monkeypatch.setattr(cap, "time", clock)
    monkeypatch.setattr(hdc_module, "time", clock)

    def event(name, timeout=None):
        state.events.append((name, state.now, timeout))
        state.now += state.delays.get(name, 0.0)

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            event("terminate")

        def kill(self):
            event("kill")
            self.returncode = 0

        def wait(self, timeout):
            event("wait", timeout)
            if state.wait_timeout:
                raise subprocess.TimeoutExpired("fixture-emulator", timeout)
            self.returncode = 0
            return 0

    manager = mod.HarmonyVmManager(runtime_dir=tmp_path)
    runtime = mod.HarmonyVmRuntime(
        vm_id="budget-vm", name="budget-vm", instance_name="aiphone_harmony_budget_vm",
        instance_path=str(manager.instance_path), image_root="", hdc_port=12345,
        hdc_serial="127.0.0.1:12345", lease_token="old-budget-lease", ready=True,
        process=Process(), driver_fport_port=16556,
        driver=SimpleNamespace(close=lambda: event("driver_close")),
    )
    config = manager.instance_path / runtime.instance_name / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("owned-private-data")
    manager._runtimes[runtime.vm_id] = runtime
    manager._known[runtime.vm_id] = {**runtime.persistent_dict(), "created": True}
    manager._save_registry()
    register_managed_vm(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    register_managed_serial(runtime.hdc_serial, runtime.driver_fport_port)
    manager._drop_driver_cache = lambda _identity: event("cache_close")

    def tools(**_kwargs):
        event("find_tools")
        return SimpleNamespace(emulator="/fake/Emulator"), []

    def native(args, **kwargs):
        assert args == ["/fake/Emulator", "-stop", runtime.instance_name]
        event("emulator_stop", kwargs["timeout"])
        if state.native_exits:
            runtime.process.returncode = 0
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def hdc(*args, **kwargs):
        if args == ("fport", "ls"):
            event("fport_list", kwargs["timeout"])
            return "tcp:16556 tcp:8012"
        if args[:2] == ("fport", "rm"):
            assert kwargs["serial"] == runtime.hdc_serial
            event("fport_remove", kwargs["timeout"])
            return ""
        assert args == ("tconn", runtime.hdc_serial, "-remove")
        event("hdc_remove", kwargs["timeout"])
        return ""

    def targets(**kwargs):
        if kwargs:
            assert kwargs["strict"] is True
        event("targets")
        return []

    def processes(_attrs):
        event("process_scan")
        return iter(())

    monkeypatch.setattr(mod, "find_harmony_tools", tools)
    monkeypatch.setattr(mod.subprocess, "run", native)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Stop spawned a process"))
    monkeypatch.setattr(mod, "hdc_run", hdc)
    monkeypatch.setattr(mod, "hdc_list_targets", targets)
    monkeypatch.setattr(psutil, "process_iter", processes)
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    yield manager, runtime, state, config, event
    for vm_id, token in [(runtime.vm_id, runtime.lease_token), ("other-vm", "other-lease")]:
        unregister_managed_vm(vm_id, token)
    unregister_managed_serial(runtime.hdc_serial)


def assert_timeout_preserved(manager, runtime, result, config):
    assert result["ok"] is False and result["reason"] == "operation_timeout"
    assert result["details"]["cleanup_confirmed"] is False
    assert result["details"]["instance_observation"]["resources_released"] is False
    assert manager._runtimes[runtime.vm_id] is runtime
    assert manager._known[runtime.vm_id]["lease_token"] == runtime.lease_token
    assert managed_vm_owner(runtime.hdc_serial) == (runtime.vm_id, runtime.lease_token)
    assert config.read_text() == "owned-private-data"


@pytest.mark.parametrize("phase", ["cache_close", "driver_close", "fport_list", "fport_remove", "find_tools", "emulator_stop", "hdc_remove", "targets", "process_scan"])
def test_expired_step_never_begins_the_next_cleanup_action(stop_env, phase):
    manager, runtime, state, config, _event = stop_env
    state.delays[phase] = 2.0
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert_timeout_preserved(manager, runtime, result, config)
    assert state.events[-1][0] == phase
    assert all(started < 101.0 for _name, started, _timeout in state.events)


@pytest.mark.parametrize("phase", ["terminate", "wait"])
def test_expiry_during_process_wait_never_forces_a_late_kill(stop_env, phase):
    manager, runtime, state, config, _event = stop_env
    state.native_exits = False
    state.wait_timeout = True
    state.delays[phase] = 2.0
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert_timeout_preserved(manager, runtime, result, config)
    assert "kill" not in [name for name, *_ in state.events]
    assert state.events[-1][0] == phase


@pytest.mark.parametrize("lock_name", ["vm", "start"])
def test_lock_delay_consumes_original_budget_before_any_mutation(stop_env, monkeypatch, lock_name):
    manager, runtime, state, config, _event = stop_env
    waits = []
    class Lock:
        def acquire(self, **kwargs):
            waits.append(kwargs["timeout"])
            state.now += 2.0
            return True
        def release(self):
            pass
    if lock_name == "vm":
        monkeypatch.setattr(manager, "_vm_lock", lambda _vm: Lock())
    else:
        monkeypatch.setattr(manager, "_start_lock", Lock())
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert_timeout_preserved(manager, runtime, result, config)
    assert waits == [1.0] and state.events == [] and runtime.ready is True


def test_remaining_timeout_is_passed_to_every_native_wait(stop_env):
    manager, runtime, state, config, _event = stop_env
    state.delays.update({name: 0.2 for name in ["cache_close", "driver_close", "fport_list", "fport_remove", "find_tools", "emulator_stop", "hdc_remove"]})
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=105.0)
    assert result["ok"] is True and result["details"]["cleanup_confirmed"] is True
    native = [(name, at, timeout) for name, at, timeout in state.events if timeout is not None]
    assert native and all(0 < timeout <= 105.0 - at + 1e-9 for _name, at, timeout in native)
    assert next(timeout for name, _at, timeout in native if name == "emulator_stop") == pytest.approx(4.0)
    assert runtime.vm_id not in manager._runtimes and managed_vm_owner(runtime.hdc_serial) is None
    assert manager._known[runtime.vm_id]["lease_token"] == runtime.lease_token
    assert config.read_text() == "owned-private-data"


def test_expired_stop_and_foreign_owner_do_not_touch_other_vm(stop_env):
    manager, runtime, state, _config, _event = stop_env
    register_managed_vm("other-vm", runtime.hdc_serial, "other-lease")
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert result["ok"] is False and result["reason"] == "hdc_owned_by_another_vm"
    assert state.events == [] and runtime.ready is True
    assert manager._runtimes[runtime.vm_id] is runtime
    assert managed_vm_owner(runtime.hdc_serial) == ("other-vm", "other-lease")


@pytest.mark.parametrize("phase", ["progress", "executor"])
async def test_handler_keeps_deadline_from_before_progress_and_worker_queue(stop_env, monkeypatch, phase):
    manager, runtime, state, config, _event = stop_env
    reports, refreshed = [], []
    async def send(payload):
        reports.append(payload)
        if phase == "progress" and payload.get("state") == "stopping":
            state.now += 2.0
    async def queued_worker(function, *args, **kwargs):
        if phase == "executor":
            state.now += 2.0
        return function(*args, **kwargs)
    async def refresh(_client):
        refreshed.append(True)
    monkeypatch.setattr(mod.asyncio, "to_thread", queued_worker)
    monkeypatch.setattr(manager, "_refresh_devices_safe", refresh)
    monkeypatch.setattr(manager, "observe_instance", lambda *_a, **_k: pytest.fail("expired Stop reopened inspection"))
    await manager.handle_stop(SimpleNamespace(send=send), {
        "vm_id": runtime.vm_id, "hdc_serial": runtime.hdc_serial, "lease_token": runtime.lease_token,
        "operation_timeout_sec": 1, "_operation_deadline": 9999999,
    })
    assert_timeout_preserved(manager, runtime, reports[-1], config)
    assert state.events == [] and refreshed == []


def test_old_direct_stop_without_deadline_keeps_success_contract(stop_env):
    manager, runtime, state, _config, _event = stop_env
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    assert result["ok"] is True
    assert next(timeout for name, _at, timeout in state.events if name == "emulator_stop") == 20.0


@pytest.mark.parametrize("action", ["stop", "delete", "start_rollback"])
def test_legacy_opaque_driver_timeout_keeps_single_cleanup_attempt(stop_env, monkeypatch, action):
    manager, runtime, state, config, event = stop_env
    def opaque_timeout():
        event("driver_close")
        raise TimeoutError("opaque client close timeout, not operation deadline")
    runtime.driver = SimpleNamespace(close=opaque_timeout)
    run_stop = mod.subprocess.run
    def native(args, **kwargs):
        if args[1] == "-delete":
            event("emulator_delete", kwargs["timeout"])
            config.unlink()
            config.parent.rmdir()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return run_stop(args, **kwargs)
    monkeypatch.setattr(mod.subprocess, "run", native)
    if action == "delete":
        result = manager.delete_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
        assert result["ok"] is True and not config.exists()
    elif action == "start_rollback":
        result = manager._cleanup_failed_start(runtime, rollback_new_definition=False)
        assert result["cleanup_confirmed"] is True and config.is_file()
    else:
        result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
        assert result["ok"] is True and config.is_file()
    assert [name for name, *_ in state.events].count("emulator_stop") == 1
    assert runtime.vm_id not in manager._runtimes


def test_opaque_timeout_before_remaining_deadline_is_not_a_budget_expiry(stop_env):
    manager, runtime, _state, _config, event = stop_env
    def opaque_timeout():
        event("driver_close")
        raise TimeoutError("driver timeout while operation still has time")
    runtime.driver = SimpleNamespace(close=opaque_timeout)
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=105.0)
    assert result["ok"] is True


def test_hdc_failed_query_is_not_treated_as_absent_transport(stop_env, monkeypatch):
    manager, runtime, state, config, _event = stop_env
    def unavailable(**kwargs):
        assert kwargs["strict"] is True
        raise hdc_module.HdcError(["fixture"], 1, "", "query denied")
    monkeypatch.setattr(mod, "hdc_list_targets", unavailable)
    monkeypatch.setattr(manager, "_port_is_free", lambda _p: pytest.fail("failed query is not absence"))
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert_timeout_preserved(manager, runtime, result, config)


def test_registry_identity_scan_cannot_expire_then_begin_native_stop(stop_env):
    manager, runtime, state, config, _event = stop_env
    manager._runtimes.pop(runtime.vm_id)
    state.delays["process_scan"] = 2.0
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert result["reason"] == "operation_timeout" and result["details"]["cleanup_confirmed"] is False
    assert [name for name, *_ in state.events] == ["process_scan"]
    assert manager._known[runtime.vm_id]["lease_token"] == runtime.lease_token
    assert managed_vm_owner(runtime.hdc_serial) == (runtime.vm_id, runtime.lease_token)
    assert config.read_text() == "owned-private-data"


@pytest.mark.parametrize("phase", ["crash_terminate", "crash_wait"])
def test_exact_crash_helper_cannot_be_killed_after_budget_expires(stop_env, monkeypatch, phase):
    manager, runtime, state, config, event = stop_env
    state.delays[phase] = 2.0
    class Crash:
        pid = 101
        info = {"name": "emulator-crash-service", "cmdline": [
            "/fake/emulator-crash-service", "-socket", runtime.instance_name + "_crash",
            "-data-dir", str(config.parent),
        ]}
        def terminate(self):
            event("crash_terminate")
        def wait(self, timeout):
            event("crash_wait", timeout)
            raise psutil.TimeoutExpired(timeout, pid=self.pid)
        def kill(self):
            pytest.fail("crash helper killed after deadline")
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter([Crash()]))
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token, deadline=101.0)
    assert_timeout_preserved(manager, runtime, result, config)
    assert state.events[-1][0] == phase


def test_hdc_fallback_receives_only_remaining_time(stop_env, monkeypatch):
    _manager, _runtime, state, _config, _event = stop_env
    timeouts = []
    def run(*args, timeout):
        timeouts.append(timeout)
        if args[-1] == "-v":
            state.now += 0.6
            raise hdc_module.HdcError(list(args), 1, "", "unsupported flag")
        return "127.0.0.1:12345 TCP Connected fixture HarmonyOS"
    monkeypatch.setattr(hdc_module, "hdc_run", run)
    result = hdc_module.hdc_list_targets(deadline=101.0, strict=True)
    assert timeouts == pytest.approx([1.0, 0.4])
    assert result[0].status == "Connected"


@pytest.mark.parametrize("strict", [False, True])
def test_hdc_strict_failure_is_opt_in_and_default_behavior_is_unchanged(stop_env, monkeypatch, strict):
    timeouts = []
    def run(*args, timeout):
        timeouts.append(timeout)
        raise hdc_module.HdcError(list(args), 1, "", "unavailable")
    monkeypatch.setattr(hdc_module, "hdc_run", run)
    if strict:
        with pytest.raises(hdc_module.HdcError):
            hdc_module.hdc_list_targets(deadline=101.0, strict=True)
        assert timeouts == [1.0, 1.0]
    else:
        assert hdc_module.hdc_list_targets() == []
        assert timeouts == [5.0, 5.0]


@pytest.mark.parametrize("returned", [False, True])
def test_hdc_deadline_blocks_late_empty_success_or_fallback(stop_env, monkeypatch, returned):
    _manager, _runtime, state, _config, _event = stop_env
    calls = []
    def run(*args, timeout):
        calls.append(timeout)
        state.now += 2.0
        if not returned:
            raise hdc_module.HdcError(list(args), 1, "", "timeout")
        return "[Empty]"
    monkeypatch.setattr(hdc_module, "hdc_run", run)
    with pytest.raises(TimeoutError):
        hdc_module.hdc_list_targets(deadline=101.0, strict=True)
    assert calls == [1.0]


@pytest.mark.parametrize("output", ["[Fail]Connect server failed", "hdc server is not started"])
def test_hdc_textual_query_failure_is_not_strict_empty_inventory(stop_env, monkeypatch, output):
    monkeypatch.setattr(hdc_module, "hdc_run", lambda *_a, **_k: output)
    with pytest.raises(hdc_module.HdcError):
        hdc_module.hdc_list_targets(deadline=101.0, strict=True)
    assert hdc_module.hdc_list_targets() == []
