"""Map HarmonyOS UiTest's visible component tree to the common frame vocabulary."""

from __future__ import annotations

import re

from .harmony_sim_panels import Screen, reconcile_drawer
from math import isfinite


_RECT = re.compile(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]")
_ROLES = {
    "button": "button",
    "toggle": "switch",
    "switch": "switch",
    "checkbox": "checkbox",
    "checkboxgroup": "checkbox",
    "radio": "radio",
    "slider": "slider",
    "text": "text",
    "span": "text",
    "statictext": "text",
    "textclock": "text",
    "heading": "heading",
    "paragraph": "paragraph",
    "listitem": "list_item",
    "link": "link",
    "textfield": "input",
    "image": "image",
    "symbolglyph": "image",
    "textinput": "input",
    "textarea": "input",
    "search": "input",
    "richeditor": "input",
    "list": "list",
    "grid": "grid",
    "scroll": "scroll_area",
    "swiper": "scroll_area",
    "tabbar": "navigation",
    "tab": "tab",
    "tabbaritem": "tab",
    "option": "option",
    "listboxoption": "option",
    "tabs": "navigation",
    "navigation": "navigation",
    "menu": "menu",
    "menuitem": "menuitem",
    "dialog": "dialog",
    "alertdialog": "dialog",
    "customdialog": "dialog",
    "sheetwrapper": "dialog",
    "keyboard": "keyboard",
    "inputmethodwindow": "keyboard",
    "web": "web",
    "rootwebarea": "document",
    "progressindicator": "progress",
    "loadingprogress": "loading",
}


def _bool(value):
    if type(value) is bool:
        return value
    return {"true": True, "false": False}.get(str(value).strip().lower())


def rect(value):
    if isinstance(value, str):
        match = _RECT.fullmatch(value.strip())
        value = tuple(map(int, match.groups())) if match else None
    if isinstance(value, dict):
        value = [value.get(k) for k in ("left", "top", "right", "bottom")]
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(type(v) is int for v in value):
        left, top, right, bottom = value
        if right > left and bottom > top:
            return left, top, right, bottom
    return None


def _clip(box, outer):
    if not box:
        return None
    clipped = (
        max(box[0], outer[0]),
        max(box[1], outer[1]),
        min(box[2], outer[2]),
        min(box[3], outer[3]),
    )
    return clipped if clipped[0] < clipped[2] and clipped[1] < clipped[3] else None


def _clean(value):
    return value.strip() if isinstance(value, str) else ""


def _zero_opacity(attrs):
    try:
        return float(attrs["opacity"]) == 0
    except (KeyError, ValueError, TypeError):
        return False


def _arkweb_opacity_stub(root):
    # Some UiTest versions write zero for EVERY virtual accessibility node,
    # including their visible rootWebArea. Mixed compositor values are real.
    if not root.get("children") or not _zero_opacity(root["attributes"]):
        return False
    stack = [root]
    while stack:
        current = stack.pop()
        attrs = current.get("attributes", {})
        if (
            _bool(attrs.get("visible")) is True
            and attrs.get("opacity") not in (None, "")
            and not _zero_opacity(attrs)
        ):
            return False
        stack.extend(child for child in (current.get("children") or []) if isinstance(child, dict))
    return True


def _visible(attrs, virtual_opacity):
    visible = _bool(attrs.get("visible"))
    if (
        visible is False
        or _bool(attrs.get("ariaHidden"))
        or _bool(attrs.get("aria-hidden"))
        or attrs.get("accessibilityLevel") == "no-hide-descendants"
    ):
        return False
    return not _zero_opacity(attrs) or (virtual_opacity and visible is True)


def _number(value, default=None):
    try:
        answer = float(value)
        return answer if isfinite(answer) else default
    except (TypeError, ValueError):
        return default


def _route_cover(children, viewport):
    destinations = []
    for index, child in enumerate(children):
        a = child.get("attributes") or {}
        if a.get("type") == "NavDestination" and _visible(a, False):
            order = (_number(a.get("zIndex"), 0), index)
            destinations.append((order, a))
    front = [(order, a) for order, a in destinations if _bool(a.get("focused")) is True]
    if len(front) != 1 or len(destinations) < 2:
        return None
    order, a = front[0]
    opaque_fill = re.fullmatch(r"#(?:[fF]{2})?[0-9a-fA-F]{6}", str(a.get("backgroundColor")))
    box = _clip(rect(a.get("bounds")), viewport)
    if opaque_fill and _number(a.get("opacity")) == 1 and box:
        return order, box
    return None


