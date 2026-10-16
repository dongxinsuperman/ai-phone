"""Android lifecycle fault regressions; never inspect or control host devices."""
from __future__ import annotations

import asyncio
import errno
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.android_vm import capability as cap
from ai_phone.agent.android_vm import manager as mod
from ai_phone.agent.drivers.base import DeviceInfo

_PORT_BIND_CHECK = mod._is_port_free


@pytest.fixture(autouse=True)
def isolate_host(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("test must not invoke host tools")

    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_a, **_k: iter(()))
    monkeypatch.setattr(mod, "find_android_tools", lambda: (None, ["test isolation"]))
    monkeypatch.setattr(mod, "_is_port_free", lambda _port: True)
    monkeypatch.setattr(mod, "_STOP_VERIFY_TIMEOUT_SEC", 0)
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "avds"))
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    mod.get_settings.cache_clear()
    cap._IMAGE_CACHE.clear()
    yield
    mod.get_settings.cache_clear()


def runtime(vm_id="a", *, process=None, ready=True):
    return mod.VmRuntime(vm_id, vm_id, "emulator-5554", 5554, process, 0,
                         avd_name=f"aiphone_vm_{vm_id}", ready=ready)


class Client:
    def __init__(self):
        self.agent_id = "test-agent"
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return True

    async def refresh_devices(self):
        return None


def checked_operation_details(message, state, *, resources_released=False):
    """保留原操作/清理契约断言，另核对本轮新增的只读观察结构。"""
    details = dict(message["details"])
    observation = details.pop("instance_observation")
    assert set(observation) == {"vm_id", "state", "identity", "resources_released", "reason"}
    assert observation["vm_id"] == message["vm_id"]
    assert observation["state"] == state
    assert observation["resources_released"] is resources_released
    assert observation["identity"] == {
        "avd_name": "" if state in {"missing", "unknown"} else f"aiphone_vm_{message['vm_id']}",
    }
    assert observation["reason"]
    evidence = details.pop("exit_evidence", {})
    if evidence.get("confirmed"):
        assert evidence["process_inventory_complete"] is True
        assert evidence["matching_process_ids"] == evidence["blocked_ports"] == []
    return details


class OwnedProcess:
    def __init__(self, *, refuse=False):
        self.alive = True
        self.refuse = refuse
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated += 1
        if self.refuse:
            raise PermissionError("test denied")
        self.alive = False

    def kill(self):
        self.killed += 1
        self.alive = False

    def wait(self, timeout=None):
        if self.alive:
            raise mod.psutil.TimeoutExpired(timeout)
        return 0


def fake_tools(monkeypatch):
    tools = cap.AndroidVmTools("/test/adb", "/test/emulator", "/test/avdmanager", sdk_root="")
    monkeypatch.setattr(mod, "find_android_tools", lambda: (tools, []))
    return tools


async def test_sweep_retains_live_process_on_adb_loss_and_restores_identity(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    proc = OwnedProcess()
    rt = runtime(process=proc)
    manager._runtimes["a"] = rt
    client = Client()
    for _ in range(4):
        await manager.sweep_vanished_vms(client, set())
    assert manager._runtimes["a"] is rt
    assert proc.terminated == proc.killed == 0
    assert [m["state"] for m in client.sent] == ["error"]
    assert client.sent[0]["details"]["cleanup_confirmed"] is False

    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_other")
    await manager.sweep_vanished_vms(client, {"emulator-5554"})
    assert rt.transport_unavailable is True
    assert len(client.sent) == 1
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    monkeypatch.setattr(manager, "_getprop", lambda *_: "1")
    await manager.sweep_vanished_vms(client, {"emulator-5554"})
    assert rt.transport_unavailable is False
    assert client.sent[-1]["reason"] == "transport_restored"


@pytest.mark.parametrize("uncertain", ["process_inventory", "occupied_port", "adopted_process"])
async def test_sweep_missing_transport_is_not_exit_proof(tmp_path, monkeypatch, uncertain):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    rt = runtime()
    manager._runtimes["a"] = rt
    if uncertain == "process_inventory":
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    elif uncertain == "occupied_port":
        monkeypatch.setattr(mod, "_is_port_free", lambda _: False)
    else:
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([(object(), 5554)], True))
    client = Client()
    await manager.sweep_vanished_vms(client, set())
    await manager.sweep_vanished_vms(client, set())
    assert manager._runtimes["a"] is rt
    assert client.sent[-1]["state"] == "error"


