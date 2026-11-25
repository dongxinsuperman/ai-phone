"""Find unlabelled regions in an Android screenshot using Pillow.

These regions carry a tentative tap action. Shape and alignment provide a
location but cannot establish what it does, whether it responds, or its name.
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from itertools import chain
from statistics import median

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat


@dataclass(frozen=True)
class _Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    @property
    def area(self):
        return self.width * self.height

    @property
    def center(self):
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def intersection(self, other):
        w = min(self.right, other.right) - max(self.left, other.left)
        h = min(self.bottom, other.bottom) - max(self.top, other.top)
        return max(0, w) * max(0, h)

    def union(self, other):
        return _Box(min(self.left, other.left), min(self.top, other.top),
                    max(self.right, other.right), max(self.bottom, other.bottom))

    def tuple(self):
        return self.left, self.top, self.right, self.bottom


_RUN = re.compile(b"\xff+")


def _components(mask):
    """Label adjacent horizontal runs, returning their bounds and pixel counts."""
    width, height = mask.size
    raw = mask.tobytes()
    parent, stats = [], []
    previous = []

    def root(index):
        while index != parent[index]:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for y in range(height):
        start = y * width
        current = []
        pos = 0
        for match in _RUN.finditer(raw, start, start + width):
            left, right = match.start() - start, match.end() - start
            while pos < len(previous) and previous[pos][1] < left:
                pos += 1
            idx = pos
            label = None
            while idx < len(previous) and previous[idx][0] <= right:
                neighbour = root(previous[idx][2])
                if label is None:
                    label = neighbour
                elif label != neighbour:
                    parent[neighbour] = label
                    into, other = stats[label], stats[neighbour]
                    into[0] = min(into[0], other[0])
                    into[1] = min(into[1], other[1])
                    into[2] = max(into[2], other[2])
                    into[3] = max(into[3], other[3])
                    into[4] += other[4]
                idx += 1
            if label is None:
                label = len(parent)
                parent.append(label)
                stats.append([left, y, right, y + 1, 0])
            entry = stats[label]
            entry[0] = min(entry[0], left)
            entry[2] = max(entry[2], right)
            entry[3] = y + 1
            entry[4] += right - left
            current.append((left, right, label))
        previous = current
    return [(_Box(*s[:4]), s[4]) for i, s in enumerate(stats) if parent[i] == i]


def painted_foreground(png_bytes: bytes) -> tuple[int, int, int, int] | None:
    """Find an isolated light panel over a dimmed page when Android omits the modal."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as original:
            width, height = original.size
            scale = min(1, 240 / max(width, height))
            w, h = max(1, round(width * scale)), max(1, round(height * scale))
            reduced = original.resize((w, h), Image.Resampling.BOX).convert("RGB")
    except (OSError, ValueError):
        return None

    brightness = np.asarray(reduced).max(axis=2)
    bright = Image.fromarray(np.uint8(brightness >= 185) * 255)
    joined = bright.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    for box, count in sorted(_components(joined), key=lambda component: -component[1]):
        if not .11 * w * h <= box.area <= .92 * w * h or count < .70 * box.area:
            continue
        cx, cy = box.center
        floating = (box.area <= .72 * w * h and box.left >= .025 * w and box.right <= .975 * w
                    and box.top >= .04 * h and box.bottom <= .96 * h
                    and .39 * w <= cx <= .61 * w and .34 * h <= cy <= .66 * h)
        sheet = (box.left <= .04 * w and box.right >= .96 * w
                 and box.bottom >= .96 * h and box.top >= .08 * h)
        if sheet and box.top < .23 * h:
            # A nearly full-height sheet still leaves two curved corners against the scrim.
            # A straight transition from a dark app toolbar to white page content does not.
            left, right = box.left, box.right - 1
            sheet = any(
                np.mean(brightness[y, left + 4:right - 3] >= 185) >= .95
                and brightness[y, left] < 155 and brightness[y, right] < 155
                and min(brightness[y + 3, left], brightness[y + 3, right]) >= 185
                for y in range(max(0, box.top - 1), min(h - 3, box.top + 2))
            )
        if not (floating or sheet):
            continue

        # Check the whole page around the panel, leaving out the system bars and
        # the antialiased panel edge. A bright card on an undimmed page is common.
        around = np.ones((h, w), dtype=bool)
        around[:max(2, round(.04 * h))] = False
        around[round(.95 * h):] = False
        around[max(0, box.top - 3):min(h, box.bottom + 3), max(0, box.left - 3):min(w, box.right + 3)] = False
        backdrop = brightness[around]
        if not len(backdrop) or np.mean(backdrop >= 165) > .045:
            continue
        # A strong scrim can leave the underlying layout visible in only a
        # small fraction of the background, especially on wide bottom sheets.
        low, high = np.percentile(backdrop, [2, 90])
        if not (35 <= high <= 158 and high - low >= 4):
            continue
        interior = brightness[box.top:box.bottom, box.left:box.right]
        if np.mean(interior >= 215) < .34:
            continue
        # Allow for one downsampled pixel on either side of a rounded edge.
        return (max(0, (box.left - 1) * width // w), max(0, (box.top - 1) * height // h),
                min(width, math.ceil((box.right + 1) * width / w)),
                min(height, math.ceil((box.bottom + 1) * height / h)))
    return None


def _horizontal_close(mask):
    # Close small gaps between adjacent character strokes, without joining
    # an icon to its caption on the next line.
    out = mask
    for offset in (1, 2):
        out = ImageChops.lighter(out, ImageChops.offset(out, offset, 0))
    for offset in (1, 2):
        out = ImageChops.darker(out, ImageChops.offset(out, -offset, 0))
    out = ImageChops.lighter(out, ImageChops.offset(out, 0, 1))
    out = ImageChops.darker(out, ImageChops.offset(out, 0, -1))
    # ImageChops.offset wraps around at the image boundary.
    out.paste(0, (0, 0, min(3, out.width), out.height))
    out.paste(0, (max(0, out.width - 3), 0, out.width, out.height))
    return out


def _aligned_groups(items, coordinate, tolerance):
    groups = []
    for item in sorted(items, key=coordinate):
        if not groups or coordinate(item) - coordinate(groups[-1][0]) > tolerance:
            groups.append([])
        groups[-1].append(item)
    return groups


def _uniform_runs(items, coordinate, min_gap, max_gap):
    if not items:
        return
    items = sorted(items, key=coordinate)
    run = [items[0]]
    gaps = []
    for item in items[1:]:
        gap = coordinate(item) - coordinate(run[-1])
        valid = min_gap <= gap <= max_gap
        typical = median(gaps) if gaps else gap
        if valid and abs(gap - typical) <= max(3, typical * .22):
            run.append(item)
            gaps.append(gap)
        else:
            if len(run) >= 3:
                yield run
            run = [run[-1], item] if valid else [item]
            gaps = [gap] if valid else []
    if len(run) >= 3:
        yield run


def _has_detail(ink, box, mx, my, fraction):
    inner = ink.crop((box.left + mx, box.top + my, box.right - mx, box.bottom - my))
    return inner.histogram()[255] >= max(4, inner.width * inner.height * fraction)


def _repeated_items(ink):
    fine = ink.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    parts = [b for b, _ in _components(fine)]
    words = [b for b, _ in _components(_horizontal_close(ink))
             if 6 <= b.height <= 25 and max(12, b.height * 1.4) <= b.width <= ink.width * .89
             and _has_detail(ink, b, 3, 2, .12)]
    icons = [b for b in parts if 8 <= b.width <= 57 and 8 <= b.height <= 57
             and .51 <= b.width / b.height <= 1.95]
    pairs = []
    for word in words:
        matches = [icon for icon in icons if -1 <= word.top - icon.bottom <= 21
                   and 1.08 * word.height <= icon.height <= 4.2 * word.height
                   and abs(icon.center[0] - word.center[0]) <= max(4, .15 * word.width)]
        if matches:
            icon = min(matches, key=lambda b: (abs(b.center[0] - word.center[0]), word.top - b.bottom))
            pairs.append((word, icon))
    for group in _aligned_groups(pairs, lambda pair: pair[0].center[1], 5):
        for run in _uniform_runs(group, lambda pair: pair[0].center[0], 30, 165):
            heights = [icon.height for _, icon in run]
            if max(heights) <= 2.2 * min(heights):
                for word, icon in run:
                    yield "grid_item", word.union(icon)

    # A repeated end marker and an opposing column of aligned strokes provide
    # a useful row shape even when no border has been drawn.
    markers = [b for b in parts if 4 <= b.width <= 15 and 5 <= b.height <= 19
               and .27 <= b.width / b.height <= 1.4]
    for column in _aligned_groups(markers, lambda b: b.center[0], 2):
        for size in _aligned_groups(column, lambda b: b.height, 2):
            for run in _uniform_runs(size, lambda b: b.center[1], 17, 100):
                for direction in (-1, 1):
                    opposing = []
                    for marker in run:
                        choices = [w for w in words if abs(w.center[1] - marker.center[1]) <= 4
                                   and w.width >= 22 and direction * (w.center[0] - marker.center[0]) > ink.width * .18]
                        if not choices:
                            break
                        nearest_margin = min if direction < 0 else max
                        opposing.append(nearest_margin(choices, key=lambda w: w.center[0]))
                    if len(opposing) != len(run):
                        continue
                    left = [b.left for b in opposing]
                    right = [b.right for b in opposing]
                    if min(max(left) - min(left), max(right) - min(right)) > 12:
                        continue
                    for marker, word in zip(run, opposing):
                        row = marker.union(word)
                        if row.width >= ink.width * .4:
                            yield "aligned_row", row


def _flat_interiors(gradient, threshold):
    flat = gradient.point([255 if i <= threshold else 0 for i in range(256)])
    for b, pixels in _components(flat):
        if (25 <= b.width <= gradient.width * .98 and 11 <= b.height <= gradient.width * .69
                and 1.2 <= b.width / b.height <= 15.8 and b.area >= 450 and pixels >= b.area * .5):
            yield b, pixels / b.area


def _edge_coverage(subset, vertical):
    # Collapse a narrow strip to count rows/columns touching its edge. The
    # strip allows for antialiasing and a small offset from the interior.
    strip = subset.resize((1, subset.height) if vertical else (subset.width, 1), Image.Resampling.BOX)
    return 1 - strip.histogram()[0] / (strip.width * strip.height)


def _sides(edge, box, mx, my):
    vertical = [_edge_coverage(edge.crop((x - 4, box.top + my, x + 4, box.bottom - my)), True)
                for x in (box.left, box.right)]
    horizontal = [_edge_coverage(edge.crop((box.left + mx, y - 4, box.right - mx, y + 4)), False)
                  for y in (box.top, box.bottom)]
    return sorted(vertical + horizontal)


def _surfaces(gradient, ink):
    # The stricter edge threshold finds borders on pale backgrounds; the
    # looser one connects shaded areas of the same card. Require a matching
    # strict interior so texture in a picture does not create a new card.
    strict = dict(_flat_interiors(gradient, 10))
    found = strict.copy()
    for box, fill in _flat_interiors(gradient, 20):
        if any(box.intersection(seed) >= max(box.area, seed.area) * .55 for seed in strict):
            found[box] = max(fill, found.get(box, 0))
    edge = gradient.point([255 if i >= 10 else 0 for i in range(256)])
    for b in sorted(found, key=lambda b: (-b.area, b.top, b.left)):
        # A loosely filled rectangle needs a separate aligned peer. Otherwise
        # interior features of a single illustration can look like a panel.
        if found[b] < .60 and not any(
            not b.intersection(peer) and abs(b.center[1] - peer.center[1]) <= 4
            and abs(b.height - peer.height) <= 5 for peer in found
        ):
            continue
        margin = max(2, round(b.height * .12))
        mx = min(margin, max(2, round(b.width * .12)))
        if not _has_detail(ink, b, mx, margin, .009):
            continue
        sides = _sides(edge, b, mx, margin)
        if sides[0] < .38 or sides[1] < .75:
            continue
        strong = _sides(ink, b, mx, margin)
        if strong[-1] < .18 or (strong[0] >= .38 and strong[1] >= .65):
            yield "enclosed_surface", b


def _repeated_surfaces(gradient, ink):
    """Add low-contrast surfaces only when each has ink and belongs to a regular group."""
    soft_edges = gradient.point([0 if value < 3 else 255 for value in range(256)])
    candidates = []
    for box, occupancy in _flat_interiors(gradient, 2):
        if occupancy < .70 or box.width < 2 * box.height:
            continue
        margin = max(2, round(box.height * .14))
        if not _has_detail(ink, box, margin, margin, .003):
            continue
        if min(_sides(soft_edges, box, margin, margin)) < .74:
            continue
        candidates.append(box)
    found = set()
    for seed in candidates:
        peers = [other for other in candidates
                 if abs(seed.width - other.width) <= max(2, seed.width * .12)
                 and abs(seed.height - other.height) <= max(2, seed.height * .16)]
        for axis in (0, 1):
            for group in _aligned_groups(peers, lambda box: box.center[1 - axis], 3):
                min_gap = max(4, round((seed.width if axis == 0 else seed.height) * .90))
                for run in _uniform_runs(group, lambda box: box.center[axis], min_gap, gradient.size[axis] // 2):
                    found.update(run)
    for box in sorted(found, key=lambda item: (item.top, item.left)):
        yield "repeated_surface", box


def _has_ring_cross(patch):
    """A thin circular stroke with a bare inner annulus and four diagonals."""
    size = 33
    center = (size - 1) / 2
    patch = patch.resize((size, size), Image.Resampling.BILINEAR)
    lo, hi = patch.getextrema()
    if hi - lo < 55:
        return False
    # Threshold the local crop. Both dark symbols on light and their inverse work.
    hist = patch.histogram()
    total, weighted = size * size, sum(i * c for i, c in enumerate(hist))
    count, partial, best, threshold = 0, 0, 0, lo
    for level in range(lo, hi):
        count += hist[level]
        partial += level * hist[level]
        if 0 < count < total:
            variance = (weighted * count - partial * total) ** 2 / (count * (total - count))
            if variance > best:
                best, threshold = variance, level
    pixels = patch.load()
    for polarity in (True, False):
        def on(x, y):
            return (pixels[round(center + x * center), round(center + y * center)] > threshold) == polarity

        # The four arms must all reach away from the centre. Off-diagonal ink
        # distinguishes a cross from a blob, plus, bullet or filled round icon.
        arms = [sum(on(sx * d, sy * d) for d in (.09, .14, .19, .24, .29))
                for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1))]
        if min(arms) < 4:
            continue
        off = sum(on(dx * radius, dy * radius) for dx, dy in _AXIS_DIRECTIONS for radius in (.22, .32, .43))
        if off > 1:
            continue
        ring = sum(any(on(dx * radius, dy * radius) for radius in (.73, .79, .85, .91, .97))
                   for dx, dy in _CIRCLE_DIRECTIONS)
        # A filled badge or cog may also contain crossed strokes. A thin ring
        # has a mostly empty band separating its limb from the diagonal marks.
        gap = sum(on(dx * r, dy * r) for dx, dy in _CIRCLE_DIRECTIONS for r in (.58, .64, .70))
        if ring >= 22 and gap <= 9:
            return True
    return False


