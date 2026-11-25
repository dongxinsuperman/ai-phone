"""Exercise the production simulator adapter at the WDA transport boundary, without a device."""

import base64
import io
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from ai_phone.agent import ios_sim_observation as ios
from ai_phone.agent.ios_sim_snapshot import from_xctest, native_keyboard
from ai_phone.agent.mobile_sim_errors import MobileSimError
from ai_phone.shared import mobile_frame
from ai_phone.shared.mobile_frame import build_frame


SERIAL = "505B8B63-809B-466F-8744-04541B861AE4"
XML = """<AppiumAUT><XCUIElementTypeApplication bundleId="org.example.demo"
 x="0" y="0" width="120" height="200">
 <XCUIElementTypeButton name="button.stable.key" label="Read entry" enabled="true"
   x="10" y="40" width="60" height="20"/>
 <XCUIElementTypeStaticText label="Archive" x="2" y="120" width="44" height="8"/>
 </XCUIElementTypeApplication></AppiumAUT>"""


def png(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (240, 400), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def simulator(monkeypatch):
    """Dispatch production HTTP invocations into an in-process, recorded WDA server."""
    state = SimpleNamespace(
        boot="sim-boot-1", sid="wda-1", package="org.example.demo", xml=XML, png=png(),
        calls=[], element_type="XCUIElementTypeTextField", tap_error=None,
        window={"x":0, "y":0, "width":120, "height":200},
        screen={"screenSize": {"width":120, "height":200}, "scale":2}, has_window_rect=True,
        app_setting="auto", app_windows={}, front_apps=[],
        endpoint=SimpleNamespace(wda_port=49152, duo=None),
    )
    real_client = httpx.Client

    def dispatch(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        state.calls.append((request.method, path, body))
        if path == "/status":
            return httpx.Response(200, json={"sessionId": state.sid,
                                            "value": {"ios": {"simulatorVersion": "26.0"}}})
        if path == "/session":
            assert state.sid is None
            state.sid = "created-once"
            return httpx.Response(200, json={"value": {"sessionId": state.sid}})
        if path == "/wda/homescreen":
            return httpx.Response(200, json={"value": None})
        prefix = f"/session/{state.sid}"
        assert path.startswith(prefix)
        route = path[len(prefix):]
        if route == "/wda/tap" and state.tap_error:
            if isinstance(state.tap_error, Exception):
                raise state.tap_error
            status, error = state.tap_error
            return httpx.Response(status, json={"value": {"error": error}})
        if route == "/window/rect" and not state.has_window_rect:
            return httpx.Response(404, json={"value": {"error": "unknown command"}})
        if route == "/appium/settings" and request.method == "POST":
            state.app_setting = body["settings"]["defaultActiveApplication"]
        effective_app = state.package if state.app_setting == "auto" else state.app_setting
        window = state.app_windows.get(effective_app, state.window)
        fixtures = {
            "/window/size": {key: window[key] for key in ("width", "height")},
            "/window/rect": window, "/wda/screen": state.screen,
            "/rotation": {"z": 0},
            "/wda/activeAppInfo": {"bundleId": effective_app}, "/source": state.xml,
            "/appium/settings": {"defaultActiveApplication":state.app_setting},
            "/wda/apps/list": [{"bundleId":bundle} for bundle in state.front_apps],
            "/screenshot": base64.b64encode(state.png).decode("ascii"),
            "/element/active": {ios._ELEMENT_KEY: "text-control"},
            "/element/text-control/attribute/type": state.element_type,
            "/element/text-control/clear": None, "/element/text-control/value": None,
            "/wda/tap": None, "/wda/tap/0": None, "/actions": None, "/wda/apps/launch": None,
        }
        return httpx.Response(200, json={"value": fixtures[route]})

    monkeypatch.setattr(ios, "get_sim_endpoint", lambda serial: state.endpoint if serial == SERIAL else None)
    monkeypatch.setattr(ios, "boot_identity", lambda *args: state.boot)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(dispatch), **kw))
    return state


def call(state, action, **fields):
    return ios.execute({"serial": SERIAL, "binding": state.boot,
                        "deadline": time.time() + 20, "action": action, **fields})