async def test_sweep_does_not_race_locked_lifecycle_operation(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    rt = runtime()
    manager._runtimes["a"] = rt
    rt.missing_ticks = 2
    client = Client()
    with manager._vm_lock("a"):
        await manager.sweep_vanished_vms(client, set())
    assert manager._runtimes["a"] is rt
    assert client.sent == []


def test_stop_retains_owned_process_when_sync_channel_is_unavailable(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    proc = OwnedProcess()
    manager._runtimes["a"] = runtime(process=proc)
    result = manager.stop_sync("a")
    assert result["ok"] is False
    assert result["details"]["cleanup_confirmed"] is False
    assert proc.terminated == 0
    assert manager._runtimes["a"].process is proc


def test_sweep_captures_operation_id_before_later_stop(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    rt = runtime(process=OwnedProcess(refuse=True))
    rt.observed_request_id = "start-1"
    rt.missing_ticks = 1
    manager._runtimes["a"] = rt
    old_status = manager._sweep_runtime(rt, set())
    assert old_status["observed_request_id"] == "start-1"
    assert manager.stop_sync("a", request_id="stop-2")["ok"] is False
    assert rt.observed_request_id == "stop-2"
    assert old_status["observed_request_id"] == "start-1"


def test_delete_retains_runtime_when_stop_fails(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    proc = OwnedProcess(refuse=True)
    rt = runtime(process=proc)
    manager._runtimes["a"] = rt
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    delete = Mock(side_effect=AssertionError("must not delete a live AVD"))
    monkeypatch.setattr(manager, "_delete_avd", delete)
    result = manager.delete_sync("a")
    assert result["ok"] is False
    assert result["reason"] == "stop_unconfirmed"
    assert result["details"]["cleanup_confirmed"] is False
    assert manager._runtimes["a"] is rt
    delete.assert_not_called()


def test_delete_stale_serial_never_kills_other_vm(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_new")
    monkeypatch.setattr(mod, "_is_port_free", lambda _: False)
    kill = Mock(side_effect=AssertionError("stale serial must not be killed"))
    delete = Mock(side_effect=AssertionError("cleanup is not confirmed"))
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    monkeypatch.setattr(manager, "_delete_avd", delete)
    result = manager.delete_sync("old", "emulator-5554")
    assert result["ok"] is False
    assert "old" in manager._runtimes
    kill.assert_not_called()
    delete.assert_not_called()


def test_stop_tracks_adopted_process_but_cannot_destroy_without_sync(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    proc = OwnedProcess()
    monkeypatch.setattr(mod, "_find_avd_processes", lambda name: (
        [(proc, 5556)] if name == "aiphone_vm_a" and proc.alive else [], True
    ))
    result = manager.stop_sync("a")
    assert result["ok"] is False
    assert result["adb_serial"] == "emulator-5556"
    assert proc.terminated == 0
    assert "a" in manager._runtimes


def test_stop_all_counts_only_confirmed_cleanup(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    manager._runtimes["a"] = runtime(process=OwnedProcess(refuse=True))
    manager._runtimes["b"] = runtime("b")
    assert manager.stop_all() == 1
    assert set(manager._runtimes) == {"a"}


def test_stop_graceful_command_is_not_itself_exit_proof(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    proc = OwnedProcess(refuse=True)
    manager._runtimes["a"] = runtime(process=proc)
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    monkeypatch.setattr(manager, "_kill_emulator", lambda *_: True)
    assert manager.stop_sync("a")["ok"] is False
    assert "a" in manager._runtimes


@pytest.mark.parametrize("kill_exits", [True, False])
def test_destroy_waits_for_exit_before_releasing_runtime(tmp_path, monkeypatch, kill_exits):
    class StubbornProcess(OwnedProcess):
        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1
            self.alive = not kill_exits

    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    proc = StubbornProcess()
    manager._runtimes["a"] = runtime(process=proc)
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    monkeypatch.setattr(manager, "_delete_avd", lambda *_: True)
    assert manager.delete_sync("a")["ok"] is kill_exits
    assert proc.terminated == proc.killed == 1
    assert ("a" in manager._runtimes) is not kill_exits


def test_assigned_port_is_not_silently_replaced(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(mod, "_is_port_free", lambda port: port != 5554)
    with pytest.raises(RuntimeError, match="未自动换端口"):
        manager._choose_port(prefer=5554)
    assert manager._choose_port() == 5556


async def test_start_identity_mismatch_is_retained_without_provisioning_other_vm(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    proc = OwnedProcess()
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_ensure_avd", lambda *_a, **_kw: None)
    monkeypatch.setattr(manager, "_wait_boot_completed", lambda *_: None)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_other")
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *_a, **_kw: proc)
    provision = Mock(side_effect=AssertionError("must not provision wrong identity"))
    kill = Mock(side_effect=AssertionError("must not kill stale serial"))
    monkeypatch.setattr(manager, "_provision_device", provision)
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "request_id": "start-1", "assigned_port": 5554})
    assert client.sent[-1]["state"] == "error"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert client.sent[-1]["details"]["instance_created"] is True
    assert proc.terminated == 0
    assert manager._runtimes["a"].process is proc
    provision.assert_not_called()
    kill.assert_not_called()


async def test_precreation_failure_stop_then_first_creation_remains_possible(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    client = Client()
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": False, "reason": "image missing"})
    await manager._start_and_report(client, {"vm_id": "a", "request_id": "start-1"})
    assert client.sent[-1]["details"]["instance_created"] is False
    assert manager._runtimes == {}

    await manager.handle_stop(client, {"vm_id": "a", "request_id": "stop-2"})
    stopped = client.sent[-1]
    assert stopped["state"] == "stopped"
    assert stopped["reason"] == "not_running"
    assert stopped["details"]["avd_name"] == "aiphone_vm_a"
    assert stopped["details"]["instance_created"] is False

    fake_tools(monkeypatch)
    proc = OwnedProcess()
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_wait_boot_completed", lambda *_: None)
    monkeypatch.setattr(manager, "_provision_device", lambda *_: None)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *_a, **_kw: proc)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run)
    await manager._start_and_report(client, {
        "vm_id": "a", "request_id": "start-3", "assigned_port": 5554,
        "require_existing_instance": False,
    })
    assert client.sent[-1]["state"] == "running"
    assert client.sent[-1]["details"]["instance_created"] is True
    assert client.sent[-1]["details"]["created_in_attempt"] is True
    assert any(call.args[0][1:3] == ["create", "avd"] for call in run.call_args_list)
    assert manager.stop_sync("a")["details"]["instance_created"] is True


async def test_failed_ensure_does_not_turn_placeholder_into_created_instance(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_ensure_avd", Mock(side_effect=RuntimeError("create failed")))
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "request_id": "start-1"})
    assert checked_operation_details(client.sent[-1], "stopped", resources_released=True) == {"cleanup_confirmed": True, "instance_created": False}
    assert manager.stop_sync("a")["details"]["instance_created"] is False


def failing_boot_setup(tmp_path, monkeypatch, *, already_exists=False):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    owned_avd = tmp_path / "avds" / "aiphone_vm_a.avd"
    other_avd = tmp_path / "avds" / "aiphone_vm_other.avd"
    other_avd.mkdir(parents=True)
    (other_avd / "userdata.img").write_text("other instance data")
    if already_exists:
        owned_avd.mkdir(parents=True)
        (owned_avd / "config.ini").write_text("managed=true")
        (owned_avd / "userdata.img").write_text("owned instance data")

    def ensure(*_args, **_kwargs):
        if already_exists:
            return False
        owned_avd.mkdir(parents=True, exist_ok=True)
        (owned_avd / "config.ini").write_text("managed=true")
        (owned_avd / "userdata.img").write_text("owned instance data")
        return True

    proc = OwnedProcess()
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_ensure_avd", ensure)
    monkeypatch.setattr(manager, "_wait_boot_completed", Mock(side_effect=TimeoutError("test boot timeout")))
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *_a, **_kw: proc)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run)
    return manager, owned_avd, other_avd, proc, run


async def test_first_binding_failure_deletes_only_owned_instance_once(tmp_path, monkeypatch):
    manager, owned_avd, other_avd, proc, run = failing_boot_setup(tmp_path, monkeypatch)
    client = Client()
    await manager._start_and_report(client, {
        "vm_id": "a", "request_id": "first-bind", "rollback_new_instance": True,
        "require_existing_instance": False,
    })
    assert [message["state"] for message in client.sent] == ["error"]
    assert client.sent[-1]["reason"] == "start_failed"
    assert checked_operation_details(client.sent[-1], "missing") == {
        "cleanup_confirmed": True, "instance_created": False, "created_in_attempt": True,
    }
    assert not owned_avd.exists()
    assert (other_avd / "userdata.img").read_text() == "other instance data"
    assert manager._runtimes == {}
    assert proc.terminated == 1
    assert run.call_count == 1
    assert run.call_args.args[0] == ["/test/avdmanager", "delete", "avd", "-n", "aiphone_vm_a"]


@pytest.mark.parametrize("rollback, require_existing", [(False, True), (False, False), (True, True)])
async def test_restart_failure_never_deletes_saved_avd(tmp_path, monkeypatch, rollback, require_existing):
    manager, owned_avd, other_avd, proc, run = failing_boot_setup(tmp_path, monkeypatch, already_exists=True)
    client = Client()
    await manager._start_and_report(client, {
        "vm_id": "a", "request_id": "restart", "rollback_new_instance": rollback,
        "require_existing_instance": require_existing,
    })
    assert checked_operation_details(client.sent[-1], "running") == {"cleanup_confirmed": False, "instance_created": True}
    assert (owned_avd / "userdata.img").read_text() == "owned instance data"
    assert (other_avd / "userdata.img").read_text() == "other instance data"
    assert proc.terminated == 0
    assert "a" in manager._runtimes
    run.assert_not_called()


async def test_existing_instance_start_failure_cannot_skip_failed_sync(tmp_path, monkeypatch):
    manager, owned_avd, _other_avd, proc, run = failing_boot_setup(tmp_path, monkeypatch, already_exists=True)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    sync = Mock(return_value=False)
    kill = Mock(side_effect=AssertionError("existing data must not be destroyed on failed sync"))
    monkeypatch.setattr(manager, "_sync_guest", sync)
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "require_existing_instance": True})
    assert client.sent[-1]["reason"] == "start_failed"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert (owned_avd / "userdata.img").read_text() == "owned instance data"
    assert manager._runtimes["a"].process is proc
    assert proc.terminated == proc.killed == 0
    sync.assert_called_once()
    kill.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
async def test_first_binding_rollback_failure_is_final_without_retry(tmp_path, monkeypatch, failure):
    manager, owned_avd, _other_avd, _proc, run = failing_boot_setup(tmp_path, monkeypatch)
    if failure == "nonzero":
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="AVD busy")
    else:
        run.side_effect = mod.subprocess.TimeoutExpired("delete avd", 30)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert client.sent[-1]["state"] == "error"
    assert "test boot timeout" in client.sent[-1]["error"]
    assert "清理未完成" in client.sent[-1]["error"]
    assert checked_operation_details(client.sent[-1], "stopped") == {
        "cleanup_confirmed": False, "instance_created": True, "created_in_attempt": True,
    }
    assert owned_avd.exists()
    assert run.call_count == 1
    await asyncio.sleep(0)
    assert run.call_count == 1