_AXIS_DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_CIRCLE_DIRECTIONS = tuple((math.cos(i * math.tau / 24), math.sin(i * math.tau / 24)) for i in range(24))


def _ring_cross_icons(reduced, gradient):
    strong = gradient.point([0 if n < 65 else 255 for n in range(256)])
    gray = reduced.convert("L")
    short = min(reduced.size)
    for box, edge_pixels in _components(strong):
        if (not max(11, .016 * short) <= min(box.width, box.height) <= max(13, .20 * short)
                or not .82 <= box.width / box.height <= 1.22
                or not .13 <= edge_pixels / box.area <= .68):
            continue
        if _has_ring_cross(gray.crop(box.tuple())):
            yield "icon", box


def _same_target(candidate, known):
    overlap = candidate.intersection(known)
    x, y = candidate.center
    if not (known.left <= x < known.right and known.top <= y < known.bottom
            and .44 * candidate.area <= known.area <= 3.8 * candidate.area):
        return False
    if overlap >= candidate.area * .55:
        return True
    # A native row can name only the text/indicator band within its painted surface.
    return (min(candidate.width, known.width) >= .80 * max(candidate.width, known.width)
            and overlap >= .88 * min(candidate.area, known.area))


def _gradient(image):
    high = image.filter(ImageFilter.MaxFilter(3))
    low = image.filter(ImageFilter.MinFilter(3))
    red, green, blue = ImageChops.difference(high, low).split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def _blank_bar_color(image, box):
    """Require a broad, neutral, empty interior with a faintly contrasting surround."""
    if box.height < 4 or box.width < max(10, 1.65 * box.height):
        return None
    inset = max(1, round(box.height * .14))
    inner = image.crop((box.left + inset, box.top + inset, box.right - inset, box.bottom - inset))
    if max(high - low for low, high in inner.getextrema()) > 14:
        return None
    # Keep even a tiny high-contrast glyph touching the rim of a pale button.
    if max(high - low for low, high in image.crop(box.tuple()).getextrema()) > 48:
        return None
    cx, cy = box.center
    fill = image.getpixel((cx, cy))
    if max(fill) - min(fill) > 24:
        return None
    radius = max(3, inset)
    probes = ((box.left - radius, cy), (box.right + radius, cy),
              (cx, box.top - radius), (cx, box.bottom + radius))
    nearby = [image.getpixel((x, y)) for x, y in probes if 0 <= x < image.width and 0 <= y < image.height]
    faint_edges = sum(max(color) - min(color) <= 24 and
                      3 <= max(abs(a - b) for a, b in zip(color, fill)) <= 48 for color in nearby)
    return fill if faint_edges >= 2 else None


