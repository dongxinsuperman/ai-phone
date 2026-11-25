"""Regression boundaries where direct access meets the shared device lock."""
import asyncio
from types import SimpleNamespace

import pytest

from ai_phone.ai_connect import api
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.ai_connect.android import allowed_service
from ai_phone.agent import native_direct
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.shared import protocol as P


class ObservingAgent:
    def __init__(self, hub, agent_id="agent"):
        self.hub = hub
        self.agent_id = agent_id

    async def send_json(self, message):
        assert message["action"] == "bind"
        api.unified.resolve(self.hub, self.agent_id, {**message, "ok": True, "android_observation": 1,
                                                     "transport": {"A": 1, "B": 2}[message["serial"]]})


@pytest.mark.asyncio
async def test_direct_claim_is_scoped_and_revoked(client, app, session):
    direct = api.state(app)
    hub = app.state.hub = Hub()
    conn = await hub.register_agent("agent", "mac", "Darwin", ObservingAgent(hub), host_id="usb-host")
    hub.set_agent_capabilities("agent", [P.CAP_UNIFIED_OPERATIONS])
    await hub.set_devices("agent", {"A", "B"})
    for serial in ("A", "B"):
        session.add(Device(serial=serial, agent_id="agent", platform="android", status="online"))
        hub.set_device_readiness(serial, {"ready": True})
    await session.commit()
    host = conn.native_channel = NativeAgentChannel(conn.ws)
    host.devices["A"] = 1
    devices = (await client.get(api.BASE + "/devices")).json()
    assert [(row["serial"], row["status"]) for row in devices] == [("A", "available")]
    assert not direct.leases and app.state.lock_store.peek("A") is None
    claim = api.BASE + "/sessions"
    assert (await client.post(claim, json={"serial": "B"})).status_code == 409
    lock = await app.state.lock_store.acquire("A", "feature", "job")
    assert (await client.post(claim, json={"serial": "A"})).status_code == 409
    assert not direct.leases
    await app.state.lock_store.release("A", lock.token)
    response = await client.post(claim, json={"serial": "A"})
    assert response.status_code == 201
    body = response.json()
    held = {"Authorization": "Bearer " + body["token"]}
    assert body["session"] == f"{api.BASE}/sessions/{body['session_id']}"
    assert (await client.get(body["session"])).status_code == 403
    assert (await client.delete(body["release"], headers={"Authorization": "Bearer " + body["session_id"]})).status_code == 403
    assert (await client.post(claim, headers=held, json={"serial": "B"})).status_code == 409
    assert app.state.lock_store.peek("A").token != body["token"]
    screenshot = body["session"] + "/screenshot"  # Legacy raw USB route
    native = body["session"] + "/adb"  # Previously distributed prototype clients.
    assert (await client.get(screenshot)).status_code == 403
    assert (await client.post(native, headers=held, json={"service": "host:devices"})).status_code == 400
    conn.native_channel = None
    assert (await client.get(screenshot, headers=held)).status_code == 503
    from unittest.mock import AsyncMock
    temporarily_offline = SimpleNamespace(app=app, headers={"authorization": "Bearer " + body["token"]},
                                          accept=AsyncMock(), close=AsyncMock())
    await api.native_socket(body["session"].split("/")[-1], temporarily_offline)
    assert temporarily_offline.close.await_args.kwargs["code"] == 1013
    conn.native_channel = host
    assert body["expires_in"] == 1800 and body["native"]["protocol"] == "adb-connect"
    assert (await client.get(body["session"], headers=held)).status_code == 200
    started = asyncio.Event()
    ended = asyncio.Event()

    async def call(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    host.call = call
    active = asyncio.create_task(client.post(native, headers=held, json={"service": "shell:id"}))
    await asyncio.wait_for(started.wait(), 2)
    assert (await client.delete(body["release"], headers=held)).status_code == 204
    assert ended.is_set() and (await active).status_code == 410
    assert app.state.lock_store.peek("A") is None
    assert (await client.get(screenshot, headers=held)).status_code == 410
    assert (await client.post(native, headers=held, json={"service": "shell:id"})).status_code == 410

    # An old direct session must never operate on a replacement USB connection
    # or release a lock subsequently acquired by the existing scheduler.
    rebound = (await client.post(claim, json={"serial": "A"})).json()
    access = {"Authorization": "Bearer " + rebound["token"]}
    assert rebound["session_id"] != body["session_id"] and rebound["token"] != body["token"]
    assert (await client.delete(body["release"], headers=held)).status_code == 410
    assert (await client.get(rebound["session"], headers=held)).status_code == 403
    assert (await client.get(rebound["session"], headers=access)).status_code == 200
    host.devices["A"] = 2
    assert (await client.get(rebound["session"] + "/screenshot", headers=access)).status_code == 503
    store = app.state.lock_store
    replacement = await store.acquire("A", "feature", "job", force=True)
    assert (await client.get(rebound["session"] + "/screenshot", headers=access)).status_code == 410
    assert (await client.delete(rebound["release"], headers=access)).status_code == 204
    assert store.peek("A").token == replacement.token


@pytest.mark.asyncio
async def test_clean_client_can_read_guide_and_discover_without_occupying(client, app):
    assert (await client.get(api.BASE + "/guide")).status_code == 200
    catalog = await client.get(api.BASE + "/devices")
    assert catalog.status_code == 200 and catalog.json() == []
    assert (await client.post(api.BASE + "/sessions", json={"serial": "unknown"})).status_code == 409
    assert not api.state(app).leases
    for path in ("/pair", "/access", "/access/instance-key"):
        assert (await client.post(api.BASE + path)).status_code == 410


def test_only_device_shell_services():
    assert allowed_service("shell,v2,raw:am start -a android.settings.SETTINGS")
    assert allowed_service("exec:screencap -p")
    for name in ["host:kill", "host:tport:serial:B", "tcp:5037", "shell,v2,pty:ls", "shell:", "shell:ls\0host:kill"]:
        assert not allowed_service(name)


@pytest.mark.asyncio
async def test_agent_advertises_native_transports_from_its_online_device_snapshot(monkeypatch):
    client = AgentWSClient("ws://localhost/ws/agent", "test", "agent", "mac")
    client._last_devices = [
        {"serial": "emulator-5554", "platform": "android", "status": "online"},
        {"serial": "offline-android", "platform": "android", "status": "offline"},
        {"serial": "iphone", "platform": "ios", "status": "online"},
        {"serial": "simulator", "platform": "ios_sim", "status": "online"},
    ]
    native = native_direct.NativeDeviceAccess(client)
    sent = []

    async def android(serials):
        assert serials == {"emulator-5554"}
        return {"emulator-5554": 7}

    async def usb(serials):
        assert serials == {"iphone"}
        return {"iphone": SimpleNamespace(devid=8)}

    async def simulators(serials):
        assert serials == {"simulator"}
        return {"simulator": (8370, "a" * 64)}

    async def send(payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(native, "android_inventory", android)
    monkeypatch.setattr(native, "usb_inventory", usb)
    monkeypatch.setattr(native, "simulator_inventory", simulators)
    monkeypatch.setattr(client, "send", send)
    await native.refresh_inventory(client)
    await native.inventory_task
    await native.refresh_inventory(client, {"emulator-5554", "iphone", "simulator"})
    await native.inventory_task
    assert sent == [{"type": P.MSG_AI_DIRECT_INVENTORY, "devices": {"emulator-5554": 7},
                     "wda_devices": {"iphone": 8}, "wda_simulators": {"simulator": "a" * 64},
                     "hdc_devices": {}}]


@pytest.mark.asyncio
async def test_native_tunnels_route_to_two_agents_and_revoke_one_phone(client, app, session):
    from ai_phone.server.api._deps import lock_store
    from ai_phone.server.ws.agent_ws import _dispatch

    class Socket:
        def __init__(self, headers=None):
            self.app = app
            self.headers = headers or {}
            self.incoming = asyncio.Queue()
            self.sent = asyncio.Queue()
            self.closed = asyncio.Event()

        async def accept(self):
            pass

        async def close(self, **kwargs):
            self.closed.set()

        async def receive(self):
            return await self.incoming.get()

        async def send_bytes(self, data):
            await self.sent.put(data)

    direct = api.state(app)
    hub = app.state.hub = Hub()
    store = app.state.lock_store
    peers = {}
    peer_tasks = []

    class NativeSocket(ObservingAgent):
        async def send_json(self, msg):
            if msg["type"] != P.MSG_AI_DIRECT_REQUEST:
                return await super().send_json(msg)
            if msg.get("cancel"):
                await _dispatch(hub, store, self.agent_id, {
                    "type": P.MSG_AI_DIRECT_RESULT, "id": msg["id"], "closed": True,
                })
                return
            assert msg["serial"] == self.agent_id
            ticket = msg["native_ticket"]
            peer = Socket({"authorization": "Bearer " + ticket})
            peers[self.agent_id] = peer
            peer_tasks.append(asyncio.create_task(api.agent_pipe(peer)))
            await asyncio.sleep(0)
            assert ticket not in direct.tickets

    for serial, transport in (("A", 1), ("B", 2)):
        socket = NativeSocket(hub, serial)
        conn = await hub.register_agent(serial, serial, "Darwin", socket, host_id="host-" + serial)
        conn.native_channel = NativeAgentChannel(socket)
        hub.set_agent_capabilities(serial, [P.CAP_AI_DIRECT_NATIVE])
        await hub.set_devices(serial, {serial})
        hub.set_device_readiness(serial, {"ready": True})
        session.add(Device(serial=serial, agent_id=serial, platform="android", status="online"))
        await _dispatch(hub, store, serial, {"type": P.MSG_AI_DIRECT_INVENTORY, "devices": {serial: transport}})
    await session.commit()
    leases, owners, clients, tasks = {}, {}, {}, []
    for serial in ("A", "B"):
        body = (await client.post(api.BASE + "/sessions", json={"serial": serial})).json()
        assert body["api"] == {}
        ident = body["session"].split("/")[-1]
        leases[serial] = direct.leases[ident]
        owners[serial] = {"authorization": "Bearer " + body["token"]}
        sock = clients[serial] = Socket(owners[serial])
        tasks.append(asyncio.create_task(api.native_socket(ident, sock)))
        await sock.incoming.put({"type": "websocket.receive", "bytes": b"device stream"})
        while serial not in peers:
            await asyncio.sleep(0)
        assert await asyncio.wait_for(peers[serial].sent.get(), 1) == b"device stream"

    lease_a, lease_b = leases["A"], leases["B"]
    stamp_b = lease_b.expires
    path_a = api.BASE + "/sessions/" + next(k for k, v in direct.leases.items() if v is lease_a)
    # Status and an idle connected socket never renew. A device request on either
    # entry renews the same canonical lock, independently of the other phone.
    before = lease_a.expires
    assert (await client.get(path_a, headers=owners["A"])).status_code == 200
    assert lease_a.expires == before
    wrong_phone = Socket(owners["B"])
    await api.native_socket(path_a.split("/")[-1], wrong_phone)
    assert wrong_phone.closed.is_set() and lease_a.expires == before
    await peers["A"].incoming.put({"type": "websocket.receive", "text": "request"})
    await peers["A"].incoming.put({"type": "websocket.receive", "bytes": b"ok"})
    assert await asyncio.wait_for(clients["A"].sent.get(), 1) == b"ok"
    assert lease_a.expires > before and lease_b.expires == stamp_b
    before = lease_a.expires
    async def screenshot(*args):
        return b"\x89PNG\r\n\x1a\nIEND\xaeB\x60\x82"
    hub.agent_native_channel("A").call = screenshot
    assert (await client.get(path_a + "/screenshot", headers=owners["A"])).status_code == 200
    assert lease_a.expires > before
    assert (await client.delete(path_a, headers=owners["A"])).status_code == 204
    assert peers["A"].closed.is_set() and clients["A"].closed.is_set()
    denied = Socket(owners["A"])
    await api.native_socket(path_a.split("/")[-1], denied)
    assert denied.closed.is_set()
    assert (await client.get(path_a + "/screenshot", headers=owners["A"])).status_code == 410

    await clients["B"].incoming.put({"type": "websocket.receive", "bytes": b"still connected"})
    assert await asyncio.wait_for(peers["B"].sent.get(), 1) == b"still connected"
    assert not clients["B"].closed.is_set() and lease_b.expires == stamp_b
    lock_store(clients["B"]).peek("B").last_heartbeat_at -= api.LEASE_SECONDS + 1
    lease_b.expires -= api.LEASE_SECONDS + 1
    await asyncio.wait_for(clients["B"].closed.wait(), 2)
    assert peers["B"].closed.is_set() and not app.state.lock_store.peek("B")
    ident_b = next(k for k, v in direct.leases.items() if v is lease_b)
    assert (await client.get(f"{api.BASE}/sessions/{ident_b}/screenshot", headers=owners["B"])).status_code == 410
    assert (await client.post(api.BASE + "/sessions", json={"serial": "B"})).status_code == 201
    await asyncio.gather(*tasks, *peer_tasks)
    assert not direct.tickets


@pytest.mark.asyncio
async def test_interactive_adb_input_is_activity_without_parsing_commands(monkeypatch):
    from contextlib import asynccontextmanager

    from ai_phone.ai_connect import native

    received, sent = asyncio.Queue(), asyncio.Queue()

    class Wire:
        async def __aiter__(self):
            while (chunk := await received.get()) is not None:
                yield chunk

        async def send(self, data):
            await sent.put(data)

    reader = asyncio.StreamReader()
    reader.feed_data(b"OKAY")
    writes = []

    async def drain():
        pass

    @asynccontextmanager
    async def selected(serial, transport):
        assert (serial, transport) == ("A", 3)
        yield reader, SimpleNamespace(write=writes.append, drain=drain)

    def packet(name, remote, local, data=b""):
        magic = int.from_bytes(name, "little") ^ 0xffffffff
        return native.HEADER.pack(name, remote, local, len(data), 0, magic) + data

    monkeypatch.setattr(native, "device_connection", selected)
    device = native.DeviceTransport(Wire(), "A", 3)
    device.version = 0x01000001
    task = asyncio.create_task(device.run())
    try:
        opening = packet(b"OPEN", 7, 0, b"shell,v2,pty:\0")
        await received.put(opening[:10])
        await received.put(opening[10:])
        assert await asyncio.wait_for(sent.get(), 1) == "request"
        _, local, remote, *_ = native.HEADER.unpack(await sent.get())
        assert remote == 7
        await received.put(packet(b"OKAY", 7, local))
        await received.put(packet(b"WRTE", 7, local, b"test input\n"))
        assert await asyncio.wait_for(sent.get(), 1) == "request"
        assert (await sent.get())[:4] == b"OKAY" and sent.empty()
        assert writes[-1] == b"test input\n"
    finally:
        await received.put(None)
        await asyncio.wait_for(task, 1)
