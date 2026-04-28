"""phash 单元测试。"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from ai_phone.agent.runner.phash import (
    compute_phash,
    diff_rate,
    hamming_distance,
)


def _img_bytes(color: tuple, size: int = 64) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compute_phash_returns_int_for_valid_bytes():
    data = _img_bytes((128, 128, 128))
    h = compute_phash(data)
    assert isinstance(h, int)
    assert h >= 0


def test_compute_phash_returns_none_for_garbage():
    assert compute_phash(b"not-an-image") is None
    assert compute_phash(b"") is None


def test_same_image_hashes_equal():
    a = _img_bytes((30, 60, 90))
    b = _img_bytes((30, 60, 90))
    assert compute_phash(a) == compute_phash(b)
    assert diff_rate(compute_phash(a), compute_phash(b)) == 0.0


def test_different_images_have_positive_diff():
    a = _img_bytes((0, 0, 0))
    # 纯色图经 pHash 后差异很小（都等于均值），构造带图形的图片以产生差异
    img = Image.new("RGB", (64, 64), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 32, 32], fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b = buf.getvalue()

    rate = diff_rate(compute_phash(a), compute_phash(b))
    assert rate > 0.0


def test_hamming_distance_none_returns_max():
    assert hamming_distance(None, 0) == 256
    assert hamming_distance(1, None) == 256
