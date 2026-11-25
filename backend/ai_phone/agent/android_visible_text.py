"""Read glyph-shaped lines in Android screenshots and associate them with evidenced controls."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re

import numpy as np
from PIL import Image, ImageDraw

from .android_visual_regions import _components


Rect = tuple[int, int, int, int]


def _area(r):
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _overlap(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _inside(a, b):
    return _overlap(a, b) >= 0.96 * _area(a)


def _same(a, b, threshold=0.78):
    return _overlap(a, b) >= threshold * max(_area(a), _area(b))


def _rect(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    left, top, right, bottom = map(int, value)
    box = max(0, left), max(0, top), min(size[0], right), min(size[1], bottom)
    return box if _area(box) else None


@dataclass(frozen=True)
class _Panel:
    box: Rect
    background: int


def _luma(color):
    return round(float(np.dot(color, (0.299, 0.587, 0.114))))


def _panels(image):
    # Recover a visible card's own flat surface. No relationship is inferred
    # solely because a caption is beside a thumbnail or shares a column.
    width, height = image.size
    scale = max(1, width / 480, height / 980)
    sw, sh = max(1, round(width / scale)), max(1, round(height / scale))
    small = np.asarray(image.resize((sw, sh), Image.Resampling.BOX), dtype=np.int16)
    samples = small[::3, ::3].reshape(-1, 3)
    bins = samples.astype(np.int32) // 8
    packed = (bins[:, 0] * 32 + bins[:, 1]) * 32 + bins[:, 2]
    counts = np.bincount(packed, minlength=32768)
    panels, colors = [], []
    for key in np.argsort(counts)[-9:][::-1]:
        if counts[key] < len(samples) * 0.009:
            break
        color = np.median(samples[packed == key], axis=0).astype(np.int16)
        if any(np.max(np.abs(color - other)) <= 5 for other in colors):
            continue
        colors.append(color)
        mask = np.max(np.abs(small - color), axis=2) <= 5
        for box, pixels in _components(Image.fromarray(mask.astype(np.uint8) * 255)):
            if box.width < 19 or box.height < 10 or box.area > sw * sh * 0.69 or pixels < 0.32 * box.area:
                continue
            # Continuous sides distinguish the surface from disconnected white
            # areas in photographs and from the background wrapping the page.
            crop = mask[box.top : box.bottom, box.left : box.right]
            dx, dy = max(1, box.width // 10), max(1, box.height // 10)
            central_x, central_y = crop[:, dx:-dx], crop[dy:-dy, :]
            sides = sorted(
                (
                    central_x[:3].any(axis=0).mean(),
                    central_x[-3:].any(axis=0).mean(),
                    central_y[:, :3].any(axis=1).mean(),
                    central_y[:, -3:].any(axis=1).mean(),
                )
            )
            if sides[0] < 0.43 or sides[1] < 0.71:
                continue
            rect = (
                round(box.left * width / sw),
                round(box.top * height / sh),
                round(box.right * width / sw),
                round(box.bottom * height / sh),
            )
            if not any(_same(rect, p.box, 0.93) for p in panels):
                panels.append(_Panel(rect, _luma(color)))
    return sorted(panels, key=lambda panel: _area(panel.box))


def _spans(active, max_gap):
    idx = np.flatnonzero(active)
    if not len(idx):
        return []
    gaps = np.flatnonzero(np.diff(idx) > max_gap + 1) + 1
    return [(int(part[0]), int(part[-1]) + 1) for part in np.split(idx, gaps)]


@dataclass
class _Line:
    box: Rect
    mask: np.ndarray
    contrast: float
    regularity: float = 0.0
    alternate: np.ndarray | None = None


def _dominant_luma(patch):
    sample = patch[:: max(1, patch.shape[0] // 50), :: max(1, patch.shape[1] // 70)].ravel()
    bins = sample // 8
    most = np.bincount(bins, minlength=32).argmax()
    return int(np.median(sample[bins == most]))


def _lines(gray, rgb, rect, background, obstacles, protected, density):
    left, top, right, bottom = rect
    patch = gray[top:bottom, left:right]
    if background is None:
        background = _dominant_luma(patch)
    dark_ink = background >= 120
    threshold = max(27, round(0.16 * max(background, 255 - background)))
    mask = patch < background - threshold if dark_ink else patch > background + threshold
    for x, y, r, b in obstacles:
        if _overlap(rect, (x, y, r, b)):
            mask[
                max(0, y - top) : min(bottom, b) - top,
                max(0, x - left) : min(right, r) - left,
            ] = False
    mask[[0, -1], :] = False
    mask[:, [0, -1]] = False
    # A picture border or a divider spanning multiple text lines joins their
    # row projections. Retain the original pixels of character-sized components
    # before estimating baselines. This also keeps thin punctuation beside words.
    cleaned = np.zeros_like(mask)
    max_height = max(68, 44 * density)
    for part, _ in _components(Image.fromarray(mask.astype(np.uint8) * 255)):
        if part.height > max_height or part.width > max(12 * density, 5 * part.height):
            continue
        section = np.s_[part.top : part.bottom, part.left : part.right]
        cleaned[section] |= mask[section]
    mask = cleaned
    for y1, y2 in _spans(mask.sum(axis=1) >= 2, max(1, round(density * 0.7))):
        line_h = y2 - y1
        if not max(6, 4 * density) <= line_h <= max(68, 44 * density):
            continue
        strip = mask[y1:y2]
        for x1, x2 in _spans(strip.any(axis=0), max(2, round(line_h * 0.56))):
            if x2 - x1 < line_h * 0.55:
                continue
            rough = strip[:, x1:x2]
            rows = np.flatnonzero(rough.any(axis=1))
            y0, y3 = y1 + int(rows[0]), y1 + int(rows[-1]) + 1
            box = left + x1, top + y0, left + x2, top + y3
            if y3 - y0 < max(6, 4 * density) or any(_overlap(box, q) for q in protected):
                continue
            fg = patch[y0:y3, x1:x2]
            support = mask[y0:y3, x1:x2]
            colors = fg[support]
            core = float(np.percentile(colors, 20 if dark_ink else 80))
            contrast = abs(background - core)
            ink_rgb = rgb[box[1] : box[3], box[0] : box[2]]
            # Use the original antialiased glyph contours. Preprocessing above
            # locates the line; it must not remove small strokes from a glyph.
            channel = ink_rgb.max(axis=2) if dark_ink else ink_rgb.min(axis=2)
            dark, light = np.percentile(channel, [12, 88])
            if light - dark < 55:
                channel = fg
                dark, light = np.percentile(channel, [12, 88])
            cutoff = dark + 0.34 * (light - dark) if dark_ink else light - 0.34 * (light - dark)
            strokes = (channel <= cutoff) if dark_ink else (channel >= cutoff)
            ratio = strokes.mean()
            if not 0.06 <= ratio <= 0.68 or contrast < 36:
                continue
            # Flat-colour text has a common stroke colour. Matching arbitrary
            # photographic detail glyph by glyph produces plausible nonsense.
            ink_colors = ink_rgb[strokes]
            ink_center = np.median(ink_colors, axis=0)
            if np.mean(np.max(np.abs(ink_colors - ink_center), axis=1) < 40) < 0.73:
                continue
            yield _Line(box, strokes, contrast)


def _isolated_ink(mask, box, margin):
    # A white glyph with a dark keyline on white paper has the *same colour*
    # as the paper. Its ink is enclosed while the paper reaches the crop edge.
    left, top, right, bottom = box
    h, w = mask.shape
    x0, y0 = max(0, left - margin), max(0, top - margin)
    patch = mask[y0 : min(h, bottom + margin), x0 : min(w, right + margin)]
    padded = np.pad(patch, 1, constant_values=True)
    img = Image.fromarray(padded.astype(np.uint8) * 255)
    ImageDraw.floodfill(img, (0, 0), 0)
    clipped = np.asarray(img)[top - y0 + 1 : bottom - y0 + 1, left - x0 + 1 : right - x0 + 1] > 0
    return clipped


def _complete_ends(mask, box):
    left, top, right, bottom = box
    h = bottom - top
    # Components establish the baseline. Inspect the original projected ink
    # beside it as well: a character can consist entirely of detached strokes.
    x0, x1 = max(0, left - 7 * h), min(mask.shape[1], right + 7 * h)
    y0, y1 = max(0, top - round(0.13 * h)), min(mask.shape[0], bottom + round(0.13 * h))
    ink = _isolated_ink(mask, (x0, y0, x1, y1), max(2, round(0.16 * h)))
    spans = _spans(ink.any(axis=0), 1)
    densities = []
    for a, b in spans:
        if left <= a + x0 and b + x0 <= right and b - a >= 0.45 * h:
            section = ink[:, a:b]
            rows = np.flatnonzero(section.any(axis=1))
            if rows[-1] - rows[0] >= 0.53 * h:
                densities.append(section.sum() / ((b - a) * (rows[-1] - rows[0] + 1)))
    min_fill = max(0.19, 0.62 * float(np.median(densities))) if densities else 0.22
    for side in (-1, 1):
        for a, b in reversed(spans) if side < 0 else spans:
            start, end = a + x0, b + x0
            if (side < 0 and start >= left) or (side > 0 and end <= right):
                continue
            gap = left - end if side < 0 else start - right
            if gap > max(2, 0.34 * h):
                break
            section = ink[:, a:b]
            rows = np.flatnonzero(section.any(axis=1))
            height, width = int(rows[-1] - rows[0] + 1), b - a
            fill = section.sum() / (width * height)
            narrow_bar = 0.70 * h <= width <= 1.20 * h and height <= 0.20 * h and fill >= 0.50
            character = 0.47 * h <= height <= 1.24 * h and 0.14 * h <= width <= 1.40 * h and fill >= min_fill
            if not (character or narrow_bar):
                break
            left, right = min(left, start), max(right, end)
            top, bottom = (
                min(top, y0 + int(rows[0])),
                max(bottom, y0 + int(rows[-1]) + 1),
            )
    return left, top, right, bottom


def _ink_bands(mask, gray, density, protected):
    """Find repeatable character-scale strokes on a baseline of their own colour."""
    parts = _components(Image.fromarray(mask.astype(np.uint8) * 255))
    min_h = max(6, min(12, round(3.0 * density)))
    max_h = max(68, round(44 * density))
    parts = [
        (p, n)
        for p, n in parts
        if 0 < p.height <= max_h and p.width <= max(7, p.height * 3) and n >= 2 and n < p.area * 0.97
    ]
    anchors = [
        (p, n)
        for p, n in parts
        if p.height >= 0.56 * min_h
        and 0.18 * p.height <= p.width <= 1.85 * p.height
        and n >= max(6, 0.19 * p.area)
    ]
    # Look across a strip only at strokes with comparable height and baseline.
    # Rows from a background illustration or an adjoining icon cannot join a
    # word by a single pixel somewhere above or below its printed line.
    step = max(8, round(6 * density))
    strips = defaultdict(list)
    pieces = defaultdict(list)
    for p, n in parts:
        pieces[((p.top + p.bottom) // 2) // step].append(p)
    for p, n in anchors:
        strips[((p.top + p.bottom) // 2) // step].append(p)
    emitted, scanned = [], set()
    for seed, _ in sorted(anchors, key=lambda row: -row[0].height):
        h = seed.height
        if h < min_h:
            break
        tol = max(2, round(0.14 * h))
        # Similar components on one baseline lead to the same candidate run.
        key = (round(seed.top / tol), round(seed.bottom / tol), tol)
        if key in scanned:
            continue
        scanned.add(key)
        y0, y1 = seed.top - tol, seed.bottom + tol
        local = [
            p
            for s in range(max(0, y0 // step), y1 // step + 1)
            for p in strips[s]
            if y0 <= p.top
            and p.bottom <= y1
            and p.height >= 0.46 * h
            and min(abs(p.bottom - seed.bottom), abs(p.top - seed.top)) <= tol
        ]
        local.sort(key=lambda p: p.left)
        runs = []
        right = -max_h
        for p in local:
            if p.left - right > max(2, round(0.47 * h)):
                runs.append([])
            runs[-1].append(p)
            right = max(right, p.right)
        for run in runs:
            if len(run) < 2:
                continue
            left, right = min(p.left for p in run), max(p.right for p in run)
            if not 1.48 * h <= right - left <= 32 * h:
                continue
            aligned = [p for p in run if p.height >= 0.65 * h and p.width >= 0.29 * h]
            if len(aligned) < 2:
                continue
            # Recover detached radicals, diacritics and strokes, but only from
            # between the ends established by the aligned printed components.
            run_top, run_bottom = min(p.top for p in run), max(p.bottom for p in run)
            siblings = [
                p
                for s in range(max(0, y0 // step), y1 // step + 1)
                for p in pieces[s]
                if p.right >= left
                and p.left <= right
                and p.top >= y0
                and p.bottom <= y1
                and p.left >= left - max(2, 0.15 * h)
                and p.right <= right + max(2, 0.15 * h)
            ]
            if siblings:
                left, right = (
                    min(left, min(p.left for p in siblings)),
                    max(right, max(p.right for p in siblings)),
                )
                run_top = min(run_top, min(p.top for p in siblings))
                run_bottom = max(run_bottom, max(p.bottom for p in siblings))
            box = _complete_ends(mask, (left, run_top, right, run_bottom))
            if any(_overlap(box, q) for q in protected) or any(_same(box, prev, 0.89) for prev in emitted):
                continue
            left, run_top, right, run_bottom = box
            ink = _isolated_ink(mask, box, max(2, round(0.16 * h)))
            if not 0.12 <= ink.mean() <= 0.78:
                continue
            ys, xs = np.nonzero(ink)
            x, y, r, b = (
                int(xs.min()),
                int(ys.min()),
                int(xs.max() + 1),
                int(ys.max() + 1),
            )
            box = left + x, run_top + y, left + r, run_top + b
            if box[2] - box[0] < 1.48 * (box[3] - box[1]):
                continue
            ink = ink[y:b, x:r]
            if ink.all():
                continue
            patch = gray[box[1] : box[3], box[0] : box[2]]
            contrast = float(abs(np.median(patch[ink]) - np.median(patch[~ink])))
            # Irregular fragments in artwork get a lower reading priority than
            # successive strong glyphs on a shared baseline.
            regularity = min(1.0, sum(p.width for p in aligned) / (right - left))
            emitted.append(box)
            yield _Line(box, ink, contrast, regularity)


def _colour_lines(rgb, gray, density, protected):
    # Bright neutral infill and dark strokes remain measurable even on a
    # gradient or photograph. The intermediate layer also retains pale print
    # whose contrast against white paper is too small for a dark-ink mask.
    high, low = rgb.max(axis=2), rgb.min(axis=2)
    for layer, channel, light in (
        (low >= 188, low, True),
        (high <= 129, high, False),
        ((high <= 207) & (high - low <= 26), gray, False),
    ):
        for line in _ink_bands(layer, gray, density, protected):
            x, y, r, b = line.box
            pad = max(3, round(0.17 * (b - y)))
            x0, y0 = max(0, x - pad), max(0, y - pad)
            point = channel[y:b, x:r]
            foreground, surround = (
                np.median(point[line.mask]),
                np.median(point[~line.mask]),
            )
            separation = foreground - surround if light else surround - foreground
            if separation > 24:
                # Use the line's own antialias transition for a second contour
                # on sloped or bold text, where a one-pixel change alters joins.
                cutoff = foreground - 0.34 * separation if light else foreground + 0.34 * separation
                patch = channel[y0 : b + pad, x0 : r + pad]
                view = patch >= cutoff if light else patch <= cutoff
                other = _isolated_ink(view, (x - x0, y - y0, r - x0, b - y0), pad)
                if (
                    np.count_nonzero(other ^ line.mask) > 0.015 * line.mask.size
                    and 0.10 < other.mean() < 0.77
                ):
                    line.alternate = other
            yield line


def _controls(targets, size):
    controls = []
    for target in targets:
        box = _rect(target.get("bounds"), size)
        if (
            box
            and target.get("point")
            and target.get("id")
            and any(a in ("tap", "try_tap", "input") for a in target.get("actions", ()))
        ):
            controls.append((target, box))
    return controls


def _bind_system_panels(targets, records, panels, protected, gray, size):
    """Bind unowned system labels to their measured panel, keeping ambiguous titles blank."""
    if not records:
        return
    controls = _controls(targets, size)
    surface = size[0] * size[1]
    next_index = 1 + max((int(t["id"][1:]) for t in targets if re.fullmatch(r"V\d+", t["id"])), default=0)
    free = [
        (row, box) for row in records if not row.get("target") and (box := _rect(row.get("bounds"), size))
    ]
    for panel in sorted(panels, key=lambda p: _area(p.box)):
        rect = panel.box
        if _area(rect) >= 0.55 * surface or any(_overlap(rect, other) for other in protected):
            continue
        rows = [
            (row, b)
            for row, b in free
            if not row.get("target") and _inside(b, rect) and _area(b) < 0.75 * _area(rect)
        ]
        if not rows:
            continue
        colocated = [(t, b) for t, b in controls if _same(rect, b, 0.82)]
        if len(colocated) > 1:
            continue
        target = colocated[0][0] if colocated else None
        # Nested or intersecting sibling buttons prevent assigning their labels to the whole card.
        if any(t is not target and not _inside(rect, b) and _overlap(b, rect) for t, b in controls):
            continue
        rows.sort(key=lambda pair: (pair[1][1], pair[1][0]))
        title, title_box = rows[0]
        others = [(row, b) for row, b in rows[1:] if (row["text"], b) != (title["text"], title_box)]
        if others:

            def emphasis(box):
                left, top, right, bottom = box
                contrast = np.abs(gray[top:bottom, left:right].astype(np.int16) - panel.background)
                strength = float(np.percentile(contrast, 92))
                ink = float(np.mean(contrast > max(12, strength * 0.5)))
                return (bottom - top) * (0.65 + min(180, strength) / 350) * (1 + min(0.5, ink))

            if any(b[1] < title_box[3] - 1 for _, b in others) or emphasis(title_box) < 1.08 * max(
                emphasis(b) for _, b in others
            ):
                title = None
        if target is None:
            # An anonymous colour patch alone cannot provide an action. Here an independently
            # detected physical panel encloses a title and a subordinate line of system text.
            if not title or not others or len(targets) >= 512:
                continue
            target = {
                "id": f"V{next_index}",
                "source": "pixels",
                "role": "enclosed_surface",
                "name": None,
                "name_source": None,
                "bounds": list(rect),
                "state": {},
                "point": [(title_box[0] + title_box[2]) // 2, (title_box[1] + title_box[3]) // 2],
                "actions": ["try_tap"],
            }
            targets.append(target)
            controls.append((target, rect))
            next_index += 1
        for row, _ in rows:
            row["target"] = target["id"]
        if title and not target.get("name"):
            target.update(
                name=title["text"],
                name_source="system",
                name_bounds=list(title_box),
                system_name_source="contained_text",
            )


def _reading_zones(panel, inner, density):
    # A framed graphic supplies a boundary on any of its four sides. Read the
    # clear neighbouring space *inside the same physical panel* independently;
    # the moving content of a thumbnail must not merge successive text rows.
    left, top, right, bottom = panel
    x, y, r, b = inner
    gap = max(2, round(2.8 * density), round(0.075 * min(r - x, b - y)))
    left, top, right, bottom = left + gap, top + gap, right - gap, bottom - gap
    options = [
        (left, top, x - gap, bottom),
        (r + gap, top, right, bottom),
        (left, top, right, y - gap),
        (left, b + gap, right, bottom),
    ]
    return [q for q in options if q[2] - q[0] >= 13 * density and q[3] - q[1] >= 6 * density]


def _owners(box, controls, panels, screen_area):
    direct = [(t, b) for t, b in controls if _inside(box, b) and _area(b) < screen_area * 0.65]
    direct.sort(key=lambda row: _area(row[1]))
    if direct:
        target, control_box = direct[0]
        # Overlapping siblings do not establish ownership. Nested independent
        # children own their own text; a broad panel cannot borrow their names.
        if any(not _inside(control_box, outer) or _same(outer, control_box) for _, outer in direct[1:]):
            return None, None
        if any(
            t is not target and _inside(b, control_box) and not _same(b, control_box) for t, b in controls
        ):
            return None, None
        return target, None
    for panel in panels:
        if not _inside(box, panel.box):
            continue
        nested = [(t, b) for t, b in controls if _inside(b, panel.box)]
        if len(nested) != 1:
            # A larger ancestor cannot resolve two independent controls in the
            # same panel, or show that an unrelated nearby one owns a free label.
            if len(nested) > 1:
                return None, None
            continue
        target, child = nested[0]
        if (
            target.get("source") == "pixels"
            and 0.085 <= _area(child) / _area(panel.box) <= 0.84
            and not _overlap(box, child)
        ):
            return target, panel.box
    return None, None


def _system_has_line(rect, text, labels):
    line_height = rect[3] - rect[1]
    reading = "".join(text.split()).casefold()
    for box, label in labels:
        if _same(rect, box, 0.62):
            return True
        if not _inside(rect, box) or line_height < 0.28 * (box[3] - box[1]):
            continue
        if _in_system_name(text, label):
            return True
        # Android sometimes uses the entire button as the bounds of its label.
        # A nearly identical reading centered on that label adds no second line.
        name = "".join(label.split()).casefold()
        if (
            len(reading) == len(name) >= 4
            and abs(rect[1] + rect[3] - box[1] - box[3]) <= line_height
            and abs(rect[0] + rect[2] - box[0] - box[2]) <= line_height
            and sum(a != b for a, b in zip(reading, name)) == 1
        ):
            return True
    return False


def _in_system_name(text, name):
    return bool(name and "".join(text.split()).casefold() in "".join(name.split()).casefold())


def _fragment(line, target, established):
    left, top, right, bottom = line.box
    h = bottom - top
    for other, owner, _ in established:
        if other is line or owner is not target:
            continue
        x, y, r, b = other.box
        if (
            abs(y - top) <= 0.20 * h
            and abs(b - bottom) <= 0.20 * h
            and x <= left + 0.1 * h
            and r >= right - 0.1 * h
            and (left - x >= 0.68 * h or r - right >= 0.68 * h)
        ):
            return True
    return False


def _page_content(line, target, outer, size):
    if outer or target is None:
        return False
    box = _rect(target.get("bounds"), size)
    h, w = line.box[3] - line.box[1], line.box[2] - line.box[0]
    # Text deep inside an image or document may be perfectly legible without
    # providing a name for the whole interactive preview surface.
    return bool(
        box
        and _area(box) > 0.045 * size[0] * size[1]
        and box[3] - box[1] > 6 * h
        and box[2] - box[0] > max(5 * w, 14 * h)
    )


def _prominence(line):
    return (line.box[3] - line.box[1]) * (0.65 + 0.35 * min(1, line.contrast / 135))


def _number_unowned(lines, targets, size):
    occupied = []
    for target in targets:
        box = _rect(target.get("bounds"), size)
        actions = target.get("actions", ())
        if not box or ("scroll" in actions and _area(box) >= 0.78 * size[0] * size[1]):
            continue
        if any(a in ("tap", "input", "try_tap") for a in actions):
            occupied.append(box)
    ids = {target.get("id") for target in targets}
    serial = 0
    for row in lines:
        if len(targets) >= 512:
            break
        box = row["bounds"]
        if (
            row["target"]
            or not any(c.isalnum() for c in row["text"])
            or any(_overlap(box, other) for other in occupied)
        ):
            continue
        serial += 1
        while f"T{serial}" in ids:
            serial += 1
        identifier = f"T{serial}"
        row["target"] = identifier
        targets.append(
            {
                "id": identifier,
                "role": "visible_text",
                "source": "pixels",
                "name": row["text"],
                "name_source": "screen",
                "name_bounds": list(box),
                "actions": ["try_tap"],
                "bounds": list(box),
                "point": [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2],
                "state": {},
            }
        )
        occupied.append(box)


def _system_text_context(image, targets, excluded, system_records):
    protected = [q for value in excluded if (q := _rect(value, image.size))]
    for target in targets:
        if target.get("state", {}).get("password") or target.get("password"):
            box = _rect(target.get("bounds"), image.size)
            if box:
                protected.append(box)
    gray = np.asarray(image.convert("L"))
    panels = _panels(image)
    _bind_system_panels(targets, system_records, panels, protected, gray, image.size)
    return protected, gray, panels


def associate_system_text(image, targets, excluded, system_records):
    """Associate existing Android text with cards using geometry and contrast."""
    if any(not row.get("target") for row in system_records):
        _system_text_context(image, targets, excluded, system_records)


def enrich_from_pixels(
    image: Image.Image,
    targets: list[dict],
    excluded: list[Rect],
    system_text_boxes: list[tuple[str, Rect]],
    system_records: list[dict] | None = None,
    *,
    number_unowned: bool = True,
) -> list[dict]:
    """Read original pixel lines. Only a control or its measured visual card can own a name."""
    from .android_glyphs import recognize_masks

    image = image.convert("RGB")
    width, height = image.size
    if width < 6 or height < 6:
        return []
    screen = (0, 0, width, height)
    protected, gray, panels = _system_text_context(image, targets, excluded, system_records)
    rgb = np.asarray(image)
    controls = [
        (t, b)
        for t, b in _controls(targets, image.size)
        if not any(p[0] <= t["point"][0] < p[2] and p[1] <= t["point"][1] < p[3] for p in protected)
    ]
    system_labels = [(q, label) for label, b in system_text_boxes if label and (q := _rect(b, image.size))]
    # Each independent control, measured surface, and the remaining page get a
    # turn. Interior pictures and child controls cannot merge two adjacent lines.
    regions = [(p.box, p.background) for p in panels]
    regions.extend((box, None) for _, box in controls if not any(_same(box, p.box) for p in panels))
    regions.append((screen, None))
    density = max(0.7, min(width, height) / 432)
    lines = []
    geometries = list(dict.fromkeys(q for q, _ in regions))
    for _, child in controls:
        parent = next(
            (p for p in panels if _inside(child, p.box) and 0.085 <= _area(child) / _area(p.box) <= 0.84),
            None,
        )
        if parent:
            regions.extend((q, parent.background) for q in _reading_zones(parent.box, child, density))
    for region, background in regions:
        if any(_inside(region, p) for p in protected):
            continue
        blockers = [
            q
            for q in geometries
            if _area(q) < 0.79 * _area(region)
            and _inside(q, region)
            and _area(q) >= min(90 * density * density, _area(region) * 0.07)
        ]
        for line in _lines(gray, rgb, region, background, blockers, protected, density):
            if not any(_same(line.box, prev.box, 0.78) for prev in lines):
                lines.append(line)
    ranked_lines = [(line, *_owners(line.box, controls, panels, width * height)) for line in lines]

    # Spend the bounded recognition batch on the primary text of unlabelled
    # controls first. Illustration fragments and free text follow it.
    def priority(entry):
        line, target, outer = entry
        rank = (
            0
            if outer and target and not target.get("name")
            else (1 if target and not target.get("name") else 2)
        )
        return rank, -_prominence(line), line.box[1], line.box[0]

    ranked_lines.sort(key=priority)
    words = recognize_masks([line.mask for line, _, _ in ranked_lines])
    aligned = [
        (line, *_owners(line.box, controls, panels, width * height))
        for line in _colour_lines(rgb, gray, density, protected)
    ]
    # Try the independent, colour-isolated readings after normal control text
    # has had its batch. Give a clean row priority over interior art fragments.
    aligned.sort(
        key=lambda entry: (
            priority(entry)[0],
            -entry[0].regularity,
            -_prominence(entry[0]),
            entry[0].box[1],
            entry[0].box[0],
        )
    )
    recognized = [(entry, text) for entry, text in zip(ranked_lines, words) if text and text.strip()]
    extra = [
        entry
        for entry in aligned
        if not any(_same(entry[0].box, prior[0].box, 0.85) for prior, _ in recognized)
    ]
    more = recognize_masks([line.mask for line, _, _ in extra])
    retry = [
        (i, entry[0].alternate)
        for i, entry in enumerate(extra[:48])
        if not more[i] and entry[0].alternate is not None
    ]
    for (index, _), word in zip(retry, recognize_masks([m for _, m in retry])):
        more[index] = word
    recognized.extend((entry, text) for entry, text in zip(extra, more) if text and text.strip())
    established = ranked_lines + [entry for entry in aligned if entry[0].regularity >= 0.66]
    owned, result = {}, []
    for (line, target, outer), text in recognized:
        if _fragment(line, target, established):
            continue
        content = _page_content(line, target, outer, image.size)
        if _system_has_line(line.box, text, system_labels) or (
            target
            and (target.get("name") == text or (not content and _in_system_name(text, target.get("name"))))
        ):
            continue
        if any(_same(line.box, prev["bounds"], 0.76) for prev in result):
            continue
        result.append(
            {
                "text": text,
                "bounds": list(line.box),
                "target": target["id"] if target else None,
            }
        )
        if target is not None and not target.get("name") and not content:
            owned.setdefault(target["id"], []).append((line, text, outer, target))
    for rows in owned.values():
        outside = [row for row in rows if row[2] is not None]
        choices = outside or rows
        ranked = sorted(choices, key=lambda row: _prominence(row[0]), reverse=True)
        if len(ranked) > 1 and _prominence(ranked[0][0]) < 1.19 * _prominence(ranked[1][0]):
            continue
        line, text, outer, target = ranked[0]
        target.update(name=text, name_source="screen", name_bounds=list(line.box))
        if (
            outer
            and outer[0] > 0
            and outer[1] > 0
            and outer[2] < width
            and outer[3] < height
            and not any(_overlap(outer, p) for p in protected)
        ):
            target["bounds"] = list(outer)
    from .android_numeric_text import read_large_numbers

    for text, box in read_large_numbers(image, protected):
        target, _ = _owners(box, controls, panels, width * height)
        if _system_has_line(box, text, system_labels) or (target and _in_system_name(text, target.get("name"))):
            continue
        if not any(_overlap(box, row["bounds"]) for row in result):
            result.append({"text": text, "bounds": list(box), "target": target["id"] if target else None})
    result.sort(key=lambda row: (row["bounds"][1], row["bounds"][0]))
    if number_unowned:
        _number_unowned(result, targets, image.size)
    return result
