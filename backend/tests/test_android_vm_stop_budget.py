"""One-budget Android cleanup; fake time/processes only, never host devices."""
from __future__ import annotations

import asyncio
import re
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
        monkeypatch.setattr(manager, "_sync_guest", lambda *_: True)
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
    monkeypatch.setattr(manager, "_delete_avd", lambda *_: True)
    assert manager.delete_sync("a")["ok"] is True
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


def test_destroy_rejected_native_command_keeps_existing_owned_force_path(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    monkeypatch.setattr(manager, "_kill_emulator", lambda *_: False)
    monkeypatch.setattr(manager, "_delete_avd", lambda *_: True)
    assert manager.delete_sync("a")["ok"] is True
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
    popen = Mock(return_value=proc)
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    find_live = mod._find_avd_processes
    monkeypatch.setattr(mod, "_find_avd_processes", lambda name: find_live(name) if popen.called else ([], True))

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
    assert client.sent[-1]["details"]["cleanup_confirmed"] is first_bind
    assert proc.signals == ([("terminate", 102.0)] if first_bind else [])
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


def sync_marker(args):
    assert args[:4] == ["/isolated/adb", "-s", "emulator-5554", "shell"]
    match = re.fullmatch(r"/system/bin/sync && printf '%s\\n' '([0-9a-f]{32})'", args[4])
    assert match, args
    return match.group(1)


def test_normal_stop_sync_marker_precedes_original_exit_and_rechecks_identity(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc], commands=False)
    calls = []

    def run(args, **kwargs):
        if args[-3:] == ["emu", "avd", "name"]:
            calls.append("identity")
            return SimpleNamespace(returncode=0, stdout="aiphone_vm_a\nOK\n", stderr="")
        if args[3] == "shell":
            calls.append("sync")
            assert kwargs["timeout"] == 120
            clock.advance(2)
            return SimpleNamespace(returncode=0, stdout=sync_marker(args) + "\n", stderr="")
        assert args[-2:] == ["emu", "kill"]
        calls.append("kill")
        proc.exit_at = clock.now + 1
        return SimpleNamespace(returncode=0, stdout=b"OK", stderr=b"")

    monkeypatch.setattr(mod.subprocess, "run", run)
    result = manager.stop_sync("a")
    assert calls == ["identity", "sync", "identity", "kill"]
    assert result["ok"] is True
    assert result["details"]["sync_completed"] is True
    assert proc.signals == []
    assert clock.now == 103


@pytest.mark.parametrize("reply", ["empty", "wrong", "extra", "nonzero", "timeout", "late"])
def test_unconfirmed_sync_never_closes_or_forces_instance(isolated, monkeypatch, tmp_path, reply):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc], commands=False)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    kill = Mock(side_effect=AssertionError("no emulator close after unconfirmed sync"))
    monkeypatch.setattr(manager, "_kill_emulator", kill)

    def run(args, **kwargs):
        marker = sync_marker(args)
        if reply == "timeout":
            clock.advance(kwargs["timeout"])
            raise mod.subprocess.TimeoutExpired(args, kwargs["timeout"])
        if reply == "late":
            clock.advance(kwargs["timeout"] + 1)
        out = {"empty": "", "wrong": "previous-marker\n", "extra": marker + "\nextra\n"}.get(reply, marker + "\n")
        return SimpleNamespace(returncode=1 if reply == "nonzero" else 0, stdout=out, stderr="")

    command = Mock(side_effect=run)
    monkeypatch.setattr(mod.subprocess, "run", command)
    result = manager.stop_sync("a", deadline=130)
    assert result["ok"] is False
    assert result["reason"] == ("operation_timeout" if reply in {"timeout", "late"} else "stop_unconfirmed")
    assert result["details"]["cleanup_confirmed"] is False
    if reply not in {"timeout", "late"}:
        assert result["details"]["sync_completed"] is False
        assert result["error"] == "未确认本次系统数据同步完成，未执行停止"
    assert command.call_count == 1
    kill.assert_not_called()
    assert not proc.signals and not proc.waits
    assert manager._runtimes["a"] is rt


def test_previous_success_marker_cannot_ack_a_new_sync_attempt(isolated, monkeypatch, tmp_path):
    clock, tools = isolated
    manager = mod.AndroidVmManager(runtime_dir=tmp_path)
    markers = []

    def run(args, **_kwargs):
        markers.append(sync_marker(args))
        return SimpleNamespace(returncode=0, stdout=markers[0] + "\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert manager._sync_guest(tools, "emulator-5554", clock.now + 30) is True
    assert manager._sync_guest(tools, "emulator-5554", clock.now + 30) is False
    assert markers[0] != markers[1]


def test_identity_changed_after_sync_never_receives_kill_or_force(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc], commands=False)
    identity = Mock(side_effect=["aiphone_vm_a", "aiphone_vm_other"])
    monkeypatch.setattr(manager, "_emulator_avd_name", identity)
    monkeypatch.setattr(mod.subprocess, "run", lambda args, **_kw: SimpleNamespace(returncode=0, stdout=sync_marker(args) + "\n", stderr=""))
    kill = Mock(side_effect=AssertionError("replacement serial must not be killed"))
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    result = manager.stop_sync("a")
    assert result["ok"] is False
    assert result["details"]["sync_completed"] is True
    assert identity.call_count == 2
    assert not proc.signals and not proc.waits
    assert manager._runtimes["a"] is rt
    kill.assert_not_called()


