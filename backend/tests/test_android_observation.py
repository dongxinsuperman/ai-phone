import io
import json
import base64
import time
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest
from PIL import Image, ImageDraw

from ai_phone.agent import android_visual_regions
from ai_phone.agent.android_observation import describe


def png(width, height):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, format="PNG")
    return stream.getvalue()


def test_logical_bounds_project_before_merging_screenshot_targets(monkeypatch):
    xml = """<hierarchy>
      <node class="android.app.Dialog" bounds="[180,20][400,200]" text="Settings">
        <node clickable="true" bounds="[241,41][360,91]" text="Apply"/>
        <node clickable="true" bounds="[200,164][380,195]" text="Behind keyboard"/>
      </node>
    </hierarchy>"""
    window = "mType=ime mVisible=true mFrame=[0,160][400,200]"
    image = png(200, 100)

    def pixels(data, width, height, system_targets):
        assert data == image and (width, height) == (200, 100)
        button, covered = system_targets
        assert button["bounds"] == [121, 21, 180, 46] and button["point"] == [150, 33]
        assert covered["point"] is None
        return [
            {
                "id": "V1",
                "name": None,
                "source": "pixels",
                "actions": ["try_tap"],
                "bounds": [125, 12, 145, 20],
                "point": [135, 16],
                "state": {},
            }
        ]

    monkeypatch.setattr(android_visual_regions, "visual_targets", pixels)
    frame = describe(xml, image, 200, 100, window, display_size=(400, 200))
    assert frame["warnings"] == []
    assert frame["keyboard"]["bounds"] == [0, 80, 200, 100]
    assert frame["dialogs"][0]["bounds"] == [90, 10, 200, 100]
    assert frame["targets"][-1]["point"] == [135, 16]


def test_equal_display_size_preserves_frame_and_foreground_dialog():
    xml = """<hierarchy>
      <node text="Page behind dialog" clickable="true" bounds="[0,0][200,100]"/>
      <node class="android.app.Dialog" text="Choose layout" bounds="[50,30][150,90]">
        <node text="Confirm" clickable="true" bounds="[75,55][125,85]"/>
      </node>
    </hierarchy>"""
    image = png(200, 100)
    direct = describe(xml, image, 200, 100, "mInputShown=false")
    assert describe(xml, image, 200, 100, "mInputShown=false", display_size=(200, 100)) == direct
    assert direct["texts"] == ["Choose layout", "Confirm"]
    assert [target["name"] for target in direct["targets"]] == ["Confirm"]
    assert direct["targets"][0]["point"] == [100, 70]


