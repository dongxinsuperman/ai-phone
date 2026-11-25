"""Public routes, Agent capability negotiation and generation-scoped simulator receipts."""

import base64
import io
import json
from types import SimpleNamespace
from collections import Counter

import pytest
import pytest_asyncio
from fastapi import HTTPException
from PIL import Image
from pydantic import TypeAdapter

from ai_phone.ai_connect import android_actions, api, unified
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.ai_connect.android_changes import changes
from ai_phone.ai_connect.android_actions import Action
from ai_phone.ai_connect.frame_fields import public_keyboard
from ai_phone.ai_connect.unified import MUTATIONS
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.server.ws.agent_ws import _dispatch
from ai_phone.shared import protocol as P
from ai_phone.shared.mobile_frame import build_frame


IOS = "06AC70ED-B449-43B7-81D2-E38BF84CDE1D"
HARMONY = "harmony-vm:managed-two"
USB = "usb-device"


class SimulatorWire:
    def __init__(self, hub, store):
        self.hub, self.store = hub, store
        self.calls = []
        self.bindings = {IOS: "private-boot-one", HARMONY: "private-boot-two"}
        self.actions = {IOS: ["observe", "screenshot", "tap", "open_app"], HARMONY: ["observe", "tap"]}
        self.revisions = Counter()
        self.invalid_structure = False
        self.foreign_replies = False
        image = io.BytesIO()
        Image.new("RGB", (32, 48), "lightgray").save(image, format="PNG")
        self.png = base64.b64encode(image.getvalue()).decode("ascii")

    def frame(self, serial, platform, return_image):
        self.revisions[serial] += 1
        state = self.revisions[serial] % 2 == 0
        if platform == "android":
            # A deployed observation Agent has no structure or image-omission flag.
            return {"width": 32, "height": 48, "display": {"rotation": 0}, "png": self.png,
                    "texts": ["Legacy heading"], "targets": [], "tree_available": True, "texts_truncated": False,
                    "keyboard": {"visible": None}, "dialogs": [], "warnings": []}
        nodes = [{"role": "list", "bounds": [0, 0, 32, 48], "children": [
            {"role": "toggle", "name": "Option", "bounds": [1, 4, 30, 24], "actions": ["tap"],
             "resource_id": "option", "state": {"checked": state}},
            {"role": "input", "bounds": [0, 29, 31, 46], "protected": True, "safe_label": "Password",
             "value": "private-input-value", "actions": ["tap"]},
            {"role": "heading", "text": "Details", "level": 4, "bounds": [1, 24, 30, 29]}]}]
        frame = build_frame(nodes, platform=platform, width=32, height=48, include_image=False,
                            display={"rotation": 0, "driver_endpoint": "private-driver-url"})
        frame["_raw_grid_v1"] = "private-grid"
        frame["raw_source"] = "private-tree"
        frame["targets"][0]["driver_endpoint"] = "private-driver-url"
        frame["structure"]["children"][0]["raw_attributes"] = {"value": "private-tree"}
        protected = frame["structure"]["children"][0]["children"][1]
        protected.update(value="private-input-value", children=[{"role": "text", "text": "private-input-value"}])
        if self.invalid_structure:
            frame["structure"]["children"][0]["belongs_to"] = "from-a-different-frame"
        if return_image:
            frame["png"] = self.png
        return frame

    async def send_json(self, message):
        self.calls.append(dict(message))
        serial, action = message["serial"], message["action"]
        result = {"type": P.MSG_UNIFIED_OPERATION_RESULT, "request_id": message["request_id"],
                  "serial": serial, "ok": True}
        if serial == USB:
            assert message["type"] == P.MSG_UNIFIED_OPERATION and "platform" not in message and "binding" not in message
            if action == "bind":
                assert "transport" not in message
                result.update(transport=7, android_observation=1)
            else:
                assert message["transport"] == 7
                if action == "observe":
                    result.update(self.frame(serial, "android", True))
        else:
            platform = "ios" if serial == IOS else "harmony"
            assert message["type"] == P.MSG_MOBILE_SIMULATOR_OPERATION and message["platform"] == platform
            assert "transport" not in message
            result["platform"] = platform
            if action == "bind":
                assert "binding" not in message
                result.update(binding=self.bindings[serial], action_types=self.actions[serial])
            else:
                result["binding"] = message["binding"]
                if message["binding"] != self.bindings[serial]:
                    result.update(ok=False, status=409, error="模拟器已重新启动")
                elif action == "observe":
                    result.update(self.frame(serial, platform, message["include_screenshot"]))
                elif action == "screenshot":
                    result.update(png=self.png, width=32, height=48, display={"rotation": 0})
                else:
                    assert action in self.actions[serial]
                if self.foreign_replies:
                    for mismatch in ({"serial": "another-device"}, {"platform": "android"}, {"binding": "different-boot"}):
                        await _dispatch(self.hub, self.store, "agent", {**result, **mismatch, "ok": False, "status": 418})
        await _dispatch(self.hub, self.store, "agent", result)


