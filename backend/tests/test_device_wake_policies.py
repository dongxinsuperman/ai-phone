from __future__ import annotations

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.device_config.resolver import resolve_wake_decision
from ai_phone.server.device_config.service import upsert_wake_policy
from ai_phone.server.hub import Hub
from ai_phone.server.runner.dispatch import RunDispatchService


class FakeWS:
    def __init__(self) -> None:
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_device_wake_policy_crud(client):
    resp = await client.post(
        "/api/device-wake-policies",
        json={
            "serial": "H1",
            "platform": "harmony",
            "wake_swipe": True,
            "remark": "needs swipe",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["serial"] == "H1"
    assert data["platform"] == "harmony"
    assert data["wake_swipe"] is True

    resp = await client.get("/api/device-wake-policies")
    assert resp.status_code == 200
    assert [row["serial"] for row in resp.json()] == ["H1"]

    resp = await client.patch(
        "/api/device-wake-policies/H1",
        json={"wake_swipe": False, "remark": "off"},
    )
    assert resp.status_code == 200
    assert resp.json()["wake_swipe"] is False
    assert resp.json()["remark"] == "off"

    resp = await client.delete("/api/device-wake-policies/H1")
    assert resp.status_code == 200
    assert resp.json() == {"serial": "H1", "deleted": True}


@pytest.mark.asyncio
async def test_device_wake_policy_rejects_ios(client):
    resp = await client.post(
        "/api/device-wake-policies",
        json={"serial": "I1", "platform": "ios", "wake_swipe": True},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "ios_not_configurable"


@pytest.mark.asyncio
async def test_device_wake_policy_rejects_android(client):
    resp = await client.post(
        "/api/device-wake-policies",
        json={"serial": "A1", "platform": "android", "wake_swipe": True},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "android_not_configurable"


@pytest.mark.asyncio
async def test_resolve_wake_decision_uses_db_only(_test_engine, session):
    assert await resolve_wake_decision(session, "A1", "android") == {}
    assert await resolve_wake_decision(session, "H1", "harmony") == {"wake_swipe": False}
    assert await resolve_wake_decision(session, "I1", "ios") == {}

    await upsert_wake_policy(
        session,
        serial="H1",
        platform="harmony",
        wake_swipe=True,
        remark="",
    )
    await session.commit()

    assert await resolve_wake_decision(session, "H1", "harmony") == {"wake_swipe": True}
    assert await resolve_wake_decision(session, "H1", "android") == {}


@pytest.mark.asyncio
async def test_agent_brain_dispatch_includes_wake_policy(_test_engine, session):
    await upsert_wake_policy(
        session,
        serial="H1",
        platform="harmony",
        wake_swipe=True,
        remark="",
    )
    await session.commit()

    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("agent-1", "agent", "test", ws)
    dispatch = RunDispatchService(
        hub=hub,
        session_factory=db_module.get_session_factory(),
    )

    result = await dispatch.dispatch(
        run_id="run-1",
        serial="H1",
        agent_id="agent-1",
        goal="do it",
        engine="midscene",
        dispatch_source="api",
        platform="harmony",
    )

    assert result == {"dispatched": True, "execution_mode": "agent_brain"}
    assert ws.sent[0]["wake_policy"] == {"wake_swipe": True}


@pytest.mark.asyncio
async def test_agent_brain_dispatch_does_not_send_android_wake_policy(_test_engine):
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("agent-1", "agent", "test", ws)
    dispatch = RunDispatchService(
        hub=hub,
        session_factory=db_module.get_session_factory(),
    )

    result = await dispatch.dispatch(
        run_id="run-android",
        serial="A1",
        agent_id="agent-1",
        goal="do it",
        engine="midscene",
        dispatch_source="api",
        platform="android",
    )

    assert result == {"dispatched": True, "execution_mode": "agent_brain"}
    assert "wake_policy" not in ws.sent[0]