def _uncovered(box, covers):
    """Choose a visible rectangle for a native target partially behind another route."""
    parts = [box] if box else []
    for cover in covers:
        remaining = []
        for part in parts:
            overlap = _clip(part, cover)
            if not overlap:
                remaining.append(part)
                continue
            x, y, r, b = part
            x1, y1, x2, y2 = overlap
            pieces = ((x, y, r, y1), (x, y2, r, b), (x, y1, x1, y2), (x2, y1, r, y2))
            remaining.extend(p for p in pieces if p[2] > p[0] and p[3] > p[1])
        parts = remaining
    return max(parts, key=lambda p: (p[2] - p[0]) * (p[3] - p[1]), default=None)


def _items(root):
    yield root
    for child in root["children"]:
        yield from _items(child)


def _hit_items(root, passing=False, constrained=False):
    # A child may explicitly accept taps within a passing-through parent.
    # Neither this override nor an unparsed clip supplies a full-page hit shape.
    constrained = constrained or passing or root.get("_hit_clip", False)
    if root.get("_pass_through"):
        passing = True
    elif root.get("_accepts_hit"):
        passing = False
    if not passing:
        yield root, constrained
    for child in root["children"]:
        yield from _hit_items(child, passing, constrained)


def _label_footprint(root, page, *, owned=False):
    """Readable, spatially separated labels, optionally belonging to one hit owner."""
    width, height = page[2] - page[0], page[3] - page[1]
    boxes, stack = [], [root]
    while stack:
        child = stack.pop()
        if owned and child is not root and (child["actions"] or child.get("_pass_through")):
            continue
        box = child["bounds"]
        label = child.get("text") or child.get("name") or ""
        if (
            box
            and box != page
            and min(box[2] - box[0], box[3] - box[1]) >= 4
            and child["role"] != "image"
            and any(c.isalnum() for c in label)
        ):
            boxes.append(box)
        stack.extend(child["children"])
    if len(boxes) < 2:
        return None
    xs, ys = zip(*[((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes])
    if (max(xs) - min(xs)) / width < 0.20 and (max(ys) - min(ys)) / height < 0.20:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1]) if box else 0


def _web_layers(document, warnings):
    """Keep visible words; distinguish confirmed catching from intersecting Web hit areas."""
    page = document["bounds"]
    if not page:
        return
    for parent in _items(document):
        full = [c for c in parent["children"] if c["bounds"] == page]
        if len(full) < 2:
            continue
        layers = [(c, foot) for c in full if (foot := _label_footprint(c, page))]
        layers.sort(key=lambda layer: layer[0].get("_web_z", 0))
        for idx, (front, footprint) in enumerate(layers):
            if not idx or not front.get("_web_extent"):
                continue
            hit = list(_hit_items(front))
            local = [
                n["bounds"] for n, _ in hit if n["bounds"] and n["bounds"] != page and "tap" in n["actions"]
            ]
            constrained_shape = any(constrained for _, constrained in hit)
            block = any(
                n["bounds"] == page and n.get("_blocks_hit") and not constrained for n, constrained in hit
            )
            catches = block or any(
                n["bounds"] == page
                and not constrained
                and "tap" in n["actions"]
                and (
                    _label_footprint(n, page, owned=True)
                    or (not n["children"] and not n.get("name") and not n.get("text") and local)
                )
                for n, constrained in hit
            )
            if not catches and not local:
                continue
            changed = False
            for behind, old_foot in layers[:idx]:
                if behind.get("_web_extent") != front["_web_extent"]:
                    continue
                common = _clip(old_foot, footprint)
                # A wide accessibility wrapper can contain a narrow pane. Two
                # disjoint columns keep their own controls unless a blocking
                # platform hit-test mode is explicitly reported.
                if not block and not common:
                    continue
                broad_overlap = (
                    not constrained_shape
                    and _area(footprint) >= 0.35 * _area(page)
                    and _area(common) >= 0.35 * min(_area(old_foot), _area(footprint))
                )
                for item in _items(behind):
                    if not item["actions"] or not item["bounds"]:
                        continue
                    if not catches and not broad_overlap and not any(_clip(item["bounds"], b) for b in local):
                        continue
                    if "tap" in item["actions"]:
                        item["actions"] = ([] if catches else ["try_tap"]) + [
                            a for a in item["actions"] if a != "tap"
                        ]
                        changed = True
                    if catches and "scroll" in item["actions"]:
                        item["actions"].remove("scroll")
                        changed = True
            if changed:
                warning = (
                    "Web 全屏交互层覆盖了较早的控件，两层文字可能同时可见，点击请使用前景区域"
                    if catches
                    else "Web 的可交互内容在同一位置重叠，较早一层的 tap 已标为 try_tap，点击归属需从下一帧核对"
                )
                if warning not in warnings:
                    warnings.append(warning)


