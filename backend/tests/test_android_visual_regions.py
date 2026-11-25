import io

from PIL import Image, ImageDraw

from ai_phone.agent.android_visual_regions import visual_targets


def png(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_repeated_icons_fill_only_missing_slots_and_stay_unnamed():
    image = Image.new("RGB", (432, 330), "white")
    draw = ImageDraw.Draw(image)
    for y in (70, 165):
        for x in (65, 165, 265, 365):
            draw.rounded_rectangle((x - 11, y - 11, x + 11, y + 11), 5, fill="#529ada")
            for offset in (-16, -8, 0, 8):
                draw.rectangle((x + offset, y + 22, x + offset + 6, y + 31), fill="#243039")
    raw = png(image)
    initial = visual_targets(raw, 432, 330, [])
    assert len(initial) == 8
    assert all(item["role"] == "grid_item" for item in initial)
    known = [{"actions": ["tap"], "name": None, "bounds": item["bounds"]} for item in initial[:5]]
    known.append({"actions": ["tap"], "bounds": [0, 0, 432, 330]})
    remaining = visual_targets(raw, 432, 330, known)
    assert [item["bounds"] for item in remaining] == [item["bounds"] for item in initial[5:]]
    assert [item["id"] for item in remaining] == ["V1", "V2", "V3"]
    assert all(item["source"] == "pixels" and item["name"] is None
               and item["actions"] == ["try_tap"] and item["state"] == {} for item in remaining)


def test_large_card_is_first_in_output_and_scales_to_the_device():
    image = Image.new("RGB", (432, 215), "#3682de")
    draw = ImageDraw.Draw(image)
    for left, top, right, bottom in [(90, 45, 263, 105), (282, 45, 380, 105), (90, 138, 180, 200)]:
        draw.rounded_rectangle((left, top, right, bottom), 7, fill="#f8fbff")
        draw.rectangle((left + 12, top + 12, left + 54, top + 16), fill="#222222")
        draw.rectangle((left + 12, top + 21, left + 28, top + 24), fill="#7b8291")
    raw = png(image)
    known = [{"actions": ["scroll"], "bounds": [0, 0, 864, 430]}]
    items = visual_targets(raw, 864, 430, known)
    assert len(items) == 3
    x, y = items[0]["point"]
    assert items[0]["id"] == "V1" and items[0]["role"] == "enclosed_surface"
    assert 180 < x < 526 and 90 < y < 210
    assert all(0 <= i["bounds"][0] < i["bounds"][2] <= 864 for i in items)


def test_solid_placeholders_dont_become_buttons_even_in_a_grid():
    image = Image.new("RGB", (432, 330), "white")
    draw = ImageDraw.Draw(image)
    for x in (65, 165, 265, 365):
        draw.rounded_rectangle((x - 11, 45, x + 11, 67), 3, fill="#c9cbd0")
        draw.rounded_rectangle((x - 20, 82, x + 20, 94), 3, fill="#c9cbd0")
    for y in (135, 190, 245):
        draw.rounded_rectangle((25, y, 250, y + 29), 5, fill="#e8e8e8")
    assert visual_targets(png(image), 432, 330, []) == []


def test_bad_image_or_inconsistent_orientation_has_no_coordinates():
    assert visual_targets(b"not an image", 100, 200, []) == []
    raw = png(Image.new("RGB", (100, 200)))
    assert visual_targets(raw, 200, 100, []) == []


def test_skeleton_requires_repeated_faint_bars_and_preserves_unnamed_controls():
    from ai_phone.agent.android_visual_regions import is_placeholder

    image = Image.new("RGB", (380, 260), "#f5f4f8")
    draw = ImageDraw.Draw(image)
    bars = [(20, y, 150, y + 24) for y in (24, 64, 104, 144)]
    for left, top, right, bottom in bars:
        draw.rounded_rectangle((left, top, right - 1, bottom - 1), 5, fill="#e7e7ef")
    draw.rounded_rectangle((188, 24, 211, 47), 4, fill="#e7e7ef")
    draw.rounded_rectangle((184, 74, 343, 108), 5, fill="white")
    draw.polygon([(194, 81), (205, 91), (194, 101)], fill="#40404a")
    draw.rounded_rectangle((184, 125, 343, 160), 5, fill="#55a9e1")

    assert all(is_placeholder(image, b) for b in bars)
    assert not is_placeholder(image, (188, 24, 212, 48))  # An opaque, unnamed square icon.
    assert not is_placeholder(image, (184, 74, 344, 109))  # A grey arrow inside a pale control.
    assert not is_placeholder(image, (184, 125, 344, 161))
    assert not is_placeholder(image.crop((0, 0, 165, 55)), bars[0])


def test_loading_filter_preserves_an_actionable_visual_row_with_an_icon(monkeypatch):
    from ai_phone.agent import android_visual_regions as regions

    is_placeholder = regions.is_placeholder

    image = Image.new("RGB", (432, 240), "#f5f4f8")
    draw = ImageDraw.Draw(image)
    for y in (15, 65, 115):
        draw.rounded_rectangle((15, y, 140, y + 26), 6, fill="#e7e7ef")
    draw.rounded_rectangle((180, 98, 400, 165), 7, fill="#f9f9fc")
    draw.ellipse((368, 119, 386, 138), fill="#f8cd37")
    draw.rounded_rectangle((181, 173, 199, 178), 2, fill="#e7e7ef")
    draw.rounded_rectangle((180, 190, 400, 224), 5, fill="white")
    draw.line((195, 191, 208, 191), fill="#343844", width=2)
    assert not is_placeholder(image, (181, 173, 200, 179))
    assert not is_placeholder(image, (180, 190, 401, 225))
    bounds = (180, 98, 401, 166)
    assert not is_placeholder(image, bounds)

    # Drive the public list path with both proposals already located. The
    # suppressed bar must not leave a gap in the formal IDs of real controls.
    monkeypatch.setattr(regions, "_repeated_items", lambda _ink: ())
    proposals = [("enclosed_surface", regions._Box(15, 15, 141, 42)),
                 ("enclosed_surface", regions._Box(*bounds))]
    monkeypatch.setattr(regions, "_surfaces", lambda _grad, _ink: proposals)
    visible = visual_targets(png(image), *image.size, [])
    assert [(target["id"], target["bounds"], target["name"]) for target in visible] == [
        ("V1", list(bounds), None),
    ]


def test_android_placeholder_ids_require_anonymous_taps_at_an_actual_family_member(monkeypatch):
    from ai_phone.agent import android_visual_regions as regions

    image = Image.new("RGB", (420, 310), "#f5f4f8")
    draw = ImageDraw.Draw(image)
    bars = [(18, y, 164, y + 24) for y in (20, 63, 106, 149, 192, 235)]
    separate = (225, 89, 371, 113)
    for x, y, r, b in [*bars, separate]:
        draw.rounded_rectangle((x, y, r - 1, b - 1), 6, fill="#e7e7ef")
    draw.text((40, 198), "OK", fill="#707075")

    def target(index, bounds, **changes):
        return {"id": f"A{index}", "source": "android", "bounds": bounds,
                "name": None, "actions": ["tap"], **changes}

    targets = [
        target(1, bars[0]), target(2, bars[1], name="继续"),
        target(3, bars[2]), target(4, bars[3], system_hint="menu_icon"),
        target(5, bars[4]), target(6, bars[5], role="toggle"),
        target(7, separate), target(8, bars[0], source="pixels"),
        target(9, bars[1], actions=["tap", "input"]), target(10, bars[5], state={"selected": True}),
        target(11, [-1, 0, 10, 15]),
    ]
    raw = png(image)
    calls = []
    original = regions._loading_families

    def count_families(reduced, gradient):
        calls.append(reduced.size)
        return original(reduced, gradient)

    monkeypatch.setattr(regions, "_loading_families", count_families)
    assert regions.system_placeholders(raw, targets) == {"A1", "A3"}
    assert calls == [image.size]
    assert regions.system_placeholders(b"invalid PNG", targets) == set()
    assert regions.system_placeholders(raw, [target(12, bars[0], name="确认")]) == set()
    assert calls == [image.size]


def test_single_native_pixel_mark_survives_downsampling_to_the_detection_grid():
    from ai_phone.agent.android_visual_regions import system_placeholders

    image = Image.new("RGB", (1296, 780), "#f5f4f8")
    draw = ImageDraw.Draw(image)
    bars = [(90, y, 666, y + 84) for y in (60, 187, 314, 441, 568)]
    for x, y, r, b in bars:
        draw.rounded_rectangle((x, y, r - 1, b - 1), radius=15, fill="#e7e7ef")
    x, y, _, _ = bars[2]
    draw.point((x + 31, y + 30), fill=(220, 220, 232))
    items = [{"id": f"A{i}", "source": "android", "actions": ["tap"], "bounds": b} for i, b in enumerate(bars)]
    assert system_placeholders(png(image), items) == {"A0", "A1", "A3", "A4"}


def test_an_isolated_pale_android_button_keeps_its_action():
    from ai_phone.agent.android_visual_regions import system_placeholders

    image = Image.new("RGB", (432, 240), "#f5f4f8")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((58, 60, 305, 92), radius=5, fill="#e7e7ef")
    candidate = {"id": "A1", "source": "android", "actions": ["tap"], "bounds": [58, 60, 306, 93]}
    assert system_placeholders(png(image), [candidate]) == set()


def test_inked_low_contrast_options_are_grouped_without_duplicating_a_known_or_accented_target():
    image = Image.new("RGB", (420, 360), "white")
    draw = ImageDraw.Draw(image)
    for index, y in enumerate((80, 132, 184, 236)):
        fill = "#ffd766" if index == 2 else "#f7f7f9"
        draw.rounded_rectangle((22, y, 389, y + 34), radius=6, fill=fill)
        draw.text((185, y + 9), str(index + 1), fill="#25252c")
    known = [{"actions": ["tap"], "bounds": [22, 80, 390, 115]}]
    results = visual_targets(png(image), *image.size, known)
    assert sorted(item["point"][1] for item in results) == [149, 201, 253]
    assert all(item["name"] is None and item["actions"] == ["try_tap"] for item in results)


def test_only_ring_with_four_interior_diagonals_is_offered_and_known_icon_is_not_duplicated():
    for background, ink in (("white", "#202023"), ("#202023", "white")):
        image = Image.new("RGB", (420, 200), background)
        draw = ImageDraw.Draw(image)
        for cx, shape in ((34, "cross"), (104, "plus"), (174, "ring"), (244, "bare_cross"), (314, "wide_rim")):
            if shape != "bare_cross":
                draw.ellipse((cx - 14, 16, cx + 14, 44), outline=ink, width=6 if shape == "wide_rim" else 2)
            if shape in {"cross", "bare_cross", "wide_rim"}:
                draw.line((cx - 5, 25, cx + 5, 35), fill=ink, width=2)
                draw.line((cx - 5, 35, cx + 5, 25), fill=ink, width=2)
            if shape == "plus":
                draw.line((cx - 6, 30, cx + 6, 30), fill=ink, width=2)
                draw.line((cx, 24, cx, 36), fill=ink, width=2)
        raw = png(image)
        icons = [t for t in visual_targets(raw, *image.size, []) if t["role"] == "icon"]
        assert len(icons) == 1 and icons[0]["point"] == [34, 30]
        assert icons[0]["name"] is None and icons[0]["actions"] == ["try_tap"]
        assert visual_targets(raw, *image.size, [{"actions": ["tap"], "bounds": [12, 10, 56, 50]}]) == []


def test_independent_cards_and_regular_blank_placeholders_do_not_acquire_group_actions():
    image = Image.new("RGB", (420, 360), "white")
    draw = ImageDraw.Draw(image)
    for y in (30, 82, 134, 186):
        draw.rounded_rectangle((22, y, 190, y + 30), radius=5, fill="#f7f7f9")
    for x, y, w, h in ((204, 27, 165, 25), (230, 107, 128, 39), (198, 226, 206, 26)):
        draw.rounded_rectangle((x, y, x + w, y + h), radius=4, fill="#f7f7f9")
        draw.line((x + 12, y + 10, x + 40, y + 10), fill="#303038", width=2)
    assert not any(t["role"] in {"repeated_surface", "icon"} for t in visual_targets(png(image), *image.size, []))
