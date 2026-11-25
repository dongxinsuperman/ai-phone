"""iOS native WDA shares the public device lock and scopes each WDA session."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_phone.ai_connect import api, ios_wda
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.server.ws.agent_ws import _dispatch
from ai_phone.shared import protocol as P
from ai_phone.shared.mobile_frame import build_frame


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
    hub.set_agent_capabilities("agent", [P.CAP_MOBILE_SIMULATOR_OPERATIONS])

    rows = (await client.get(api.BASE + "/devices")).json()
    assert {row["serial"]: row["device_kind"] for row in rows} == {PHONE: "physical", SIMULATOR: "virtual"}
    response = await client.post(endpoint, json={"serial": SIMULATOR})
    assert response.status_code == 201
    lease = response.json()
    assert lease["native"]["protocol"] == "wda-http"
    assert lease["api"] == {}
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
    create = await client.post(base + "/session", headers=headers, json={"capabilities": {"alwaysMatch": {}}})
    public_id = create.json()["value"]["sessionId"]
    assert public_id != host.active and create.json()["sessionId"] == public_id
    assert host.calls[-1] == (PHONE, 7, "GET", "/session/device-session")
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
@pytest.mark.parametrize("simulator", [True, False])
async def test_ios_dual_channel_shares_lease_and_rejects_old_frames(client, app, session, monkeypatch, simulator):
    host = await configure(app, session)
    hub = app.state.hub
    serial = SIMULATOR if simulator else PHONE
    generation = "1" * 64
    if simulator:
        await hub.set_devices("agent", {PHONE, OTHER, SIMULATOR})
        hub.set_device_extra(serial, {"device_kind": "virtual", "is_virtual": True,
                                      "vm_platform": "ios", "vm_instance_id": "sim"})
        hub.set_device_readiness(serial, {"ready": True})
        session.add(Device(serial=serial, agent_id="agent", platform="ios_sim", status="online"))
        await session.commit()
        host.wda_simulators[serial] = generation
    hub.set_agent_capabilities("agent", [P.CAP_IOS_OBSERVATION, P.CAP_MOBILE_SIMULATOR_OPERATIONS])

    taps = []
    image = io.BytesIO()
    Image.new("RGB", (60, 90), "white").save(image, format="PNG")
    png = image.getvalue()

    async def deliver(agent, message):
        assert agent == "agent" and message["serial"] == serial and message["platform"] == "ios"
        expected = P.MSG_MOBILE_SIMULATOR_OPERATION if simulator else P.MSG_IOS_USB_OPERATION
        assert message["type"] == expected
        if simulator:
            assert message["binding"] == generation and "transport" not in message
        else:
            assert message["transport"] == 7 and "binding" not in message
        reply = {key: message[key] for key in ("serial", "platform", "request_id")}
        reply.update(type=P.MSG_UNIFIED_OPERATION_RESULT, ok=True)
        if simulator:
            reply["binding"] = generation
        if message["action"] == "observe":
            name = "Next page" if taps else "Open page"
            reply.update(build_frame([{"role": "button", "name": name, "bounds": [4, 6, 56, 42], "actions": ["tap"]}],
                                     platform="ios", width=60, height=90, png=png, include_image=True,
                                     display={"rotation": 0}))
        else:
            assert message["action"] == "tap"
            taps.append([message["x"], message["y"]])
        await _dispatch(hub, app.state.lock_store, agent, reply)
        return True

    monkeypatch.setattr(hub, "send_to_agent", deliver)
    response = await client.post(api.BASE + "/sessions", json={"serial": serial})
    assert response.status_code == 201
    lease = response.json()
    headers = {"Authorization": "Bearer " + lease["token"]}
    wda = lease["native"]["url"]
    assert lease["native"]["protocol"] == "wda-http"
    created = (await client.post(wda + "/session", headers=headers)).json()
    wda_session = wda + "/session/" + created["sessionId"]

    observe, act = (lease["api"][key] for key in ("observe", "act"))
    before = (await client.get(observe, headers=headers)).json()
    target, = before["targets"]
    assert before["screenshot"]["annotated"] and base64.b64decode(before["screenshot"]["data"]) != png
    assert (await client.get(wda + "/status", headers=headers)).status_code == 200
    tap = {"action": "tap", "frame_id": before["frame_id"], "target": target["id"]}
    body = (await client.post(act, headers=headers, json=tap)).json()
    assert body["action_status"] == "executed" and taps == [target["point"]]
    after = body["observation"]
    assert after["frame_id"] != before["frame_id"] and after["screenshot"]["annotated"]
    assert after["targets"][0]["name"] == "Next page" and after["native"] == lease["native"]
    assert (await client.post(act, headers=headers, json=tap)).status_code == 409

    native = await client.get(wda_session + "/source", headers=headers)
    assert native.status_code == 200 and native.json()["sessionId"] == created["sessionId"]
    assert (await client.post(wda_session + "/wda/tap", headers=headers, json={"x": 1, "y": 2})).status_code == 200
    tap.update(frame_id=after["frame_id"], target=after["targets"][0]["id"])
    assert (await client.post(act, headers=headers, json=tap)).status_code == 409

    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    assert (await client.get(observe, headers=headers)).status_code == 410
    assert (await client.get(wda + "/status", headers=headers)).status_code == 410


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
async def test_ios_forwards_feature_capabilities_and_device_routes_under_the_lease(client, app, session, monkeypatch):
    host = await configure(app, session)
    relay = host.call_wda
    posted = []

    async def capture(serial, transport, method, target, body, **kwargs):
        if method == "POST" and target == "/session":
            posted.append(json.loads(body))
        return await relay(serial, transport, method, target, body, **kwargs)

    monkeypatch.setattr(host, "call_wda", capture)
    lease = (await client.post(api.BASE + "/sessions", json={"serial": PHONE})).json()
    headers = {"Authorization": "Bearer " + lease["token"]}
    base = lease["native"]["url"]
    assert (await client.get(base + "/wda/device/info", headers=headers)).status_code == 200
    body = {"capabilities": {"alwaysMatch": {"appium:waitForIdleTimeout": 0}, "firstMatch": [{}]}}
    response = await client.post(base + "/session", json=body, headers=headers)
    assert response.status_code == 200 and posted == [body]
    ident = response.json()["value"]["sessionId"]
    assert (await client.patch(base + f"/session/{ident}/appium/settings", headers=headers)).status_code == 200
    assert host.calls[-1] == (PHONE, 7, "PATCH", "/session/created-session/appium/settings")
    assert (await client.get(base + "/session/created-session/source", headers=headers)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_call", [("GET", "/status"), ("POST", "/session")])
async def test_failed_wda_session_creation_keeps_existing_session(client, app, session, monkeypatch, failed_call):
    host = await configure(app, session)
    lease = (await client.post(api.BASE + "/sessions", json={"serial": PHONE})).json()
    headers = {"Authorization": "Bearer " + lease["token"]}
    base = lease["native"]["url"]
    existing = await client.post(base + "/session", headers=headers, json={"capabilities": {"alwaysMatch": {}}})
    assert existing.status_code == 200
    ident = existing.json()["value"]["sessionId"]
    relay = host.call_wda

    async def fail_call(serial, transport, method, target, body, **kwargs):
        if (method, target) == failed_call:
            return 500, b'{"value":{"error":"session not created"}}'
        return await relay(serial, transport, method, target, body, **kwargs)

    monkeypatch.setattr(host, "call_wda", fail_call)
    custom = {"capabilities": {"alwaysMatch": {"appium:waitForIdleTimeout": 0}}}
    failure = await client.post(base + "/session", headers=headers, json=custom)
    assert failure.status_code == 500 and failure.json()["value"]["error"] == "session not created"
    source = await client.get(base + f"/session/{ident}/source", headers=headers)
    assert source.status_code == 200 and source.json()["sessionId"] == ident
    assert host.calls[-1] == (PHONE, 7, "GET", "/session/device-session/source")


@pytest.mark.asyncio
async def test_ios_web_inspector_uses_the_same_device_lease_and_token(client, app, session, monkeypatch):
    host = await configure(app, session)
    lease = (await client.post(api.BASE + "/sessions", json={"serial": PHONE})).json()
    ident = lease["session_id"]
    headers = {"authorization": "Bearer " + lease["token"]}
    assert lease["native"]["web_inspector"]["url"].endswith(f"/{ident}/webkit")
    routed = []

    async def tunnel(sock, direct, bound, selected, check, activity, **options):
        assert selected is host and check() is None and bound.serial == PHONE
        assert options == {"webkit": True}
        await activity()
        routed.append(bound.serial)

    async def close(**options):
        routed.append(options["code"])

    monkeypatch.setattr(api, "connect_native", tunnel)
    await api.native_webkit_socket(ident, SimpleNamespace(app=app, headers={}, close=close))
    await api.native_webkit_socket(ident, SimpleNamespace(app=app, headers=headers, close=close))
    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    await api.native_webkit_socket(ident, SimpleNamespace(app=app, headers=headers, close=close))
    assert routed == [1008, PHONE, 1008]


@pytest.mark.asyncio
async def test_simulator_inspector_opens_after_service_start_and_reports_device_eof(monkeypatch):
    from ai_phone.agent import ios_sim_observation, ios_webkit

    calls = []
    sent = []
    reader = asyncio.StreamReader()
    reader.feed_data(b"reply")
    reader.feed_eof()

    def identity(*_):
        calls.append("identity")
        return "same-boot"

    async def start(serial):
        assert serial == SIMULATOR
        calls.append("start")

    async def closed():
        pass

    async def connect(address):
        assert address == "/inspector"
        calls.append("connect")
        return reader, SimpleNamespace(close=lambda: calls.append("close"), wait_closed=closed)

    class Peer:
        async def __aiter__(self):
            while True:
                yield await asyncio.Future()

        async def send(self, data):
            sent.append(data)

    monkeypatch.setattr(ios_sim_observation, "boot_identity", identity)
    monkeypatch.setattr(ios_webkit, "simulator_socket", lambda serial: "/inspector")
    monkeypatch.setattr(ios_webkit, "start_simulator_inspector", start)
    monkeypatch.setattr(asyncio, "open_unix_connection", connect)
    with pytest.raises(ConnectionError, match="Web Inspector 已关闭连接"):
        await ios_webkit.forward(Peer(), SIMULATOR, "same-boot", simulator_port=8100)
    assert calls == ["identity", "start", "connect", "identity", "close"]
    assert sent == [b"reply"]


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


def test_ios_wda_allows_device_routes_and_only_the_leased_session_id():
    assert ios_wda.allowed_request("GET", "/wda/activeAppInfo", None)
    assert ios_wda.allowed_request("GET", "/wda/device/info", None)
    assert ios_wda.allowed_request("PATCH", "/session/current/appium/settings", "current")
    assert not ios_wda.allowed_request("GET", "/session/current/../old-session/source", "current")
    assert not ios_wda.allowed_request("GET", "/session/current-other/source", "current")