def _loading_families(image, gradient):
    # A pale blank button or a single grey glyph gives no evidence of loading.
    # Look for a family of equally spaced, solid bars of the same colour/size.
    bars = [(box, color) for box, occupancy in _flat_interiors(gradient, 3)
            if occupancy >= .78 and (color := _blank_bar_color(image, box)) is not None]
    families = []
    for box, color in bars:
        peers = [other for other, fill in bars if max(abs(a - b) for a, b in zip(fill, color)) <= 8
                 and abs(box.width - other.width) <= max(2, box.width * .16)
                 and abs(box.height - other.height) <= max(2, box.height * .16)]
        for axis in (0, 1):
            aligned = [peer for peer in peers if abs(peer.center[1 - axis] - box.center[1 - axis]) <= 3]
            if len(aligned) >= 3 and any(_uniform_runs(
                aligned, lambda b: b.center[axis], 5, image.size[axis] // 2,
            )):
                families.append((box, color))
                break
    return families


def _matches_loading_family(box, fill, families, image_size):
    # A small unrelated icon between rows should survive even when loading is
    # visible elsewhere on the same screen. Large empty cards often use white
    # while the repeated rows use grey.
    if not families:
        return False
    if box.width >= image_size[0] * .45 and box.height >= image_size[1] * .10:
        return True
    return any(max(abs(a - b) for a, b in zip(fill, color)) <= 8
               and box.height >= peer.height * .45 and box.width >= peer.width * .5
               for peer, color in families)