@pytest.mark.parametrize("unresolved_port", [5556, 0])
def test_unresolved_second_process_is_not_discarded_to_fake_unique_sync_target(isolated, monkeypatch, tmp_path, unresolved_port):
    clock, _ = isolated
    first, second = Process(clock), Process(clock, pid=72, port=unresolved_port)
    manager, rt = managed(monkeypatch, tmp_path, clock, [first, second], commands=False)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda _tools, serial, *_: "aiphone_vm_a" if serial == "emulator-5554" else "")
    sync = Mock(side_effect=AssertionError("ambiguous process list must not sync"))
    kill = Mock(side_effect=AssertionError("ambiguous process list must not close"))
    monkeypatch.setattr(manager, "_sync_guest", sync)
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    result = manager.stop_sync("a")
    assert result["reason"] == "stop_unconfirmed"
    assert manager._runtimes["a"] is rt
    assert first.signals == second.signals == []
    sync.assert_not_called()
    kill.assert_not_called()


@pytest.mark.parametrize("serial", ["USB-physical", "192.0.2.10:5555"])
def test_physical_serial_cannot_enter_guest_sync_path(isolated, monkeypatch, tmp_path, serial):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [], commands=False)
    rt.process, rt.adb_serial, rt.port = proc, serial, 0
    sync = Mock(side_effect=AssertionError("physical device must not be synchronized"))
    monkeypatch.setattr(manager, "_sync_guest", sync)
    assert manager.stop_sync("a")["ok"] is False
    sync.assert_not_called()
    assert not proc.signals


def test_stop_all_requires_normal_sync_and_does_not_destroy_on_failure(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc])
    sync = Mock(return_value=False)
    kill = Mock(side_effect=AssertionError("stop_all cannot skip sync"))
    monkeypatch.setattr(manager, "_sync_guest", sync)
    monkeypatch.setattr(manager, "_kill_emulator", kill)
    assert manager.stop_all() == 0
    assert manager._runtimes["a"] is rt
    assert not proc.signals
    sync.assert_called_once()
    kill.assert_not_called()


def test_already_stopped_is_idempotent_without_adb_or_sync(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    manager, _ = managed(monkeypatch, tmp_path, clock, [], commands=False)
    monkeypatch.setattr(mod, "find_android_tools", lambda: (None, ["offline"]))
    sync = Mock(side_effect=AssertionError("already stopped requires no guest command"))
    monkeypatch.setattr(manager, "_sync_guest", sync)
    result = manager.stop_sync("a")
    assert result["ok"] is True
    assert result["details"]["cleanup_confirmed"] is True
    assert "sync_completed" not in result["details"]
    assert manager._runtimes == {}
    sync.assert_not_called()


def test_explicit_delete_skips_guest_sync_but_still_confirms_owned_exit(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, _ = managed(monkeypatch, tmp_path, clock, [proc])
    sync = Mock(side_effect=AssertionError("destroy does not require a functioning guest"))
    monkeypatch.setattr(manager, "_sync_guest", sync)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "")
    deletion = Mock(return_value=True)
    monkeypatch.setattr(manager, "_delete_avd", deletion)
    assert manager.delete_sync("a")["ok"] is True
    assert proc.signals == [("terminate", 100.0)]
    sync.assert_not_called()
    deletion.assert_called_once()


@pytest.mark.asyncio
async def test_stop_message_cannot_opt_into_destroy_or_supply_completion_marker(isolated, monkeypatch, tmp_path):
    clock, _ = isolated
    proc = Process(clock)
    manager, rt = managed(monkeypatch, tmp_path, clock, [proc], commands=False)
    monkeypatch.setattr(manager, "_emulator_avd_name", lambda *_: "aiphone_vm_a")
    commands = []

    def run(args, **_kwargs):
        sync_marker(args)
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", run)
    client = Client()
    await manager.handle_stop(client, {"vm_id": "a", "request_id": "untrusted-$(touch never)",
                                      "destroy": True, "force": True, "sync_marker": "fake"})
    assert client.sent[-1]["reason"] == "stop_unconfirmed"
    assert len(commands) == 1
    assert "untrusted" not in commands[0][-1] and "fake" not in commands[0][-1]
    assert manager._runtimes["a"] is rt
    assert not proc.signals
