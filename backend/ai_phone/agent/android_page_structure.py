"""Build the public page hierarchy from the nodes accepted by the Android frame reader."""

from __future__ import annotations

from collections import defaultdict

from ai_phone.shared.mobile_frame import structure_from_observation


_WIDGET_ROLES = {
    "WebView": "document", "RecyclerView": "list", "ListView": "list", "ExpandableListView": "list",
    "GridView": "grid", "ScrollView": "scroll_area", "HorizontalScrollView": "scroll_area",
    "Toolbar": "toolbar", "ActionBar": "toolbar", "TabLayout": "tab_bar", "TabWidget": "tab_bar",
    "BottomNavigationView": "navigation", "NavigationView": "navigation", "Menu": "menu",
    "ListItem": "list_item", "Button": "button", "ImageButton": "button", "TextView": "text",
    "EditText": "input", "CheckBox": "toggle", "Switch": "toggle", "RadioButton": "toggle",
}
_ROLES = {
    "dialog", "alert", "list", "grid", "heading", "row", "navigation", "toolbar", "tab", "tab_bar",
    "menu", "button", "link", "image", "form", "section", "document", "list_item", "table",
}
_ROLE_ALIASES = {"alertdialog": "dialog", "listitem": "list_item", "tablist": "tab_bar", "img": "image"}


def _native_role(node):
    role = node.get("role", "").lower()
    if role in _ROLES or role in _ROLE_ALIASES:
        return _ROLE_ALIASES.get(role, role)
    if node.get("heading") == "true" or node.get("accessibility-heading") == "true":
        return "heading"
    widget = node.get("class", "").rsplit(".", 1)[-1]
    return _WIDGET_ROLES.get(widget, "group")


def _inside(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def page_structure(observation, visited, label_paths, modal_path, modal_kind, panel):
    """Use the exact XML path of each accepted text and control. Never reread raw accessibility labels."""
    keyboard = observation["keyboard"]
    ime = keyboard.get("bounds") if keyboard.get("visible") else None

    def readable(box):
        return (not panel or _inside(panel, box)) and (not ime or not _overlaps(ime, box))

    targets, by_path = [], {}
    for original in observation["targets"]:
        target = original
        box = target["bounds"]
        if (ime and _inside(ime, box)) or (panel and not _inside(panel, box)):
            continue
        path = target.get("tree_path")
        if path in visited:
            node, _, _, protected = visited[path]
            if protected:
                if node.get("password") != "true":
                    continue
                target = {**target, "state": {**target.get("state", {}), "password": True}}
        if target.get("name") and not readable(target.get("name_bounds", box)):
            target = {**target, "name": None, "name_source": None}
        if target.get("point") is None and "tap" in target["actions"]:
            target = {**target, "actions": [action for action in target["actions"] if action != "tap"]}
        if path in visited:
            by_path[path] = target
        targets.append(target)

    by_id = {target["id"]: target for target in targets}
    texts_by_path, system_rows = defaultdict(list), []
    for row, path in zip(observation["system_texts"], label_paths, strict=True):
        if readable(row["bounds"]):
            owner = by_id.get(row.get("target"))
            if row.get("target") and not owner:
                row = {**row, "target": None}
            system_rows.append(row)
            if (owner and owner.get("name_source") == "system" and owner["name"] == row["text"]
                    and owner.get("name_bounds") == row["bounds"]):
                continue  # The target already exposes this exact label at this position.
            texts_by_path[path].append(row)

    # Rebuild the accepted Android tree bottom-up without copying wrapper chains or hidden XML siblings.
    # Parent paths use child indexes from the public Android target reader, so sibling order stays native.
    nested = defaultdict(list)
    for path, (node, box, enabled, protected) in reversed(visited.items()):
        children = list(reversed(nested.pop(path, [])))
        target = by_path.get(path)
        rows = texts_by_path.get(path, ())
        role = modal_kind if path == modal_path else _native_role(node)
        if protected:
            children = []
        if not (target or rows or children or (path == modal_path and modal_kind)):
            continue
        parent = path.rpartition("/")[0] if "/" in path else None
        if not target and not rows and role == "group" and len(children) <= 1:
            nested[parent].extend(reversed(children))
            continue
        view = {"role": role}
        if box:
            view["bounds"] = list(panel if path == modal_path else box)
        if not enabled:
            view["state"] = {"enabled": False}
        if target:
            view["target"] = target["id"]
            if role in ("group", "text"):
                view["role"] = target["role"]
        elif rows:
            row, *rows = rows
            view.update(text=row["text"], source="system")
            if role == "group" and not children:
                view["role"] = "text"
        text_children = [{"role": "text", "text": row["text"], "source": "system", "bounds": row["bounds"]}
                         for row in rows]
        if children or text_children:
            view["children"] = text_children + children
        nested[parent].append(view)

    # An explicit modal may be below several unvisited ancestors of the hierarchy root.
    roots = [view for group in nested.values() for view in reversed(group)]
    facts = {**observation, "targets": targets, "system_texts": system_rows,
             "screen_texts": [row for row in observation["screen_texts"] if readable(row["bounds"])]}
    return structure_from_observation(facts, native_nodes=roots)