def is_placeholder(image: Image.Image, bounds) -> bool:
    """Check a pixel proposal in original-image coordinates against visible skeleton evidence."""
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or not all(type(v) is int for v in bounds):
        return False
    x, y, r, b = bounds
    if not (0 <= x < r <= image.width and 0 <= y < b <= image.height):
        return False
    w = min(image.width, 432)
    h = max(1, round(image.height * w / image.width))
    reduced = image.convert("RGB").resize((w, h), Image.Resampling.BOX)
    box = _Box(round(x*w/image.width), round(y*h/image.height),
               round(r*w/image.width), round(b*h/image.height))
    fill = _blank_bar_color(reduced, box)
    return fill is not None and _matches_loading_family(
        box, fill, _loading_families(reduced, _gradient(reduced)), reduced.size,
    )


def _empty_native_interior(image, box):
    # Inspect the native pixels: a one-pixel glyph can disappear when a phone
    # screenshot is reduced to 432 columns. Ignore only the rounded perimeter.
    inset = max(1, round(min(box.height, box.width) * .035))
    core = image.crop((box.left + inset, box.top + inset, box.right - inset, box.bottom - inset))
    if min(core.size) < 2:
        return False
    mask = Image.new("L", core.size)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, core.width - 1, core.height - 1), radius=core.height // 2, fill=255,
    )
    return all(high - low <= 3 for low, high in ImageStat.Stat(core, mask).extrema)


