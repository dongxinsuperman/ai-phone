"""Default Doubao main VLM: Chat Completions + strict client-owned window.

Only transport and request history differ from Responses. Use the same fixed
System/Case/substeps/Map, Seed XML parser, Decision and TokenCounter contracts.
No early round, historical summary or provider response ID survives the window.
Local runner/recorder histories remain outside this client's bounded state.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import time
import uuid
from collections import deque
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from ai_phone.config import Settings, get_settings
from ai_phone.shared import actions as A
from ai_phone.shared.seed_gui_actions import extract_thought, parse_actions
from ai_phone.shared.vlm import Decision, TokenCounter
from ai_phone.shared.llm.output_limits import DOUBAO_CHAT_MAX_COMPLETION_TOKENS


class DoubaoChatWindowClient:
    """A window includes the current user and at most N-1 completed exchanges.

    The fixed Map is prepended to the oldest retained user on a deep copy; it
    never consumes a history slot or accumulates in the stored exchanges.
    Failed HTTP attempts are not dialogue rounds. A successful but invalid XML
    reply is retained for the runner's existing same-image correction round.
    """

    DEFAULT_USER_PROMPT = "What's the next step that you will do to help with the task?"

    def __init__(
        self,
        system_prompt: str,
        counter: Optional[TokenCounter] = None,
        *,
        initial_user_context: Optional[str] = None,
        settings: Optional[Settings] = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        cfg = settings or get_settings()
        self.api_url = (cfg.vlm_chat_api_url or "").strip()
        self.api_key = (cfg.vlm_api_key or "").strip()
        self.model = (cfg.vlm_model or "").strip()
        if not self.api_url or not self.api_key or not self.model:
            raise RuntimeError("豆包 Chat 主 VLM 配置缺失，请检查 AI_PHONE_PHONE_VLM_* 配置")
        rounds = getattr(cfg, "vlm_history_window_rounds", 5)
        # Server runtime overrides use model_copy; enforce bounds here as well.
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 64:
            raise RuntimeError("AI_PHONE_VLM_HISTORY_WINDOW_ROUNDS 必须是 1-64 的整数")
        self.window_rounds = rounds
        self.timeout = timeout_seconds
        self.system_prompt = system_prompt
        self.initial_user_context = (initial_user_context or "").strip()
        self.counter = counter or TokenCounter()
        self.pending_hints: list[str] = []
        self.segment_count = 1
        self._history: deque[tuple[Dict[str, Any], str]] = deque(maxlen=rounds - 1)
        # Route affinity only, not provider memory. Never share across Runs or
        # include phone numbers, credentials, Case text or device identifiers.
        self._prompt_cache_key = "aiphone-run-" + uuid.uuid4().hex

    @property
    def last_prompt_tokens(self) -> int:
        return self.counter.last_prompt_tokens

    def add_hint(self, text: str) -> None:
        if text:
            self.pending_hints.append(text)

    def should_reset_session(self) -> bool:
        # No provider history chain: the bounded window, not the legacy token
        # threshold, controls history. Do not inject count-only resume hints.
        return False

    def reset_session(self, resume_hint: Optional[str] = None) -> Optional[str]:
        """Compatibility no-op; this client never starts provider segments."""
        return None

    def _messages(self, current_user: Dict[str, Any]) -> list[Dict[str, Any]]:
        # Read system_prompt now: Runner injects substeps after construction.
        messages: list[Dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for user, raw in self._history:
            messages.extend(
                [
                    copy.deepcopy(user),
                    {"role": "assistant", "content": raw},
                ]
            )
        messages.append(copy.deepcopy(current_user))
        if self.initial_user_context:
            # Preserve the original System contract: Map lives in first User.
            messages[1]["content"].insert(0, {"type": "text", "text": self.initial_user_context})
        return messages

    async def _post_with_retry(
        self,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        *,
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        """Independent transport with the legacy one-retry/backoff policy."""
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.post(self.api_url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 1:
                    raise
                logger.warning("Chat 滑窗网络异常 {}，0.5s 后重试一次", type(exc).__name__)
                await asyncio.sleep(0.5)
                continue
            if response.status_code == 200:
                return response.json()
            if (response.status_code == 429 or 500 <= response.status_code < 600) and attempt == 0:
                logger.warning("Chat 滑窗 HTTP {}，0.5s 后重试一次", response.status_code)
                await asyncio.sleep(0.5)
                continue
            raise RuntimeError(f"VLM Chat API 失败: status={response.status_code} body={response.text[:500]}")
        raise RuntimeError("VLM Chat API 重试结束但没有返回响应")

    @staticmethod
    def _response_text(data: Dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        return ""

    async def decide(self, screenshot_bytes: bytes, *, mime: str = "image/jpeg") -> Decision:
        data_url = f"data:{mime};base64," + base64.b64encode(screenshot_bytes).decode("ascii")
        pending_backup = list(self.pending_hints)
        self.pending_hints.clear()
        current_user: Dict[str, Any] = {
            "role": "user",
            "content": [
                *[{"type": "text", "text": hint} for hint in pending_backup],
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": self.DEFAULT_USER_PROMPT},
            ],
        }
        payload = {
            "model": self.model,
            "messages": self._messages(current_user),
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "max_completion_tokens": DOUBAO_CHAT_MAX_COMPLETION_TOKENS,
            "prompt_cache_key": self._prompt_cache_key,
        }
        # No tools / caching / store / previous_response_id.
        # Implicit caching is automatic; keeping a route key does not add history.
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        started = time.monotonic()
        try:
            data = await self._post_with_retry(payload, headers, timeout_seconds=float(self.timeout))
            elapsed_ms = int((time.monotonic() - started) * 1000)
            raw = self._response_text(data)
            self.counter.record("VLM决策", self.model, data.get("usage"))
            self._history.append((current_user, raw))
            parsed = parse_actions(raw)
            if not parsed:
                parsed = [
                    A.ParsedAction(
                        action=A.ACTION_ASSERT_FAIL,
                        content=f"无法解析决策输出: Seed GUI XML缺失或非法: {raw[:100]}",
                        raw="assert_fail(content='无法解析决策输出: Seed GUI XML缺失或非法')",
                    )
                ]
            action_strs = [item.raw or item.action for item in parsed]
            logger.info(
                "VLM Chat 滑窗决策耗时 {}ms | 窗口={} | 历史响应={}",
                elapsed_ms,
                self.window_rounds,
                len(payload["messages"]) // 2 - 1,
            )
            return Decision(
                thought=extract_thought(raw),
                action_str=action_strs[0],
                action_strs=action_strs,
                elapsed_ms=elapsed_ms,
                raw_content=raw,
                parsed_actions=parsed,
            )
        except Exception as exc:
            if pending_backup:
                self.pending_hints[:0] = pending_backup
            raise RuntimeError(f"VLM 决策异常: {type(exc).__name__}: {exc}") from exc
