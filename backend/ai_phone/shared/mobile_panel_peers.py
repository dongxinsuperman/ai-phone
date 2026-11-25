"""Recover a faint, physically enclosed peer of an already observed labelled control candidate."""

from __future__ import annotations


def _covers(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _overlaps(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _same_row_or_column(box, peer):
    w, h = box[2] - box[0], box[3] - box[1]
    pw, ph = peer[2] - peer[0], peer[3] - peer[1]
    if _overlaps(box, peer) or abs(w - pw) > .14 * pw or abs(h - ph) > .21 * ph:
        return False
    dx = abs((box[0] + box[2] - peer[0] - peer[2]) / 2)
    dy = abs((box[1] + box[3] - peer[1] - peer[3]) / 2)
    return (dx <= .08 * pw and dy <= 3 * ph) or (dy <= .10 * ph and dx <= 3 * pw)


def _has_subtitle(title, records):
    return any(row["text"] != title["text"] and row["bounds"][1] > title["bounds"][1]
               and row["bounds"][3] > title["bounds"][3] for row in records)


def _label_groups(views, rows):
    records = {(r["text"], tuple(r["bounds"])): r for r in rows}
    headings, labels = [], []

    def visit(nodes, group):
        for node in nodes:
            kids = node.get("children", ())
            role, text = node.get("role"), node.get("text")
            if text and node.get("bounds") and not node.get("target"):
                row = records.get((text, tuple(node["bounds"])))
                if row is not None:
                    if role in {"heading", "title"}:
                        headings.append((row, group))
                    if role in {"text", "label", "heading", "title"}:
                        labels.append((row, group))
            # AX frequently inserts anonymous one-child wrappers between sibling text lines.
            next_group = id(node) if len(kids) > 1 or (kids and node.get("name") and not text) else group
            visit(kids, next_group)

    visit(views, None)
    return headings, labels


def recover_labelled_peers(image, views, targets, rows, excluded, limit):
    """Requires an aligned, already measured peer, another measured panel, and native sibling text."""
    known = {t["id"]: t for t in targets if t.get("system_name_source") == "contained_heading"
             and t.get("source") == "pixels"}
    if not known:
        return False
    headings, labels = _label_groups(views, rows)
    peers = [(row, group, known[row["target"]]["bounds"]) for row, group in headings
             if row.get("target") in known and row["text"] == known[row["target"]]["name"]
             and _has_subtitle(row, (r for r, g in labels if g == group and r.get("target") == row["target"]))]
    pending = [(r, g) for r, g in headings if not r.get("target") and any(g == p[1] for p in peers)]
    if not pending:
        return False
    from ai_phone.agent.android_visible_text import _panels

    panels = [p.box for p in _panels(image)]
    obstacles = list(excluded) + [t["bounds"] for t in targets if t.get("bounds") and t.get("point")
                                  and set(t.get("actions", ())) & {"tap", "try_tap", "input"}]
    index = max((int(t["id"][1:]) for t in targets if t["id"].startswith("V") and t["id"][1:].isdigit()), default=0)
    for title, group in pending:
        if title.get("target"):
            continue
        matches = [box for box in panels if _covers(box, title["bounds"])
                   and any(g == group and _same_row_or_column(box, peer) for _, g, peer in peers)
                   and not any(_overlaps(box, blocked) for blocked in obstacles)]
        if len(matches) != 1:
            continue
        box = matches[0]
        # An independent heading in the same panel makes its identity ambiguous.
        other_titles = [r for r, _ in headings if _covers(box, r["bounds"])
                        and (r["text"] != title["text"]
                             or max(abs(a - b) for a, b in zip(title["bounds"], r["bounds"])) > 2)]
        body = [r for r, g in labels if group == g and not r.get("target") and _covers(box, r["bounds"])]
        if other_titles or not _has_subtitle(title, body):
            continue
        if len(targets) >= limit:
            return True
        index += 1
        ident = f"V{index}"
        x, y, r, b = title["bounds"]
        targets.append({"id": ident, "source": "pixels", "role": "enclosed_surface", "actions": ["try_tap"],
                        "name": title["text"], "name_source": "system", "name_bounds": list(title["bounds"]),
                        "system_name_source": "contained_heading", "bounds": list(box), "state": {},
                        "point": [(x + r) // 2, (y + b) // 2]})
        for row in body:
            row["target"] = ident
        obstacles.append(box)
    return False
