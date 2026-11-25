from copy import deepcopy
from types import ModuleType
import sys

from PIL import Image, ImageDraw
from ai_phone.shared.mobile_card_labels import add_printed_card_titles, drop_fully_owned_wrappers
from ai_phone.shared.mobile_frame import build_frame


def roots():
    return [{"role": "group", "actions": ["tap"], "bounds": [30, 45, 108, 95], "children": [
        {"role": "group", "actions": ["tap"], "bounds": [30, 45, 108, 95], "children": [
            {"role": "image", "name": "resource-xyz", "bounds": [30, 45, 108, 95]}]},
        {"role": "text", "text": "Beta", "bounds": [101, 40, 113, 53]},
    ]}]


def attach(monkeypatch, nodes=None, row=None, more_controls=None, image=None):
    from ai_phone.shared.mobile_frame import _NativeTree
    tree = _NativeTree("harmony", (180, 230), {})
    views = []
    for i, node in enumerate(nodes if nodes is not None else roots()):
        views += tree.visit(node, f"/{i}")
    frame = {"width": 180, "height": 230, "targets": tree.targets, "system_texts": list(tree.rows.values()), "screen_texts": []}
    if more_controls:
        frame["targets"] += more_controls
    drop_fully_owned_wrappers(views, frame)
    reading = {"text": "Holiday pass", "bounds": [35, 50, 97, 65], "source": "screen", "method": "glyph_templates"}
    reader = ModuleType("ai_phone.shared.mobile_card_text")
    reader.read_card_titles = lambda *_: [deepcopy(reading)] if row is None else deepcopy(row)
    monkeypatch.setitem(sys.modules, "ai_phone.shared.mobile_card_text", reader)
    add_printed_card_titles(image or Image.new("RGB", (180, 230), "white"), views, frame, [])
    return frame, views


def test_coincident_inert_container_keeps_its_badge_and_retires_its_empty_number():
    result = build_frame(roots(), platform="harmony", width=180, height=230)
    assert [t["id"] for t in result["targets"]] == ["H2"]
    assert result["system_texts"] == [{"text": "Beta", "bounds": [101, 40, 113, 53], "source": "system", "target": None}]
    assert any(child.get("text") == "Beta" for child in result["structure"]["children"][0]["children"])


def test_subordinate_image_hint_and_printed_title_both_survive(monkeypatch):
    result, views = attach(monkeypatch)
    [target] = result["targets"]
    assert (target["name"], target["name_source"], target["source"], target["system_hint"]) == (
        "Holiday pass", "screen", "harmony", "resource-xyz")
    assert target["actions"] == ["tap"] and target["name_bounds"] == [35, 50, 97, 65]
    assert result["screen_texts"] == [{"text": "Holiday pass", "bounds": [35, 50, 97, 65], "source": "screen", "target": "H2"}]
    assert views[0]["children"][0]["children"][0]["name"] == "resource-xyz"


def test_own_native_button_label_is_authoritative(monkeypatch):
    nodes = roots()
    card = nodes[0]["children"][0]
    card.update(role="button", name="Regional tickets")
    result, _ = attach(monkeypatch, nodes)
    [target] = result["targets"]
    assert (target["name"],target["name_source"]) == ("Regional tickets","system")
    assert result["screen_texts"][0]["target"] == target["id"]
    assert "system_hint" not in target


def test_identical_actionable_siblings_or_independent_subbuttons_are_not_merged(monkeypatch):
    tree = [roots()[0]["children"][0]]
    result, _ = attach(monkeypatch,tree + deepcopy(tree))
    assert len(result["targets"]) == 2 and result["screen_texts"][0]["target"] is None
    tree[0]["children"].append({"role":"button","name":"Remove","actions":["tap"],"bounds":[70,46,105,66]})
    result, _ = attach(monkeypatch,tree)
    assert len(result["targets"]) == 2 and result["targets"][1]["name"] == "Remove" and result["screen_texts"][0]["target"] is None


def test_intersecting_independent_sibling_keeps_its_own_ink(monkeypatch):
    nodes=roots() + [{"role":"button","name":"Star","actions":["tap"],"bounds":[70,46,105,66]}]
    result, _ = attach(monkeypatch,nodes)
    assert len(result["targets"]) == 2 and result["targets"][1]["name"] == "Star" and result["screen_texts"][0]["target"] is None


