"""Build one mobile observation from platform-reported nodes and optional, same-frame pixels.

The platform adapter owns device interaction and coordinate conversion. This module never
contacts a device. All bounds are half-open original-image pixels in the same coordinate space
as a tap. A normalized node may have role, type, name, visible text/value, bounds, explicit
actions, boolean state, children, a safe input label, protected, visible and modal flags.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field

from PIL import Image

from .mobile_card_labels import add_printed_card_titles, drop_fully_owned_wrappers
from .mobile_drawn_rows import add_separated_rows
from .mobile_located_text import remove_duplicate_screen_lines
from .mobile_panel_peers import recover_labelled_peers
from .mobile_structure import structure_from_observation
from .mobile_visual_labels import hide_unpainted_static_text, prefer_label_in_existing_target


__all__ = ["build_frame", "structure_from_observation"]

_PREFIX = {"ios": "I", "harmony": "H", "android": "A"}
_ACTIONS = {"tap", "try_tap", "input", "scroll"}
_STATES = {"enabled", "focused", "selected", "checked", "expanded", "password", "read_only"}
_WRAPPERS = {"group", "page", "application", "window", "container", "view", "layout", "other"}
_PAGE_AREAS = {"page", "application", "window", "document", "web", "webview", "main", "form",
               "dialog", "sheet", "scroll_area", "list", "grid", "table", "navigation", "keyboard"}
_MAX_TARGETS = 512
_IMAGE_NAME_FIELDS = {"accessibility_text", "accessibility_label", "description", "image_text", "resource_id"}


def _string(value):
    return value.strip() if isinstance(value, str) else ""


def _image_hint(value):
    # An accessible image description or a local icon identifier may be public. A source URL is not one.
    return value if (value and len(value) <= 256 and not value.lower().startswith("data:")
                     and not any(c in value for c in ("://", "?", "#", "\n", "\r"))) else None


def _intersection(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    return (left, top, right, bottom) if left < right and top < bottom else None


def _box(value, viewport):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(n) is not int for n in value):
        return None
    return _intersection(value, viewport)


def _parts_outside(box, obstacle):
    cut = _intersection(box, obstacle)
    if not cut:
        return [box]
    x, y, r, b = box
    left, top, right, bottom = cut
    return [part for part in ((x, y, r, top), (x, bottom, r, b), (x, top, left, bottom),
                              (right, top, r, bottom)) if part[0] < part[2] and part[1] < part[3]]


def _center(box):
    return [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]


def _inside_point(box, point):
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def _hosts_independent_content(role, box, screen):
    if role in _PAGE_AREAS:
        return True
    # A screen-sized layout can handle taps and contain multiple unrelated pieces of page content.
    if role not in _WRAPPERS or not box:
        return False
    return (box[2] - box[0]) * (box[3] - box[1]) >= .65 * screen[2] * screen[3]


def _sole_visual_anchor(children):
    """A broad tappable wrapper may wrap one small icon. Its geometry can still guide a tap."""
    while len(children) == 1:
        leaf = children[0]
        if leaf.get("target"):
            return None
        children = leaf.get("children", ())
        if not children and (leaf.get("name") or leaf.get("text") or leaf["role"] in {"icon", "image"}):
            return leaf.get("bounds")
    return None


def _tap_point(box, hint, name_box, obstacles):
    preferred = []
    if (isinstance(hint, (list, tuple)) and len(hint) == 2
            and all(type(n) is int for n in hint)):
        preferred.append(list(hint))
    if name_box:
        preferred.append(_center(name_box))
    preferred.append(_center(box))
    for point in preferred:
        if _inside_point(box, point) and not any(_inside_point(other, point) for other in obstacles):
            return point
    free = [box]
    for other in obstacles:
        free = [part for remaining in free for part in _parts_outside(remaining, other)]
    if not free:
        return None
    return _center(max(free, key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1])))


def _avoid_independent_tap_points(targets, hit_constraints):
    """Keep a native target's recommendation clear of separately actionable, localized hit areas."""
    native = [(t, t["tree_path"] + "/", tuple(t["bounds"])) for t in targets
              if t.get("source") in _PREFIX and t.get("tree_path") and t.get("point")
              and "tap" in t.get("actions", ())]
    for target, path, box in native:
        point = target["point"]
        # A broad accessibility wrapper can name a small icon. Its own label at this point
        # is stronger evidence than the empty space elsewhere in the wrapper.
        anchor, children = hit_constraints.get(target["id"], (None, ()))
        footprint = box
        for witness in (anchor, target.get("name_bounds")):
            local = _box(witness, box)
            if local and _inside_point(local, point):
                footprint = local
                break
        peers = []
        width, height = footprint[2] - footprint[0], footprint[3] - footprint[1]
        for _, peer_path, peer_box in native:
            if path.startswith(peer_path) or peer_path.startswith(path):
                continue
            peer_width, peer_height = peer_box[2] - peer_box[0], peer_box[3] - peer_box[1]
            if peer_width <= width and peer_height <= height and (peer_width, peer_height) != (width, height):
                overlap = _intersection(footprint, peer_box)
                if overlap:
                    peers.append(overlap)
        if not any(_inside_point(peer, point) for peer in peers):
            continue
        # Hierarchy order alone does not establish which sibling receives the tap.
        # Subtract both alternatives and the original descendant/disabled reservations.
        obstacles = [*children, *peers]
        target["point"] = _tap_point(footprint, point, None, obstacles)
        if target["point"] is None:
            target["actions"] = [action for action in target["actions"] if action == "scroll"]


