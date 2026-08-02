import pytest

from ai_phone.agent.health.probe import ProbeOutcome
from ai_phone.agent.health.supervisor import (
    _BACKOFF_MAX_SEC,
    ReadinessSupervisor,
    _backoff_seconds,
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


# ---------------------------------------------------------------------------
# 失败退避：连续失败到阈值后拉长探测间隔，避免固定频率探测放大 WDA 连接堆积
# ---------------------------------------------------------------------------


def test_backoff_silent_below_threshold():
    """阈值内的偶发失败不退避——抖动恢复要快。"""
    for fails in (0, 1, 2):
        assert _backoff_seconds(fails, 5.0, 3) == 0.0


def test_backoff_grows_then_caps():
    assert _backoff_seconds(3, 5.0, 3) == 10.0
    assert _backoff_seconds(4, 5.0, 3) == 20.0
    # 封顶后不再增长，保证恢复最多晚 _BACKOFF_MAX_SEC 被发现
    assert _backoff_seconds(5, 5.0, 3) == _BACKOFF_MAX_SEC
    assert _backoff_seconds(50, 5.0, 3) == _BACKOFF_MAX_SEC


class _StubProbe:
    def __init__(self, outcomes):
        self._outcomes = outcomes

    async def probe(self):
        return self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]


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
    在 probe 之前就把 iOS 设备全部短路掉，测不到退避逻辑。
    """
    monkeypatch.setattr(
        "ai_phone.agent.drivers.ios.was_last_ios_scan_ok", lambda: True
    )


@pytest.mark.asyncio
async def test_healthy_device_is_never_deferred(monkeypatch, ios_scan_ok):
    """健康设备零影响——每轮都探。"""
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(monkeypatch, probed, lambda _s: ProbeOutcome(ready=True))

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios")],
        send_message=lambda msg: _true(),
    )
    for _ in range(5):
        await sup._tick_once()
        clock[0] += 5.0

    assert probed == ["S1"] * 5


async def _true():
    return True


@pytest.mark.asyncio
async def test_failing_device_is_probed_less_often(monkeypatch, ios_scan_ok):
    """连续失败到阈值后进入退避，后续轮次被跳过。"""
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    _install_stub_probe(
        monkeypatch,
        probed,
        lambda _s: ProbeOutcome(ready=False, not_ready_reason="driver_probe_failed"),
    )

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios")],
        send_message=lambda msg: _true(),
    )
    # 前 3 轮（阈值内）全速探测
    for _ in range(3):
        await sup._tick_once()
        clock[0] += 5.0
    assert len(probed) == 3

    # 第 3 次失败后退避 10 秒；再走两个 5 秒 tick 只应命中一次
    await sup._tick_once()  # t+15，退避到 t+20，跳过
    assert len(probed) == 3
    clock[0] += 5.0
    await sup._tick_once()  # t+20，到点，探测
    assert len(probed) == 4


@pytest.mark.asyncio
async def test_recovery_clears_backoff_immediately(monkeypatch, ios_scan_ok):
    """探通立刻回到常速，不让退避拖慢已恢复设备。"""
    probed: list[str] = []
    clock = [1000.0]
    _freeze_monotonic(monkeypatch, clock)
    healthy = {"v": False}

    def build(platform, serial, timeout_sec):  # noqa: ARG001
        probed.append(serial)
        if healthy["v"]:
            return _StubProbe([ProbeOutcome(ready=True)])
        return _StubProbe(
            [ProbeOutcome(ready=False, not_ready_reason="driver_probe_failed")]
        )

    monkeypatch.setattr("ai_phone.agent.health.supervisor.build_probe_for", build)

    sup = ReadinessSupervisor(
        device_lister=lambda: [("S1", "ios")],
        send_message=lambda msg: _true(),
    )
    for _ in range(4):
        await sup._tick_once()
        clock[0] += 5.0

    state = sup._states[("S1", "ios")]
    assert state.consecutive_fail >= 3
    assert state.next_probe_at > 0.0

    # 到点探通一次
    clock[0] = state.next_probe_at
    healthy["v"] = True
    await sup._tick_once()

    state = sup._states[("S1", "ios")]
    assert state.consecutive_fail == 0
    assert state.next_probe_at == 0.0

    # 之后每轮都探
    before = len(probed)
    clock[0] += 5.0
    await sup._tick_once()
    assert len(probed) == before + 1


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
async def test_self_heal_success_resets_backoff(monkeypatch, ios_scan_ok):
    """自愈说「我处理了」→ 计数清零、立刻重探，别让退避把它压在 30 秒后。"""
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
    # 每轮推进 30 秒（退避封顶值），保证每轮都真的探测，跑满阈值那一次
    for _ in range(sup_mod._SELF_HEAL_AFTER_FAILS):
        await sup._tick_once()
        clock[0] += 30.0

    state = sup._states[("S1", "ios_sim")]
    assert state.consecutive_fail == 0, "自愈成功后计数没清零"
    assert state.next_probe_at == 0.0, "自愈后仍在退避期，下一轮不会立即重探"


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
async def test_backoff_is_per_device(monkeypatch, ios_scan_ok):
    """一台设备退避不拖累同 Agent 上的其它设备（iPhone + iPad 共存场景）。"""
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
    assert probed.count("BAD") < 6
