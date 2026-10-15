"""One-budget Android cleanup; fake time/processes only, never host devices."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ai_phone.agent.android_vm import manager as mod
from ai_phone.agent.android_vm.capability import AndroidVmTools
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.shared import protocol as P


class Clock:
    now = 100.0

    def advance(self, seconds):
        self.now += seconds


class Process:
    def __init__(self, clock, *, pid=71, port=5554, exit_after=None, env=None, env_error=False):
        self.clock, self.pid, self.port = clock, pid, port
        self.exit_at = None if exit_after is None else clock.now + exit_after
        self.code = None
        self.env, self.env_error = env or {}, env_error
        self.waits, self.signals, self.env_keys = [], [], []

    def environ(self):
        if self.env_error:
            raise mod.psutil.AccessDenied(self.pid)
        owner = self

        class SingleKeyOnly(dict):
            def get(self, key):
                owner.env_keys.append(key)
                assert key == mod._NATIVE_GRACE_ENV
                return owner.env.get(key)

            def __repr__(self):
                raise AssertionError("process environment must not be logged")

        return SingleKeyOnly()

    def poll(self):
        if self.code is not None:
            return self.code
        return 0 if self.exit_at is not None and self.clock.now >= self.exit_at else None

    def wait(self, timeout):
        self.waits.append((self.clock.now, timeout))
        if self.poll() is not None:
            return self.poll()
        if self.exit_at is not None and self.exit_at <= self.clock.now + timeout:
            self.clock.now = self.exit_at
            return 0
        self.clock.advance(timeout)
        raise mod.psutil.TimeoutExpired(timeout, pid=self.pid)

    def terminate(self):
        self.signals.append(("terminate", self.clock.now))
        self.code = -15

    def kill(self):
        self.signals.append(("kill", self.clock.now))
        self.code = -9


class Client:
    agent_id = "isolated-agent"

    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(value)
        return True

    async def refresh_devices(self):
        pass


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(mod.time, "sleep", clock.advance)
    forbidden = Mock(side_effect=AssertionError("no host native command allowed"))
    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_: iter(()))
    monkeypatch.setattr(mod, "_is_port_free", lambda _: True)
    tools = AndroidVmTools("/isolated/adb", "/isolated/emulator", "/isolated/avdmanager", sdk_root="")
    monkeypatch.setattr(mod, "find_android_tools", lambda: (tools, []))
    monkeypatch.setenv("ANDROID_AVD_HOME", str(tmp_path / "avds"))
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    mod.get_settings.cache_clear()
    yield clock, tools
    mod.get_settings.cache_clear()


def managed(monkeypatch, tmp_path, clock, processes, *, commands=True):
    manager = mod.AndroidVmManager(runtime_dir=tmp_path / "runtime")
    rt = mod.VmRuntime("a", "a", "emulator-5554", 5554, processes[0] if processes else None, 0,
                       avd_name="aiphone_vm_a", ready=True)
    manager._runtimes["a"] = rt
    manager._created_vm_ids.add("a")
    monkeypatch.setattr(mod, "_find_avd_processes", lambda name: (
        [(proc, proc.port) for proc in processes if proc.poll() is None] if name == "aiphone_vm_a" else [], True,
    ))
    if commands:
        monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
        monkeypatch.setattr(manager, "_kill_emulator", lambda *_: True)
    return manager, rt


def test_sdk_default_allows_eight_second_normal_exit_without_signal(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock, exit_after=8)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    monkeypatch.setenv(mod._NATIVE_GRACE_ENV, "1")  # New Agent environment is irrelevant.
    assert manager.stop_sync("a")["ok"] is True
    assert proc.waits == [(100.0, 20.0)]
    assert proc.signals == []
    assert clock.now == 108
    assert proc.env_keys == [mod._NATIVE_GRACE_ENV]


@pytest.mark.parametrize("raw,expected", [("35", 35), (" +30 ", 30), ("00021", 21)])
def test_exact_adopted_process_integer_override(isolated, monkeypatch, tmp_path, raw, expected):
    clock, _ = isolated
    proc = Process(clock, exit_after=8, env={mod._NATIVE_GRACE_ENV: raw})
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    rt.process = None  # Real process was adopted after Agent restart.
    monkeypatch.setenv(mod._NATIVE_GRACE_ENV, "2")
    assert manager.stop_sync("a")["ok"] is True
    assert proc.waits == [(100.0, expected)]
    assert proc.signals == []


@pytest.mark.parametrize("raw", ["0.5", "1e-3", "-1", "0", "NaN", "inf", "bad", "9" * 400])
def test_non_integer_or_unrepresentable_override_never_shortens_grace(isolated, monkeypatch, tmp_path, raw):
    clock, _ = isolated
    proc = Process(clock, exit_after=8, env={mod._NATIVE_GRACE_ENV: raw})
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    assert manager.stop_sync("a", deadline=130)["ok"] is True
    assert proc.waits == [(100.0, 30.0)]
    assert proc.signals == []


def test_unknown_process_environment_uses_remaining_budget_not_new_agent_env(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock, exit_after=30, env_error=True)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    monkeypatch.setenv(mod._NATIVE_GRACE_ENV, "1")
    assert manager.stop_sync("a", deadline=160)["ok"] is True
    assert proc.waits == [(100.0, 60.0)]
    assert proc.signals == []


@pytest.mark.parametrize("env_error", [False, True])
def test_budget_expiry_never_starts_force_and_retains_identity(isolated, monkeypatch, tmp_path, env_error):
    clock, _ = isolated
    proc = Process(clock, env_error=env_error)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    result = manager.stop_sync("a", deadline=110)
    assert result["reason"] == "operation_timeout"
    assert result["details"]["cleanup_confirmed"] is False
    assert result["details"]["instance_created"] is True
    assert manager._runtimes["a"] is rt
    assert proc.signals == []
    assert clock.now == 110


def test_multiple_pids_share_one_grace_and_duplicate_pid_is_not_stopped_twice(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    first, second = Process(clock), Process(clock, pid=72)
    manager, _ = managed(monkeypatch, tmp_path, clock, [first, first, second])
    assert manager.stop_sync("a")["ok"] is True
    assert first.signals == second.signals == [("terminate", 120.0)]
    assert first.waits == [(100.0, 20.0), (120.0, 5)]
    assert second.waits == [(120.0, 5)]


def test_last_accepted_serial_gets_native_window_without_per_pid_reset(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    first, second = Process(clock), Process(clock, pid=72, port=5556)
    manager, _ = managed(monkeypatch, tmp_path, clock, [first, second])
    accepted = []

    def kill(*_):
        clock.advance(3)
        accepted.append(clock.now)
        return True

    monkeypatch.setattr(manager, "_kill_emulator", kill)
    assert manager.stop_sync("a")["ok"] is True
    assert accepted == [103, 106]
    assert first.signals == second.signals == [("terminate", 126.0)]


@pytest.mark.parametrize("elapsed", [0, 0.2])
def test_fast_exit_does_not_wait_out_native_window(isolated, monkeypatch, tmp_path, elapsed):
    clock, _ = isolated
    proc = Process(clock, exit_after=elapsed)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    assert manager.stop_sync("a")["ok"] is True
    assert clock.now == pytest.approx(100 + elapsed)
    assert not proc.signals


def test_rejected_native_command_keeps_existing_owned_force_path(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    monkeypatch.setattr(manager, "_kill_emulator", lambda *_: False)
    assert manager.stop_sync("a")["ok"] is True
    assert proc.signals == [("terminate", 100.0)]
    assert proc.env_keys == []


def test_vm_lock_wait_is_in_the_same_budget(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    acquire = Mock(side_effect=lambda timeout: clock.advance(timeout) or False)
    lock = SimpleNamespace(acquire=acquire, release=Mock())
    monkeypatch.setattr(manager, "_vm_lock", lambda _: lock)
    assert manager.stop_sync("a", deadline=100.02)["reason"] == "operation_timeout"
    assert acquire.call_args.kwargs["timeout"] == pytest.approx(0.02)
    assert manager._runtimes["a"] is rt
    assert not proc.waits and not proc.signals
    lock.release.assert_not_called()


def test_native_commands_get_remaining_budget_and_no_command_starts_after_expiry(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc], commands=False)

    def run(args, **kwargs):
        assert args[-3:] == ["emu", "avd", "name"]
        assert kwargs["timeout"] == pytest.approx(0.5)
        clock.advance(0.6)
        return SimpleNamespace(returncode=0, stdout="aiphone_vm_a\n", stderr="")

    command = Mock(side_effect=run)
    monkeypatch.setattr(mod.subprocess, "run", command)
    assert manager.stop_sync("a", deadline=100.5)["reason"] == "operation_timeout"
    assert command.call_count == 1
    assert not proc.signals
    assert manager._runtimes["a"] is rt


def test_late_exit_observation_cannot_report_success_or_forget_identity(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    manager, rt = managed(monkeypatch, tmp_path, clock, [])
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    monkeypatch.setattr(manager, "_runtime_exit_confirmed", lambda _: clock.advance(2) or True)
    result = manager.stop_sync("a", deadline=101)
    assert result["reason"] == "operation_timeout"
    assert manager._runtimes["a"] is rt


def test_delete_does_not_restart_budget_after_stop(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    avd = tmp_path / "avds/aiphone_vm_a.avd"
    avd.mkdir(parents=True)
    (avd / "config.ini").write_text("owned=true")
    (avd / "userdata.img").write_text("keep until confirmed deletion")

    def run(args, **kwargs):
        assert args[1:3] == ["delete", "avd"]
        assert kwargs["timeout"] == 10  # Stop consumed 20 of the one 30s budget.
        clock.advance(10)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    command = Mock(side_effect=run)
    monkeypatch.setattr(mod.subprocess, "run", command)
    result = manager.delete_sync("a", deadline=130)
    assert result["reason"] == "operation_timeout"
    assert command.call_count == 1
    assert "a" in manager._created_vm_ids
    assert (avd / "userdata.img").read_text() == "keep until confirmed deletion"


def test_delete_never_proceeds_when_stop_exhausted_budget(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    deletion = Mock(side_effect=AssertionError("live AVD must not be deleted"))
    monkeypatch.setattr(manager, "_delete_avd", deletion)
    assert manager.delete_sync("a", deadline=110)["reason"] == "operation_timeout"
    deletion.assert_not_called()
    assert manager._runtimes["a"] is rt


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "delete"])
async def test_executor_queue_cannot_restart_control_budget(isolated, monkeypatch, tmp_path, action):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])

    async def delayed_executor(function, *args, **kwargs):
        assert kwargs["deadline"] == pytest.approx(100.02)
        clock.advance(0.03)
        return function(*args, **kwargs)

    monkeypatch.setattr(mod.asyncio, "to_thread", delayed_executor)
    client = Client()
    message = {"vm_id": "a", "request_id": "control", "operation_timeout_sec": 0.02, "_operation_deadline": 10**12}
    await getattr(manager, "handle_" + action)(client, message)
    assert client.sent[-1]["reason"] == "operation_timeout"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert manager._runtimes["a"] is rt
    assert not proc.waits and not proc.signals
    assert message["_operation_deadline"] == 10**12


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "delete"])
@pytest.mark.parametrize("budget", [None, 0, -1, "invalid", float("inf"), float("nan")])
async def test_invalid_control_budget_cannot_execute_native_cleanup(isolated, monkeypatch, tmp_path, action, budget):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    client = Client()
    await getattr(manager, "handle_" + action)(client, {"vm_id": "a", "operation_timeout_sec": budget})
    assert client.sent[-1]["reason"] == "bad_request"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is False
    assert manager._runtimes["a"] is rt
    assert not proc.waits and not proc.signals


@pytest.mark.asyncio
async def test_stopping_status_wait_is_included_before_native_work(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    client = Client()

    async def delayed_send(value):
        client.sent.append(value)
        if value["state"] == "stopping":
            clock.advance(0.03)
        return True

    client.send = delayed_send
    await manager.handle_stop(client, {"vm_id": "a", "operation_timeout_sec": 0.02})
    assert client.sent[-1]["reason"] == "operation_timeout"
    assert manager._runtimes["a"] is rt
    assert not proc.waits and not proc.signals


@pytest.mark.asyncio
@pytest.mark.parametrize("first_bind", [True, False])
async def test_failed_start_has_one_bounded_cleanup_but_preserves_existing_avd(isolated, monkeypatch, tmp_path, first_bind):
    clock, _ = isolated
    proc = Process(clock)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    manager._runtimes.clear()
    if first_bind:
        manager._created_vm_ids.clear()
    avd = tmp_path / "avds/aiphone_vm_a.avd"
    avd.mkdir(parents=True)
    (avd / "config.ini").write_text("owned=true")
    (avd / "userdata.img").write_text("saved data")
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_ensure_avd", lambda *_a, **_k: first_bind)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *_a, **_k: proc)

    def failed_boot(*_):
        clock.advance(2)  # The one-second start budget is exhausted.
        raise TimeoutError("start failed")

    monkeypatch.setattr(manager, "_wait_boot_completed", failed_boot)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    command = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", command)
    client = Client()
    await manager._start_and_report(client, {
        "vm_id": "a", "rollback_new_instance": first_bind,
        "require_existing_instance": not first_bind, "operation_timeout_sec": 1,
    })
    assert client.sent[-1]["reason"] == "start_failed"
    assert client.sent[-1]["details"]["cleanup_confirmed"] is True
    assert proc.signals == [("terminate", 102.0)]
    assert command.call_count == int(first_bind)
    assert avd.exists() is not first_bind
    if not first_bind:
        assert (avd / "userdata.img").read_text() == "saved data"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [P.MSG_VM_STOP, P.MSG_VM_DELETE])
async def test_ws_default_cleanup_budget_includes_prior_lane_wait(isolated, kind):
    clock, _ = isolated
    client = AgentWSClient(ws_url="ws://unused", token="isolated", agent_id="isolated", agent_name="isolated")
    previous = asyncio.get_running_loop().create_future()
    client._vm_lanes[("android", "a")] = previous
    client._send_vm_failure = AsyncMock()
    handler = AsyncMock()
    message = {"type": kind, "vm_id": "a"}
    await client._dispatch_vm_handler(handler, message)
    tasks = list(client._vm_tasks)
    clock.advance(121)
    previous.set_result(None)
    await asyncio.gather(*tasks)
    handler.assert_not_called()
    assert client._send_vm_failure.call_args.args[1] == "operation_timeout"
    assert "operation_timeout_sec" not in message


@pytest.mark.asyncio
async def test_ws_explicit_remaining_budget_is_not_replaced_by_default(isolated):
    clock, _ = isolated
    client = AgentWSClient(ws_url="ws://unused", token="isolated", agent_id="isolated", agent_name="isolated")
    previous = asyncio.get_running_loop().create_future()
    client._vm_lanes[("android", "a")] = previous
    handler = AsyncMock()
    message = {"type": P.MSG_VM_STOP, "vm_id": "a", "operation_timeout_sec": 10}
    await client._dispatch_vm_handler(handler, message)
    tasks = list(client._vm_tasks)
    clock.advance(3)
    previous.set_result(None)
    await asyncio.gather(*tasks)
    assert handler.call_args.args[1]["operation_timeout_sec"] == 7
    assert message["operation_timeout_sec"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [P.MSG_VM_START, P.MSG_VM_CAPABILITY_PROBE, P.MSG_HARMONY_VM_STOP, P.MSG_IOS_SIM_VM_STOP])
async def test_ws_android_cleanup_default_does_not_change_other_messages(isolated, kind):
    client = AgentWSClient(ws_url="ws://unused", token="isolated", agent_id="isolated", agent_name="isolated")
    handler = AsyncMock()
    message = {"type": kind, "vm_id": "a"}
    await client._dispatch_vm_handler(handler, message)
    await asyncio.gather(*list(client._vm_tasks))
    assert handler.call_args.args[1] == message
    assert "operation_timeout_sec" not in message


@pytest.mark.asyncio
async def test_server_stop_forwards_the_existing_120_second_operation(session, monkeypatch):
    from ai_phone.server.android_vm import api
    from ai_phone.server.hub import Hub
    from ai_phone.server.models import AndroidVmInstance
    from ai_phone.server.vm_lifecycle import runtime, update_runtime

    vm = AndroidVmInstance(name="budget", alias="budget", state="running", assigned_agent_id="own-agent",
                           adb_serial="emulator-5554", api_level=33, abi="arm64")
    session.add(vm)
    await session.flush()
    update_runtime(vm, owner_host_id="own-host", instance_created=True)
    await session.commit()
    hub = Hub()
    monkeypatch.setattr(hub, "has_agent", lambda _: True)
    monkeypatch.setattr(hub, "agent_host_id", lambda _: "own-host")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(hub, "send_to_agent", send)
    monkeypatch.setattr(api, "arm_operation_timeout", lambda *_: None)
    await api.stop_instance(vm_id=vm.id, session=session, hub=hub)
    payload = send.call_args.args[1]
    assert payload["type"] == P.MSG_VM_STOP
    assert payload["operation_timeout_sec"] == runtime(vm)["operation"]["timeout_sec"] == 120
