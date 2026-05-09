"""轨迹缓存 action 清洗 adapter。

不同主 VLM backend 的原始动作日志形态差异很大：
- doubao_responses: 项目 DSL，如 click(point='<point>...')
- claude_cu: Anthropic computer tool 原始日志，如 computer.left_click({...})
- gpt_cu: OpenAI computer_call 原始日志，如 computer.click({...})

这里做高冗余、低耦合的独立映射：不 import 各家 VLM client 的私有函数，
只消费 RunLog/RunStep 里的 raw action 字符串，并统一吐项目内 ParsedAction。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from ai_phone.shared import actions as A

_COMPUTER_ACTION_RE = re.compile(r"^\s*computer\.(\w+)\((.*)\)\s*$", re.DOTALL)
_PLATFORM_RAW_ACTION_RE = re.compile(
    r"^\s*platform\.(\w+)\(\s*app_name\s*=\s*['\"]([^'\"]+)['\"]\s*\)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_cache_action(raw_action: str, *, backend: str) -> A.ParsedAction:
    """按主 VLM backend 解析 raw action。"""
    normalized_backend = (backend or "doubao_responses").strip().lower()
    if normalized_backend == "claude_cu":
        return parse_claude_cache_action(raw_action)
    if normalized_backend == "gpt_cu":
        return parse_gpt_cache_action(raw_action)
    return parse_doubao_cache_action(raw_action)


def parse_doubao_cache_action(raw_action: str) -> A.ParsedAction:
    """豆包路径：直接使用项目 DSL 解析器。"""
    return A.parse_action(raw_action)


def parse_claude_cache_action(raw_action: str) -> A.ParsedAction:
    """Claude CU 路径：解析 Anthropic computer tool raw 日志。"""
    platform = _parse_platform_action(raw_action)
    if platform is not None:
        return platform

    parsed = _parse_computer_raw(raw_action)
    if parsed is None:
        return A.parse_action(raw_action)
    tool_name, args = parsed
    coord = _coord(args.get("coordinate"))
    raw_repr = raw_action.strip()

    if tool_name == "left_click":
        return _point_action(A.ACTION_CLICK, coord, raw_repr)
    if tool_name == "right_click":
        return _point_action(A.ACTION_LONG_PRESS, coord, raw_repr)
    if tool_name == "double_click":
        return _point_action(A.ACTION_DOUBLE_TAP, coord, raw_repr)
    if tool_name == "left_click_drag":
        start = _coord(args.get("start_coordinate"))
        end = coord
        if start is not None and end is not None:
            return A.ParsedAction(
                action=A.ACTION_DRAG,
                start_point=start,
                end_point=end,
                raw=raw_repr,
                coord_space="absolute",
            )
        return _unknown(raw_repr)
    if tool_name == "type":
        return A.ParsedAction(
            action=A.ACTION_TYPE,
            content=str(args.get("text") or ""),
            raw=raw_repr,
            coord_space="absolute",
        )
    if tool_name == "scroll":
        direction = str(args.get("scroll_direction") or "down").lower()
        if direction not in {"up", "down", "left", "right"}:
            direction = "down"
        try:
            amount = int(args.get("scroll_amount") or 1)
        except (TypeError, ValueError):
            amount = 1
        return A.ParsedAction(
            action=A.ACTION_SCROLL,
            point=coord or [500, 500],
            direction=direction,
            scroll_amount=max(1, min(10, amount)),
            raw=raw_repr,
            coord_space="absolute",
        )
    if tool_name == "key":
        return _key_action(str(args.get("text") or ""), raw_repr)
    if tool_name == "wait":
        try:
            seconds = max(1, int(round(float(args.get("duration") or 1000) / 1000.0)))
        except (TypeError, ValueError):
            seconds = 1
        return A.ParsedAction(
            action=A.ACTION_WAIT,
            seconds=seconds,
            raw=raw_repr,
            coord_space="absolute",
        )
    if tool_name == "screenshot":
        return A.ParsedAction(
            action=A.ACTION_WAIT,
            seconds=1,
            raw=raw_repr,
            coord_space="absolute",
        )
    return _unknown(raw_repr)


def parse_gpt_cache_action(raw_action: str) -> A.ParsedAction:
    """OpenAI CU 路径：解析 computer-use-preview raw 日志。"""
    platform = _parse_platform_action(raw_action)
    if platform is not None:
        return platform

    parsed = _parse_computer_raw(raw_action)
    if parsed is None:
        return A.parse_action(raw_action)
    tool_name, args = parsed
    raw_repr = raw_action.strip()

    if tool_name == "click":
        point = _xy(args)
        button = str(args.get("button") or "left").lower()
        if button == "right":
            return _point_action(A.ACTION_LONG_PRESS, point, raw_repr)
        return _point_action(A.ACTION_CLICK, point, raw_repr)
    if tool_name == "double_click":
        return _point_action(A.ACTION_DOUBLE_TAP, _xy(args), raw_repr)
    if tool_name == "scroll":
        point = _xy(args) or [500, 500]
        try:
            sx = int(args.get("scroll_x") or 0)
            sy = int(args.get("scroll_y") or 0)
        except (TypeError, ValueError):
            sx = sy = 0
        if abs(sy) >= abs(sx):
            direction = "down" if sy > 0 else "up"
            magnitude = abs(sy)
        else:
            direction = "right" if sx > 0 else "left"
            magnitude = abs(sx)
        amount = max(1, min(10, int(round(magnitude / 100)))) if magnitude else 1
        return A.ParsedAction(
            action=A.ACTION_SCROLL,
            point=point,
            direction=direction,
            scroll_amount=amount,
            raw=raw_repr,
            coord_space="absolute",
        )
    if tool_name == "type":
        return A.ParsedAction(
            action=A.ACTION_TYPE,
            content=str(args.get("text") or ""),
            raw=raw_repr,
            coord_space="absolute",
        )
    if tool_name == "keypress":
        keys = args.get("keys") or []
        if isinstance(keys, list) and keys:
            return _key_action(str(keys[0]), raw_repr)
        return _unknown(raw_repr)
    if tool_name == "wait":
        return A.ParsedAction(
            action=A.ACTION_WAIT,
            seconds=1,
            raw=raw_repr,
            coord_space="absolute",
        )
    if tool_name == "drag":
        path = args.get("path") or []
        if isinstance(path, list) and len(path) >= 2:
            start = _xy(path[0]) if isinstance(path[0], dict) else None
            end = _xy(path[-1]) if isinstance(path[-1], dict) else None
            if start is not None and end is not None:
                return A.ParsedAction(
                    action=A.ACTION_DRAG,
                    start_point=start,
                    end_point=end,
                    raw=raw_repr,
                    coord_space="absolute",
                )
        return _unknown(raw_repr)
    if tool_name == "screenshot":
        return A.ParsedAction(
            action=A.ACTION_WAIT,
            seconds=1,
            raw=raw_repr,
            coord_space="absolute",
        )
    return _unknown(raw_repr)


def _parse_computer_raw(raw_action: str) -> Optional[tuple[str, Dict[str, Any]]]:
    match = _COMPUTER_ACTION_RE.match(raw_action or "")
    if match is None:
        return None
    tool_name = match.group(1).strip()
    raw_args = match.group(2).strip()
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as exc:
        logger.warning("无法解析 computer action JSON: {} | raw={}", exc, raw_action[:300])
        return None
    if not isinstance(args, dict):
        return None
    return tool_name, args


def _parse_platform_action(raw_action: str) -> Optional[A.ParsedAction]:
    match = _PLATFORM_RAW_ACTION_RE.match(raw_action or "")
    if match is None:
        return None
    action_name = match.group(1).strip().lower()
    app_name = match.group(2).strip()
    if action_name not in {A.ACTION_OPEN_APP, A.ACTION_CLOSE_APP}:
        return _unknown(raw_action)
    return A.ParsedAction(
        action=action_name,
        name=app_name,
        raw=raw_action.strip(),
        coord_space="absolute",
    )


def _point_action(
    action: str,
    point: Optional[List[int]],
    raw: str,
) -> A.ParsedAction:
    if point is None:
        return _unknown(raw)
    return A.ParsedAction(
        action=action,
        point=point,
        raw=raw,
        coord_space="absolute",
    )


def _key_action(key_name: str, raw: str) -> A.ParsedAction:
    normalized = _normalize_key_name(key_name)
    if normalized in {"home"}:
        return A.ParsedAction(action=A.ACTION_PRESS_HOME, raw=raw, coord_space="absolute")
    if normalized in {"back", "escape", "esc"}:
        return A.ParsedAction(action=A.ACTION_PRESS_BACK, raw=raw, coord_space="absolute")
    keycode = A.X11_TO_ANDROID_KEYCODE.get(normalized)
    if keycode is None:
        return _unknown(raw)
    return A.ParsedAction(
        action=A.ACTION_KEY_EVENT,
        keycode=keycode,
        raw=raw,
        coord_space="absolute",
    )


def _normalize_key_name(key_name: str) -> str:
    raw = str(key_name or "").strip().lower()
    normalized = raw.replace("-", "_").replace(" ", "_")
    aliases = {
        "arrowup": "up",
        "arrow_up": "up",
        "uparrow": "up",
        "arrowdown": "down",
        "arrow_down": "down",
        "downarrow": "down",
        "arrowleft": "left",
        "arrow_left": "left",
        "leftarrow": "left",
        "arrowright": "right",
        "arrow_right": "right",
        "rightarrow": "right",
        "pageup": "page_up",
        "page_up": "page_up",
        "pagedown": "page_down",
        "page_down": "page_down",
        "back_space": "backspace",
        "backspace": "backspace",
        "del": "delete",
        "delete": "delete",
        "esc": "esc",
        "escape": "escape",
        "return": "return",
        "enter": "enter",
        "spacebar": "space",
        "space": "space",
        "tab": "tab",
        "volumeup": "volume_up",
        "volume_up": "volume_up",
        "volumedown": "volume_down",
        "volume_down": "volume_down",
    }
    return aliases.get(normalized, normalized)


def _coord(value: Any) -> Optional[List[int]]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return [int(value[0]), int(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def _xy(value: Dict[str, Any]) -> Optional[List[int]]:
    try:
        return [int(value.get("x")), int(value.get("y"))]
    except (TypeError, ValueError):
        return None


def _unknown(raw: str) -> A.ParsedAction:
    return A.ParsedAction(action="unknown", raw=raw, coord_space="absolute")