def test_observe_geometry_numbered_image_and_independent_screenshot(simulator, monkeypatch):
    def unexpected_recognition(*_args):
        pytest.fail("iOS observe uses system text and does not recognize text from the screenshot")

    monkeypatch.setattr(mobile_frame, "_screen_text_evidence", unexpected_recognition)
    monkeypatch.setattr(mobile_frame, "add_printed_card_titles", unexpected_recognition)
    bind = ios.bind(SERIAL, time.time() + 20)
    assert bind["binding"] == simulator.boot
    assert {"screenshot", "observe", "tap", "scroll", "input", "open_app"} <= set(bind["action_types"])
    observed = call(simulator, "observe", include_screenshot=False)
    assert "png" not in observed and observed["structure"] and observed["tree_available"]
    control = next(t for t in observed["targets"] if t["name"] == "Read entry")
    assert control["bounds"] == [20, 80, 140, 120]
    assert control["name_source"] == "system" and control["actions"] == ["tap"]
    assert control["resource_id"] == "button.stable.key"
    assert any(r["text"] == "Archive" and r["source"] == "system" for r in observed["system_texts"])
    screenshot = call(simulator, "screenshot")
    assert base64.b64decode(screenshot["png"]) == simulator.png
    first_call = len(simulator.calls)
    marked = call(simulator, "observe")
    paths = [path for _, path, _ in simulator.calls[first_call:]]
    assert [path.split("/")[-1] for path in paths if path.endswith(("/source", "/screenshot"))] == [
        "source", "screenshot",
    ]
    assert marked["marked_targets"] and base64.b64decode(marked["png"]) != simulator.png
    assert marked["structure_status"] == "available"
    assert not marked["screen_texts"] and not marked["screen_texts_available"]
    point = control["point"]
    call(simulator, "tap", x=point[0], y=point[1], width=240, height=400, display=observed["display"])
    post = next(t for t in simulator.calls if t[1].endswith("/wda/tap"))
    assert post[2] == {"x": point[0] / 2, "y": point[1] / 2}
    assert not any(path == "/session" or method == "DELETE" for method, path, _ in simulator.calls)


def test_no_active_wda_session_creates_once_and_stale_controls_never_dispatch(simulator):
    simulator.sid = None
    lease = ios.bind(SERIAL, time.time() + 20)
    frame = call(simulator, "screenshot")
    assert sum(path == "/session" for _, path, _ in simulator.calls) == 1
    simulator.sid = "other-session-same-boot"
    with pytest.raises(MobileSimError) as stale:
        call(simulator, "tap", x=10, y=10, width=240, height=400, display=frame["display"])
    assert stale.value.status == 409
    assert all(not path.endswith("/wda/tap") for _, path, _ in simulator.calls)
    assert ios.bind(SERIAL, time.time() + 20)["binding"] == lease["binding"]
    simulator.boot = "new-boot"
    count = len(simulator.calls)
    with pytest.raises(MobileSimError) as reboot:
        ios.execute({"serial": SERIAL, "deadline": time.time()+10, "binding": lease["binding"], "action": "home"})
    assert reboot.value.status == 409
    assert len(simulator.calls) == count