def _visible_nodes(nodes):
    for node in nodes:
        if isinstance(node, dict) and node.get("visible") is not False:
            yield node
            yield from _visible_nodes(node.get("children") or ())


def _bind_system_headings(views, targets, rows):
    """Two independent observations must agree: a measured panel owns a positioned, native heading."""
    panels = {t["id"]: t for t in targets if t.get("source") == "pixels"
              and t.get("role") in {"enclosed_surface", "repeated_surface"} and "try_tap" in t.get("actions", ())}
    if not panels:
        return
    owners = {(r["text"], tuple(r["bounds"])): r["target"] for r in rows if r.get("target") in panels}
    headings = {}
    for node in _visible_nodes(views):
        box = node.get("bounds")
        title = node.get("text")
        if node.get("role") not in {"heading", "title"} or node.get("target") or not title or not box:
            continue
        ident = owners.get((title, tuple(box)))
        if not ident or list(_intersection(panels[ident]["bounds"], box) or ()) != list(box):
            continue
        matches = headings.setdefault(ident, [])
        # A web heading and its static-text child can report the same glyphs with 1–2 px rounding.
        if not any(word == title and max(abs(a - b) for a, b in zip(box, other)) <= 2 for word, other in matches):
            matches.append((title, box))
    for ident, matches in headings.items():
        if len(matches) == 1:
            title, box = matches[0]
            panels[ident].update(name=title, name_source="system", name_bounds=list(box),
                                 system_name_source="contained_heading")


def _foreground(nodes, screen):
    visible = list(_visible_nodes(nodes))
    modals = [n for n in visible if n.get("modal") is True and _box(n.get("bounds"), screen)]
    if not modals:
        return nodes, None
    modal = modals[-1]
    descendants = {id(n) for n in _visible_nodes([modal])}
    # The keyboard can be an independently reported window above the modal.
    keyboard = [n for n in visible if id(n) not in descendants and n.get("input_overlay") is True]
    covered = {id(node) for root in keyboard for node in _visible_nodes([root])}
    keyboard += [n for n in visible if id(n) not in descendants | covered and n.get("role") == "keyboard"]
    return [modal, *keyboard], _box(modal["bounds"], screen)


@dataclass
class _Control:
    target: dict
    # Title/leaf candidates belonging to this control, never to an actionable descendant.
    labels: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    image_hints: list = field(default_factory=list)