def test_extra_copy_in_one_image_never_has_its_first_line_chosen_as_title(monkeypatch):
    rows = [{"text":"Northbound","bounds":[35,49,64,60]}, {"text":"Gate five","bounds":[36,74,78,83]}]
    result, _ = attach(monkeypatch,row=rows)
    assert result["targets"][0]["name"] == "resource-xyz"
    assert [r["text"] for r in result["screen_texts"]] == ["Northbound","Gate five"]


def test_a_fully_covered_parent_keeps_a_different_native_gesture():
    nodes=roots()
    nodes[0]["actions"]=["tap","scroll"]
    result=build_frame(nodes,platform="ios",width=180,height=230)
    assert [t["actions"] for t in result["targets"]] == [["scroll"],["tap"]]

def test_only_the_image_whose_edges_are_painted_owns_text_in_overlapping_images(monkeypatch):
    cards = [roots()[0]["children"][0]] + [
        {"role": "group", "bounds": [0, 32, 141, 209], "actions": ["tap"], "children": [
            {"role": "image", "name": "background-asset", "bounds": [0, 32, 141, 209]}]}]
    image = Image.new("RGB", (180, 230), "white")
    ImageDraw.Draw(image).rounded_rectangle([30, 45, 107, 94], radius=4, fill="#0759b1")
    frame, _ = attach(monkeypatch, cards, image=image)
    assert [(t["name"], t["name_source"]) for t in frame["targets"]] == [
        ("Holiday pass", "screen"), ("background-asset", "system")]
    assert frame["screen_texts"][0]["target"] == "H1"


def test_two_painted_independent_image_edges_remain_ambiguous(monkeypatch):
    cards = [roots()[0]["children"][0]] + [
        {"role": "group", "bounds": [16, 32, 141, 175], "actions": ["tap"], "children": [
            {"role": "image", "name": "other-image", "bounds": [16, 32, 141, 175]}]}]
    image = Image.new("RGB", (180, 230), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([16, 32, 140, 174], fill="#f9ca62")
    draw.rectangle([30, 45, 107, 94], fill="#0759b1")
    frame, _ = attach(monkeypatch, cards, image=image)
    assert frame["screen_texts"][0]["text"] == "Holiday pass" and frame["screen_texts"][0]["target"] is None
    assert all(t["name_source"] == "system" for t in frame["targets"])


def test_optional_image_reader_failure_preserves_native_targets_and_reports_the_gap(monkeypatch):
    from ai_phone.shared.mobile_frame import _NativeTree
    tree = _NativeTree("harmony", (180, 230), {})
    views = tree.visit(roots()[0], "/0")
    frame = {"width": 180, "height": 230, "targets": tree.targets, "screen_texts": [], "warnings": []}
    reader = ModuleType("ai_phone.shared.mobile_card_text")

    def fail(*args):
        raise OSError("font index unavailable")

    reader.read_card_titles = fail
    monkeypatch.setitem(sys.modules, "ai_phone.shared.mobile_card_text", reader)
    before = deepcopy(frame["targets"])
    add_printed_card_titles(Image.new("RGB", (180, 230)), views, frame, [])
    assert frame["targets"] == before and frame["warnings"] == ["printed_card_text_unavailable"]

def test_native_card_can_have_a_small_inset_around_its_sole_printed_image(monkeypatch):
    nodes = roots()
    card = nodes[0]["children"][0]
    card["children"][0]["bounds"] = [32, 47, 106, 93]
    frame, _ = attach(monkeypatch,nodes)
    assert frame["targets"][0]["name"] == "Holiday pass"
    assert frame["targets"][0]["system_hint"] == "resource-xyz"
    assert frame["screen_texts"][0]["target"] == frame["targets"][0]["id"]


def test_an_inset_thumbnail_cannot_donate_its_text_as_page_action_name(monkeypatch):
    nodes = roots()
    card = nodes[0]["children"][0]
    card["children"][0]["bounds"] = [31, 46, 69, 80]
    frame, _ = attach(monkeypatch,nodes)
    assert frame["targets"][0]["name"] == "resource-xyz"
    assert frame["screen_texts"][0]["target"] is None
    assert frame["screen_texts"][0]["source"] == "screen"


def test_printed_label_outside_an_inset_image_is_readable_without_an_action(monkeypatch):
    nodes = roots()
    card = nodes[0]["children"][0]
    card["children"][0]["bounds"] = [32, 47, 106, 93]
    frame, _ = attach(monkeypatch,nodes,row=[{"text":"Badge", "bounds":[31,45,71,59]}])
    assert frame["targets"][0]["name"] == "resource-xyz"
    assert frame["screen_texts"][0]["target"] is None
