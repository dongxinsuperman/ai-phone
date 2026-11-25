import io

import pytest
from PIL import Image, ImageDraw

from ai_phone.agent.android_frame_marks import annotate_frame


def _png(size=(300, 180), mode="RGB"):
    image = Image.new(mode, size, (38, 46, 65, 93) if mode == "RGBA" else (38, 46, 65))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_marks_retain_original_size_half_open_overlapping_bounds_and_transparency():
    raw = _png(mode="RGBA")
    targets = [
        {"id": "A17", "bounds": [10, 15, 194, 140], "point": [35, 72]},
        {"id": "V3", "bounds": [112, 58, 241, 164]},
    ]
    saved = [dict(t, bounds=list(t["bounds"])) for t in targets]
    frame = annotate_frame(raw, targets)
    original = Image.open(io.BytesIO(raw))
    marked = Image.open(io.BytesIO(frame))
    assert frame != raw and marked.format == "PNG"
    assert marked.size == original.size and marked.mode == "RGBA"
    for point in [(10, 110), (193, 110), (85, 139), (112, 110), (240, 150), (195, 163)]:
        assert marked.getpixel(point) == (220, 32, 32, 255)
    for point in [(9, 110), (194, 110), (85, 140), (241, 150), (195, 164), (40, 80)]:
        assert marked.getpixel(point) == original.getpixel(point)
    assert targets == saved
    assert annotate_frame(raw, targets[::-1]) == frame


def test_crowded_edge_badges_use_each_original_id_once_and_remain_readable(monkeypatch):
    seen = {}
    draw_text = ImageDraw.ImageDraw.text

    def record(draw, point, text, *args, **kwargs):
        assert text not in seen
        seen[text] = draw.textbbox(point, text, font=kwargs["font"])
        return draw_text(draw, point, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    targets = [
        {"id": "V91", "bounds": [-3, 16, 20, 37]},
        {"id": "A8", "bounds": [0, 16, 20, 37]},
        {"id": "A12", "bounds": [250, 127, 300, 180]},
        {"id": "V4", "bounds": [280, 148, 310, 201]},
    ]
    out = Image.open(io.BytesIO(annotate_frame(_png(), targets)))
    assert set(seen) == {"V91", "A8", "A12", "V4"}
    for key, (left, top, right, bottom) in seen.items():
        assert 0 <= left < right <= out.width and 0 <= top < bottom <= out.height
        crop = out.crop((left, top, right, bottom))
        assert any(r > g + 30 and b < r for r, g, b in crop.get_flattened_data()), key
        for other, (x, y, r, b) in seen.items():
            if key != other:
                assert min(r, right) <= max(x, left) or min(b, bottom) <= max(y, top)


def test_even_physically_overcrowded_thumbnail_returns_a_full_sized_png():
    raw = _png((32, 18))
    targets = [{"id": f"V{i}", "bounds": [1, 1, 31, 17]} for i in range(1, 90)]
    marked = Image.open(io.BytesIO(annotate_frame(raw, targets)))
    assert marked.size == (32, 18) and marked.format == "PNG"


def test_empty_list_keeps_original_bytes_and_invalid_ids_cannot_be_substituted():
    raw = _png()
    assert annotate_frame(raw, []) == raw
    with pytest.raises(ValueError, match="unique"):
        annotate_frame(raw, [{"id": "A1", "bounds": [2, 3, 4, 7]}] * 2)
    with pytest.raises(ValueError, match="visible pixels"):
        annotate_frame(raw, [{"id": "A1", "bounds": [-1, 0, 0, 1]}])


def test_one_pixel_at_the_bottom_right_stays_visible_under_its_badge():
    frame = annotate_frame(_png(), [{"id": "V17", "bounds": [299, 179, 300, 180]}])
    marked = Image.open(io.BytesIO(frame))
    assert marked.getpixel((299, 179)) == (220, 32, 32)
