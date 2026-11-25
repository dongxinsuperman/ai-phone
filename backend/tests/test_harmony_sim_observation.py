import base64
import hashlib
import io
import json
import subprocess
import time

from PIL import Image, ImageDraw
import pytest

from ai_phone.agent import harmony_sim_observation as hm
from ai_phone.agent.harmony_sim_tree import native_page
from ai_phone.agent.harmony_vm.registry import (
    register_managed_vm,
    unregister_managed_vm,
)
from ai_phone.agent.mobile_sim_errors import MobileSimError


BOOT = "01234567-89ab-cdef-0123-456789abcdef"
VM = "hm-unified-adapter-test"
SERIAL = f"harmony-vm:{VM}"
ADDRESS = "127.0.0.1:63919"


def node(kind, bounds, children=(), **attrs):
    return {
        "attributes": {"type": kind, "bounds": bounds, **attrs},
        "children": list(children),
    }


def scene():
    return node(
        "",
        "[0,0][160,240]",
        [
            node(
                "root",
                "[0,8][160,240]",
                [
                    node("Button", "[0,8][55,30]", text="Behind", clickable="true"),
                    node(
                        "Dialog",
                        "[0,8][160,240]",
                        [
                            node(
                                "Column",
                                "[20,48][140,207]",
                                [
                                    node("Text", "[32,58][90,76]", text="Notice"),
                                    node(
                                        "Text",
                                        "[32,85][129,125]",
                                        text="A visible message.",
                                    ),
                                    node(
                                        "Button",
                                        "[40,151][114,178]",
                                        [
                                            node(
                                                "Text",
                                                "[55,157][91,174]",
                                                text="Continue",
                                            )
                                        ],
                                        clickable="true",
                                    ),
                                    node(
                                        "Image",
                                        "[111,55][134,76]",
                                        clickable="true",
                                        id="dismiss_resource",
                                    ),
                                ],
                            )
                        ],
                        clickable="true",
                    ),
                ],
                bundleName="org.sample.notes",
                abilityName="Entry",
                visible="true",
            ),
            node("WindowScene", "[0,0][160,8]", bundleName="os.shell"),
        ],
    )


def frame_png(size=(160, 240), color="white"):
    out = io.BytesIO()
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).rectangle((40, 151, 113, 177), outline="gray")
    image.save(out, format="PNG")
    return out.getvalue(), size


@pytest.fixture
def guest(monkeypatch):
    register_managed_vm(VM, ADDRESS, "original-run")
    monkeypatch.setattr(hm, "hdc_shell", lambda *_a, **_k: BOOT)
    try:
        yield hm._guest(SERIAL, time.time() + 6)
    finally:
        unregister_managed_vm(VM, "original-run")
        unregister_managed_vm(VM, "replacement-run")


def all_nodes(nodes):
    for item in nodes:
        yield item
        yield from all_nodes(item["children"])


def test_native_modal_owns_title_body_and_nested_button_while_background_is_absent():
    assert not native_page(node("", "[0,0][160,240]"), 160, 240)[3]
    roots, bundle, ability, valid = native_page(scene(), 160, 240)
    assert (bundle, ability, valid) == ("org.sample.notes", "Entry", True)
    assert len(roots) == 1
    assert roots[0]["modal"] and roots[0]["bounds"] == [20, 48, 140, 207]
    nodes = list(all_nodes(roots))
    assert [n["text"] for n in nodes if n["text"]] == [
        "Notice",
        "A visible message.",
        "Continue",
    ]
    assert [(n["type"], n.get("name")) for n in nodes if n["actions"]] == [
        ("Button", None),
        ("Image", None),
    ]


