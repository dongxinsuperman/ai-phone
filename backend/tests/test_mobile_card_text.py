import pytest
from PIL import Image, ImageDraw, ImageFont

from ai_phone.shared.mobile_card_text import _styles, read_card_titles


def test_opposite_ink_on_gradient_cards_requires_glyphs_and_respects_protected_bounds():
    harmony = next(((path, face) for path, face, weight in _styles() if weight == 800), None)
    if harmony is None:
        pytest.skip("this regression uses the optional HarmonyOS Sans SC SDK typeface")
    path, face = harmony
    font = ImageFont.truetype(str(path), 29, index=face, layout_engine=ImageFont.Layout.BASIC)
    font.set_variation_by_axes([800])
    image = Image.new("RGB", (420, 420), "#b2c0cb")
    draw = ImageDraw.Draw(image)
    targets, captions = [], ["森林巡护", "河岸路线", None]
    for index, caption in enumerate(captions):
        top = 15 + index * 130
        bounds = [20, top, 396, top + 98]
        for offset in range(98):
            tone = offset * 32 // 98
            colour = (
                (225 - tone, 248 - tone, 196 + tone) if index != 1 else (38 + tone, 82 - tone, 136 + tone)
            )
            draw.line((20, top + offset, 395, top + offset), fill=colour)
        ink = (43, 55, 48) if index != 1 else (251, 245, 252)
        if caption:
            draw.text((42, top + 30), caption, font=font, fill=ink, anchor="lt")
        else:
            for left in range(42, 170, 30):
                draw.ellipse((left, top + 30, left + 23, top + 53), outline=ink, width=4)
                draw.rectangle((left + 6, top + 38, left + 15, top + 46), fill=ink)
        targets.append({"id": str(index), "bounds": bounds, "point": [200, top + 40], "actions": ["tap"]})

    rows = read_card_titles(image, targets)
    assert [row["text"] for row in rows] == captions[:2]
    for row, target in zip(rows, targets):
        x, y, right, bottom = row["bounds"]
        left, top, limit, end = target["bounds"]
        assert left < x < right < limit and top < y < bottom < end
        assert row["source"] == "screen" and row["method"] == "glyph_templates"
        assert "target" not in row

    assert [row["text"] for row in read_card_titles(image, targets, [targets[0]["bounds"]])] == [captions[1]]
    targets[0]["actions"].append("input")
    targets[1]["state"] = {"password": True}
    assert read_card_titles(image, targets) == []


def test_empty_or_non_actionable_subject_requires_no_font_or_pixel_scan(monkeypatch):
    def unexpected_load():
        pytest.fail("there is no actionable rectangle to read")

    monkeypatch.setattr("ai_phone.shared.mobile_card_text._styles", unexpected_load)
    image = Image.new("RGB", (240, 320))
    assert read_card_titles(image, []) == []
    target = {"bounds": [10, 20, 230, 100], "point": [120, 60], "actions": []}
    assert read_card_titles(image, [target]) == []


def test_shared_interior_cannot_hide_extra_stroke_tips(monkeypatch):
    import numpy as np

    from ai_phone.shared import mobile_card_text as card

    ink = np.zeros((26, 24), dtype=bool)
    ink[4:8, :] = ink[11:14, 3:21] = ink[19:22, 3:21] = True
    ink[8:26, 10:14] = True
    ink[:4, 3:6] = ink[:4, 10:14] = ink[:4, 18:21] = True
    same_interior = ink.copy()
    same_interior[:4, :10] = same_interior[:4, 14:] = False
    same_strokes = ink.copy()
    same_strokes[11:14, 3:21] = False
    same_strokes[13:16, 3:21] = True
    query = card._centered(ink)
    wrong, correct = card._centered(same_interior), card._centered(same_strokes)
    assert query @ wrong > query @ correct

    def letter(code, pitch):
        shape = correct if code == ord("甲") else wrong
        variants = np.repeat(shape[None, :], 6, axis=0)
        metrics = np.tile((np.log(24 / 26), 1, 24 / 26), (6, 1))
        return variants, metrics, card._edge_runs(variants)

    monkeypatch.setattr(card, "_letter", letter)
    card._match_raster.cache_clear()
    try:
        result = card._match_raster(card.glyphs._vector(ink).tobytes(), 24, 26, 26, (ord("甲"), ord("乙")))
        assert result.character == "甲"
    finally:
        card._match_raster.cache_clear()


def test_adjacent_sparse_glyphs_need_baseline_width_and_individual_strokes():
    import numpy as np

    from ai_phone.shared.mobile_card_edges import complete_caption

    styles = _styles()
    style = next((s for s in styles if s[2] == 800), None)
    if style is None:
        pytest.skip("this raster comparison uses the optional HarmonyOS Sans SC SDK typeface")
    font = ImageFont.truetype(str(style[0]), 42, index=style[1], layout_engine=ImageFont.Layout.BASIC)
    font.set_variation_by_axes([800])
    body, anchor = "城市规划", (102, 80)
    mask = Image.new("L", (420, 128))
    ImageDraw.Draw(mask).text(anchor, body, font=font, fill=255, anchor="ls")
    ink = np.asarray(mask) > 120
    yy, xx = np.nonzero(ink)
    x, y, right, bottom = xx.min(), yy.min(), xx.max() + 1, yy.max() + 1
    bounds, owner = (x, y, right, bottom), (8, 8, 410, 118)

    for prefix in ("一", "二", "三", "=", "—", "－", "", None):
        image = Image.new("RGB", mask.size, "#f5e3f5")
        draw = ImageDraw.Draw(image)
        draw.text(anchor, body, font=font, fill="#303246", anchor="ls")
        if prefix is None:
            draw.ellipse((64, 50, 98, 76), fill="#303246")
        elif prefix:
            start = anchor[0] - font.getlength(prefix)
            draw.text((start, anchor[1]), prefix, font=font, fill="#303246", anchor="ls")
        pixels = np.asarray(image, dtype=np.int16)
        seed = ink[y:bottom, x:right]
        result = complete_caption(pixels, owner, bounds, seed, body, 42, [], styles)
        printed = result[0] if result else None
        if prefix in ("一", "二", "三", "=", ""):
            assert printed == prefix + body
        else:
            assert printed is None
        if prefix == "二":
            assert result[1][0] < x
            assert (
                complete_caption(pixels, owner, bounds, seed, body, 42, [(60, 30, 100, 85)], styles) is None
            )