@pytest_asyncio.fixture
async def mobile(app, session):
    hub = app.state.hub = Hub()
    socket = SimulatorWire(hub, app.state.lock_store)
    conn = await hub.register_agent("agent", "test-host", "Darwin", socket, host_id="usb-host")
    conn.native_channel = NativeAgentChannel(socket)
    conn.native_channel.devices[USB] = 7
    hub.set_agent_capabilities("agent", [P.CAP_UNIFIED_OPERATIONS, P.CAP_ANDROID_OBSERVATION, P.CAP_MOBILE_SIMULATOR_OPERATIONS])
    ios = {"device_kind": "virtual", "is_virtual": True, "vm_platform": "ios", "vm_instance_id": "managed-one"}
    harmony = {**ios, "vm_platform": "harmony", "vm_instance_id": "managed-two", "hdc_serial": "private-hdc"}
    records = [(IOS, "ios_sim", ios, True), (HARMONY, "harmony", harmony, True), (USB, "android", {}, True),
               ("harmony-vm:other", "harmony", harmony, True), ("physical-ios", "ios", ios, True),
               ("unmanaged-hdc", "harmony", {}, True), ("emulator-5558", "android", {}, True),
               ("harmony-vm:sleeping", "harmony", {**harmony, "vm_instance_id": "sleeping"}, False)]
    for serial, platform, extra, ready in records:
        session.add(Device(serial=serial, agent_id="agent", platform=platform, status="online"))
        hub.set_device_extra(serial, extra)
        hub.set_device_readiness(serial, {"ready": ready})
    await hub.set_devices("agent", {row[0] for row in records})
    await session.commit()
    return socket


async def claim(client, serial):
    response = await client.post(api.BASE + "/sessions", json={"serial": serial})
    assert response.status_code == 201, response.text
    lease = response.json()
    return lease, {"Authorization": "Bearer " + lease["token"]}


async def test_catalog_and_claim_require_only_the_native_channel(client, mobile, app):
    rows = (await client.get(api.BASE + "/devices")).json()
    assert [(r["serial"], r["status"]) for r in rows] == [(USB, "available")]
    assert not mobile.calls and not api.state(app).leases
    for serial in (IOS, HARMONY, "emulator-5558"):
        assert (await client.post(api.BASE + "/sessions", json={"serial": serial})).status_code == 409
        assert mobile.store.peek(serial) is None
    mobile.hub.set_agent_capabilities("agent", [P.CAP_AI_DIRECT_NATIVE, P.CAP_MOBILE_SIMULATOR_OPERATIONS])
    assert [row["serial"] for row in (await client.get(api.BASE + "/devices")).json()] == [USB]
    native_only, headers = await claim(client, USB)
    assert native_only["native"]["protocol"] == "adb-connect" and native_only["api"] == {}
    assert (await client.delete(native_only["release"], headers=headers)).status_code == 204
    assert not mobile.calls and not api.state(app).leases


