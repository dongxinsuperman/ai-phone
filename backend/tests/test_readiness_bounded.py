"""Native probe and recovery bounds, with every external operation replaced."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from ai_phone.agent.health import probe as probe_module
from ai_phone.agent.health.probe import BaseProbe, ProbeOutcome
from ai_phone.agent.health import supervisor as supervisor_module
from ai_phone.agent.health.supervisor import ReadinessSupervisor, _State


class NativeProbe(BaseProbe):
    async def _do_probe(self):
        return await self._run_sync_probe()


@pytest.mark.asyncio
async def test_timeout_never_resubmits_same_native_probe_until_it_exits():
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Probe(NativeProbe):
        def _probe_sync(self):
            calls.append(True)
            entered.set()
            assert release.wait(2)
            return ProbeOutcome(ready=True)

    probe = Probe("A1", timeout_sec=0.01)
    try:
        assert (await probe.probe()).ready is False
        assert entered.is_set()
        for _ in range(10):
            assert (await probe.probe()).ready is False
        assert len(calls) == 1
    finally:
        release.set()
        await probe._pending
    # The timed-out success itself is discarded; readiness needs a new check.
    assert (await probe.probe()).ready is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_probe_pool_counts_native_work_after_async_timeout(monkeypatch):
    release = threading.Event()
    slots = threading.BoundedSemaphore(2)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(probe_module, "_PROBE_EXECUTOR", executor)
    monkeypatch.setattr(probe_module, "_PROBE_SLOTS", slots)

    class Probe(NativeProbe):
        def _probe_sync(self):
            assert release.wait(2)
            return ProbeOutcome(ready=True)

    probes = [Probe(str(i), timeout_sec=0.01) for i in range(3)]
    try:
        outcomes = await asyncio.gather(*(probe.probe() for probe in probes))
        assert all(not outcome.ready for outcome in outcomes)
        assert "工作池繁忙" in outcomes[2].hint
        assert slots.acquire(blocking=False) is False
    finally:
        release.set()
        await asyncio.gather(*(probe._pending for probe in probes))
        executor.shutdown(wait=True)
    assert slots.acquire(blocking=False) is True
    slots.release()


def test_android_native_shells_share_one_deadline(monkeypatch):
    clock = [10.0]
    timeouts = []
    commands = []

    class Device:
        def shell(self, command, *, timeout):
            commands.append(command)
            timeouts.append(timeout)
            clock[0] += 0.8
            if command == "echo ok":
                return "ok"
            if "dumpsys power" in command:
                return "Display Power: state=ON"
            return "mDreamingLockscreen=true"

    monkeypatch.setattr(probe_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("adbutils.adb", SimpleNamespace(device=lambda **_: Device()))
    monkeypatch.setattr(probe_module, "get_settings", lambda: SimpleNamespace(
        android_screen_off_dispatchable=True, android_wake_before_run=True,
    ))
    result = probe_module.AndroidProbe("A1", timeout_sec=2.0)._probe_sync()
    assert result.ready is False
    assert len(commands) == 3  # No fourth native shell after the budget expired.
    assert timeouts == pytest.approx([2.0, 1.2, 0.4])


def test_ios_status_and_locked_share_one_deadline(monkeypatch):
    from ai_phone.agent.drivers import ios

    clock = [10.0]
    timeouts = []
    monkeypatch.setattr(probe_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ios, "_WDA_CLIENT_MAP", {"I1": SimpleNamespace(base_url="http://test")})

    def get(_url, *, timeout):
        timeouts.append(timeout)
        clock[0] += 1.5
        return SimpleNamespace(status_code=200, json=lambda: {"value": False})

    monkeypatch.setattr("httpx.get", get)
    assert probe_module.IosProbe("I1", timeout_sec=2.0)._probe_sync().ready is True
    assert timeouts == pytest.approx([2.0, 0.5])


@pytest.mark.asyncio
async def test_ready_device_is_published_before_slow_peer_finishes(monkeypatch):
    release, good_reported = asyncio.Event(), asyncio.Event()
    sent = []

    class Probe:
        def __init__(self, serial):
            self.serial = serial

        async def probe(self):
            if self.serial == "slow":
                await release.wait()
            return ProbeOutcome(ready=True)

    async def send(msg):
        sent.append(msg)
        if msg["serial"] == "good":
            good_reported.set()
        return True

    monkeypatch.setattr(supervisor_module, "build_probe_for", lambda _p, s, **_: Probe(s))
    sup = ReadinessSupervisor(
        device_lister=lambda: [("slow", "android"), ("good", "android")], send_message=send,
    )
    task = asyncio.create_task(sup._tick_once())
    try:
        await asyncio.wait_for(good_reported.wait(), 1)
        assert not task.done()
        assert [m["serial"] for m in sent] == ["good"]
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_disappeared_device_does_not_receive_late_ready(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    devices = [("A1", "android")]
    sent = []

    class Probe:
        async def probe(self):
            entered.set()
            await release.wait()
            return ProbeOutcome(ready=True)

    async def send(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr(supervisor_module, "build_probe_for", lambda *_, **__: Probe())
    sup = ReadinessSupervisor(device_lister=lambda: list(devices), send_message=send)
    task = asyncio.create_task(sup._tick_once())
    await entered.wait()
    devices.clear()
    release.set()
    await task
    assert sent == []


@pytest.mark.parametrize("changed", ["identity", "generation"])
@pytest.mark.asyncio
async def test_replaced_vm_does_not_receive_previous_probe_result(monkeypatch, changed):
    entered, release = asyncio.Event(), asyncio.Event()
    identity = {"identity": "vm-A", "generation": "boot-1"}
    sent = []

    class Probe:
        async def probe(self):
            entered.set()
            await release.wait()
            return ProbeOutcome(ready=True)

    async def send(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr(supervisor_module, "build_probe_for", lambda *_, **__: Probe())
    sup = ReadinessSupervisor(
        device_lister=lambda: [("emulator-5554", "android")], send_message=send,
        device_identity=lambda *_: identity["identity"],
        device_generation=lambda *_: identity["generation"],
    )
    task = asyncio.create_task(sup._tick_once())
    await entered.wait()
    identity[changed] = "new"
    release.set()
    await task
    assert sent == []
    await sup._tick_once()
    assert sent[0]["vm_instance_id"] == identity["identity"]
    assert sent[0]["ready"] is True


@pytest.mark.asyncio
async def test_physical_readiness_keeps_existing_protocol():
    sent = []

    async def send(msg):
        sent.append(msg)
        return True

    sup = ReadinessSupervisor(
        device_lister=lambda: [], send_message=send, device_identity=lambda *_: "",
    )
    await sup._maybe_send(("physical", "android"), _State())
    assert "vm_instance_id" not in sent[0]


@pytest.mark.asyncio
async def test_failed_probe_of_retained_scan_downgrades_without_grace(monkeypatch):
    monkeypatch.setattr("ai_phone.agent.drivers.was_last_device_scan_ok", lambda _: False)
    sent = []

    async def send(msg):
        sent.append(msg)
        return True

    sup = ReadinessSupervisor(device_lister=lambda: [], send_message=send)
    state = _State()
    state.ready = state.ever_ready = True
    state.reason = None
    sup._states[("A1", "android")] = state
    await sup._apply_outcome(
        ("A1", "android"), ProbeOutcome(False, "adb_offline"), fail_threshold=3,
    )
    assert sent[0]["ready"] is False
    assert sent[0]["fail_streak"] == 1


@pytest.mark.asyncio
async def test_self_heal_is_single_flight_and_does_not_block_other_devices(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    repairs = []
    sent = []

    class Probe:
        def __init__(self, serial):
            self.serial = serial

        async def probe(self):
            return ProbeOutcome(self.serial == "good", "wda_not_ready")

    def repair(serial, platform):
        repairs.append((serial, platform))
        entered.set()
        assert release.wait(2)
        return True

    async def send(msg):
        sent.append(msg)
        return True

    monkeypatch.setattr(supervisor_module, "build_probe_for", lambda _p, s, **_: Probe(s))
    sup = ReadinessSupervisor(
        device_lister=lambda: [("bad", "ios_sim"), ("good", "android")],
        send_message=send, self_heal=repair,
    )
    state = _State()
    state.consecutive_fail = supervisor_module._SELF_HEAL_AFTER_FAILS - 1
    sup._states[("bad", "ios_sim")] = state
    try:
        await sup._tick_once()
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(3):
            sup.mark_all_dirty()
            await sup._tick_once()
        assert repairs == [("bad", "ios_sim")]
        assert len([m for m in sent if m["serial"] == "good"]) == 4
    finally:
        release.set()
        await asyncio.gather(*sup._heal_tasks.values())
    assert state.consecutive_fail == 0


@pytest.mark.asyncio
async def test_queued_repair_does_not_repair_a_replacement_instance():
    entered, release = threading.Event(), threading.Event()
    identities = {"A": "old-A", "B": "old-B"}
    calls = []

    def repair(serial, _platform):
        calls.append(serial)
        if serial == "A":
            entered.set()
            assert release.wait(2)
        return True

    async def send(_msg):
        return True

    sup = ReadinessSupervisor(
        device_lister=lambda: [], send_message=send, self_heal=repair,
        device_identity=lambda serial, _: identities[serial],
    )
    for serial in ("A", "B"):
        state = _State()
        state.consecutive_fail = supervisor_module._SELF_HEAL_AFTER_FAILS
        sup._states[(serial, "ios_sim")] = state
        sup._maybe_self_heal((serial, "ios_sim"), state)
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        identities["B"] = "new-B"
    finally:
        release.set()
        await asyncio.gather(*sup._heal_tasks.values())
    assert calls == ["A"]
