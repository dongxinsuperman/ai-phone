import base64
import io

import pytest
from PIL import Image, ImageDraw

from ai_phone.shared.mobile_frame import build_frame, structure_from_observation


def walk(node):
    yield node
    for child in node.get("children", ()):
        yield from walk(child)


def specimen():
    return [{
        "role": "window", "bounds": [0, 0, 200, 240], "children": [
            {"role": "navigation", "bounds": [0, 0, 200, 34], "children": [
                {"role": "heading", "text": "Libraries", "bounds": [50, 5, 160, 28]}]},
            {"role": "list", "actions": ["scroll"], "bounds": [0, 40, 200, 210], "children": [
                {"role": "row", "type": "Cell", "actions": ["tap"], "bounds": [5, 45, 195, 106], "children": [
                    {"role": "text", "text": "Central", "bounds": [10, 50, 86, 66]},
                    {"role": "text", "text": "Open today", "bounds": [10, 75, 92, 93]},
                    {"role": "button", "name": "Directions", "actions": ["tap"], "bounds": [121, 46, 192, 100]}]},
                {"role": "switch", "type": "Toggle", "name": "Show closed", "actions": ["tap"],
                 "resource_id": "filter.closed", "state": {"checked": False}, "bounds": [12, 120, 186, 155]},
                {"role": "button", "name": "Restore", "actions": ["tap"], "state": {"enabled": False},
                 "bounds": [20, 164, 187, 200]},
                {"role": "text", "text": "Offscreen", "bounds": [0, 330, 180, 400]}]}]}]


@pytest.mark.parametrize("platform,prefix", [("ios", "I"), ("harmony", "H")])
def test_no_image_is_an_actionable_hierarchy_with_independent_children(platform, prefix):
    result = build_frame(specimen(), platform=platform, width=200, height=240, include_image=False)

    assert "png" not in result and not result["marked_targets"]
    controls = {entry["name"]: entry for entry in result["targets"]}
    assert set(controls) == {None, "Central", "Directions", "Show closed"}
    row = controls["Central"]
    assert (row["id"], row["point"], row["name_source"], row["source"]) == (
        f"{prefix}2", [48, 58], "system", platform)
    assert controls["Show closed"]["state_evidence"] == {"checked": "node"}
    assert controls["Show closed"]["resource_id"] == "filter.closed"
    assert {s["text"]: s["target"] for s in result["system_texts"]} == {
        "Libraries": None, "Central": row["id"], "Open today": row["id"]}
    assert all(x["source"] == "system" for x in result["system_texts"])
    structure = list(walk(result["structure"]))
    assert {x["target"] for x in structure if "target" in x} == {t["id"] for t in result["targets"]}
    row_view = next(x for x in structure if x.get("target") == row["id"])
    nested = list(walk(row_view))
    assert any(x.get("name") == "Directions" and x.get("actions") == ["tap"] for x in nested)
    assert any(x.get("text") == "Open today" and x.get("belongs_to") == row["id"] for x in nested)
    assert not any(x.get("text") == "Offscreen" for x in structure)
    disabled = next(x for x in structure if x.get("name") == "Restore")
    assert not disabled.get("actions") and disabled["state"]["enabled"] is False


def test_only_the_explicit_front_dialog_owns_actions_and_secrets_are_hidden():
    node = {"role": "window", "children": [
        {"role": "button", "name": "Behind", "actions": ["tap"], "bounds": [2, 2, 58, 30]},
        {"role": "dialog", "modal": True, "bounds": [5, 25, 95, 105], "children": [
            {"role": "input", "protected": True, "safe_label": "Password", "value": "plain secret",
             "name": "plain secret", "bounds": [8, 33, 90, 60], "actions": ["tap", "input"],
             "children": [{"role": "text", "text": "plain secret", "bounds": [9, 34, 50, 55]}]},
            {"role": "button", "text": "Done", "bounds": [35, 72, 65, 98], "actions": ["tap"]}]}]}
    result = build_frame([node], platform="harmony", width=100, height=120, include_image=False)
    assert "plain secret" not in repr(result) and "Behind" not in repr(result)
    assert result["dialogs"] == [{"kind": "dialog", "bounds": [5, 25, 95, 105], "source": "harmony"}]
    assert [t["name"] for t in result["targets"]] == ["Password", "Done"]
    assert [(r["role"], r.get("target")) for r in walk(result["structure"])] == [
        ("page", None), ("dialog", None), ("input", "H1"), ("button", "H2")]