def _node(
    raw,
    viewport,
    inherited_disabled=False,
    virtual_opacity=False,
    web_document=False,
    covers=(),
    opaque_context=True,
    warnings=None,
    in_web=False,
):
    if not isinstance(raw, dict) or not isinstance(raw.get("attributes"), dict):
        return None
    a = raw["attributes"]
    if not _visible(a, virtual_opacity or web_document):
        return None
    original_bounds = rect(a.get("bounds"))
    clipped = _clip(original_bounds, viewport)
    if original_bounds and not clipped:
        return None
    box = _uncovered(clipped, covers)
    kind = _clean(a.get("type")) or "window"
    role = _ROLES.get(kind.lower(), "group")
    enabled = _bool(a.get("enabled"))
    disabled = inherited_disabled or enabled is False
    children = []
    child_viewport = (
        clipped
        if clipped and (_bool(a.get("clip")) or role == "web" or kind == "NavDestination")
        else viewport
    )
    raw_children = [c for c in (raw.get("children") or []) if isinstance(c, dict)]
    opaque_context = opaque_context and _number(a.get("opacity"), 1) == 1
    front = (
        _route_cover(raw_children, child_viewport) if opaque_context and kind == "NavigationContent" else None
    )
    covered_route = False
    for index, item in enumerate(raw_children):
        attrs = item.get("attributes") or {}
        hidden_by = covers
        if front and attrs.get("type") == "NavDestination" and _bool(attrs.get("focused")) is False:
            if (_number(attrs.get("zIndex"), 0), index) < front[0]:
                hidden_by = (*covers, front[1])
                covered_route |= _clip(rect(attrs.get("bounds")), front[1]) is not None
        is_web_root = (
            role == "web" and _clean((item.get("attributes") or {}).get("type")).lower() == "rootwebarea"
        )
        use_virtual_visibility = virtual_opacity or (is_web_root and _arkweb_opacity_stub(item))
        if (
            child := _node(
                item,
                child_viewport,
                disabled,
                use_virtual_visibility,
                is_web_root,
                hidden_by,
                opaque_context,
                warnings,
                in_web or is_web_root,
            )
        ) is not None:
            children.append(child)
    if (
        covered_route
        and warnings is not None
        and not any(
            i.get("text") or i.get("name") or i.get("actions") for child in children for i in _items(child)
        )
    ):
        warnings.append("前景路由已覆盖上一页，本帧的前景路由尚未报告有名字或可操作的控件，可重新观察")
    # An empty or invisible role must not suppress children that do have usable bounds.
    if not box and not children:
        return None
    accessible_name = _clean(a.get("accessibilityText"))
    description = _clean(a.get("description"))
    name = accessible_name or description
    text = _clean(a.get("text"))
    if role in ("web", "document"):
        # ArkWeb exposes the navigation URL (including its query) as Web.text.
        # Neither the navigation address nor a document title is drawn content.
        name, text = "", ""
    elif kind == "image":
        # Chromium image text is its alternative label, not printed lettering.
        fallback = "" if re.match(r"(?:https?://|data:|blob:)", text, re.I) else text
        name, text = name or fallback, ""
    input_type = str(a.get("inputType", "")).lower()
    protected = (
        _bool(a.get("password")) is True
        or _bool(a.get("isPassword")) is True
        or "password" in input_type
        or "password" in kind.lower()
        or (role == "input" and bool(text) and all(ch in "●•*·" for ch in text))
    )
    if protected:
        # The description and child text sometimes repeat the typed value.
        name = _clean(a.get("hint")) or None
        text, children = "", []
    if role == "input" and not name:
        name = _clean(a.get("hint"))
    state = {}
    for key in ("enabled", "focused", "checkable"):
        value = _bool(a.get(key))
        if value is not None:
            state[key] = value
    selected = _bool(a.get("selected"))
    aria_selected = _bool(a.get("aria-selected", a.get("ariaSelected")))
    if aria_selected is not None:
        state["selected"] = aria_selected
    elif selected is True or (
        selected is False and (role in ("tab", "option") or _bool(a.get("selectable")))
    ):
        # ArkWeb reports selected=false on ordinary buttons whether or not they
        # have a custom selected mark. Emit a negative only for a selectable role.
        state["selected"] = selected
    if disabled:
        state["enabled"] = False
    if state.get("checkable") or role in ("switch", "checkbox", "radio"):
        checked = _bool(a.get("checked"))
        if checked is not None:
            state["checked"] = checked
    actions = []
    if box and not disabled:
        if _bool(a.get("clickable")) is True:
            actions.append("tap")
        if _bool(a.get("scrollable")) is True:
            actions.append("scroll")
    if role == "text" and actions == ["tap"]:
        role = "button"
    if role == "dialog":
        # A Harmony Dialog often has full-screen scrim bounds and one clearly
        # bounded content layout. Expose that layout instead of the scrim.
        actions = []
        if box and len(children) == 1 and children[0].get("bounds"):
            inner = children[0]["bounds"]
            if _clip(inner, box) == tuple(inner):
                box = tuple(inner)
    result = {
        "role": role,
        "type": kind,
        "name": name or None,
        "text": text or None,
        "bounds": list(box) if box else None,
        "actions": actions,
        "state": state,
        "children": children,
    }
    if role == "image" and name:
        result["system_name_source"] = (
            "accessibility_text" if accessible_name else "description" if description else "image_text"
        )
    if in_web:
        opacity = _number(a.get("opacity"))
        color = _clean(a.get("backgroundColor"))
        if (opacity is not None and 0 < opacity < 1) or (
            re.fullmatch(r"#[0-9A-Fa-f]{8}", color) and color[1:3].lower() != "ff"
        ):
            result["_possibly_transparent"] = True
        result["_web_extent"] = rect(a.get("origBounds")) or original_bounds
        result["_web_z"] = _number(a.get("zIndex"), 0)
        hit_mode = _clean(a.get("hitTestBehavior")).lower().split(".")[-1]
        pointer = _clean(a.get("pointerEvents") or a.get("pointer-events")).lower()
        if _clean(a.get("clipPath") or a.get("clip-path")).lower() not in ("", "none"):
            result["_hit_clip"] = True
        if hit_mode in {"transparent", "none"} or pointer in {"none", "box-none"}:
            result["_pass_through"] = True
        elif pointer in {"auto", "all", "box-only"} or hit_mode in {"default", "block"}:
            result["_accepts_hit"] = True
        if hit_mode == "block" and not result.get("_pass_through"):
            result["_blocks_hit"] = True
    if role == "input" and not protected:
        result["value"] = text or None
    if protected:
        result["protected"] = True
        result["safe_label"] = _clean(a.get("hint")) or None
    if role == "dialog":
        result["modal"] = True
    resource = _clean(a.get("id"))
    if resource:
        result["resource_id"] = resource
    return result