def system_placeholders(png_bytes: bytes, targets: list[dict]) -> set[str]:
    """Return IDs of anonymous Android tap regions which coincide with repeated, empty loading bars.

    An existing system action requires stronger evidence than a pixel proposal.
    An unrelated blank control on the same loading screen keeps its action.
    """
    anonymous = [
        item for item in targets
        if item.get("source") == "android" and isinstance(item.get("id"), str)
        and not item.get("name") and not item.get("system_hint")
        and item.get("actions") in (["tap"], ("tap",))
        and item.get("role") not in {"input", "toggle", "scroll_area"}
        and not any(item.get("state", {}).get(key) for key in ("selected", "focused", "checked"))
    ]
    if not anonymous or not png_bytes:
        return set()
    try:
        with Image.open(io.BytesIO(png_bytes)) as source:
            image = source.convert("RGB")
    except (OSError, ValueError):
        return set()
    width = min(image.width, 432)
    height = max(1, round(image.height * width / image.width))
    reduced = image.resize((width, height), Image.Resampling.BOX)
    empty = []
    for item in anonymous:
        bounds = item.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or not all(type(n) is int for n in bounds):
            continue
        x, y, r, b = bounds
        if not (0 <= x < r <= image.width and 0 <= y < b <= image.height):
            continue
        box = _Box(round(x * width / image.width), round(y * height / image.height),
                   round(r * width / image.width), round(b * height / image.height))
        fill = _blank_bar_color(reduced, box)
        if fill is not None and _empty_native_interior(image, _Box(*bounds)):
            empty.append((item["id"], box, fill))
    if not empty:
        return set()
    families = _loading_families(reduced, _gradient(reduced))
    return {
        key for key, box, fill in empty
        if any(box.intersection(peer) >= .72 * max(box.area, peer.area)
               and max(abs(a - b) for a, b in zip(fill, color)) <= 8 for peer, color in families)
    }


