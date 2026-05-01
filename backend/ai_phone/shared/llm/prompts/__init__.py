"""模型专属 Prompt 模板集合 + 后端分派工厂。

每家协议有自己的"系统提示词风格"——豆包系喜欢严格的 ``Action: click(...)``
DSL，Claude Computer Use 走 ``computer`` tool + 自然语言推理 + ``FINISHED:``
关键字宣告，OpenAI ``computer-use-preview`` 又有自己的 "Don't ask for
confirmation" 等约束。同一份 case prompt 在三家身上效果差异巨大，所以分
家维护：

    - ``doubao.py``：现有 SYSTEM_PROMPT_TEMPLATE / build_user_prompt 的 facade
    - ``claude_cu.py``：Claude Computer Use 专用模板
    - ``gpt_cu.py``：OpenAI computer-use-preview 专用模板

对外只暴露一个 :func:`build_system_prompt_for_backend` 工厂——调用方
（``vlm_loop.py``）把 ``settings.vlm_backend`` 传进来，自动分派。三家模板
内部签名统一 ``(goal, substeps_text=None) -> str``，便于未来扩家。

历史版本注释里说"P1 阶段本目录除 __init__.py 外为空，待 P2/P5/P7 逐步落
地"——三个模板已落地，本 __init__.py 同步从"占位"升级为"工厂"。漏掉
的 init 改造最后一步：``vlm_loop.py`` 由直接 import 豆包版改为走本工厂。
"""
from __future__ import annotations

from ai_phone.shared.llm.prompts.claude_cu import (
    build_system_prompt as _build_system_prompt_claude_cu,
)
from ai_phone.shared.llm.prompts.doubao import (
    build_system_prompt as _build_system_prompt_doubao,
)
from ai_phone.shared.llm.prompts.gpt_cu import (
    build_system_prompt as _build_system_prompt_gpt_cu,
)

__all__ = [
    "build_system_prompt_for_backend",
    "build_unknown_action_hint",
]


def build_system_prompt_for_backend(
    goal: str,
    *,
    substeps_text: str | None = None,
    backend: str | None = None,
) -> str:
    """按 ``vlm_backend`` 分派到对应家的 system prompt 模板。

    - ``doubao_responses``（默认）：豆包 ``Thought:/Action:`` 文本 DSL
    - ``claude_cu``：Claude Computer Use ``computer`` tool + ``FINISHED:`` 关键字
    - ``gpt_cu``：OpenAI computer-use-preview + "Don't ask for confirmation"

    ``backend`` 取值无效 / 为空时回退豆包版（向后兼容老调用 + 单测无 settings 场景）。
    """
    b = (backend or "doubao_responses").strip().lower()
    if b == "claude_cu":
        return _build_system_prompt_claude_cu(goal, substeps_text=substeps_text)
    if b == "gpt_cu":
        return _build_system_prompt_gpt_cu(goal, substeps_text=substeps_text)
    return _build_system_prompt_doubao(goal, substeps_text=substeps_text)


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
    "close_app / assert_fail。"
    "例如点击请写 click(point='<point>x y</point>')；打开应用写 "
    "open_app(app_name='XXX')；等待写 wait(seconds=N)。"
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
    "There is NO `open_app` / `close_app` builtin — to launch an app, "
    "press the Home key (key='Home') to return to the home screen, then "
    "left_click the app icon. "
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
    "There is NO `open_app` / `close_app` builtin — to launch an app, "
    "use keypress with keys=['Home'] to return to the home screen, then "
    "click the app icon. "
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
