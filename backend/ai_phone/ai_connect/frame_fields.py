"""Whitelist public frame evidence. Raw accessibility sources and device bindings stay on the Agent wire."""

from __future__ import annotations

from ai_phone.shared.mobile_frame import structure_from_observation


TARGET_FIELDS = ("id", "source", "name", "name_source", "role", "text", "value", "bounds", "name_bounds",
                 "actions", "point", "state", "state_evidence", "tree_path", "class", "resource_id",
                 "system_name_source", "system_hint", "system_hint_kind")
TEXT_FIELDS = ("text", "bounds", "target", "source")
STRUCTURE_FIELDS = ("role", "name", "name_source", "source", "text", "value", "level", "bounds",
                    "target", "belongs_to", "actions", "state", "system_name_source", "system_hint", "system_hint_kind")
_STATE = {"selected", "focused", "checked", "enabled", "expanded", "editable", "checkable", "pressed", "visible",
          "password", "secure", "read_only", "scrollable", "clickable", "focusable"}


def public_record(record: dict, keys: tuple[str, ...]) -> dict:
    if not isinstance(record, dict):
        raise ValueError("invalid frame record")
    out = {}
    for key in keys:
        if key not in record:
            continue
        value = record[key]
        if key == "level":
            if record.get("role") != "heading" or type(value) is not int or not 1 <= value <= 6:
                continue
        elif key == "system_hint_kind":
            if value not in ("image_text", "resource_id", "image_name") or not record.get("system_hint"):
                continue
        elif key in ("state", "state_evidence"):
            if not isinstance(value, dict):
                raise ValueError("invalid frame state")
            value = {k: v for k, v in value.items() if k in _STATE and type(v) is (bool if key == "state" else str)}
        elif key in ("bounds", "name_bounds", "point", "actions"):
            if value is not None and (not isinstance(value, (list, tuple))
                    or not all(type(v) is (str if key == "actions" else int) for v in value)):
                raise ValueError("invalid frame record property")
            if value is not None and key in ("bounds", "name_bounds") and (
                    len(value) != 4 or not (0 <= value[0] < value[2] and 0 <= value[1] < value[3])):
                raise ValueError("invalid frame rectangle")
            if value is not None and key == "point" and len(value) != 2:
                raise ValueError("invalid frame point")
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError("invalid frame record property")
        out[key] = value
    state = out.get("state", {})
    if record.get("protected") is True or state.get("password") or state.get("secure"):
        hidden = {v for key in ("text", "value") if isinstance((v := record.get(key)), str) and v}
        out.pop("text", None)
        out.pop("value", None)
        out.pop("system_hint", None)
        out.pop("system_hint_kind", None)
        if out.get("name") in hidden:
            out["name"], out["name_source"] = None, None
    return out


def public_keyboard(record: dict, width: int, height: int) -> dict:
    keyboard = public_record(record, ("visible", "bounds", "source"))
    body = keyboard.get("bounds")
    area = record.get("occlusion_bounds")
    if (keyboard.get("visible") is not True or not body or not isinstance(area, (tuple, list))
            or len(area) != 4 or not all(type(n) is int for n in area)):
        return keyboard
    left, top, right, bottom = area
    if (0 <= left <= body[0] < body[2] <= right <= width
            and 0 <= top <= body[1] < body[3] <= bottom <= height):
        keyboard["occlusion_bounds"] = list(area)
    return keyboard


def public_structure(result: dict, fields: dict, ids: set[str]) -> dict:
    root = result.get("structure")
    if root is None:
        return structure_from_observation(fields)
    if not isinstance(root, dict) or root.get("role") != "page":
        raise ValueError("invalid observation structure")

    def visit(node: dict) -> dict:
        clean = public_record(node, STRUCTURE_FIELDS)
        for link in ("target", "belongs_to"):
            if clean.get(link) is not None and clean[link] not in ids:
                raise ValueError("structure refers to a target outside this frame")
        sensitive = node.get("protected") is True or any(clean.get("state", {}).get(k) for k in ("password", "secure"))
        if not sensitive and "children" in node:
            children = node["children"]
            if not isinstance(children, list):
                raise ValueError("invalid structure children")
            clean["children"] = [visit(child) for child in children]
        return clean

    return visit(root)
