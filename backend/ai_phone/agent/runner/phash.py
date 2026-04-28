"""16x16 平均哈希（pHash 简化版），与 Groovy `computePHash` 对齐。

用于"页面稳定检测"：两张截图转灰度缩放到 16x16（共 256 像素），算像素均值，
每个像素 > 均值则位 1，否则位 0，得到 256 位整数。两张图异或后的 bit 数
÷ 256 即为差异率，小于阈值（默认 0.005）判稳定。

保持 Groovy 原始算法不动，方便日后和历史 Sonic 日志对齐。
"""
from __future__ import annotations

import io
from typing import Optional

from PIL import Image

_HASH_SIZE = 16
_TOTAL_BITS = _HASH_SIZE * _HASH_SIZE  # 256


def compute_phash(image_bytes: bytes) -> Optional[int]:
    """返回 256-bit 哈希（Python int）。失败返回 None。"""
    if not image_bytes:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("L").resize(
            (_HASH_SIZE, _HASH_SIZE), Image.Resampling.LANCZOS
        )
    except Exception:
        return None

    pixels = list(img.getdata())
    if len(pixels) != _TOTAL_BITS:
        return None
    avg = sum(pixels) // _TOTAL_BITS
    h = 0
    for i, v in enumerate(pixels):
        if v > avg:
            h |= 1 << i
    return h


def hamming_distance(h1: Optional[int], h2: Optional[int]) -> int:
    """两个哈希的汉明距离（差异位数）。任一为 None 时回落为最大差异 256。"""
    if h1 is None or h2 is None:
        return _TOTAL_BITS
    # Python 3.10+ 有 int.bit_count()，3.9 还得用 bin() 数 '1'。
    return bin(h1 ^ h2).count("1")


def diff_rate(h1: Optional[int], h2: Optional[int]) -> float:
    """汉明距离 / 256，范围 [0, 1]。"""
    return hamming_distance(h1, h2) / _TOTAL_BITS
