"""Server / Agent / 前端协议共用的纯函数与数据类型。

- actions: VLM 动作集定义 + 解析器 + 坐标归一化（迁移自 Groovy parseAction）
- prompt:  System Prompt 文本（迁移自 Groovy buildSystemPrompt）
- vlm:     VLM HTTP 客户端 + TokenCounter（迁移自 Groovy vlmDecide / recordTokenUsage）
- protocol: WS 消息契约（TypedDict）
- log:     Sonic 风格日志事件构造器
"""
from __future__ import annotations

from ai_phone.shared.actions import (
    KNOWN_ACTIONS,
    ParsedAction,
    extract_action,
    extract_seconds_from_thought,
    extract_thought,
    parse_action,
    vlm_point_to_abs,
)
from ai_phone.shared.log import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, error, info, make_log, warn
from ai_phone.shared.prompt import build_system_prompt
from ai_phone.shared.vlm import Decision, TokenCounter, VLMClient

__all__ = [
    "KNOWN_ACTIONS",
    "ParsedAction",
    "extract_action",
    "extract_seconds_from_thought",
    "extract_thought",
    "parse_action",
    "vlm_point_to_abs",
    "build_system_prompt",
    "Decision",
    "TokenCounter",
    "VLMClient",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "LEVEL_ERROR",
    "make_log",
    "info",
    "warn",
    "error",
]
