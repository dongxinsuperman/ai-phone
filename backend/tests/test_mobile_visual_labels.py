import pytest
from PIL import Image, ImageDraw

from ai_phone.shared.mobile_frame import build_frame
from ai_phone.shared.mobile_visual_labels import hide_unpainted_static_text, prefer_label_in_existing_target


def test_only_unowned_static_text_with_no_drawn_pixels_is_removed():
    image = Image.new("RGB", (160, 130), "white")
    ImageDraw.Draw(image).text((6, 5), "Visible", fill="#fefefe")
    visible, hidden, button = [3, 3, 88, 21], [5, 40, 90, 57], [5, 80, 90, 100]
    views = [{"role": "text", "text": "Visible", "bounds": visible},
             {"role": "text", "text": "Behind", "bounds": hidden},
             {"role": "button", "target": "I1", "name": "Open", "bounds": button, "children": [
                 {"role": "text", "text": "Open", "bounds": button}]}]
    observation = {"system_texts": [{"text": s, "bounds": box, "target": ref}
                                    for s, box, ref in [("Visible", visible, None), ("Behind", hidden, None),
                                                        ("Open", button, "I1")]]}
    hide_unpainted_static_text(image, views, observation)
    assert [r["text"] for r in observation["system_texts"]] == ["Visible", "Open"]
    assert "text" not in views[1] and views[-1]["name"] == "Open" and views[-1]["children"][0]["text"] == "Open"


@pytest.mark.parametrize("variant,rename", [
    ("misread", True), ("exact", True), ("background_image", False), ("generic_label", False),
    ("two_readings", False), ("two_images", False), ("disabled", False),
])
def test_existing_pixel_word_prefers_a_single_colocated_system_image_name(variant, rename):
    name, word = "Travel", "Trovel" if variant != "exact" else "Travel"
    host = [5, 12, 170, 60] if variant != "background_image" else [0, 0, 500, 800]
    box = [55, 21, 154, 47]
    images = [{"role": "image", "name": name if variant != "generic_label" else "Route map",
               "name_source": "system", "bounds": host}]
    if variant == "two_images":
        images.append({**images[0], "bounds": [6, 13, 169, 58]})
    if variant == "disabled":
        images[0]["state"] = {"enabled": False}
    target = {"id": "T1", "role": "visible_text", "name": word, "name_source": "screen", "source": "pixels",
              "actions": ["try_tap"], "point": [97, 36], "bounds": box, "state": {}}
    targets = [target]
    if variant == "two_readings":
        targets.append({**target, "id": "T2", "name": "More", "bounds": [34, 43, 120, 59]})
    frame = {"targets": targets, "system_texts": [], "screen_texts": [
        {"text": t["name"], "bounds": t["bounds"], "target": t["id"], "source": "screen"} for t in targets]}
    prefer_label_in_existing_target(images, frame)
    assert target["actions"] == ["try_tap"] and target["source"] == "pixels" and target["state"] == {}
    assert target["bounds"] == box and target["point"] == [97, 36]
    if rename:
        assert target["name"] == name and target["name_bounds"] == host and target["name_source"] == "system"
        assert frame["screen_texts"] == [] and frame["system_texts"] == [
            {"text": name, "bounds": host, "target": "T1", "source": "system"}]
    else:
        assert target["name_source"] == "screen" and frame["system_texts"] == []


def test_keyboard_accessory_occludes_the_app_but_its_own_toolbar_and_keys_survive():
    nodes = [{"role": "text", "text": "Shown", "bounds": [4, 4, 104, 26]},
             {"role": "text", "text": "Covered", "bounds": [4, 78, 104, 92]},
             {"role": "group", "input_overlay": True, "bounds": [0, 66, 140, 150], "children": [
                 {"role": "button", "name": "Done", "bounds": [60, 71, 105, 90], "actions": ["tap"]},
                 {"role": "keyboard", "bounds": [11, 112, 127, 150], "children": [
                     {"role": "key", "name": "Return", "bounds": [80, 115, 126, 148], "actions": ["tap"]}]}]}]
    frame = build_frame(nodes, platform="ios", width=140, height=150, include_image=False,
                        keyboard={"visible": True, "bounds": [11, 112, 127, 150], "occlusion_bounds": [0, 66, 140, 150]})
    assert frame["texts"] == ["Shown"]
    assert [t["name"] for t in frame["targets"]] == ["Done", "Return"]


def test_a_partial_pixel_reading_of_the_same_positioned_paragraph_does_not_gain_a_tap():
    sentence = "出发前，确认班次后再购票。"
    native = {"role": "text", "text": sentence, "bounds": [10, 10, 280, 124]}
    partial = {"id": "T1", "role": "visible_text", "actions": ["try_tap"], "source": "pixels",
               "name_source": "screen", "name": "出发前,", "bounds": [15, 22, 118, 46]}
    elsewhere = {**partial, "id": "T2", "bounds": [88, 144, 195, 168]}
    different = {**partial, "id": "T3", "name": "路线图", "bounds": [41, 66, 122, 92]}
    targets = [partial, elsewhere, different]
    frame = {"targets": targets, "system_texts": [{"text": sentence, "bounds": native["bounds"], "target": None}],
             "screen_texts": [{"text": t["name"], "bounds": t["bounds"], "target": t["id"]} for t in targets]}
    prefer_label_in_existing_target([native], frame)
    assert [t["id"] for t in frame["targets"]] == [r["target"] for r in frame["screen_texts"]] == ["T2", "T3"]
    assert frame["system_texts"][0]["text"] == sentence


@pytest.mark.parametrize("extra,keeps_short", [(False, True), (True, False)])
def test_two_independent_long_readings_anchor_a_short_fragment_of_the_same_passage(extra, keeps_short):
    sentence = "City bus schedules change on the weekend."
    native = {"role": "text", "text": sentence, "bounds": [0, 0, 320, 100]}
    words = ["on", "schedules"] + (["weekend"] if extra else [])
    targets = [{"id": f"T{i}", "role": "visible_text", "actions": ["try_tap"], "source": "pixels",
                "name_source": "screen", "name": word, "bounds": [5 + 60*i, 5, 50 + 60*i, 29]}
               for i, word in enumerate(words, 1)]
    frame = {"targets": targets, "system_texts": [{"text": sentence, "bounds": native["bounds"], "target": None}],
             "screen_texts": [{"text": t["name"], "bounds": t["bounds"], "target": t["id"]} for t in targets]}
    prefer_label_in_existing_target([native], frame)
    assert [t["name"] for t in frame["targets"]] == (["on"] if keeps_short else [])
