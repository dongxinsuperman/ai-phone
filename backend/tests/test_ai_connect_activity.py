"""Read-only native traffic and writes share a lease with the numbered frame API."""
import asyncio
import io
import struct
from types import SimpleNamespace

import pytest
from PIL import Image

from ai_phone.ai_connect import api, hdc, ios_wda, unified
from ai_phone.ai_connect.activity import is_read_only_shell
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.ai_connect.android import is_read_only_service
from ai_phone.server.hub import Hub
from ai_phone.shared.mobile_frame import build_frame


def hdc_packet(command, payload=b"", *, channel=300):
    def varint(value):
        result = bytearray()
        while value >= 128:
            result.append((value & 127) | 128)
            value >>= 7
        return bytes(result) + bytes([value])

    protected = b"\x08" + varint(channel) + b"\x10" + varint(command) + b"\x18\x00\x20\x09"
    return struct.pack("!2s2sBHI", b"HW", b"\0\0", 1, len(protected), len(payload)) + protected + payload


@pytest.mark.parametrize(("command", "read_only"), [
    (b"cat /proc/net/unix", True),
    (b"cat /proc/1234/cmdline /proc/1234/stat", True),
    (b"cat /proc/1234/cmdline\t/proc/1234/stat  /proc/1234/status", True),
    (b"cat /proc/1234/cmdline /system/build.prop", False),
    (b"cat /proc/1234/cmdline /proc/1234/stat && true", False),
    (b"cat /proc/1234/cmdline /proc/1234/stat; true", False),
    (b"cat /proc/1234/cmdline\n/proc/1234/stat", False),
    (b"cat /proc/1234/cmdline /proc/1234/stat > /data/local/tmp/output", False),
    (b"cat /proc/1234/cmdline /proc/$PID/stat", False),
    (b"cat /proc/1234/cmdline /proc/../system/build.prop", False),
])
def test_proc_probe_activity_requires_every_argument_to_be_a_literal_proc_path(command, read_only):
    assert is_read_only_shell(command) is read_only
    assert is_read_only_service(b"shell,v2,raw:" + command) is read_only
    expected = hdc.ActivityKind.RENEW if read_only else hdc.ActivityKind.WRITE
    assert hdc.Activity().feed(hdc_packet(1001, command + b"\0")) == expected


def test_hdc_read_classification_handles_split_shell_payloads_and_combined_frames():
    tracker = hdc.Activity()
    heartbeat = hdc_packet(5000, channel=0)
    forward = hdc_packet(2505, b"forwarded traffic")
    probe = hdc_packet(1001, b"cat /proc/net/unix\0")
    write = hdc_packet(1001, b"cat /proc/net/unix; uitest uiInput click 4 5")
    assert not any(tracker.feed(bytes([byte])) for byte in heartbeat)
    assert tracker.feed(forward + probe[:-2]) == hdc.ActivityKind.RENEW
    assert tracker.feed(probe[-2:] + hdc_packet(2)) == hdc.ActivityKind.RENEW
    assert tracker.feed(write[:-2]) == hdc.ActivityKind.NONE
    assert tracker.feed(write[-2:] + forward) == hdc.ActivityKind.WRITE
    assert tracker.feed(hdc_packet(2001, b"uitest uiInput click 4 5\n")) == hdc.ActivityKind.WRITE


class Socket:
    def __init__(self, app, headers=None):
        self.app = app
        self.headers = headers or {}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def close(self, **_kwargs):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        await self.outgoing.put(data)


