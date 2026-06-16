"""多协议 LLM 适配层 · 公共入口。

对 ``vlm_loop`` 暴露两个工厂：
    - :func:`create_main_vlm`：根据 ``settings.vlm_backend`` 分发到对应主 VLM
      实现（doubao_responses / claude_cu / gpt_cu）。
    - :func:`create_assistant`：根据 ``settings.assistant_backend`` 分发到对
      应辅助系统实现（doubao_chat / claude / openai）。

之所以用工厂而不是依赖注入：
1. runner 启动时一次性决定后端，运行期不切换 → 工厂足够。
2. 工厂可以在"切错家但缺 import"时抛出友好提示，避免 import-time 直接崩。
3. 三家实现互相不 import，工厂里"按需 import"，没 anthropic 包也不影响豆包跑。

外部配置入口是 ``AI_PHONE_PHONE_VLM_*`` / ``AI_PHONE_AUX_*`` 两块；这里的
``vlm_backend`` / ``assistant_backend`` 是内部派生字段，不再建议直接手填 ENV。

支持组合：
    - 全 doubao（默认，存量行为）
    - 全 claude
    - 全 openai
经过测试，再放开混搭。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ai_phone.config import Settings, get_settings
from ai_phone.shared.llm.base import (
    BaseAssistant,
    BaseMainVLM,
    Decision,
    TokenCounter,
)

if TYPE_CHECKING:
    pass

__all__ = [
    "BaseAssistant",
    "BaseMainVLM",
    "Decision",
    "TokenCounter",
    "create_main_vlm",
    "create_assistant",
    "SUPPORTED_VLM_BACKENDS",
    "SUPPORTED_ASSISTANT_BACKENDS",
]


# 显式枚举支持的后端 ID，方便配置校验和文档自动生成。
SUPPORTED_VLM_BACKENDS = ("doubao_responses", "claude_cu", "gpt_cu")
SUPPORTED_ASSISTANT_BACKENDS = ("doubao_chat", "claude", "openai")


def create_main_vlm(
    system_prompt: str,
    *,
    counter: Optional[TokenCounter] = None,
    settings: Optional[Settings] = None,
) -> BaseMainVLM:
    """实例化主 VLM 客户端，按 ``settings.vlm_backend`` 分派。

    ``settings`` 留参便于测试时注入隔离的 Settings；生产路径走默认全局缓存。

    错误处理：未知后端 / 缺三方包 / 配置缺失（API key / URL / model）都在
    本函数里以友好提示抛出，避免 vlm_loop 启动期跳到一堆栈底层（httpx /
    anthropic 包内部）才暴露问题。
    """
    cfg = settings or get_settings()
    backend = (cfg.vlm_backend or "doubao_responses").strip().lower()

    if backend == "doubao_responses":
        # 默认路径：复用现有 VLMClient（方舟 Responses API），不做任何包装
        from ai_phone.shared.vlm import VLMClient

        return VLMClient(system_prompt=system_prompt, counter=counter)

    if backend == "claude_cu":
        try:
            from ai_phone.shared.llm.main.claude_cu import ClaudeComputerUseClient
        except ImportError as exc:
            raise RuntimeError(
                "vlm_backend=claude_cu 但 Claude 主 VLM 实现未安装/不可用："
                f"{exc}。请确认 ai_phone/shared/llm/main/claude_cu.py 存在且依赖完整。"
            ) from exc
        return ClaudeComputerUseClient(system_prompt=system_prompt, counter=counter)

    if backend == "gpt_cu":
        try:
            from ai_phone.shared.llm.main.gpt_cu import GPTComputerUseClient
        except ImportError as exc:
            raise RuntimeError(
                "vlm_backend=gpt_cu 但 GPT 主 VLM 实现未安装/不可用："
                f"{exc}。请确认 ai_phone/shared/llm/main/gpt_cu.py 存在且依赖完整。"
            ) from exc
        return GPTComputerUseClient(system_prompt=system_prompt, counter=counter)

    raise RuntimeError(
        f"未知的 vlm_backend={backend!r}，"
        f"支持的取值：{SUPPORTED_VLM_BACKENDS}。请检查 AI_PHONE_PHONE_VLM_PROVIDER "
        "或配置派生逻辑。"
    )


def create_assistant(
    *,
    counter: Optional[TokenCounter] = None,
    settings: Optional[Settings] = None,
) -> BaseAssistant:
    """实例化辅助系统客户端，按 ``settings.assistant_backend`` 分派。

    与 :func:`create_main_vlm` 完全独立——主 VLM 走 Claude 时辅助仍可走
    豆包，反之亦然。
    """
    cfg = settings or get_settings()
    backend = (cfg.assistant_backend or "doubao_chat").strip().lower()

    if backend == "doubao_chat":
        try:
            from ai_phone.shared.llm.assistants.doubao import DoubaoAssistant
        except ImportError as exc:
            raise RuntimeError(
                "assistant_backend=doubao_chat 但豆包辅助实现不可用：" f"{exc}"
            ) from exc
        return DoubaoAssistant(counter=counter)

    if backend == "claude":
        try:
            from ai_phone.shared.llm.assistants.claude import ClaudeAssistant
        except ImportError as exc:
            raise RuntimeError(
                "assistant_backend=claude 但 Claude 辅助实现未安装/不可用：" f"{exc}"
            ) from exc
        return ClaudeAssistant(counter=counter)

    if backend == "openai":
        try:
            from ai_phone.shared.llm.assistants.openai import OpenAIAssistant
        except ImportError as exc:
            raise RuntimeError(
                "assistant_backend=openai 但 OpenAI 辅助实现未安装/不可用：" f"{exc}"
            ) from exc
        return OpenAIAssistant(counter=counter)

    raise RuntimeError(
        f"未知的 assistant_backend={backend!r}，"
        f"支持的取值：{SUPPORTED_ASSISTANT_BACKENDS}。请检查 AI_PHONE_AUX_PROVIDER "
        "或配置派生逻辑。"
    )
