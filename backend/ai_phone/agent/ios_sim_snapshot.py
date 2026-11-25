"""Normalize an XCTest accessibility snapshot in the pixel coordinates of one capture."""
from __future__ import annotations

import math
import re
import unicodedata
from xml.etree import ElementTree as ET


_ROLES = {
    "Application": "application", "Window": "window", "NavigationBar": "navigation",
    "TabBar": "navigation", "Toolbar": "toolbar", "CollectionView": "list",
    "Table": "list", "Outline": "list", "ScrollView": "scroll_area", "WebView": "webview",
    "Cell": "cell", "StaticText": "text", "Heading": "heading", "Image": "image",
    "Icon": "icon", "Button": "button", "Link": "link", "Key": "key", "Switch": "switch",
    "Toggle": "switch", "Tab": "tab", "CheckBox": "checkbox", "RadioButton": "radio",
    "TextField": "input", "TextView": "input", "SearchField": "input",
    "SecureTextField": "input", "Keyboard": "keyboard", "Menu": "menu",
    "MenuItem": "menu_item", "SegmentedControl": "group", "Slider": "slider",
    "Picker": "picker", "PickerWheel": "picker", "Alert": "dialog", "Sheet": "dialog",
    "PageIndicator": "page_indicator", "ActivityIndicator": "progress", "ProgressIndicator": "progress",
    "StatusBar": "status_bar",
}
_TAPPABLE = {"button", "link", "tab", "switch", "checkbox", "radio", "key", "menu_item", "icon"}
_SCROLLABLE = {"list", "scroll_area", "picker"}
_INPUT_TYPES = {"XCUIElementTypeTextField", "XCUIElementTypeTextView",
                "XCUIElementTypeSecureTextField", "XCUIElementTypeSearchField"}


def _flag(value):
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    return None


def _type(element):
    return (element.get("type") or element.tag).removeprefix("XCUIElementType")