def test_source_background_exclusion_keyboard_and_password_value():
    source = """<XCUIElementTypeApplication x="0" y="0" width="120" height="200">
    <XCUIElementTypeButton label="Background" x="0" y="0" width="50" height="25"/>
    <XCUIElementTypeSheet x="3" y="30" width="110" height="60" visible="true">
      <XCUIElementTypeSecureTextField label="Passcode" name="pin_field" value="PRIVATE_VALUE"
          x="8" y="40" width="80" height="14"/>
      <XCUIElementTypeButton name="ui.private.action" label="ui.private.action"
          enabled="true" value="1" x="10" y="60" width="12" height="13"/>
      <XCUIElementTypeButton label="Disabled" enabled="false" x="40" y="61" width="14" height="12"/>
      <XCUIElementTypeProgressIndicator x="60" y="61" width="14" height="12"/>
    </XCUIElementTypeSheet>
    <XCUIElementTypeKeyboard x="0" y="140" width="120" height="60">
      <XCUIElementTypeKey label="Space" x="18" y="160" width="38" height="16"/>
    </XCUIElementTypeKeyboard></XCUIElementTypeApplication>"""
    nodes, bundle = from_xctest(source, (120, 200), (240, 400))
    result = build_frame(nodes, platform="ios", width=240, height=400, include_image=False,
                         keyboard=native_keyboard(nodes), package=bundle)
    targets = result["targets"]
    assert result["keyboard"]["bounds"] == [0,280,240,400] and result["dialogs"][0]["source"] == "ios"
    assert {t["name"] for t in targets} == {"Passcode", None, "Space"}
    assert "PRIVATE_VALUE" not in json.dumps(result)
    anonymous = next(t for t in targets if t["name"] is None)
    assert anonymous["resource_id"] == "ui.private.action" and "selected" not in anonymous["state"]
    assert "Disabled" in json.dumps(result["structure"])


def test_webkit_reports_one_heading_not_its_rank_or_an_icon_font_as_visible_text():
    source = """<XCUIElementTypeApplication x="0" y="0" width="120" height="200">
    <XCUIElementTypeWebView x="0" y="0" width="120" height="200">
      <XCUIElementTypeOther label="A new section" traits="Header" value="3" x="30" y="25" width="60" height="12">
        <XCUIElementTypeStaticText label="A new section" traits="Header" x="30" y="25" width="59" height="12"/>
      </XCUIElementTypeOther>
      <XCUIElementTypeStaticText label="&#xf2aa;" x="20" y="15" width="5" height="6"/>
      <XCUIElementTypeOther label="Scroll position" value="0%" x="1" y="25" width="5" height="12"/>
    </XCUIElementTypeWebView></XCUIElementTypeApplication>"""
    nodes, _ = from_xctest(source, (120, 200), (240, 400))
    heading, icon, bar = nodes[0]["children"][0]["children"]
    assert heading["role"] == "heading" and heading["level"] == 3 and not heading["children"]
    assert icon["role"] == "image" and not icon["name"] and "text" not in icon
    assert "value" not in bar
    result = build_frame(nodes, platform="ios", width=240, height=400, include_image=False)
    assert result["texts"] == ["A new section"]


def test_an_explicit_input_placeholder_names_the_field_without_claiming_it_was_entered():
    source = """<XCUIElementTypeApplication x="0" y="0" width="120" height="200">
    <XCUIElementTypeTextField placeholderValue="Street" value="Street" x="3" y="5" width="55" height="12"/>
    <XCUIElementTypeSearchField placeholderValue="City" value="Paris" x="3" y="25" width="55" height="12"/>
    </XCUIElementTypeApplication>"""
    nodes, _ = from_xctest(source, (120, 200), (240, 400))
    empty, filled = nodes[0]["children"]
    assert empty["text"] == "Street" and empty["name"] == "Street" and "value" not in empty
    assert filled["name"] == "City" and filled["value"] == "Paris" and "text" not in filled


def test_an_image_and_image_button_preserve_the_explicit_english_accessibility_label():
    source = """<XCUIElementTypeApplication x="0" y="0" width="120" height="200">
    <XCUIElementTypeImage name="Help" label="Help" x="3" y="5" width="55" height="12"/>
    <XCUIElementTypeImage name="checkout_cta" label="Buy now" traits="Button"
                          x="3" y="25" width="55" height="12"/>
    </XCUIElementTypeApplication>"""
    nodes, _ = from_xctest(source, (120, 200), (240, 400))
    icon, button = nodes[0]["children"]
    assert (icon["name"], icon["role"], icon["system_name_source"]) == ("Help", "image", "accessibility_label")
    assert (button["name"], button["role"], button["system_name_source"]) == ("Buy now", "button", "accessibility_label")
    assert button["resource_id"] == "checkout_cta"


