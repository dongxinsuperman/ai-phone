import pytest

from ai_phone.agent.health.probe import ProbeOutcome
from ai_phone.agent.health.supervisor import (
    ReadinessSupervisor,
    _State,
)


@pytest.mark.asyncio
async def test_readiness_send_failure_is_not_deduped():
    calls = []

    async def sender(msg):
        calls.append(msg)
        return False

    sup = ReadinessSupervisor(device_lister=lambda: [], send_message=sender)
    state = _State()
    state.ready = True
    state.reason = None

    await sup._maybe_send(("S1", "android"), state)
    await sup._maybe_send(("S1", "android"), state)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_readiness_send_success_is_deduped():
    calls = []

    async def sender(msg):
        calls.append(msg)
        return True

    sup = ReadinessSupervisor(device_lister=lambda: [], send_message=sender)
    state = _State()
    state.ready = True
    state.reason = None

    await sup._maybe_send(("S1", "android"), state)
    await sup._maybe_send(("S1", "android"), state)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_readiness_mark_all_dirty_forces_resend_after_reconnect():
    calls = []

    async def sender(msg):
        calls.append(msg)
        return True

    sup = ReadinessSupervisor(device_lister=lambda: [], send_message=sender)
    state = _State()
    state.ready = True
    state.reason = None

    await sup._maybe_send(("S1", "ios"), state)
    await sup._maybe_send(("S1", "ios"), state)
    sup.mark_all_dirty()
    await sup._maybe_send(("S1", "ios"), state)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_readiness_same_state_is_resent_after_interval(monkeypatch):
    calls = []
    wall_now = 1000.0
    mono_now = 2000.0

    def fake_time():
        return wall_now

    def fake_monotonic():
        return mono_now

    async def sender(msg):
        calls.append(msg)
        return True

    monkeypatch.setattr(
        "ai_phone.agent.health.supervisor.time.time",
        fake_time,
    )
    monkeypatch.setattr(
        "ai_phone.agent.health.supervisor.time.monotonic",
        fake_monotonic,
    )
    sup = ReadinessSupervisor(
        device_lister=lambda: [],
        send_message=sender,
        resend_interval_sec=30.0,
    )
    state = _State()
    state.ready = False
    state.reason = "adb_offline"

    await sup._maybe_send(("S1", "android"), state)
    await sup._maybe_send(("S1", "android"), state)
    assert len(calls) == 1

    mono_now = 2031.0
    await sup._maybe_send(("S1", "android"), state)

    assert len(calls) == 2
    assert calls[1]["ready"] is False
    assert calls[1]["not_ready_reason"] == "adb_offline"


class _StubProbe:
    def __init__(self, outcomes):
        self._outcomes = outcomes

    async def probe(self):
        return self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]


@pytest.mark.asyncio
async def test_harmony_probes_are_serialized_within_one_tick(monkeypatch):
    """两台 Harmony 共用同一 hdc server，readiness 不得并发打 hdc。"""
    active = 0
    overlap = False
    completed = []

    class TrackingProbe:
        def __init__(self, serial):
            self.serial = serial

        async def probe(self):
            nonlocal active, overlap
            active += 1
            overlap = overlap or active > 1
            await __import__("asyncio").sleep(0.01)
            completed.append(self.serial)
            active -= 1
            return ProbeOutcome(ready=True)

    monkeypatch.setattr(
        "ai_phone.agent.health.supervisor.build_probe_for",
        lambda _platform, serial, timeout_sec: TrackingProbe(serial),  # noqa: ARG005
    )

    sup = ReadinessSupervisor(
        device_lister=lambda: [("H1", "harmony"), ("H2", "harmony")],
        send_message=lambda _msg: _true(),
    )
    await sup._tick_once()

    assert overlap is False
    assert completed == ["H1", "H2"]


def _install_stub_probe(monkeypatch, probed, outcome_factory):
    def build(platform, serial, timeout_sec):  # noqa: ARG001
        probed.append(serial)
        return _StubProbe([outcome_factory(serial)])

    monkeypatch.setattr("ai_phone.agent.health.supervisor.build_probe_for", build)


def _freeze_monotonic(monkeypatch, clock):
    monkeypatch.setattr(
        "ai_phone.agent.health.supervisor.time.monotonic", lambda: clock[0]
    )


@pytest.fixture
def ios_scan_ok(monkeypatch):
    """让 iOS USB 扫描短路分支不触发。

    CI / 开发机没有 usbmuxd，was_last_ios_scan_ok() 恒为 False，会让 _tick_once
    在 probe 之前就把 iOS 设备全部短路掉，测不到 probe 之后的逻辑。
    """
    monkeypatch.setattr(
        "ai_phone.agent.drivers.ios.was_last_ios_scan_ok", lambda: True
    )


