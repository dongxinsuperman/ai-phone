"""Describe a captured Android frame. All rectangles use original PNG pixels."""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops

from ai_phone.shared.mobile_visual_labels import reconcile_static_paragraphs

from .android_page_structure import page_structure
from .android_visual_regions import painted_foreground


Rect = tuple[int, int, int, int]
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_COMPONENT = re.compile(r"(?<![\w.])([A-Za-z][\w]*(?:\.[A-Za-z][\w]*)*)/(\.?[A-Za-z_$][\w.$]*)")
_MAX_TEXTS = 512
_MAX_TEXT_LENGTH = 512
_MAX_TARGETS = 512
_ASSET_KEY = re.compile(r"[a-z][a-z0-9]*|[A-Za-z0-9_./-]*[./_-][A-Za-z0-9_./-]*")


def _intersection(a: Rect, b: Rect) -> Rect | None:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    return (left, top, right, bottom) if left < right and top < bottom else None


def _bounds(node, viewport, to_png):
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    return _intersection(to_png(tuple(map(int, match.groups()))), viewport) if match else None


def _center(box):
    return [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]


def _contains(box, point):
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def _subtract(box, obstacle):
    overlap = _intersection(box, obstacle)
    if overlap is None:
        return [box]
    left, top, right, bottom = box
    cut_left, cut_top, cut_right, cut_bottom = overlap
    pieces = (
        (left, top, right, cut_top),
        (left, cut_bottom, right, bottom),
        (left, cut_top, cut_left, cut_bottom),
        (cut_right, cut_top, right, cut_bottom),
    )
    return [piece for piece in pieces if piece[0] < piece[2] and piece[1] < piece[3]]


def _point(box, label_box, obstacles):
    for anchor in (label_box, box):
        if anchor:
            point = _center(anchor)
            if not any(_contains(other, point) for other in obstacles):
                return point
    available = [box]
    for other in obstacles:
        available = [part for remaining in available for part in _subtract(remaining, other)]
    if available:
        largest = max(available, key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1]))
        return _center(largest)
    return None


def _visible(node):
    return all(node.get(key) != "false" for key in ("visible-to-user", "visible", "displayed"))


def _widget(node):
    return node.get("class", "").rsplit(".", 1)[-1]


def _capabilities(node):
    widget = _widget(node)
    editable = node.get("editable") == "true" or widget.endswith("EditText")
    scrollable = node.get("scrollable") == "true"
    checkable = node.get("checkable") == "true" or widget in {
        "Switch",
        "SwitchCompat",
        "CheckBox",
        "RadioButton",
        "ToggleButton",
    }
    role = "input" if editable else "toggle" if checkable else "scroll_area" if scrollable else "control"
    actions = [
        name
        for name, available in (
            ("tap", node.get("clickable") == "true"),
            ("input", editable),
            ("scroll", scrollable),
        )
        if available
    ]
    return role, actions


def _keyboard(dump, screen, to_png):
    sources = []
    for line in (dump or "").splitlines():
        if not re.search(r"\b(?:mType|type)=(?:ime|ITYPE_IME)\b", line, re.I):
            continue
        visible = re.search(r"\b(?:mVisible|visible)=(true|false)\b", line, re.I)
        if visible:
            frame = re.search(
                r"\b(?:mFrame|frame)\s*=\s*(?:Rect\()?\[?(-?\d+)[, ]+(-?\d+)\]?\s*-?\s*\[?(-?\d+)[, ]+(-?\d+)",
                line,
                re.I,
            )
            rect = _intersection(to_png(tuple(map(int, frame.groups()))), screen) if frame else None
            sources.append((visible.group(1).lower() == "true", rect))
    if sources and len({state for state, _ in sources}) == 1:
        visible = sources[0][0]
        result = {"visible": visible, "source": "window.insets.ime"}
        bounds = {rect for _, rect in sources if rect}
        if visible and len(bounds) == 1:
            result["bounds"] = list(bounds.pop())
        return result
    shown = re.search(r"\bmInputShown=(true|false)\b", dump or "")
    if shown:
        return {"visible": shown.group(1) == "true", "source": "input_method.mInputShown"}
    return {"visible": None}


