from __future__ import annotations

import asyncio
import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from ai_phone.agent.workbench_orientation import observe_orientation
from ai_phone.server.models import Device
from ai_phone.server.workbench_orientation import OrientationRequests
from ai_phone.shared import protocol as P


class Display:
    def __init__(self, platform, *, naturally_landscape=False, locked=False):
        self.angle = int(naturally_landscape)
        self.offset = int(naturally_landscape)
        self.locked = locked
        self.commands = []
        self._device = self
        self._wda = self

    def rotation(self):
        return self.angle

    def shell(self, command, **kwargs):
        self.commands.append(command)
        if command == "wm size":
            return "Physical size: 600x300" if self.offset else "Physical size: 300x600"
        if "user_rotation" in command and not self.locked:
            self.angle = int(command.split()[-1])

    def rotation_quadrant(self):
        return self.angle

    def set_orientation(self, name):
        self.angle = {"PORTRAIT": 0, "LANDSCAPE": 1, "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT": 3}[name]

    def window_size(self):
        return (600, 300) if (self.angle - self.offset) % 2 else (300, 600)

    def screenshot_png(self):
        buf = io.BytesIO()
        Image.new("RGB", self.window_size()).save(buf, format="PNG")
        return buf.getvalue()


@pytest.mark.parametrize("platform", ["android", "ios_sim", "harmony"])
def test_native_receipt_confirms_each_cardinal_direction(platform):
    device = Display(platform, naturally_landscape=platform == "android")

    def harmony_rotate(driver, angle, _deadline):
        driver.angle = angle
        return True

    for direction, width, height in (("landscape_left", 600, 300), ("landscape_right", 600, 300),
                                     ("portrait", 300, 600)):
        result = observe_orientation(device, platform, direction, harmony_rotate=harmony_rotate,
                                     expires_at=time.time() + 2)
        assert result["orientation"] == direction
        assert (result["width"], result["height"]) == (width, height)
        assert bool(result.get("jpeg")) is (platform != "android")
    assert [cmd.rsplit(" ", 1)[1] for cmd in device.commands if "user_rotation" in cmd] in ([], ["2", "0", "1"])


def test_app_lock_does_not_fabricate_landscape_and_resync_does_not_rotate():
    device = Display("android", locked=True)
    with pytest.raises(RuntimeError, match="未确认目标方向"):
        observe_orientation(device, "android", "landscape_right", harmony_rotate=None,
                            expires_at=time.time() + 0.35)
    mutations = [command for command in device.commands if "settings" in command]
    result = observe_orientation(device, "android", None, harmony_rotate=None, expires_at=time.time() + 2)
    assert result["orientation"] == "portrait"
    assert [command for command in device.commands if "settings" in command] == mutations


async def test_only_matching_agent_serial_and_request_can_complete():
    requests = OrientationRequests()
    hub = SimpleNamespace(agent_id_for_serial=lambda _: "owner", send_to_agent=AsyncMock(return_value=True))
    task = asyncio.create_task(requests.request(hub, "emulator-5554", "landscape_right"))
    for _ in range(10):
        await asyncio.sleep(0)
        if hub.send_to_agent.await_args:
            break
    msg = hub.send_to_agent.await_args.args[1]
    reply = {"serial": msg["serial"], "request_id": msg["request_id"], "success": True}
    requests.resolve("different-agent", reply)
    requests.resolve("owner", {**reply, "serial": "other"})
    requests.resolve("owner", {**reply, "request_id": "stale"})
    assert not requests.pending["emulator-5554"][2].done()
    with pytest.raises(RuntimeError, match="正在切换"):
        await requests.request(hub, "emulator-5554", "portrait")
    requests.resolve("owner", reply)
    assert await task == reply
    assert not requests.pending


async def test_endpoint_checks_session_and_legacy_agent_before_dispatch(app, client, session, monkeypatch):
    from ai_phone.server.api import devices

    serial = "emulator-5554"
    session.add(Device(serial=serial, platform="android", status="online"))
    await session.commit()

    hub = SimpleNamespace(agent_id_for_serial=lambda _: "owner", agent_supports=lambda *_: False)
    app.dependency_overrides[devices.HubDep.dependency] = lambda: hub
    request = AsyncMock(return_value={"success": True, "result": {"orientation": "landscape_right"}})
    monkeypatch.setattr(devices.orientation_requests, "request", request)
    token = (await app.state.lock_store.acquire(serial, "fixture", "session")).token
    endpoint = f"/api/devices/{serial}/orientation"
    assert (await client.post(endpoint, json={"lock_token": "incorrect", "orientation": "portrait"})).status_code == 403
    assert (await client.post(endpoint, json={"lock_token": token, "orientation": "portrait"})).status_code == 422
    assert not request.called
    hub.agent_supports = lambda _, capability: capability == P.CAP_WORKBENCH_ORIENTATION
    result = await client.post(endpoint, json={"lock_token": token, "orientation": "landscape_right"})
    assert result.status_code == 200
    assert result.json() == {"orientation": "landscape_right"}
