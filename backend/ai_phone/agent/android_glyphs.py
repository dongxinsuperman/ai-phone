"""Read a line of visible ink by comparing its glyphs with local system fonts."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import string
import sys
import threading

import numpy as np
from PIL import Image, ImageFont


_SIZE = (24, 26)
_EM = 44
_MAX_LINES = 48
_MAX_CELLS = 768
_LOCK = threading.Lock()
_PUNCTUATION = "，。！？、：；（）《》【】「」〈〉…·×÷±°—"


@lru_cache(maxsize=1)
def _faces() -> tuple[list[tuple[Path, int]], list[tuple[Path, int]]]:
    """Prefer regular, simplified Chinese system faces; font files are optional."""
    cjk: list[tuple[Path, int]] = []
    latin: list[tuple[Path, int]] = []
    if sys.platform == "darwin":
        fonts = Path("/System/Library/Fonts")
        for path in sorted(
            Path("/System/Library/AssetsV2").glob("com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc")
        ):
            cjk.extend([(path, 3), (path, 7)])
        cjk.extend(
            [
                (fonts / "PingFang.ttc", 3),
                (fonts / "Hiragino Sans GB.ttc", 0),
                (fonts / "STHeiti Light.ttc", 0),
            ]
        )
        latin.extend([(fonts / "Supplemental/Arial.ttf", 0), (fonts / "Helvetica.ttc", 0)])
    elif sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        cjk.extend((fonts / name, 0) for name in ("msyh.ttc", "msyhbd.ttc", "Deng.ttf", "simhei.ttf"))
        latin.extend((fonts / name, 0) for name in ("arial.ttf", "segoeui.ttf"))
    else:
        bases = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local/share/fonts"]
        for base in bases:
            for sub in ("opentype/noto", "truetype/noto", "noto", ""):
                cjk.extend(
                    (base / sub / name, idx)
                    for name, idx in (
                        ("NotoSansCJK-Regular.ttc", 2),
                        ("NotoSansCJKsc-Regular.otf", 0),
                        ("NotoSansCJK-Medium.ttc", 2),
                    )
                )
            cjk.extend(
                [
                    (base / "truetype/wqy/wqy-zenhei.ttc", 0),
                    (base / "truetype/droid/DroidSansFallbackFull.ttf", 0),
                ]
            )
            latin.extend(
                (base / name, 0)
                for name in (
                    "truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf",
                    "truetype/dejavu/DejaVuSans.ttf",
                    "truetype/liberation2/LiberationSans-Regular.ttf",
                    "TTF/DejaVuSans.ttf",
                )
            )
        cjk.append((Path("/system/fonts/NotoSansCJK-Regular.ttc"), 2))
        latin.append((Path("/system/fonts/Roboto-Regular.ttf"), 0))

    def installed(candidates, limit):
        result = []
        for path, index in dict.fromkeys(candidates):
            try:
                if path.is_file():
                    ImageFont.truetype(str(path), _EM, index=index, layout_engine=ImageFont.Layout.BASIC)
                    result.append((path, index))
            except (OSError, ValueError):
                continue
            if len(result) == limit:
                break
        return result

    return installed(cjk, 3), installed(latin, 2)


@lru_cache(maxsize=1)
def _han() -> str:
    return "".join(
        bytes((row, col)).decode("gb2312", errors="ignore")
        for row in range(0xB0, 0xF8)
        for col in range(0xA1, 0xFF)
    )


def _bounds(mask):
    ys = np.flatnonzero(mask.any(axis=1))
    if not len(ys):
        return None
    xs = np.flatnonzero(mask.any(axis=0))
    return int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1


def _vector(ink):
    img = Image.fromarray(ink.astype(np.uint8) * 255).resize(_SIZE, Image.Resampling.BILINEAR)
    vec = np.asarray(img, dtype=np.float32).ravel() - 80.0
    return vec / max(float(np.linalg.norm(vec)), 1.0)


def _bitmap_bytes(bitmap):
    # Pillow's font masks are L-mode ImagingCore objects. Use Pillow's bulk
    # encoder instead of iterating over the pixels in Python with bytes(bitmap).
    return Image.Image()._new(bitmap).tobytes()


@dataclass
class _Bank:
    vectors: np.ndarray
    labels: np.ndarray
    aspects: np.ndarray
    heights: np.ndarray
    bottoms: np.ndarray
    bearings: np.ndarray
    ink_stats: tuple[np.ndarray, np.ndarray, np.ndarray]
    tile_norms: np.ndarray


def _tiles(vectors):
    # Non-overlapping quarters give a detached radical its own comparison.
    return vectors.reshape(*vectors.shape[:-1], 2, _SIZE[1] // 2, 2, _SIZE[0] // 2)


def _tile_norms(vectors):
    return np.maximum(1e-5, np.sqrt((_tiles(vectors) ** 2).sum(axis=(-3, -1))))


def _ink_stats(vectors):
    total = vectors.sum(axis=1)
    offset = np.maximum(0.0, -vectors.min(axis=1))
    norm = np.sqrt(np.maximum(1e-8, 1.0 + 2 * offset * total + vectors.shape[1] * offset**2))
    return total, offset, norm


@lru_cache(maxsize=2)
def _bank(han: bool) -> _Bank | None:
    cjk, latin = _faces()
    if han:
        fonts = [(p, i, _EM) for p, i in cjk]
        alphabet = _han()
    else:
        faces = list(dict.fromkeys(latin + cjk))
        fonts = [(p, i, size) for p, i in faces for size in (24, _EM)]
        alphabet = string.ascii_letters + string.digits + string.punctuation + _PUNCTUATION

    vectors, labels, metrics = [], [], []
    for path, face, size in fonts:
        try:
            font = ImageFont.truetype(str(path), size, index=face, layout_engine=ImageFont.Layout.BASIC)
        except (OSError, ValueError):
            continue
        missing = font.getmask("\u0378", mode="L")
        missing_signature = (missing.size, _bitmap_bytes(missing))
        for char in alphabet:
            mask, offset = font.getmask2(char, mode="L", anchor="ls")
            w, h = mask.size
            if not w or not h:
                continue
            buf = _bitmap_bytes(mask)
            if (mask.size, buf) == missing_signature:
                continue
            ink = np.frombuffer(buf, np.uint8).reshape(h, w) > 106
            bounds = _bounds(ink)
            if bounds is None:
                continue
            y0, y1, x0, x1 = bounds
            width, height = x1 - x0, y1 - y0
            advance = size if han else font.getlength(char)
            left = (offset[0] + x0) / size
            right = (advance - offset[0] - x1) / size
            metrics.append((np.log(width / height), height / size, (offset[1] + y1) / size, left, right))
            labels.append(ord(char))
            vectors.append(_vector(ink[y0:y1, x0:x1]))
    if not vectors:
        return None
    values = np.asarray(metrics, dtype=np.float32)
    mat = np.asarray(vectors)
    return _Bank(
        mat,
        np.asarray(labels, dtype=np.int32),
        values[:, 0],
        values[:, 1],
        values[:, 2],
        values[:, 3:],
        _ink_stats(mat),
        _tile_norms(mat),
    )


@dataclass
class _Cell:
    start: int
    end: int
    left: int
    right: int
    top: int
    bottom: int
    vector: np.ndarray


def _cells(mask):
    if not isinstance(mask, np.ndarray) or mask.ndim != 2 or mask.dtype != np.bool_ or mask.size > 800_000:
        return None
    box = _bounds(mask)
    if box is None:
        return None
    top, bottom, left, right = box
    line = mask[top:bottom, left:right]
    height, width = line.shape
    if not 9 <= height <= 180 or width > 4096 or width < 2 or line.mean() > 0.72:
        return None
    cols = np.flatnonzero(np.diff(np.pad(line.any(axis=0).astype(np.int8), 1))).reshape(-1, 2)
    if not len(cols) or len(cols) > 96:
        return None
    cells = []
    for start, (lo, _) in enumerate(cols):
        for last in range(start, min(len(cols), start + 7)):
            hi = cols[last, 1]
            if hi - lo > 1.57 * height or (
                last > start and cols[last, 0] - cols[last - 1, 1] > 0.27 * height
            ):
                break
            crop = line[:, lo:hi]
            ys = np.flatnonzero(crop.any(axis=1))
            y0, y1 = int(ys[0]), int(ys[-1]) + 1
            cells.append(_Cell(start, last + 1, int(lo), int(hi), y0, y1, _vector(crop[y0:y1])))
    return (len(cols), height, cells) if cells else None


def _matches(cells):
    """Keep nearby alternatives from each alphabet before fitting line metrics."""
    vectors = np.stack([c.vector for c in cells])
    heights = np.array([c.bottom - c.top for c in cells], dtype=np.float32)
    aspects = np.log(np.array([c.right - c.left for c in cells], dtype=np.float32) / heights)
    qsum, qoffset, qnorm = _ink_stats(vectors)
    qtiles = _tile_norms(vectors)
    parts = []
    for han in (False, True):
        # A narrow Latin stroke cannot be a full Chinese glyph in a mixed line.
        valid = np.flatnonzero(aspects > -0.82) if han else np.arange(len(cells))
        if not len(valid):
            continue
        # Build only once even when two Agent requests arrive on different threads.
        with _LOCK:
            bank = _bank(han)
        if bank is None:
            continue
        count = min(16, len(bank.labels))
        ids = np.zeros((len(cells), count), dtype=np.intp)
        scores = np.full(ids.shape, -4.0, dtype=np.float32)
        foreground = np.full(ids.shape, -4.0, dtype=np.float32)
        tiles = np.zeros((*ids.shape, 4), dtype=np.float32)
        bsum, boffset, bnorm = bank.ink_stats
        for chunk in np.array_split(valid, max(1, (len(valid) + 47) // 48)):
            sim = vectors[chunk] @ bank.vectors.T
            diff = np.abs(aspects[chunk, None] - bank.aspects)
            centered = sim - 0.095 * diff
            ix = np.argpartition(centered, -count, axis=1)[:, -count:]
            ids[chunk] = ix
            scores[chunk] = np.take_along_axis(centered, ix, axis=1)
            # Recover ink-only cosine from the centered templates without a
            # second raster bank. This independent ranking helps cross fonts.
            dot = np.take_along_axis(sim, ix, axis=1)
            dot += qoffset[chunk, None] * bsum[ix] + boffset[ix] * qsum[chunk, None]
            dot += vectors.shape[1] * qoffset[chunk, None] * boffset[ix]
            dot /= qnorm[chunk, None] * bnorm[ix]
            foreground[chunk] = dot - 0.065 * np.take_along_axis(diff, ix, axis=1)
            local = (_tiles(vectors[chunk, None, :]) * _tiles(bank.vectors[ix])).sum(axis=(-3, -1))
            local /= qtiles[chunk, None, :, :] * bank.tile_norms[ix]
            tiles[chunk] = local.reshape(len(chunk), count, 4)
        parts.append(
            (
                bank.labels[ids],
                scores,
                bank.heights[ids],
                bank.bottoms[ids],
                bank.bearings[ids],
                foreground,
                tiles,
            )
        )
    if not parts:
        return None
    return tuple(np.concatenate(values, axis=1) for values in zip(*parts))


def _grid_read(h, cells, data):
    """A repeated pitch gives independent evidence for joining detached radicals."""
    single = [c for c in cells if c.end == c.start + 1]
    centers = np.array(
        [
            (c.left + c.right) / 2
            for c in single
            if 0.63 * h <= c.right - c.left <= 1.28 * h and c.bottom - c.top >= 0.60 * h
        ]
    )
    if len(centers) < 3:
        return None
    adjacent = np.diff(centers)
    usual = adjacent[(adjacent >= 0.85 * h) & (adjacent <= 1.36 * h)]
    if not len(usual):
        return None
    slots = np.rint((centers - centers[0]) / np.median(usual)).astype(int)
    if np.any(np.diff(slots) <= 0):
        return None
    lo, hi = np.triu_indices(len(centers), 1)
    pitch = np.median((centers[hi] - centers[lo]) / (slots[hi] - slots[lo]))
    phase = np.median(centers - pitch * slots)
    if not 0.85 * h <= pitch <= 1.36 * h or np.max(np.abs(centers - phase - pitch * slots)) > 0.12 * pitch:
        return None
    groups = {}
    for c in single:
        slot = round(((c.left + c.right) / 2 - phase) / pitch)
        center = phase + pitch * slot
        if c.left < center - 0.56 * pitch or c.right > center + 0.56 * pitch:
            return None
        groups.setdefault(slot, []).append(c)
    if max(groups) - min(groups) + 1 != len(groups):
        return None
    indices = {(c.start, c.end): i for i, c in enumerate(cells)}
    labels, shape, heights, _, _, ink, tiles = data
    line, strengths = [], []
    for run in groups.values():
        i = indices.get((run[0].start, run[-1].end))
        if i is None:
            return None
        c = cells[i]
        dh = np.abs((c.bottom - c.top) / pitch - heights[i])
        a, b = shape[i] - 0.14 * dh, ink[i] - 0.11 * dh
        first, other = a.argmax(), b.argmax()
        code = labels[i, first]
        rivals = labels[i] != code
        if code != labels[i, other] or not rivals.any() or a[first] < 0.69 or b[other] < 0.78:
            return None
        alternate = np.where(rivals, a, -np.inf).argmax()
        shape_margin = a[first] - a[alternate]
        ink_margin = b[other] - b[rivals].max()
        if shape_margin < 0.025 or ink_margin < 0.014:
            differences = tiles[i, first] - tiles[i, alternate]
            # A near tie over the whole character needs a distinct local stroke
            # and agreement in the other quarters. A shared component is no vote.
            if (
                min(shape_margin, ink_margin) < 0.006
                or differences.max() < 0.055
                or differences.min() < -0.035
                or (differences >= -0.01).sum() < 3
            ):
                return None
        label = chr(int(code))
        if not ("\u3400" <= label <= "\u9fff" or label in _PUNCTUATION):
            return None
        if label.isalnum():
            strengths.append(a[first])
        line.append(label)
    if len(strengths) < 3 or np.median(strengths) < 0.80:
        return None
    return "".join(line)


@lru_cache(maxsize=26)
def _case_needs_baseline(lower: str) -> bool:
    """Compare size-normalized upper/lower outlines across the installed faces."""
    bank = _bank(False)
    if bank is None:
        return True
    small, capital = bank.labels == ord(lower), bank.labels == ord(lower.upper())
    if not small.any() or not capital.any():
        return True
    shapes = bank.vectors[small] @ bank.vectors[capital].T
    shapes -= 0.095 * np.abs(bank.aspects[small, None] - bank.aspects[capital])
    # If both forms look like the other case at ordinary recognition quality,
    # their absolute height alone cannot establish case on an isolated line.
    return min(np.median(shapes.max(axis=0)), np.median(shapes.max(axis=1))) >= 0.82


def _read(span_count, h, cells, data):
    labels, raw, heights, bottoms, bearings, _, _ = data
    observed_h = np.array([c.bottom - c.top for c in cells], dtype=np.float32)[:, None]
    observed_b = np.array([c.bottom for c in cells], dtype=np.float32)[:, None]
    widths = np.array([c.right - c.left for c in cells], dtype=np.float32)
    starts = [c.start for c in cells]
    ends = [c.end for c in cells]
    # Several baselines cover a line of Chinese, capitals, digits or lowercase
    # with descenders. Strong individual shapes also vote for size and baseline.
    geometries = {(1.08, 0.9), (1.16, 0.92), (1.36, 1.0), (1.36, 0.80), (1.72, 1.0)}
    possible = (raw > 0.82) & (observed_h >= 0.55 * h)
    with np.errstate(divide="ignore"):
        em_guess = observed_h / heights / h
    base_guess = observed_b / h - em_guess * bottoms
    sel = possible & (em_guess > 0.78) & (em_guess < 2.2) & (base_guess > 0.55) & (base_guess < 1.13)
    if np.any(sel):
        bins, counts = np.unique(
            np.column_stack((np.rint(em_guess[sel] * 12), np.rint(base_guess[sel] * 18))),
            axis=0,
            return_counts=True,
        )
        geometries.update((a / 12, b / 18) for a, b in bins[np.argsort(counts)[-7:]])
    candidates = {}
    for scale, baseline in geometries:
        em = scale * h
        dh = np.abs(observed_h / em - heights)
        db = np.abs((observed_b - baseline * h) / em - bottoms)
        score = raw - 0.49 * dh - 0.49 * db
        score[(dh > 0.24) | (db > 0.17)] = -4.0
        threshold = np.where(labels >= 0x3400, 0.78, 0.82)
        plausible = np.flatnonzero(np.any(score >= threshold, axis=1))
        paths = [None] * (span_count + 1)
        paths[0] = (0.0, "", 0.0, 0)
        for i in plausible:
            prev = paths[starts[i]]
            if prev is None:
                continue
            c = cells[i]
            text = prev[1]
            local = score[i]
            if text:
                # The side bearings can distinguish full-width punctuation from
                # ASCII and prevent interpreting a gap inside one glyph as a space.
                gaps = (c.left - prev[3]) / em - prev[2] - bearings[i, :, 0]
                gap_error = np.minimum(np.abs(gaps), np.abs(gaps - 0.30) + 0.05)
                gap_error = np.minimum(gap_error, np.abs(gaps - 1.0) + 0.09)
                local = local - 0.36 * np.minimum(gap_error, 0.45)
            best = int(local.argmax())
            code = labels[i, best]
            primary = local[best]
            competitors = local[labels[i] != code]
            if primary < threshold[i, best] or (competitors.size and primary - competitors.max() < 0.025):
                continue
            if text and gaps[best] > 0.19:
                text += " "
            weight = float(widths[i])
            total = prev[0] + (float(primary) - 0.63) * weight - 0.022 * h
            dest = ends[i]
            if paths[dest] is None or total > paths[dest][0]:
                paths[dest] = (total, text + chr(int(code)), float(bearings[i, best, 1]), c.right)
        result = paths[-1]
        if result is not None:
            text = result[1]
            if not any(char.isalnum() for char in text) or (len(text) == 1 and text in "Il1Oo0"):
                continue
            candidates[text] = max(candidates.get(text, -float("inf")), result[0])
    ordered = sorted(candidates, key=candidates.get, reverse=True)
    if not ordered:
        return None
    if len(ordered) > 1 and candidates[ordered[0]] - candidates[ordered[1]] < 0.025 * h:
        return None
    text = ordered[0]
    if len(text) == 1 and text in string.ascii_letters and _case_needs_baseline(text.lower()):
        return None
    if any("\u3400" <= char <= "\u9fff" for char in text):
        # A horizontal bar or a run of dots can have indistinguishable encodings.
        if "—" in text or "···" in text or "..." in text or any(mark + " " in text for mark in ",:;!?("):
            return None
    return text


@lru_cache(maxsize=32)
def _sized_font(path, face, em):
    return ImageFont.truetype(str(path), em, index=face, layout_engine=ImageFont.Layout.BASIC)


@lru_cache(maxsize=3072)
def _sized_glyph(path, face, em, stroke, char):
    bitmap = _sized_font(path, face, em).getmask(char, mode="L", stroke_width=stroke)
    w, h = bitmap.size
    ink = np.frombuffer(_bitmap_bytes(bitmap), np.uint8).reshape(h, w) > 115
    box = _bounds(ink)
    if box is None:
        return None
    y0, y1, x0, x1 = box
    return _vector(ink[y0:y1, x0:x1]), np.log((x1 - x0) / (y1 - y0)), (y1 - y0) / em, (x1 - x0) / em


@lru_cache(maxsize=1)
def _styled_faces():
    faces = []
    for path, index in _faces()[0]:
        faces.append((path, index))
        # These system collections provide a heavier cut of the same typeface.
        heavy = {"PingFang.ttc": 11, "Hiragino Sans GB.ttc": 2}.get(path.name)
        if heavy is not None:
            faces.append((path, heavy))
    available = []
    for path, face in dict.fromkeys(faces):
        try:
            _sized_font(path, face, _EM)
        except (OSError, ValueError):
            continue
        available.append((path, face))
    return tuple(available)


def _column_strength(ink):
    columns = ink.sum(axis=0, dtype=np.float32)
    return float(np.dot(columns, columns)) / (columns.sum() * ink.shape[0])


def _upright(ink):
    # Test a slant only when it improves the vertical alignment of several stems.
    score = _column_strength(ink)
    winner = ink
    for angle in (-0.24, -0.16, 0.16, 0.24):
        shifts = np.rint((np.arange(ink.shape[0]) - (ink.shape[0] - 1) / 2) * angle).astype(int)
        aligned = np.zeros((ink.shape[0], ink.shape[1] + int(np.ptp(shifts))), bool)
        for y, dx in enumerate(shifts - shifts.min()):
            aligned[y, dx : dx + ink.shape[1]] = ink[y]
        strength = _column_strength(aligned)
        if strength > score:
            score, winner = strength, aligned
    if score < _column_strength(ink) + 0.035:
        return ink
    y0, y1, x0, x1 = _bounds(winner)
    return winner[y0:y1, x0:x1]


def _even_cells(mask, *, unread=False):
    # Small printed CJK and heavy styles can have one-pixel or touching gaps.
    # Require an ink valley at every estimated boundary and account for all ink.
    y0, y1, x0, x1 = _bounds(mask)
    ink = mask[y0:y1, x0:x1]
    height, width = ink.shape
    if not 12 <= height <= 52 or not 1.65 <= width / height <= 8.5:
        return None
    thin = height > 24 and ink.mean() < 0.40
    if thin and not (unread and round(width / height / 1.08) == 2):
        return None
    ink = _upright(ink)
    height, width = ink.shape
    n = round(width / height / 1.08)
    if not 2 <= n <= 8:
        return None
    pitch = (width + 0.08 * height) / n
    if not 0.94 * height <= pitch <= 1.28 * height:
        return None
    columns = ink.sum(axis=0)
    cuts = [0]
    for j in range(1, n):
        center = round(j * pitch - 0.04 * height)
        radius = max(1, round(0.105 * pitch))
        ix = np.arange(max(cuts[-1] + 1, center - radius), min(width - 1, center + radius) + 1)
        if not len(ix):
            return None
        cut = int(ix[np.argmin(columns[ix] + np.abs(ix - center) / pitch * height * 0.55)])
        if columns[cut] > (0 if thin else max(1, 0.16 * height)):
            return None
        cuts.append(cut)
    cuts.append(width)
    cells = []
    for i, (left, right) in enumerate(zip(cuts, cuts[1:])):
        box = _bounds(ink[:, left:right])
        if box is None:
            return None
        y0, y1, x0, x1 = box
        if y1 - y0 < 0.62 * pitch or x1 - x0 < 0.46 * pitch:
            return None
        cells.append(
            _Cell(i, i + 1, left + x0, left + x1, y0, y1, _vector(ink[y0:y1, left + x0 : left + x1]))
        )
    return cells, pitch, thin


def _at_screen_size(cells, pitch, data, current, *, thin_pair=False):
    labels, shape, heights, _, _, ink, _ = data
    candidates = []
    coarse_text = []
    outline_margins = []
    for i, c in enumerate(cells):
        match = 0.47 * shape[i] + 0.53 * ink[i] - 0.20 * np.abs((c.bottom - c.top) / pitch - heights[i])
        order = np.argsort(match)[::-1]
        if match[order[0]] < 0.70:
            return None
        coarse_text.append(chr(int(labels[i, order[0]])))
        opponents = match[labels[i] != labels[i, order[0]]]
        outline_margins.append(float(match[order[0]] - opponents.max()) if opponents.size else 0.0)
        options = {}
        for ix in order:
            code = int(labels[i, ix])
            if 0x3400 <= code <= 0x9FFF and code not in options:
                options[code] = float(match[ix])
                if len(options) == 6:
                    break
        if len(options) < 2:
            return None
        candidates.append(options)

    styles = []
    pair_votes = []
    faces = _styled_faces()
    refs = [(p, f, round(pitch), sw) for p, f in faces for sw in ((0, 1) if pitch >= 24 else (0,))]
    if len(refs) == 1:
        p, f, em, sw = refs[0]
        refs.append((p, f, em + 1, sw))
    for path, face, em, stroke in refs:
        text, scores, margins = [], [], []
        for c, choices in zip(cells, candidates):
            h, w = (c.bottom - c.top) / pitch, (c.right - c.left) / pitch
            aspect = np.log(w / h)
            ranked = []
            for code, prior in choices.items():
                try:
                    template = _sized_glyph(path, face, em, stroke, chr(code))
                except (OSError, ValueError):
                    template = None
                if template is None:
                    continue
                vector, ratio, th, tw = template
                score = (
                    0.78 * float(np.dot(c.vector, vector))
                    + 0.22 * prior
                    - 0.08 * abs(aspect - ratio)
                    - 0.14 * abs(h - th)
                    - 0.10 * abs(w - tw)
                )
                ranked.append((score, code))
            if len(ranked) < 2:
                break
            ranked.sort(reverse=True)
            scores.append(ranked[0][0])
            margins.append(ranked[0][0] - ranked[1][0])
            text.append(chr(ranked[0][1]))
        if len(text) == len(cells):
            styles.append((float(np.mean(scores)), "".join(text), min(scores), min(margins)))
            if len(cells) == 2 and min(scores) >= 0.59 and min(margins) >= 0.035:
                pair_votes.append((styles[-1][0], styles[-1][1], path))
    if not styles:
        return None
    styles.sort(reverse=True)
    quality, text, weakest, margin = styles[0]
    agrees = any(other == text and score >= quality - 0.07 for score, other, _, _ in styles[1:])
    tied = any(other != text and score >= quality - 0.008 for score, other, _, _ in styles[1:])
    # A nearly identical native-size rendering can resolve an earlier mistake
    # made by the scaled bank, even if a neighboring character topped that bank.
    exact = quality >= 0.93 and weakest >= 0.90 and margin >= 0.09 and agrees and not tied
    if exact:
        return text
    if thin_pair and min(outline_margins) < 0.045:
        return None
    # Thin one-pixel strokes can score poorly on exact overlap. When there are
    # two separated, square glyphs, demand independent typefaces agreeing on
    # both letters with a clear lead over every competing character.
    if (
        len(cells) == 2
        and current is None
        and text == "".join(coarse_text)
        and cells[1].left - cells[0].right >= max(1, 0.055 * pitch)
        and min(outline_margins) >= 0.045
        and weakest >= 0.60
        and not tied
    ):
        votes = [(name, face) for score, name, face in pair_votes if score >= quality - 0.08]
        if (
            len(votes) >= 3
            and all(name == text for name, face in votes)
            and len({face for name, face in votes}) >= 2
        ):
            return text
    certain = quality >= 0.77 and weakest >= 0.70 and margin >= 0.025 and agrees and not tied
    if certain and text == "".join(coarse_text) and current in (None, text):
        return text
    if current and current == text and margin >= 0.033 and not tied:
        return current
    return None


def recognize_masks(masks: list[np.ndarray]) -> list[str | None]:
    """Read complete lines of foreground ink, preserving order and abstaining on uncertainty."""
    result: list[str | None] = [None] * len(masks)
    lines = []
    cells = []
    for index, mask in enumerate(masks[:_MAX_LINES]):
        parsed = _cells(mask)
        if parsed is None:
            continue
        count, height, row = parsed
        if len(cells) + len(row) > _MAX_CELLS:
            continue
        start = len(cells)
        cells.extend(row)
        lines.append((index, count, height, slice(start, len(cells))))
    if cells and (data := _matches(cells)) is not None:
        for index, count, height, group in lines:
            subset = tuple(x[group] for x in data)
            spaced = _grid_read(height, cells[group], subset)
            free = _read(count, height, cells[group], subset)
            if not (spaced and free and spaced != free):
                result[index] = spaced or free
    refinement = []
    fine_cells = []
    for index, mask in enumerate(masks[:_MAX_LINES]):
        if (
            not isinstance(mask, np.ndarray)
            or mask.ndim != 2
            or mask.dtype != np.bool_
            or mask.size > 800_000
            or not mask.size
            or not mask.any()
        ):
            continue
        current = result[index]
        if current and not all("\u3400" <= ch <= "\u9fff" for ch in current):
            continue
        fitted = _even_cells(mask, unread=current is None)
        if not fitted:
            continue
        row, pitch, thin = fitted
        if len(fine_cells) + len(row) > 40:
            break
        start = len(fine_cells)
        fine_cells.extend(row)
        refinement.append((index, pitch, slice(start, len(fine_cells)), thin))
    if fine_cells and (fine := _matches(fine_cells)) is not None:
        for index, pitch, group, thin in refinement:
            subset = tuple(x[group] for x in fine)
            verified = _at_screen_size(fine_cells[group], pitch, subset, result[index], thin_pair=thin)
            # Ink bounds omit the side bearings. For a thin pair that still
            # abstained, accept a higher pitch only if two neighboring sizes
            # independently recover the same complete word.
            if thin and not verified:
                nearby = _at_screen_size(fine_cells[group], pitch + 1, subset, None, thin_pair=True)
                if nearby and nearby == _at_screen_size(
                    fine_cells[group], pitch + 2, subset, None, thin_pair=True
                ):
                    verified = nearby
            if verified or (result[index] and pitch >= 24):
                result[index] = verified
    return result