async def test_first_binding_failed_stop_does_not_delete_live_avd(tmp_path, monkeypatch):
    manager, owned_avd, _other_avd, proc, run = failing_boot_setup(tmp_path, monkeypatch)
    proc.refuse = True
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert checked_operation_details(client.sent[-1], "running") == {
        "cleanup_confirmed": False, "instance_created": True, "created_in_attempt": True,
    }
    assert owned_avd.exists()
    assert manager._runtimes["a"].process is proc
    run.assert_not_called()


async def test_first_binding_preflight_failure_needs_no_native_cleanup(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": False, "reason": "image missing"})
    cleanup = Mock(side_effect=AssertionError("no native resource was created"))
    monkeypatch.setattr(manager, "delete_sync", cleanup)
    monkeypatch.setattr(manager, "stop_sync", cleanup)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert checked_operation_details(client.sent[-1], "unknown") == {
        "cleanup_confirmed": True, "instance_created": False, "created_in_attempt": False,
    }
    cleanup.assert_not_called()


async def test_first_binding_post_create_config_failure_deletes_created_avd_once(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_write_avd_screen_config", Mock(side_effect=RuntimeError("config write failed")))
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert checked_operation_details(client.sent[-1], "unknown") == {
        "cleanup_confirmed": True, "instance_created": False, "created_in_attempt": True,
    }
    assert [call.args[0][1:3] for call in run.call_args_list] == [["list", "avd"], ["create", "avd"], ["delete", "avd"]]
    assert "--force" not in run.call_args_list[1].args[0]


async def test_first_binding_refuses_existing_avd_without_changing_or_stopping_it(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    avd = tmp_path / "avds" / "aiphone_vm_a.avd"
    avd.mkdir(parents=True)
    (avd / "userdata.img").write_text("existing saved data")
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="Name: aiphone_vm_a\n", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run)
    mutate = Mock(side_effect=AssertionError("older instance must remain untouched"))
    monkeypatch.setattr(manager, "_write_avd_screen_config", mutate)
    monkeypatch.setattr(manager, "stop_sync", mutate)
    monkeypatch.setattr(manager, "delete_sync", mutate)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert client.sent[-1]["state"] == "error"
    assert "已有同名 AVD" in client.sent[-1]["error"]
    assert checked_operation_details(client.sent[-1], "stopped") == {
        "cleanup_confirmed": False, "instance_created": True,
        "created_in_attempt": False, "instance_preexisting": True,
    }
    assert (avd / "userdata.img").read_text() == "existing saved data"
    assert manager._runtimes == {}
    assert run.call_count == 1
    mutate.assert_not_called()


async def test_first_binding_cannot_reuse_already_tracked_instance(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    rt = runtime(process=OwnedProcess())
    manager._runtimes["a"] = rt
    manager._created_vm_ids.add("a")
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert checked_operation_details(client.sent[-1], "running") == {
        "cleanup_confirmed": False, "instance_created": True,
        "created_in_attempt": False, "instance_preexisting": True,
    }
    assert manager._runtimes["a"] is rt
    assert rt.process.terminated == 0


async def test_first_binding_create_collision_failure_never_deletes_unknown_existing_data(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    run = Mock(side_effect=[
        SimpleNamespace(returncode=0, stdout="", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="already exists"),
    ])
    monkeypatch.setattr(mod.subprocess, "run", run)
    cleanup = Mock(side_effect=AssertionError("failed native create is not ownership proof"))
    monkeypatch.setattr(manager, "delete_sync", cleanup)
    client = Client()
    await manager._start_and_report(client, {"vm_id": "a", "rollback_new_instance": True})
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert "created_in_attempt" not in client.sent[-1]["details"]
    assert run.call_count == 2
    assert "--force" not in run.call_args_list[1].args[0]
    cleanup.assert_not_called()


def test_boot_wait_uses_remaining_operation_budget(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    tools = fake_tools(monkeypatch)
    rt = runtime()
    rt.operation_deadline = mod.time.monotonic() + 0.5
    manager._runtimes["a"] = rt
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="1", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run)
    manager._wait_boot_completed(tools, "emulator-5554")
    assert run.call_count == 2
    assert all(0 < call.kwargs["timeout"] <= 0.5 for call in run.call_args_list)


def test_expired_boot_budget_does_not_start_another_native_wait(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    tools = fake_tools(monkeypatch)
    rt = runtime()
    rt.operation_deadline = mod.time.monotonic() - 1
    manager._runtimes["a"] = rt
    run = Mock(side_effect=AssertionError("operation budget already expired"))
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="startup deadline exceeded"):
        manager._wait_boot_completed(tools, "emulator-5554")
    run.assert_not_called()


@pytest.mark.parametrize("wire_deadline", [0.0, 1000000000.0])
async def test_executor_queue_time_cannot_restart_start_budget(tmp_path, monkeypatch, wire_deadline):
    clock = {"now": 100.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    probe = Mock(side_effect=AssertionError("expired operation must not call native preflight"))
    monkeypatch.setattr(manager, "probe", probe)

    async def queued_worker(func, *args, **kwargs):
        assert args[0]["_operation_deadline"] == pytest.approx(100.02)
        clock["now"] += 0.06
        return func(*args, **kwargs)

    monkeypatch.setattr(mod.asyncio, "to_thread", queued_worker)
    message = {
        "vm_id": "a", "request_id": "queued-start", "rollback_new_instance": True,
        "operation_timeout_sec": 0.02, "_operation_deadline": wire_deadline,
    }
    original = dict(message)
    client = Client()
    await manager.handle_start(client, message)
    assert [item["state"] for item in client.sent] == ["starting", "error"]
    assert "startup deadline exceeded" in client.sent[-1]["error"]
    assert checked_operation_details(client.sent[-1], "unknown") == {
        "cleanup_confirmed": True, "instance_created": False, "created_in_attempt": False,
    }
    assert message == original
    assert manager._runtimes == {}
    probe.assert_not_called()


@pytest.mark.parametrize("acquired", [False, True])
def test_lifecycle_lock_wait_is_included_in_start_budget(tmp_path, monkeypatch, acquired):
    clock = {"now": 100.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    probe = Mock(side_effect=AssertionError("expired operation must not create or start"))
    monkeypatch.setattr(manager, "probe", probe)
    releases = []

    class DelayedLock:
        def acquire(self, *, timeout):
            assert timeout == pytest.approx(0.02)
            clock["now"] += 0.06
            return acquired

        def release(self):
            releases.append(True)

    monkeypatch.setattr(manager, "_vm_lock", lambda _: DelayedLock())
    message = {"vm_id": "a", "rollback_new_instance": True, "operation_timeout_sec": 0.02}
    with pytest.raises(mod.VmStartFailure, match="startup deadline exceeded") as failure:
        manager.start_sync(message)
    assert failure.value.cleanup_confirmed is True
    assert failure.value.created_in_attempt is False
    assert "_operation_deadline" not in message
    assert releases == ([True] if acquired else [])
    probe.assert_not_called()


def test_start_cannot_reuse_failed_start_placeholder(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    manager._runtimes["a"] = runtime(ready=False)
    with pytest.raises(RuntimeError, match="尚未就绪"):
        manager.start_sync({"vm_id": "a"})


@pytest.mark.parametrize("returncode, output", [
    (1, ""), (0, ""), (0, "Name: aiphone_vm_a_suffix\n")
])
def test_restart_never_recreates_missing_or_unknown_avd(tmp_path, monkeypatch, returncode, output):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    tools = fake_tools(monkeypatch)
    run = Mock(return_value=SimpleNamespace(returncode=returncode, stdout=output, stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="无法读取|原有实例缺失"):
        manager._ensure_avd(
            tools, avd_name="aiphone_vm_a", system_image="image", width=1080, height=1920,
            density=420, orientation="portrait", ram_mb=0, vm_heap_mb=0,
            internal_storage_mb=0, sdcard_mb=0, hardware={}, require_existing=True,
        )
    assert run.call_count == 1
    assert run.call_args.args[0] == ["/test/avdmanager", "list", "avd"]


async def test_start_handler_awaits_completion_for_command_queue(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(*_):
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "_start_and_report", start)
    task = asyncio.create_task(manager.handle_start(Client(), {"vm_id": "a"}))
    await entered.wait()
    assert not task.done()
    release.set()
    await task


async def test_failed_avd_inventory_is_not_empty_success(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    manager._runtimes["a"] = runtime()
    fake_tools(monkeypatch)
    monkeypatch.setattr(mod.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="")))
    client = Client()
    await manager.report_orphan_reconcile(client)
    message = client.sent[-1]
    assert message["inventory_complete"] is False
    assert message["unknown_vm_ids"] == ["a"]
    assert message["stopped_vm_ids"] == []


async def test_failed_adb_inventory_retains_unknown_avd(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)

    def run(args, **_kwargs):
        if args == ["/test/adb", "devices"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="offline")
        return SimpleNamespace(returncode=0, stdout="Name: aiphone_vm_a\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert manager.reconcile_running_vms_sync() == []
    client = Client()
    await manager.report_orphan_reconcile(client)
    message = client.sent[-1]
    assert message["inventory_complete"] is False
    assert message["unknown_vm_ids"] == ["a"]
    assert message["stopped_vm_ids"] == []


async def test_adb_absent_live_process_is_unknown_during_reconcile(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    manager.reconcile_running_vms_sync()
    monkeypatch.setattr(manager, "list_managed_avd_vmids", lambda: ["a"])
    monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([(object(), 5554)], True))
    client = Client()
    await manager.report_orphan_reconcile(client)
    assert client.sent[-1]["unknown_vm_ids"] == ["a"]
    assert client.sent[-1]["stopped_vm_ids"] == []


def test_inventory_includes_managed_process_with_deleted_avd_definition(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    managed = SimpleNamespace(
        info={"name": "emulator"}, cmdline=lambda: ["emulator", "-avd", "aiphone_vm_orphan", "-port", "5554"]
    )
    external = SimpleNamespace(info={"name": "emulator"}, cmdline=lambda: ["emulator", "-avd", "personal"])
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_: iter([managed, external]))
    assert manager.list_managed_avd_vmids() == ["orphan"]
    assert manager._avd_inventory_proofs == {}


async def test_stopped_instance_proof_comes_only_from_successful_avd_listing(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    manager._running_inventory_complete = True
    manager._runtimes["remembered"] = runtime("remembered")
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout="Name: aiphone_vm_stopped\nName: external\n", stderr=""
    ))
    client = Client()
    await manager.report_orphan_reconcile(client)
    assert client.sent[-1]["instance_proofs"] == {"stopped": {"avd_name": "aiphone_vm_stopped"}}
    assert client.sent[-1]["inventory_complete"] is True
    assert manager.stop_sync("stopped")["details"]["instance_created"] is True
    assert manager.stop_sync("remembered")["details"]["instance_created"] is False
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout="", stderr=""))
    await manager.report_orphan_reconcile(client)
    assert client.sent[-1]["instance_proofs"] == {}
    assert client.sent[-1]["inventory_complete"] is False


def test_delete_reports_no_instance_after_confirmed_cleanup(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout="Name: aiphone_vm_a\n", stderr=""
    ))
    assert manager.list_managed_avd_vmids() == ["a"]
    monkeypatch.setattr(manager, "_delete_avd", lambda *_: True)
    result = manager.delete_sync("a")
    assert result["details"] == {"cleanup_confirmed": True, "instance_created": False}
    assert manager.stop_sync("a")["details"]["instance_created"] is False
    assert manager._avd_inventory_proofs == {}


def test_reconcile_never_kills_external_emulator(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setenv("AI_PHONE_ANDROID_VM_KILL_FOREIGN", "true")
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout="List of devices attached\nemulator-5554 device\n", stderr=""
    ))
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "personal_avd")
    kill = Mock(side_effect=AssertionError("foreign VM must be untouched"))
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    assert manager.reconcile_running_vms_sync() == []
    kill.assert_not_called()


async def test_reclaimed_booting_instance_is_not_reported_running_until_booted(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    fake_tools(monkeypatch)
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout="List of devices attached\nemulator-5554 device\n", stderr=""
    ))
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    monkeypatch.setattr(manager, "_getprop", lambda *_: "")
    assert manager.reconcile_running_vms_sync() == []
    assert "a" in manager._runtimes
    client = Client()
    await manager.sweep_vanished_vms(client, {"emulator-5554"})
    assert client.sent == []
    monkeypatch.setattr(manager, "_getprop", lambda *_: "1")
    await manager.sweep_vanished_vms(client, {"emulator-5554"})
    assert client.sent[-1]["state"] == "running"


def test_delete_failure_leaves_avd_files_and_reports_failure(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    tools = fake_tools(monkeypatch)
    avd_dir = tmp_path / "avds" / "aiphone_vm_a.avd"
    avd_dir.mkdir(parents=True)
    (avd_dir / "config.ini").write_text("test=true")
    ini = avd_dir.parent / "aiphone_vm_a.ini"
    ini.write_text(f"path={avd_dir}")
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mod.shutil, "rmtree", Mock(side_effect=PermissionError("in use")))
    assert manager._delete_avd(tools, "aiphone_vm_a") is False
    assert avd_dir.exists()
    assert ini.exists()


@pytest.mark.parametrize("list_returncode", [0, 1])
async def test_reconnect_finds_owned_disk_leftovers_without_avd_index(tmp_path, monkeypatch, list_returncode):
    tools = fake_tools(monkeypatch)
    owned = tmp_path / "avds" / "aiphone_vm_a.avd"
    owned.mkdir(parents=True)
    (owned / "userdata.img").write_text("left after failed removal")
    (owned.parent / "personal.avd").mkdir()
    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=list_returncode, stdout="", stderr=""
    ))
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    manager._running_inventory_complete = True
    client = Client()
    await manager.report_orphan_reconcile(client)
    assert client.sent[-1]["vm_ids"] == ["a"]
    assert client.sent[-1]["instance_proofs"] == {}
    assert client.sent[-1]["inventory_complete"] is (list_returncode == 0)
    assert mod._managed_avd_files(tools) == {"a"}


