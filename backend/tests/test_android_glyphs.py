import numpy as np
from PIL import ImageFont
import pytest

from ai_phone.agent import android_glyphs as glyphs


def _render(text, face, size, threshold=115):
    path, index = face
    font = ImageFont.truetype(str(path), size, index=index, layout_engine=ImageFont.Layout.BASIC)
    bitmap = font.getmask(text, mode="L")
    ink = np.frombuffer(bytes(bitmap), np.uint8).reshape(bitmap.size[::-1]) > threshold
    return np.pad(ink, ((4, 3), (5, 2)))


def test_mixed_masks_keep_order_and_visible_punctuation():
    cjk, latin = glyphs._faces()
    if not (latin or cjk):
        pytest.skip("no readable system font installed")
    face = (latin or cjk)[0]
    masks = [_render(text, face, n) for text, n in (("2.75", 31), ("72%", 37), ("RED 42", 38))]
    masks.insert(1, np.zeros((30, 42), bool))
    assert glyphs.recognize_masks(masks) == ["2.75", None, "72%", "RED 42"]


def test_chinese_at_original_sizes_and_with_disconnected_parts():
    cjk, _ = glyphs._faces()
    if not cjk:
        pytest.skip("no Chinese system font installed")
    words = ["春风明月", "河流山谷", "山丘42"]
    masks = [_render(text, cjk[0], n) for text, n in zip(words, (35, 44, 32))]
    assert glyphs.recognize_masks(masks) == words


def test_uncertain_shapes_stay_unnamed():
    y, x = np.ogrid[-16:17, -16:17]
    icon = x * x + y * y < 180
    bars = np.tile(np.arange(32) % 4 < 2, (32, 1))
    masks = [
        icon,
        bars,
        np.ones((32, 4), dtype=bool),
        np.eye(32, dtype=bool),
        np.random.default_rng(14).random((25, 24)) > 0.5,
        np.zeros((4, 3, 2), dtype=bool),
    ]
    assert glyphs.recognize_masks(masks) == [None] * len(masks)


def test_missing_fonts_quietly_abstain(monkeypatch):
    glyphs._bank.cache_clear()
    monkeypatch.setattr(glyphs, "_faces", lambda: ([], []))
    try:
        sample = np.tile(np.arange(30) % 5 < 2, (27, 1))
        assert glyphs.recognize_masks([sample]) == [None]
    finally:
        glyphs._bank.cache_clear()


def test_rechecks_two_small_glyphs_at_their_original_pixel_size():
    face = next((f for f in glyphs._faces()[0] if f[0].name == "PingFang.ttc"), None)
    if face is None:
        pytest.skip("the font of this small-raster regression is unavailable")
    mask = _render("溪竹", face, 19, threshold=138)
    assert glyphs.recognize_masks([mask]) == ["溪竹"]


def test_unknown_prefix_cannot_be_discarded_to_name_a_whole_line():
    cjk, latin = glyphs._faces()
    if not (latin or cjk):
        pytest.skip("no readable system font installed")
    word = _render("RED", (latin or cjk)[0], 32)
    random = np.random.default_rng(122).random((word.shape[0], 32)) > 0.53
    same_line = np.concatenate((random, np.zeros((word.shape[0], 3), bool), word), axis=1)
    assert glyphs.recognize_masks([same_line]) == [None]


@pytest.mark.parametrize("size", (22, 46))
def test_isolated_case_requires_evidence_but_mixed_text_keeps_its_baseline(size):
    cjk, latin = glyphs._faces()
    if not (latin or cjk):
        pytest.skip("no readable system font installed")
    face = (latin or cjk)[0]
    words = ["C", "c", "S", "s", "I", "l", "1", "A", "D", "a", "d", "C4D", "Csv 42"]
    masks = [_render(word, face, size) for word in words]
    assert glyphs.recognize_masks(masks) == [None] * 7 + words[7:]


def test_separated_thin_cjk_pair_uses_native_size_evidence():
    face = next((f for f in glyphs._faces()[0] if f[0].name == "PingFang.ttc"), None)
    if face is None:
        pytest.skip("the font of this thin-stroke regression is unavailable")
    # Independent UI labels, rendered at a different size and stroke threshold.
    words = ["春雨", "竹林"]
    masks = [_render(word, face, 29, threshold=185) for word in words]
    assert glyphs.recognize_masks(masks) == words


def test_thin_pair_cannot_gain_a_confusable_radical_from_extra_fonts():
    from pathlib import Path

    path = Path("/System/Library/Fonts/STHeiti Light.ttc")
    if not path.is_file():
        pytest.skip("the independent test typeface is unavailable")
    mask = _render("银杏", (path, 0), 32, threshold=120)
    assert glyphs.recognize_masks([mask])[0] in (None, "银杏")