def test_an_identifier_without_accessibility_label_never_becomes_an_accessible_name():
    source = """<XCUIElementTypeApplication x="0" y="0" width="120" height="200">
    <XCUIElementTypeImage name="Help" x="3" y="5" width="55" height="12"/>
    <XCUIElementTypeButton name="checkout_cta" label="" x="3" y="25" width="55" height="12"/>
    </XCUIElementTypeApplication>"""
    nodes, _ = from_xctest(source, (120, 200), (240, 400))
    icon, button = nodes[0]["children"]
    assert [n["resource_id"] for n in (icon, button)] == ["Help", "checkout_cta"]
    assert all(n["name"] is None and "system_name_source" not in n for n in (icon, button))


def test_landscape_app_envelope_uses_full_visible_window_without_reorienting_controls():
    source = """<XCUIElementTypeApplication x="0" y="0" width="100" height="150">
    <XCUIElementTypeWindow x="0" y="0" width="150" height="100">
    <XCUIElementTypeButton label="Continue" x="125" y="10" width="15" height="20"/>
    </XCUIElementTypeWindow></XCUIElementTypeApplication>"""
    nodes, _ = from_xctest(source, (150,100), (450,300))
    assert nodes[0]["bounds"] == [0,0,450,300]
    assert nodes[0]["children"][0]["children"][0]["bounds"] == [375,30,420,90]


def test_keyboard_occlusion_uses_its_native_host_and_preserves_sibling_accessory():
    source = """<XCUIElementTypeApplication x="0" y="0" width="120" height="200">
    <XCUIElementTypeWebView x="0" y="0" width="120" height="200">
      <XCUIElementTypeToolbar x="0" y="96" width="120" height="104"/>
    </XCUIElementTypeWebView>
    <XCUIElementTypeOther x="0" y="96" width="120" height="104">
      <XCUIElementTypeToolbar x="0" y="100" width="120" height="18"/>
    </XCUIElementTypeOther>
    <XCUIElementTypeOther x="0" y="96" width="120" height="104">
      <XCUIElementTypeOther name="SystemInputAssistantView" x="15" y="118" width="90" height="18"/>
      <XCUIElementTypeKeyboard x="15" y="136" width="90" height="64"/>
    </XCUIElementTypeOther></XCUIElementTypeApplication>"""
    nodes, _ = from_xctest(source, (120,200), (240,400))
    web, toolbar_host, keyboard_host = nodes[0]["children"]
    assert native_keyboard(nodes)["occlusion_bounds"] == [0,192,240,400]
    assert native_keyboard(nodes)["bounds"] == [30,272,210,400]
    assert toolbar_host["input_overlay"] and keyboard_host["input_overlay"]
    assert "input_overlay" not in web and "input_overlay" not in web["children"][0]
    no_proof = source.replace("SystemInputAssistantView", "A plain application label")
    nodes, _ = from_xctest(no_proof, (120,200), (240,400))
    assert "occlusion_bounds" not in native_keyboard(nodes)


def test_input_only_replaces_focused_editable_and_home_uses_device_route(simulator):
    call(simulator, "input", text="new value")
    writes = [(path, body) for method, path, body in simulator.calls if method == "POST"]
    assert writes == [("/session/wda-1/element/text-control/clear", {}),
                      ("/session/wda-1/element/text-control/value", {"text": "new value"})]
    simulator.calls.clear()
    call(simulator, "input", text="")
    assert sum(path.endswith("/clear") for _, path, _ in simulator.calls) == 1
    simulator.calls.clear()
    simulator.element_type = "XCUIElementTypeDatePicker"
    with pytest.raises(MobileSimError) as error:
        call(simulator, "input", text="bad target")
    assert error.value.status == 409
    assert not any(method == "POST" for method, _, _ in simulator.calls)
    call(simulator, "home")
    assert simulator.calls[-1][1] == "/wda/homescreen"


