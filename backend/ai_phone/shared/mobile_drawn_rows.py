"""Find visually separated rows whose positioned native captions have no native action."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat


_WRAPPERS = {"group", "view", "other", "container", "layout", "row", "list_item", "cell"}
_CAPTIONS = {"text", "label", "heading", "title"}


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _distance(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


@dataclass
class _Row:
    view: dict
    record: dict
    box: tuple
    label: tuple


def _row(view, records, obstacles):
    box = view.get("bounds")
    if (view.get("role") not in _WRAPPERS or not box or view.get("target") or view.get("name")
            or view.get("state", {}).get("enabled") is False):
        return None
    leaf = view
    while leaf.get("role") in _WRAPPERS:
        children = leaf.get("children", ())
        if (len(children) != 1 or leaf.get("name") or leaf.get("text") or leaf.get("target")
                or leaf.get("state", {}).get("enabled") is False):
            return None
        leaf = children[0]
    label = leaf.get("bounds")
    if (leaf.get("role") not in _CAPTIONS or leaf.get("source") != "system" or not label
            or not leaf.get("text") or leaf.get("children") or leaf.get("target")
            or leaf.get("state", {}).get("enabled") is False):
        return None
    record = records.get((leaf["text"], tuple(label)))
    if record is None or record.get("target") or any(_overlaps(box, other) for other in obstacles):
        return None
    left, top, right, bottom = box
    label_left, label_top, label_right, label_bottom = label
    # The wrapper must provide its own wide row and leading/top padding around one whole line.
    # Its bottom padding may be clipped at the viewport edge even when every glyph is on screen.
    h = label_bottom - label_top
    if (not (left <= label_left < label_right <= right and top <= label_top < label_bottom <= bottom)
            or h < 6 or label_right - label_left < 1.4 * h or right - left < 1.25 * (label_right - label_left)
            or min(label_left - left, label_top - top) < .09 * h or bottom - top > 4 * h):
        return None
    return _Row(view, record, tuple(box), tuple(label))


def _adjacent(a, b):
    left, top, right, bottom = a.box
    next_left, next_top, next_right, next_bottom = b.box
    h1 = a.label[3] - a.label[1]
    h2 = b.label[3] - b.label[1]
    rounding = max(2, .09 * min(h1, h2))
    heights = (bottom - top, next_bottom - next_top)
    return (top < next_top and abs(bottom - next_top) <= rounding
            and max(abs(left - next_left), abs(right - next_right)) <= rounding
            and abs(a.label[0] - b.label[0]) <= .2 * min(h1, h2)
            and abs((a.label[1] - top) - (b.label[1] - next_top)) <= .22 * min(h1, h2)
            and min(h1, h2) >= .77 * max(h1, h2) and min(heights) >= .57 * max(heights))


def _pairs(nodes, records, obstacles, owners):
    prior = None
    for node in nodes:
        current = _row(node, records, obstacles)
        if prior and current and _adjacent(prior, current):
            yield prior, current
        prior = current
        # A scroll container may own a page of independent rows. A control with a tap owns its children.
        if not node.get("target") or owners.get(node["target"]) == ["scroll"]:
            yield from _pairs(node.get("children", ()), records, obstacles, owners)


def _paper(image, box):
    sample = image.crop(box).resize((35, 15), Image.Resampling.NEAREST)
    color = tuple(ImageStat.Stat(sample).median)
    pixels = sample.load()
    matches = sum(_distance(pixels[x, y], color) <= 6 for y in range(15) for x in range(35))
    return color if matches >= .81 * 35 * 15 else None


def _ink(image, row, paper):
    x, y, right, bottom = row.label
    patch = image.crop(row.label)
    differences = ImageChops.difference(patch, Image.new("RGB", patch.size, paper)).split()
    mask = ImageChops.lighter(ImageChops.lighter(*differences[:2]), differences[2]).point(
        lambda value: 255 if value >= 30 else 0)
    ink = mask.getbbox()
    if not ink:
        return None
    left, top, end, base = ink
    if end - left < .69 * (right - x) or base - top < .42 * (bottom - y):
        return None
    tight = mask.crop(ink)
    columns, lines = tight.getprojection()
    # Distinct strokes must fill the line, with space between glyphs. A toolbar arrow, a rule,
    # the edge of an image, and a solid colored swatch can all make a text box nonuniform.
    count = sum(not c.isspace() for c in row.record["text"])
    bins = max(2, min(12, count))
    coverage = tight.resize((bins, 1), Image.Resampling.BOX).tobytes()
    groups = sum(on and (i == 0 or not columns[i - 1]) for i, on in enumerate(columns))
    if (groups < min(3, max(2, count // 3)) or min(coverage) < 7 or max(coverage) > 193
            or sum(lines) < .78 * len(lines)):
        return None
    mid_x, mid_y = (end - left) // 2, (base - top) // 2
    point_y = min((i for i, on in enumerate(lines) if on), key=lambda i: abs(i - mid_y))
    line = tight.crop((0, point_y, end - left, point_y + 1)).tobytes()
    point_x = min((i for i, on in enumerate(line) if on), key=lambda i: abs(i - mid_x))
    return [x + left, y + top, x + end, y + base], [x + left + point_x, y + top + point_y]


def _separator(image, a, b, paper):
    left, right = max(a.box[0], b.box[0]), min(a.box[2], b.box[2])
    h = min(a.label[3] - a.label[1], b.label[3] - b.label[1])
    boundary = (a.box[3] + b.box[1]) // 2
    radius = max(3, round(h * .11))
    t, end = max(0, boundary - radius), min(image.height, boundary + radius + 1)
    pad = max(1, (right - left) // 25)
    strip = image.crop((left + pad, t, right - pad, end)).resize((81, end - t), Image.Resampling.NEAREST)
    pixels = strip.load()
    bands = []
    fills = []
    for y in range(end - t):
        colors = [pixels[x, y] for x in range(81)]
        center = colors[len(colors) // 2]
        # A separator is a run in one distinct color, with matching page surface on both sides.
        uniform = sum(_distance(c, center) <= 4 for c in colors) >= .86 * len(colors)
        fills.append(sum(_distance(c, paper) <= 6 for c in colors) >= .83 * len(colors))
        if uniform and _distance(center, paper) >= 12:
            bands.append(y)
    if not bands or len(bands) != bands[-1] - bands[0] + 1 or len(bands) > max(3, h * .09):
        return None
    first, last = bands[0], bands[-1]
    if not any(fills[max(0, first - 3):first]) or not any(fills[last + 1:last + 4]):
        return None
    return [left + pad, t + first, right - pad, t + last + 1]


def _panel_edge(image, pair, paper):
    # Require at least one common painted panel edge on both rows. Horizontal rules within
    # ordinary full-page prose cannot supply this additional evidence for an anonymous group.
    pixels = image.load()
    for side in (0, 2):
        supported = []
        for row in pair:
            top, bottom = row.box[1], row.box[3]
            h = row.label[3] - row.label[1]
            radius = max(3, min(16, round(.12 * h)))
            direction = 1 if side == 0 else -1
            edge = row.box[side]
            inside = edge + direction * (radius + 1)
            if min(edge - radius, inside) < 0 or max(edge + radius, inside) >= image.width:
                break
            hits = 0
            for step in (.2, .35, .5, .65, .8):
                y = min(bottom - 1, top + int((bottom - top) * step))
                if _distance(pixels[inside, y], paper) <= 7:
                    contrasting = sum(_distance(pixels[x, y], paper) >= 12
                                      for x in range(edge - radius, edge + radius + 1))
                    hits += contrasting >= 2
            supported.append(hits >= 3)
        if len(supported) == 2 and all(supported):
            return True
    return False


def add_separated_rows(image, views, observation, excluded, limit):
    """Attach candidate row actions using co-located pixels. True means an eligible target was truncated."""
    targets = observation["targets"]
    records = {(r["text"], tuple(r["bounds"])): r for r in observation["system_texts"]
               if r.get("source") == "system" and r.get("bounds") and not r.get("target")}
    obstacles = list(excluded) + [t["bounds"] for t in targets if t.get("bounds")
                                 and set(t.get("actions", ())) & {"tap", "try_tap", "input"}]
    owners = {t["id"]: t.get("actions") for t in targets}
    index = max((int(i[1:]) for i in owners if i.startswith("V") and i[1:].isdigit()), default=0)
    # Collect pairs before attaching their parent references: a third row can share the middle row.
    pairs = list(_pairs(views, records, obstacles, owners))
    for pair in pairs:
        a, b = pair
        background, other = _paper(image, a.box), _paper(image, b.box)
        if background is None or other is None or _distance(background, other) > 6:
            continue
        divider = _separator(image, a, b, background)
        if not divider or not _panel_edge(image, pair, background):
            continue
        glyphs = [_ink(image, row, background) for row in pair]
        if not all(glyphs):
            continue
        for row, (ink_box, point) in zip(pair, glyphs):
            if row.record.get("target"):
                continue
            if len(targets) >= limit:
                return True
            index += 1
            ident = f"V{index}"
            targets.append({"id": ident, "source": "pixels", "role": "row", "actions": ["try_tap"],
                            "name": row.record["text"], "name_source": "system", "name_bounds": list(row.label),
                            "system_name_source": "contained_row_text", "bounds": list(row.box),
                            "point": point, "state": {}, "visual_evidence": {
                                "separator_bounds": divider, "glyph_bounds": ink_box}})
            row.record["target"] = row.view["target"] = ident
    return False