def _box(element, logical, pixels):
    try:
        x, y, w, h = (float(element.attrib[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or min(w, h) <= 0:
        return None
    sx, sy = pixels[0] / logical[0], pixels[1] / logical[1]
    left, top = max(0, math.floor(x * sx)), max(0, math.floor(y * sy))
    right, bottom = min(pixels[0], math.ceil((x+w) * sx)), min(pixels[1], math.ceil((y+h) * sy))
    return [left, top, right, bottom] if left < right and top < bottom else None


def _read_node(element, logical, pixels, in_web=False):
    if not isinstance(element.tag, str):
        return []
    kind = _type(element)
    role = _ROLES.get(kind, "group")
    traits = set(re.findall(r"[A-Za-z]+", element.get("traits") or ""))
    if role in ("group", "image", "text"):
        if "Button" in traits:
            role = "button"
        elif "Link" in traits:
            role = "link"
        elif "Header" in traits:
            role = "heading"
    in_web = in_web or role == "webview"
    children = [node for child in element for node in _read_node(child, logical, pixels, in_web)]
    visible = _flag(element.get("visible"))
    bounds = _box(element, logical, pixels)
    if visible is False or not bounds:
        # XCTest has geometrically empty wrapper nodes with individually visible children.
        return children
    accessibility_label = (element.get("label") or "").strip()
    name = accessibility_label
    identifier = (element.get("name") or "").strip()
    value = (element.get("value") or "").strip()
    placeholder = (element.get("placeholderValue") or "").strip() if role == "input" else ""
    if name and name == identifier and re.fullmatch(r"(?:[\w-]+\.){2,}[\w.-]+", name, re.ASCII):
        name = ""
    secure = kind == "SecureTextField"
    if secure:
        # Never turn a masked value or password-repeating accessibility label into public text.
        if name == value or (name and set(name) <= set("•●*")):
            name = ""
        value = ""
        children = []
    level = None
    if in_web and role == "heading" and name and name != value and value in {"1", "2", "3", "4", "5", "6"}:
        # WebKit exposes the HTML heading level as the parent's accessibility value.
        level, value = int(value), ""
    if role in ("text", "heading"):
        name = name or value
        if name and all(unicodedata.category(c) == "Co" for c in name):
            # Icon-font codepoints are not readable text or evidence of an action.
            name, value, role = "", "", "image"
    visible_placeholder = bool(placeholder and value == placeholder)
    if placeholder:
        name = name or placeholder
        if visible_placeholder:
            # WDA falls back to placeholderValue when the native input has no contents.
            value = ""
    if in_web and role == "heading" and name:
        # WebKit can emit overlapping header and static-text nodes for the same DOM heading.
        # Keep independently actionable descendants and other headings elsewhere on the screen.
        tolerances = [math.ceil(pixels[i % 2] / logical[i % 2]) for i in range(4)]
        content = []
        for child in children:
            if (child["role"] in ("heading", "text") and child.get("name") == name and not child["actions"]
                    and all(abs(a - b) <= delta for a, b, delta in zip(bounds, child["bounds"], tolerances))):
                content.extend(child["children"])
            else:
                content.append(child)
        children = content
    state = {k: flag for k in ("enabled", "selected", "focused")
             if (flag := _flag(element.get(k))) is not None}
    if "NotEnabled" in traits:
        state["enabled"] = False
    if "Selected" in traits:
        state["selected"] = True
    if role in ("switch", "checkbox", "radio") and (checked := _flag(value.lower())) is not None:
        state["checked"] = checked
        value = ""
    if secure:
        state["password"] = True
    actions = []
    if state.get("enabled") is not False and _flag(element.get("hittable")) is not False:
        if role in _TAPPABLE:
            actions = ["tap"]
        elif role == "input":
            actions = ["tap", "input"]
        elif role == "cell":
            # A native table cell can be a read-only row.
            actions = ["try_tap"]
        elif role in _SCROLLABLE:
            actions = ["scroll"]
    node = {
        "role": role, "type": f"XCUIElementType{kind}", "bounds": bounds,
        "name": name or None, "actions": actions, "state": state, "children": children,
    }
    if role in {"button", "image"} and name and name == accessibility_label:
        node["system_name_source"] = "accessibility_label"
    if role in {"heading", "text"} and name:
        node["text"] = name
    if visible_placeholder and not secure:
        node["text"] = placeholder
    if value and value != name and role in {"text", "heading", "input", "picker"}:
        node["value"] = value
    if level:
        node["level"] = level
    if identifier and identifier != name and not secure:
        node["resource_id"] = identifier
    if secure:
        node["protected"] = True
        if name:
            node["safe_label"] = name
    if role == "dialog":
        node["modal"] = True
    return [node]


def _with_ancestors(nodes, ancestry=()):
    for node in nodes:
        yield node, ancestry
        yield from _with_ancestors(node["children"], (*ancestry, node))


def _mark_input_overlay(nodes, viewport):
    paths = list(_with_ancestors(nodes))
    assistants = [path for node, path in paths if node.get("resource_id") == "SystemInputAssistantView"
                  or node.get("name") == "SystemInputAssistantView"]
    for keyboard, path in paths:
        if keyboard["role"] != "keyboard":
            continue
        # Use the smallest non-full-screen ancestor shared by the actual keyboard
        # and UIKit's input-assistant surface. A full host window also contains the app.
        group = next((n for n in reversed(path) if n["role"] in {"group", "window"}
                      and n["bounds"] != viewport and any(
                          any(ancestor is n for ancestor in chain) for chain in assistants)), None)
        if group is None:
            continue
        box = group["bounds"]
        group["input_overlay"] = True
        keyboard["occlusion_bounds"] = box
        # UIKit can expose the accessory toolbar in a sibling overlay with the
        # exact same bounds. Its own controls remain in front of the occlusion.
        for toolbar, lineage in paths:
            if toolbar["role"] != "toolbar" or any(n["role"] == "webview" for n in lineage):
                continue
            peer = next((n for n in reversed(lineage) if n["bounds"] == box), None)
            if peer is not None and not any(n["role"] == "webview" for n, _ in _with_ancestors([peer])):
                peer["input_overlay"] = True


def from_xctest(xml: str, logical: tuple[float, float], pixels: tuple[int, int]):
    root = ET.fromstring(xml)
    application = next((e for e in root.iter() if _type(e) == "Application"), None)
    if application is None:
        raise ValueError("XCTest source has no application")
    package = application.get("bundleId")
    nodes = _read_node(application, logical, pixels)
    viewport = [0, 0, *pixels]
    for node in nodes:
        # XCTest sometimes keeps its non-actionable Application wrapper in portrait.
        # A full-screen child Window supplies direct evidence of its actual envelope.
        if node["role"] == "application" and node["bounds"] != viewport and any(
                child["role"] == "window" and child["bounds"] == viewport for child in node["children"]):
            node["bounds"] = viewport
    _mark_input_overlay(nodes, viewport)
    return nodes, package


def native_keyboard(nodes):
    """Only report a keyboard's geometry when it is present in the current XCTest snapshot."""
    for node in nodes:
        if node["role"] == "keyboard":
            info = {"visible": True, "bounds": node["bounds"], "source": "ios"}
            if node.get("occlusion_bounds"):
                info["occlusion_bounds"] = node["occlusion_bounds"]
            return info
        found = native_keyboard(node["children"])
        if found:
            return found
    return None
