"""Harmony native HDC uses the same device lock as the existing native channels."""
import asyncio
import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_phone.agent.hdc_host import _firmware_tunnel, forward, pinned_handshake
from ai_phone.ai_connect import api, hdc, unified
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.ai_connect.hdc_ports import PortStreams
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.shared import protocol as P
from ai_phone.shared.mobile_frame import build_frame


def test_activity_ignores_fragmented_hdc_control_frames():
    def packet(channel, payload):
        header = bytes((8, channel, 16, 2, 24, 0, 32, 9))
        return struct.pack("!2s2sBHI", b"HW", b"\0\0", 1, len(header), len(payload)) + header + payload

    tracker = hdc.Activity()
    heartbeats = packet(0, b"payload") * 2
    assert not any(tracker.feed(bytes((byte,))) for byte in heartbeats)
    action = packet(17, b"command")
    assert not tracker.feed(action[:12])
    assert tracker.feed(action[12:15])
    assert not tracker.feed(action[15:] + heartbeats)
    assert tracker.feed(heartbeats + action)


def test_native_hdc_host_handshake_pins_one_device():
    banner = b"OHOS HDC\0\0\0H"
    padded = b"OHOS HDC\0\0\0W" + bytes(32)
    wire = pinned_handshake(padded, "USB001", banner)
    assert wire[4:16] == banner
    assert wire[16:48] == b"USB001".ljust(32, b"\0")
    with pytest.raises(ValueError):
        pinned_handshake(padded[:12] + b"USB002".ljust(32, b"\0"), "USB001", banner)


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "upstream", "reply"), [
    (b"help verbose", b"help verbose", b"native response"),
    (b"bugreport report.txt", b"bugreport", b"native response"),
    (b"list targets -v", None, b"USB001\n"),
    (b"wait", None, b"Wait for connected target is USB001\n"),
    (b"discover", None, b"[Fail] This command requires access to the HDC host\n"),
])
async def test_native_hdc_forwards_target_commands_and_keeps_host_operations_scoped(monkeypatch, command, upstream, reply):
    def frame(data):
        return struct.pack("!I", len(data)) + data

    hello = b"OHOS HDC\0\0\0H" + bytes(32)
    incoming = iter([frame(hello) + frame(command + b"\0")])
    outgoing = []
    received = asyncio.get_running_loop().create_future()

    async def recv():
        return next(incoming)

    async def send(data):
        outgoing.append(data)

    async def wait_closed():
        await asyncio.Future()

    async def host(reader, writer):
        writer.write(frame(hello))
        await writer.drain()
        if upstream is None:
            received.set_result(await reader.read())
        else:
            target = frame(hello[:12] + b"USB001".ljust(32, b"\0"))
            length = len(target) + len(frame(upstream + b"\0"))
            received.set_result(await reader.readexactly(length))
            writer.write(frame(reply))
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(host, "127.0.0.1", 0) as listener:
        monkeypatch.setenv("OHOS_HDC_SERVER_PORT", str(listener.sockets[0].getsockname()[1]))
        peer = SimpleNamespace(recv=recv, send=send, wait_closed=wait_closed)
        await asyncio.wait_for(forward(peer, "USB001"), 2)
        actual = await asyncio.wait_for(received, 2)
    expected = frame(hello[:12] + b"USB001".ljust(32, b"\0")) + frame(upstream + b"\0") if upstream else b""
    assert actual == expected
    assert outgoing == ([frame(hello), "request", frame(reply)] if upstream else [frame(hello), frame(reply)])


