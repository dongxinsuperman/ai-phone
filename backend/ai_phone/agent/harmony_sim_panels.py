"""Reconcile a bounded ArkWeb drawer with the actual painted frame."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image


def _walk(node):
    yield node
    for child in node["children"]:
        yield from _walk(child)


def _intersection(a, b):
    x, y, r, t = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x, y, r, t) if x < r and y < t else None


def _contains(outer, inner):
    return (
        inner and outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    )


def _edge_panel(mask, page):
    if (
        mask["bounds"] != page
        or "tap" not in mask["actions"]
        or mask.get("_pass_through")
        or mask.get("_hit_clip")
        or mask.get("_possibly_transparent")
    ):
        return None
    pane = mask
    while len(pane["children"]) == 1 and pane["bounds"] == page:
        pane = pane["children"][0]
        if pane.get("_pass_through") or pane.get("_hit_clip") or pane.get("_possibly_transparent"):
            return None
    box = pane["bounds"]
    if not box or box == page or "tap" not in pane["actions"]:
        return None
    x, y, r, t = page
    a, b, c, d = box
    if b == y and d == t and c - a >= (r - x) / 4:
        if c == r and a > x:
            return pane, (x, y, a, t), 0, a
        if a == x and c < r:
            return pane, (c, y, r, t), 0, c
    if a == x and c == r and d - b >= (t - y) / 4:
        if d == t and b > y:
            return pane, (x, y, r, b), 1, b
        if b == y and d < t:
            return pane, (x, d, r, t), 1, d
    return None


def _lettering(node):
    box = node["bounds"]
    text = node.get("text") or ""
    return (
        box
        and node["role"] != "image"
        and not node.get("protected")
        and len(text) >= 2
        and any(char.isalnum() for char in text)
        and box[2] - box[0] >= 6
        and box[3] - box[1] >= 5
    )


def _crossing_rule(pixels, x, y, along, across):
    """All contrast is a thin straight mark that continues outside this text box."""
    height, width = across.shape[0] + 1, across.shape[1]
    if width < 16 or height < 5:
        return False
    rows = np.zeros(height, dtype=bool)
    rows |= along.any(axis=1)
    rows[:-1] |= across.any(axis=1)
    active = np.flatnonzero(rows)
    if not len(active) or active[-1] - active[0] > 2:
        return False
    extension = min(12, max(4, width // 8))
    reach = (width * 4 + 4) // 5
    for row in active:
        if row >= across.shape[0]:
            continue
        edges = across[row]
        # A short or isolated dash may belong to a character. Require a
        # continuous edge through the label boundary into adjacent pixels.
        intervals = []
        if x >= extension and edges[:reach].all():
            intervals.append(slice(x - extension, x))
        if x + width + extension <= pixels.shape[1] and edges[-reach:].all():
            intervals.append(slice(x + width, x + width + extension))
        for columns in intervals:
            pair = pixels[y + row : y + row + 2, columns].astype(np.int16)
            if len(pair) == 2 and (np.abs(pair[1] - pair[0]).max(axis=1) > 3).all():
                return True
    return False


class Screen:
    """Decode only when the tree actually exposes two competing interactive layers."""

    def __init__(self, png, size):
        self.size = size
        self.png = png
        self.rgb = None

    def pixels(self):
        if self.rgb is None and self.png:
            try:
                with Image.open(io.BytesIO(self.png)) as image:
                    if image.size == self.size:
                        self.rgb = np.asarray(image.convert("RGB"))
                    else:
                        self.png = None
            except (ValueError, OSError):
                self.png = None
        return self.rgb

    def has_boundary(self, page, axis, edge):
        pixels = self.pixels()
        if pixels is None:
            return False
        if axis == 1:
            pixels = pixels.transpose(1, 0, 2)
            lo, hi, start, end = page[1], page[3], page[0], page[2]
        else:
            lo, hi, start, end = page[0], page[2], page[1], page[3]
        radius = min(6, edge - lo, hi - edge)
        if radius < 2 or end - start < 12:
            return False
        a = pixels[start:end, edge - radius : edge - radius // 2].mean(axis=1)
        b = pixels[start:end, edge + radius // 2 : edge + radius].mean(axis=1)
        difference = np.abs(a - b).max(axis=1)
        return sum(float((tile >= 18).mean()) > 0.55 for tile in np.array_split(difference, 4)) >= 3

    def stroke(self, box):
        x, y, r, t = box
        patch = self.rgb[y:t, x:r].astype(np.int16)
        if min(patch.shape[:2]) < 2:
            return False
        dx = np.abs(np.diff(patch, axis=1)).max(axis=2) > 3
        dy = np.abs(np.diff(patch, axis=0)).max(axis=2) > 3
        if not (dx.any() or dy.any()):
            return False
        if _crossing_rule(self.rgb, x, y, dx, dy) or _crossing_rule(
            self.rgb.transpose(1, 0, 2), y, x, dy.T, dx.T
        ):
            return None
        return True


def _has_paint_evidence(pane, older, page, axis, edge, screen):
    if not screen.has_boundary(page, axis, edge):
        return False
    front = list(_walk(pane))
    if not any(n is not pane and "tap" in n["actions"] for n in front):
        return False
    if not any(
        _lettering(n) and _contains(pane["bounds"], n["bounds"]) and screen.stroke(n["bounds"]) for n in front
    ):
        return False
    # Compare only independent native lettering for which no reported foreground
    # leaf could supply the observed ink. If some still has ink, the current
    # image cannot establish a visually occluding pane.
    foreground = [n["bounds"] for n in front if n is not pane and n["bounds"] and not n["children"]]
    smooth = []
    for n in _walk(older):
        box = n["bounds"]
        if not _lettering(n) or not _contains(pane["bounds"], box):
            continue
        if any(_intersection(box, other) for other in foreground):
            continue
        painted = screen.stroke(box)
        if painted is True:
            return False
        # A divider crossing a native text box proves neither its letters nor a
        # blank patch. Only independent smooth boxes count as disappearance.
        if painted is None:
            continue
        if not any(_intersection(box, other) for other in smooth):
            smooth.append(box)
    if len(smooth) < 3:
        return False
    # Independently located evidence must extend across the pane, not merely
    # describe three adjacent sub-elements inside one unpainted icon.
    cx = [(b[0] + b[2]) / 2 for b in smooth]
    cy = [(b[1] + b[3]) / 2 for b in smooth]
    return max(cx) - min(cx) >= 0.35 * (page[2] - page[0]) or max(cy) - min(cy) >= 0.35 * (page[3] - page[1])


def _outside_panel(node, exterior):
    original = node["bounds"]
    visible = _intersection(original, exterior) if original else None
    # The full-window tap mask receives touches outside its own pane, too.
    node["actions"] = []
    node["children"] = [child for child in node["children"] if _outside_panel(child, exterior)]
    if original is not None:
        if not visible and not node["children"]:
            return False
        if tuple(original) != visible:
            # A clipped word cannot certify its whole accessible label.
            node["text"], node["name"] = None, None
            for field in ("value", "safe_label", "resource_id", "system_name_source"):
                node.pop(field, None)
        node["bounds"] = list(visible) if visible else None
    return bool(node["children"] or visible)


def reconcile_drawer(document, screen, warnings):
    """Clip covered background evidence after a positioned mask, pane and pixels agree."""
    page = document["bounds"]
    if not page:
        return
    for parent in _walk(document):
        layers = [n for n in parent["children"] if n["bounds"] == page and n.get("_web_extent")]
        if len(layers) < 2:
            continue
        layers.sort(key=lambda n: n.get("_web_z", 0))
        for index, front in enumerate(layers):
            info = _edge_panel(front, page) if index else None
            if info is None:
                continue
            pane, outside, axis, edge = info
            for older in layers[:index]:
                if older["_web_extent"] != front["_web_extent"] or not _has_paint_evidence(
                    pane, older, page, axis, edge, screen
                ):
                    continue
                if not _outside_panel(older, outside):
                    parent["children"].remove(older)
                front["_captures_taps_in"] = [list(outside)]
                note = "前景面板内保留可读可操作内容，面板外仅保留露出的背景；外侧点击由当前遮罩接收"
                if note not in warnings:
                    warnings.append(note)
