"""Confirm heavy, square-script captions in an existing visible card using local font outlines."""

from __future__ import annotations

import os
from collections import Counter
import threading
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import numpy as np
from PIL import Image, ImageFont

from ai_phone.agent import android_glyphs as glyphs, android_visible_text as text

_RASTER_LOCK = threading.Lock()


class _Reading(NamedTuple):
    character: str
    quality: float
    margin: float
    votes: float


@lru_cache(maxsize=1)
def _styles():
    sdk_roots = [
        Path(v) for key in ("DEVECO_SDK_HOME", "HOS_SDK_HOME", "OHOS_SDK_HOME") if (v := os.environ.get(key))
    ]
    sdk_roots.extend(
        parent / "DevEco-Studio.app/Contents/sdk"
        for parent in (Path("/Applications"), Path.home() / "Applications")
    )
    choices = []
    for sdk in sdk_roots:
        for base in (sdk, sdk / "default"):
            path = base / "hms/previewer/resources/fonts/HarmonyOS_Sans_SC.ttf"
            if not path.is_file():
                continue
            try:
                axes = ImageFont.truetype(str(path), 24).get_variation_axes()
            except (OSError, ValueError):
                continue
            if len(axes) == 1 and axes[0]["minimum"] <= 500 and axes[0]["maximum"] >= 900:
                choices.extend((path, 0, weight) for weight in range(500, 901, 100))
                break
        if choices:
            break
    installed = glyphs._styled_faces()
    bold = [
        (path, face)
        for path, face in installed
        if any(
            tag in glyphs._sized_font(path, face, 24).getname()[1].casefold()
            for tag in ("bold", "heavy", "black", "w6")
        )
    ]
    choices.extend((path, face, 0) for path, face in (bold or installed)[:3])
    return tuple(choices)


@lru_cache(maxsize=96)
def _font(path, face, weight, size):
    face_font = ImageFont.truetype(str(path), size, index=face, layout_engine=ImageFont.Layout.BASIC)
    if weight:
        face_font.set_variation_by_axes([weight])
    return face_font


def _centered(ink):
    vector = glyphs._vector(ink)
    vector -= vector.mean()
    return vector / max(float(np.linalg.norm(vector)), 1e-8)


@lru_cache(maxsize=1024)
def _letter(code, pitch):
    # Compare at the measured screen pitch, allowing one pixel for raster rounding.
    vectors, metrics = [], []
    for path, face, weight in _styles():
        for size in (pitch - 1, pitch, pitch + 1):
            with _RASTER_LOCK:
                raster = _font(path, face, weight, size).getmask(chr(code), mode="L")
            ink = np.frombuffer(bytes(raster), np.uint8).reshape(raster.size[::-1]) > 107
            box = glyphs._bounds(ink)
            if box is None:
                vectors.append(np.zeros(glyphs._SIZE[0] * glyphs._SIZE[1], np.float32))
                metrics.append((0, 0, 0))
                continue
            top, bottom, left, right = box
            width, height = right - left, bottom - top
            vectors.append(_centered(ink[top:bottom, left:right]))
            metrics.append((np.log(width / height), height / size, width / size))
    # Bounded, half-precision storage keeps repeated card captions from rebuilding the font rasters.
    matrix = np.asarray(vectors, np.float16)
    return matrix, np.asarray(metrics, np.float32), _edge_runs(matrix).astype(np.int8)


@lru_cache(maxsize=1)
def _bold_alphabet():
    # A regular-font shortlist may omit a heavily painted letter entirely. Compare the same
    # complete alphabet against a bold local face before fine matching at the observed size.
    styles = _styles()
    path, face, weight = next((style for style in styles if style[2] == 800), styles[0])
    alphabet = np.fromiter((ord(char) for char in glyphs._han()), dtype=np.int32)
    font = _font(path, face, weight, glyphs._SIZE[1])
    vectors = np.zeros((len(alphabet), glyphs._SIZE[0] * glyphs._SIZE[1]), dtype=np.float32)
    for index, code in enumerate(alphabet):
        raster = font.getmask(chr(code), mode="L")
        ink = np.frombuffer(bytes(raster), np.uint8).reshape(raster.size[::-1]) > 115
        box = glyphs._bounds(ink)
        if box:
            top, bottom, left, right = box
            vectors[index] = _centered(ink[top:bottom, left:right])
    return alphabet, vectors


