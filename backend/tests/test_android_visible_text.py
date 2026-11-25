"""Layout and ownership tests; glyph accuracy is covered by test_android_glyphs."""

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ai_phone.agent import android_glyphs
from ai_phone.agent.android_visible_text import enrich_from_pixels


class Scene:
    def __init__(self):
        self.image = Image.new("RGB", (720, 450), "#e4e7ed")
        self.draw = ImageDraw.Draw(self.image)
        self.labels = {}

    def label(self, point, text, size=24, color="#242635"):
        font = ImageFont.load_default(size=size)
        mask = Image.new("L", self.image.size)
        ImageDraw.Draw(mask).text(point, text, fill=255, font=font, anchor="lt")
        self.draw.text(point, text, fill=color, font=font, anchor="lt")
        box = mask.getbbox()
        self.labels[text] = self._signature(np.asarray(mask.crop(box)) >= 145)
        return box

    @staticmethod
    def _signature(mask):
        ys, xs = np.nonzero(mask)
        trimmed = mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        raster = np.asarray(Image.fromarray(trimmed).resize((140, 28), Image.Resampling.NEAREST))
        return raster, trimmed.shape[1] / trimmed.shape[0]

    def recognize(self, masks):
        # A fixed alphabet of complete rendered phrases isolates image layout
        # from host fonts. Tests still require the original stroke silhouettes.
        result = []
        for mask in masks:
            pixels, aspect = self._signature(mask)
            words = [
                word
                for word, (expected, ratio) in self.labels.items()
                if abs(aspect / ratio - 1) < 0.12
                and 2 * (pixels & expected).sum() / (pixels.sum() + expected.sum()) > 0.86
            ]
            result.append(words[0] if len(words) == 1 else None)
        return result