def _window_scope(dump, packages):
    """Use the focused component. A focused-app fallback must agree with the captured tree."""
    for window_field in ("mCurrentFocus", "mFocusedApp"):
        lines = re.findall(rf"^\s*{window_field}\s*=\s*([^\n]+)", dump or "", re.M)
        components = {match.groups() for line in lines if (match := _COMPONENT.search(line))}
        if len(components) == 1:
            package, activity = components.pop()
            if window_field == "mCurrentFocus" or packages == {package}:
                return {
                    "package": package,
                    "activity": package + activity if activity.startswith(".") else activity,
                }
    return {"package": next(iter(packages))} if len(packages) == 1 else {}


def _foreground(root, screen, to_png):
    """An explicit dialog or open Android drawer owns the foreground interaction."""
    dialogs, drawers = [], []

    def visit(node, path):
        if not _visible(node):
            return
        box = _bounds(node, screen, to_png)
        widget = _widget(node)
        if box and (widget.endswith("Dialog") or node.get("role") in ("dialog", "alertdialog")):
            dialogs.append((node, path, "dialog", box))
        children = list(enumerate(node))
        # Android DrawerLayout's first child is the content; visible drawer children sit over it.
        if widget == "DrawerLayout":
            for index, child in children[1:]:
                rect = _bounds(child, screen, to_png)
                if not _visible(child) or not rect:
                    continue
                # A drawer may wrap both its scrim and its much narrower panel.
                panes = [
                    b
                    for pane in child
                    if _visible(pane)
                    and pane.get("clickable") != "true"
                    and (b := _bounds(pane, rect, to_png))
                    and len(pane)
                ]
                drawers.append((child, f"{path}/{index}", "drawer", panes[-1] if panes else rect))

        def order(entry):
            raw = entry[1].get("drawing-order", "")
            return int(raw) if raw.isdecimal() else entry[0]

        for index, child in sorted(children, key=order):
            visit(child, f"{path}/{index}")

    visit(root, "")
    return (dialogs or drawers or [(root, "", None, screen)])[-1]


def _painted_owner(root, screen, to_png, panel):
    """A matching system subtree can distinguish controls directly under the panel."""
    tolerance = max(2, round(.05 * min(screen[2:])))

    def owners(node, path, under_scrim=False):
        if not _visible(node) or node.get("enabled") == "false":
            return
        box = _bounds(node, screen, to_png)
        if box and all(abs(a - b) <= tolerance for a, b in zip(box, panel)):
            controls = {
                _bounds(child, box, to_png) for child in node.iter("node") if child is not node
                and _visible(child) and child.get("enabled") != "false" and _capabilities(child)[1]
            }
            if under_scrim or len(controls - {None}) >= 2:
                yield node, path, under_scrim
                return
        scrim = (box == screen and node.get("clickable") == "true"
                 and not (node.get("text") or node.get("content-desc")))
        for index, child in enumerate(node):
            yield from owners(child, f"{path}/{index}", under_scrim or scrim)

    candidates = list(owners(root, ""))
    scrim_panels = [candidate for candidate in candidates if candidate[2]]
    matches = scrim_panels or candidates
    return matches[0][:2] if len(matches) == 1 else (root, "")


def _painted_target(region, panel, illustrations):
    """A painted shell or illustration can provide text without being a tap target."""
    if region.get("role") != "enclosed_surface":
        return True
    left, top, right, bottom = region["bounds"]
    width, height = panel[2] - panel[0], panel[3] - panel[1]
    spans_panel = left - panel[0] <= .04 * width and panel[2] - right <= .04 * width
    if spans_panel and (bottom - top >= .25 * height or top - panel[1] <= .06 * height):
        return False
    if right - left >= .55 * width and bottom - top >= .22 * height:
        return not any(_intersection(region["bounds"], box) == tuple(region["bounds"]) for box in illustrations)
    return True


@dataclass
class _Label:
    text: str
    source: str
    box: Rect
    node: ET.Element


