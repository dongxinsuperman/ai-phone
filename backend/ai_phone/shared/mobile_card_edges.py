"""Check the adjoining ideographic cell for detached ink before naming the whole card."""

from __future__ import annotations

from functools import lru_cache
import threading
import numpy as np
from PIL import ImageFont

_FONT_LOCK = threading.Lock()


def _runs(values):
    indices = np.flatnonzero(values)
    return [
        (part[0], part[-1] + 1)
        for part in np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
        if len(part)
    ]


def _strokes(ink, pitch):
    bands = []
    for top, bottom in _runs(ink.any(axis=1)):
        patch = ink[top:bottom]
        cols = np.flatnonzero(patch.any(axis=0))
        left, right = cols[0], cols[-1] + 1
        if (
            bottom - top > 0.23 * pitch
            or not 0.25 * pitch <= right - left <= 1.17 * pitch
            or len(cols) != right - left
            or patch[:, left:right].mean() < 0.77
        ):
            return ()
        bands.append((left, right, top, bottom))
    return tuple(bands) if 1 <= len(bands) <= 3 else ()


@lru_cache(maxsize=1)
def _sparse_alphabet():
    # The full bold alphabet already exists. Only its independently horizontal strokes or
    # solid bars can match sparse neighbours; include punctuation and symbols as competitors.
    from . import mobile_card_text as scanner

    with scanner._RASTER_LOCK:
        alphabet, matrix = scanner._bold_alphabet()
    shape = matrix.reshape(-1, scanner.glyphs._SIZE[1], scanner.glyphs._SIZE[0])
    lo = shape.min((1, 2))
    hi = shape.max((1, 2))
    mask = shape > ((lo + hi)[:, None, None] / 2)
    strongest = mask.sum(axis=2)
    eligible = ((strongest >= 0.59 * mask.shape[2]) * strongest).sum(axis=1) >= 0.87 * strongest.sum(axis=1)
    symbols = (
        range(32, 127),
        range(0x2000, 0x2070),
        range(0x2190, 0x2300),
        range(0x2500, 0x2580),
        range(0x3000, 0x3040),
        range(0xFF00, 0xFFA0),
    )
    return tuple(
        dict.fromkeys(
            [chr(int(v)) for v in alphabet[eligible]] + [chr(v) for group in symbols for v in group]
        )
    )


@lru_cache(maxsize=24)
def _font(style, pitch):
    path, face, weight = style
    font = ImageFont.truetype(str(path), pitch, index=face, layout_engine=ImageFont.Layout.BASIC)
    if weight:
        font.set_variation_by_axes([weight])
    return font


@lru_cache(maxsize=2048)
def _measure(style, pitch, character):
    with _FONT_LOCK:
        font = _font(style, pitch)
        bitmap, origin = font.getmask2(character, mode="L", anchor="ls")
    ink = np.frombuffer(bytes(bitmap), np.uint8).reshape(bitmap.size[::-1]) >= 115
    yy, xx = np.nonzero(ink)
    if not len(xx):
        return None
    left, right, top, bottom = xx.min(), xx.max() + 1, yy.min(), yy.max() + 1
    bands = _strokes(ink, pitch)
    return (origin[0] + left, origin[0] + right, origin[1] + top, origin[1] + bottom), tuple(
        (x0 + origin[0], x1 + origin[0], y0 + origin[1], y1 + origin[1]) for x0, x1, y0, y1 in bands
    )


@lru_cache(maxsize=48)
def _bar_templates(style, pitch):
    records = {}
    for char in _sparse_alphabet():
        row = _measure(style, pitch, char)
        if row and row[1]:
            with _FONT_LOCK:
                advance = _font(style, pitch).getlength(char)
            records[char] = (np.asarray(row[1], dtype="float32"), advance)
    return records