class _NativeTree:
    def __init__(self, platform, size, keyboard):
        self.tap_captures = []
        self.platform = platform
        self.screen = (0, 0, *size)
        self.keyboard_box = _box(keyboard.get("bounds"), self.screen) if keyboard.get("visible") else None
        self.occlusion_box = (_box(keyboard.get("occlusion_bounds"), self.screen) or self.keyboard_box
                              if self.keyboard_box else None)
        self.targets = []
        self.hit_constraints = {}
        self.rows = {}
        self.excluded = []
        self.truncated = False

    def visit(self, node, path, owner=None, boundary=None, enabled=True, viewport=None, in_keyboard=False):
        if not isinstance(node, dict) or node.get("visible") is False:
            return []
        viewport = viewport or self.screen
        raw_bounds = node.get("bounds")
        box = _box(raw_bounds, viewport)
        role = _string(node.get("role")) or "group"
        in_keyboard = in_keyboard or role == "keyboard" or node.get("input_overlay") is True
        children = node.get("children") or ()
        if box and self.occlusion_box and not in_keyboard:
            free = _parts_outside(box, self.occlusion_box)
            box = max(free, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), default=None)
        if raw_bounds is not None and not box and not children:
            return []
        state = {k: v for k, v in (node.get("state") or {}).items() if k in _STATES and type(v) is bool}
        enabled = enabled and state.get("enabled") is not False
        sensitive = node.get("protected") is True or state.get("password") is True
        name = _string(node.get("name")) if not sensitive else _string(node.get("safe_label"))
        text = "" if sensitive else _string(node.get("text"))
        value = "" if sensitive else _string(node.get("value"))
        actions = list(dict.fromkeys(a for a in node.get("actions") or () if a in _ACTIONS))
        if box and (sensitive or not enabled or role in {"input", "progress", "progressbar", "loading"}):
            self.excluded.append(box)
        view = {"role": role}
        if role == "heading" and type(node.get("level")) is int and 1 <= node["level"] <= 6:
            view["level"] = node["level"]
        if box:
            view["bounds"] = list(box)
        field = node.get("system_name_source") if not sensitive else None
        origin = field if field in _IMAGE_NAME_FIELDS else None
        image_hint = None
        if role == "image" and not sensitive:
            # A native ID by itself supplies a debugging hint. Do not publish addresses or query strings.
            resource = _image_hint(_string(node.get("resource_id")))
            if not name and resource:
                origin, image_hint = "resource_id", resource
            elif name and origin in {"resource_id", "image_text"}:
                image_hint = _image_hint(name)
            if image_hint:
                view.update(system_hint=image_hint, system_hint_kind=origin)
        hint_only = role == "image" and origin in {"resource_id", "image_text"}
        if origin:
            view["system_name_source"] = origin
        # A container's accessible title can help identify a region even when it is not printed text.
        if name and name != text and (not hint_only or (origin == "image_text" and image_hint)):
            view.update(name=name, name_source="system")
        if text:
            view.update(text=text, source="system")
        if value and value != text:
            view.update(value=value, source="system")
        if sensitive:
            state["password"] = True
            children = ()
        if not enabled:
            state["enabled"] = False
        # XCTest and UiTest repeat enabled=true and focused=false on most pieces of decoration.
        # The action table retains every explicit bit. Keep static entries focused on meaningful state.
        static_state = {k: v for k, v in state.items() if v or k in {"enabled", "checked", "expanded"}}
        if static_state.get("enabled") is True:
            static_state.pop("enabled")
        if state and actions:
            view["state"] = state
        elif static_state and (role not in _WRAPPERS or static_state.get("enabled") is False):
            view["state"] = static_state

        control = None
        if actions or (not enabled and role not in _WRAPPERS):
            # A disabled/geometry-less descendant still forms its own semantic boundary.
            if boundary and box:
                boundary.blocked.append(box)
            owner = None
            boundary = None
            if box and actions and enabled and role not in {"progress", "progressbar", "loading"}:
                if len(self.targets) < _MAX_TARGETS:
                    ident = f"{_PREFIX[self.platform]}{len(self.targets) + 1}"
                    target = {"id": ident, "role": role, "source": self.platform,
                              "name": None, "name_source": None, "actions": actions,
                              "bounds": list(box), "state": state, "tree_path": path,
                              "class": _string(node.get("type"))}
                    if state:
                        target["state_evidence"] = {k: "node" for k in state}
                    if resource_id := _string(node.get("resource_id")):
                        target["resource_id"] = resource_id
                    self.targets.append(target)
                    view["target"] = ident
                    control = boundary = _Control(target)
                    if actions != ["scroll"]:
                        owner = control
                else:
                    self.truncated = True
        if box:
            belongs = owner.target["id"] if owner else None
            for word in dict.fromkeys(s for s in (text, value) if s):
                self.rows.setdefault((word, box, belongs),
                                     {"text": word, "source": "system", "bounds": list(box), "target": belongs})
            # A text field's current contents, or a switch's current value, do not name the action.
            label = name or (text if role not in {"input", "password", "toggle", "switch"} else "")
            if owner and hint_only:
                if image_hint:
                    owner.image_hints.append((image_hint, origin))
            elif owner and label:
                owner.labels.append((label, box, origin))
        nested = []
        within = box if box and (node.get("clips_children") is True or "scroll" in actions) else viewport
        content_area = _hosts_independent_content(role, box, self.screen)
        descendant_owner = None if content_area else owner
        for index, child in enumerate(children):
            nested.extend(self.visit(child, f"{path}/{index}", descendant_owner, boundary, enabled, within, in_keyboard))
        if control:
            chosen, name_box, chosen_from = ((name, box, origin) if name and not hint_only
                                               else next(iter(control.labels), (None, None, None)))
            if chosen:
                control.target.update(name=chosen, name_source="system", name_bounds=list(name_box))
                if chosen_from:
                    control.target["system_name_source"] = chosen_from
            elif len(set(control.image_hints)) == 1:
                hint, hint_from = control.image_hints[0]
                control.target.update(system_hint=hint, system_hint_kind=hint_from)
            obstacles = list(control.blocked) if actions != ["scroll"] else []
            anchor = name_box
            if not anchor and content_area and role in _WRAPPERS and role not in _PAGE_AREAS:
                anchor = _sole_visual_anchor(nested)
            if anchor or obstacles:
                self.hit_constraints[control.target["id"]] = (anchor, obstacles)
            point = _tap_point(box, node.get("point"), anchor, obstacles)
            control.target["point"] = point
            if not point:
                control.target["actions"] = [a for a in actions if a == "scroll"]
            elif "tap" in actions:
                # Supplied only for a native foreground handler whose coverage the adapter has measured.
                for captured in node.get("_captures_taps_in") or ():
                    area = _box(captured, box)
                    if area:
                        self.tap_captures.append(area)
        if raw_bounds is not None and not box:
            return nested
        if nested:
            view["children"] = nested
        return [view]


