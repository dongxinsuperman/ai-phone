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

__all__ = ["build_system_prompt_for_backend"]


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
