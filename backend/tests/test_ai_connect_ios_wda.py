"""iOS native WDA shares the public device lock and scopes each WDA session."""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from ai_phone.ai_connect import api, ios_wda
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.server.ws.agent_ws import _dispatch
from ai_phone.shared import protocol as P


PHONE = "1234567890abcdef1234567890abcdef12345678"
OTHER = "00008110-000123451A80001E"
SIMULATOR = "4EF6F43A-420E-4DA9-85B9-8FFC32F8DB69"


@pytest.mark.asyncio
async def test_wda_replies_and_disconnect_are_scoped_to_one_agent_socket(app):
    from fastapi import HTTPException

    hub = Hub()
    store = app.state.lock_store
    queues = {name: asyncio.Queue() for name in ("mac-a", "mac-b")}
    channels = {}
    for name, queue in queues.items():
        ws = SimpleNamespace(send_json=queue.put)
        conn = await hub.register_agent(name, name, "Darwin", ws, host_id=name)
        channels[name] = conn.native_channel = NativeAgentChannel(ws)

    pending_a = asyncio.create_task(channels["mac-a"].call_wda(PHONE, 7, "GET", "/status", b""))
    pending_b = asyncio.create_task(channels["mac-b"].call_wda(OTHER, 7, "GET", "/status", b""))
    incoming_a = await asyncio.wait_for(queues["mac-a"].get(), 1)
    incoming_b = await asyncio.wait_for(queues["mac-b"].get(), 1)
    assert incoming_a["type"] == incoming_b["type"] == P.MSG_AI_DIRECT_REQUEST
    assert (incoming_a["serial"], incoming_b["serial"]) == (PHONE, OTHER)
    payload = b'{"value":{"ready":true}}'
    reply = {"type": P.MSG_AI_DIRECT_RESULT, "id": incoming_b["id"], "status": 200,
             "data": base64.b64encode(payload).decode()}
    await _dispatch(hub, store, "mac-a", reply)
    await hub.unregister_agent("mac-a")
    with pytest.raises(HTTPException) as error:
        await asyncio.wait_for(pending_a, 1)
    assert error.value.status_code == 503 and not pending_b.done()
    await _dispatch(hub, store, "mac-b", reply)
    assert await asyncio.wait_for(pending_b, 1) == (200, payload)


class WdaHost:
    wda_transport = NativeAgentChannel.wda_transport

    def __init__(self):
        self.devices = {}
        self.wda_devices = {PHONE: 7}
        self.wda_simulators = {}
        self.calls = []
        self.ready = True
        self.active = "device-session"

    async def call_wda(self, serial, transport, method, target, body, **_):
        self.calls.append((serial, transport, method, target))
        if target == "/status":
            result = {"sessionId": self.active, "value": {"ready": self.ready}}
            if serial == SIMULATOR:
                result["value"]["ios"] = {"simulatorVersion": "27.2"}
        elif target == "/session":
            assert json.loads(body)["capabilities"]
            self.active = "created-session"
            result = {"value": {"sessionId": self.active, "capabilities": {}}}
        elif target == "/session/" + self.active:
            result = {"sessionId": self.active, "value": {"sessionId": self.active, "capabilities": {}}}
        else:
            result = {"sessionId": self.active, "value": None}
        return 200, json.dumps(result).encode()


async def configure(app, session):
    hub = app.state.hub = Hub()
    host = WdaHost()
    conn = await hub.register_agent("agent", "mac", "Darwin", SimpleNamespace(), host_id="usb-host")
    conn.native_channel = host
    await hub.set_devices("agent", {PHONE, OTHER})
    for serial in (PHONE, OTHER):
        session.add(Device(serial=serial, agent_id="agent", platform="ios", status="online"))
        hub.set_device_readiness(serial, {"ready": True})
    await session.commit()
    return host


@pytest.mark.asyncio
async def test_simulator_wda_uses_managed_instance_and_boot_generation(client, app, session):
    host = await configure(app, session)
    hub = app.state.hub
    await hub.set_devices("agent", {PHONE, OTHER, SIMULATOR})
    session.add(Device(serial=SIMULATOR, agent_id="agent", platform="ios_sim", status="online"))
    await session.commit()
    hub.set_device_readiness(SIMULATOR, {"ready": True})
    generation = "1" * 64
    host.wda_simulators[SIMULATOR] = generation
    endpoint = api.BASE + "/sessions"
    assert (await client.post(endpoint, json={"serial": SIMULATOR})).status_code == 409
    fields = hub.get_device_extra(SIMULATOR)
    fields.update({"device_kind": "virtual", "is_virtual": True, "vm_platform": "ios", "vm_instance_id": "original"})
    hub.set_device_extra(SIMULATOR, fields)

    rows = (await client.get(api.BASE + "/devices")).json()
    assert {row["serial"]: row["device_kind"] for row in rows} == {PHONE: "physical", SIMULATOR: "virtual"}
    response = await client.post(endpoint, json={"serial": SIMULATOR})
    assert response.status_code == 201
    lease = response.json()
    assert lease["native"]["protocol"] == "wda-http"
    headers = {"Authorization": "Bearer " + lease["token"]}
    status = lease["native"]["url"] + "/status"
    assert (await client.get(status, headers=headers)).status_code == 200
    assert host.calls[-1] == (SIMULATOR, generation, "GET", "/status")

    host.wda_simulators[SIMULATOR] = "2" * 64
    assert (await client.get(status, headers=headers)).status_code == 503
    host.wda_simulators[SIMULATOR] = generation
    hub.set_device_extra(SIMULATOR, {**fields, "vm_instance_id": "replacement"})
    assert (await client.get(status, headers=headers)).status_code == 503
    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    assert (await client.get(status, headers=headers)).status_code == 410