@pytest.mark.asyncio
async def test_hdc_port_mapping_transfers_multiple_streams_and_closes_them_together():
    class Peer:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def send(self, data):
            await self.other.queue.put(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

    async def echo(reader, writer):
        try:
            while data := await reader.read(4096):
                writer.write(data.upper())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    client, agent = Peer(), Peer()
    client.other, agent.other = agent, client
    async with await asyncio.start_server(echo, "127.0.0.1", 0) as service:
        outgoing = PortStreams(client)
        incoming = PortStreams(agent, destination=service.sockets[0].getsockname()[1])
        running = [asyncio.create_task(outgoing.run()), asyncio.create_task(incoming.run())]
        open_streams = []
        try:
            async with await asyncio.start_server(outgoing.accept, "127.0.0.1", 0) as mapping:
                port = mapping.sockets[0].getsockname()[1]
                for _ in range(2):
                    open_streams.append(await asyncio.open_connection("127.0.0.1", port))
                for index, (_, writer) in enumerate(open_streams):
                    writer.write(f"channel{index}".encode())
                    await writer.drain()
                for index, (reader, _) in enumerate(open_streams):
                    assert await asyncio.wait_for(reader.readexactly(8), 2) == f"CHANNEL{index}".encode()
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            for reader, writer in open_streams:
                try:
                    assert await asyncio.wait_for(reader.read(), 2) == b""
                finally:
                    writer.close()
                    await writer.wait_closed()


@pytest.mark.asyncio
async def test_hdc_firmware_uses_only_transferred_bytes_and_cleans_up_staging():
    frames = []
    messages = []

    async def send(data):
        messages.append(data)

    async def client_frame(limit):
        assert limit == 3
        return b"ota"

    async def drain():
        pass

    async def download():
        path = Path(frames[0].removeprefix(b"handshake")[4:].decode().split()[1].rstrip("\0"))
        assert path.name == "upload.zip" and path.read_bytes() == b"ota"
        frames.append(path)

    peer = SimpleNamespace(send=send)
    writer = SimpleNamespace(write=frames.append, drain=drain)
    await _firmware_tunnel(peer, writer, b"handshake", client_frame, download,
                           json.dumps([["update", ""], ".zip", 3]).encode())
    assert len(messages) == 2 and set(messages) == {"request"}
    assert not frames[1].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("dual", [False, True])
@pytest.mark.parametrize("simulator", [False, True])
async def test_harmony_claim_is_bound_to_one_device(client, app, session, dual, simulator):
    hub = app.state.hub = Hub()
    serial = "harmony-vm:local" if simulator else "USB-HARMONY-001"
    transport, boot = "a" * 64, "b" * 64
    messages = []
    tapped = []
    shot = io.BytesIO()
    Image.new("RGB", (60, 90), "white").save(shot, format="PNG")

    async def send_json(msg):
        messages.append(msg)
        if msg["type"] == P.MSG_AI_DIRECT_REQUEST:
            host.resolve({"id": msg["id"], "binding": boot, "action_types": ["observe", "tap"]})
            return
        assert msg["type"] == P.MSG_MOBILE_SIMULATOR_OPERATION
        assert msg["binding"] == boot and msg["platform"] == "harmony" and msg["serial"] == serial
        fields = {name: msg[name] for name in ("serial", "platform", "request_id", "binding")}
        if msg["action"] == "tap":
            tapped.append([msg["x"], msg["y"]])
        else:
            assert msg["action"] == "observe"
            name = "Destination" if tapped else "Open page"
            fields.update(build_frame([{"role": "button", "name": name, "actions": ["tap"], "bounds": [3, 6, 55, 36]}],
                                      platform="harmony", width=60, height=90, png=shot.getvalue(),
                                      display={"width": 60, "height": 90}, recognize_screen_text=False))
        unified.resolve(hub, "agent", {"ok": True, **fields})

    connection = await hub.register_agent("agent", "host", "Darwin", SimpleNamespace(send_json=send_json), host_id="host")
    host = connection.native_channel = NativeAgentChannel(connection.ws)
    host.update({"hdc_devices": {serial: transport, "other-usb": "c" * 64}})
    caps = [P.CAP_MOBILE_SIMULATOR_OPERATIONS]
    if dual:
        caps.append(P.CAP_HARMONY_OBSERVATION)
    hub.set_agent_capabilities("agent", caps)
    await hub.set_devices("agent", {serial, "other-usb"})
    for device in (serial, "other-usb"):
        session.add(Device(serial=device, agent_id="agent", platform="harmony", status="online"))
        hub.set_device_readiness(device, {"ready": True})
    if simulator:
        hub.set_device_extra(serial, {"device_kind": "virtual", "is_virtual": True,
                                      "vm_platform": "harmony", "vm_instance_id": "local"})
    await session.commit()

    catalog = (await client.get(api.BASE + "/devices")).json()
    assert {row["serial"]: row["device_kind"] for row in catalog} == {
        serial: "virtual" if simulator else "physical", "other-usb": "physical",
    }
    assert not messages
    claim = await client.post(api.BASE + "/sessions", json={"serial": serial})
    assert claim.status_code == 201
    lease = claim.json()
    assert lease["native"]["protocol"] == ("hdc-connect" if simulator else "hdc-server")
    assert [(m["serial"], m["transport"], m["hdc_bind"]) for m in messages] == [(serial, transport, True)]
    assert (await client.post(api.BASE + "/sessions", json={"serial": serial})).status_code == 409
    other = await client.post(api.BASE + "/sessions", json={"serial": "other-usb"})
    assert other.status_code == 201
    other_lease = other.json()
    assert other_lease["session_id"] != lease["session_id"]
    holders = {"Authorization": "Bearer " + lease["token"]}
    native = api.state(app).leases[lease["session_id"]]
    assert native.unified_target.binding == boot
    if dual:
        observe, act = (lease["api"][key] for key in ("observe", "act"))
        frame = (await client.get(observe, headers=holders)).json()
        target, = frame["targets"]
        assert frame["screenshot"]["annotated"] and frame["screen_texts"] == []
        tap = {"action": "tap", "frame_id": frame["frame_id"], "target": target["id"]}
        executed = (await client.post(act, headers=holders, json=tap)).json()
        updated = executed["observation"]
        assert executed["action_status"] == "executed" and tapped == [target["point"]]
        assert updated["frame_id"] != frame["frame_id"] and updated["targets"][0]["name"] == "Destination"
        await api.state(app).activity(native, app.state.lock_store, hub)
        tap.update(frame_id=updated["frame_id"], target=updated["targets"][0]["id"])
        assert (await client.post(act, headers=holders, json=tap)).status_code == 409
    else:
        assert lease["api"] == {}
    assert (await client.delete(lease["release"], headers=holders)).status_code == 204
    assert (await client.get(lease["session"], headers=holders)).status_code == 410
    other_headers = {"Authorization": "Bearer " + other_lease["token"]}
    assert (await client.get(other_lease["session"], headers=other_headers)).status_code == 200
    assert (await client.delete(other_lease["release"], headers=other_headers)).status_code == 204
    if dual:
        assert (await client.get(observe, headers=holders)).status_code == 410