@dataclass
class _Control:
    node: ET.Element
    path: str
    box: Rect
    role: str
    actions: list[str]
    labels: list[_Label] = field(default_factory=list)
    children: list[Rect] = field(default_factory=list)
    checkboxes: list[ET.Element] = field(default_factory=list)
    image_hint: str | None = None


def _image_names_control(text, box, owner):
    """WebView exposes both author-supplied alt text and asset keys as Image.text."""
    if not owner or owner.role == "scroll_area" or not text:
        return False
    if _ASSET_KEY.fullmatch(text):
        return False
    common = _intersection(box, owner.box)
    if not common:
        return False
    shared_area = (common[2] - common[0]) * (common[3] - common[1])
    owner_area = (owner.box[2] - owner.box[0]) * (owner.box[3] - owner.box[1])
    return shared_area >= 0.80 * owner_area


def _ambiguous_image_names(root, png_bytes, screen, to_png):
    # WebView may publish the same alt text for two visibly different image buttons.
    # Repeated ordinary labels and matching copies of the same graphic remain usable.
    boxes_by_name = {}
    for node in root.iter("node"):
        text = node.get("text", "").strip()
        if text and not _ASSET_KEY.fullmatch(text) and _widget(node) in ("Image", "ImageView") and _visible(node):
            box = _bounds(node, screen, to_png)
            if box:
                boxes_by_name.setdefault(text, set()).add(box)
    repeated = {text: rects for text, rects in boxes_by_name.items() if len(rects) > 1}
    if not repeated:
        return set()
    ambiguous = set()
    with Image.open(io.BytesIO(png_bytes)) as png:
        gray = png.convert("L")
        for text, rects in repeated.items():
            samples = (gray.crop(box).resize((48, 16), Image.Resampling.BILINEAR) for box in rects)
            first = next(samples)
            for sample in samples:
                changed = ImageChops.difference(first, sample).histogram()
                if sum(changed[24:]) > 0.08 * 48 * 16:
                    ambiguous.add(text)
                    break
    return ambiguous


def _password_evidence(root):
    """Keep private values out of every label, even when another node repeats them."""
    values, hint_echoes = set(), {}
    for editor in root.iter("node"):
        if editor.get("password") != "true":
            continue
        hint = editor.get("hint")
        showing_value = any(editor.get(k) == "false" for k in ("showing-hint-text", "showingHintText"))
        autofilled = any(editor.get(k) == "true" for k in ("autofilled", "is-autofilled", "isAutofilled"))
        for child in editor.iter():
            values.update(child.get(k) for k in ("value", "autofill-value", "autofillValue") if child.get(k))
            text = child.get("text")
            if not text:
                continue
            if child is editor and text == hint and not showing_value and not autofilled:
                # Some UIAutomator dumps put getHintText() into both attributes of an empty editor.
                hint_echoes[editor] = hint
            else:
                values.add(text)
    # Only the field's own explicit hint can use the exception. A conflicting private value wins.
    safe_hint_fields = {editor for editor, hint in hint_echoes.items() if not any(value in hint for value in values)}
    return values | set(hint_echoes.values()), safe_hint_fields


def _labels(node, box, role, web, protected, secrets, safe_hint_fields, owner, ambiguous_images):
    if protected:
        keys = ("hint",)
    elif role == "input":
        keys = ("content-desc", "hint", "text")
    else:
        keys = ("text", "content-desc", "hint")
    for key in keys:
        raw = node.get(key, "")
        own_hint = key == "hint" and node in safe_hint_fields
        if not raw or (not own_hint and any(value in raw for value in secrets)):
            continue
        text = " ".join("".join(c for c in raw if unicodedata.category(c) not in ("Co", "Cf")).split())
        if not any(c.isalnum() for c in text):
            continue
        widget = _widget(node)
        if web and key == "text" and widget in ("Image", "ImageView"):
            if not _image_names_control(text, box, owner):
                continue
            if text in ambiguous_images:
                owner.image_hint = text
                continue
            key = "image_text"
        if (
            key == "content-desc"
            and widget
            and widget.lower() in text.lower()
            and re.search(r"\b[A-Za-z_]\w*\s*=\s*\d+\b", text)
        ):
            continue
        yield _Label(text, key, box, node)


