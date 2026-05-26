import pytest

from ai_phone.agent.health.supervisor import ReadinessSupervisor, _State


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