def test_hidden_clipped_disabled_sensitive_nodes_keep_only_safe_evidence():
    clear = node(
        "Column",
        "[1,1][140,181]",
        [
            node("Text", "[2,2][90,20]", text="hidden", visible=False),
            node("Text", "[2,20][90,40]", text="transparent", opacity=0),
            node(
                "TextInput",
                "[2,50][100,75]",
                [node("Text", "[3,52][99,72]", text="protected-value")],
                text="protected-value",
                description="protected-value",
                inputType="NewPassword",
                hint="Secret",
            ),
            node(
                "Button",
                "[138,90][180,126]",
                text="Dim",
                enabled="false",
                clickable="true",
            ),
            node("Scroll", "[2,130][138,175]", scrollable=True),
            node(
                "Toggle",
                "[300,240][360,280]",
                text="offscreen",
                checkable=True,
                checked=True,
            ),
        ],
        clip="true",
    )
    roots, *_ = native_page([clear], 160, 240)
    nodes = list(all_nodes(roots))
    serialized = json.dumps(nodes)
    assert all(t not in serialized for t in ("hidden", "transparent", "protected-value", "offscreen"))
    edit = next(n for n in nodes if n["role"] == "input")
    assert edit["protected"] and edit["safe_label"] == "Secret"
    dim = next(n for n in nodes if n.get("text") == "Dim")
    assert dim["bounds"] == [138, 90, 140, 126] and not dim["actions"]
    assert next(n for n in nodes if n["role"] == "scroll_area")["actions"] == ["scroll"]


def test_arkweb_virtual_nodes_use_visibility_and_clip_to_the_visible_native_host():
    def virtual(kind, bounds, **attrs):
        return node(kind, bounds, **{"visible": True, "opacity": "0.000000", **attrs})

    controls = [
        virtual("button", "[2,20][140,60]", text="Read details", clickable=True),
        virtual("staticText", "[4,80][140,96]", text="Visible sentence"),
        virtual(
            "button",
            "[5,100][120,124]",
            text="Web hidden",
            visible=False,
            clickable=True,
        ),
        virtual(
            "button",
            "[5,125][120,150]",
            text="AX hidden",
            ariaHidden=True,
            clickable=True,
        ),
        virtual("button", "[0,190][160,210]", text="Outside web", clickable=True),
    ]
    document = virtual("rootWebArea", "[0,0][160,240]", children=controls, text="internal title")
    host = node(
        "Web",
        "[15,10][150,180]",
        [document],
        visible=True,
        opacity="1.000000",
        text="https://invalid.test/?token=private",
    )
    tree = node(
        "Column",
        "[0,0][160,240]",
        [
            host,
            node(
                "Button",
                "[15,190][135,238]",
                text="Native hidden",
                clickable=True,
                opacity=0,
            ),
        ],
    )
    parts, *_ = native_page(tree, 160, 240)
    visible = list(all_nodes(parts))
    assert [(i["text"], i["bounds"]) for i in visible if i["actions"]] == [
        ("Read details", [15, 20, 140, 60])
    ]
    assert {i["text"] for i in visible if i["text"]} == {
        "Read details",
        "Visible sentence",
    }
    assert "private" not in json.dumps(visible) and "internal title" not in json.dumps(visible)
    host["attributes"]["opacity"] = "0"
    assert not any(i["actions"] for i in all_nodes(native_page(tree, 160, 240)[0]))


def test_arkweb_uses_real_opacity_on_versions_with_mixed_compositor_values():
    controls = [
        node(
            "button",
            "[3,20][125,44]",
            text="Present",
            clickable=True,
            visible=True,
            opacity=1,
        ),
        node(
            "button",
            "[3,54][125,84]",
            text="Transparent",
            clickable=True,
            visible=True,
            opacity=0,
        ),
    ]
    doc = node("rootWebArea", "[0,0][160,240]", controls, visible=True, opacity=0)
    host = node("Web", "[0,0][160,240]", [doc], visible=True, opacity=1)
    parts = native_page(host, 160, 240)[0]
    assert [i["text"] for i in all_nodes(parts) if i["actions"]] == ["Present"]