def test_managed_metadata_never_marks_a_real_device_virtual(tmp_path):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    rt = runtime()
    rt.adb_serial = "REAL-DEVICE"
    manager._runtimes["a"] = rt
    device = DeviceInfo(serial="REAL-DEVICE", platform="android")
    assert manager.decorate_devices([device]) == [device]
    assert device.extra == {}


def test_process_identity_matches_exact_managed_avd_only(monkeypatch):
    def proc(name, args):
        return SimpleNamespace(info={"name": name}, cmdline=lambda: args)

    expected = proc("qemu-system-aarch64", ["qemu", "-avd", "aiphone_vm_a", "-port", "5554"])
    wrong = proc("emulator", ["emulator", "-avd", "aiphone_vm_a_other", "-port", "5556"])
    other = proc("adb", ["adb", "-avd", "aiphone_vm_a"])
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_: iter([expected, wrong, other]))
    assert mod._find_avd_processes("aiphone_vm_a") == ([(expected, 5554)], True)


def test_external_avd_names_cannot_alias_managed_vm_id():
    assert mod._vmid_from_avd_name("aiphone_vm_a-b") == ""
    assert mod._vmid_from_avd_name("aiphone_vm_" + "a" * 33) == ""
    assert mod._vmid_from_avd_name("aiphone_vm_a_b") == "a_b"