def _remove_captured_pixel_actions(frame, areas):
    if not areas:
        return
    removed = {t["id"] for t in frame["targets"]
               if t.get("source") == "pixels" and t.get("actions") == ["try_tap"] and t.get("point")
               and any(_inside_point(box, t["point"]) for box in areas)}
    if not removed:
        return
    frame["targets"][:] = [t for t in frame["targets"] if t["id"] not in removed]
    for row in frame["screen_texts"] + frame["system_texts"]:
        if row.get("target") in removed:
            row["target"] = None


def _screen_evidence(png, image, observation, excluded):
    # These Android modules are pure image functions. They import no device tool, model or Feature driver.
    targets = observation["targets"]
    # The legacy skeleton detector uses 'android' to distinguish native targets from pixel suggestions.
    # Its geometric test also applies to anonymous native controls on the other platforms.
    try:
        from ai_phone.agent.android_visual_regions import system_placeholders, visual_targets
        hidden = system_placeholders(png, [{**t, "source": "android"} for t in targets])
        regions = visual_targets(png, *image.size, [t for t in targets if t["id"] not in hidden])
    except (OSError, ImportError, ValueError, RuntimeError):
        observation["warnings"].append("visual_targets_unavailable")
        hidden, regions = set(), []
    excluded.extend(tuple(t["bounds"]) for t in targets if t["id"] in hidden)
    if hidden:
        targets[:] = [t for t in targets if t["id"] not in hidden]
        for row in observation["system_texts"]:
            if row["target"] in hidden:
                row["target"] = None
    regions = [r for r in regions if not any(_intersection(r["bounds"], area) for area in excluded)]
    room = _MAX_TARGETS - len(targets)
    observation["targets_truncated"] |= len(regions) > room
    targets.extend({**r, "name_source": None} for r in regions[:room])
    rows = observation["system_texts"]
    try:
        from ai_phone.agent.android_visible_text import enrich_from_pixels
        words = enrich_from_pixels(image, targets, excluded, [(r["text"], tuple(r["bounds"])) for r in rows], rows)
        observation["screen_texts"] = [{**row, "source": "screen"} for row in words]
        observation["screen_texts_available"] = True
    except (OSError, ImportError, ValueError, RuntimeError):
        observation["warnings"].append("screen_texts_unavailable")


