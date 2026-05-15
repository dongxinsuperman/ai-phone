"""海外辅 vlm 协议对齐 helper（共享给 ephemeral / recovery / v3 locator）。

见 ``docs/executable-logic-contract.md §14``：当主 vlm 是海外 Computer Use
（``claude_cu`` / ``gpt_cu``）时，所有"操作手机的辅 vlm"（标签 vlm 即
ephemeral gate / 辅助 vlm 即 recovery / 定位 vlm 即 v3 plan locator）都用
**主 vlm 同一个模型 + 同一把 key + 同一个 endpoint**，但调用方式从 CU
agent loop 翻译成"chat 单次协议"——既复用主 vlm 的视觉能力，又避免 CU
agent 反射在"单次 verdict / 单次定位"任务上把模型带飞。

为什么需要协议翻译：

- claude_cu：主链路打 ``anthropic-beta: computer-use-2025-01-24`` 头并挂
  ``computer`` 工具走 ``/v1/messages``。同一个 endpoint 不带 beta 头、不挂
  工具时退回普通 messages chat —— 模型回到"看图回答"模式，按 prompt 输出
  文本即可。所以 URL 复用，backend 翻译成 ``claude_messages`` 让下游 chat
  实现按"不开 CU"的方式调。
- gpt_cu：主链路用 ``/v1/responses`` + ``computer_use_preview`` 工具。chat
  通道用 ``/v1/chat/completions``，URL 必须按后缀替换；backend 翻译成
  ``openai_compatible``。
"""
from __future__ import annotations

from typing import Tuple


def overseas_cu_to_chat_config(
    *,
    main_backend: str,
    main_api_url: str,
    main_api_key: str,
    main_model: str,
) -> Tuple[str, str, str, str]:
    """把海外主 vlm CU 配置翻译成同模型 + chat 单次协议配置。

    返回 ``(backend, api_url, api_key, model)``。``backend`` 用下游 chat
    实现已经支持的字面量：``claude_messages`` / ``openai_compatible``。

    其它（豆包系 / 未知 / 自部署 OpenAI 兼容代理跑豆包模型等）一律按
    ``openai_compatible`` 兜底返回，URL 不动。这条分支理论上不会被命中——
    海外辅 vlm 翻译只在 ``claude_cu`` / ``gpt_cu`` 时调用——但保留兜底避免
    上游漏判时静默错乱。
    """
    backend = (main_backend or "").strip().lower()
    if backend == "claude_cu":
        return ("claude_messages", main_api_url, main_api_key, main_model)
    if backend == "gpt_cu":
        return (
            "openai_compatible",
            gpt_responses_url_to_chat(main_api_url),
            main_api_key,
            main_model,
        )
    return ("openai_compatible", main_api_url, main_api_key, main_model)


def gpt_responses_url_to_chat(url: str) -> str:
    """把 OpenAI Responses 端点翻译成 chat completions 端点。

    主链路 ``gpt_cu`` 一般配 ``https://api.openai.com/v1/responses``，chat 通道
    需要 ``/v1/chat/completions``。仅做后缀替换，自部署代理保持原 host/path
    前缀（如 ``https://my-proxy.internal/openai/v1/responses`` →
    ``https://my-proxy.internal/openai/v1/chat/completions``）。
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    if raw.endswith("/v1/responses"):
        return raw[: -len("/v1/responses")] + "/v1/chat/completions"
    if raw.endswith("/responses"):
        return raw[: -len("/responses")] + "/chat/completions"
    return raw


def main_vlm_is_overseas_cu(
    *,
    main_vlm_backend: str,
    configured_vlm_backend: str,
) -> bool:
    """主 vlm 是否走海外 Computer Use 链路（claude_cu / gpt_cu）。

    要求 ``main_vlm_backend`` 和 ``configured_vlm_backend`` 一致——前者由
    调用方注入（实际跑的主链路），后者来自 settings.vlm_backend（配置侧），
    避免某些临时切换场景下错误命中协议翻译路径。
    """
    backend = (main_vlm_backend or configured_vlm_backend or "").strip().lower()
    configured = (configured_vlm_backend or "").strip().lower()
    return backend in {"claude_cu", "gpt_cu"} and configured == backend
