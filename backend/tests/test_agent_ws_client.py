import pytest
import json

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


@pytest.mark.asyncio
async def test_pre_hello_runs_before_first_hello(monkeypatch):
    order = []
    ready = {"value": False}
    sent = []

    class _FakeWs:
        async def send(self, raw):
            sent.append(json.loads(raw))

        async def close(self):
            return None

    class _FakeConnect:
        async def __aenter__(self):
            return _FakeWs()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def fake_ws_connect(*args, **kwargs):  # noqa: ANN001
        return _FakeConnect()

    monkeypatch.setattr(ws_client_module, "ws_connect", fake_ws_connect)

    def provider():
        order.append("provider")
        serial = "VM1" if ready["value"] else "MISS"
        return [{"serial": serial, "platform": "android"}]

    client = AgentWSClient(
        ws_url="ws://server/ws/agent",
        token="t",
        agent_id="agent-1",
        agent_name="mac",
        device_provider=provider,
    )

    async def pre_hello(_client):
        order.append("pre")
        ready["value"] = True

    async def on_connect(_client):
        order.append("connect")

    async def one_session():
        order.append("session")
        await client.stop()

    client.on_pre_hello(pre_hello)
    client.on_connect(on_connect)
    client._session_loop = one_session  # type: ignore[method-assign]

    await client.run_forever()

    assert order == ["pre", "provider", "connect", "session"]
    hello = [payload for payload in sent if payload["type"] == P.MSG_HELLO][0]
    assert hello["devices"][0]["serial"] == "VM1"
