# Copied from main prompts at 2d13a03; maintained independently for long rescue.
"""独立长程救援的后端提示词分派。模板从 2d13a03 复制后独立维护。

只共用动作协议、Map 契约等基础能力，不导入普通主执行的提示词模板。
"""
from __future__ import annotations

from .claude_cu import (
    build_system_prompt as _build_system_prompt_claude_cu,
)
from .doubao import (
    build_system_prompt as _build_system_prompt_doubao,
)
from .gpt_cu import (
    build_system_prompt as _build_system_prompt_gpt_cu,
)

__all__ = [
    "build_system_prompt_for_backend",
    "build_unknown_action_hint",
]


def _build_backend_prompt(
    goal: str,
    *,
    substeps_text: str | None = None,
    function_map_context: str | None = None,
    backend: str | None = None,
    zh_readable: bool = False,
) -> str:
    """按 ``vlm_backend`` 分派到对应家的 system prompt 模板。

    - ``doubao_responses``（默认）：豆包 ``Thought + seed:tool_call`` 文本协议
    - ``claude_cu``：Claude Computer Use ``computer`` tool + ``FINISHED:`` 关键字
    - ``gpt_cu``：OpenAI computer-use-preview + "Don't ask for confirmation"

    ``function_map_context`` 正文不会进入返回值；非空只会启用不含正文的 Map
    使用契约。保留该关键字参数，兼容既有内部/外部调用方。

    ``backend`` 取值无效 / 为空时回退豆包版（向后兼容老调用 + 单测无 settings 场景）。
    """
    b = (backend or "doubao_responses").strip().lower()
    if b == "claude_cu":
        return _build_system_prompt_claude_cu(
            goal,
            substeps_text=substeps_text,
            function_map_context=function_map_context,
            zh_readable=zh_readable,
        )
    if b == "gpt_cu":
        return _build_system_prompt_gpt_cu(
            goal,
            substeps_text=substeps_text,
            function_map_context=function_map_context,
            zh_readable=zh_readable,
        )
    return _build_system_prompt_doubao(
        goal,
        substeps_text=substeps_text,
        function_map_context=function_map_context,
    )


# ---------------------------------------------------------------------------
# 未知动作纠偏提示（runner 在解析失败时注入到下一轮 user 头部）
# ---------------------------------------------------------------------------
# 三家可识别动作集完全不同——直接发豆包动作清单给 Claude/GPT 会让它们
# 主动模仿（实测 Claude 收到含 ``open_app`` 的提示后，把整段
# ``open_app(app_name='洋葱学园')`` 当成 type 的 text 输入到屏幕上，完全
# 跑偏）。所以纠偏提示同样必须按 backend 分家。
_UNKNOWN_ACTION_HINT_DOUBAO = (
    "⚠️ 你上一步输出的动作名「{action}」不在规范动作集合里，未被执行。"
    "请严格使用以下动作名之一：click / long_press / type / scroll / drag / "
    "open_app / press_home / press_back / finished / double_tap / wait / "
    "close_app / take_screenshot / assert_fail。"
    "动作必须放在 <seed:tool_call> 中，function name 使用上述动作名，"
    "parameter 必须符合 System Prompt 中的 JSON Schema；不要输出 Action: 行。"
    "请基于当前页面重新决策并输出规范动作。"
)

# Claude Computer Use（computer_20250124）内置动作集，与 claude_cu.py 的
# _tool_use_to_parsed_action 映射表对齐。**没有 open_app / close_app**
# ——这是手机自动化项目级抽象，不是 computer tool 内置动作；要打开 App
# 必须先 home 回桌面再点 App 图标。终态走 ``FINISHED:`` / ``ASSERT_FAIL:``
# 文本关键字而非 tool 调用，与 system prompt 同协议。
_UNKNOWN_ACTION_HINT_CLAUDE_CU = (
    "⚠️ Your previous action \"{action}\" was not recognized and was "
    "discarded. Use the `computer` tool with one of these actions: "
    "left_click / right_click / double_click / left_click_drag / type / "
    "scroll / key / wait. "
    "For `key`, use only mapped X11 names: Return / Tab / BackSpace / "
    "Delete / space / Up / Down / Left / Right / Page_Up / Page_Down / "
    "Home / Back / Escape / Menu / search / volume_up / volume_down. "
    "To launch / close an app, prefer the PLATFORM_ACTION text protocol "
    "(`PLATFORM_ACTION: open_app(app_name='X')` on its own line) — do NOT "
    "press Home + hunt the icon. "
    "To declare task outcome, end your assistant message with "
    "`FINISHED: <reason>` or `ASSERT_FAIL: <reason>` on its own line "
    "(NOT a tool call)."
)

# OpenAI computer-use-preview 内置动作集，与 gpt_cu.py 的
# _computer_call_to_parsed_action 映射表对齐。同样**没有 open_app**
# 概念；keypress 走 X11/xdotool key 名（"Home" / "BackSpace" 等）。
_UNKNOWN_ACTION_HINT_GPT_CU = (
    "⚠️ Your previous action \"{action}\" was not recognized and was "
    "discarded. Use the computer tool with one of these actions: "
    "click / double_click / scroll / type / keypress / wait / drag. "
    "For `keypress`, use only mapped key names: Enter / Return / Tab / "
    "BackSpace / Delete / space / Up / Down / Left / Right / Page_Up / "
    "Page_Down / Home / Back / Escape / Menu / search / volume_up / "
    "volume_down. "
    "To launch / close an app, prefer the PLATFORM_ACTION text protocol "
    "(`PLATFORM_ACTION: open_app(app_name='X')` on its own line) — do NOT "
    "use keypress(['Home']) + hunt the icon. "
    "To declare task outcome, end your assistant message with "
    "`FINISHED: <reason>` or `ASSERT_FAIL: <reason>` on its own line "
    "(NOT a tool call)."
)


def build_unknown_action_hint(action: str, *, backend: str | None = None) -> str:
    """按 ``vlm_backend`` 生成"未知动作纠偏提示"，runner 注入下一轮 user 头部。

    每家提示用各自模型能识别的动作名表述，避免 Claude/GPT 看到豆包 DSL
    （open_app / close_app / press_home 等）后误以为是自己的动作集而尝试
    模仿——实测 Claude 收到含 open_app 的纠偏提示后，会把
    ``open_app(app_name='X')`` 整串当成 type 的 text 输入到屏幕。
    """
    b = (backend or "doubao_responses").strip().lower()
    if b == "claude_cu":
        template = _UNKNOWN_ACTION_HINT_CLAUDE_CU
    elif b == "gpt_cu":
        template = _UNKNOWN_ACTION_HINT_GPT_CU
    else:
        template = _UNKNOWN_ACTION_HINT_DOUBAO
    return template.format(action=action)


def build_system_prompt_for_backend(goal: str, **kwargs) -> str:
    return (
        "【独立长程救援执行器】\n"
        "你接手的是已经执行过一部分的 Case，不是普通首跑，也不是一次性局部救援。"
        "使用自己的持续会话，根据最新截图逐步完成剩余任务。"
        "交接记录给出执行起点与事实，原始 Case、Map 和验收要求保持完整；"
        "先确定尚未满足的业务子步骤，之后保持顺序，不因缓存编号较大就假定业务目标已完成。\n"
        + _build_backend_prompt(goal, **kwargs)
    )