def _choose_name(control):
    if not control.labels:
        return None, None, None
    label = control.labels[0]
    if label.node is not control.node and not any(c.isalpha() for c in label.text):
        label = next((entry for entry in control.labels if any(c.isalpha() for c in entry.text)), label)
    name = label.text
    bounds = label.box
    if name.endswith((":", "：", "|", "｜")):
        next_label = next((entry for entry in control.labels if entry.text != name), None)
        if next_label:
            name += next_label.text
            other = next_label.box
            bounds = (min(bounds[0], other[0]), min(bounds[1], other[1]),
                      max(bounds[2], other[2]), max(bounds[3], other[3]))
    return name[:_MAX_TEXT_LENGTH], label, bounds


def _target(control, index, keyboard_box):
    node = control.node
    name, label, name_bounds = _choose_name(control)
    state, evidence = {}, {}
    for key in ("selected", "focused", "checked"):
        if key == "checked" and control.role != "toggle":
            continue
        if node.get(key) in ("true", "false"):
            state[key] = node.get(key) == "true"
            evidence[key] = "node"
    if label and "tap" in control.actions and not state.get("selected"):
        child_selection = {
            entry.node.get("selected")
            for entry in control.labels
            if entry.node is not node
            and entry.text == label.text
            and entry.node.get("selected") in ("true", "false")
        }
        if len(child_selection) == 1:
            state["selected"] = child_selection.pop() == "true"
            evidence["selected"] = "owned_name_node"
    if (
        control.role == "control"
        and "tap" in control.actions
        and not control.children
        and len(control.checkboxes) == 1
    ):
        control.role = "toggle"
        child = control.checkboxes[0]
        if child.get("checked") in ("true", "false"):
            state["checked"] = child.get("checked") == "true"
            evidence["checked"] = "owned_checkable_node"
    obstacles = list(control.children) if control.actions != ["scroll"] else []
    if keyboard_box:
        obstacles.append(keyboard_box)
    point = _point(control.box, label.box if label else None, obstacles)
    target = {
        "id": f"A{index}",
        "source": "android",
        "name": name,
        "name_source": "system" if name else None,
        "role": control.role,
        "actions": control.actions,
        "bounds": list(control.box),
        "point": point,
        "state": state,
        "tree_path": control.path,
        "class": node.get("class", ""),
    }
    if node.get("resource-id"):
        target["resource_id"] = node.get("resource-id")
    if label:
        target["system_name_source"] = ("" if label.node is node else "descendant_") + label.source
        target["name_bounds"] = list(name_bounds)
    elif control.image_hint:
        target["system_hint"] = control.image_hint
    if evidence:
        target["state_evidence"] = evidence
    return target


