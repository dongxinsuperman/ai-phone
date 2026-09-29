"""Doubao Seed GUI XML protocol -> ai-phone ParsedAction adapter."""
from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from ai_phone.shared import actions as A


_POINT_SCHEMA = {
    "type": "string",
    "description": "0-1000 normalized coordinate in exact form <point>x y</point>",
    "pattern": r"^<point>(?:0|[1-9][0-9]{0,2}|1000) (?:0|[1-9][0-9]{0,2}|1000)</point>$",
}


# Model-visible XML actions. ``key_event`` deliberately stays internal-only:
# Android/iOS/Harmony use different integer key tables, so exposing one unconstrained
# integer schema would let the model generate actions that are invalid on two of the
# three platforms. Existing internal ParsedAction/keycode consumers remain compatible.
ACTION_SCHEMAS: list[dict[str, Any]] = [
    {"name": "click", "parameters": {"type": "object", "properties": {"point": _POINT_SCHEMA}, "required": ["point"]}},
    {"name": "long_press", "parameters": {"type": "object", "properties": {"point": _POINT_SCHEMA}, "required": ["point"]}},
    {"name": "double_tap", "parameters": {"type": "object", "properties": {"point": _POINT_SCHEMA}, "required": ["point"]}},
    {"name": "left_double", "parameters": {"type": "object", "properties": {"point": _POINT_SCHEMA}, "required": ["point"]}},
    {"name": "type", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "scroll", "parameters": {"type": "object", "properties": {"point": _POINT_SCHEMA, "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "Content browsing direction, not finger movement: down reveals lower content; up reveals upper content or returns to top; right reveals content on the right; left reveals content on the left."}, "amount": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["point", "direction"]}},
    {"name": "drag", "parameters": {"type": "object", "properties": {"start_point": _POINT_SCHEMA, "end_point": _POINT_SCHEMA}, "required": ["start_point", "end_point"]}},
    {"name": "open_app", "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}},
    {"name": "close_app", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "press_home", "parameters": {"type": "object", "properties": {}}},
    {"name": "press_back", "parameters": {"type": "object", "properties": {}}},
    {"name": "wait", "parameters": {"type": "object", "properties": {"seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, "required": ["seconds"]}},
    {"name": "take_screenshot", "parameters": {"type": "object", "properties": {"save_to_album": {"type": "boolean"}}, "required": ["save_to_album"]}},
    {"name": "finished", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "assert_fail", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]


def schemas_prompt_text(action_names: set[str] | None = None) -> str:
    schemas = ACTION_SCHEMAS
    if action_names is not None:
        schemas = [item for item in schemas if item["name"] in action_names]
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in schemas)


def extract_thought(content: str) -> str:
    before = (content or "").split("<seed:tool_call>", 1)[0].strip()
    if before.lower().startswith("thought:"):
        return before[len("thought:"):].strip()
    if before.startswith(("思考：", "思考:")):
        return before[3:].strip()
    return before


def parse_actions(
    content: str,
    *,
    allow_internal_actions: bool = False,
) -> list[A.ParsedAction]:
    try:
        from ui_tars.action_parser import parse_xml_action_65
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Seed GUI XML协议需要依赖 ui-tars==0.5.1") from exc
    try:
        calls = parse_xml_action_65(content or "")
        parsed = [_to_parsed(item) for item in calls]
        public_actions = {
            {"left_double": A.ACTION_DOUBLE_TAP}.get(item["name"], item["name"])
            for item in ACTION_SCHEMAS
        }
        for item in parsed:
            _validate(item)
            if not allow_internal_actions and item.action not in public_actions:
                raise ValueError(
                    f"Seed GUI action is internal-only and not model-visible: {item.action}"
                )
        return parsed
    except Exception as exc:  # Model output is untrusted; retry on the same frame.
        logger.warning("Seed GUI XML解析/校验失败: {}: {}", type(exc).__name__, str(exc)[:240])
        return []


def _point(value: Any) -> list[int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*<point>\s*(\d+)\s+(\d+)\s*</point>\s*", value)
    if not match:
        return None
    point = [int(match.group(1)), int(match.group(2))]
    return point if all(0 <= coordinate <= 1000 for coordinate in point) else None


def _quote(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _to_parsed(call: dict[str, Any]) -> A.ParsedAction:
    raw_name = str(call.get("function") or "")
    params = dict(call.get("parameters") or {})
    name = {"left_double": A.ACTION_DOUBLE_TAP}.get(raw_name, raw_name)
    parsed = A.ParsedAction(action=name, coord_space="normalized")
    parsed.point = _point(params.get("point"))
    parsed.start_point = _point(params.get("start_point"))
    parsed.end_point = _point(params.get("end_point"))
    if "content" in params:
        parsed.content = str(params["content"])
    if "direction" in params:
        parsed.direction = str(params["direction"])
    if name == A.ACTION_OPEN_APP:
        parsed.name = str(params.get("app_name") or "")
    elif name == A.ACTION_CLOSE_APP:
        parsed.name = str(params.get("name") or "")
    if name == A.ACTION_WAIT:
        value = params.get("seconds")
        if value is not None and not isinstance(value, bool):
            parsed.seconds = int(value)
    if name == A.ACTION_SCROLL:
        amount = int(params.get("amount", 1))
        if not 1 <= amount <= 10:
            raise ValueError("scroll amount must be in [1, 10]")
        parsed.scroll_amount = amount
    if name == A.ACTION_KEY_EVENT and params.get("keycode") is not None:
        parsed.keycode = int(params["keycode"])
    if name == A.ACTION_TAKE_SCREENSHOT:
        value = params.get("save_to_album", True)
        if not isinstance(value, bool):
            raise ValueError("save_to_album must be boolean")
        parsed.save_to_album = value
    parsed.raw = _canonical_raw(parsed)
    return parsed


def _validate(p: A.ParsedAction) -> None:
    if not p.is_known:
        raise ValueError(f"unknown Seed GUI action: {p.action}")
    if p.action in {A.ACTION_CLICK, A.ACTION_LONG_PRESS, A.ACTION_DOUBLE_TAP} and p.point is None:
        raise ValueError(f"{p.action} requires point")
    if p.action == A.ACTION_DRAG and (p.start_point is None or p.end_point is None):
        raise ValueError("drag requires start_point/end_point")
    if p.action == A.ACTION_SCROLL and (p.point is None or p.direction not in {"up", "down", "left", "right"}):
        raise ValueError("scroll requires point and valid direction")
    if p.action in {A.ACTION_OPEN_APP, A.ACTION_CLOSE_APP} and not p.name:
        raise ValueError(f"{p.action} requires app name")
    if p.action == A.ACTION_KEY_EVENT and p.keycode is None:
        raise ValueError("key_event requires keycode")
    if p.action == A.ACTION_WAIT and (p.seconds is None or not 1 <= p.seconds <= 60):
        raise ValueError("wait requires seconds in [1, 60]")
    if p.action in {A.ACTION_TYPE, A.ACTION_FINISHED, A.ACTION_ASSERT_FAIL} and not (p.content or ""):
        raise ValueError(f"{p.action} requires non-empty content")


def _canonical_raw(p: A.ParsedAction) -> str:
    def pt(v: list[int] | None) -> str:
        return f"<point>{v[0]} {v[1]}</point>" if v else "<point>0 0</point>"
    if p.action in {A.ACTION_CLICK, A.ACTION_LONG_PRESS, A.ACTION_DOUBLE_TAP}:
        return f"{p.action}(point='{pt(p.point)}')"
    if p.action == A.ACTION_DRAG:
        return f"drag(start_point='{pt(p.start_point)}', end_point='{pt(p.end_point)}')"
    if p.action == A.ACTION_SCROLL:
        amount = f", amount={p.scroll_amount}" if p.scroll_amount > 1 else ""
        return f"scroll(point='{pt(p.point)}', direction='{p.direction}'{amount})"
    if p.action == A.ACTION_TYPE:
        return f"type(content='{_quote(p.content or '')}')"
    if p.action == A.ACTION_OPEN_APP:
        return f"open_app(app_name='{_quote(p.name or '')}')"
    if p.action == A.ACTION_CLOSE_APP:
        return f"close_app(name='{_quote(p.name or '')}')"
    if p.action == A.ACTION_WAIT:
        return f"wait(seconds={p.seconds or 1})"
    if p.action in {A.ACTION_FINISHED, A.ACTION_ASSERT_FAIL}:
        return f"{p.action}(content='{_quote(p.content or '')}')"
    if p.action == A.ACTION_KEY_EVENT:
        return f"key_event(keycode={p.keycode})"
    if p.action == A.ACTION_TAKE_SCREENSHOT:
        return f"take_screenshot(save_to_album={'true' if p.save_to_album else 'false'})"
    return f"{p.action}()"
