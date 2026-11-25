"""Read large, regularly spaced numeric displays drawn in several ink colours."""

from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

from .android_glyphs import _bank, _vector
from .android_visual_regions import _components


def _runs(projection):
    edges = np.flatnonzero(np.diff(np.pad(projection.astype(np.int8), 1)))
    return edges.reshape(-1, 2)


@lru_cache(maxsize=1)
def _digits():
    bank = _bank(False)
    if bank is None:
        return None
    digits = (bank.labels >= ord("0")) & (bank.labels <= ord("9"))
    if np.unique(bank.labels[digits]).size != 10:
        return None
    return bank.vectors[digits], bank.labels[digits]


def _digit(mask, alphabet):
    vectors, labels = alphabet
    scores = vectors @ _vector(mask)
    ranked = sorted(((float(scores[labels == ord(ch)].max()), ch) for ch in "0123456789"), reverse=True)
    (score, digit), (runner_up, _) = ranked[:2]
    if score >= 0.72 and score - runner_up >= 0.095:
        return digit
    # Some display faces draw a straight diagonal tail on a 6 or 9. Require
    # the enclosed counter to be on the correct side before relaxing the score.
    if digit in "69" and score >= 0.575 and score - runner_up >= 0.16:
        holes = Image.fromarray(np.pad(~mask, 1, constant_values=True).astype(np.uint8) * 255).copy()
        ImageDraw.floodfill(holes, (0, 0), 0)
        inside = [part for part, n in _components(holes) if n >= 0.045 * mask.size]
        if len(inside) == 1:
            cy = (inside[0].center[1] - 1) / mask.shape[0]
            if (digit == "9" and cy < 0.43) or (digit == "6" and cy > 0.57):
                return digit
    return None


def _read(delta, origin, height, alphabet):
    ink = delta >= 8
    cells = _runs(ink.any(axis=0))
    if not 4 <= len(cells) <= 40 or np.max(cells[1:, 0] - cells[:-1, 1]) > 0.63 * height:
        return None
    ys = np.flatnonzero(ink.any(axis=1))
    top, bottom = int(ys.min()), int(ys.max() + 1)
    height = bottom - top
    result = []
    for left, right in cells:
        contrast = delta[:, left:right]
        # A leading zero and an active digit can have very different contrast.
        stroke = contrast >= max(4.0, 0.46 * np.percentile(contrast[contrast >= 8], 78))
        rows = _runs(stroke.any(axis=1))
        if not len(rows):
            return None
        top_ink, bottom_ink = int(rows[0, 0]), int(rows[-1, 1])
        ink_height = bottom_ink - top_ink
        if ink_height >= 0.78 * height and len(rows) == 1 and bottom_ink >= bottom - 0.13 * height:
            char = _digit(stroke[top_ink:bottom_ink], alphabet)
        elif (
            len(rows) == 2
            and right - left <= 0.32 * height
            and rows[1, 0] - rows[0, 1] > 0.13 * height
            and all(
                0.05 * height <= hi - lo <= 0.30 * height and 0.63 <= (right - left) / (hi - lo) <= 1.45
                for lo, hi in rows
            )
            and 0.6 <= (rows[0, 1] - rows[0, 0]) / (rows[1, 1] - rows[1, 0]) <= 1.6
        ):
            char = ":"
        elif (
            len(rows) == 1
            and ink_height <= 0.27 * height
            and 0.55 <= (right - left) / ink_height <= 1.6
            and bottom - bottom_ink <= 0.12 * height
            and stroke[top_ink:bottom_ink].mean() > 0.42
        ):
            char = "."
        else:
            return None
        if char is None:
            return None
        result.append(char)
    text = "".join(result)
    if (
        not text[0].isdigit()
        or not text[-1].isdigit()
        or not any(c in ".:" for c in text)
        or any(not a.isdigit() and not b.isdigit() for a, b in zip(text, text[1:]))
        or sum(c.isdigit() for c in text) < 3
    ):
        return None
    x, y = origin
    return text, (x + int(cells[0, 0]), y + top, x + int(cells[-1, 1]), y + bottom)


def read_large_numbers(image, excluded):
    """Return complete numerical lines with justified digits and separators."""
    width, height = image.size
    scale = max(1, min(width, height) / 336)
    thumb = image.resize((round(width / scale), round(height / scale)), Image.Resampling.BOX)
    reduced = np.asarray(thumb, dtype=np.int16)
    samples = reduced[::4, ::4].reshape(-1, 3)
    cols = samples.astype(np.int32) // 8
    packed = (cols[:, 0] * 32 + cols[:, 1]) * 32 + cols[:, 2]
    counts = np.bincount(packed, minlength=32768)
    rgb = np.asarray(image)
    found = []
    for key in np.argsort(counts)[-3:][::-1]:
        if counts[key] < len(samples) * 0.10:
            break
        colour = np.median(samples[packed == key], axis=0).astype(np.int16)
        contrast = np.max(np.abs(reduced - colour), axis=2)
        parts = _components(Image.fromarray((contrast >= 8).astype(np.uint8) * 255))
        short = min(thumb.size)
        glyphs = sorted(
            (
                p
                for p, n in parts
                if max(12, 0.079 * short) <= p.height <= 0.24 * short
                and 0.16 * p.height <= p.width <= 1.35 * p.height
                and 0.095 * p.area <= n <= 0.88 * p.area
            ),
            key=lambda p: p.left,
        )
        for seed in glyphs:
            h = seed.height
            line = [
                p
                for p in glyphs
                if abs(p.top - seed.top) <= 0.13 * h and abs(p.bottom - seed.bottom) <= 0.13 * h
            ]
            if len(line) < 3:
                continue
            groups = []
            for p in line:
                if not groups or p.left - groups[-1][-1].right > 1.14 * h:
                    groups.append([])
                groups[-1].append(p)
            for group in groups:
                if len(group) < 3 or seed is not group[0]:
                    continue
                x = max(0, int((group[0].left - 0.37 * h) * scale))
                y = max(0, int((min(p.top for p in group) - 0.14 * h) * scale))
                r = min(width, round((group[-1].right + 0.37 * h) * scale))
                b = min(height, round((max(p.bottom for p in group) + 0.14 * h) * scale))
                if any(x < pr and px < r and y < pb and py < b for px, py, pr, pb in excluded):
                    continue
                patch = np.max(np.abs(rgb[y:b, x:r].astype(np.int16) - colour), axis=2)
                if (patch < 6).mean() < 0.45 or min((patch[0] < 6).mean(), (patch[-1] < 6).mean()) < 0.92:
                    continue
                alphabet = _digits()
                result = _read(patch, (x, y), h * scale, alphabet) if alphabet else None
                if result and result not in found:
                    found.append(result)
    return found