def describe(
    xml: str | bytes | None,
    png_bytes: bytes,
    width: int,
    height: int,
    window_dump: str | None,
    display_size: tuple[int, int] | None = None,
    webview_pages=(),
) -> dict:
    """Read Android's rotated logical display into the PNG coordinate space used by actions."""
    screen = (0, 0, width, height)
    device_width, device_height = display_size or (width, height)
    if min(width, height, device_width, device_height) <= 0:
        raise ValueError("Display and PNG dimensions must be positive")

    def to_png(box: Rect) -> Rect:
        if (width, height) == (device_width, device_height):
            return box
        # Act sends floor(png * display / image). Ceil at both ends projects exactly the
        # image pixels which hit a half-open Android interval, including one-pixel controls.
        left, top, right, bottom = box
        return (
            (left * width + device_width - 1) // device_width,
            (top * height + device_height - 1) // device_height,
            (right * width + device_width - 1) // device_width,
            (bottom * height + device_height - 1) // device_height,
        )

    keyboard = _keyboard(window_dump, screen, to_png)
    warnings, controls, targets = [], [], []
    texts: dict[str, None] = {}
    texts_truncated = False
    system_labels = {}
    visited = {}
    excluded = []
    try:
        root = ET.fromstring(xml) if xml else None
    except (ValueError, ET.ParseError):
        root = None
    tree_available = root is not None and any(node.tag == "node" for node in root.iter())
    tree_partial = tree_available and root.get("partial") == "true"
    dialogs = [] if tree_available else None
    active, path, modal, panel = _foreground(root, screen, to_png) if tree_available else (None, "", None, screen)
    painted = None if modal else painted_foreground(png_bytes)
    modal_box = panel if modal else painted
    painted_subtree = False
    if painted and tree_available:
        active, path = _painted_owner(root, screen, to_png, painted)
        painted_subtree = active is not root
    if painted and painted[3] == height and painted[1] >= .22 * height:
        # A bottom sheet can draw its header and close icon just above the solid surface.
        left, top, right, bottom = painted
        modal_box = (left, max(0, top - round(min(width, height) * .11)), right, bottom)
    if modal_box:
        excluded.extend(_subtract(screen, modal_box))
    modal_path = modal_kind = None
    packages = set()
    if not window_dump or not window_dump.strip():
        warnings.append("android_window_unavailable")
    if tree_partial:
        warnings.append("android_tree_partial")
    if tree_available:
        keyboard_box = keyboard.get("bounds") if keyboard.get("visible") else None
        ambiguous_images = _ambiguous_image_names(active, png_bytes, screen, to_png)
        packages = {node.get("package") for node in active.iter("node") if node.get("package")}
        secrets, safe_hint_fields = _password_evidence(root)

        def visit(node, path, viewport, owner=None, web=False, protected=False, enabled=True):
            nonlocal texts_truncated
            if not _visible(node):
                return
            web = web or _widget(node) == "WebView"
            protected = protected or node.get("password") == "true"
            enabled = enabled and node.get("enabled") != "false"
            box = _bounds(node, viewport, to_png)
            role, actions = _capabilities(node)
            if node.get("bounds") and not box:
                return
            if box and not actions and (_widget(node) == "ProgressBar" or node.get("role") == "progressbar"):
                excluded.append(box)
                return
            visited[path] = (node, box, enabled, protected)
            if box and (node.get("password") == "true" or role == "input"):
                # The visible value of a text field is not its action label.
                excluded.append(box)
            if actions:
                parent = owner
                owner = None  # Independent controls never lend a label or a selection to their parent.
                in_front = not painted or (box and _intersection(box, modal_box) == box)
                panel_wrapper = node is active and (modal or painted_subtree) and len(node)
                if box and enabled and in_front and not panel_wrapper:
                    owner = _Control(node, path, box, role, actions)
                    if parent:
                        parent.children.append(box)
                    controls.append(owner)
            elif box and enabled and owner and role == "toggle":
                owner.checkboxes.append(node)
            if (box and (not keyboard_box or not _intersection(box, keyboard_box))
                    and (not modal_box or _intersection(box, modal_box) == box)):
                # The tree may still publish labels underneath the open IME or a drawer's scrim.
                if owner and web and not protected and _widget(node) in ("Image", "ImageView"):
                    raw = node.get("text", "")
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,63}", raw) and not any(
                        secret in raw for secret in secrets
                    ):
                        owner.image_hint = raw
                for label in _labels(node, box, role, web, protected, secrets, safe_hint_fields, owner, ambiguous_images):
                    if len(label.text) > _MAX_TEXT_LENGTH:
                        texts_truncated = True
                        label.text = label.text[:_MAX_TEXT_LENGTH]
                    # An icon's spoken description can name a target. A filled field hides its placeholder.
                    onscreen = not protected and (label.source in ("text", "image_text")
                                                  or (label.source == "hint" and not node.get("text")))
                    owned = owner and (node is owner.node or owner.role != "scroll_area")
                    if onscreen:
                        key = (label.text, box, owner.path if owned else None)
                        if len(system_labels) < _MAX_TEXTS or key in system_labels:
                            system_labels.setdefault(key, path)
                        else:
                            texts_truncated = True
                    if onscreen and label.text not in texts:
                        if len(texts) < _MAX_TEXTS:
                            texts[label.text] = None
                        else:
                            texts_truncated = True
                    if owned and enabled:
                        if role != "input" or label.source != "text":
                            owner.labels.append(label)
            for index, child in enumerate(node):
                visit(child, f"{path}/{index}", box or viewport, owner, web, protected, enabled)

        visit(active, path, screen)
        targets = [
            _target(control, index, keyboard_box) for index, control in enumerate(controls[:_MAX_TARGETS], 1)
        ]
        if modal:
            modal_path, modal_kind = path, modal
            dialogs.append(
                {"kind": modal, "label": next(iter(texts), None), "bounds": list(panel), "source": "android"}
            )
    else:
        warnings.append("android_tree_unavailable")

    try:
        from .android_visual_regions import system_placeholders, visual_targets

        skeletons = system_placeholders(png_bytes, targets)
        if skeletons:
            excluded.extend(tuple(t["bounds"]) for t in targets if t["id"] in skeletons)
            targets = [t for t in targets if t["id"] not in skeletons]
        regions = visual_targets(png_bytes, width, height, targets)
    except Exception:
        # Pixel proposals are supplemental. A failed decoder must not discard the system tree.
        warnings.append("visual_targets_unavailable")
        regions = []
    if modal_box:
        regions = [
            region
            for region in regions
            if _intersection(region["bounds"], modal_box) == tuple(region["bounds"])
        ]
    if painted:
        illustrations = [
            box for node, box, _, _ in visited.values()
            if box and _widget(node) in ("Image", "ImageView") and not _capabilities(node)[1]
        ]
        regions = [region for region in regions if _painted_target(region, painted, illustrations)]
    if keyboard.get("visible") and keyboard.get("bounds"):
        excluded.append(tuple(keyboard["bounds"]))
    if excluded:
        regions = [
            region for region in regions
            if not any(_contains(box, _center(region["bounds"])) for box in excluded)
        ]
    targets_truncated = len(controls) > _MAX_TARGETS or len(targets) + len(regions) > _MAX_TARGETS
    room = _MAX_TARGETS - len(targets)
    for region in regions[:room]:
        targets.append({**region, "name_source": None, "state": region.get("state", {})})
    system_ids = {t["tree_path"]: t["id"] for t in targets if t["source"] == "android"}
    system_texts = [
        {"text": value, "bounds": list(box), "target": system_ids.get(owner), "source": "system"}
        for value, box, owner in system_labels
    ]
    system_texts_truncated = texts_truncated or tree_partial
    try:
        from .android_visible_text import associate_system_text

        with Image.open(io.BytesIO(png_bytes)) as png:
            associate_system_text(png.convert("RGB"), targets, excluded, system_texts)
    except Exception:
        warnings.append("system_text_association_unavailable")
    if painted:
        # The sheet is visible even when the Android tree has only a page wrapper.
        dialogs = [{
            "kind": "sheet" if painted[3] == height else "dialog",
            "bounds": list(painted),
            "source": "screen",
        }]
    if webview_pages:
        from .android_webview import merge

        with Image.open(io.BytesIO(png_bytes)) as png:
            webview_truncated = merge(webview_pages, png.convert("RGB"), (device_width, device_height),
                                      targets, excluded, modal_box, _MAX_TARGETS)
        targets_truncated = targets_truncated or webview_truncated
    result = {
        **_window_scope(window_dump, packages),
        "texts": list(texts),
        "system_texts": system_texts,
        "system_texts_truncated": system_texts_truncated,
        "screen_texts": [],
        "screen_texts_available": False,
        "screen_texts_truncated": False,
        "targets": targets,
        "keyboard": keyboard,
        "dialogs": dialogs,
        "tree_available": tree_available,
        "texts_truncated": texts_truncated,
        "targets_truncated": targets_truncated or tree_partial,
        "warnings": warnings,
    }
    reconcile_static_paragraphs(result, retain_readings=True)
    result["structure"] = page_structure(result, visited, system_labels.values(), modal_path, modal_kind, modal_box)
    return result
