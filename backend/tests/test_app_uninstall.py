from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from ai_phone.server.app_uninstall import get_app_uninstall_waiter
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Device
from ai_phone.server.ws.agent_ws import _dispatch
from ai_phone.shared import protocol as P


class _ReplyWs:
    def __init__(self, reply: Optional[Dict[str, Any]]) -> None:
        self.reply = reply
        self.sent = []

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent.append(payload)
        if self.reply is None:
            return
        get_app_uninstall_waiter().resolve(
            {
                "type": P.MSG_APP_UNINSTALL_RESULT,
                "request_id": payload["request_id"],
                "serial": payload["serial"],
                "platform": payload["platform"],
                "package_name": payload["package_name"],
                **self.reply,
            }
        )


async def _seed_device(
    app,
    session,
    *,
    serial: str = "A1",
    platform: str = "android",
    status: str = "online",
    reply: Optional[Dict[str, Any]] = None,
) -> _ReplyWs:
    hub = Hub()
    app.state.hub = hub
    ws = _ReplyWs(reply)
    await hub.register_agent("agent-a", "agent-a", "Darwin", ws)
    await hub.set_devices("agent-a", {serial})
    hub.set_device_readiness(serial, {"ready": True, "hint": "ok", "ts": 1.0})
    session.add(
        Device(
            serial=serial,
            agent_id="agent-a",
            platform=platform,
            brand="test",
            model="test",
            os_version="1",
            status=status,
            last_seen_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return ws


@pytest.fixture(autouse=True)
def _reset_waiter():
    waiter = get_app_uninstall_waiter()
    waiter.reset_for_tests()
    yield
    waiter.reset_for_tests()


@pytest.mark.asyncio
async def test_agent_ws_dispatch_resolves_waiting_http_request():
    hub = Hub()
    ws = _ReplyWs(reply=None)
    await hub.register_agent("agent-a", "agent-a", "Darwin", ws)
    await hub.set_devices("agent-a", {"A1"})
    waiter = get_app_uninstall_waiter()
    waiting = asyncio.create_task(
        waiter.request(
            hub=hub,
            request_id="req-1",
            serial="A1",
            platform="android",
            package_name="com.example.demo",
            timeout_sec=1,
        )
    )
    await asyncio.sleep(0)

    await _dispatch(
        hub,
        DeviceLockStore(),
        "agent-a",
        {
            "type": P.MSG_APP_UNINSTALL_RESULT,
            "request_id": "req-1",
            "serial": "A1",
            "platform": "android",
            "package_name": "com.example.demo",
            "success": True,
            "reason": "",
            "message": "卸载成功",
        },
    )

    assert (await waiting)["success"] is True


@pytest.mark.asyncio
async def test_uninstall_success_returns_200_and_releases_lock(client, app, session):
    ws = await _seed_device(
        app,
        session,
        platform="ios_sim",
        reply={"success": True, "reason": "", "message": "卸载成功"},
    )

    resp = await client.post(
        "/api/devices/A1/apps/uninstall",
        json={"package_name": "com.example.demo"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "success": True,
        "serial": "A1",
        "platform": "ios",
        "package_name": "com.example.demo",
        "message": "卸载成功",
    }
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == P.MSG_APP_UNINSTALL_START
    assert ws.sent[0]["platform"] == "ios_sim"
    assert ws.sent[0]["package_name"] == "com.example.demo"
    assert app.state.lock_store.peek("A1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        ("app_not_found", "APP_NOT_FOUND"),
        ("uninstall_failed", "UNINSTALL_FAILED"),
    ],
)
async def test_uninstall_business_failures_return_400(
    client, app, session, reason, expected_code
):
    await _seed_device(
        app,
        session,
        reply={"success": False, "reason": reason, "message": "operation failed"},
    )

    resp = await client.post(
        "/api/devices/A1/apps/uninstall",
        json={"package_name": "com.example.demo"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == {
        "code": expected_code,
        "message": "operation failed",
    }
    assert app.state.lock_store.peek("A1") is None


@pytest.mark.asyncio
async def test_uninstall_rejects_busy_device_without_dispatch(client, app, session):
    ws = await _seed_device(
        app,
        session,
        reply={"success": True, "reason": "", "message": "ok"},
    )
    await app.state.lock_store.acquire(
        "A1", holder="another-run", holder_type="auto", ttl_seconds=300
    )

    resp = await client.post(
        "/api/devices/A1/apps/uninstall",
        json={"package_name": "com.example.demo"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "DEVICE_BUSY"
    assert ws.sent == []


@pytest.mark.asyncio
async def test_uninstall_timeout_returns_504_and_releases_lock(
    client, app, session, monkeypatch
):
    from ai_phone.server.api import devices as devices_api

    await _seed_device(app, session, reply=None)
    monkeypatch.setattr(devices_api, "APP_UNINSTALL_RESPONSE_TIMEOUT_SEC", 0.01)

    resp = await client.post(
        "/api/devices/A1/apps/uninstall",
        json={"package_name": "com.example.demo"},
    )

    assert resp.status_code == 504, resp.text
    assert resp.json()["detail"]["code"] == "UNINSTALL_TIMEOUT"
    assert app.state.lock_store.peek("A1") is None


@pytest.mark.asyncio
async def test_uninstall_requires_live_agent(client, app, session):
    await _seed_device(
        app,
        session,
        reply={"success": True, "reason": "", "message": "ok"},
    )
    await app.state.hub.unregister_agent("agent-a")

    resp = await client.post(
        "/api/devices/A1/apps/uninstall",
        json={"package_name": "com.example.demo"},
    )

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_uninstall_rejects_invalid_package_name(client, app, session):
    await _seed_device(
        app,
        session,
        reply={"success": True, "reason": "", "message": "ok"},
    )

    resp = await client.post(
        "/api/devices/A1/apps/uninstall",
        json={"package_name": "com.example.demo; rm -rf /"},
    )

    assert resp.status_code == 422, resp.text