@pytest.mark.asyncio
async def test_ios_wda_claim_actions_and_release_are_lease_scoped(client, app, session):
    host = await configure(app, session)
    assert [row["serial"] for row in (await client.get(api.BASE + "/devices")).json()] == [PHONE]
    claim = api.BASE + "/sessions"
    assert (await client.post(claim, json={"serial": OTHER})).status_code == 409
    host.ready = False
    assert (await client.post(claim, json={"serial": PHONE})).status_code == 503
    assert app.state.lock_store.peek(PHONE) is None
    host.ready = True

    response = await client.post(claim, json={"serial": PHONE})
    assert response.status_code == 201
    lease = response.json()
    headers = {"Authorization": "Bearer " + lease["token"]}
    assert lease["native"]["protocol"] == "wda-http" and lease["api"] == {}
    base = lease["native"]["url"]
    assert (await client.get(base + "/status")).status_code == 403
    assert (await client.get(base + "/status", headers=headers)).json() == {"value": {"ready": True}}
    assert (await client.get(base + "/session/device-session/source", headers=headers)).status_code == 409
    assert (await client.post(claim, json={"serial": PHONE})).status_code == 409
    assert (await client.post(base + "/session", headers=headers,
                              json={"capabilities": {"alwaysMatch": {"bundleId": "com.apple.Preferences"}}})).status_code == 400

    create = await client.post(base + "/session", headers=headers, json={"capabilities": {"alwaysMatch": {}}})
    public_id = create.json()["value"]["sessionId"]
    assert public_id != host.active and create.json()["sessionId"] == public_id
    assert host.calls[-1] == (PHONE, 7, "GET", "/session/device-session")
    assert (await client.post(base + "/session", headers=headers)).status_code == 409
    session_path = base + "/session/" + public_id
    for url, body in (("/wda/apps/launch", {"bundleId": "com.apple.Preferences"}),
                      ("/wda/tap", {"x": 1, "y": 2})):
        reply = await client.post(session_path + url, headers=headers, json=body)
        assert reply.status_code == 200 and reply.json()["sessionId"] == public_id
    assert (await client.get(session_path + "/source?format=xml", headers=headers)).status_code == 200
    assert host.calls[-1] == (PHONE, 7, "GET", "/session/device-session/source?format=xml")

    host.wda_devices[PHONE] = 8
    assert (await client.get(base + "/screenshot", headers=headers)).status_code == 503
    host.wda_devices[PHONE] = 7
    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    assert all(call[2] != "DELETE" for call in host.calls)
    assert (await client.post(session_path + "/wda/tap", headers=headers)).status_code == 410
    assert app.state.lock_store.peek(PHONE) is None


@pytest.mark.asyncio
async def test_ios_creates_physical_session_only_if_missing(client, app, session):
    host = await configure(app, session)
    host.active = ""
    lease = (await client.post(api.BASE + "/sessions", json={"serial": PHONE})).json()
    headers = {"Authorization": "Bearer " + lease["token"]}
    base = lease["native"]["url"]
    created = (await client.post(base + "/session", headers=headers)).json()
    public_id = created["value"]["sessionId"]
    assert host.calls[-1] == (PHONE, 7, "POST", "/session")
    path = base + "/session/" + public_id
    assert (await client.delete(path, headers=headers)).status_code == 200
    assert (await client.get(path, headers=headers)).status_code == 409
    adopted = (await client.post(base + "/session", headers=headers)).json()
    assert adopted["value"]["sessionId"] != public_id
    assert host.calls[-1] == (PHONE, 7, "GET", "/session/created-session")


@pytest.mark.asyncio
async def test_ios_release_cancels_in_flight_wda_request(client, app, session, monkeypatch):
    host = await configure(app, session)
    lease = (await client.post(api.BASE + "/sessions", json={"serial": PHONE})).json()
    headers = {"Authorization": "Bearer " + lease["token"]}
    base = lease["native"]["url"]
    created = await client.post(base + "/session", headers=headers, json={"capabilities": {"alwaysMatch": {}}})
    public_id = created.json()["value"]["sessionId"]
    received = asyncio.Event()
    closed = asyncio.Event()
    call = host.call_wda

    async def blocking_wda(serial, transport, method, target, body, **opts):
        if target.endswith("/wda/tap"):
            received.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return await call(serial, transport, method, target, body, **opts)

    monkeypatch.setattr(host, "call_wda", blocking_wda)
    tap = asyncio.create_task(client.post(base + f"/session/{public_id}/wda/tap", headers=headers))
    await asyncio.wait_for(received.wait(), 1)
    result = await client.delete(lease["release"], headers=headers)
    assert result.status_code == 204 and closed.is_set()
    assert (await tap).status_code == 410


def test_ios_wda_limits_paths_to_http_reads_and_this_lease_session():
    assert ios_wda.allowed_request("GET", "/wda/activeAppInfo", None)
    assert not ios_wda.allowed_request("POST", "/wda/activeAppInfo", None)
    assert not ios_wda.allowed_request("GET", "/session/current/../old-session/source", "current")
    assert not ios_wda.allowed_request("GET", "/session/current-other/source", "current")
