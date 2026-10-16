"""One stop budget through real handlers/helpers; all native I/O and time are fake."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from ai_phone.agent import ws_client as ws_mod
from ai_phone.agent.drivers import ios_simulator as discovery
from ai_phone.agent.drivers import ios_simulator_wda as wda
from ai_phone.agent.ios_sim import manager as mod
from ai_phone.config import get_settings
from ai_phone.shared import protocol as P


VM = "budgetabc123"
UDID = "fixture-owned-udid"


class Clock:
    now = 100.0

    def advance(self, seconds):
        self.now += seconds


class Process:
    pid = 4242

    def __init__(self, clock, *, poll_seconds=0, terminate_seconds=0, wait_seconds=None):
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.terminate_seconds = terminate_seconds
        self.wait_seconds = wait_seconds
        self.signals, self.waits = [], []
        self.exited = False
        self.killed = False

    def poll(self):
        self.clock.advance(self.poll_seconds)
        return 0 if self.exited else None

    def terminate(self):
        self.signals.append(("terminate", self.clock.now))
        self.clock.advance(self.terminate_seconds)

    def kill(self):
        self.signals.append(("kill", self.clock.now))
        self.killed = True

    def wait(self, timeout):
        self.waits.append((timeout, self.clock.now))
        if self.wait_seconds is not None or self.killed:
            self.clock.advance(self.wait_seconds if self.wait_seconds is not None else 1)
            self.exited = True
            return 0
        self.clock.advance(timeout)
        raise wda.subprocess.TimeoutExpired("fixture xcodebuild", timeout)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()
    wda.reset_ports_for_tests()
    discovery.reset_managed_for_tests()
    clock = Clock()
    fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time, sleep=clock.advance)
    for module in (mod, wda, ws_mod):
        monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_kw: iter(()))
    monkeypatch.setattr(mod.IosSimVmManager, "_local_port_free", staticmethod(lambda _: True))
    events, delays = [], {}
    instances = {UDID: {"udid": UDID, "name": mod.managed_sim_name(VM), "state": "Booted",
                       "deviceTypeIdentifier": "fixture-phone", "isAvailable": True}}

    def native(*args, **kwargs):
        events.append({"args": args, "at": clock.now, "timeout": kwargs.get("timeout")})
        clock.advance(delays.get(args[0], 0))
        if args == ("list", "devices", "-j"):
            return json.dumps({"devices": {"fixture-runtime": list(instances.values())}})
        if args == ("shutdown", UDID):
            instances[UDID]["state"] = "Shutdown"
            return ""
        if args == ("terminate", UDID, "fixture.wda"):
            return ""
        raise AssertionError(f"Unexpected native call: {args}")

    monkeypatch.setattr(mod, "simctl_run", native)
    monkeypatch.setattr(wda, "simctl_run", native)
    manager = mod.IosSimVmManager(runtime_dir=tmp_path / "runtime")
    ports = wda.allocate_ports(UDID)
    launcher = wda.IosSimulatorWdaLauncher(UDID, ports=ports, expect_sim_name=mod.managed_sim_name(VM))
    launcher._runner_bundle_id = "fixture.wda"
    runtime = mod.SimVmRuntime(
        vm_id=VM, name=VM, udid=UDID, sim_name=mod.managed_sim_name(VM),
        device_type="fixture-phone", runtime_id="fixture-runtime", started_at=0,
        wda_port=ports.wda, mjpeg_port=ports.mjpeg, launcher=launcher, ready=True,
    )
    manager._runtimes[VM] = runtime
    discovery.mark_managed(UDID)
    monkeypatch.setattr(manager, "_refresh_devices_safe", AsyncMock())
    yield SimpleNamespace(clock=clock, manager=manager, runtime=runtime, launcher=launcher,
                          ports=ports, events=events, delays=delays, instances=instances)
    wda._forget_xcodebuild(UDID)
    wda.reset_ports_for_tests()
    discovery.reset_managed_for_tests()
    get_settings.cache_clear()


def assert_preserved(env):
    assert env.manager._runtimes[VM] is env.runtime
    assert env.runtime.udid == UDID and env.runtime.launcher is env.launcher
    assert discovery.is_managed(UDID)
    assert wda.peek_ports(UDID) == env.ports
    assert wda._load_reservations()[UDID] == env.ports.wda


def attach_process(env, **kwargs):
    process = Process(env.clock, **kwargs)
    env.launcher._xcodebuild_proc = process
    wda._remember_xcodebuild(UDID, process)
    return process


def assert_timeout(env, result):
    assert result["ok"] is False and result["reason"] == "operation_timeout"
    assert result["udid"] == UDID
    assert result["details"]["cleanup_confirmed"] is False
    assert result["details"]["instance_observation"]["state"] == "unknown"
    assert_preserved(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["none", "progress-send", "executor-queue", "instance-lock"])
async def test_ws_stop_never_starts_native_after_original_budget(isolated, monkeypatch, phase):
    env = isolated
    received, sent = [], []
    if phase == "instance-lock":
        underlying = threading.RLock()

        class DelayedLock:
            delayed = False

            def acquire(self, blocking=True, timeout=-1):
                if blocking and not self.delayed:
                    self.delayed = True
                    env.clock.advance(2)
                return underlying.acquire(blocking, timeout)

            def release(self):
                underlying.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_args):
                self.release()

        lock = DelayedLock()
        monkeypatch.setattr(env.manager, "_vm_lock", lambda _: lock)

    async def to_thread(function, *args, **kwargs):
        if phase == "executor-queue":
            env.clock.advance(2)
        return function(*args, **kwargs)

    monkeypatch.setattr(mod.asyncio, "to_thread", to_thread)
    client = ws_mod.AgentWSClient(ws_url="ws://fixture.invalid", token="fixture", agent_id="fixture", agent_name="fixture")

    async def send(payload):
        sent.append(payload)
        if phase == "progress-send" and payload.get("state") == "stopping":
            env.clock.advance(2)
        return True

    monkeypatch.setattr(client, "send", send)

    async def handler(current_client, message):
        received.append(message["operation_timeout_sec"])
        await env.manager.handle_stop(current_client, message)

    await client._dispatch_vm_handler(handler, {
        "type": P.MSG_IOS_SIM_VM_STOP, "vm_id": VM, "udid": UDID,
        "request_id": "fixture-stop", "operation_timeout_sec": 5.0,
    })
    tasks = list(client._vm_tasks)
    env.clock.advance(4)
    await asyncio.gather(*tasks)
    assert received == [1.0]
    if phase == "none":
        assert sent[-1]["ok"] is True
        assert all(event["at"] < 105 for event in env.events)
        assert all(event["timeout"] <= 1 for event in env.events)
        assert VM not in env.manager._runtimes
        assert wda._load_reservations()[UDID] == env.ports.wda
    else:
        assert env.events == [], f"Expired attempt still issued native commands: {env.events}"
        assert_preserved(env)
        assert sent[-1]["ok"] is False and sent[-1]["reason"] == "operation_timeout"
        assert sent[-1]["details"]["cleanup_confirmed"] is False
        assert sent[-1]["details"]["instance_observation"]["state"] == "unknown"
        assert len([payload for payload in sent if payload.get("state") == "error"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, 0, -1, "invalid", float("inf"), float("nan")])
async def test_invalid_stop_budget_has_no_native_side_effect(isolated, budget):
    env = isolated
    client = SimpleNamespace(send=AsyncMock())
    await env.manager.handle_stop(client, {"vm_id": VM, "udid": UDID, "operation_timeout_sec": budget})
    payload = client.send.call_args.args[0]
    assert payload["ok"] is False and payload["reason"] == "bad_request"
    assert payload["details"]["cleanup_confirmed"] is False
    assert client.send.call_count == 1 and env.events == []
    assert_preserved(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_deadline", [-1, float("nan"), 10**12])
async def test_stop_ignores_network_monotonic_fields(isolated, wire_deadline):
    env = isolated
    message = {"vm_id": VM, "udid": UDID, "operation_timeout_sec": 1,
               "_operation_deadline": wire_deadline, "_monotonic": 10**12}
    client = SimpleNamespace(send=AsyncMock())
    await env.manager.handle_stop(client, message)
    assert client.send.call_args.args[0]["ok"] is True
    assert all(event["timeout"] <= 1 for event in env.events)
    assert message["_operation_deadline"] is wire_deadline


def test_stop_lock_wait_is_bounded_and_failed_acquire_does_not_release(isolated, monkeypatch):
    env = isolated
    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == 1
            env.clock.advance(timeout)
            return False

        def release(self):
            pytest.fail("unowned lock must not be released")
    monkeypatch.setattr(env.manager, "_vm_lock", lambda _: BusyLock())
    assert_timeout(env, env.manager.stop_sync(VM, UDID, deadline=101))
    assert env.events == []


def test_native_calls_only_receive_remaining_stop_budget(isolated):
    env = isolated
    env.delays.update(list=1, terminate=2, shutdown=3)
    result = env.manager.stop_sync(VM, UDID, deadline=110)
    assert result["ok"] is True
    assert [event["timeout"] for event in env.events] == [10, 9, 7, 4, 3]
    assert [event["at"] for event in env.events] == [100, 101, 103, 106, 107]
    assert env.clock.now == 108
    assert wda._load_reservations()[UDID] == env.ports.wda


@pytest.mark.parametrize("phase", ["identity", "wda", "shutdown", "verification"])
def test_expired_native_stage_cannot_start_following_mutation_or_forget(isolated, monkeypatch, phase):
    env = isolated
    if phase == "verification":
        original = mod.simctl_run
        reads = 0
        def native(*args, **kwargs):
            nonlocal reads
            result = original(*args, **kwargs)
            if args[0] == "list":
                reads += 1
                if reads == 2:
                    env.clock.advance(2)
            return result
        monkeypatch.setattr(mod, "simctl_run", native)
    else:
        env.delays[{"identity": "list", "wda": "terminate", "shutdown": "shutdown"}[phase]] = 2
    result = env.manager.stop_sync(VM, UDID, deadline=101)
    assert_timeout(env, result)
    expected = {"identity": ["list"], "wda": ["list", "terminate"],
                "shutdown": ["list", "terminate", "shutdown"],
                "verification": ["list", "terminate", "shutdown", "list"]}
    assert [event["args"][0] for event in env.events] == expected[phase]
    assert all(event["at"] < 101 and event["timeout"] <= 1 for event in env.events)


@pytest.mark.parametrize("state", ["Shutdown", "missing"])
def test_already_stopped_or_missing_guest_does_not_forget_after_wda_timeout(isolated, state):
    env = isolated
    if state == "missing":
        env.instances.pop(UDID)
    else:
        env.instances[UDID]["state"] = state
    env.delays["terminate"] = 2
    assert_timeout(env, env.manager.stop_sync(VM, UDID, deadline=101))
    assert [event["args"][0] for event in env.events] == ["list", "terminate"]


@pytest.mark.parametrize("kwargs,signals,waits", [
    ({"poll_seconds": 2}, [], []),
    ({"terminate_seconds": 2}, [("terminate", 100)], []),
    ({}, [("terminate", 100)], [(1, 100)]),
    ({"wait_seconds": 2}, [("terminate", 100)], [(1, 100)]),
])
def test_xcodebuild_timeout_never_escalates_or_drops_owned_handle(isolated, kwargs, signals, waits):
    env = isolated
    process = attach_process(env, **kwargs)
    assert_timeout(env, env.manager.stop_sync(VM, UDID, deadline=101))
    assert process.signals == signals and process.waits == waits
    assert env.launcher._xcodebuild_proc is process and wda._XCODEBUILD_PROCS[UDID] is process
    assert [event["args"][0] for event in env.events] == ["list"]


def test_xcodebuild_can_escalate_and_stop_when_budget_remains(isolated):
    env = isolated
    process = attach_process(env)
    result = env.manager.stop_sync(VM, UDID, deadline=120)
    assert result["ok"] is True and env.instances[UDID]["state"] == "Shutdown"
    assert process.signals == [("terminate", 100), ("kill", 110)]
    assert process.waits == [(10, 100), (5, 110)]
    assert env.launcher._xcodebuild_proc is None and UDID not in wda._XCODEBUILD_PROCS
    shutdown = next(event for event in env.events if event["args"][0] == "shutdown")
    assert shutdown["timeout"] == 9 and shutdown["at"] == 111


def test_identity_conflict_never_stops_another_instance(isolated):
    env = isolated
    env.instances["foreign"] = {**env.instances[UDID], "udid": "foreign", "name": "personal-simulator"}
    result = env.manager.stop_sync(VM, "foreign", deadline=105)
    assert result["ok"] is False and result["reason"] == "udid_name_mismatch"
    assert all(event["args"][0] == "list" for event in env.events)
    assert_preserved(env)


@pytest.mark.parametrize("internal", [False, True])
def test_legacy_direct_stop_keeps_original_native_timeouts(isolated, internal):
    env = isolated
    stop = env.manager._stop_sync_locked if internal else env.manager.stop_sync
    assert stop(VM, UDID)["ok"] is True
    expected = [30, 15, 60, 30] + ([] if internal else [5])
    assert [event["timeout"] for event in env.events] == expected
    assert wda._load_reservations()[UDID] == env.ports.wda


def test_legacy_wda_error_still_confirms_guest_without_forgetting_owned_handle(isolated):
    env = isolated
    process = attach_process(env)
    def failed_terminate():
        raise TimeoutError("fixture process operation failed")
    process.terminate = failed_terminate
    result = env.manager._stop_sync_locked(VM, UDID)
    assert result["ok"] is False and result["reason"] == "stop_failed"
    assert env.instances[UDID]["state"] == "Shutdown"
    assert env.launcher._xcodebuild_proc is process and wda._XCODEBUILD_PROCS[UDID] is process
    assert [event["args"][0] for event in env.events] == ["list", "shutdown", "list"]
    assert_preserved(env)