async def _true():
    return True


# ---------------------------------------------------------------------------
# 卡死自愈：连续探不通到阈值时通知回调，由回调决定动不动手
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_self_heal_fires_once_at_threshold(monkeypatch, ios_scan_ok):
    """只在跨过阈值那一次调用，不是每轮都调。

    自愈动作有代价（停 WDA 再拉起），反复触发只会让一台正在恢复中的设备一直被
    打断。
    """
    from ai_phone.agent.health import supervisor as sup_mod

    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda _s: ProbeOutcome(ready=False, not_ready_reason="wda_not_ready"),
    )
    calls: list[tuple] = []

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios_sim")],
        send_message=lambda msg: _true(),
        self_heal=lambda serial, platform: (calls.append((serial, platform)), False)[1],
    )
    # 跑足够多轮，跨过阈值后继续跑
    for _ in range(40):
        await sup._tick_once()
        clock[0] += 30.0

    assert len(calls) == 1, f"自愈被触发了 {len(calls)} 次，应该只有 1 次"
    assert calls[0] == ("S1", "ios_sim")
    assert sup._states[("S1", "ios_sim")].consecutive_fail >= (
        sup_mod._SELF_HEAL_AFTER_FAILS
    )


@pytest.mark.asyncio
async def test_self_heal_success_resets_fail_count(monkeypatch, ios_scan_ok):
    """自愈说「我处理了」→ 计数清零，从头开始攒，不会下一轮又触发一次。"""
    from ai_phone.agent.health import supervisor as sup_mod

    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda _s: ProbeOutcome(ready=False, not_ready_reason="wda_not_ready"),
    )

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios_sim")],
        send_message=lambda msg: _true(),
        self_heal=lambda serial, platform: True,
    )
    for _ in range(sup_mod._SELF_HEAL_AFTER_FAILS):
        await sup._tick_once()
        clock[0] += 5.0

    state = sup._states[("S1", "ios_sim")]
    assert state.consecutive_fail == 0, "自愈成功后计数没清零"


@pytest.mark.asyncio
async def test_self_heal_retries_after_a_while_if_still_broken(
    monkeypatch, ios_scan_ok
):
    """自愈后设备还是不通 → 计数重新累积，过一段时间再试一次。

    不是每轮都试（那会把恢复中的设备一直打断），也不是只试一次就放弃。
    """
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda _s: ProbeOutcome(ready=False, not_ready_reason="wda_not_ready"),
    )
    calls: list[str] = []

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios_sim")],
        send_message=lambda msg: _true(),
        self_heal=lambda serial, platform: (calls.append(serial), True)[1],
    )
    for _ in range(30):
        await sup._tick_once()
        clock[0] += 30.0

    assert 2 <= len(calls) <= 6, f"重试节奏不合理：30 轮里试了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_self_heal_exception_does_not_break_the_loop(monkeypatch, ios_scan_ok):
    """自愈回调抛异常不能把巡检带崩——它只是个附加动作。"""
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda _s: ProbeOutcome(ready=False, not_ready_reason="wda_not_ready"),
    )

    def _boom(_serial, _platform):
        raise RuntimeError("自愈炸了")

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios_sim")],
        send_message=lambda msg: _true(),
        self_heal=_boom,
    )
    for _ in range(10):
        await sup._tick_once()
        clock[0] += 30.0
    # 不抛即通过，且探测照常继续
    assert len(probed) >= 6


@pytest.mark.asyncio
async def test_no_self_heal_callback_is_fine(monkeypatch, ios_scan_ok):
    """不传回调时行为与从前完全一致。"""
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda _s: ProbeOutcome(ready=False, not_ready_reason="wda_not_ready"),
    )
    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios")],
        send_message=lambda msg: _true(),
    )
    for _ in range(10):
        await sup._tick_once()
        clock[0] += 30.0


@pytest.mark.asyncio
async def test_every_device_is_probed_every_tick(monkeypatch, ios_scan_ok):
    """一台设备探不通不影响同 Agent 上其它设备的探测节奏。"""
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda s: ProbeOutcome(ready=True)
        if s == "GOOD"
        else ProbeOutcome(ready=False, not_ready_reason="driver_probe_failed"),
    )

    sup = ReadinessSupervisor(
        device_lister=lambda: [("GOOD", "ios"), ("BAD", "ios")],
        send_message=lambda msg: _true(),
    )
    for _ in range(6):
        await sup._tick_once()
        clock[0] += 5.0

    assert probed.count("GOOD") == 6
    assert probed.count("BAD") == 6
