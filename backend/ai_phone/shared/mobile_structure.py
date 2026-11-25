"""A readable, ordered view of the same controls and words that an observation exposes."""

from __future__ import annotations

from collections import defaultdict


_GROUP_ROLES = {"application", "page", "window", "group", "container", "layout", "view", "other"}
_AREA_ROLES = {
    "dialog", "alert", "sheet", "drawer", "keyboard", "list", "list_item", "row",
    "section", "navigation", "toolbar", "tab_bar", "table", "scroll_area", "form", "menu",
}


def _area(bounds):
    return (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])


def _encloses(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _target_node(target):
    return {
        "role": target.get("role") or "control",
        "target": target["id"],
        "name": target.get("name"),
        "name_source": target.get("name_source"),
        "source": target.get("source"),
        "actions": list(target.get("actions") or []),
        "bounds": target.get("bounds"),
        **({"state": target["state"]} if target.get("state") else {}),
        **{key: target[key] for key in ("system_hint", "system_hint_kind", "system_name_source") if target.get(key)},
    }


def _ancestors(path):
    while isinstance(path, str) and "/" in path:
        path = path.rsplit("/", 1)[0]
        yield path


def _place_captioned_surfaces(page, by_id, refs):
    """Keep related native captions and the pixel target together at the original reading position."""
    if not refs:
        return
    owners, captions = {}, defaultdict(list)

    def visit(parent, inside=None):
        for node in parent.get("children", ()):
            ident = node.get("target")
            if ident in refs:
                owners[ident] = parent
            ref = node.get("belongs_to")
            own_caption = ref in refs and node.get("role") in {"heading", "title", "label", "text"} and not ident
            if own_caption and ref != inside:
                captions[ref].append((parent, node))
            visit(node, ref if own_caption else inside)

    visit(page)
    for ref, entries in captions.items():
        surface = by_id[ref]
        parent = entries[0][0]
        if ref not in owners or parent is surface or any(p is not parent for p, _ in entries):
            continue
        if not any(n.get("role") in {"heading", "title"} and n.get("text") == surface["name"] for _, n in entries):
            continue
        # Leave independently actionable descendants at their native position.
        def contains_control(node):
            return bool(node.get("target")) or any(contains_control(child) for child in node.get("children", ()))

        if any(contains_control(node) for _, node in entries):
            continue
        owners[ref]["children"] = [n for n in owners[ref]["children"] if n is not surface]
        indexed = {id(n) for _, n in entries}
        first = entries[0][1]
        parent["children"] = [surface if n is first else n for n in parent["children"]
                              if n is first or id(n) not in indexed]
        surface["children"] = [n for _, n in entries] + surface.get("children", [])


def _rows_with_repeated_actions(nodes):
    """Expose a repeated column of same-named buttons beside their own visible captions.

    A WebView can publish row titles and buttons as flat siblings. Only group a caption which
    overlaps exactly one button's vertical extent. A layout row itself has no inferred action.
    """
    buttons = [(i, n) for i, n in enumerate(nodes)
               if n.get("target") and n.get("bounds") and n.get("name")
               and n.get("source") != "pixels" and "tap" in n.get("actions", ())]
    if len(buttons) < 2 or len({n["name"] for _, n in buttons}) == len(buttons):
        return nodes
    owner = {}
    groups = {}
    for i, button in buttons:
        left, top, right, bottom = button["bounds"]
        width = right - left
        # Independently selectable peers in the same row make attachment ambiguous.
        peers = [(j, other) for j, other in buttons if j != i]
        if any(t < bottom and top < b for _, other in peers for _, t, _, b in [other["bounds"]]):
            continue
        column = [other for _, other in peers if abs(other["bounds"][0] - left) <= .25 * width
                  and abs(other["bounds"][2] - right) <= .25 * width]
        if not any(n["name"] == button["name"] for n in column):
            # Include the first row of a list when the other rows share a different action caption.
            if len(column) < 2 or len({n["name"] for n in column}) == len(column):
                continue
        labels = []
        for j, candidate in enumerate(nodes):
            box = candidate.get("bounds")
            if (not box or candidate.get("target") or candidate.get("children")
                    or not (candidate.get("text") or candidate.get("name") or candidate.get("value"))
                    or candidate.get("role") not in {"image", "text", "heading", "label"}):
                continue
            x, y, r, b = box
            middle = (y + b) / 2
            if (r <= left or right <= x) and top <= middle < bottom and b - y <= 1.3 * (bottom - top):
                labels.append(j)
        if not any(nodes[j].get("text") and nodes[j].get("role") != "image" for j in labels):
            continue
        members = sorted([i, *labels])
        start = members[0]
        for j in members:
            owner[j] = start
        boxes = [nodes[j]["bounds"] for j in members]
        bounds = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes)]
        groups[start] = {"role": "row", "source": "layout", "bounds": bounds, "children": [nodes[j] for j in members]}
    return [groups.get(i, n) for i, n in enumerate(nodes) if i not in owner or i == owner[i]]