def _shortlist(cells):
    regular = glyphs._matches(cells)
    with _RASTER_LOCK:
        alphabet, templates = _bold_alphabet()
    vectors = np.stack([cell.vector for cell in cells])
    vectors -= vectors.mean(axis=1, keepdims=True)
    vectors /= np.maximum(1e-8, np.linalg.norm(vectors, axis=1, keepdims=True))
    scores = vectors @ templates.T
    index = np.argpartition(scores, -8, axis=1)[:, -8:]
    return np.concatenate((regular[0], alphabet[index]), axis=1) if regular else alphabet[index]


def _edge_runs(vectors):
    # At two depths along each outer edge, count distinct ink tips. Interior overlap by itself
    # cannot justify a candidate whose printed outline has fewer separated strokes.
    raster = vectors.reshape(*vectors.shape[:-1], glyphs._SIZE[1], glyphs._SIZE[0])
    limits = raster.min(axis=(-2, -1), keepdims=True) + 0.52 * np.ptp(raster, axis=(-2, -1), keepdims=True)
    ink = raster > limits
    counts = []
    for side in (ink, ink[..., ::-1, :], ink.swapaxes(-2, -1), ink.swapaxes(-2, -1)[..., ::-1, :]):
        depths = []
        for depth in (2, 3):
            edge = side[..., :depth, :].any(axis=-2)
            depths.append((edge[..., 1:] & ~edge[..., :-1]).sum(-1) + edge[..., 0])
        counts.append(np.stack(depths, axis=-1))
    return np.stack(counts, axis=-2)


def _read_cell(cell, codes, pitch):
    codes = tuple(sorted({int(k) for k in codes if 0x3400 <= k <= 0x9FFF}))
    if len(codes) < 2:
        return None
    return _match_raster(cell.vector.tobytes(), cell.right - cell.left, cell.bottom - cell.top, pitch, codes)


@lru_cache(maxsize=1024)
def _match_raster(raster, width, height, pitch, codes):
    # Repeated observations can reuse an identical foreground silhouette and shortlist.
    vector = np.frombuffer(raster, dtype=np.float32)
    samples = [_letter(code, round(pitch)) for code in codes]
    template = np.stack([s[0] for s in samples])
    metrics = np.stack([s[1] for s in samples])
    query = vector - vector.mean()
    query /= max(float(np.linalg.norm(query)), 1e-8)
    delta = np.abs(metrics - (np.log(width / height), height / pitch, width / pitch))
    scores = template @ query - delta @ (0.05, 0.08, 0.08)
    query_edges = _edge_runs(query)
    reference_edges = np.stack([s[2] for s in samples])
    # Require two extra separated strokes at both depths, across every available font/style.
    contradicts_ink = (query_edges - reference_edges.max(axis=1)).min(axis=-1).max(axis=-1) >= 2
    scores[contradicts_ink, :] = -2
    # Reward a character that fits multiple variations instead of a single fortuitous raster match.
    top = min(4, scores.shape[1])
    strength = np.partition(scores, -top, axis=1)[:, -top:].mean(axis=1)
    order = np.argsort(-strength)
    best, other = order[:2]
    votes = np.mean(np.argmax(scores, axis=0) == best)
    return _Reading(
        chr(codes[best]), float(strength[best]), float(strength[best] - strength[other]), float(votes)
    )