async def simulator_target(mobile, serial):
    platform = "ios" if serial == IOS else "harmony"
    target = await unified.bind(mobile.hub, "agent", serial, platform)
    return SimpleNamespace(serial=serial, unified_target=target, screen=None)


@pytest.mark.parametrize("serial", [IOS, HARMONY])
async def test_simulator_adapter_keeps_scoped_targets_screenshots_and_receipts(mobile, serial):
    # These observation adapters exist independently of a public dual-channel lease.
    lease = await simulator_target(mobile, serial)
    mobile.foreign_replies = True
    before = await android_actions.observe(mobile.hub, lease)
    previous = lease.screen
    assert "private-" not in json.dumps(before)
    toggle, _, heading = before["structure"]["children"][0]["children"]
    assert toggle["target"] == before["targets"][0]["id"] and toggle["actions"] == ["tap"]
    assert (heading["role"], heading["level"], heading["text"]) == ("heading", 4, "Details")
    tap = android_actions.Tap(action="tap", frame_id=before["frame_id"], target=toggle["target"])
    await unified.operate(mobile.hub, lease, "tap", **android_actions.parameters(tap, previous))
    after = await android_actions.observe(mobile.hub, lease)
    assert before["frame_id"] != after["frame_id"]
    for frame in (before, after):
        assert frame["screenshot"]["annotated"] and frame["screenshot"]["data"]
    assert changes(previous.observation, lease.screen.observation)["state_changes"] == [
        {"id": toggle["target"], "name": "Option", "property": "checked", "before": False, "after": True}]
    with pytest.raises(HTTPException, match="409"):
        android_actions.parameters(tap, lease.screen)
    if serial == IOS:
        await unified.operate(mobile.hub, lease, "open_app", package="com.example.my-app")
        assert mobile.calls[-1]["package"] == "com.example.my-app"
        assert (await unified.screenshot(mobile.hub, lease)).body == base64.b64decode(mobile.png)
    else:
        with pytest.raises(HTTPException, match="422"):
            await unified.operate(mobile.hub, lease, "home")


async def test_bad_bind_bad_source_and_simulator_restart_cannot_authorize_stale_inputs(mobile):
    mobile.actions[IOS] = ["tap"]
    with pytest.raises(HTTPException, match="502"):
        await simulator_target(mobile, IOS)
    mobile.actions[IOS].append("observe")
    lease = await simulator_target(mobile, IOS)
    before = await android_actions.observe(mobile.hub, lease)
    tap = android_actions.Tap(action="tap", frame_id=before["frame_id"], target=before["targets"][0]["id"])
    mobile.invalid_structure = True
    with pytest.raises(HTTPException, match="502"):
        await android_actions.observe(mobile.hub, lease)
    with pytest.raises(HTTPException, match="409"):
        android_actions.parameters(tap, lease.screen)
    mobile.invalid_structure = False
    fresh = await android_actions.observe(mobile.hub, lease)
    tap = tap.model_copy(update={"frame_id": fresh["frame_id"]})
    mobile.bindings[IOS] = "new-boot"
    with pytest.raises(HTTPException, match="409"):
        await unified.operate(mobile.hub, lease, "tap", **android_actions.parameters(tap, lease.screen))
    with pytest.raises(HTTPException, match="409"):
        await android_actions.observe(mobile.hub, lease)
    new_lease = await simulator_target(mobile, IOS)
    assert (await android_actions.observe(mobile.hub, new_lease))["screenshot"]
    assert mobile.calls[-1]["binding"] == "new-boot"


async def test_simulator_that_loses_readiness_while_binding_is_rejected(mobile):
    send = mobile.send_json

    async def drop_readiness(message):
        mobile.hub.set_device_readiness(IOS, {"ready": False})
        await send(message)

    mobile.send_json = drop_readiness
    with pytest.raises(HTTPException, match="503"):
        await simulator_target(mobile, IOS)