def test_uuid_vm_ids_keep_distinct_avd_names_beyond_shared_24_character_prefix():
    prefix = "0123456789abcdef01234567"
    vm_ids = [prefix + "89abcdef", prefix + "76543210"]
    names = [mod._safe_avd_name(vm_id) for vm_id in vm_ids]
    assert names[0] != names[1]
    assert names == [f"aiphone_vm_{vm_id}" for vm_id in vm_ids]
    assert [mod._vmid_from_avd_name(name) for name in names] == vm_ids


@pytest.mark.parametrize("vm_id", ["a9581dcf2569", "deadbeef0001", "0123456789ab"])
def test_legacy_vm_ids_keep_existing_avd_names(vm_id):
    name = f"aiphone_vm_{vm_id}"
    assert mod._safe_avd_name(vm_id) == name
    assert mod._vmid_from_avd_name(name) == vm_id


def test_process_permission_failure_is_unknown(monkeypatch):
    proc = SimpleNamespace(info={"name": "emulator"}, cmdline=Mock(side_effect=mod.psutil.AccessDenied(42)))
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_: iter([proc]))
    assert mod._find_avd_processes("aiphone_vm_a") == ([], False)


@pytest.mark.parametrize("alive, status, complete", [
    (False, mod.psutil.STATUS_RUNNING, True),
    (True, mod.psutil.STATUS_ZOMBIE, True),
    (True, mod.psutil.STATUS_RUNNING, False),
])
def test_nameless_process_only_ignored_when_proven_gone(monkeypatch, alive, status, complete):
    proc = SimpleNamespace(info={"name": None}, is_running=lambda: alive, status=lambda: status)
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_: iter([proc]))
    assert mod._find_avd_processes("aiphone_vm_a") == ([], complete)