def _foreground(nodes):
    dialogs, keyboards = [], []

    def visit(node):
        if node.get("modal"):
            dialogs.append(node)
        if node["role"] == "keyboard":
            keyboards.append(node)
        for child in node["children"]:
            visit(child)

    for node in nodes:
        visit(node)
    return [dialogs[-1], *keyboards] if dialogs else nodes


def native_page(raw, width, height, *, warnings=None, screenshot=None):
    """Return visible native roots, bundle/Ability, and authoritative validity."""
    viewport = (0, 0, width, height)
    messages = warnings if warnings is not None else []
    screen = Screen(screenshot, (width, height)) if screenshot else None
    if isinstance(raw, dict) and isinstance(raw.get("attributes"), dict):
        records = [raw]
    elif isinstance(raw, list):
        records = [r for r in raw if isinstance(r, dict) and isinstance(r.get("attributes"), dict)]
    else:
        records = []
    nodes, app_windows = [], []
    for record in records:
        attrs = record["attributes"]
        # UiTest uses a role-less outer container for all windows on the display.
        windows = (
            (record.get("children") or [])
            if not attrs.get("type") and not attrs.get("bundleName")
            else [record]
        )
        for window in windows:
            node = _node(window, viewport, warnings=messages)
            if node is None:
                continue
            for document in _items(node):
                if document["role"] == "document":
                    if screen:
                        reconcile_drawer(document, screen, messages)
                    _web_layers(document, messages)
            nodes.append(node)
            a = window["attributes"]
            if a.get("bundleName"):
                box = node["bounds"] or viewport
                priority = (
                    bool(a.get("abilityName")),
                    _bool(a.get("focused")) is True,
                    (box[2] - box[0]) * (box[3] - box[1]),
                )
                app_windows.append((priority, _clean(a["bundleName"]), _clean(a.get("abilityName"))))
    active = max(app_windows, default=((), None, None), key=lambda row: row[0])
    return _foreground(nodes), active[1], active[2], bool(nodes)
