"""辅助系统 · Anthropic Claude Messages API 实现（原生）。

辅助系统三个底层方法（与 ``BaseAssistant`` 协议对齐）：
    1. ``match_package``：起跑线包名匹配（纯文本，固定 thinking=False）
    2. ``chat_text``：通道判定 / 审判 / 子步骤拆解三场景共用入口
    3. ``verify_finished``：finished 终局裁决（带图）

走 Anthropic 原生 ``/v1/messages``，**不**依赖 LiteLLM。开源用户拿到 Anthropic
key 直接配 ``AI_PHONE_ASSISTANT_*`` 即可使用。

关键差异（vs Doubao 实现）：
- 端点不同（api.anthropic.com vs ark.cn-beijing.volces.com）
- headers 用 x-api-key（豆包是 Bearer）
- 思考链字段：``thinking={"type":"enabled","budget_tokens":N}``（仅 4-thinking
  系列模型支持，普通 chat 模型 Anthropic 会忽略，但加上不会报错）
- 图片用 ``{"type":"image","source":{"type":"base64",...}}``（豆包是 image_url）
- response.content 是 list of blocks，需要 join text 块
- usage 字段：input_tokens / output_tokens（无 prompt/completion 别名）

辅助系统不需要 ``anthropic-beta: computer-use-*`` 头，因为没有调用 computer
tool。只主 VLM 才需要那个头。
"""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from ai_phone.config import get_settings
from ai_phone.shared.llm.base import AnalysisResult, TokenCounter

__all__ = ["ClaudeAssistant"]