def test_binding_is_guest_boot_and_active_vm_launch_specific(guest, monkeypatch):
    assert hm.bind(SERIAL, time.time() + 3)["binding"] == guest.binding
    assert hm.bind(SERIAL, time.time() + 3)["action_types"] == list(hm.ACTION_TYPES)
    with pytest.raises(MobileSimError) as different_kind:
        hm.bind(ADDRESS, time.time() + 3)
    assert different_kind.value.status == 409
    register_managed_vm(VM, ADDRESS, "replacement-run")
    with pytest.raises(MobileSimError) as expired:
        guest.check()
    assert expired.value.status == 409
    assert hm._guest(SERIAL, time.time() + 3).binding != guest.binding
    monkeypatch.setattr(hm, "hdc_shell", lambda *_a, **_k: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    with pytest.raises(MobileSimError) as changed:
        hm.execute(
            {
                "serial": SERIAL,
                "binding": guest.binding,
                "action": "home",
                "deadline": time.time() + 3,
            }
        )
    assert changed.value.status == 409


def test_guest_command_requires_completion_and_guards_mutation_on_targeted_boot(guest, monkeypatch):
    recorded = []

    def shell(addr, cmd, **kwargs):
        assert addr == ADDRESS
        recorded.append(cmd)
        return "__AI_PHONE_BOOT_CHANGED"

    monkeypatch.setattr(hm, "hdc_shell", shell)
    with pytest.raises(MobileSimError) as exc:
        guest.shell("uitest uiInput keyEvent Back", time.time() + 5)
    assert exc.value.status == 409
    assert BOOT in recorded[0] and recorded[0].index(BOOT) < recorded[0].index("uitest uiInput")
    monkeypatch.setattr(hm, "hdc_shell", lambda *_a, **_k: "[Fail]Device not found")
    with pytest.raises(MobileSimError) as failed:
        guest.shell("uitest uiInput keyEvent Back", time.time() + 3)
    assert failed.value.status == 502


def test_restricted_kernel_uuid_binds_to_the_same_guest_boot_events(guest, monkeypatch):
    marker = "events:8593133:11439201:36313955"
    calls = []

    def read(_addr, command, **_kwargs):
        calls.append(command)
        if command == hm._READ_BOOT_EVENTS:
            return marker
        if "__AI_PHONE_STATUS" in command:
            return "__AI_PHONE_BOOT_CHANGED"
        return "Permission denied"

    monkeypatch.setattr(hm, "hdc_shell", read)
    events_guest = hm._guest(SERIAL, time.time() + 3)
    assert events_guest.boot == marker and events_guest.binding != guest.binding
    with pytest.raises(MobileSimError) as reboot:
        events_guest.shell("uitest uiInput keyEvent Back", time.time() + 3)
    assert reboot.value.status == 409
    assert all(s in calls[-1] for s in ("ohos.boot.time.init", "boot.completed", marker))
    marker = "events:0:unknown:0"
    with pytest.raises(MobileSimError) as unreadable:
        hm._guest(SERIAL, time.time() + 3)
    assert unreadable.value.status == 503


def test_guest_shell_normalizes_each_boot_event_and_rejects_ambiguous_output():
    def output(kernel):
        prefix = (
            'param() { case "$2" in *.kernel) printf "%s" "$KERNEL";; '
            '*.init) printf "11439201 ";; *.completed) printf "36313955";; esac; }; '
        )
        return subprocess.check_output(
            ["sh", "-c", prefix + hm._READ_BOOT_EVENTS],
            env={"KERNEL": kernel},
            text=True,
        )

    assert output("8593133 ") == "events:8593133:11439201:36313955"
    for invalid in ("", "denied 8593133", "8593 133", "8593133?", "0"):
        assert not hm._BOOT_EVENTS.fullmatch(output(invalid))


def test_structure_only_uses_native_bounds_when_screenshots_are_unavailable(guest, monkeypatch):
    monkeypatch.setattr(hm, "_capture", lambda *a, **k: (None, scene(), None))
    result = hm._observe(guest, {"deadline": time.time() + 8, "include_screenshot": False})
    assert "png" not in result and "structure" in result
    assert result["width"] == 160 and result["height"] == 240
    assert result["structure"] and result["tree_available"]
    assert any(t["name"] == "Continue" and "tap" in t["actions"] for t in result["targets"])
    assert "Behind" not in json.dumps(result)
    with pytest.raises(MobileSimError):
        hm._observe(guest, {"deadline": time.time() + 8, "include_screenshot": True})


def test_observe_retries_rotated_snapshot_and_uses_only_the_stable_current_tree(guest, monkeypatch):
    portrait = frame_png()
    landscape = frame_png(size=(240, 160))
    captures = iter([(portrait, scene(), landscape), (portrait, scene(), portrait)])
    monkeypatch.setattr(hm, "_capture", lambda *a, **k: next(captures))
    result = hm._observe(guest, {"deadline": time.time() + 10, "include_screenshot": False})
    assert result["tree_available"] and result["display"] == {
        "width": 160,
        "height": 240,
    }
    assert any(t["name"] == "Continue" for t in result["targets"])
    monkeypatch.setattr(hm, "_capture", lambda *a, **k: (portrait, scene(), landscape))
    result = hm._observe(guest, {"deadline": time.time() + 10, "include_screenshot": False})
    assert not result["tree_available"] and result["width"] == 240 and result["warnings"]
    assert all(t.get("name") != "Continue" for t in result["targets"])


def test_screenshot_is_raw_and_coordinate_action_verifies_the_current_dimensions(guest, monkeypatch):
    original = frame_png()
    monkeypatch.setattr(hm, "_capture", lambda *a, **k: (None, None, original))
    raw = hm._screenshot(guest, time.time() + 8)
    assert hashlib.sha256(base64.b64decode(raw["png"])).digest() == hashlib.sha256(original[0]).digest()
    captured = []
    monkeypatch.setattr(
        hm._Guest,
        "shell",
        lambda self, cmd, deadline: captured.append(cmd) or "No Error",
    )
    message = {
        "serial": SERIAL,
        "binding": guest.binding,
        "deadline": time.time() + 8,
        "action": "tap",
        "width": 160,
        "height": 240,
        "display": raw["display"],
        "x": 159,
        "y": 239,
    }
    assert hm.execute(message) == {"device_x": 159, "device_y": 239}
    with pytest.raises(MobileSimError) as out:
        hm.execute({**message, "x": 160})
    assert out.value.status == 422 and captured == ["uitest uiInput click 159 239"]
    monkeypatch.setattr(hm, "_capture", lambda *a, **k: (None, None, frame_png((240, 160))))
    with pytest.raises(MobileSimError) as stale:
        hm.execute(message)
    assert stale.value.status == 409 and len(captured) == 1


def test_app_launcher_is_resolved_from_declared_bundle_and_command_payload_is_bounded():
    info = {
        "mainEntry": "entry",
        "hapModuleInfos": [
            {
                "moduleName": "entry",
                "mainAbility": "Other",
                "abilityInfos": [
                    {"name": "Other"},
                    {
                        "name": "FirstAbility",
                        "skills": [{"actions": ["action.system.home"]}],
                    },
                    {"name": "no; command", "isLauncherAbility": True},
                ],
            }
        ],
    }
    assert hm._entry_ability("BundleInfo:\n" + json.dumps(info)) == "FirstAbility"
    assert hm._entry_ability("no such bundle") is None
    with pytest.raises(MobileSimError) as exc:
        hm._act(
            None,
            {
                "action": "open_app",
                "package": "x; echo other",
                "deadline": time.time() + 1,
            },
        )
    assert exc.value.status == 422


def test_acknowledges_uitest_success_and_preserves_real_errors(guest, monkeypatch):
    message = {"action": "back", "deadline": time.time() + 2}
    monkeypatch.setattr(hm._Guest, "shell", lambda self, cmd, deadline: "No Error\n")
    assert hm._act(guest, message) == {}
    monkeypatch.setattr(hm._Guest, "shell", lambda self, cmd, deadline: "No Error\nerror: input failed")
    with pytest.raises(MobileSimError) as result:
        hm._act(guest, message)
    assert result.value.status == 502
    monkeypatch.setattr(hm._Guest, "shell", lambda self, cmd, deadline: "[Fail] unknown command")
    with pytest.raises(MobileSimError) as rejected:
        hm._act(guest, message)
    assert rejected.value.status == 422
    for missing in ("", "incomplete status"):
        monkeypatch.setattr(hm._Guest, "shell", lambda self, cmd, deadline: missing)
        with pytest.raises(MobileSimError) as unknown:
            hm._act(guest, message)
        assert unknown.value.status == 502
    monkeypatch.setattr(hm._Guest, "shell", lambda self, cmd, deadline: "start ability successfully.")
    assert hm._act(guest, {**message, "action": "open_app", "package": "com.sample.app"}) == {}


def test_opaque_focused_route_masks_only_covered_old_controls():
    old = node(
        "NavDestination",
        "[0,0][160,240]",
        [
            node("Button", "[20,40][80,70]", text="Behind", clickable=True),
            node("Button", "[70,80][150,110]", text="Partially visible", clickable=True),
        ],
        focused=False,
        opacity=1,
        backgroundColor="#FFFFFFFF",
    )
    new = node(
        "NavDestination",
        "[0,0][115,240]",
        [node("Button", "[12,22][64,42]", text="Top", clickable=True)],
        focused=True,
        opacity=1,
        backgroundColor="#FF333344",
    )
    nav = node("NavigationContent", "[0,0][160,240]", [old, new])

    def current():
        return {i["text"]: i for i in all_nodes(native_page(nav, 160, 240)[0]) if i["actions"]}

    assert set(current()) == {"Top", "Partially visible"}
    assert current()["Partially visible"]["bounds"] == [115, 80, 150, 110]
    new["attributes"]["backgroundColor"] = "#AA333344"
    assert "Behind" in current()
    new["attributes"]["backgroundColor"] = "#FF333344"
    old["attributes"]["zIndex"] = 2
    assert "Behind" in current()
    nav["attributes"]["opacity"] = 0.5
    old["attributes"]["zIndex"] = 0
    assert "Behind" in current()
    new["attributes"].update(bounds="[0,0][160,240]")
    new["children"].clear()
    nav["attributes"]["opacity"] = 1
    messages = []
    assert not [i for i in all_nodes(native_page(nav, 160, 240, warnings=messages)[0]) if i["actions"]]
    assert len(messages) == 1


def test_arkweb_selected_state_requires_a_selectable_control_or_reported_selection():
    tree = node(
        "Column",
        "[0,0][160,240]",
        [
            node("button", "[2,2][45,35]", text="Plain", clickable=True, selected=False),
            node("tab", "[2,40][45,90]", text="Tab", clickable=True, selected=False),
            node(
                "button",
                "[2,100][60,150]",
                text="Selected",
                clickable=True,
                selected=True,
            ),
            node(
                "button",
                "[2,152][60,222]",
                text="ARIA",
                clickable=True,
                **{"aria-selected": False},
            ),
        ],
    )
    current = {i["text"]: i["state"] for i in all_nodes(native_page(tree, 160, 240)[0]) if i["actions"]}
    assert "selected" not in current["Plain"]
    assert current["Tab"]["selected"] is False and current["Selected"]["selected"] is True
    assert current["ARIA"]["selected"] is False


def test_arkweb_coextensive_interactive_layers_distinguish_drawing_from_hit_testing():
    box = "[0,0][160,240]"
    underlying = node(
        "genericContainer",
        box,
        [
            node("button", "[5,12][43,32]", text="One", clickable=True),
            node("button", "[25,190][83,220]", text="Two", clickable=True),
        ],
    )
    front = node(
        "genericContainer",
        box,
        [
            node("staticText", "[31,54][124,90]", text="Overlay title"),
            node("staticText", "[30,131][112,153]", text="Instruction"),
            node("button", "[39,165][102,211]", text="Choice", clickable=True),
        ],
        clickable=True,
    )
    floating = node(
        "genericContainer",
        box,
        [node("image", "[123,89][140,107]", description="Icon")],
        clickable=True,
    )
    canvas = node("canvas", box)
    doc = node(
        "rootWebArea",
        box,
        [node("genericContainer", box, [underlying, front]), floating, canvas],
    )
    host = node("Web", box, [doc], opacity=1, visible=True)

    def observed():
        warnings = []
        by_label = {
            i["text"]: i["actions"]
            for i in all_nodes(native_page(host, 160, 240, warnings=warnings)[0])
            if i["text"]
        }
        return by_label, warnings

    got, issues = observed()
    assert got["One"] == [] and got["Two"] == [] and got["Choice"] == ["tap"] and issues
    front["attributes"]["clickable"] = False
    got, issues = observed()
    assert got["One"] == ["try_tap"] and got["Choice"] == ["tap"] and issues
    front["attributes"]["hitTestBehavior"] = "HitTestMode.Transparent"
    got, _ = observed()
    assert got["One"] == ["tap"] and got["Choice"] == ["tap"]
    front["attributes"]["hitTestBehavior"] = ""
    front["children"].pop()
    got, issues = observed()
    assert got["One"] == ["tap"] and not issues


def test_web_passing_through_layers_disjoint_panes_and_tiny_labels_keep_underlying_actions():
    box = "[0,0][400,800]"
    app = node(
        "genericContainer",
        box,
        [
            node("button", "[20,40][92,85]", text="Up", clickable=True),
            node("button", "[20,540][92,585]", text="Down", clickable=True),
        ],
    )

    def read(layer, *, upper_action="tap"):
        doc = node("rootWebArea", box, [node("genericContainer", box, [app, layer])])
        items = native_page(node("Web", box, [doc]), 400, 800)[0]
        actions = {i["text"]: i["actions"] for i in all_nodes(items) if i["text"]}
        assert actions["Up"] == [upper_action] and actions["Down"] == ["tap"]

    disjoint = node(
        "genericContainer",
        box,
        [
            node("button", "[285,55][377,95]", text="North", clickable=True),
            node("button", "[285,622][377,662]", text="South", clickable=True),
        ],
        clickable=True,
    )
    read(disjoint)
    tiny = node(
        "genericContainer",
        box,
        [
            node("Text", "[18,43][19,44]", text="Tiny"),
            node("Text", "[19,532][20,533]", text="Other tiny"),
        ],
        clickable=True,
    )
    read(tiny)
    documented = node(
        "genericContainer",
        box,
        [
            node("Text", "[25,60][140,100]", text="Title"),
            node("Text", "[28,541][185,585]", text="Message"),
            node("button", "[30,430][145,470]", text="Choose", clickable=True),
        ],
        clickable=True,
    )
    for attr, value in (
        ("pointerEvents", "none"),
        ("pointer-events", "none"),
        ("hitTestBehavior", "HitTestMode.None"),
    ):
        read(node("genericContainer", box, [documented], **{attr: value}))
    documented["attributes"]["clipPath"] = "inset(200px 0 0 0)"
    read(documented)
    restored = node(
        "genericContainer",
        box,
        [
            node("button", "[20,40][92,85]", text="Covers upper", clickable=True, pointerEvents="auto"),
            node("button", "[285,622][377,662]", text="Other option", clickable=True, pointerEvents="auto"),
        ],
        pointerEvents="none",
    )
    read(restored, upper_action="try_tap")


def test_visual_drawer_clips_background_names_only_with_same_frame_paint_evidence():
    viewport = "[0,0][160,240]"
    behind = node(
        "genericContainer",
        viewport,
        [
            node("button", "[50,20][95,38]", text="Older one", clickable=True),
            node("Text", "[50,55][95,71]", text="Other witness"),
            node("button", "[50,120][97,140]", text="Older two", clickable=True),
            node("button", "[50,200][94,221]", text="Older three", clickable=True),
            node("button", "[4,110][30,124]", text="Outside", clickable=True),
            node("Text", "[15,175][72,197]", text="Partly covered"),
        ],
    )
    panel = node(
        "genericContainer",
        "[40,0][160,240]",
        [
            node("button", "[93,81][145,98]", text="Front action", clickable=True),
            node("Text", "[84,152][146,167]", text="Panel body"),
        ],
        clickable=True,
    )
    overlay = node("genericContainer", viewport, [panel], clickable=True)
    doc = node("rootWebArea", viewport, [node("genericContainer", viewport, [behind, overlay])])
    root = node("Web", viewport, [doc])
    image = Image.new("RGB", (160, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 0, 159, 239), fill=(38, 42, 48))
    draw.text((95, 81), "Action", fill="white")
    draw.text((87, 152), "Body", fill="white")

    def render(which=None):
        stream = io.BytesIO()
        if which:
            which.save(stream, format="PNG")
        normalized = native_page(root, 160, 240, screenshot=stream.getvalue() or None)[0]
        return list(all_nodes(normalized))

    def read_words(nodes):
        return {n["text"]: n for n in nodes if n.get("text")}

    revised = render(image)
    words = read_words(revised)
    assert set(words) == {"Outside", "Front action", "Panel body"}
    assert not words["Outside"]["actions"] and words["Front action"]["actions"] == ["tap"]
    assert [n["_captures_taps_in"] for n in revised if n.get("_captures_taps_in")] == [[[0, 0, 40, 240]]]
    assert "Older one" in read_words(render())
    assert "Older one" in read_words(render(Image.new("RGB", (32, 80))))
    divider = image.copy()
    ImageDraw.Draw(divider).line((40, 55, 151, 55), fill="white")
    assert "Older one" not in read_words(render(divider))
    marked = divider.copy()
    ImageDraw.Draw(marked).text((53, 57), "Legible", fill="white")
    assert "Older one" in read_words(render(marked))
    only_two_flat = behind["children"].pop(0)
    assert "Older two" in read_words(render(divider))
    behind["children"].insert(0, only_two_flat)
    transparent = image.copy()
    ImageDraw.Draw(transparent).text((52, 120), "Visible", fill="white")
    assert "Older two" in read_words(render(transparent))
    panel["attributes"]["opacity"] = "0.55"
    assert "Older one" in read_words(render(image))
    panel["attributes"].pop("opacity")
    doc["children"][0]["children"].pop()
    assert read_words(render(image))["Older three"]["actions"] == ["tap"]


def test_image_names_record_original_system_field_and_ignore_navigation_metadata():
    items = node(
        "Web",
        "[0,0][160,240]",
        [
            node(
                "rootWebArea",
                "[0,0][160,240]",
                [
                    node(
                        "image",
                        "[0,0][40,30]",
                        accessibilityText="Calendar",
                        description="Other",
                        text="image-key",
                    ),
                    node("image", "[0,40][40,70]", description="Schedule", text="asset"),
                    node("image", "[0,80][40,110]", text="document-mark"),
                    node("image", "[0,120][40,150]", text="https://invalid.test/?private=dummy"),
                ],
            )
        ],
    )
    imgs = [n for n in all_nodes(native_page(items, 160, 240)[0]) if n["role"] == "image"]
    assert [(n["name"], n.get("system_name_source")) for n in imgs] == [
        ("Calendar", "accessibility_text"),
        ("Schedule", "description"),
        ("document-mark", "image_text"),
        (None, None),
    ]


def test_crossing_divider_is_unknown_while_letters_or_a_contained_dash_remain_evidence():
    from ai_phone.agent.harmony_sim_panels import Screen

    image = Image.new("RGB", (140, 100), "black")

    def stroke(im):
        stream = io.BytesIO()
        im.save(stream, format="PNG")
        screen = Screen(stream.getvalue(), im.size)
        screen.pixels()
        return screen.stroke((24, 15, 100, 80))

    assert stroke(image) is False
    draw = ImageDraw.Draw(image)
    draw.line((8, 25, 117, 25), fill="white")
    assert stroke(image) is None
    draw.text((38, 44), "Word", fill="white")
    assert stroke(image) is True
    vertical = Image.new("RGB", image.size, "black")
    draw = ImageDraw.Draw(vertical)
    draw.line((53, 1, 53, 99), fill="white")
    assert stroke(vertical) is None
    short = Image.new("RGB", image.size, "black")
    ImageDraw.Draw(short).line((38, 49, 86, 49), fill="white")
    assert stroke(short) is True