@pytest.mark.parametrize("dimmed", [True, False])
def test_painted_modal_suppresses_background_and_panel_shell(monkeypatch, dimmed):

    image = Image.new("RGB", (360, 600), "#505050" if dimmed else "#ececec")
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 85, 355, 93), fill="#424242")
    draw.rectangle((5, 478, 355, 486), fill="#424242")
    draw.rounded_rectangle((36, 170, 323, 449), 10, fill="white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    xml = """<hierarchy><node class="android.webkit.WebView" clickable="true" bounds="[0,0][360,600]">
        <node text="Background" clickable="true" bounds="[18,76][170,115]"/>
        <node text="Covered" clickable="true" bounds="[81,258][220,297]"/>
        <node class="android.view.View" clickable="true" bounds="[36,170][324,450]">
          <node text="Accept" clickable="true" bounds="[125,325][235,380]"/>
          <node text="Cancel" clickable="true" bounds="[150,398][210,435]"/>
        </node>
    </node></hierarchy>"""
    regions = [
        {"id": "V1", "bounds": [40, 174, 320, 445], "point": [180, 309]},
        {"id": "V2", "bounds": [80, 245, 240, 302], "point": [160, 273]},
        {"id": "V3", "bounds": [19, 460, 130, 510], "point": [75, 485]},
    ]
    for region in regions:
        region.update(source="pixels", role="enclosed_surface", actions=["try_tap"], name=None)
    monkeypatch.setattr(android_visual_regions, "visual_targets", lambda *args: regions)
    frame = describe(xml, buffer.getvalue(), 360, 600, "mInputShown=false")
    expected = {"Accept", "Cancel"}
    assert set(frame["texts"]) == (expected if dimmed else expected | {"Background", "Covered"})
    assert [t["id"] for t in frame["targets"] if t["source"] == "pixels"] == (["V2"] if dimmed else ["V1", "V2", "V3"])
    assert len([t for t in frame["targets"] if t["source"] == "android"]) == (2 if dimmed else 6)


def test_password_value_and_descendant_echo_are_excluded():
    xml = """<hierarchy>
      <node class="android.widget.EditText" password="true" text="raw-secret" hint="Password" bounds="[0,0][200,40]">
        <node text="nested-secret" bounds="[0,0][200,40]"/>
      </node>
      <node content-desc="echo raw-secret" clickable="true" bounds="[0,40][200,65]"/>
      <node text="echo nested-secret" bounds="[0,65][200,85]"/>
    </hierarchy>"""
    result = describe(xml, png(200, 100), 200, 100, "")
    assert result["targets"][0]["name"] == "Password"
    assert result["targets"][0]["actions"] == ["input"]
    serialized = json.dumps(result)
    assert "raw-secret" not in serialized and "nested-secret" not in serialized


@pytest.mark.parametrize(("attributes", "expected", "private"), [
    ({"text": "", "hint": "Passphrase"}, "Passphrase", []),
    ({"text": "Passphrase", "hint": "Passphrase"}, "Passphrase", []),
    ({"text": "Passphrase", "hint": "Passphrase", "showing-hint-text": "true"}, "Passphrase", []),
    ({"text": "typed-credential", "hint": "Passphrase"}, "Passphrase", ["typed-credential"]),
    ({"text": "••••", "hint": "Passphrase", "autofilled": "true", "autofill-value": "saved-credential"},
     "Passphrase", ["saved-credential"]),
    ({"text": "filled-credential", "hint": "filled-credential", "showingHintText": "false"},
     None, ["filled-credential"]),
    ({"text": "filled-credential", "hint": "filled-credential", "autofilled": "true"},
     None, ["filled-credential"]),
    ({"text": "typed-only"}, None, ["typed-only"]),
])
def test_password_names_only_use_independent_system_hints(attributes, expected, private):
    root = ET.Element("hierarchy")
    ET.SubElement(root, "node", {"class": "android.widget.EditText", "password": "true",
                                "bounds": "[2,4][160,44]", **attributes})
    for word in private:
        ET.SubElement(root, "node", {"text": "Echo " + word, "bounds": "[2,58][200,87]"})
    result = describe(ET.tostring(root), png(220, 120), 220, 120, "mInputShown=false")
    target, = result["targets"]
    assert target["name"] == expected and target["name_source"] == ("system" if expected else None)
    if expected:
        assert target["system_name_source"] == "hint" and target["name_bounds"] == [2, 4, 160, 44]
    assert not result["texts"] and not result["system_texts"] and not result["screen_texts"]
    assert all(word not in json.dumps(result) for word in private)


def test_a_matching_password_hint_cannot_override_another_fields_private_value():
    xml = '''<hierarchy>
      <node class="android.widget.EditText" password="true" text="shared-private" hint="shared-private"
            bounds="[0,0][180,35]"/>
      <node class="android.widget.EditText" password="true" text="shared-private" hint="Second field"
            bounds="[0,40][180,75]"/>
    </hierarchy>'''
    frame = describe(xml, png(200, 100), 200, 100, "mInputShown=false")
    assert [target["name"] for target in frame["targets"]] == [None, "Second field"]
    assert "shared-private" not in json.dumps(frame)


def test_android_observation_keeps_system_text_without_running_screenshot_ocr(monkeypatch):
    from ai_phone.agent import android_visible_text

    xml = '''<hierarchy><node text="The morning train leaves from this platform." bounds="[5,4][210,82]"/>
      <node clickable="true" text="Platform" bounds="[8,100][70,140]"/></hierarchy>'''
    calls = []
    monkeypatch.setattr(android_visible_text, "enrich_from_pixels", lambda *args, **kwargs: calls.append(args))
    frame = describe(xml, png(220, 150), 220, 150, "mInputShown=false")
    assert calls == []
    assert [t["id"] for t in frame["targets"]] == ["A1"]
    assert frame["screen_texts"] == []
    assert frame["screen_texts_available"] is False
    assert frame["system_texts"][0]["text"] == "The morning train leaves from this platform."


def test_only_the_nearest_actionable_owner_inherits_its_name_nodes_selection():
    xml = """<hierarchy>
      <node clickable="true" bounds="[0,0][200,100]" content-desc="Options" selected="false">
        <node text="Options" selected="false" bounds="[0,0][60,20]"/>
        <node clickable="true" bounds="[0,25][200,60]" selected="false">
          <node text="Normal" selected="false" bounds="[0,25][60,60]"/>
        </node>
        <node clickable="true" bounds="[0,60][200,100]" selected="false">
          <node text="Fast" selected="true" bounds="[0,60][60,100]"/>
        </node>
      </node>
    </hierarchy>"""
    frame = describe(xml, png(200, 100), 200, 100, "mInputShown=false")
    targets = [t for t in frame["targets"] if t["source"] == "android"]
    assert [(t["name"], t["state"]["selected"]) for t in targets] == [
        ("Options", False),
        ("Normal", False),
        ("Fast", True),
    ]
    assert all(t["state_evidence"]["selected"] == "owned_name_node" for t in targets)


def test_named_control_and_its_secondary_text_stay_together():

    xml = """<hierarchy>
      <node scrollable="true" bounds="[0,0][200,120]">
        <node clickable="true" content-desc="Open record" bounds="[0,10][180,85]">
          <node text="Quarterly" bounds="[12,21][104,43]"/>
          <node text="18:30" bounds="[12,50][70,70]"/>
          <node clickable="true" content-desc="Save" bounds="[134,18][174,60]"/>
        </node>
        <node text="Section" bounds="[4,86][90,114]"/>
      </node>
    </hierarchy>"""
    frame = describe(xml, png(200, 120), 200, 120, "mInputShown=false")
    item = next(t for t in frame["targets"] if t["name"] == "Open record")
    assert item["name_source"] == "system" and item["system_name_source"] == "content-desc"
    assert {s["text"]: s["target"] for s in frame["system_texts"]} == {
        "Quarterly": item["id"], "18:30": item["id"], "Section": None,
    }
    assert next(t for t in frame["targets"] if t["name"] == "Save")["id"] != item["id"]


def test_public_agent_reads_and_marks_the_same_selected_png(monkeypatch):
    from ai_phone.agent import android_frame_marks, android_observation, unified_operations as operations
    from ai_phone.shared.android_frame_regions import sample_original

    raw = png(230, 140)
    captured = []
    display = {"width": 230, "height": 140, "rotation": 0}
    xml = '<hierarchy><node text="Open" clickable="true" bounds="[60,30][160,100]"/></hierarchy>'
    monkeypatch.setattr(operations.observation_io, "display_state", lambda *args: dict(display))
    monkeypatch.setattr(operations.observation_io, "hierarchy", lambda *args: xml)

    def read(_device, command, *_args):
        if command == operations.observation_io.WINDOW:
            return b"mInputShown=false"
        captured.append(command)
        return raw

    inputs = []
    describe_frame = android_observation.describe
    paint = android_frame_marks.annotate_frame

    def describe_selected(xml, selected, *args, **kwargs):
        inputs.append(selected)
        return describe_frame(xml, selected, *args, **kwargs)

    def mark_selected(selected, targets):
        inputs.append(selected)
        return paint(selected, targets)

    monkeypatch.setattr(android_observation, "describe", describe_selected)
    monkeypatch.setattr(android_frame_marks, "annotate_frame", mark_selected)
    monkeypatch.setattr(operations.observation_io, "read_exec", read)
    result = operations._observe(object(), time.time() + 10)
    marked = base64.b64decode(result["png"])
    assert len(captured) == 1 and inputs == [raw, raw] and result["marked_targets"] is True
    assert result["targets"][0]["name_source"] == "system"
    assert result["targets"][0]["bounds"] == [60, 30, 160, 100]
    with Image.open(io.BytesIO(marked)) as marked_image:
        assert marked_image.size == (230, 140)
        assert marked_image.convert("RGB").tobytes() != Image.open(io.BytesIO(raw)).tobytes()
        assert result["_raw_grid_v1"] != sample_original(marked_image)
    with Image.open(io.BytesIO(raw)) as original:
        assert result["_raw_grid_v1"] == sample_original(original)


_CONTROL_GRID = ((8, 8, 58, 32), (90, 8, 142, 32), (174, 8, 228, 32),
                 (8, 68, 58, 96), (90, 68, 142, 96), (174, 68, 228, 96))


def _layout_png(color):
    image = Image.new("RGB", (240, 108), "white")
    draw = ImageDraw.Draw(image)
    for left, top, right, bottom in _CONTROL_GRID:
        draw.rectangle((left + 4, top + 4, right - 4, bottom - 4), fill=color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _mock_frames(monkeypatch, frames, boxes, *, transition=False):
    from ai_phone.agent import android_frame_marks, unified_operations as operations

    display = {"width": 240, "height": 108, "rotation": 1}
    monkeypatch.setattr(operations.observation_io, "display_state", lambda *_: display)
    xml = "<hierarchy>" + "".join(
        f'<node text="Control {i}" clickable="true" bounds="[{left},{top}][{right},{bottom}]"/>'
        for i, (left, top, right, bottom) in enumerate(boxes)
    ) + "</hierarchy>"
    tree = Mock(return_value=xml)
    monkeypatch.setattr(operations.observation_io, "hierarchy", tree)
    screenshots = Mock(side_effect=frames)

    def read(_, command, *_args):
        if command == operations.observation_io.WINDOW:
            return b"mDisplayFrozen=true" if transition else b"mInputShown=false"
        return screenshots()

    monkeypatch.setattr(operations.observation_io, "read_exec", read)
    mark = Mock(side_effect=lambda image, _: image)
    monkeypatch.setattr(android_frame_marks, "annotate_frame", mark)
    result = operations._observe(object(), time.time() + 9)
    assert mark.call_count == 1
    assert mark.call_args.args[0] == base64.b64decode(result["png"])
    return result, screenshots.call_count, tree.call_count


def test_observation_returns_the_first_loading_frame_without_retry(monkeypatch):
    loading, settled = _layout_png("white"), _layout_png("navy")
    result, captures, reads = _mock_frames(monkeypatch, [loading, settled], _CONTROL_GRID, transition=True)
    assert (captures, reads) == (1, 1)
    assert base64.b64decode(result["png"]) == loading


def test_missing_tree_keeps_the_single_screenshot_and_marks_structure_unavailable(monkeypatch):
    from ai_phone.agent import unified_operations as operations

    raw = png(240, 108)
    display = {"width": 240, "height": 108, "rotation": 1}
    monkeypatch.setattr(operations.observation_io, "display_state", lambda *_: display)
    monkeypatch.setattr(operations, "_observation_metadata", lambda *_: ("", "", [], []))
    capture = Mock(return_value=raw)
    monkeypatch.setattr(operations.observation_io, "read_exec", capture)
    frame = operations._observe(object(), time.time() + 9)
    capture.assert_called_once()
    assert frame["targets"] == [] and frame["structure_status"] == "unavailable"
    assert base64.b64decode(frame["png"]) == raw


def test_unlabelled_targets_mark_observation_structure_partial(monkeypatch):
    from ai_phone.agent import unified_operations as operations

    raw = png(240, 108)
    display = {"width": 240, "height": 108, "rotation": 1}
    xml = '<hierarchy><node clickable="true" bounds="[8,8][58,32]"/></hierarchy>'
    monkeypatch.setattr(operations.observation_io, "display_state", lambda *_: display)
    monkeypatch.setattr(operations, "_observation_metadata", lambda *_: (xml, "", [], []))
    monkeypatch.setattr(operations.observation_io, "read_exec", lambda *_: raw)
    frame = operations._observe(object(), time.time() + 9)
    assert frame["tree_available"] is True
    assert [(target["id"], target["name"]) for target in frame["targets"]] == [("A1", None)]
    assert frame["structure_status"] == "partial"


def test_contradictory_png_and_display_orientation_never_offer_unsafe_coordinates(monkeypatch):
    from ai_phone.agent import unified_operations as operations

    portrait = png(108, 240)
    with pytest.raises(operations.OperationError, match="无法按图操作"):
        _mock_frames(monkeypatch, [portrait], _CONTROL_GRID)


def test_webview_image_text_can_name_its_own_control_but_asset_keys_cannot():
    xml = '''<hierarchy><node class="android.webkit.WebView" bounds="[0,0][240,140]">
      <node clickable="true" bounds="[0,0][110,58]">
        <node class="android.widget.Image" text="预约记录" bounds="[0,0][110,58]"/>
        <node class="android.widget.Image" text="nav_active__v2" bounds="[33,8][79,47]"/>
      </node>
      <node clickable="true" bounds="[120,0][230,58]">
        <node class="android.widget.Image" text="active" bounds="[120,0][230,58]"/>
      </node>
      <node clickable="true" bounds="[0,70][220,138]">
        <node class="android.widget.Image" text="通知图标" bounds="[4,78][28,104]"/>
      </node>
    </node></hierarchy>'''
    frame = describe(xml, png(240, 140), 240, 140, "mInputShown=false")
    assert [(t['name'], t['name_source']) for t in frame['targets']] == [
        ('预约记录', 'system'), (None, None), (None, None),
    ]
    assert frame['targets'][0]['system_name_source'] == 'descendant_image_text'
    assert [(w['text'], w['target']) for w in frame['system_texts']] == [('预约记录', 'A1')]


def test_two_image_buttons_with_conflicting_graphics_keep_duplicated_alt_as_a_hint():
    from PIL import ImageDraw

    xml = '''<hierarchy><node class="android.webkit.WebView" bounds="[0,0][120,40]">
      <node clickable="true" bounds="[0,0][60,40]">
        <node class="android.widget.Image" text="Open panel" bounds="[0,0][60,40]"/>
      </node>
      <node clickable="true" bounds="[60,0][120,40]">
        <node class="android.widget.Image" text="Open panel" bounds="[60,0][120,40]"/>
      </node>
    </node></hierarchy>'''
    identical = describe(xml, png(120, 40), 120, 40, None)
    assert [t['name'] for t in identical['targets'] if t['source'] == 'android'] == ['Open panel'] * 2
    art = Image.new('RGB', (120, 40), 'white')
    ImageDraw.Draw(art).rectangle((3, 3, 47, 34), fill='black')
    data = io.BytesIO()
    art.save(data, format='PNG')
    changed = describe(xml, data.getvalue(), 120, 40, None)
    buttons = [t for t in changed['targets'] if t['source'] == 'android']
    assert [(t['name'], t['system_hint']) for t in buttons] == [(None, 'Open panel')] * 2


def test_loading_region_is_removed_from_the_public_list_before_visual_naming(monkeypatch):
    from ai_phone.agent import android_visible_text

    xml = '''<hierarchy>
      <node clickable="true" bounds="[15,8][115,36]"/>
      <node clickable="true" text="Continue" bounds="[15,48][115,76]"/>
    </hierarchy>'''
    monkeypatch.setattr(android_visual_regions, 'system_placeholders', lambda *args: {'A1'})

    def check_exclusions(image, targets, excluded, system_records):
        assert [t['id'] for t in targets] == ['A2']
        assert (15, 8, 115, 36) in excluded

    monkeypatch.setattr(android_visible_text, 'associate_system_text', check_exclusions)
    frame = describe(xml, png(160, 90), 160, 90, 'mInputShown=false')
    assert [t['name'] for t in frame['targets']] == ['Continue']
    assert not frame['warnings']


def test_readiness_skips_transition_and_loading_probes_then_captures_once(monkeypatch):
    from ai_phone.agent import android_webview, unified_operations as operations

    focus = 'mCurrentFocus=Window{1 u0 com.example/.Page}'
    display = {"width": 240, "height": 108, "rotation": 1}
    xml = '<hierarchy><node text="Ready" clickable="true" bounds="[8,8][58,32]"/></hierarchy>'
    loading_xml = '<hierarchy><node class="android.widget.ProgressBar" bounds="[10,10][40,40]"/></hierarchy>'
    raw = _layout_png("navy")
    probes = iter([focus + "\nmAppTransitionState=APP_STATE_RUNNING"] + [focus] * 3)
    shots = []
    metadata = Mock(side_effect=[(loading_xml, focus, [], []), (xml, focus, [], [])])

    def read(_device, command, *_args):
        if command == operations.observation_io.READINESS_WINDOW:
            return next(probes).encode()
        shots.append(command)
        return raw

    monkeypatch.setattr(operations.observation_io, "display_state", lambda *_: display)
    monkeypatch.setattr(operations.observation_io, "read_exec", read)
    monkeypatch.setattr(operations, "_observation_metadata", metadata)
    monkeypatch.setattr(android_webview, "loading", Mock(side_effect=[True, False, False]))
    frame = operations._observe(object(), time.time() + 10, wait_ready=True, expected_text="Ready")
    assert len(shots) == 1 and metadata.call_count == 2
    assert frame["readiness"] == "expected_text_present"
    assert frame["targets"][0]["name"] == "Ready"


def test_readiness_timeout_does_not_capture_a_loading_frame(monkeypatch):
    from ai_phone.agent import unified_operations as operations

    display = {"width": 240, "height": 108, "rotation": 1}
    monkeypatch.setattr(operations.observation_io, "display_state", lambda *_: display)
    capture = Mock()
    monkeypatch.setattr(operations.observation_io, "read_exec", capture)
    with pytest.raises(operations.OperationError, match="未确认页面就绪") as error:
        operations._observe(object(), time.time() + 1, wait_ready=True)
    assert error.value.status == 504
    capture.assert_not_called()
