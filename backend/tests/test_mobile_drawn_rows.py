from copy import deepcopy

import pytest
from PIL import Image, ImageDraw, ImageFont

from ai_phone.shared.mobile_drawn_rows import add_separated_rows
from ai_phone.shared.mobile_structure import structure_from_observation


def _scene(variation=None, count=2):
    paper = "#2e3643" if variation == "dark" else "white"
    ink = "#d2d9e0" if variation == "dark" else "#585858"
    image = Image.new("RGB", (460, 70 + count * 64), paper if variation == "prose" else "#c2c6cb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 30, 435, 29 + count * 64), fill=paper)
    font = ImageFont.load_default(size=20)
    wrappers, records = [], []
    for index, caption in enumerate(("Central station", "Harbor market", "River library")[:count]):
        top = 30 + index * 64
        if index and variation != "no_rule":
            draw.line((24, top, 435, top), fill="#8e949c", width=1)
        position = (54, top + 20)
        bounds = list(draw.textbbox(position, caption, font=font, anchor="lt"))
        if index == 0 and variation == "swatch":
            draw.rectangle(bounds, fill=ink)
        elif index == 0 and variation == "empty":
            pass
        elif index == 0 and variation == "tiny_icon":
            draw.ellipse((54, top + 22, 62, top + 30), fill=ink)
        else:
            draw.text(position, caption, font=font, anchor="lt", fill=ink)
        label_box = [bounds[0] - 2, bounds[1] - 2, bounds[2] + 2, bounds[3] + 2]
        leaf = {"role": "text", "source": "system", "text": caption, "bounds": label_box}
        view = {"role": "group", "bounds": [24, top, 436, top + 64], "children": [leaf]}
        wrappers.append(view)
        records.append({"text": caption, "source": "system", "target": None, "bounds": label_box})
    excluded = []
    targets = [{"id": "V7", "bounds": [0, 0, 18, 16], "actions": ["try_tap"], "source": "pixels"}]
    if variation == "extra_caption":
        wrappers[0]["children"].append({"role": "text", "text": "Extra", "source": "system", "bounds": [58, 70, 91, 81]})
    if variation == "non_adjacent":
        wrappers.insert(1, {"role": "image", "name": "Divider decoration", "bounds": [26, 88, 36, 93]})
    if variation == "two_sections":
        wrappers = [{"role": "section", "children": [node]} for node in wrappers]
    if variation == "excluded":
        excluded.append([400, 42, 428, 58])
    if variation == "existing_target":
        targets.append({"id": "I3", "bounds": [390, 43, 410, 67], "actions": ["tap"]})
    if variation == "disabled":
        wrappers[0]["state"] = {"enabled": False}
    return image, [{"role": "section", "children": wrappers}], {"targets": targets, "system_texts": records}, excluded


def _walk(node):
    yield node
    for child in node.get("children", ()):
        yield from _walk(child)


@pytest.mark.parametrize("mode", [None, "dark"])
def test_binds_real_pixels_and_system_caption_without_promoting_native_semantics(mode):
    image, views, observation, excluded = _scene(mode, count=3)
    assert not add_separated_rows(image, views, observation, excluded, 4)
    rows = observation["targets"][1:]
    assert [t["id"] for t in rows] == ["V8", "V9", "V10"]
    assert [r["target"] for r in observation["system_texts"]] == [t["id"] for t in rows]
    for target, wrapper in zip(rows, views[0]["children"]):
        assert target["role"] == "row" and target["actions"] == ["try_tap"] and target["state"] == {}
        assert target["source"] == "pixels" and target["name_source"] == "system"
        assert target["name_bounds"] == wrapper["children"][0]["bounds"]
        assert target["bounds"] == wrapper["bounds"] and wrapper["target"] == target["id"]
        x, y = target["point"]
        left, top, right, bottom = target["visual_evidence"]["glyph_bounds"]
        assert left <= x < right and top <= y < bottom and image.getpixel((x, y)) != image.getpixel((38, y))
    tree = structure_from_observation(observation, native_nodes=views)
    for target in rows:
        (control,) = [n for n in _walk(tree) if n.get("target") == target["id"]]
        assert control["actions"] == ["try_tap"]
        assert control["children"][0]["belongs_to"] == target["id"]
        assert control["children"][0]["text"] == control["name"]
    before = deepcopy((views, observation))
    assert not add_separated_rows(image, views, observation, excluded, 4)
    assert (views, observation) == before


@pytest.mark.parametrize("variation", ["empty", "swatch", "tiny_icon", "prose", "no_rule", "extra_caption",
                                       "non_adjacent", "two_sections", "excluded", "existing_target", "disabled"])
def test_independent_structure_paint_and_obstruction_evidence(variation):
    image, views, observation, excluded = _scene(variation)
    before = deepcopy((views, observation))
    assert not add_separated_rows(image, views, observation, excluded, 512)
    assert (views, observation) == before


def test_limit_keeps_existing_ids_and_marks_only_materialized_rows():
    image, views, observation, excluded = _scene()
    assert add_separated_rows(image, views, observation, excluded, 2)
    assert [t["id"] for t in observation["targets"]] == ["V7", "V8"]
    assert [r["target"] for r in observation["system_texts"]] == ["V8", None]