def visual_targets(png_bytes: bytes, width: int, height: int, system_targets: list[dict]) -> list[dict]:
    """Return screenshot-space proposals whose local tap area lacks a system target."""
    if not png_bytes or width <= 0 or height <= 0:
        return []
    try:
        with Image.open(io.BytesIO(png_bytes)) as original:
            if (original.width > original.height) != (width > height):
                return []
            work_width = min(original.width, 432)
            work_height = max(1, round(original.height * work_width / original.width))
            reduced = original.convert("RGB").resize((work_width, work_height), Image.Resampling.BOX)
    except (OSError, ValueError):
        return []

    gradient = _gradient(reduced)
    ink = gradient.point([255 if i >= 32 else 0 for i in range(256)])
    known = []
    for item in system_targets:
        bounds = item.get("bounds")
        if "tap" in item.get("actions", ()) and isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            known.append(_Box(*bounds))
    results, included = [], []
    loading = None
    for kind, box in chain(_repeated_items(ink), _surfaces(gradient, ink),
                           _repeated_surfaces(gradient, ink), _ring_cross_icons(reduced, gradient)):
        fill = _blank_bar_color(reduced, box)
        if fill is not None:
            if loading is None:
                loading = _loading_families(reduced, gradient)
            if _matches_loading_family(box, fill, loading, reduced.size):
                continue
        box = _Box(round(box.left * width / work_width), round(box.top * height / work_height),
                   round(box.right * width / work_width), round(box.bottom * height / work_height))
        if any(_same_target(box, other) for other in known):
            continue
        if any(box.intersection(other) >= max(box.area, other.area) * .68 for other in included):
            continue
        included.append(box)
        results.append({"id": f"V{len(results) + 1}", "name": None, "role": kind, "source": "pixels",
                        "actions": ["try_tap"], "bounds": list(box.tuple()), "point": list(box.center), "state": {}})
    return results
