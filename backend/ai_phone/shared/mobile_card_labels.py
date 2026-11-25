"""Relate lettering painted into a native image control to the one physically operable target."""
from __future__ import annotations

from collections import defaultdict

import numpy as np

_LAYOUT = {"group", "container", "layout", "view", "other"}
_CARD_ROLES = _LAYOUT | {"button", "link", "tab", "list_item", "image"}


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", ()))


def _covers(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _fills_card(card, picture):
    # Native padding and pixel rounding may place the full-card image a few pixels inside its control.
    if not picture or not _covers(card, picture):
        return False
    inset = max(2, min(8, round(.035 * min(card[2] - card[0], card[3] - card[1]))))
    return all(abs(a - b) <= inset for a, b in zip(card, picture))


def drop_fully_owned_wrappers(views, frame):
    """Only one nested native control can be hit when it covers the whole inert outer target."""
    targets = {t["id"]: t for t in frame["targets"]}
    retired = set()
    for node in _walk(views):
        ref = node.get("target")
        target = targets.get(ref)
        if (not target or target["role"] not in _LAYOUT or target.get("point") is not None
                or target.get("actions")):
            continue
        children = [targets.get(c.get("target")) for c in node.get("children", ())]
        if any(t and t["bounds"] == target["bounds"] and "tap" in t.get("actions", ()) and t.get("point")
               for t in children):
            retired.add(ref)
            node.pop("target")
    if retired:
        frame["targets"][:] = [t for t in frame["targets"] if t["id"] not in retired]
        for row in frame["system_texts"]:
            if row.get("target") in retired:
                row["target"] = None


def _native_image_controls(views, frame):
    by_ref = {t["id"]: t for t in frame["targets"]}
    screen_area = frame["width"] * frame["height"]
    for node in _walk(views):
        t = by_ref.get(node.get("target"))
        if (not t or t["role"] not in _CARD_ROLES or "tap" not in t.get("actions", ()) or not t.get("point")
                or t.get("state", {}).get("password")):
            continue
        box = t["bounds"]
        if (box[2] - box[0]) * (box[3] - box[1]) >= .65 * screen_area:
            continue
        nested = list(_walk(node.get("children", ())))
        if any(c.get("target") for c in nested):
            continue
        images = [c for c in [node, *nested] if c["role"] == "image" and _fills_card(box, c.get("bounds"))]
        if len(images) != 1:
            continue
        # Native titles, values and printed text keep their primary status. An asset description
        # inherited solely from the full-size child image is also retained when its print is read.
        inherited = not any(node.get(k) for k in ("name", "text", "value")) and all(
            c is images[0] or not any(c.get(k) for k in ("name", "text", "value")) for c in nested)
        image = images[0]
        hint = image.get("system_hint") or image.get("name")
        explicit = image.get("system_name_source") in {"accessibility_text", "accessibility_label", "description"}
        yield t, hint if inherited and not explicit else None, image["bounds"]


def _scroll_clipped_edges(target, targets):
    path = target.get("tree_path", "")
    clipped = set()
    for ancestor in targets:
        ancestor_path = ancestor.get("tree_path")
        if "scroll" not in ancestor.get("actions", ()) or not ancestor_path or not path.startswith(ancestor_path + "/"):
            continue
        clipped.update(i for i, edge in enumerate(target["bounds"]) if edge == ancestor["bounds"][i])
    return clipped


def _has_painted_boundary(image, box, clipped=()):
    """A rounded or gradient card still has a sustained colour change across its straight edges."""
    x, y, r, b = box
    width, height = image.size
    dx, dy = (r - x) // 5, (b - y) // 5
    if not dx or not dy:
        return False
    step = max(1, min(4, round(.03 * min(r - x, b - y))))
    sides = []
    for index, horizontal, edge, inner in ((1, True, y, y + step), (3, True, b - 1, b - 1 - step),
                                          (0, False, x, x + step), (2, False, r - 1, r - 1 - step)):
        if index in clipped:
            continue
        stop = height if horizontal else width
        outer = 2 * edge - inner
        if not 0 <= outer < stop:
            continue
        if horizontal:
            a, z = ((x + dx, k, r - dx, k + 1) for k in (inner, outer))
        else:
            a, z = ((k, y + dy, k + 1, b - dy) for k in (inner, outer))
        near, far = (np.asarray(image.crop(rect), dtype=np.int16) for rect in (a, z))
        sides.append((np.max(np.abs(near - far), axis=-1) >= 18).mean() >= .72)
    return len(sides) >= 3 and all(sides)


def add_printed_card_titles(image, views, frame, excluded):
    controls = list(_native_image_controls(views, frame))
    try:
        from .mobile_card_text import read_card_titles

        # Reading comes before assigning a target. A positioned word may still stand on its own when
        # the UI tree does not establish that its enclosing view owns the corresponding operation.
        readings = read_card_titles(image, frame["targets"], excluded)
    except (ImportError, OSError, RuntimeError, ValueError):
        frame["warnings"].append("printed_card_text_unavailable")
        return
    if not readings:
        return
    by_id = {t["id"]: (t, hint) for t, hint, _ in controls}
    groups = defaultdict(list)
    edges = {}
    existing = {(r["text"], tuple(r["bounds"])): r for r in frame["screen_texts"]}
    for record in readings:
        text, box = record["text"], record["bounds"]
        # Leave a positioned native reading of the same ink in its original row.
        if any(row["text"] == text and _covers(row["bounds"], box)
               and (row["bounds"][3] - row["bounds"][1]) <= 1.9 * (box[3] - box[1])
               for row in frame.get("system_texts", ())):
            continue
        owners = [t["id"] for t, _, picture in controls if _covers(picture, box)]
        if len(owners) > 1:
            for ref in owners:
                if ref not in edges:
                    target = by_id[ref][0]
                    edges[ref] = _has_painted_boundary(image, target["bounds"],
                                                       _scroll_clipped_edges(target, frame["targets"]))
            owners = [ref for ref in owners if edges[ref]]
        ref = owners[0] if len(owners) == 1 else None
        if ref:
            card = by_id[ref][0]
            if any(t is not card and t.get("actions") and _covers(card["bounds"], t["bounds"])
                   and max(box[0], t["bounds"][0]) < min(box[2], t["bounds"][2])
                   and max(box[1], t["bounds"][1]) < min(box[3], t["bounds"][3]) for t in frame["targets"]):
                ref = None
        location = (text, tuple(box))
        prior = existing.get(location)
        if prior is not None and prior.get("target") not in (None, ref):
            continue
        if prior is None:
            prior = {"text": text, "bounds": list(box), "target": ref, "source": "screen"}
            frame["screen_texts"].append(prior)
            existing[location] = prior
        elif ref:
            prior["target"] = ref
        if ref:
            groups[ref].append(location)
    if frame["screen_texts"]:
        frame["screen_texts_available"] = True
    for ref, words in groups.items():
        target, hint = by_id[ref]
        unique = set(words)
        if len(unique) != 1 or (target.get("name") and target["name"] != hint):
            continue
        title, box = next(iter(unique))
        if title == target.get("name"):
            continue
        if hint:
            target["system_hint"] = hint
            target.setdefault("system_hint_kind", "image_name")
        target.update(name=title, name_source="screen", name_bounds=list(box))