def _tidy(nodes, *, native_order):
    result = []
    for node in nodes:
        children = _tidy(node.pop("children", ()), native_order=native_order)
        # A heading can contain its own accessibility text leaf at effectively the same position.
        if node["role"] in {"heading", "title"} and node.get("text") and node.get("bounds"):
            reduced = []
            for child in children:
                if (child.get("role") in {"text", "heading", "title", "label"} and not child.get("target")
                        and child.get("text") == node["text"] and child.get("bounds")
                        and max(abs(a - b) for a, b in zip(node["bounds"], child["bounds"])) <= 2):
                    reduced.extend(child.get("children", ()))
                else:
                    reduced.append(child)
            children = reduced
        # Accessible trees often contain long chains of anonymous layout wrappers.
        # Keep a branching grouping, and always keep a semantic section or action.
        if (node["role"] in _GROUP_ROLES and not any(node.get(k) for k in ("target", "name", "text", "value"))
                and not node.get("state") and len(children) <= 1):
            result.extend(children)
            continue
        if (len(children) == 1 and node["role"] == children[0]["role"]
                and not any(node.get(k) for k in ("target", "name", "text", "value", "state"))):
            result.extend(children)
            continue
        if children:
            node["children"] = children
        if node.get("target") or children or any(k in node for k in ("text", "name", "value", "state", "system_hint")):
            result.append(node)
    if not native_order:
        result.sort(key=lambda node: ((node.get("bounds") or [0, 0])[1], (node.get("bounds") or [0, 0])[0]))
    return _rows_with_repeated_actions(result)


def structure_from_observation(observation: dict, *, native_nodes: list[dict] | None = None) -> dict:
    """Use native parentage when supplied. Older Android Agents retain their recorded target paths.

    Names and target references come from the final list, including any pixel supplementation.
    A word can belong to a control without becoming a separately executable control itself.
    """
    if native_nodes is None and isinstance(observation.get("structure"), dict):
        return observation["structure"]
    targets = {t["id"]: t for t in observation.get("targets", ()) if isinstance(t, dict) and t.get("id")}
    page = {"role": "page", "children": []}
    by_id = {}
    containers = []
    present = defaultdict(list)
    native_order = native_nodes is not None

    def copy_node(original):
        node = {k: original[k] for k in (
            "role", "name", "name_source", "text", "value", "source", "bounds", "state", "level",
            "system_hint", "system_hint_kind", "system_name_source"
        ) if k in original}
        node.setdefault("role", "group")
        ident = original.get("target")
        if ident in targets and ident not in by_id:
            target_view = _target_node(targets[ident])
            if node["role"] not in _GROUP_ROLES | {"control"}:
                target_view["role"] = node["role"]
            node.update(target_view)
            by_id[ident] = node
        if node.get("bounds"):
            box = tuple(node["bounds"])
            for text in (node.get("name"), node.get("text"), node.get("value")):
                if isinstance(text, str) and text:
                    present[(text, box)].append(node)
            if node["role"] in _AREA_ROLES:
                containers.append(node)
        node["children"] = [copy_node(child) for child in original.get("children", ()) if isinstance(child, dict)]
        return node

    if native_nodes:
        page["children"] = [copy_node(node) for node in native_nodes]

    def container_for(bounds):
        if not bounds:
            return page
        # Geometry supplies only a containing region. It never declares the text or region clickable.
        choices = [n for n in containers if _encloses(n["bounds"], bounds) and n["bounds"] != bounds]
        if not choices:
            return page
        choices.sort(key=lambda n: _area(n["bounds"]))
        if len(choices) > 1 and _area(choices[0]["bounds"]) == _area(choices[1]["bounds"]):
            return page
        return choices[0]

    # Materialize all target views before linking children to parents, irrespective of list order.
    additions = []
    for ident, target in targets.items():
        if ident not in by_id:
            by_id[ident] = _target_node(target)
            additions.append(target)
    paths = {t["tree_path"]: by_id[ident] for ident, t in targets.items() if isinstance(t.get("tree_path"), str)}
    for target in additions:
        node = by_id[target["id"]]
        parent = next((paths[path] for path in _ancestors(target.get("tree_path")) if path in paths), None)
        if parent is None:
            parent = container_for(node.get("bounds"))
        parent.setdefault("children", []).append(node)
        if node["role"] in _AREA_ROLES and node.get("bounds"):
            containers.append(node)

    seen_rows = set()
    for key, source in (("system_texts", "system"), ("screen_texts", "screen")):
        for row in observation.get(key) or ():
            if not isinstance(row, dict):
                continue
            text, box, ident = row.get("text"), row.get("bounds"), row.get("target")
            if not text or not box:
                continue
            position = (text, tuple(box))
            witness = (position, ident)
            if witness in seen_rows:
                continue
            seen_rows.add(witness)
            owner = by_id.get(ident)
            candidates = present.get(position, ())
            if candidates:
                if ident:
                    for item in candidates:
                        if "target" not in item:
                            item["belongs_to"] = ident
                continue
            if owner and text in (owner.get("name"), owner.get("text"), owner.get("value")):
                continue
            leaf = {"role": "text", "text": text, "source": source, "bounds": box}
            parent = owner or container_for(box)
            parent.setdefault("children", []).append(leaf)

    if not ("system_texts" in observation or "screen_texts" in observation):
        # Pre-located-text Agents reported plain strings. Do not make up locations or provenance.
        known = {t.get("name") for t in targets.values()}
        for text in dict.fromkeys(observation.get("texts", ())):
            if text and text not in known:
                page["children"].append({"role": "text", "text": text, "source": "unknown"})
    page["children"] = _tidy(page["children"], native_order=native_order)
    refs = {ident for ident, target in targets.items() if target.get("system_name_source") == "contained_heading"}
    _place_captioned_surfaces(page, by_id, refs)
    return page
