import pytest

from ai_phone.shared.mobile_frame import build_frame


@pytest.mark.parametrize("role", ["web", "webview", "document", "form", "page", "group"])
def test_page_handler_keeps_its_action_and_does_not_borrow_the_instruction_or_icon(role):
    nodes = [{"role": role, "actions": ["tap"], "bounds": [0, 0, 300, 420], "point": [150, 16],
              "children": [{"role": "image", "name": "mail-outline", "bounds": [27, 175, 49, 197]},
                           {"role": "paragraph", "text": "Choose a destination", "bounds": [20, 240, 280, 259]},
                           {"role": "input", "name": "Recipient", "actions": ["tap"], "bounds": [55, 165, 230, 201]}]}]
    frame = build_frame(nodes, platform="harmony", width=300, height=420)
    outer, field = frame["targets"]
    assert (outer["name"], outer["name_source"], outer["actions"], outer["point"]) == (None, None, ["tap"], [150, 16])
    assert "name_bounds" not in outer and field["name"] == "Recipient" and field["name_source"] == "system"
    assert frame["system_texts"] == [
        {"source": "system", "text": "Choose a destination", "bounds": [20, 240, 280, 259], "target": None}]


def test_a_page_keeps_its_own_direct_name_and_a_large_real_button_keeps_its_child_label():
    nodes = [{"role": "web", "name": "Map", "actions": ["tap"], "bounds": [0, 0, 240, 300], "children": [
        {"role": "text", "text": "Minutes remaining", "bounds": [1, 6, 70, 22]},
        {"role": "button", "actions": ["tap"], "bounds": [2, 48, 239, 296], "children": [
            {"role": "text", "text": "Navigate", "bounds": [82, 144, 154, 175]}]}]}]
    frame = build_frame(nodes, platform="harmony", width=240, height=300)
    assert [(t["name"], t["name_source"]) for t in frame["targets"]] == [("Map", "system"), ("Navigate", "system")]
    assert [(r["text"], r["target"]) for r in frame["system_texts"]] == [("Minutes remaining", None), ("Navigate", "H2")]


def test_an_opaque_geometry_hint_from_a_single_small_icon_remains_a_tap_point():
    nodes = [{"role": "group", "actions": ["tap"], "bounds": [0, 0, 210, 300], "children": [
        {"role": "image", "name": "asset", "bounds": [182, 110, 210, 140]}]}]
    [surface] = build_frame(nodes, platform="harmony", width=210, height=300)["targets"]
    assert surface["name"] is None and surface["point"] == [196, 125] and surface["actions"] == ["tap"]


def test_card_title_is_still_owned_by_the_card_and_body_is_scoped_to_it():
    nodes = [{"role": "document", "bounds": [0, 0, 430, 640], "children": [
        {"role": "group", "bounds": [20, 85, 410, 148], "actions": ["tap"], "children": [
            {"role": "heading", "text": "Day pass", "bounds": [40, 92, 136, 112]},
            {"role": "text", "text": "Valid until midnight", "bounds": [40, 119, 219, 143]}]}]}]
    frame = build_frame(nodes, platform="ios", width=430, height=640)
    [item] = frame["targets"]
    assert item["name"] == "Day pass" and item["name_bounds"] == [40, 92, 136, 112]
    assert all(row["target"] == item["id"] for row in frame["system_texts"])
