"""Optional per-action metadata. Never changes or substitutes an executable action."""
from __future__ import annotations

import json
import re
from typing import Any


MAX_ACTION_SUMMARY_CHARS = 300


def normalize_action_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    # Reject, rather than truncate, metadata that could lose its target/context.
    return text if text and len(text) <= MAX_ACTION_SUMMARY_CHARS else None


ACTION_SUMMARY_POLICY_ZH = """
## 当前动作摘要（附加元数据）
每个非终态动作同时提供 action_summary，通常一两句、最多300字。
描述本条实际要做的操作及目标，保留截图上可见的控件原文、必要的区域/位置；
输入说明输入框，滚动说明操作区域，拖拽说明起止对象。执行值仍以原动作参数为准。
涉及弹窗、引导、权限、安全、支付或业务确认时，保留其类型、处理对象，以及是否
正在阻挡当前操作的可见事实，供后续清洗分类；不得自行宣布该动作可跳过。
不复制子步骤判读、Map全文或整段Thought，不写后续计划，不声称动作已经成功。
不把当前控件替换成Case最终目标，不编造截图中不存在的信息；无法明确描述时返回空字符串。
摘要不改变动作、坐标、执行参数、数量或顺序，原有Thought与终态规则照常执行。
"""


NATIVE_ACTION_SUMMARY_POLICY = """
## Per-action summaries (metadata, not tools or execution evidence)
Alongside this turn's original actions, emit one standalone assistant TEXT line:
ACTION_SUMMARY: {"computer":[{"action":"<native action name>","summary":"<brief target description>"}],"platform":[]}
Use valid single-line JSON. `computer` follows the order of this turn's original
computer calls; `action` is their exact native action name. `platform` follows the
PLATFORM_ACTION declarations, using names such as open_app. Omit an unused group.
Provide one entry per action in that group; use an empty summary when uncertain.
Keep the native computer tool and all its parameters unchanged. Do not put this
metadata in a tool call, thinking/reasoning block, or FINISHED/ASSERT_FAIL reason.
Each summary is at most 300 characters and describes only that action's operation
and target. Preserve exact visible UI text and necessary region/position. For
input name the field; for scrolling name the region; for dragging name the endpoints.
For popups, guides, permissions, security, payments or confirmations, retain the
visible type/object and whether it obstructs the current task; never declare a
step optional or safe to skip. Follow the existing human-readable language policy.
Do not copy substep verdicts, the whole Map or reasoning. Do not describe future
actions or claim this action has succeeded. Do not change action selection/order.
"""


_SUMMARY_LINE = re.compile(r"^[ \t]*ACTION_SUMMARY[ \t]*[:：][ \t]*(.*)$", re.MULTILINE)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate summary field")
        result[key] = value
    return result


def extract_action_summaries(text: str) -> tuple[str, dict]:
    """Remove metadata before existing Thought/terminal/platform parsing.

    Malformed or repeated metadata falls back without rejecting native actions.
    """
    matches = list(_SUMMARY_LINE.finditer(text))
    if not matches:
        return text, {}
    clean = _SUMMARY_LINE.sub("", text)
    if len(matches) != 1:
        return clean, {}
    try:
        data = json.loads(matches[0].group(1), object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return clean, {}
    return clean, data if isinstance(data, dict) else {}


def summaries_for_actions(metadata: dict, group: str, names: list[str]) -> list[str | None]:
    """Bind against original call names/order before adapters reorder or drop calls."""
    fallback = [None] * len(names)
    rows = metadata.get(group)
    if not isinstance(rows, list) or len(rows) != len(names):
        return fallback
    if any(not isinstance(row, dict) or row.get("action") != name
           for row, name in zip(rows, names)):
        return fallback
    return [normalize_action_summary(row.get("summary")) for row in rows]
