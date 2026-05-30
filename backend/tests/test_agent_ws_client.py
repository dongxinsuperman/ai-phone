import pytest

from ai_phone.agent import ws_client as ws_client_module
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.shared import protocol as P


@pytest.mark.asyncio
async def test_rescan_loop_sends_periodic_device_snapshot(monkeypatch):
    class StopLoop(Exception):
        pass

    sleeps = []
    sent = []

    async def fake_sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) > 6:
            raise StopLoop

    async def fake_send(payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(ws_client_module.asyncio, "sleep", fake_sleep)

    client = AgentWSClient(
        ws_url="ws://server/ws/agent",
        token="t",
        agent_id="agent-1",
        agent_name="mac",
        rescan_interval=1.0,
        device_snapshot_refresh_sec=3.0,
        device_provider=lambda: [{"serial": "S1", "platform": "android"}],
    )
    client._ws = object()
    client._last_serials = {"S1"}
    client.send = fake_send

    with pytest.raises(StopLoop):
        await client._rescan_loop()

    hello_payloads = [msg for msg in sent if msg.get("type") == P.MSG_HELLO]
    assert len(hello_payloads) == 2
    assert [msg["devices"][0]["serial"] for msg in hello_payloads] == ["S1", "S1"]
