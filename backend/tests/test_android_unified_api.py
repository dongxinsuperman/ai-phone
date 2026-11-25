"""Public Android observe/act behavior over a regular Agent socket."""
import base64
import asyncio
import io
import time
from collections import Counter

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from ai_phone.ai_connect import api, unified
from ai_phone.ai_connect.agent_channel import NativeAgentChannel
from ai_phone.agent.android_frame_marks import annotate_frame
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.server.ws.agent_ws import _dispatch, _on_disconnect
from ai_phone.shared import protocol as P


class StubAgent:
    transports = {"usb-one": 7, "usb-two": 11, "usb-legacy": 13}

    def __init__(self, hub, store):
        self.hub = hub
        self.store = store
        self.calls = []
        self.revisions = Counter()
        self.errors = {}
        output = io.BytesIO()
        Image.new("RGB", (32, 48), "blue").save(output, format="PNG")
        self.png = base64.b64encode(output.getvalue()).decode("ascii")

    def observation(self, serial):
        self.revisions[serial] += 1
        revision = self.revisions[serial]
        # A1 refers to a new location on each snapshot, even on the same phone.
        point = [2 + revision, self.transports[serial]]
        return {
            "png": self.png, "width": 32, "height": 48,
            "display": {"width": 32, "height": 48, "rotation": 0},
            "package": "com.example.reader", "activity": ".ReaderActivity", "rotation": 0,
            "texts": [f"{serial} page {revision}"],
            "targets": [
                {"id": "A1", "name": "Next", "role": "control", "source": "android", "actions": ["tap"],
                 "bounds": [0, 0, 32, 20], "point": point, "state": {"selected": revision % 2 == 0},
                 "state_evidence": {"selected": "owned_name_node"}},
                {"id": "A2", "name": "Chapters", "role": "list", "source": "android", "actions": ["scroll"],
                 "bounds": [0, 20, 32, 48], "point": [15, 32], "state": {}},
                {"id": "V1", "name": None, "source": "pixels", "role": "region", "actions": ["try_tap"],
                 "bounds": [20, 5, 31, 30], "point": [23, 11 + revision], "state": {}},
            ],
            "keyboard": {"visible": False}, "dialogs": [], "warnings": [],
            "tree_available": True, "texts_truncated": False, "targets_truncated": False,
        }

    async def send_json(self, message):
        assert message["type"] == P.MSG_UNIFIED_OPERATION
        serial, action = message["serial"], message["action"]
        self.calls.append(message.copy())
        response = {"type": P.MSG_UNIFIED_OPERATION_RESULT, "request_id": message["request_id"],
                    "serial": serial, "ok": True}
        if action == "bind":
            response["transport"] = self.transports[serial]
            if serial != "usb-legacy":
                response["android_observation"] = 1
        else:
            assert message["transport"] == self.transports[serial]
            if action in self.errors:
                response.update(ok=False, status=self.errors[action], error=f"{action} failed at device")
            elif action in {"observe", "screenshot"}:
                response.update(self.observation(serial))
            elif action == "tap":
                response.update(device_x=message["x"], device_y=message["y"])
            else:
                assert action in {"back", "home", "scroll", "open_app", "input"}
        await _dispatch(self.hub, self.store, "agent", response)

    def mutations(self):
        return [msg for msg in self.calls if msg["action"] not in {"bind", "screenshot", "observe"}]


@pytest_asyncio.fixture
async def agent(app, session):
    hub = app.state.hub = Hub()
    socket = StubAgent(hub, app.state.lock_store)
    conn = await hub.register_agent("agent", "mac", "Darwin", socket, host_id="usb-host")
    conn.native_channel = NativeAgentChannel(socket)
    conn.native_channel.devices = socket.transports.copy()
    hub.set_agent_capabilities("agent", [P.CAP_UNIFIED_OPERATIONS, P.CAP_ANDROID_OBSERVATION])
    await hub.set_devices("agent", set(socket.transports))
    for serial in socket.transports:
        session.add(Device(serial=serial, agent_id="agent", platform="android", status="online"))
        hub.set_device_readiness(serial, {"ready": True})
    await session.commit()
    return socket


async def claim(client, serial):
    response = await client.post(api.BASE + "/sessions", json={"serial": serial})
    assert response.status_code == 201, response.text
    lease = response.json()
    return lease, {"Authorization": "Bearer " + lease["token"]}