class ClaudeAssistant:
    """辅助系统 · Anthropic Messages API。

    端点统一从 ``settings.assistant_*`` 读取；api_key 留空时回退使用
    ``settings.vlm_api_key``——开源用户多数情况主辅同 key。
    """

    def __init__(self, *, counter: Optional[TokenCounter] = None) -> None:
        self.counter = counter or TokenCounter()

    # ------------------------------------------------------------------
    # 通用底层：发起一次 Messages API 调用
    # ------------------------------------------------------------------
    async def _post(
        self,
        *,
        messages: List[Dict[str, Any]],
        thinking: bool,
        scene: str,
        system: Optional[str] = None,
        timeout: float = 60.0,
    ) -> str:
        """发送 messages → 拼接 text blocks → 返回字符串。

        ``thinking`` 在 Claude 下的语义：
        - True：``payload.thinking={"type":"enabled","budget_tokens":<budget>}``
        - False：不带 thinking 字段

        budget_tokens 复用 ``settings.vlm_main_thinking_budget``（同一项目用同一个
        预算上限；如未来需要拆分可加 ``assistant_thinking_budget``）。
        """
        settings = get_settings()
        model = settings.assistant_model
        api_key = settings.assistant_api_key or settings.vlm_api_key
        api_url = settings.assistant_api_url
        if not (api_key and api_url and model):
            raise RuntimeError(
                "Claude 辅助系统配置缺失，请检查 AI_PHONE_ASSISTANT_API_URL / "
                "AI_PHONE_ASSISTANT_API_KEY / AI_PHONE_ASSISTANT_MODEL"
            )

        # max_tokens=8192：anthropic /v1/messages 是硬上限不是目标值。
        # 辅助系统的 chat_text 会承担通道判定 / 审判 / 子步骤拆解三种场景，
        # 审判模式开 thinking 时 thought 长尾可达数千 token，4096 偶发被截
        # 后整个 JSON / DSL 输出不闭合，被解析层判错。统一拉到 8192，
        # 与 trajectory_cache 几个辅助 vlm 站点对齐。
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 8192,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if thinking and settings.vlm_main_thinking_budget > 0:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": int(settings.vlm_main_thinking_budget),
            }

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"{scene} Claude Messages API 请求失败: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        data = resp.json()

        # response.content 是 list of blocks（text / thinking）。我们只关心 text
        # 块拼接；thinking 是模型内部推理，调用方不消费。
        text_parts: List[str] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text") or ""
                if isinstance(t, str):
                    text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            raise RuntimeError(f"{scene} Claude 未返回可解析文本")

        # usage 归一化为 TokenCounter 兼容格式
        usage = data.get("usage") or {}
        normalized: Dict[str, Any] = {
            "cache_accounting": "read_write",
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            ),
        }
        cache_read = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        if cache_read is not None:
            normalized["cache_read_tokens"] = int(cache_read)
            normalized["input_tokens_details"] = {"cached_tokens": int(cache_read)}
        if cache_write is not None:
            normalized["cache_write_tokens"] = int(cache_write)
        self.counter.record(scene, model, normalized)
        return text

    # ------------------------------------------------------------------
    # ① 起跑线 · 包名匹配
    # ------------------------------------------------------------------
    async def match_package(self, app_name: str, packages: List[str]) -> str:
        """根据应用名从已安装包列表里挑出最佳包名。

        协议（与豆包实现一致）：
        - 模型只输出"一个完整包名"或字面量 "NONE"
        - 返回 NONE / 空 → 适配层翻译为空串 ``""``
        - 包名匹配是高频轻任务，固定 thinking=False
        """
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
            "Business note: if the user description contains 「洋葱」 (e.g. 「洋葱学园」, "
            "「洋葱数学」), pay special attention to packages whose name contains "
            "\"yangcong\", \"guanghe\", or \"ycmath\". On iOS the apps from this "
            "vendor use multiple naming conventions (public builds often use "
            "yangcong345, enterprise/test builds often use guanghe or ycmath); "
            "do NOT skip a candidate just because it lacks the literal \"yangcong\".\n\n"
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
        """通用纯文本 chat 调用。"""
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
        """断言系统裁决调用：双图 + 文本 → Claude 视觉对照 → 模型原始文本。

        与豆包实现行为一致：
        - 系统消息固定为"严格保守的结果验收裁决器"
        - prev_before_bytes 为 None 时退化单图
        - 双图均使用 base64 source（Anthropic 原生格式，与豆包 image_url 不同）
        """
        user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if prev_before_bytes is not None:
            user_content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(prev_before_bytes).decode("ascii"),
                    },
                }
            )
        user_content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(final_bytes).decode("ascii"),
                },
            }
        )
        return await self._post(
            messages=[{"role": "user", "content": user_content}],
            thinking=thinking,
            scene="断言系统",
            system=(
                "You are a strict, conservative result-verification adjudicator."
            ),
            timeout=120.0,
        )

    # ------------------------------------------------------------------
    # ⑥ 大盘 AI 分析（高级文本：system + user + 透出 usage / 耗时）
    # ------------------------------------------------------------------
    async def analyze_text(
        self,
        *,
        system: str,
        user: str,
        label: str = "AI 分析",
        thinking: bool = False,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> AnalysisResult:
        """大盘 AI 分析专用：system 走 payload.system 字段、user 走 messages[0]。

        Anthropic 协议把 system 单独抽成顶层字段（不像 OpenAI / 豆包是
        ``messages[0]``）。usage 字段是 ``input_tokens / output_tokens``，需要
        翻译到统一的 prompt/completion 别名。
        """
        settings = get_settings()
        model = settings.assistant_model
        api_key = settings.assistant_api_key or settings.vlm_api_key
        api_url = settings.assistant_api_url
        if not (api_key and api_url and model):
            raise RuntimeError(
                "Claude 辅助系统配置缺失，请检查 AI_PHONE_ASSISTANT_API_URL / "
                "AI_PHONE_ASSISTANT_API_KEY / AI_PHONE_ASSISTANT_MODEL"
            )

        # max_tokens=8192：finished 终局裁决要带图比对动作前后两帧并写完整
        # 理由（断言通过 / 不通过 / 需要重试），与 chat_text 同样存在长尾
        # 截断风险。anthropic /v1/messages 是硬上限不是目标值，调高只防截断。
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 8192,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if thinking and settings.vlm_main_thinking_budget > 0:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": int(settings.vlm_main_thinking_budget),
            }
            # Anthropic 硬约束：thinking enabled 时 temperature 必须 = 1，
            # 否则 API 直接 400 ``temperature may only be set to 1 when
            # thinking is enabled``。``analyze_text`` 把 thinking 与
            # temperature 都暴露成公开关键字参数，调用方很容易传出非法
            # 组合（比如想要确定性输出 temperature=0.2 + 想要思考
            # thinking=True）→ 这里强制覆盖兜底，避免组合参数静默 400。
            if temperature != 1:
                logger.warning(
                    "{} thinking enabled 时 Anthropic 强制 temperature=1，"
                    "已忽略调用方传入的 temperature={}",
                    label,
                    temperature,
                )
            payload["temperature"] = 1

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            raise RuntimeError(
                f"{label} Claude Messages API 请求失败: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        data = resp.json()

        text_parts: List[str] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text") or ""
                if isinstance(t, str):
                    text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            raise RuntimeError(f"{label} Claude 未返回可解析文本")

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        # 归一化进 TokenCounter（与 _post 里的处理逻辑保持一致）
        normalized: Dict[str, Any] = {
            "cache_accounting": "read_write",
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        cache_read = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        if cache_read is not None:
            normalized["cache_read_tokens"] = int(cache_read)
            normalized["input_tokens_details"] = {"cached_tokens": int(cache_read)}
        if cache_write is not None:
            normalized["cache_write_tokens"] = int(cache_write)
        self.counter.record(label, model, normalized)

        return AnalysisResult(
            model=model,
            text=text,
            elapsed_ms=elapsed_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