def test_scroll_bounded_and_failed_taps_are_never_replayed(simulator):
    screen = call(simulator, "screenshot")
    kwargs = {"width": 240, "height": 400, "display": screen["display"]}
    call(simulator, "scroll", bounds=[10, 40, 210, 340], direction="down", **kwargs)
    actions = simulator.calls[-1][2]["actions"][0]["actions"]
    assert actions[0]["y"] > actions[2]["y"] and actions[0]["x"] == actions[2]["x"]
    with pytest.raises(MobileSimError):
        call(simulator, "tap", x=240, y=0, **kwargs)
    simulator.tap_error = httpx.ReadTimeout("no reply")
    with pytest.raises(MobileSimError) as timeout:
        call(simulator, "tap", x=60, y=80, **kwargs)
    assert timeout.value.status == 504 and not any(p.endswith("/wda/tap/0") for _, p, _ in simulator.calls)
    simulator.tap_error = (404, "unknown command")
    call(simulator, "tap", x=60, y=80, **kwargs)
    assert simulator.calls[-1][1].endswith("/wda/tap/0")


def test_duo_rejects_change_of_active_panel_and_routes_pixels_to_selected_panel(simulator):
    touched = []
    budgets = []
    panel = SimpleNamespace(id=3, uuid="panel3", rotation="rot0", pixel_size=(240,400),
                            native_point=lambda x,y: (x/240,y/400))
    def get_panel(deadline):
        budgets.append(deadline)
        return panel
    simulator.endpoint.duo = SimpleNamespace(capture_id=7, display=get_panel, _send=lambda *a: touched.append(a))
    with pytest.raises(MobileSimError) as other:
        call(simulator, "observe")
    assert other.value.status == 409
    simulator.endpoint.duo.capture_id = 3
    frame = call(simulator, "screenshot")
    call(simulator, "tap", x=39, y=83, width=240, height=400, display=frame["display"])
    assert touched == [(budgets[-1], "touch", "panel3", str(39/240), str(83/400),
                        str(39/240), str(83/400), "80", "1", "100")]
    panel.uuid = "replacement-panel"
    with pytest.raises(MobileSimError):
        call(simulator, "tap", x=39, y=83, width=240, height=400, display=frame["display"])
    assert len(touched) == 1


def test_ipad_windowed_app_uses_global_pixels_and_wda_app_relative_gestures(simulator):
    # Exactly the screen aspect ratio; an aspect-only check cannot detect this floating panel.
    simulator.window = {"x":15, "y":40, "width":60, "height":100}
    frame = call(simulator,"screenshot")
    assert frame["width"] == 240 and frame["display"]["width"] == 120
    call(simulator,"tap",x=90,y=140,width=240,height=400,display=frame["display"])
    assert simulator.calls[-1][2] == {"x":30, "y":30}
    call(simulator,"back")
    swipe = simulator.calls[-1][2]["actions"][0]["actions"]
    assert (swipe[0]["x"],swipe[0]["y"]) == (2,50)
    simulator.window = {**simulator.window, "x": 20}
    with pytest.raises(MobileSimError) as moved:
        call(simulator, "tap", x=90,y=140,width=240,height=400,display=frame["display"])
    assert moved.value.status == 409