def test_covered_controls_cannot_receive_a_tap_through_the_keyboard():
    nodes = [{"role": "button", "name": "Under keyboard", "actions": ["tap"], "bounds": [10, 89, 70, 105]},
             {"role": "button", "name": "Partly covered", "actions": ["tap"], "bounds": [2, 59, 90, 90]},
             {"role": "keyboard", "bounds": [0, 75, 110, 120], "children": [
                 {"role": "key", "name": "Delete", "actions": ["tap"], "bounds": [65, 82, 95, 107]}]}]
    result = build_frame(nodes, platform="ios", width=110, height=120, include_image=False,
                         keyboard={"visible": True, "bounds": [0, 75, 110, 120]})
    assert [t["name"] for t in result["targets"]] == ["Partly covered", "Delete"]
    assert result["targets"][0]["point"][1] < 75


def test_no_image_keeps_pixel_evidence_identical_without_base64_or_rendering(monkeypatch):
    from ai_phone.agent import android_frame_marks, android_visible_text, android_visual_regions
    region = {"id": "V1", "role": "enclosed_surface", "bounds": [55, 42, 186, 101],
              "source": "pixels", "actions": ["try_tap"], "point": [120, 71], "state": {}, "name": None}
    monkeypatch.setattr(android_visual_regions, "visual_targets", lambda *args: [region.copy()])

    def read(image, targets, excluded, labels, records):
        assert image.getpixel((0, 0)) == (255, 255, 255)
        targets[0].update(name="Plans", name_source="screen", name_bounds=[69, 60, 162, 80])
        return [{"text": "Plans", "bounds": [69, 60, 162, 80], "target": "V1"}]

    monkeypatch.setattr(android_visible_text, "enrich_from_pixels", read)
    raw = Image.new("RGB", (200, 120), "white")
    ImageDraw.Draw(raw).rectangle(region["bounds"], fill="lightblue")
    buf = io.BytesIO()
    raw.save(buf, format="PNG")
    png = buf.getvalue()
    options = dict(platform="ios", width=200, height=120, png=png, tree_available=False)
    marked = build_frame([], **options)
    assert marked["marked_targets"]
    with Image.open(io.BytesIO(base64.b64decode(marked["png"]))) as output:
        assert output.size == (200, 120) and output.getpixel((55, 42)) != raw.getpixel((55, 42))

    def unexpected(*args):
        raise AssertionError("No-image responses must not render a numbered PNG")

    monkeypatch.setattr(android_frame_marks, "annotate_frame", unexpected)
    structure_only = build_frame([], include_image=False, **options)
    assert "png" not in structure_only
    for key in ("structure", "targets", "texts", "screen_texts", "_raw_grid_v1"):
        assert structure_only[key] == marked[key]
    assert not structure_only["tree_available"] and structure_only["screen_texts_available"]
    assert structure_only["structure"]["children"][0]["name_source"] == "screen"


def test_stale_png_dimensions_rejects_mispositioned_targets():
    png = io.BytesIO()
    Image.new("RGB", (40, 80)).save(png, format="PNG")
    with pytest.raises(ValueError, match="coordinate size"):
        build_frame([], platform="ios", width=80, height=40, png=png.getvalue())


