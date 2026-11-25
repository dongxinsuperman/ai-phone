import io
import json
import time
from unittest.mock import Mock

from PIL import Image

from ai_phone.agent import android_frame_marks, android_observation, unified_operations


def white_png():
    buffer = io.BytesIO()
    Image.new("RGB", (240, 160), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def descendants(node):
    yield node
    for child in node.get("children", []):
        yield from descendants(child)


def test_android_structure_keeps_native_list_and_owned_body_within_foreground():
    xml = '''<hierarchy>
      <node text="Background only" clickable="true" bounds="[0,0][240,160]"/>
      <node role="dialog" bounds="[5,5][235,155]">
        <node class="android.widget.ListView" scrollable="true" bounds="[8,8][232,151]">
          <node heading="true" text="Reading list" bounds="[12,10][110,26]"/>
          <node class="android.widget.Button" content-desc="Open book" clickable="true" bounds="[12,28][225,68]">
            <node text="Sea and ice" bounds="[15,30][120,42]"/>
            <node text="Volume one" bounds="[15,48][110,62]"/>
          </node>
          <node class="android.widget.EditText" password="true" text="secret-value" hint="Passphrase"
                bounds="[12,73][225,96]"/>
          <node text="Under keyboard" clickable="true" bounds="[12,124][212,147]"/>
        </node>
      </node>
    </hierarchy>'''
    frame = android_observation.describe(xml, white_png(), 240, 160, 'mType=ime mVisible=true mFrame=[0,118][240,160]')
    dialog, = frame["structure"]["children"]
    assert dialog["role"] == "dialog"
    listing, = dialog["children"]
    assert listing["role"] == "list" and listing["actions"] == ["scroll"]
    heading, book, password = listing["children"]
    assert heading["role"] == "heading" and heading["text"] == "Reading list" and "target" not in heading
    assert book["role"] == "button" and book["name"] == "Open book" and book["actions"] == ["tap"]
    assert [child["text"] for child in book["children"]] == ["Sea and ice", "Volume one"]
    assert all(child["belongs_to"] == book["target"] and "target" not in child for child in book["children"])
    assert password["name"] == "Passphrase" and password["state"]["password"] is True
    published = json.dumps(frame)
    assert all(word not in published for word in ("Background only", "secret-value", "Under keyboard"))


def test_truncated_labels_do_not_reappear_from_raw_xml(monkeypatch):
    monkeypatch.setattr(android_observation, "_MAX_TEXTS", 2)
    xml = '''<hierarchy><node text="First" bounds="[0,0][100,20]"/>
      <node text="Second" bounds="[0,20][100,40]"/><node text="Not published" bounds="[0,40][100,60]"/>
    </hierarchy>'''
    result = android_observation.describe(xml, white_png(), 240, 160, "mInputShown=false")
    assert result["texts_truncated"] is True
    words = [n["text"] for n in descendants(result["structure"]) if "text" in n]
    assert words == ["First", "Second"]


def test_explicit_no_image_skips_drawing_and_preserves_coordinates_and_private_change_sample(monkeypatch):
    operations = unified_operations
    png = white_png()
    xml = '<hierarchy><node clickable="true" text="Save" bounds="[25,30][100,60]"/></hierarchy>'
    display = {"width": 240, "height": 160, "rotation": 1}
    monkeypatch.setattr(operations, "device_transport", lambda _: 1)
    monkeypatch.setattr(operations.adb, "device", lambda **_: object())
    monkeypatch.setattr(operations.observation_io, "display_state", lambda *_: display)
    monkeypatch.setattr(operations.observation_io, "hierarchy", lambda *_: xml)
    monkeypatch.setattr(operations.observation_io, "read_exec", lambda _, cmd, *_args:
                        b"mInputShown=false" if cmd == operations.observation_io.WINDOW else png)
    paint = Mock(wraps=android_frame_marks.annotate_frame)
    monkeypatch.setattr(android_frame_marks, "annotate_frame", paint)
    request = {"serial": "test-android-structure", "transport": 1, "action": "observe", "deadline": time.time() + 15}
    plain = operations.execute({**request, "include_screenshot": False})
    paint.assert_not_called()
    assert "png" not in plain and not plain["marked_targets"] and plain["_raw_grid_v1"]
    with_image = operations.execute(request)
    paint.assert_called_once()
    assert with_image["png"] and with_image["marked_targets"]
    for field in ("targets", "structure", "system_texts", "screen_texts", "height", "width", "display", "_raw_grid_v1"):
        assert with_image[field] == plain[field]
