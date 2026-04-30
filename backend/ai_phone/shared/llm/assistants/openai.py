"""辅助系统 · OpenAI Chat Completions 实现（原生）。

辅助系统 3 个底层方法（``BaseAssistant`` Protocol）：
    1. ``match_package``：起跑线包名匹配（纯文本，固定 thinking=False）
    2. ``chat_text``：通道判定 / 审判 / 子步骤拆解（纯文本，可选 thinking）
    3. ``verify_finished``：finished 终局裁决（带图）

走 OpenAI 原生 ``/v1/chat/completions``——比 Responses API 简单、便宜、所有 GPT
模型（gpt-4o / gpt-4.1 / gpt-4o-mini / o-系列）都支持，无 beta 头要求。

为什么辅助走 Chat Completions 而不是与主 VLM 一致的 Responses API：
- 辅助调用是无状态、单轮一来一回，不需要服务端续历史
- Chat API 兼容更老的 OpenAI 模型（gpt-4o-mini / gpt-3.5 等便宜模型）
- 不需要 ``computer_use_preview`` 工具，纯文本 / 视觉就够

差异（vs Doubao Chat 实现）：
- Authorization 还是 Bearer（一致）
- 没有 ``thinking`` 字段；推理力度走 ``reasoning_effort``，仅 o-系列模型生效，
  非推理模型 OpenAI 会静默忽略（不会 422）
- 图片用 ``image_url.url`` 字段（与豆包 Chat 完全一致——豆包 Chat 协议本就仿
  OpenAI；这是为什么本类与 ``DoubaoAssistant`` 长得很像，但端点 / 模型名 / 推
  理字段差异决定我们仍然分两个独立类，避免一家改 prompt 时影响另一家）
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import httpx

from ai_phone.config import get_settings
from ai_phone.shared.llm.base import TokenCounter

__all__ = ["OpenAIAssistant"]


# OpenAI reasoning_effort 取值（仅 o-系列推理模型生效）
_VALID_EFFORT = ("low", "medium", "high")


class OpenAIAssistant:
    """辅助系统 · OpenAI Chat Completions。

    端点统一从 ``settings.assistant_*`` 读取；api_key 留空时回退使用
    ``settings.vlm_api_key``——开源用户多数情况主辅同 key。
    """

    def __init__(self, *, counter: Optional[TokenCounter] = None) -> None:
        self.counter = counter or TokenCounter()

    # ------------------------------------------------------------------
    # 通用底层：发起一次 Chat Completions 调用
    # ------------------------------------------------------------------
    async def _post(
        self,
        *,
        messages: List[Dict[str, Any]],
        thinking: bool,
        scene: str,
        timeout: float = 60.0,
    ) -> str:
        """发送 messages → 抽 choices[0].message.content → 返回字符串。

        ``thinking=True`` 时在 payload 加 ``reasoning_effort=medium``：
        - o1 / o3 / o4-mini 等推理模型生效，效果≈豆包 thinking.enabled
        - gpt-4o / gpt-4.1 等普通模型会被 OpenAI 静默忽略（不报错）
        所以无需在客户端按模型名分支——OpenAI 服务端帮我们消化了不兼容。
        """
        settings = get_settings()
        model = settings.assistant_model
        api_key = settings.assistant_api_key or settings.vlm_api_key
        api_url = settings.assistant_api_url
        if not (api_key and api_url and model):
            raise RuntimeError(
                "OpenAI 辅助系统配置缺失，请检查 AI_PHONE_ASSISTANT_API_URL / "
                "AI_PHONE_ASSISTANT_API_KEY / AI_PHONE_ASSISTANT_MODEL"
            )

        payload: Dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "messages": messages,
        }
        if thinking:
            # 默认 medium——比 low 准、比 high 省。如果未来要让用户细调可以
            # 加 settings.assistant_reasoning_effort，目前固定 medium。
            payload["reasoning_effort"] = "medium"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"{scene} OpenAI Chat Completions 请求失败: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            ).strip()
        elif isinstance(content, str):
            text = content.strip()
        else:
            text = ""
        if not text:
            raise RuntimeError(f"{scene} OpenAI 未返回可解析文本")
        # OpenAI usage 字段：prompt_tokens / completion_tokens / total_tokens
        # （o-系列还有 reasoning_tokens 子字段，TokenCounter 不消费保留即可）
        self.counter.record(scene, model, data.get("usage"))
        return text

    # ------------------------------------------------------------------
    # ① 起跑线 · 包名匹配
    # ------------------------------------------------------------------
    async def match_package(self, app_name: str, packages: List[str]) -> str:
        if not packages:
            raise RuntimeError("无法获取设备应用列表")

        prompt = (
            "Task: From the device's installed third-party app package list, find "
            "the one that best matches the user's description.\n\n"
            f"User description: {app_name}\n\n"
            "Installed package names:\n"
            + "\n".join(packages)
            + "\n\nRequirements:\n"
            "1. Identify the app keyword in the user description\n"
            "2. Find the best-matching package from the list\n"
            "3. Output ONLY the full package name (no explanation, no extra text)\n"
            "4. If no match, output \"NONE\"\n\n"
            "Output format: package name only"
        )
        target = await self._post(
            messages=[
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            thinking=False,
            scene="包名匹配",
        )
        target = target.strip()
        if target.upper() == "NONE":
            return ""
        return target

    # ------------------------------------------------------------------
    # ② / ③ / ⑤ 通用纯文本（通道判定 / 审判 / 子步骤拆解）
    # ------------------------------------------------------------------
    async def chat_text(
        self,
        prompt: str,
        *,
        label: str = "辅助",
        thinking: bool = False,
    ) -> str:
        return await self._post(
            messages=[
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            thinking=thinking,
            scene=label,
        )

    # ------------------------------------------------------------------
    # ④ 断言系统 · finished 终局裁决（带图）
    # ------------------------------------------------------------------
    async def verify_finished(
        self,
        *,
        prompt: str,
        prev_before_bytes: Optional[bytes],
        final_bytes: bytes,
        thinking: bool = True,
    ) -> str:
        """断言系统裁决调用：双图 + 文本 → OpenAI 视觉对照 → 模型原始文本。

        协议（与豆包 / Claude 实现一致）：
        - 系统消息固定为"严格保守的结果验收裁决器"
        - prev_before_bytes 为 None 时退化单图
        - 双图均使用 image_url.url 的 base64 data URL（OpenAI 标准格式，
          与豆包 Chat 协议同源）
        """
        user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if prev_before_bytes is not None:
            prev_b64 = base64.b64encode(prev_before_bytes).decode("ascii")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{prev_b64}"},
                }
            )
        final_b64 = base64.b64encode(final_bytes).decode("ascii")
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{final_b64}"},
            }
        )
        return await self._post(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict, conservative result-verification "
                        "adjudicator."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            thinking=thinking,
            scene="断言系统",
            timeout=120.0,
        )
