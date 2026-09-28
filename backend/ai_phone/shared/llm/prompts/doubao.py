"""Prompt · Doubao 主 VLM 专用模板（facade）。

历史源 ``shared/prompt.py``。豆包系执行 ``Thought + seed:tool_call`` 文本
协议，且坐标默认归一化 0-1000；外部 Case / Map / Runner 契约不变。

为对齐多协议层"每家自己一个 prompts/<backend>.py"的约定，本文件做 facade
导出，不重复维护。

后续 Claude / GPT 需要的 prompt 风格不同（自然语言驱动 + tool_use 回调
/ computer_use_preview 工具），各自落到 ``prompts/claude_cu.py`` 与
``prompts/gpt_cu.py``，互不耦合。
"""
from __future__ import annotations

from ai_phone.shared.prompt import build_system_prompt

__all__ = ["build_system_prompt"]