@pytest.mark.parametrize(("platform", "simulator", "protocol", "route"), [
    ("android", False, "adb-connect", api.native_socket),
    ("ios", False, "wda-http", api.native_webkit_socket),
    ("harmony", False, "hdc-server", api.native_hdc_socket),
    ("harmony", True, "hdc-connect", api.native_hdc_socket),
], ids=["adb", "ios-webkit", "hdc-usb", "hdc-simulator"])
async def test_native_traffic_preserves_reads_and_invalidates_writes(
    client, app, monkeypatch, platform, simulator, protocol, route,
):
    hub = app.state.hub = Hub()
    direct = api.state(app)
    serial = "harmony-vm:local" if simulator else "test-device"
    peer = Socket(app)
    connected = asyncio.Event()

    async def host_request(message):
        if message.get("cancel"):
            host.resolve({"id": message["id"], "closed": True})
        else:
            assert message["serial"] == serial
            assert message.get("renew_activity") is (True if platform != "ios" else None)
            direct.tickets[message["native_ticket"]].ready.set_result(peer)
            connected.set()

    agent = await hub.register_agent("agent", "host", "Darwin", SimpleNamespace(send_json=host_request), host_id="host")
    host = agent.native_channel = NativeAgentChannel(agent.ws)
    await hub.set_devices("agent", {serial})
    if simulator:
        hub.set_device_extra(serial, {
            "device_kind": "virtual", "is_virtual": True, "vm_platform": "harmony", "vm_instance_id": "local",
        })
    transport = "a" * 64 if platform == "harmony" else 7
    inventory = {"android": host.devices, "ios": host.wda_devices, "harmony": host.hdc_devices}
    inventory[platform][serial] = transport
    store = app.state.lock_store
    lock = await store.acquire(serial, "test", "direct", ttl_seconds=api.LEASE_SECONDS)
    target = unified.Target("agent", platform, ("observe", "tap"), transport, "binding",
                            instance_id="local" if simulator else None)
    lease = direct.leases["lease"] = api.Lease(
        serial, "host", transport, lock.token, lock.last_heartbeat_at + lock.ttl_seconds,
        target, {"native": {"protocol": protocol}},
    )
    headers = {"authorization": "Bearer " + lease.secret}
    sock = Socket(app, headers)
    base = api.BASE + "/sessions/lease"
    started, proceed = asyncio.Event(), asyncio.Event()
    proceed.set()
    taps = []
    image = io.BytesIO()
    Image.new("RGB", (40, 60), "white").save(image, format="PNG")
    frame = build_frame(
        [{"role": "button", "name": "Open", "actions": ["tap"], "bounds": [4, 6, 30, 40]}],
        width=40, height=60, png=image.getvalue(), display={}, platform=platform, recognize_screen_text=False,
    )

    async def operate(_hub, _lease, action, **params):
        if action == "observe":
            started.set()
            await proceed.wait()
            return frame
        assert action == "tap"
        taps.append((params["x"], params["y"]))
        return {}

    async def wda(_host, _lease, method, path, _target, _body):
        assert method in ("GET", "POST") and path.endswith(("/source", "/wda/tap"))
        return 200, b'{"value":null}', lease.wda_session

    monkeypatch.setattr(unified, "operate", operate)
    monkeypatch.setattr(api, "forward_wda", wda)
    lease.wda_session = ios_wda.Session("public", "device")

    async def transfer(*, event=None, data=b"native reply"):
        source, destination = (sock, peer) if simulator else (peer, sock)
        if event is not None:
            await source.incoming.put({"type": "websocket.receive", "text": event})
        await source.incoming.put({"type": "websocket.receive", "bytes": data})
        assert await asyncio.wait_for(destination.outgoing.get(), 2) == data

    async def read_activity():
        if simulator:
            probe = hdc_packet(1001, b"cat /proc/1234/cmdline /proc/1234/stat\0")
            await transfer(data=hdc_packet(5) + hdc_packet(2501) + hdc_packet(2505) + probe[:-1])
            await transfer(data=probe[-1:] + hdc_packet(2))
        else:
            await transfer(event="request" if platform == "ios" else "renew")

    async def write_activity():
        if simulator:
            await transfer(data=hdc_packet(1001, b"uitest uiInput click 4 5"))
        elif platform == "ios":
            result = await client.post(base + "/wda/session/public/wda/tap", headers=headers)
            assert result.status_code == 200, result.text
        else:
            await transfer(event="request")

    async def observe():
        return await client.get(base + "/observe", headers=headers)

    def action_for(observation):
        return {"action": "tap", "frame_id": observation["frame_id"], "target": observation["targets"][0]["id"]}

    async def during_capture(request, activity):
        started.clear()
        proceed.clear()
        pending = asyncio.create_task(request)
        try:
            await asyncio.wait_for(started.wait(), 2)
            await activity()
        finally:
            proceed.set()
            result = await asyncio.wait_for(pending, 2)
        return result

    tunnel = asyncio.create_task(route("lease", sock))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        first = await observe()
        assert first.status_code == 200, first.text
        before, expiry, revision = lease.screen, lease.expires, lease.native_revision
        await read_activity()
        assert lease.screen is before and lease.expires > expiry and lease.native_revision == revision
        if platform == "ios":
            read_wda = await client.get(base + "/wda/session/public/source", headers=headers)
            assert read_wda.status_code == 200 and lease.screen is before and lease.native_revision == revision

        acted = await client.post(base + "/act", headers=headers, json=action_for(first.json()))
        assert acted.status_code == 200, acted.text
        assert acted.json()["action_status"] == "executed" and len(taps) == 1
        if platform == "android":
            frame_id = acted.json()["observation"]["frame_id"]
            await read_activity()
            invalid_tap = await client.post(base + "/tap", headers=headers,
                                            json={"frame_id": frame_id, "x": 40, "y": 5})
            assert invalid_tap.status_code == 422 and "坐标超出截图范围" in invalid_tap.json()["detail"]
            tapped = await client.post(base + "/tap", headers=headers, json={"frame_id": frame_id, "x": 4, "y": 5})
            assert tapped.status_code == 200 and taps[-1] == (4, 5)
        action_count = len(taps)

        observed = await during_capture(observe(), read_activity)
        assert observed.status_code == 200, observed.text
        new_frame = observed.json()
        assert new_frame["frame_id"] == lease.screen.ident and lease.native_revision == revision
        await write_activity()
        assert lease.screen is None and lease.native_revision == revision + 1
        old_act = await client.post(base + "/act", headers=headers, json=action_for(new_frame))
        old_tap = await client.post(base + "/tap", headers=headers, json={
            "frame_id": new_frame["frame_id"], "x": 4, "y": 5,
        })
        assert old_act.status_code == old_tap.status_code == 409 and len(taps) == action_count
        assert "画面编号已失效" in old_act.json()["detail"]
        assert "最新截图" in old_tap.json()["detail"]

        if platform != "ios":
            conflict = await during_capture(observe(), write_activity)
            assert conflict.status_code == 409 and lease.screen is None
            assert "取帧期间有原生操作" in conflict.json()["detail"]
        if platform == "android":
            latest = await observe()
            request = client.post(base + "/act", headers=headers, json=action_for(latest.json()))
            completed = await during_capture(request, write_activity)
            assert completed.status_code == 200 and len(taps) == action_count + 1
            receipt = completed.json()
            assert receipt["action_status"] == "executed" and receipt["observation"] is None
            assert "取帧期间有原生操作" in receipt["detail"] == receipt["observation_error"]
    finally:
        await sock.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(tunnel, 2)


@pytest.mark.parametrize(("method", "route", "body", "field"), [
    ("GET", "observe?expected_text=", None, "expected_text"),
    ("POST", "act", {"action": "tap", "x": 4, "y": 5}, "frame_id"),
    ("POST", "tap", {"frame_id": "frame", "x": -1, "y": 5}, ".x"),
])
async def test_frame_route_request_errors_have_text_detail(client, method, route, body, field):
    response = await client.request(method, f"{api.BASE}/sessions/lease/{route}", json=body)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert field in response.json()["detail"]