async def observe(client, lease, headers):
    response = await client.get(lease["api"]["observe"], headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_online_android_emulator_is_listed_when_native_transport_is_ready(client, app, session, agent):
    serial = "emulator-5554"
    host = agent.hub.agent_native_channel("agent")
    await agent.hub.set_devices("agent", {*agent.transports, serial})
    agent.hub.set_device_readiness(serial, {"ready": True})
    session.add(Device(serial=serial, agent_id="agent", platform="android", status="online"))
    await session.commit()

    async def listed():
        rows = (await client.get(api.BASE + "/devices")).json()
        return next((row["status"] for row in rows if row["serial"] == serial), None)

    assert await listed() is None
    agent.transports = {**agent.transports, serial: 21}
    host.devices[serial] = 21
    assert await listed() == "available"
    lease, headers = await claim(client, serial)
    assert lease["native"]["protocol"] == "adb-connect"
    assert await listed() == "busy"
    assert agent.calls == []
    frame = await observe(client, lease, headers)
    assert frame["texts"] == [f"{serial} page 1"]
    assert agent.calls[-1]["transport"] == 21
    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    assert (await client.get(lease["api"]["observe"], headers=headers)).status_code == 410
    assert await listed() == "available"


async def test_claim_delivers_only_the_auxiliary_endpoints_the_agent_supports(client, agent):
    modern, modern_headers = await claim(client, "usb-one")
    assert modern["native"]["protocol"] == "adb-connect"
    assert {"observe", "act", "screenshot", "tap"} <= modern["api"].keys()
    assert agent.calls == []
    home = await client.post(modern["api"]["act"], headers=modern_headers, json={"action": "home"})
    assert home.status_code == 200 and home.json()["action_status"] == "executed"
    assert home.json()["observation"]["frame_id"] and home.json()["changes"] is None
    agent.hub.set_agent_capabilities("agent", [P.CAP_UNIFIED_OPERATIONS])
    basic, _ = await claim(client, "usb-legacy")
    assert set(basic["api"]) == {"screenshot", "tap"}
    agent.hub.set_agent_capabilities("agent", [P.CAP_AI_DIRECT_NATIVE])
    rows = (await client.get(api.BASE + "/devices")).json()
    assert {row["serial"] for row in rows} == {"usb-one", "usb-two", "usb-legacy"}
    native_only, headers = await claim(client, "usb-two")
    assert native_only["native"]["protocol"] == "adb-connect"
    assert native_only["api"] == {} and native_only["action_types"] == []
    assert (await client.get(native_only["session"] + "/observe", headers=headers)).status_code == 409
    assert (await client.delete(native_only["release"], headers=headers)).status_code == 204


@pytest.mark.parametrize("interruption", ["agent_failure", "transport_lost", "host_replaced", "lost_lock"])
async def test_claim_does_not_publish_a_lease_when_native_binding_changes(client, app, agent, interruption, monkeypatch):
    direct = api.state(app)
    original = agent.store.acquire

    async def interrupted(*args, **kwargs):
        lock = await original(*args, **kwargs)
        if interruption == "agent_failure":
            await agent.hub.unregister_agent("agent")
        if interruption == "transport_lost":
            agent.hub.agent_native_channel("agent").devices.pop("usb-one")
        if interruption == "host_replaced":
            conn = await agent.hub.register_agent("agent2", "mac", "Darwin", agent, host_id="usb-host")
            conn.native_channel = NativeAgentChannel(agent)
            conn.native_channel.devices = agent.transports.copy()
            await agent.hub.set_devices("agent2", set(agent.transports))
        if interruption == "lost_lock":
            await original("usb-one", "feature-job", "job", force=True)
        return lock

    monkeypatch.setattr(agent.store, "acquire", interrupted)
    response = await client.post(api.BASE + "/sessions", json={"serial": "usb-one"})
    assert response.status_code == 409
    assert not direct.leases and "token" not in response.json()
    remaining = agent.store.peek("usb-one")
    assert (remaining.holder if remaining else None) == ("feature-job" if interruption == "lost_lock" else None)


async def test_observe_and_act_return_new_frames_and_dispatch_typed_actions(client, agent):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    assert frame["platform"] == "android" and frame["frame_id"]
    assert frame["screenshot"]["mime_type"] == "image/png" and frame["screenshot"]["annotated"] is True
    assert frame["texts"] == ["usb-one page 1"] and frame["targets"][0]["id"] == "A1"
    assert frame["keyboard"]["visible"] is False and frame["dialogs"] == []
    assert frame["tree_available"] is True and frame["texts_truncated"] is False and not frame["warnings"]
    assert frame["targets_truncated"] is False and frame["package"] == "com.example.reader"

    selected_changes = []
    for body in [
        {"action": "tap", "target": "A1"}, {"action": "tap", "x": 4, "y": 44},
        {"action": "scroll", "direction": "down", "target": "A2"}, {"action": "scroll", "direction": "up"},
        {"action": "back"}, {"action": "home"}, {"action": "input", "text": "chapter 2"},
        {"action": "open_app", "package": "com.example.reader"},
    ]:
        sent = len(agent.mutations())
        response = await client.post(
            lease["api"]["act"], headers=headers, json={**body, "frame_id": frame["frame_id"]}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["action_status"] == "executed"
        fresh = data["observation"]
        assert fresh["frame_id"] != frame["frame_id"]
        assert fresh["texts"][0] in data["changes"]["added_text"]
        dispatched, = agent.mutations()[sent:]
        assert dispatched["serial"] == lease["serial"] and dispatched["action"] == body["action"]
        for field in ("text", "direction", "package", "x", "y"):
            if field in body:
                assert dispatched[field] == body[field]
        if body.get("target") == "A1":
            assert [dispatched["x"], dispatched["y"]] == frame["targets"][0]["point"]
            selected_changes = data["changes"]["state_changes"]
        if body["action"] == "scroll":
            assert dispatched.get("bounds") == (frame["targets"][1]["bounds"] if body.get("target") else None)
        frame = fresh
    change = {"id": "A1", "name": "Next", "property": "selected", "before": False, "after": True}
    assert change in selected_changes


async def test_visual_target_can_be_used_directly_from_the_latest_action_response(client, agent):
    lease, headers = await claim(client, "usb-one")
    before = await observe(client, lease, headers)
    response = await client.post(lease["api"]["act"], headers=headers, json={"action": "back"})
    assert response.status_code == 200, response.text
    current = response.json()["observation"]
    previous_target = next(t for t in before["targets"] if t["id"] == "V1")
    target = next(t for t in current["targets"] if t["id"] == "V1")
    assert target["source"] == "pixels" and target["actions"] == ["try_tap"]
    assert current["frame_id"] != before["frame_id"] and target["point"] != previous_target["point"]

    tap = await client.post(lease["api"]["act"], headers=headers, json={
        "action": "tap", "frame_id": current["frame_id"], "target": target["id"],
    })
    assert tap.status_code == 200 and tap.json()["action_status"] == "executed"
    assert [m["action"] for m in agent.calls] == ["observe", "back", "observe", "tap", "observe"]
    dispatched = agent.mutations()[-1]
    assert [dispatched["x"], dispatched["y"]] == target["point"]


async def test_target_numbers_belong_to_one_frame_and_lease(client, agent):
    one, headers_one = await claim(client, "usb-one")
    two, headers_two = await claim(client, "usb-two")
    stale = await observe(client, one, headers_one)
    other = await observe(client, two, headers_two)
    fresh = await observe(client, one, headers_one)
    assert fresh["targets"][0]["point"] != stale["targets"][0]["point"]

    for lease, headers, frame_id, status in [
        (one, headers_two, fresh["frame_id"], 403),
        (one, headers_one, stale["frame_id"], 409),
        (two, headers_two, fresh["frame_id"], 409),
        (one, headers_one, other["frame_id"], 409),
    ]:
        response = await client.post(lease["api"]["act"], headers=headers,
                                     json={"action": "tap", "frame_id": frame_id, "target": "A1"})
        assert response.status_code == status, response.text
    invalid = await client.post(one["api"]["act"], headers=headers_one,
                                json={"action": "tap", "frame_id": fresh["frame_id"], "target": "A99"})
    assert 400 <= invalid.status_code < 500 and not agent.mutations()
    body = {"action": "tap", "frame_id": fresh["frame_id"], "target": "A1"}
    result = await client.post(one["api"]["act"], headers=headers_one, json=body)
    assert result.status_code == 200, result.text
    tap, = agent.mutations()
    assert tap["serial"] == "usb-one" and [tap["x"], tap["y"]] == fresh["targets"][0]["point"]
    last_frame_id = result.json()["observation"]["frame_id"]
    assert (await client.delete(one["release"], headers=headers_one)).status_code == 204
    old_action = {**body, "frame_id": last_frame_id}
    revoked = await client.post(one["api"]["act"], headers=headers_one, json=old_action)
    assert revoked.status_code == 410
    rebound, new_headers = await claim(client, "usb-one")
    await observe(client, rebound, new_headers)
    old_frame = await client.post(rebound["api"]["act"], headers=new_headers, json=old_action)
    assert old_frame.status_code == 409 and len(agent.mutations()) == 1


async def test_device_receipt_is_independent_of_observation_failure_and_never_replayed(client, agent):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    agent.errors["observe"] = 502
    body = {"action": "tap", "target": "A1", "frame_id": frame["frame_id"]}
    response = await client.post(lease["api"]["act"], headers=headers, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["action_status"] == "executed" and data["observation"] is None and data["changes"] is None
    assert data["native"] == lease["native"] and data["api"] == lease["api"]
    assert len(agent.mutations()) == 1
    replay = await client.post(lease["api"]["act"], headers=headers, json=body)
    assert replay.status_code == 409 and len(agent.mutations()) == 1
    del agent.errors["observe"]
    for error_status in (409, 504):
        fresh = await observe(client, lease, headers)
        agent.errors["tap"] = error_status
        sent = len(agent.mutations())
        result = await client.post(
            lease["api"]["act"], headers=headers, json={**body, "frame_id": fresh["frame_id"]}
        )
        assert result.status_code == error_status, result.text
        if error_status == 504:
            assert result.json()["action_status"] == "unknown" and result.json()["observation"] is None
            assert result.json()["native"] == lease["native"]
        assert len(agent.mutations()) == sent + 1


async def test_android_act_waits_500ms_after_the_receipt_before_observing(client, agent, monkeypatch):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    timestamps = {}
    send = agent.send_json

    async def record(message):
        if message["action"] == "observe":
            timestamps["observe"] = time.monotonic()
        await send(message)
        if message["action"] == "tap":
            timestamps["receipt"] = time.monotonic()

    monkeypatch.setattr(agent, "send_json", record)
    response = await client.post(lease["api"]["act"], headers=headers, json={
        "action": "tap", "target": "A1", "frame_id": frame["frame_id"],
    })
    assert response.status_code == 200
    assert response.json()["observation"]["frame_id"] != frame["frame_id"]
    assert timestamps["observe"] - timestamps["receipt"] >= 0.5


@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
async def test_observation_transport_failure_preserves_successful_tap_receipt(client, agent, monkeypatch, failure):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    send = agent.send_json
    if failure == "timeout":
        monkeypatch.setattr(unified, "COMMAND_TIMEOUT", -1.9)

    async def send_without_observation(message):
        if message["action"] != "observe":
            await send(message)
        elif failure == "disconnect":
            await _on_disconnect(agent.hub, agent.store, "agent", set(), ws=agent)

    monkeypatch.setattr(agent, "send_json", send_without_observation)
    response = await client.post(lease["api"]["act"], headers=headers, json={
        "action": "tap", "target": "A1", "frame_id": frame["frame_id"],
    })
    assert response.status_code == 200
    payload = response.json()
    detail = {"timeout": "等待设备返回画面超时，请重新观察", "disconnect": "设备连接已中断，未取得画面"}[failure]
    assert payload["action_status"] == "executed"
    assert payload["observation"] is None
    assert payload["observation_error"] == detail
    assert len(agent.mutations()) == 1
    if failure == "disconnect":
        followup = await client.get(lease["api"]["observe"], headers=headers)
        assert followup.status_code == 503
        assert "Agent 已离线" in followup.json()["detail"]


async def test_missing_png_with_geometry_does_not_publish_a_frame_or_replay_input(client, agent):
    lease, headers = await claim(client, "usb-one")
    before = await observe(client, lease, headers)
    original = agent.observation

    def without_png(serial):
        frame = original(serial)
        del frame["png"]
        return frame

    agent.observation = without_png
    body = {"action": "tap", "target": "A1", "frame_id": before["frame_id"]}
    receipt = (await client.post(lease["api"]["act"], json=body, headers=headers)).json()
    assert receipt["action_status"] == "executed" and receipt["observation"] is None
    assert receipt["native"] == lease["native"] and "frame_id" not in receipt
    assert (await client.post(lease["api"]["act"], json=body, headers=headers)).status_code == 409
    read = await client.get(lease["api"]["observe"], headers=headers)
    assert read.status_code == 502 and read.json()["observation"] is None
    assert read.json()["native"] == lease["native"] and read.json()["api"] == lease["api"]
    assert len(agent.mutations()) == 1


async def test_native_activity_invalidates_only_its_own_frame_and_an_overlapping_capture(client, app, agent):
    one, first_headers = await claim(client, "usb-one")
    two, second_headers = await claim(client, "usb-two")
    first = await observe(client, one, first_headers)
    second = await observe(client, two, second_headers)
    direct = api.state(app)
    lease = direct.leases[one["session_id"]]
    await direct.activity(lease, agent.store, agent.hub)
    stale = await client.post(first["api"]["act"], json={"action": "tap", "frame_id": first["frame_id"], "target": "A1"},
                              headers=first_headers)
    assert stale.status_code == 409 and not agent.mutations()
    still_valid = await client.post(second["api"]["act"], json={"action": "tap", "frame_id": second["frame_id"], "target": "A1"},
                                    headers=second_headers)
    assert still_valid.status_code == 200
    original = agent.send_json

    async def concurrent(message):
        await direct.activity(lease, agent.store, agent.hub)
        await original(message)

    agent.send_json = concurrent
    overlapping = await client.get(first["api"]["observe"], headers=first_headers)
    assert overlapping.status_code == 409 and lease.screen is None
    agent.send_json = original
    fresh = await observe(client, one, first_headers)
    assert fresh["frame_id"] != first["frame_id"] and fresh["native"] == first["native"]


async def test_agent_id_reregistered_on_another_host_cannot_operate_the_claimed_phone(client, agent):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    await agent.hub.register_agent("agent", "different-mac", "Darwin", agent, host_id="other-usb-host")
    agent.hub.set_agent_capabilities("agent", [P.CAP_UNIFIED_OPERATIONS])
    await agent.hub.set_devices("agent", {"usb-one"})
    response = await client.post(lease["api"]["act"], headers=headers, json={
        "action": "tap", "frame_id": frame["frame_id"], "target": frame["targets"][0]["id"]})
    assert response.status_code == 409 and not agent.mutations()


async def test_marked_screenshot_and_located_text_have_the_same_contract_on_observe_and_act(client, agent):
    lease, headers = await claim(client, "usb-one")
    original = agent.observation
    marked_image = Image.new("RGB", (32, 48), "blue")
    ImageDraw.Draw(marked_image).text((1, 1), "A1", fill="white")
    output = io.BytesIO()
    marked_image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")

    def with_evidence(serial):
        result = original(serial)
        title = result["texts"][0]
        caption = f"caption {agent.revisions[serial]}"
        result["targets"][0].update(name_source="system", system_name_source="text", name_bounds=[0, 0, 12, 8])
        result["targets"][2].update(name=caption, name_source="screen", name_bounds=[20, 5, 30, 14])
        return {**result, "png": encoded, "marked_targets": True, "texts": [title, caption],
                "system_texts": [{"text": title, "bounds": [0, 0, 12, 8], "source": "system", "target": "A1"}],
                "screen_texts": [{"text": caption, "bounds": [20, 5, 30, 14], "source": "screen", "target": "V1"}],
                "screen_texts_truncated": False}

    agent.observation = with_evidence
    first = await observe(client, lease, headers)
    result = await client.post(lease["api"]["act"], headers=headers, json={"action": "back", "frame_id": first["frame_id"]})
    assert result.status_code == 200
    second = result.json()["observation"]
    for i, frame in enumerate((first, second), 1):
        assert frame["marked_targets"] is True
        assert frame["screenshot"] == {"data": encoded, "mime_type": "image/png", "annotated": True, "marked_targets": True}
        assert frame["targets"][0]["system_name_source"] == "text"
        assert frame["targets"][2]["name_source"] == "screen" and frame["targets"][2]["source"] == "pixels"
        assert frame["screen_texts"][0]["target"] == frame["targets"][2]["id"]
        assert frame["screen_texts"][0]["text"] == f"caption {i}" and frame["screen_texts_truncated"] is False
        assert frame["system_texts"][0]["bounds"] == frame["targets"][0]["name_bounds"]
    assert set(result.json()["changes"]["added_text"]) == {"usb-one page 2", "caption 2"}
    assert result.json()["changes"]["text_sources"] == ["system", "screen"]

    agent.observation = original
    old = await observe(client, lease, headers)
    rendered = annotate_frame(base64.b64decode(agent.png), old["targets"])
    assert base64.b64decode(old["screenshot"]["data"]) == rendered != base64.b64decode(agent.png)
    assert old["screenshot"]["annotated"] and old["marked_targets"]
    assert "screen_texts" not in old and "system_texts" not in old

    # A modern Agent can read a caption on a screen with no target to mark.
    agent.observation = lambda serial: {**original(serial), "targets": [], "marked_targets": False,
                                        "tree_available": False, "system_texts": [],
                                        "screen_texts": [{"text": "Caption", "bounds": [1, 3, 20, 10], "target": None}]}
    empty = await observe(client, lease, headers)
    assert empty["targets"] == [] and empty["screen_texts"][0]["text"] == "Caption"
    assert not empty["screenshot"]["annotated"] and not empty["marked_targets"]


@pytest.mark.parametrize("failure", ["observe_error", "incomplete_png", "image_size", "invalid_text_records", "dangling_text_target", "duplicate_target", "screen_error"])
async def test_failed_frame_refresh_invalidates_both_old_action_routes(client, agent, failure):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    endpoint = lease["api"]["observe"]
    if failure in {"observe_error", "screen_error"}:
        operation = "screenshot" if failure == "screen_error" else "observe"
        agent.errors[operation] = 502
        endpoint = lease["api"][operation]
    else:
        original = agent.observation
        overrides = {"incomplete_png": {"png": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")},
                     "image_size": {"width": 2}, "invalid_text_records": {"screen_texts": "bad"},
                     "dangling_text_target": {"screen_texts": [{"text": "Read", "target": "A99"}]},
                     "duplicate_target": {"targets": [{"id": "A1", "actions": ["tap"]}] * 2}}
        override = overrides[failure]
        agent.observation = lambda serial: {**original(serial), **override}
    assert (await client.get(endpoint, headers=headers)).status_code == 502
    point = {"frame_id": frame["frame_id"], "x": 1, "y": 2}
    old_tap = await client.post(lease["api"]["tap"], headers=headers, json=point)
    old_target = await client.post(lease["api"]["act"], headers=headers, json={"action": "tap", "frame_id": frame["frame_id"], "target": "A1"})
    assert old_tap.status_code == old_target.status_code == 409 and not agent.mutations()


async def test_coordinate_tap_consumes_observation_frame_and_raw_screen_has_no_target_ids(client, agent):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    point = {"frame_id": frame["frame_id"], "x": 4, "y": 7}
    assert (await client.post(lease["api"]["tap"], headers=headers, json=point)).status_code == 200
    old = await client.post(lease["api"]["act"], headers=headers, json={"action": "tap", "target": "A1", "frame_id": frame["frame_id"]})
    repeat = await client.post(lease["api"]["tap"], headers=headers, json=point)
    assert old.status_code == repeat.status_code == 409
    raw = await client.get(lease["api"]["screenshot"], headers=headers)
    raw_id = raw.headers["x-ai-phone-frame-id"]
    no_ids = await client.post(lease["api"]["act"], headers=headers, json={"action": "tap", "frame_id": raw_id, "target": "A1"})
    assert no_ids.status_code == 409

async def test_observation_timeout_still_allows_screenshot_coordinate_tap_and_next_screenshot(client, agent):
    lease, headers = await claim(client, "usb-one")
    agent.errors["observe"] = 504
    failed = await client.get(lease["api"]["observe"], headers=headers)
    assert failed.status_code == 504 and failed.json()["observation"] is None

    snapshot = await client.get(lease["api"]["screenshot"], headers=headers)
    assert snapshot.status_code == 200 and snapshot.content == base64.b64decode(agent.png)
    point = {"frame_id": snapshot.headers["x-ai-phone-frame-id"], "x": 4, "y": 7}
    acted = await client.post(lease["api"]["tap"], headers=headers, json=point)
    assert acted.status_code == 200 and acted.json()["status"] == "executed"
    repeated = await client.post(lease["api"]["tap"], headers=headers, json=point)
    assert repeated.status_code == 409 and len(agent.mutations()) == 1

    after = await client.get(lease["api"]["screenshot"], headers=headers)
    assert after.status_code == 200
    assert after.headers["x-ai-phone-frame-id"] != point["frame_id"]
    assert (await client.delete(lease["release"], headers=headers)).status_code == 204
    assert (await client.get(lease["api"]["screenshot"], headers=headers)).status_code == 410


async def test_standalone_screen_text_target_uses_current_frame_point_and_screen_only_diff(client, agent):
    lease, headers = await claim(client, "usb-one")
    original = agent.observation

    def visual(serial):
        o = original(serial)
        n = agent.revisions[serial]
        label, rect = f"Caption {n}", [n, 9, n + 12, 18]
        return {**o, "texts": [label], "tree_available": False, "system_texts": [], "marked_targets": True,
                "screen_texts": [{"text": label, "bounds": rect, "target": "T1", "source": "screen"}],
                "targets": [{"id": "T1", "name": label, "name_source": "screen", "source": "pixels",
                             "actions": ["try_tap"], "bounds": rect, "name_bounds": rect, "point": [n + 6, 13]}]}

    agent.observation = visual
    old = await observe(client, lease, headers)
    action_url = lease["api"]["act"]
    def tap(frame):
        return {"action": "tap", "frame_id": frame["frame_id"], "target": "T1"}

    first = await client.post(action_url, headers=headers, json=tap(old))
    assert first.status_code == 200, first.text
    current = first.json()["observation"]
    assert first.json()["changes"]["text_sources"] == ["screen"]
    assert first.json()["changes"]["added_text"] == ["Caption 2"]
    assert current["screen_texts"][0]["target"] == current["targets"][0]["id"] == "T1"
    assert (await client.post(action_url, headers=headers, json=tap(old))).status_code == 409
    assert (await client.post(action_url, headers=headers, json=tap(current))).status_code == 200
    assert [(msg["x"], msg["y"]) for msg in agent.mutations()] == [tuple(old["targets"][0]["point"]),
                                                                  tuple(current["targets"][0]["point"])]


async def test_concurrent_target_actions_dispatch_only_once_per_frame(client, agent):
    lease, headers = await claim(client, "usb-one")
    frame = await observe(client, lease, headers)
    body = {"action": "tap", "target": "A1", "frame_id": frame["frame_id"]}
    entered, proceed = asyncio.Event(), asyncio.Event()
    send = agent.send_json

    async def delayed(message):
        if message.get("action") == "tap":
            entered.set()
            await proceed.wait()
        return await send(message)

    agent.send_json = delayed
    first = asyncio.create_task(client.post(lease["api"]["act"], headers=headers, json=body))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(client.post(lease["api"]["act"], headers=headers, json=body))
    proceed.set()
    a, b = await asyncio.gather(first, second)
    assert a.status_code == 200 and b.status_code == 409 and len(agent.mutations()) == 1


async def test_public_changes_use_private_unmarked_samples_and_do_not_return_them(client, agent):
    from ai_phone.shared.android_frame_regions import sample_original

    lease, headers = await claim(client, "usb-one")
    original = agent.observation

    def with_sample(serial):
        result = original(serial)
        revision = agent.revisions[serial]
        raw = Image.new("RGB", (32, 48), "white")
        if revision >= 2:
            ImageDraw.Draw(raw).rectangle((4, 16, 23, 31), fill="navy")
        marked = raw.copy()
        ImageDraw.Draw(marked).text((1, 1), str(revision), fill="red")
        out = io.BytesIO()
        marked.save(out, format="PNG")
        return {**result, "png": base64.b64encode(out.getvalue()).decode("ascii"),
                "marked_targets": True, "_raw_grid_v1": sample_original(raw)}

    agent.observation = with_sample
    first = await observe(client, lease, headers)
    assert "_raw_grid_v1" not in first

    async def advance(current):
        response = await client.post(lease["api"]["act"], headers=headers,
                                     json={"action": "back", "frame_id": current["frame_id"]})
        assert response.status_code == 200 and "_raw_grid_v1" not in response.text
        return response.json()

    actual = await advance(first)
    assert len(actual["changes"]["changed_regions"]) == 1
    assert not (await advance(actual["observation"]))["changes"]["changed_regions"]
    agent.observation = original
    legacy = await observe(client, lease, headers)
    assert "changed_regions" not in (await advance(legacy))["changes"]
