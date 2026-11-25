"""Exercise the HTTP and Agent operation contracts without native ADB direct access."""
import io
from types import SimpleNamespace

import pytest
from adbutils import AdbDeviceInfo
from PIL import Image

from ai_phone.agent import unified_operations
from ai_phone.ai_connect import api
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.server.ws.agent_ws import _dispatch
from ai_phone.shared import protocol as P


def test_android_emulator_uses_its_current_local_transport(monkeypatch):
    rows = [
        AdbDeviceInfo("usb-vivo", "device", {"usb": "1", "transport_id": "19"}),
        AdbDeviceInfo("emulator-5554", "device", {"transport_id": "21"}),
        AdbDeviceInfo("127.0.0.1:5555", "device", {"transport_id": "22"}),
        AdbDeviceInfo("emulator-5556", "offline", {"transport_id": "23"}),
    ]
    monkeypatch.setattr(unified_operations.adb, "list", lambda *, extended: rows)
    assert unified_operations.device_transport("emulator-5554") == 21
    assert unified_operations.device_transport("usb-vivo") == 19
    for serial in ("127.0.0.1:5555", "emulator-5556"):
        with pytest.raises(unified_operations.OperationError):
            unified_operations.device_transport(serial)


async def test_unified_operations_use_the_agent_independently_of_the_native_connection(client, app, session, monkeypatch):
    serial = "usb-vivo"
    hub = app.state.hub = Hub()
    store = app.state.lock_store
    commands = []
    taps = []
    rotation = 0
    transport = 7
    tap_result = 0
    png = io.BytesIO()
    Image.new("RGB", (10, 20), color="blue").save(png, format="PNG")

    def shell(argv, *, encoding, timeout):
        assert argv == "screencap -p 2>/dev/null" and encoding is None and timeout > 0
        return png.getvalue()

    def shell2(argv, *, timeout, v2):
        assert argv[:2] == ["input", "tap"] and timeout > 0 and v2
        taps.append(tuple(map(int, argv[2:])))
        return SimpleNamespace(returncode=tap_result)

    def adb_device(*, transport_id):
        assert transport_id == transport
        return SimpleNamespace(
            rotation=lambda: rotation,
            window_size=lambda *, landscape: SimpleNamespace(width=100, height=200),
            shell=shell,
            shell2=shell2,
        )

    monkeypatch.setattr(unified_operations.adb, "device", adb_device)
    monkeypatch.setattr(
        unified_operations.adb, "list",
        lambda *, extended: [AdbDeviceInfo(serial, "device", {"usb": "1", "transport_id": str(transport)})],
    )

    class Agent:
        async def send(self, payload):
            await _dispatch(hub, store, "agent", payload)

    class Socket:
        async def send_json(self, payload):
            assert payload["type"] == P.MSG_UNIFIED_OPERATION
            commands.append(payload["action"])
            await unified_operations.handle(Agent(), payload)

    conn = await hub.register_agent("agent", "mac", "Darwin", Socket(), host_id="usb-host")
    hub.set_agent_capabilities("agent", [P.CAP_UNIFIED_OPERATIONS, P.CAP_ANDROID_OBSERVATION])
    await hub.set_devices("agent", {serial})
    hub.set_device_readiness(serial, {"ready": True})
    session.add(Device(serial=serial, agent_id="agent", platform="android", status="online"))
    await session.commit()

    assert (await client.get(api.BASE + "/devices")).json() == []
    assert (await client.post(api.BASE + "/sessions", json={"serial": serial})).status_code == 409
    assert store.peek(serial) is None
    conn.native_channel = NativeAgentChannel(conn.ws)
    conn.native_channel.devices[serial] = transport
    catalog = await client.get(api.BASE + "/devices")
    assert catalog.json()[0]["status"] == "available"
    response = await client.post(api.BASE + "/sessions", json={"serial": serial})
    assert response.status_code == 201, response.text
    lease = response.json()
    assert lease["native"]["protocol"] == "adb-connect"
    held = {"Authorization": "Bearer " + lease["token"]}
    lock = await client.post(f"/api/devices/{serial}/lock", json={"holder": "feature", "holder_type": "job"})
    assert lock.status_code == 409

    screen = await client.get(lease["api"]["screenshot"], headers=held)
    assert screen.status_code == 200 and screen.content == png.getvalue()
    assert screen.headers["x-ai-phone-width"] == "10" and screen.headers["x-ai-phone-height"] == "20"
    body = {"frame_id": screen.headers["x-ai-phone-frame-id"], "x": 4, "y": 12}
    tap_url = lease["api"]["tap"]
    assert (await client.post(tap_url, json=body)).status_code == 403
    assert (await client.post(tap_url, headers=held, json={**body, "x": 10})).status_code == 422
    assert commands == ["screenshot"]

    result = await client.post(tap_url, headers=held, json=body)
    assert result.json() == {"status": "executed", **body, "device_x": 40, "device_y": 120}
    assert taps == [(40, 120)]
    sent = commands.copy()
    assert (await client.post(tap_url, headers=held, json=body)).status_code == 409
    assert commands == sent

    async def fresh_body():
        latest = await client.get(lease["api"]["screenshot"], headers=held)
        assert latest.status_code == 200
        return {**body, "frame_id": latest.headers["x-ai-phone-frame-id"]}

    rotation_frame = await fresh_body()
    rotation = 1
    assert (await client.post(tap_url, headers=held, json=rotation_frame)).status_code == 409
    assert len(taps) == 1
    rotation = 0
    tap_result = 1
    assert (await client.post(tap_url, headers=held, json=await fresh_body())).status_code == 502
    transport_frame = await fresh_body()
    transport = 8
    assert (await client.post(tap_url, headers=held, json=transport_frame)).status_code == 409
    assert len(taps) == 2

    assert (await client.delete(lease["release"], headers=held)).status_code == 204
    assert (await client.get(lease["api"]["screenshot"], headers=held)).status_code == 410
    assert (await client.post(tap_url, headers=held, json=body)).status_code == 410
    assert store.peek(serial) is None
