"""A small private RGB grid of the final device screenshot, sampled before target markers are drawn."""
from __future__ import annotations

import base64
from math import ceil

from PIL import Image


def _grid_size(width: int, height: int) -> tuple[int, int]:
    scale = min(1, 40 / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def sample_original(image: Image.Image) -> str:
    grid = image.convert("RGB").resize(_grid_size(*image.size), Image.Resampling.BOX)
    return base64.b64encode(grid.tobytes()).decode("ascii")


def read_sample(encoded, width: int, height: int) -> bytes | None:
    if not isinstance(encoded, str) or width <= 0 or height <= 0:
        return None
    cols, rows = _grid_size(width, height)
    size = 3 * cols * rows
    if len(encoded) != 4 * ceil(size / 3):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError):
        return None
    return data if len(data) == size else None


def changed_bounds(before: bytes, after: bytes, width: int, height: int) -> list[list[int]] | None:
    """Group adjacent cells by brighter/darker change, returning approximate half-open pixel bounds."""
    cols, rows = _grid_size(width, height)
    if not all(isinstance(sample, bytes) and len(sample) == 3 * cols * rows for sample in (before, after)):
        return None
    if before == after:
        return []
    cells = {}
    for i in range(cols * rows):
        k = 3 * i
        dr, dg, db = (after[k + channel] - before[k + channel] for channel in range(3))
        strength = max(abs(dr), abs(dg), abs(db))
        if strength >= 8:
            cells[i % cols, i // cols] = (299 * dr + 587 * dg + 114 * db >= 0, strength)

    areas = []
    while cells:
        start, (brighter, score) = cells.popitem()
        stack = [start]
        left = right = start[0]
        top = bottom = start[1]
        count = 1
        while stack:
            x, y = stack.pop()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                entry = cells.get(neighbor)
                if entry is None or entry[0] != brighter:
                    continue
                del cells[neighbor]
                stack.append(neighbor)
                score += entry[1]
                count += 1
                nx, ny = neighbor
                left, right = min(left, nx), max(right, nx)
                top, bottom = min(top, ny), max(bottom, ny)
        if count > 1 or score >= 32:
            box = [left * width // cols, top * height // rows,
                   ceil((right + 1) * width / cols), ceil((bottom + 1) * height / rows)]
            areas.append((score, box))
    areas.sort(key=lambda item: (-item[0], item[1]))
    return [box for _, box in areas[:6]]