def build_frame(nodes, *, platform: str, width: int, height: int, png: bytes | None = None,
                include_image: bool = True, package=None, activity=None, keyboard=None, dialogs=None,
                warnings=(), tree_available=True, display=None) -> dict:
    """Return Agent-side observation fields. Omitting image output preserves the same structure and IDs."""
    platform = platform.lower()
    if platform not in _PREFIX or any(type(n) is not int or n <= 0 for n in (width, height)):
        raise ValueError("A known platform and positive pixel dimensions are required")
    tree_available = bool(tree_available and nodes is not None)
    frame_keyboard = keyboard if isinstance(keyboard, dict) else {"visible": None}
    native = _NativeTree(platform, (width, height), frame_keyboard)
    chosen, modal_box = _foreground(nodes or [], native.screen) if tree_available else ([], None)
    if modal_box:
        native.excluded.extend(_parts_outside(native.screen, modal_box))
    views = []
    for index, node in enumerate(chosen):
        views.extend(native.visit(node, f"/{index}"))
    if dialogs is None and tree_available:
        dialogs = ([{"kind": chosen[0].get("role", "dialog"), "bounds": list(modal_box), "source": platform}]
                   if modal_box else [])
    result = {"platform": platform, "width": width, "height": height, "tree_available": tree_available,
              "targets": native.targets, "targets_truncated": native.truncated,
              "system_texts": list(native.rows.values()), "screen_texts": [], "texts_truncated": False,
              "screen_texts_available": False, "screen_texts_truncated": False,
              "keyboard": frame_keyboard, "dialogs": dialogs, "warnings": list(warnings or ()),
              "marked_targets": False}
    drop_fully_owned_wrappers(views, result)
    for key, value in (("package", package), ("activity", activity), ("display", display)):
        if value is not None:
            result[key] = value
    if png is not None:
        from .android_frame_regions import sample_original

        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG" or image.size != (width, height):
                raise ValueError("PNG and frame coordinate size must agree")
            raw = image.convert("RGB")
        result["_raw_grid_v1"] = sample_original(raw)
        hide_unpainted_static_text(raw, views, result)
        if native.occlusion_box:
            native.excluded.append(native.occlusion_box)
        _screen_evidence(png, raw, result, native.excluded)
        prefer_label_in_existing_target(views, result)
        remove_duplicate_screen_lines(result, views)
        add_printed_card_titles(raw, views, result, native.excluded)
        _bind_system_headings(views, result["targets"], result["system_texts"])
        result["targets_truncated"] |= recover_labelled_peers(raw, views, result["targets"], result["system_texts"],
                                                             native.excluded, _MAX_TARGETS)
        result["targets_truncated"] |= add_separated_rows(raw, views, result, native.excluded, _MAX_TARGETS)
    _avoid_independent_tap_points(result["targets"], native.hit_constraints)
    _remove_captured_pixel_actions(result, native.tap_captures)
    if png is not None and include_image:
        from ai_phone.agent.android_frame_marks import annotate_frame

        result["png"] = base64.b64encode(annotate_frame(png, result["targets"])).decode("ascii")
        result["marked_targets"] = bool(result["targets"])
    result["texts"] = list(dict.fromkeys(row["text"] for row in result["system_texts"] + result["screen_texts"]))
    result["structure"] = structure_from_observation(result, native_nodes=views)
    return result
