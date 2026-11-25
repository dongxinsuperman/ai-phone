"""Render the current Android target list as a separate, original-resolution PNG."""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont


# Layout adapted from trajectory_cache/jev_click/label_layout.py in this MIT-licensed
# project. Importing its parent package would load replay/model code into Android observe.
def _intersects(a, b, gap=3):
    return a[0] < b[2] + gap and a[2] + gap > b[0] and a[1] < b[3] + gap and a[3] + gap > b[1]


def _place_labels(candidates, sizes, image_size):
    width, height = image_size
    occupied = []
    placements = {}
    # Small target regions retain their preferred positions before containers.
    ordered = sorted(
        candidates,
        key=lambda n: (
            (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1]),
            n["id"],
        ),
    )
    for node in ordered:
        x0, y0, x1, y1 = node["bounds"]
        bw, bh = sizes[node["id"]]
        bw, bh = min(bw, width), min(bh, height)
        choices = [(x0, y0), (x0, y0 - bh - 4), (x1 - bw, y0), (x0, y1 - bh), (x1 - bw, y1 - bh)]
        # For a tiny target, a badge at the preferred corner can hide the
        # entire box. Use the nearest available outside edge in that case.
        if (x1 - x0) * (y1 - y0) < 2 * bw * bh:
            adjacent = ((x0, y0 - bh - 4), (x1 + 3, y0), (x0, y1 + 3), (x0 - bw - 3, y0))
            outside = []
            for x, y in adjacent:
                x, y = max(0, min(x, width - bw)), max(0, min(y, height - bh))
                if not _intersects((x, y, x + bw, y + bh), node["bounds"], gap=0):
                    outside.append((x, y))
            choices = outside + choices
        for distance in range(1, 9):
            for dx, dy in [(0, -1), (1, 0), (0, 1), (-1, 0), (1, -1), (-1, -1)]:
                choices.append((x0 + dx * distance * (bw + 4), y0 + dy * distance * (bh + 4)))
        selected = None
        for x, y in choices:
            x, y = max(0, min(x, width - bw)), max(0, min(y, height - bh))
            rect = (x, y, x + bw, y + bh)
            if not any(_intersects(rect, old) for old in occupied):
                selected = rect
                break
        if selected is None:
            # Bounded exhaustive grid fallback rather than painting over a badge.
            for y in range(0, max(1, height - bh + 1), bh + 4):
                for x in range(0, max(1, width - bw + 1), bw + 4):
                    rect = (x, y, x + bw, y + bh)
                    if not any(_intersects(rect, old) for old in occupied):
                        selected = rect
                        break
                if selected is not None:
                    break
        if selected is None:
            raise ValueError("Too many candidate labels to display without overlap")
        occupied.append(selected)
        placements[node["id"]] = selected
    return placements


def _visible_targets(targets, size):
    width, height = size
    result = []
    seen = set()
    for target in targets:
        key = target.get("id")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("Frame targets must have unique, nonempty string IDs")
        bounds = target.get("bounds")
        if (
            not isinstance(bounds, (tuple, list))
            or len(bounds) != 4
            or not all(type(x) is int for x in bounds)
        ):
            raise ValueError(f"Invalid pixel bounds for {key}")
        left, top, right, bottom = bounds
        visible = (max(left, 0), max(top, 0), min(right, width), min(bottom, height))
        if visible[0] >= visible[2] or visible[1] >= visible[3]:
            raise ValueError(f"No visible pixels for {key}")
        seen.add(key)
        result.append({"id": key, "bounds": visible})
    return result


def _badges(draw, nodes, size):
    # Start with Jev's 20px badges on phone frames. Recorded frames can be much
    # smaller, so retry smaller fonts before packing badges on a regular grid.
    preferred = min(20, max(9, min(size) // 20))
    for font_size in dict.fromkeys((preferred, max(6, preferred - 4), max(6, preferred - 8), 6)):
        font = ImageFont.load_default(size=font_size)
        metrics = {n["id"]: draw.textbbox((0, 0), n["id"], font=font) for n in nodes}
        pad = 2 if font_size >= 12 else 1
        sizes = {
            key: (right - left + 2 * pad, bottom - top + 2 * pad)
            for key, (left, top, right, bottom) in metrics.items()
        }
        if sum(w * h for w, h in sizes.values()) > size[0] * size[1] or any(
            w > size[0] or h > size[1] for w, h in sizes.values()
        ):
            continue
        try:
            positions = _place_labels(nodes, sizes, size)
        except ValueError:
            continue
        return font, metrics, pad, positions
    # Even on a thumbnail with fewer pixels than all the glyphs require, emit
    # the complete set of boxes and best-effort badges. Never lose the frame.
    ordered = sorted(nodes, key=lambda node: (node["bounds"][1], node["bounds"][0], node["id"]))
    max_w = max(w for w, _ in sizes.values())
    max_h = max(h for _, h in sizes.values())
    columns = max(1, min(len(nodes), round(math.sqrt(len(nodes) * size[0] * max_h / (size[1] * max_w)))))
    rows = (len(nodes) + columns - 1) // columns
    positions = {}
    for index, node in enumerate(ordered):
        row, col = divmod(index, columns)
        w, h = sizes[node["id"]]
        x = max(0, min(col * size[0] // columns, size[0] - w))
        y = max(0, min(row * size[1] // rows, size[1] - h))
        positions[node["id"]] = (x, y, min(x + w, size[0]), min(y + h, size[1]))
    return font, metrics, pad, positions


def annotate_frame(png: bytes, targets: list[dict]) -> bytes:
    """Draw every supplied ID at its half-open PNG bounds, without modifying the source.

    The caller keeps the original PNG for text reading. Invalid/duplicate targets
    raise ValueError. An unusually dense frame still receives all boxes and IDs.
    """
    if not targets:
        return png
    with Image.open(io.BytesIO(png)) as original:
        has_alpha = "A" in original.getbands() or "transparency" in original.info
        image = original.convert("RGBA" if has_alpha else "RGB")
    nodes = _visible_targets(targets, image.size)
    draw = ImageDraw.Draw(image)
    font, metrics, pad, positions = _badges(draw, nodes, image.size)

    # Jev uses red half-open frames, white badges, then a leader when a badge
    # has to move. Draw the marks on a copy at the device pixel resolution.
    for node in nodes:
        left, top, right, bottom = node["bounds"]
        draw.rectangle(
            (left, top, right - 1, bottom - 1), outline="#dc2020", width=min(2, right - left, bottom - top)
        )
    for node in nodes:
        left, top, edge, _ = node["bounds"]
        x, y, right, bottom = positions[node["id"]]
        if (x, y) != (left, top):
            cx, cy = (x + right) // 2, (y + bottom) // 2
            anchor = (max(left, min(cx, edge - 1)), top)
            draw.line((cx, cy, *anchor), fill="#dc2020", width=1)
    for node in nodes:
        key = node["id"]
        x, y, right, bottom = positions[key]
        left, top, _, _ = metrics[key]
        draw.rectangle((x, y, right - 1, bottom - 1), fill="white")
        draw.text((x + pad - left, y + pad - top), key, font=font, fill="#b00000")

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