def test_stop_evidence_identifies_process_inventory_blocker(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    result = manager.stop_sync("a", "emulator-5554")
    assert result["details"]["exit_evidence"]["reason"] == "process_inventory_incomplete"
    assert result["details"]["exit_evidence"]["process_inventory_complete"] is False


def test_stop_evidence_identifies_blocked_ports(tmp_path, monkeypatch):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(mod, "_is_port_free", lambda port: port != 5555)
    result = manager.stop_sync("a", "emulator-5554")
    evidence = result["details"]["exit_evidence"]
    assert evidence["reason"] == "port_unavailable"
    assert evidence["blocked_ports"] == [5555]
    assert evidence["matching_process_ids"] == []


@pytest.mark.skipif(mod.os.name == "nt", reason="POSIX socket reuse semantics")
def test_port_probe_matches_emulator_reuse_after_closed_tcp_connection():
    # All sockets/ports belong to this test; no ADB or fixed emulator port is accessed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(2)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(address)
            connection, _ = listener.accept()
            connection.shutdown(socket.SHUT_WR)
            connection.close()
            assert client.recv(1) == b""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as plain:
        with pytest.raises(OSError) as failure:
            plain.bind(address)
        assert failure.value.errno == errno.EADDRINUSE
    assert _PORT_BIND_CHECK(address[1]) is True


def test_port_probe_does_not_share_active_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert _PORT_BIND_CHECK(listener.getsockname()[1]) is False


def test_windows_port_probe_selects_exclusive_address_option(monkeypatch):
    calls = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def setsockopt(self, level, option, value):
            calls.append((level, option, value))

        def bind(self, _address):
            return None

    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", 123456, raising=False)
    monkeypatch.setattr(socket, "socket", lambda *_: Socket())
    assert _PORT_BIND_CHECK(5666) is True
    assert calls == [(socket.SOL_SOCKET, 123456, 1)]


def prepare_image_probe(monkeypatch):
    monkeypatch.setattr(cap, "_sdk_roots", lambda: [])
    monkeypatch.setattr(cap, "_scan_system_images", lambda _: [])
    return cap.AndroidVmTools("adb", "emulator", "avdmanager", sdkmanager="/test/sdkmanager", sdk_root="sdk-a")


def test_image_cache_collapses_concurrent_cold_queries(monkeypatch):
    tools = prepare_image_probe(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def run(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(returncode=0, stdout="system-images;android-35;google_apis;arm64-v8a | installed", stderr="")

    monkeypatch.setattr(cap.subprocess, "run", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cap.list_installed_system_images, tools)
        assert entered.wait(2)
        second = pool.submit(cap.list_installed_system_images, tools)
        release.set()
        assert first.result(2) == second.result(2)
    assert len(calls) == 1


def test_image_query_failure_does_not_cache_partial_output(monkeypatch):
    tools = prepare_image_probe(monkeypatch)
    image = "system-images;android-35;google_apis;arm64-v8a"
    run = Mock(side_effect=[
        SimpleNamespace(returncode=1, stdout=image, stderr="failed"),
        SimpleNamespace(returncode=0, stdout=image, stderr=""),
    ])
    monkeypatch.setattr(cap.subprocess, "run", run)
    assert cap.list_installed_system_images(tools) == []
    assert cap.list_installed_system_images(tools) == [image]
    assert run.call_count == 2


def test_image_cache_is_scoped_to_sdk_root(monkeypatch):
    tools = prepare_image_probe(monkeypatch)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(cap.subprocess, "run", run)
    cap.list_installed_system_images(tools)
    tools.sdk_root = "sdk-b"
    cap.list_installed_system_images(tools)
    assert run.call_count == 2