def _neighbour(rgb, owner, box, pitch, fg, side, protected):
    # Check the nearest cell on the same baseline, staying inside the original card.
    x, y, r, b = box
    reach = round(1.18 * pitch)
    height_margin = round(0.10 * pitch)
    start = max(owner[0], x - reach) if side < 0 else r
    end = x if side < 0 else min(owner[2], r + reach)
    top = max(owner[1], y - height_margin)
    bottom = min(owner[3], b + height_margin)
    if end - start < 0.28 * pitch or bottom <= top:
        return None
    if any(start < q[2] and q[0] < end and top < q[3] and q[1] < bottom for q in protected):
        # The complete printed title cannot be verified across an occluded or sensitive neighbour.
        return (start, top, end, bottom), ()
    ink = np.max(np.abs(rgb[top:bottom, start:end] - fg), axis=2) < 37
    spans = _runs(ink.any(0))
    if not spans:
        return None
    near = spans[-1] if side < 0 else spans[0]
    gap = end - start - near[1] if side < 0 else near[0]
    if gap > 0.35 * pitch:
        return None
    # Combine disconnected radicals only within the adjacent grid cell.
    mark = np.zeros_like(ink)
    for a, z in spans:
        if (near[1] - a if side < 0 else z - near[0]) <= 1.12 * pitch:
            mark[:, a:z] = ink[:, a:z]
    yy, xx = np.nonzero(mark)
    if not len(xx):
        return None
    bw, bh = xx.max() - xx.min() + 1, yy.max() - yy.min() + 1
    broad = 0.28 * pitch <= bw <= 1.15 * pitch and len(xx) >= 0.075 * pitch**2
    tall = 0.10 * pitch <= bw <= 0.28 * pitch and bh >= 0.50 * pitch and len(xx) >= 0.055 * pitch**2
    if not (broad or tall) or bh > 1.17 * pitch:
        return None
    if (side < 0 and xx.min() == 0 and start > owner[0]) or (
        side > 0 and xx.max() + 1 == end - start and end < owner[2]
    ):
        return None
    bounds = (start + int(xx.min()), top + int(yy.min()), start + int(xx.max()) + 1, top + int(yy.max()) + 1)
    bands = _strokes(mark, pitch)
    return bounds, np.asarray(
        [(a + start, z + start, t + top, b + top) for a, z, t, b in bands], dtype="float32"
    )


def _match_bars(bands, text, box, pitch, side, styles):
    if not len(bands):
        return None
    x, y, r, b = box
    size = round(pitch)
    scores = {}
    for style in styles:
        body = [_measure(style, size, char) for char in text]
        if not all(body):
            continue
        glyphs = [row[0] for row in body]
        baseline = (y - min(v[2] for v in glyphs) + b - max(v[3] for v in glyphs)) / 2
        for char, (ref, advance) in _bar_templates(style, size).items():
            if ref.shape != bands.shape:
                continue
            # Relative stroke level, width and thickness distinguish fullwidth punctuation
            # even though its binary silhouette may look identical after tight cropping.
            origin = x - glyphs[0][0] - advance if side < 0 else r + pitch - glyphs[-1][1]
            query = bands - [origin, origin, baseline, baseline]
            delta = np.abs(ref - query)
            error = (delta.mean() + np.max(delta) * 0.35) / pitch
            scores.setdefault(char, []).append(error)
    if len(scores) < 2:
        return None
    ordered = sorted((np.mean(sorted(values)[: min(3, len(values))]), ch) for ch, values in scores.items())
    first, runner = ordered[:2]
    if first[0] > 0.09 or runner[0] - first[0] < 0.018:
        return None
    return first[1]


def complete_caption(rgb, owner, box, seed, caption, pitch, protected, styles):
    x, y, r, b = box
    fg = np.median(rgb[y:b, x:r][seed], axis=0)
    result = list(box)
    word = caption
    for side in (-1, 1):
        for extensions in range(3):
            neighbour = _neighbour(rgb, owner, result, pitch, fg, side, protected)
            if neighbour is None:
                break
            if extensions == 2:
                return None
            extent, bands = neighbour
            char = _match_bars(bands, word, result, pitch, side, styles)
            if not char:
                return None
            word = char + word if side < 0 else word + char
            result = [
                min(result[0], extent[0]),
                min(result[1], extent[1]),
                max(result[2], extent[2]),
                max(result[3], extent[3]),
            ]
    return word, result