async def test_each_public_frame_includes_both_entries_and_exactly_one_png_even_for_old_queries(client, mobile):
    lease, headers = await claim(client, USB)
    assert lease["native"]["protocol"] == "adb-connect"
    first = await client.get(lease["api"]["observe"] + "?include_screenshot=false", headers=headers)
    assert first.status_code == 200
    frame = first.json()
    assert frame["structure"]["children"] == [{"role": "text", "text": "Legacy heading", "source": "unknown"}]
    invalid = await client.post(lease["api"]["act"], headers=headers,
                                json={"action": "open_app", "package": "com.example.my-app"})
    assert invalid.status_code == 422 and all(m["action"] != "open_app" for m in mobile.calls)
    acted = await client.post(lease["api"]["act"] + "?include_screenshot=false", headers=headers,
                              json={"action": "tap", "frame_id": frame["frame_id"], "x": 4, "y": 5})
    assert acted.status_code == 200
    after = acted.json()["observation"]
    assert after["frame_id"] != frame["frame_id"]
    for response, page in ((first, frame), (acted, after)):
        for field in ("session_id", "native", "api", "action_types"):
            assert page[field] == lease[field]
        assert lease["token"] not in response.text
        assert response.text.count(mobile.png) == 1
        assert page["screenshot"]["data"] == mobile.png and page["targets"] == []
    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    assert (await client.get(frame["api"]["observe"], headers=headers)).status_code == 410


async def test_public_guide_examples_validate_against_the_action_route(client):
    response = await client.get(api.BASE + "/guide")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    for field in ("native", "api.observe", "api.act", "structure.children", "action_types", "keyboard.occlusion_bounds"):
        assert field in response.text
    assert "include_screenshot" not in response.text
    examples = [json.loads(line[line.index("{"):]) for line in response.text.splitlines() if '{"action":' in line]
    validator = TypeAdapter(Action)
    assert {validator.validate_python(body).action for body in examples} == set(MUTATIONS)


async def test_keyboard_occlusion_and_picture_are_returned_together(mobile):
    lease = await simulator_target(mobile, IOS)
    keyboard = {"visible": True, "source": "system", "bounds": [3, 32, 29, 48],
                "occlusion_bounds": [0, 26, 32, 48]}
    old_frame = mobile.frame
    mobile.frame = lambda *args: {**old_frame(*args), "keyboard": {**keyboard, "raw_tree": "private-overlay"}}
    frame = await android_actions.observe(mobile.hub, lease)
    tap = android_actions.Tap(action="tap", frame_id=frame["frame_id"], target=frame["targets"][0]["id"])
    await unified.operate(mobile.hub, lease, "tap", **android_actions.parameters(tap, lease.screen))
    for page in (frame, await android_actions.observe(mobile.hub, lease)):
        assert page["keyboard"] == keyboard and page["screenshot"]["annotated"]


@pytest.mark.parametrize("extra", [
    {}, {"occlusion_bounds": [0, -1, 32, 48]}, {"occlusion_bounds": [0, 27, 33, 48]},
    {"occlusion_bounds": [0, 33, 32, 48]}, {"occlusion_bounds": [4, 26, 32, 48]},
    {"occlusion_bounds": [False, 28, 32, 48]}, {"occlusion_bounds": [0, 26, 32]},
    {"visible": False, "occlusion_bounds": [0, 26, 32, 48]},
    {"bounds": None, "occlusion_bounds": [0, 26, 32, 48]},
])
def test_missing_or_unverified_occlusion_keeps_the_original_keyboard_fields(extra):
    report = {"visible": True, "source": "system", "bounds": [3, 32, 29, 48], **extra}
    assert public_keyboard(report, 32, 48) == {k: v for k, v in report.items() if k != "occlusion_bounds"}