def target(identifier, bounds, source="pixels", name=None):
    left, top, right, bottom = bounds
    return {
        "id": identifier,
        "bounds": list(bounds),
        "point": [(left + right) // 2, (top + bottom) // 2],
        "source": source,
        "actions": ["try_tap"] if source == "pixels" else ["tap"],
        "name": name,
        "state": {},
    }


def test_text_left_of_picture_and_below_picture_have_their_own_measured_card(
    monkeypatch,
):
    scene = Scene()
    cards = [(25, 26, 349, 174), (390, 26, 693, 243)]
    for card in cards:
        scene.draw.rounded_rectangle(card, 11, fill="white")
    thumbs = [(217, 48, 327, 145), (413, 44, 666, 145)]
    for box in thumbs:
        scene.draw.rounded_rectangle(box, 5, fill="#9bb7c6")
    scene.label((47, 73), "Amber")
    scene.label((434, 170), "Quartz")
    scene.label((47, 315), "Outside")
    targets = [target("V1", thumbs[0]), target("V2", thumbs[1])]
    points = [t["point"][:] for t in targets]
    monkeypatch.setattr(android_glyphs, "recognize_masks", scene.recognize)
    lines = enrich_from_pixels(scene.image, targets, [], [])
    ids = {line["text"]: line["target"] for line in lines}
    assert ids == {"Amber": "V1", "Quartz": "V2", "Outside": "T1"}
    assert [(t["name"], t["name_source"], t["point"]) for t in targets[:2]] == [
        ("Amber", "screen", points[0]),
        ("Quartz", "screen", points[1]),
    ]
    for row, box in zip(targets, cards):
        assert all(abs(a - b) <= 3 for a, b in zip(row["bounds"], box))
    assert targets[-1]["role"] == "visible_text" and targets[-1]["actions"] == ["try_tap"]


def test_coequal_children_and_system_evidence_never_lend_or_replace_names(monkeypatch):
    scene = Scene()
    scene.draw.rounded_rectangle((20, 26, 690, 245), 9, fill="white")
    first = (43, 87, 191, 183)
    second = (514, 87, 665, 183)
    scene.draw.rounded_rectangle(first, 6, fill="#d7e4ec")
    scene.draw.rounded_rectangle(second, 6, fill="#d7e4ec")
    known_word = scene.label((68, 120), "Open")
    scene.label((267, 120), "Unowned")
    disputed = scene.label((539, 120), "Silver")
    targets = [target("A1", first, "android", "Open"), target("V1", second)]
    monkeypatch.setattr(android_glyphs, "recognize_masks", scene.recognize)
    lines = enrich_from_pixels(scene.image, targets, [], [("Open", known_word), ("OTHER", disputed)])
    assert [t["name"] for t in targets[:2]] == ["Open", None]
    assert [(r["text"], r["target"]) for r in lines] == [("Unowned", "T1")]


def test_password_coverage_and_full_screen_scroll_do_not_swallow_other_labels(
    monkeypatch,
):
    scene = Scene()
    secret = scene.label((40, 60), "Hidden")
    scene.label((40, 185), "Visible")
    targets = [target("A1", (15, 30, 370, 115), "android")]
    targets[0]["state"]["password"] = True
    targets.append(
        {
            "id": "A2",
            "bounds": [0, 0, 720, 450],
            "role": "scroll_area",
            "name": None,
            "point": [360, 225],
            "source": "android",
            "actions": ["scroll"],
        }
    )
    monkeypatch.setattr(android_glyphs, "recognize_masks", scene.recognize)
    lines = enrich_from_pixels(scene.image, targets, [secret], [])
    assert [(r["text"], r["target"]) for r in lines] == [("Visible", "T1")]
    assert targets[0]["name"] is None


def test_equal_lines_in_one_target_have_no_arbitrary_primary_name(monkeypatch):
    scene = Scene()
    panel = (45, 45, 370, 200)
    scene.draw.rounded_rectangle(panel, 8, fill="white")
    scene.label((75, 70), "North")
    scene.label((75, 133), "South")
    targets = [target("A1", panel, "android")]
    monkeypatch.setattr(android_glyphs, "recognize_masks", scene.recognize)
    lines = enrich_from_pixels(scene.image, targets, [], [])
    assert {r["text"] for r in lines} == {"North", "South"}
    assert {r["target"] for r in lines} == {"A1"}
    assert targets[0]["name"] is None


def test_printed_labels_keep_small_bounds_inside_a_large_interactive_illustration(
    monkeypatch,
):
    scene = Scene()
    paper = (28, 30, 688, 298)
    scene.draw.rectangle(paper, fill="#6b96d4")
    for x in range(29, 687, 9):
        colour = (97 + x % 40, 115 + x % 45, 192 + x % 38)
        scene.draw.rectangle((x, 31, min(687, x + 8), 297), fill=colour)
    labels = []
    for pos, word in [((77, 91), "Delta"), ((471, 205), "River")]:
        font = ImageFont.load_default(size=27)
        labels.append(scene.label(pos, word, 27, "white"))
        scene.draw.text(
            pos,
            word,
            font=font,
            anchor="lt",
            fill="white",
            stroke_fill="#323f58",
            stroke_width=3,
        )
    monkeypatch.setattr(android_glyphs, "recognize_masks", scene.recognize)
    for name in (None, "Gallery"):
        box_target = target("A7", paper, "android", name)
        rows = enrich_from_pixels(scene.image, [box_target], [], [])
        assert [(r["text"], r["target"]) for r in rows] == [
            ("Delta", "A7"),
            ("River", "A7"),
        ]
        for row, expected in zip(rows, labels):
            assert max(abs(a - b) for a, b in zip(row["bounds"], expected)) <= 2
        assert box_target["name"] == name


def test_a_piece_of_a_named_system_key_is_not_reported_as_another_reading(monkeypatch):
    scene = Scene()
    scene.label((104, 93), "Rate")
    scene.draw.line((158, 104, 169, 104), fill="#242635", width=2)
    scene.label((109, 155), "Local")
    key = target("A1", (75, 67, 237, 197), "android", "Rate−")
    monkeypatch.setattr(android_glyphs, "recognize_masks", scene.recognize)
    rows = enrich_from_pixels(scene.image, [key], [], [])
    assert [(r["text"], r["target"]) for r in rows] == [("Local", "A1")]
    assert key["name"] == "Rate−"


def test_system_caption_precedes_a_conflicting_centered_transcription_only():
    from ai_phone.agent.android_visible_text import _system_has_line

    labels = [((0, 0, 200, 90), "支付方式")]
    centered = (40, 30, 160, 60)
    assert _system_has_line(centered, "支付方试", labels)
    assert not _system_has_line(centered, "打开设置", labels)
    assert not _system_has_line((40, 59, 160, 89), "支付方试", labels)
    assert not _system_has_line(centered, "支付方试", [((0, 0, 1000, 620), "支付方式")])


def test_unowned_system_text_names_measured_card_and_keeps_caption_on_same_target():
    import numpy as np
    from ai_phone.agent.android_visible_text import _bind_system_panels, _Panel

    def labels():
        return [
            {"text": "Entry", "bounds": [22, 15, 65, 34], "target": None},
            {"text": "Short description", "bounds": [22, 39, 130, 51], "target": None},
            {"text": "Outside", "bounds": [15, 78, 88, 98], "target": None},
        ]

    size = (200, 160)
    gray = np.full(size[::-1], 250, dtype=np.uint8)
    panel = _Panel((8, 7, 161, 66), 250)
    for targets in (
        [],
        [
            {
                "id": "V5",
                "source": "pixels",
                "bounds": list(panel.box),
                "point": [90, 37],
                "name": None,
                "actions": ["try_tap"],
            }
        ],
    ):
        rows = labels()
        _bind_system_panels(targets, rows, [panel], [], gray, size)
        assert len(targets) == 1
        control = targets[0]
        assert (control["name"], control["name_source"], control["source"]) == ("Entry", "system", "pixels")
        assert control["name_bounds"] == rows[0]["bounds"] and control["actions"] == ["try_tap"]
        assert [row["target"] for row in rows] == [control["id"], control["id"], None]

    button = {
        "id": "A3",
        "source": "android",
        "bounds": [82, 13, 155, 59],
        "point": [110, 30],
        "name": "Pin",
        "actions": ["tap"],
    }
    targets = [button]
    rows = labels()
    _bind_system_panels(targets, rows, [panel], [], gray, size)
    assert targets == [button] and all(row["target"] is None for row in rows)