@pytest.mark.parametrize("variation,expected", [
    ("faint_panel", True), ("plain_paragraphs", False), ("different_section", False),
    ("overlapping_button", False), ("other_heading", False), ("protected", False), ("no_subtitle", False),
])
def test_unique_titled_peer_also_requires_its_own_measured_surface(variation, expected):
    from ai_phone.shared.mobile_panel_peers import recover_labelled_peers

    image = Image.new("RGB", (380, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([20, 20, 360, 104], radius=7, outline="steelblue", fill="aliceblue", width=3)
    if variation != "plain_paragraphs":
        draw.rounded_rectangle([20, 138, 360, 222], radius=7, outline="#d4d1dd", width=2)
    titles = ["Tram", "Rail"]
    details = ["Six stops", "Four stops"]
    cards, rows = [], []
    for i, y in enumerate([20, 138]):
        nodes = []
        for role, value, dy in [("heading", titles[i], 13), ("text", details[i], 42)]:
            if variation == "no_subtitle" and i and role == "text":
                continue
            label = [92, y + dy, 204, y + dy + 20]
            draw.text(label[:2], value, fill="black")
            nodes.append({"role": role, "text": value, "bounds": label})
            rows.append({"text": value, "bounds": label, "target": None if i else "V1"})
        cards.append(nodes)
    if variation == "other_heading":
        cards[1][1]["role"] = "heading"
    if variation == "different_section":
        views = [{"role": "section", "children": children} for children in cards]
    else:
        views = [{"role": "section", "children": cards[0] + cards[1]}]
    targets = [{"id": "V1", "name": titles[0], "role": "enclosed_surface", "source": "pixels",
                "bounds": [21, 21, 360, 104], "actions": ["try_tap"], "point": [120, 45],
                "system_name_source": "contained_heading"}]
    if variation == "overlapping_button":
        targets.append({"id": "I1", "bounds": [174, 176, 214, 216], "point": [190, 193], "actions": ["tap"]})
    excluded = [[50, 145, 75, 162]] if variation == "protected" else []
    assert not recover_labelled_peers(image, views, targets, rows, excluded, 512)
    additions = [t for t in targets if t["id"] == "V2"]
    assert bool(additions) == expected
    if expected:
        [target] = additions
        assert (target["name"], target["name_source"], target["state"], target["actions"]) == (
            "Rail", "system", {}, ["try_tap"])
        assert rows[-1]["target"] == rows[-2]["target"] == "V2"
    else:
        assert not any(r["target"] for r in rows if r["text"] in ("Rail", "Four stops"))


def test_a_measured_card_gets_its_one_semantic_heading_and_supporting_text(monkeypatch):
    from ai_phone.shared import mobile_frame

    box = [4, 4, 190, 110]
    name_box = [18, 18, 112, 42]
    raw = io.BytesIO()
    Image.new("RGB", (200, 140)).save(raw, format="PNG")

    def measured(png, image, observation, excluded):
        observation["targets"].append({"id": "V1", "role": "enclosed_surface", "source": "pixels",
                                       "bounds": box, "name": None, "actions": ["try_tap"],
                                       "point": [95, 50], "state": {}})
        for row in observation["system_texts"]:
            row["target"] = "V1"

    monkeypatch.setattr(mobile_frame, "_screen_evidence", measured)
    title = {"role": "heading", "text": "Bicycle", "bounds": name_box, "level": 2, "children": [
        {"role": "text", "text": "Bicycle", "bounds": [18, 18, 110, 42]}]}
    detail = {"role": "text", "text": "Covered parking", "bounds": [18, 53, 148, 80]}
    siblings = [{"role": "group", "children": [title]}, {"role": "group", "children": [detail]}]
    nodes = [{"role": "group", "name": "Travel", "children": siblings}]
    options = dict(platform="ios", png=raw.getvalue(), width=200, height=140, include_image=False)
    frame = build_frame(nodes, **options)
    [target] = frame["targets"]
    assert (target["name"], target["name_source"], target["actions"], target["state"], target["point"]) == (
        "Bicycle", "system", ["try_tap"], {}, [95, 50])
    assert target["system_name_source"] == "contained_heading" and target["name_bounds"] == name_box
    [card] = [n for n in walk(frame["structure"]) if n.get("target") == "V1"]
    assert [(n.get("text"), n.get("level"), n.get("belongs_to")) for n in card["children"]] == [
        ("Bicycle", 2, "V1"), ("Covered parking", None, "V1")]
    assert sum(n.get("text") == "Bicycle" for n in walk(frame["structure"])) == 1

    # Two independently positioned headings keep the candidate ambiguous, even with the same caption.
    siblings.append({"role": "heading", "text": "Bicycle", "bounds": [15, 80, 100, 100], "level": 2})
    ambiguous = build_frame(nodes, **options)
    assert ambiguous["targets"][0]["name"] is None
    assert sum(n.get("text") == "Bicycle" for n in walk(ambiguous["structure"])) == 2


def test_flat_repeated_buttons_are_grouped_with_their_own_text_without_inventing_actions():
    children = [{"role": "heading", "text": "Directory", "bounds": [10, 0, 70, 12]}]
    for y, title, detail in [(30, "North", "08:00"), (90, "South", "11:00"), (150, "West", "13:00")]:
        children.extend([
            {"role": "text", "text": title, "bounds": [8, y, 61, y + 16]},
            {"role": "text", "text": detail, "bounds": [8, y + 16, 68, y + 31]},
            {"role": "button", "name": "Open", "actions": ["tap"], "bounds": [120, y, 174, y + 33]}])
    tree = build_frame([{"role": "group", "children": children}], platform="ios", width=180, height=200,
                       include_image=False)["structure"]
    rows = [n for n in walk(tree) if n.get("source") == "layout"]
    assert len(rows) == 3 and not any(n.get("actions") or n.get("target") for n in rows)
    assert [[n.get("text") or n.get("target") for n in row["children"]] for row in rows] == [
        ["North", "08:00", "I1"], ["South", "11:00", "I2"], ["West", "13:00", "I3"]]
    assert sum(n.get("text") == "Directory" for n in walk(tree)) == 1


def test_existing_android_paths_and_real_text_keep_their_owners_without_duplicate_controls():
    frame = {"targets": [
        {"id": "A2", "role": "control", "name": "Next", "name_source": "system", "source": "android",
         "bounds": [2, 10, 70, 32], "tree_path": "/0/1/1/0", "actions": ["tap"]},
        {"id": "A1", "role": "scroll_area", "source": "android", "bounds": [0, 0, 90, 120],
         "tree_path": "/0/1", "actions": ["scroll"]}],
        "system_texts": [{"text": "Next", "bounds": [3, 12, 63, 28], "target": "A2"},
                         {"text": "Subheading", "bounds": [2, 34, 80, 47], "target": None}],
        "screen_texts": [{"text": "Notes", "bounds": [3, 50, 70, 71], "target": None}]}
    fallback = structure_from_observation(frame)
    scroller = next(n for n in walk(fallback) if n.get("target") == "A1")
    assert any(n.get("target") == "A2" for n in scroller["children"])
    assert {(n["text"], n["source"]) for n in walk(scroller) if "text" in n} == {
        ("Subheading", "system"), ("Notes", "screen")}
    native = [{"role": "list", "bounds": [0, 0, 90, 120], "target": "A1", "children": [
        {"role": "heading", "text": "Subheading", "source": "system", "bounds": [2, 34, 80, 47]}]}]
    entries = list(walk(structure_from_observation(frame, native_nodes=native)))
    assert entries[1]["role"] == "list" and sum(n.get("text") == "Subheading" for n in entries) == 1
    assert len([n for n in entries if n.get("target") == "A2"]) == 1