def _contours(rgb, box, seed):
    x, y, r, b = box
    patch = rgb[y:b, x:r].astype(np.float32)
    fg = np.median(patch[seed], axis=0)
    bg = np.median(patch[~seed], axis=0)
    direction = fg - bg
    signal = float(direction @ direction)
    if signal < 55**2 or np.mean(np.max(np.abs(patch[seed] - fg), axis=1) < 32) < 0.64:
        return None
    # Project colour onto the measured local foreground/background direction, for either light or dark ink.
    alpha = (patch - bg) @ direction / signal
    fits = []
    # Antialiased edges change which pen strokes touch. Preserve the common complete grid
    # across a spread of measured ink levels; a single favourable threshold is insufficient.
    for cutoff in (0.35, 0.45, 0.55, 0.65, 0.75):
        mask = alpha >= cutoff
        if 0.42 < mask.mean() < 0.76:
            fitted = glyphs._even_cells(mask, unread=True)
            if fitted and len(fitted[0]) >= 3:
                fits.append(fitted[:2])
    counts = Counter(len(row) for row, _ in fits)
    if not counts:
        return None
    length, votes = counts.most_common(1)[0]
    if votes < 3 or votes < 0.70 * len(fits):
        return None
    return [fit for fit in fits if len(fit[0]) == length]


def _stable_character(readings):
    counts = Counter(read.character for read in readings if read)
    if not counts:
        return None
    char, support = counts.most_common(1)[0]
    if support < 0.70 * len(readings):
        return None
    firm, strong = 0, False
    for read in readings:
        if read is None:
            continue
        if read.character != char:
            if read.quality >= 0.65 and read.margin >= 0.03 and read.votes >= 0.40:
                return None
        elif read.quality >= 0.62 and read.margin >= 0.015 and read.votes >= 0.40:
            firm += 1
            strong |= read.quality >= 0.66 and read.margin >= 0.035
    return char if firm >= 2 and strong else None


def read_card_titles(image: Image.Image, targets: list[dict], excluded=()) -> list[dict]:
    """Return located screen readings, without asserting that the printed word itself is clickable."""
    width, height = image.size
    protected = [box for value in excluded if (box := text._rect(value, image.size))]
    rectangles = set()
    for target in targets:
        box = text._rect(target.get("bounds"), image.size)
        if box and ("input" in target.get("actions", ()) or target.get("state", {}).get("password")):
            protected.append(box)
            continue
        if (
            box
            and target.get("point")
            and target.get("role") != "visible_text"
            and {"tap", "try_tap"}.intersection(target.get("actions", ()))
            and text._area(box) < 0.40 * width * height
        ):
            rectangles.add(box)
    if not rectangles or not _styles():
        return []
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    gray = np.asarray(image.convert("L"))
    density = max(1, min(width, height) / 360)
    lines = sorted(text._colour_lines(rgb, gray, density, protected), key=lambda line: -line.regularity)
    groups, cells = [], []
    for line in lines:
        box = line.box
        x, y, r, b = box
        if (
            line.regularity < 0.55
            or line.contrast < 38
            or not 14 <= b - y <= 53
            or r - x < 2.5 * (b - y)
            or not any(text._inside(box, rect) for rect in rectangles)
            or any(text._same(box, existing, 0.78) for existing, *_ in groups)
        ):
            continue
        fits = _contours(rgb, box, line.mask)
        if not fits:
            continue
        ranges = []
        for row, pitch in fits:
            ranges.append((len(cells), len(row), pitch))
            cells.extend(row)
        groups.append((box, ranges, line.mask))
        if len(groups) >= 12:
            break
    if not cells:
        return []
    labels = _shortlist(cells)
    output = []
    from .mobile_card_edges import complete_caption

    for box, ranges, seed in groups:
        reads = []
        for first, count, pitch in ranges:
            row = [_read_cell(cells[i], labels[i], pitch) for i in range(first, first + count)]
            reads.append(row)
        characters = [_stable_character(column) for column in zip(*reads)]
        if not all(characters):
            continue
        owner = min((rect for rect in rectangles if text._inside(box, rect)), key=text._area)
        pitch = float(np.median([row[2] for row in ranges]))
        complete = complete_caption(rgb, owner, box, seed, "".join(characters), pitch, protected, _styles())
        if not complete:
            continue
        label, extent = complete
        output.append(
            {
                "text": label,
                "bounds": extent,
                "source": "screen",
                "method": "glyph_templates",
            }
        )
    return sorted(output, key=lambda row: (row["bounds"][1], row["bounds"][0]))
