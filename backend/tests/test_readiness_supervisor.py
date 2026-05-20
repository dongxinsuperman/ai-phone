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