def test_duo_avoids_start_of_hid_touch_after_panel_check_uses_up_request_budget(monkeypatch):
    touched = []
    monotonic = iter([10.0,10.05])
    monkeypatch.setattr(ios.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(ios, "_remaining", lambda _: .1)
    panel = SimpleNamespace(id=2,uuid="p",rotation="rot0")
    wda = SimpleNamespace(deadline=100, endpoint=SimpleNamespace(duo=SimpleNamespace(
        display=lambda deadline: panel, capture_id=2, _send=lambda *args: touched.append(args))))
    with pytest.raises(MobileSimError) as consumed:
        ios._gesture(wda, {"panel_id":2,"panel_uuid":"p","panel_rotation":"rot0"},
                     (240,400), (10,20))
    assert consumed.value.status == 504 and not touched


def test_older_wda_scale_only_uses_full_image_and_keeps_window_offset(simulator):
    simulator.screen = {"statusBarSize": {"width": 120, "height": 10}, "scale": 2}
    simulator.window = {"x":15, "y":40, "width":60, "height":100}
    frame = call(simulator, "observe", include_screenshot=False)
    button = next(t for t in frame["targets"] if t["name"] == "Read entry")
    assert button["bounds"] == [20, 80, 140, 120]
    call(simulator,"tap",x=90,y=140,width=240,height=400,display=frame["display"])
    assert simulator.calls[-1][2] == {"x":30, "y":30}
    call(simulator, "back")
    swipe = simulator.calls[-1][2]["actions"][0]["actions"]
    assert (swipe[0]["x"],swipe[0]["y"]) == (2,50)


def test_duo_associates_only_visible_apps_matching_panel_and_restores_settings(simulator):
    panel = SimpleNamespace(id=3,uuid="inner",rotation="rot90",pixel_size=(241,401))
    simulator.endpoint.duo = SimpleNamespace(capture_id=3,display=lambda deadline: panel)
    simulator.package = "com.apple.springboard"
    simulator.front_apps = [simulator.package, "org.example.demo"]
    simulator.app_windows = {simulator.package: {"x":0,"y":0,"width":60,"height":100}}
    frame = call(simulator,"observe",include_screenshot=False)
    assert frame["tree_available"] and frame["package"] == "org.example.demo"
    assert simulator.app_setting == "auto"
    assert [b["settings"]["defaultActiveApplication"] for m,p,b in simulator.calls
            if m=="POST" and p.endswith("/appium/settings")] == ["org.example.demo","auto"]
    simulator.front_apps.append("org.example.other")
    simulator.calls.clear()
    ambiguous = call(simulator,"observe",include_screenshot=False)
    assert not ambiguous["tree_available"] and not ambiguous.get("package")
    assert "ios_active_display_tree_unavailable" in ambiguous["warnings"]
    assert not any(p.endswith("/source") or m=="POST" for m,p,_ in simulator.calls)
    with pytest.raises(MobileSimError) as unavailable:
        call(simulator,"input",text="refuse wrong panel")
    assert unavailable.value.status == 409


def test_legacy_app_window_without_origin_must_fill_physical_display(simulator, monkeypatch):
    from ai_phone.agent.drivers import simctl

    simulator.screen = {}
    simulator.has_window_rect = False
    monkeypatch.setattr(simctl, "simulator_screen", lambda _: SimpleNamespace(scale=2))
    frame = call(simulator, "screenshot")
    assert frame["width"] == 240 and frame["display"]["unknown_window_origin"]
    simulator.window = {"x":0, "y":0, "width":60, "height":100}
    with pytest.raises(MobileSimError) as unknown_origin:
        call(simulator, "screenshot")
    assert unknown_origin.value.status == 409


def test_listener_uses_guest_boot_parent_not_host_pid_or_idfv(monkeypatch):
    guest = SimpleNamespace(pid=512, name=lambda: "launchd_sim", create_time=lambda: 10.125,
                            cmdline=lambda: [f"/host/Library/Developer/CoreSimulator/Devices/{SERIAL}/data/var/run/launchd_bootstrap.plist"])
    target = SimpleNamespace(parents=lambda: [guest])
    monkeypatch.setattr(ios.psutil, "Process", lambda p: target)
    monkeypatch.setattr(ios.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="p1234\n"))
    identity = ios.boot_identity(SERIAL, 49999, time.time()+20)
    assert len(identity) == 64 and identity == ios.boot_identity(SERIAL, 49999, time.time()+20)
    with pytest.raises(MobileSimError) as wrong:
        ios.boot_identity("a-different-udid", 49999, time.time()+20)
    assert wrong.value.status == 409
    guest.create_time = lambda: 12.5
    assert ios.boot_identity(SERIAL, 49999, time.time()+20) != identity


def test_failed_tree_degrades_to_only_new_pixels(simulator):
    simulator.xml = "<malformed>"
    result = call(simulator, "observe", include_screenshot=False)
    assert not result["tree_available"] and "ios_tree_unavailable" in result["warnings"]
    assert result["width"] == 240 and "png" not in result


def test_app_switch_during_observe_does_not_return_the_old_tree(simulator, monkeypatch):
    identities = iter([(0, simulator.package), (0, "org.example.other")])
    monkeypatch.setattr(ios, "_foreground_identity", lambda _wda: next(identities))
    with pytest.raises(MobileSimError) as moved:
        call(simulator, "observe")
    assert moved.value.status == 409
    assert sum(path.endswith("/screenshot") for _, path, _ in simulator.calls) == 1
