"""轨迹缓存状态路标 MISS 后的 VLM 局部恢复。

执行能力同源：doubao 系沿用可执行 Thought/Action vision 协议；海外
``claude_cu`` / ``gpt_cu`` 主链路沿用主 VLM Computer Use 能力，只通过 prompt
把职责收窄到当前 action 的局部恢复。普通 chat/messages 兼容路径保留给非 CU
或历史配置，但不能作为海外 CU 的执行能力降级。

当前 doubao 系复用主 VLM 的 Thought/Action DSL，不另起 JSON 协议。recovery
VLM 的作用域不是重跑完整 case，而是在 action_i 的 handoff 不一致时做局部
恢复：

- ``CONTINUE_REPLAY``：当前差异可接受（视频/资源位/文案变化等），继续缓存回放
- ``WAIT_MORE``：页面可能还在加载，再等一段时间后由 ReplayRunner 重比一次
- ``REPAIR_ACTION``：输出一个合法主 VLM action，由 ReplayRunner 执行后重比
- ``ASSERT_FAIL``：轨迹已偏航或被阻挡，终止缓存通道

不做：重跑完整 case、任意跳步。这些在 v2 之后的阶段再开。
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw

from ai_phone.config import Settings, get_settings
from ai_phone.shared import actions as A


VERDICT_CONTINUE = "CONTINUE_REPLAY"
VERDICT_WAIT_MORE = "WAIT_MORE"
VERDICT_ASSERT_FAIL = "ASSERT_FAIL"
VERDICT_REPAIR_ACTION = "REPAIR_ACTION"


@dataclass
class RecoveryDecision:
    """recovery_vlm 单次调用的裁决结果。

    所有字段都设默认值，方便降级路径直接构造（缺配置 / 调用失败 / 协议解析
    失败统一收成 ASSERT_FAIL）。``raw`` 保留模型原始文本，便于 RunLog 排查。
    """

    verdict: str
    reason: str
    wait_ms: int = 0
    raw: str = ""
    elapsed_ms: int = 0
    error: str = ""
    thought: str = ""
    action_text: str = ""
    parsed_actions: List[A.ParsedAction] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.verdict in (VERDICT_CONTINUE, VERDICT_ASSERT_FAIL)


class CacheReplayRecoveryVerifier:
    """缓存回放偏航判断专线。

    只对外暴露 ``verify_alignment_miss``，把当前截图、缓存 handoff 图、动作
    上下文交给 VLM 做一次局部恢复决策。本类只负责调用和解析，具体 action
    执行由 ReplayRunner 完成。
    """

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        main_vlm_backend: Optional[str] = None,
    ) -> None:
        self.settings = settings or get_settings()
        # 主 VLM backend 用于推断 recovery 输出的坐标空间：
        #   - doubao_responses / 默认 → "normalized"（0-1000 归一化，豆包系约定）
        #   - claude_cu / gpt_cu       → "absolute" （图像像素，海外 CU 系约定）
        # 海外两家训练时就是按图像像素回坐标的，强行让它们输出 0-1000 归一化反
        # 而不准；豆包系按 prompt 指令稳定输出 0-1000，所以让 recovery prompt 跟
        # 主 VLM 训练习惯走，三家都拿到自己最稳的形式。
        self._main_vlm_backend: str = (main_vlm_backend or "").strip().lower()

    # ------------------------------------------------------------------
    # 配置 / 可用性
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        s = self.settings
        if self._use_main_executable_vlm():
            return bool(
                s.trajectory_cache_recovery_vlm_enabled
                and s.vlm_api_url
                and s.vlm_api_key
                and s.vlm_model
            )
        return bool(
            s.trajectory_cache_recovery_vlm_enabled
            and s.trajectory_cache_recovery_vlm_api_url
            and s.trajectory_cache_recovery_vlm_api_key
            and s.trajectory_cache_recovery_vlm_model
        )

    def configuration_problem(self) -> str:
        """返回一句人类可读的"为什么不可用"，方便降级路径写日志。"""
        s = self.settings
        if not s.trajectory_cache_recovery_vlm_enabled:
            return "recovery_vlm 未启用（trajectory_cache_recovery_vlm_enabled=false）"
        if self._use_main_executable_vlm():
            missing: List[str] = []
            if not s.vlm_api_url:
                missing.append("vlm_api_url")
            if not s.vlm_api_key:
                missing.append("vlm_api_key")
            if not s.vlm_model:
                missing.append("vlm_model")
            if missing:
                return f"主 VLM Computer Use 配置缺失：{','.join(missing)}"
            return ""
        missing: List[str] = []
        if not s.trajectory_cache_recovery_vlm_api_url:
            missing.append("api_url")
        if not s.trajectory_cache_recovery_vlm_api_key:
            missing.append("api_key")
        if not s.trajectory_cache_recovery_vlm_model:
            missing.append("model")
        if missing:
            return f"recovery_vlm 配置缺失：{','.join(missing)}"
        return ""

    @property
    def default_wait_ms(self) -> int:
        return int(self.settings.trajectory_cache_recovery_vlm_wait_more_ms or 1500)

    @property
    def max_wait_more(self) -> int:
        return int(self.settings.trajectory_cache_recovery_vlm_max_wait_more or 0)

    @property
    def coord_space(self) -> str:
        """根据主 VLM backend 推断 recovery 输出的坐标空间。

        覆盖范围保守：仅明确认得的海外 CU 系返回 ``absolute``；其它（豆包、
        未知 / 自部署 OpenAI 兼容代理跑豆包模型等）一律按 ``normalized`` 兜
        底，与历史豆包行为一致，不会破坏现网。
        """
        backend = self._main_vlm_backend
        if not backend:
            return "normalized"
        if backend in {"claude_cu", "gpt_cu"}:
            return "absolute"
        if "claude" in backend or backend.startswith("gpt"):
            return "absolute"
        return "normalized"

    def _use_main_executable_vlm(self) -> bool:
        backend = (
            self._main_vlm_backend
            or str(getattr(self.settings, "vlm_backend", "") or "")
        ).strip().lower()
        configured = str(getattr(self.settings, "vlm_backend", "") or "").strip().lower()
        return backend in {"claude_cu", "gpt_cu"} and configured == backend

    # ------------------------------------------------------------------
    # 主调用
    # ------------------------------------------------------------------
    async def verify_alignment_miss(
        self,
        *,
        goal: str,
        trajectory: Dict[str, Any],
        action: Dict[str, Any],
        landmark: Dict[str, Any],
        current_bytes: bytes,
        landmark_bytes: bytes,
        metrics: Dict[str, Any],
        elapsed_ms: int,
        max_wait_ms: int,
    ) -> RecoveryDecision:
        if not self.is_configured():
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason=self.configuration_problem()
                or "recovery_vlm 不可用，按保守策略终止缓存回放",
                error="not_configured",
            )

        coord_space = self.coord_space
        prompt = build_recovery_prompt(
            goal=goal,
            trajectory=trajectory,
            action=action,
            landmark=landmark,
            metrics=metrics,
            elapsed_ms=elapsed_ms,
            max_wait_ms=max_wait_ms,
            default_wait_ms=self.default_wait_ms,
            coord_space=coord_space,
        )

        if self._use_main_executable_vlm():
            return await self._decide_with_main_executable_vlm(
                prompt=prompt,
                landmark_bytes=landmark_bytes,
                current_bytes=current_bytes,
            )

        loop = asyncio.get_event_loop()
        started_at = loop.time()
        try:
            text = await asyncio.wait_for(
                self._chat_double_image(
                    prompt=prompt,
                    landmark_bytes=landmark_bytes,
                    current_bytes=current_bytes,
                ),
                timeout=float(self.settings.trajectory_cache_recovery_vlm_timeout_sec),
            )
        except asyncio.TimeoutError:
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason=(
                    f"recovery_vlm 调用超时 "
                    f"({self.settings.trajectory_cache_recovery_vlm_timeout_sec:.1f}s)，"
                    "按保守策略终止缓存回放"
                ),
                elapsed_ms=int((loop.time() - started_at) * 1000),
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason=(
                    f"recovery_vlm 调用失败：{type(exc).__name__}: {str(exc)[:160]}，"
                    "按保守策略终止缓存回放"
                ),
                elapsed_ms=int((loop.time() - started_at) * 1000),
                error=f"{type(exc).__name__}",
            )

        decision = parse_recovery_response(
            text,
            default_wait_ms=self.default_wait_ms,
            coord_space=coord_space,
        )
        decision.elapsed_ms = int((loop.time() - started_at) * 1000)
        return decision

    async def _decide_with_main_executable_vlm(
        self,
        *,
        prompt: str,
        landmark_bytes: bytes,
        current_bytes: bytes,
    ) -> RecoveryDecision:
        """海外 CU 主链路专用：使用同源 Computer Use 能力做局部恢复。

        Claude / GPT Computer Use 每轮只吃一张屏幕图。这里把“缓存 handoff
        参考图”和“当前真实截图”拼成一张诊断图，要求模型只在当前截图区域
        操作；返回后再把坐标投回当前截图坐标，交给 replay runner 按 absolute
        分支缩放到设备坐标。
        """
        canvas = _build_labeled_image_canvas(
            [
                ("cached handoff reference - do not operate here", landmark_bytes),
                ("current replay screen - operate only here", current_bytes),
            ]
        )
        current_pane = canvas.panes[1]
        system_prompt = (
            "You are the trajectory replay recovery VLM for a real mobile device.\n"
            "The screenshot is a diagnostic canvas with two panes. The left pane is "
            "only a cached reference. The right pane is the current live phone screen.\n"
            "If you need to perform a UI action, use the computer tool on the RIGHT "
            "current-screen pane only. Never click the left reference pane.\n"
            "If the replay can continue, do not use the computer tool; answer with "
            "FINISHED: <reason>. If it cannot recover, answer ASSERT_FAIL: <reason>.\n\n"
            "Important: the policy text below is shared with non-Computer-Use backends "
            "and may mention Thought/Action text DSL. For this Computer Use call, ignore "
            "that output-format section. Use the computer tool for repair actions, or "
            "FINISHED / ASSERT_FAIL text for terminal decisions.\n\n"
            + prompt
        )
        loop = asyncio.get_event_loop()
        started_at = loop.time()
        try:
            if self._main_vlm_backend == "gpt_cu":
                from ai_phone.shared.llm.main.gpt_cu import GPTComputerUseClient

                client = GPTComputerUseClient(
                    system_prompt=system_prompt,
                    api_url=self.settings.vlm_api_url,
                    api_key=self.settings.vlm_api_key,
                    model=self.settings.vlm_model,
                    timeout_seconds=float(self.settings.trajectory_cache_recovery_vlm_timeout_sec),
                )
            else:
                from ai_phone.shared.llm.main.claude_cu import ClaudeComputerUseClient

                client = ClaudeComputerUseClient(
                    system_prompt=system_prompt,
                    api_url=self.settings.vlm_api_url,
                    api_key=self.settings.vlm_api_key,
                    model=self.settings.vlm_model,
                    timeout_seconds=float(self.settings.trajectory_cache_recovery_vlm_timeout_sec),
                )
            model_decision = await asyncio.wait_for(
                client.decide(canvas.bytes),
                timeout=float(self.settings.trajectory_cache_recovery_vlm_timeout_sec),
            )
        except asyncio.TimeoutError:
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason="recovery_vlm 主 VLM Computer Use 调用超时，按保守策略终止缓存回放",
                elapsed_ms=int((loop.time() - started_at) * 1000),
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason=(
                    f"recovery_vlm 主 VLM Computer Use 调用失败："
                    f"{type(exc).__name__}: {str(exc)[:160]}"
                ),
                elapsed_ms=int((loop.time() - started_at) * 1000),
                error=type(exc).__name__,
            )

        out = _recovery_decision_from_main_vlm_decision(
            model_decision,
            current_pane=current_pane,
        )
        out.elapsed_ms = int((loop.time() - started_at) * 1000)
        return out

    # ------------------------------------------------------------------
    # VLM 协议后端
    # ------------------------------------------------------------------
    async def _chat_double_image(
        self,
        *,
        prompt: str,
        landmark_bytes: bytes,
        current_bytes: bytes,
    ) -> str:
        s = self.settings
        backend = (s.trajectory_cache_recovery_vlm_backend or "openai_compatible").strip().lower()
        if backend == "openai_compatible":
            return await self._chat_completions_double_image(
                prompt=prompt,
                landmark_bytes=landmark_bytes,
                current_bytes=current_bytes,
            )
        if backend == "doubao_responses":
            return await self._responses_double_image(
                prompt=prompt,
                landmark_bytes=landmark_bytes,
                current_bytes=current_bytes,
            )
        if backend == "claude_messages":
            return await self._messages_double_image(
                prompt=prompt,
                landmark_bytes=landmark_bytes,
                current_bytes=current_bytes,
            )
        raise RuntimeError(
            f"recovery_vlm 暂不支持 backend={backend}，"
            "当前支持 doubao_responses / openai_compatible / claude_messages"
        )

    # ------------------------------------------------------------------
    # OpenAI 兼容 chat completions（豆包方舟 chat 端点同协议）
    # ------------------------------------------------------------------
    async def _chat_completions_double_image(
        self,
        *,
        prompt: str,
        landmark_bytes: bytes,
        current_bytes: bytes,
    ) -> str:
        s = self.settings
        landmark_b64 = base64.b64encode(landmark_bytes).decode("ascii")
        current_b64 = base64.b64encode(current_bytes).decode("ascii")
        user_content: List[Dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{landmark_b64}"},
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{current_b64}"},
            },
        ]
        payload: Dict[str, Any] = {
            "model": s.trajectory_cache_recovery_vlm_model,
            "temperature": 0,
            "top_p": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是轨迹缓存回放的局部恢复 VLM。"
                        "必须使用 Thought/Action 格式输出一个局部恢复动作、"
                        "wait、finished 或 assert_fail。"
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        }
        # thinking 是豆包方舟 chat API 特有字段；OpenAI 端会忽略未知字段，无副作用。
        payload["thinking"] = {"type": "enabled"}
        headers = {
            "Authorization": f"Bearer {s.trajectory_cache_recovery_vlm_api_key}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(
            float(s.trajectory_cache_recovery_vlm_timeout_sec),
            connect=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                s.trajectory_cache_recovery_vlm_api_url,
                json=payload,
                headers=headers,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"recovery_vlm chat 失败: status={resp.status_code} "
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
            raise RuntimeError("recovery_vlm 未返回可解析文本")
        return text

    # ------------------------------------------------------------------
    # 方舟 Responses API（与主 VLM 的 doubao_responses 协议一致）
    # ------------------------------------------------------------------
    async def _responses_double_image(
        self,
        *,
        prompt: str,
        landmark_bytes: bytes,
        current_bytes: bytes,
    ) -> str:
        s = self.settings
        landmark_b64 = base64.b64encode(landmark_bytes).decode("ascii")
        current_b64 = base64.b64encode(current_bytes).decode("ascii")
        user_content: List[Dict[str, Any]] = [
            {"type": "input_text", "text": prompt},
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{landmark_b64}",
            },
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{current_b64}",
            },
        ]
        payload: Dict[str, Any] = {
            "model": s.trajectory_cache_recovery_vlm_model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "你是轨迹缓存回放的局部恢复 VLM。"
                        "必须使用 Thought/Action 格式输出一个局部恢复动作、"
                        "wait、finished 或 assert_fail。"
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "store": True,
            "caching": {"type": "enabled"},
            "thinking": {"type": "disabled"},
        }
        headers = {
            "Authorization": f"Bearer {s.trajectory_cache_recovery_vlm_api_key}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(
            float(s.trajectory_cache_recovery_vlm_timeout_sec),
            connect=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                s.trajectory_cache_recovery_vlm_api_url,
                json=payload,
                headers=headers,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"recovery_vlm responses 失败: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        text = _extract_responses_text(resp.json())
        if not text:
            raise RuntimeError("recovery_vlm 未返回可解析文本")
        return text

    # ------------------------------------------------------------------
    # Anthropic Messages API（Claude 主 VLM 用户的 recovery 通道）
    # ------------------------------------------------------------------
    async def _messages_double_image(
        self,
        *,
        prompt: str,
        landmark_bytes: bytes,
        current_bytes: bytes,
    ) -> str:
        """走 Anthropic /v1/messages 协议做双图 recovery 调用。

        与 chat completions / responses 三大差异（避免误用其它 backend）：
        1. 鉴权头是 ``x-api-key`` + ``anthropic-version``，不是 Bearer。
        2. 多模态 user content 用 ``{"type":"image","source":{"type":"base64",
           "media_type":"image/jpeg","data":<b64>}}``，不是 ``image_url``。
        3. 响应在 ``data.content`` 数组里，每块 ``{"type":"text","text":...}``
           或 ``{"type":"thinking","text":...}``；要拼所有 text 块。

        recovery 通道有意不开 thinking / 不开 tools——它只需要模型按 prompt 给
        Thought/Action 文本，越简单越稳。"""
        s = self.settings
        landmark_b64 = base64.b64encode(landmark_bytes).decode("ascii")
        current_b64 = base64.b64encode(current_bytes).decode("ascii")
        user_content: List[Dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": landmark_b64,
                },
            },
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": current_b64,
                },
            },
        ]
        payload: Dict[str, Any] = {
            "model": s.trajectory_cache_recovery_vlm_model,
            "max_tokens": 1024,
            "system": (
                "你是轨迹缓存回放的局部恢复 VLM。"
                "必须使用 Thought/Action 格式输出一个局部恢复动作、"
                "wait、finished 或 assert_fail。"
            ),
            "messages": [{"role": "user", "content": user_content}],
        }
        headers = {
            "x-api-key": s.trajectory_cache_recovery_vlm_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(
            float(s.trajectory_cache_recovery_vlm_timeout_sec),
            connect=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                s.trajectory_cache_recovery_vlm_api_url,
                json=payload,
                headers=headers,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"recovery_vlm messages 失败: status={resp.status_code} "
                f"body={resp.text[:200]}"
            )
        text = _extract_messages_text(resp.json())
        if not text:
            raise RuntimeError("recovery_vlm 未返回可解析文本")
        return text


def _extract_messages_text(data: Dict[str, Any]) -> str:
    """从 Anthropic Messages 响应中提取所有 text 块拼接后的文本。

    Claude 响应：``{"content": [{"type":"text","text":"..."}, {"type":"thinking",
    "text":"..."}, ...]}``。recovery 不开 thinking 时只会有 text 块；万一有
    thinking 也一起拼，由后续 _strip_markdown_decorations + parse 兜底处理。
    """
    pieces: List[str] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("text", "thinking"):
            t = block.get("text")
            if isinstance(t, str) and t:
                pieces.append(t)
    return "\n".join(pieces).strip()


def _extract_responses_text(data: Dict[str, Any]) -> str:
    """从 Responses API 返回中提取文本，兼容方舟/OpenAI 常见形态。"""
    texts: List[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                texts.append(content["text"])
    if not texts and isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])
    return "\n".join(t.strip() for t in texts if t and t.strip()).strip()


def parse_recovery_response(
    text: str,
    *,
    default_wait_ms: int = 1500,
    coord_space: str = "normalized",
) -> RecoveryDecision:
    """recovery VLM 输出解析。

    doubao 系新协议复用主 VLM DSL：``Thought: ...`` + ``Action: ...``。
    为兼容旧测试和灰度现场，也保留旧三态纯文本协议：

        CONTINUE_REPLAY: <一句话原因>
        WAIT_MORE: <ms>: <一句话原因>
        ASSERT_FAIL: <一句话原因>

    **跨 backend 兼容**：claude / gpt 系模型偶发会用 markdown 装饰 ``Thought:``
    / ``Action:`` 行（如 ``**Action:**``、``` `Action:` ```、```` ```python ```` 包
    起来），这些会让 ``A.extract_actions`` 的行首正则失配。入口先做一次预清洗
    剥掉装饰，再走原解析路径，保证三家 backend 解析行为一致。
    """
    raw_original = text or ""
    raw = _strip_markdown_decorations(raw_original)
    action_texts = A.extract_actions(raw) if "Action:" in raw else []
    if action_texts:
        parsed_actions = [A.parse_action(item) for item in action_texts]
        # 关键：A.parse_action 默认输出 coord_space="normalized"（豆包系约定）。
        # recovery 走海外 backend（claude_cu / gpt_cu）时模型按 prompt 指令输出
        # 的是图像绝对像素，必须显式覆写为 "absolute"，否则下游
        # ReplayRunner._parsed_point_to_abs 会把它当 0-1000 反算，坐标全错。
        normalized_coord_space = (coord_space or "normalized").strip().lower()
        if normalized_coord_space not in ("normalized", "absolute"):
            normalized_coord_space = "normalized"
        for pa in parsed_actions:
            pa.coord_space = normalized_coord_space
        parsed = parsed_actions[0]
        thought = A.extract_thought(raw)
        reason = parsed.content or thought or parsed.raw or ""
        if parsed.action == A.ACTION_FINISHED:
            return RecoveryDecision(
                verdict=VERDICT_CONTINUE,
                reason=reason or "recovery_vlm 判断当前差异可接受，继续回放",
                raw=raw,
                thought=thought,
                action_text=action_texts[0],
                parsed_actions=parsed_actions,
            )
        if parsed.action == A.ACTION_WAIT:
            wait_ms = max(100, min(10_000, int(parsed.seconds or 1) * 1000))
            return RecoveryDecision(
                verdict=VERDICT_WAIT_MORE,
                reason=reason or "recovery_vlm 判断页面仍可能在加载",
                wait_ms=wait_ms,
                raw=raw,
                thought=thought,
                action_text=action_texts[0],
                parsed_actions=parsed_actions,
            )
        if parsed.action == A.ACTION_ASSERT_FAIL:
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason=reason or "recovery_vlm 判断轨迹已偏航或功能不可达",
                raw=raw,
                thought=thought,
                action_text=action_texts[0],
                parsed_actions=parsed_actions,
            )
        if parsed.is_known:
            return RecoveryDecision(
                verdict=VERDICT_REPAIR_ACTION,
                reason=thought or f"执行局部修复动作 {parsed.action}",
                raw=raw,
                thought=thought,
                action_text=action_texts[0],
                parsed_actions=parsed_actions[:1],
            )
        return RecoveryDecision(
            verdict=VERDICT_ASSERT_FAIL,
            reason=f"recovery_vlm 输出未知动作：{parsed.action}",
            raw=raw,
            thought=thought,
            action_text=action_texts[0],
            parsed_actions=parsed_actions,
            error="unknown_action",
        )

    first_line = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    upper = first_line.upper()

    if upper.startswith(VERDICT_CONTINUE + ":") or upper == VERDICT_CONTINUE:
        reason = _split_after_colon(first_line) or "VLM 判断当前差异可接受"
        return RecoveryDecision(
            verdict=VERDICT_CONTINUE,
            reason=reason,
            raw=raw,
        )

    if upper.startswith(VERDICT_ASSERT_FAIL + ":") or upper == VERDICT_ASSERT_FAIL:
        reason = _split_after_colon(first_line) or "VLM 判断轨迹已偏航"
        return RecoveryDecision(
            verdict=VERDICT_ASSERT_FAIL,
            reason=reason,
            raw=raw,
        )

    if upper.startswith(VERDICT_WAIT_MORE + ":") or upper == VERDICT_WAIT_MORE:
        rest = _split_after_colon(first_line)
        wait_ms = default_wait_ms
        reason = "VLM 判断页面仍可能在加载"
        match = re.match(
            r"\s*(\d{2,6})\s*(?:ms|毫秒)?\s*[:：,，\-]?\s*(.*)",
            rest,
        )
        if match:
            try:
                wait_ms = int(match.group(1))
            except (TypeError, ValueError):
                wait_ms = default_wait_ms
            tail = match.group(2).strip()
            if tail:
                reason = tail
        elif rest:
            reason = rest
        wait_ms = max(100, min(10_000, wait_ms))
        return RecoveryDecision(
            verdict=VERDICT_WAIT_MORE,
            reason=reason,
            wait_ms=wait_ms,
            raw=raw,
        )

    return RecoveryDecision(
        verdict=VERDICT_ASSERT_FAIL,
        reason=f"recovery_vlm 返回非协议内容：{first_line[:80] or '(空)'}",
        raw=raw,
        error="protocol_violation",
    )


def _split_after_colon(line: str) -> str:
    if ":" not in line:
        return ""
    return line.split(":", 1)[1].strip()


# claude / gpt 偶发把 Thought / Action 行用 markdown 装饰起来，例如：
#
#     **Thought:** I need to wait...
#     `Action:` click(point='<point>540 1024</point>')
#     ```python
#     Action: click(point='<point>540 1024</point>')
#     ```
#
# 这些会让下游的 ``A.extract_actions`` / ``A.extract_thought`` 行首正则失配。
# 入口先做一次"去装饰"，再走原解析路径，三家 backend 行为对齐。
_MD_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*$", re.MULTILINE)
_MD_BOLD_KEYWORD_RE = re.compile(r"\*\*\s*(Thought|Action)\s*:?\s*\*\*", re.IGNORECASE)
_MD_INLINE_CODE_KEYWORD_RE = re.compile(r"`\s*(Thought|Action)\s*:?\s*`", re.IGNORECASE)
_MD_LIST_PREFIX_RE = re.compile(
    r"^[ \t]*[-*+][ \t]+(?=(?:\*\*)?(?:Thought|Action)\b[: ])",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_markdown_decorations(text: str) -> str:
    """剥掉 Thought / Action 行常见的 markdown 装饰，让行首关键字裸露出来。

    保守原则：只动 ``Thought:`` / ``Action:`` 关键字附近的装饰，**不**动 Action
    参数体内部的反引号 / 引号等（豆包系 ``content='...'`` 字面量也可能含
    反引号）。fence 行整行剔除（不删 fence 之间的 Action 内容，因为 Action 解
    析器只需要看到一行裸的 ``Action: <call>``）。
    """
    if not text:
        return text
    out = text
    out = _MD_BOLD_KEYWORD_RE.sub(lambda m: f"{m.group(1)}:", out)
    out = _MD_INLINE_CODE_KEYWORD_RE.sub(lambda m: f"{m.group(1)}:", out)
    out = _MD_LIST_PREFIX_RE.sub("", out)
    out = _MD_FENCE_RE.sub("", out)
    return out


class CanvasResult:
    def __init__(
        self,
        *,
        bytes_: bytes,
        panes: List[Tuple[int, int, int, int]],
    ) -> None:
        self.bytes = bytes_
        self.panes = panes


def _build_labeled_image_canvas(
    items: List[Tuple[str, bytes]],
    *,
    gap: int = 16,
    label_h: int = 34,
) -> CanvasResult:
    images: List[Image.Image] = []
    for _label, data in items:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        images.append(img)
    total_w = sum(img.width for img in images) + gap * (len(images) - 1)
    max_h = max((img.height for img in images), default=1)
    canvas = Image.new("RGB", (total_w, max_h + label_h), "white")
    draw = ImageDraw.Draw(canvas)
    panes: List[Tuple[int, int, int, int]] = []
    x = 0
    for (label, _data), img in zip(items, images):
        draw.rectangle([x, 0, x + img.width - 1, label_h - 1], fill=(238, 242, 247))
        draw.text((x + 8, 9), label, fill=(20, 24, 31))
        canvas.paste(img, (x, label_h))
        panes.append((x, label_h, img.width, img.height))
        x += img.width + gap
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return CanvasResult(bytes_=buf.getvalue(), panes=panes)


def _project_parsed_action_to_pane(
    parsed: A.ParsedAction,
    *,
    pane: Tuple[int, int, int, int],
) -> Optional[A.ParsedAction]:
    x0, y0, w, h = pane

    def project(point: Optional[List[int]]) -> Optional[List[int]]:
        if point is None:
            return None
        px = int(point[0]) - x0
        py = int(point[1]) - y0
        if px < 0 or py < 0 or px >= w or py >= h:
            return None
        return [max(0, min(px, w - 1)), max(0, min(py, h - 1))]

    point = project(parsed.point)
    start = project(parsed.start_point)
    end = project(parsed.end_point)
    if parsed.point is not None and point is None:
        return None
    if parsed.start_point is not None and start is None:
        return None
    if parsed.end_point is not None and end is None:
        return None
    return replace(
        parsed,
        point=point,
        start_point=start,
        end_point=end,
        coord_space="absolute",
    )


def _recovery_decision_from_main_vlm_decision(
    model_decision: Any,
    *,
    current_pane: Tuple[int, int, int, int],
) -> RecoveryDecision:
    parsed_actions = list(getattr(model_decision, "parsed_actions", None) or [])
    thought = str(getattr(model_decision, "thought", "") or "")
    raw = str(getattr(model_decision, "raw_content", "") or "")
    if not parsed_actions:
        return RecoveryDecision(
            verdict=VERDICT_ASSERT_FAIL,
            reason="主 VLM Computer Use 未返回可解析动作",
            raw=raw,
            thought=thought,
            error="empty_action",
        )
    parsed = parsed_actions[0]
    reason = parsed.content or thought or parsed.raw or ""
    if parsed.action == A.ACTION_FINISHED:
        return RecoveryDecision(
            verdict=VERDICT_CONTINUE,
            reason=reason or "主 VLM Computer Use 判断当前差异可接受，继续回放",
            raw=raw,
            thought=thought,
            action_text=parsed.raw,
            parsed_actions=[parsed],
        )
    if parsed.action == A.ACTION_WAIT:
        return RecoveryDecision(
            verdict=VERDICT_WAIT_MORE,
            reason=reason or "主 VLM Computer Use 判断页面仍可能在加载",
            wait_ms=max(100, min(10_000, int(parsed.seconds or 1) * 1000)),
            raw=raw,
            thought=thought,
            action_text=parsed.raw,
            parsed_actions=[parsed],
        )
    if parsed.action == A.ACTION_ASSERT_FAIL:
        return RecoveryDecision(
            verdict=VERDICT_ASSERT_FAIL,
            reason=reason or "主 VLM Computer Use 判断轨迹已偏航或功能不可达",
            raw=raw,
            thought=thought,
            action_text=parsed.raw,
            parsed_actions=[parsed],
        )
    if parsed.is_known:
        projected = _project_parsed_action_to_pane(parsed, pane=current_pane)
        if projected is None:
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason="主 VLM Computer Use 返回的动作坐标不在当前截图区域",
                raw=raw,
                thought=thought,
                action_text=parsed.raw,
                parsed_actions=[parsed],
                error="point_outside_current_pane",
            )
        return RecoveryDecision(
            verdict=VERDICT_REPAIR_ACTION,
            reason=thought or f"执行局部修复动作 {projected.action}",
            raw=raw,
            thought=thought,
            action_text=projected.raw,
            parsed_actions=[projected],
        )
    return RecoveryDecision(
        verdict=VERDICT_ASSERT_FAIL,
        reason=f"主 VLM Computer Use 输出未知动作：{parsed.action}",
        raw=raw,
        thought=thought,
        action_text=parsed.raw,
        parsed_actions=parsed_actions,
        error="unknown_action",
    )


def build_recovery_prompt(
    *,
    goal: str,
    trajectory: Dict[str, Any],
    action: Dict[str, Any],
    landmark: Dict[str, Any],
    metrics: Dict[str, Any],
    elapsed_ms: int,
    max_wait_ms: int,
    default_wait_ms: int,
    coord_space: str = "normalized",
) -> str:
    # 坐标系说明跟随主 VLM backend：豆包 normalized；claude_cu / gpt_cu absolute。
    # 海外 CU 系训练就是按图像像素输出坐标的，强行让它们输出 0-1000 归一化反而
    # 不准；豆包系按 prompt 指令稳定输出 0-1000，所以两类各按各家训练习惯走。
    norm_cs = (coord_space or "normalized").strip().lower()
    if norm_cs == "absolute":
        coord_block = (
            "【坐标系说明】\n"
            "<point>x y</point> 中的 x y 必须是相对【附图 2（当前截图）】的"
            "整数像素绝对坐标，原点在图片左上角，向右为 x、向下为 y。\n"
            "**禁止**输出 0-1000 归一化坐标。坐标超出图像范围属于错误输出。\n\n"
        )
    else:
        coord_block = (
            "【坐标系说明】\n"
            "<point>x y</point> 中的 x y 是 0-1000 归一化坐标，"
            "0/0 表示左上角，1000/1000 表示右下角，相对【附图 2（当前截图）】。\n\n"
        )
    return (
        "你是轨迹缓存回放的局部恢复 VLM。\n"
        "系统正在执行一次缓存轨迹回放，并已经定位到一个明确问题：\n"
        "执行缓存 action_i 后，当前页面截图没有对齐首次成功轨迹中 action_i 对应的 handoff 页面。\n\n"
        "handoff 页面的含义：\n"
        "它不是 action_i 刚执行后的瞬间截图，而是首次成功执行中，action_i 完成后，"
        "系统准备执行 action_{i+1} 前的页面状态。\n"
        "也就是说，它代表“当前步骤完成，并且后续缓存 action 可以继续衔接”的状态。\n\n"
        "你的任务不是重新执行完整 case。\n"
        "你的任务是判断并处理当前 action_i 的 handoff 偏差，让缓存回放尽可能继续。\n\n"
        "本提示词附带两张图（按消息顺序）：\n"
        "- 附图 1：首次成功轨迹中，当前 action 完成后的 handoff 状态路标图。\n"
        "- 附图 2：本次缓存回放执行同一 action 并按历史等待窗口重试后的当前截图。\n\n"
        "你必须按以下优先级决策，不要跳级：\n"
        "1. 先判断当前 action_i 的语义目标是否仍然可达。\n"
        "   - 如果目标控件、入口、输入框、列表项、开关、菜单项等仍然可见，"
        "或可通过少量操作暴露出来，必须优先输出一个局部修复 action。\n"
        "   - 常见可修复原因包括：缓存坐标点到了相邻控件、同组导航/列表/菜单位置发生偏移、"
        "页面滚动位置不同、布局轻微变化、弹窗/浮层/键盘/遮罩阻挡、当前 action_i 没有真正触发结果。\n"
        "   - 这些属于“缓存坐标或起跑状态失配”，不等于功能不可用，不能直接 assert_fail。\n"
        "2. 如果当前 action_i 是本次缓存回放的第一步，并且当前页面不像缓存轨迹的起跑状态：\n"
        "   - 先尝试用少量 action 回到与缓存起跑状态等价的页面，再重新执行 action_i。\n"
        "   - 可用方式包括返回、关闭阻挡层、切回正确区域、滚动/切换到 action_i 的目标可操作位置、"
        "打开目标应用或回到目标页面。\n"
        "   - 起跑线不同是缓存回放常见问题，不能因为第一步 handoff 不一致就立刻 assert_fail。\n"
        "   - 只有当无法判断如何回到起跑状态，或回到起跑状态后仍无法执行 action_i，才 assert_fail。\n"
        "3. 如果当前 action_i 已经达成，且当前页面已经满足 handoff 语义：\n"
        "   - 输出 finished(content='放行原因')，表示放行继续缓存回放。\n"
        "   - finished 只表示“当前 handoff 可衔接”，不是完成整个用户 goal。\n"
        "4. 如果附图 1（handoff 路标图）本身就是加载中、进度条、骨架屏、动画、"
        "跳转、刷新、数据渲染、异步请求等过渡态：\n"
        "   - 这类 handoff 图不适合做严格像素对齐，因为进度、文案、题目内容、"
        "资源位、动画帧都可能天然变化。\n"
        "   - 此时不要继续要求附图 2 与附图 1 完全一致；应降级判断附图 2 的"
        "当前页面状态是否仍可衔接下一条缓存 action。\n"
        "   - 如果附图 2 也是同类加载/过渡态，或已经进入比附图 1 更靠后的可衔接页面，"
        "输出 finished(content='放行原因')，让系统继续执行下一条缓存 action。\n"
        "   - 特别是下一条缓存 action 很可能就是 wait(seconds=N)：这时必须优先"
        "finished 放行，让缓存里的 wait 自己执行；不要在当前 recovery 中再输出 wait。\n"
        "   - 只有当附图 2 明显跑到无关页面、错误页面、或无法衔接下一条缓存 action 时，"
        "才按修复 action 或 assert_fail 处理。\n"
        "5. 如果附图 1 是稳定业务页面，但附图 2 像是在加载、动画、跳转、刷新、"
        "数据渲染、异步请求中：\n"
        "   - 输出 wait(seconds=N)，等待后系统会重新截图并再次对齐。\n"
        "6. 只有在以下情况下才输出 assert_fail：\n"
        "   - action_i 的目标功能/入口/控件确实不存在；\n"
        "   - 当前页面和本 case 路径明显无关，无法通过少量操作恢复；\n"
        "   - 目标被阻挡且无法处理；\n"
        "   - 业务功能本身确实失败，例如点击无反应、页面不进入、结果不可达；\n"
        "   - 已尝试修复但仍无法回到 handoff 语义；\n"
        "   - 无法判断当前差异原因，也无法确定任何安全的局部恢复动作。\n\n"
        "特别注意：\n"
        "- 缓存 action 的坐标可能因为起跑状态、页面滚动位置、同组控件位置、"
        "横向/纵向列表位置、布局变化而失效。\n"
        "- 坐标失效不等于 case 失败。\n"
        "- 如果 action_i 的语义目标仍可达，你应该重新定位目标并输出一个修复 action。\n"
        "- 当 action_i 执行后进入了错误但相近的页面时，优先尝试把当前页面修复到 "
        "action_i 应达成的 handoff 状态；不要因为当前页面暂时不等于 handoff 就直接判定后续无法衔接。\n\n"
        "稳定上下文优先级很高：\n"
        "凡是在 handoff 图中稳定呈现、并可能影响后续 action 衔接或最终断言的页面状态，"
        "都属于严格对齐范围。\n"
        "如果这类稳定上下文与 handoff 图明显不同，不能仅因为下一 action 的控件还可见就 finished 放行。\n"
        "判断是否可忽略时，必须以用户 goal 的明确语义为准；goal 没有明确允许忽略，就按严格处理。\n"
        "如果你不能确定两图为什么不一致，也不能确定该差异不影响后续衔接或最终断言，"
        "禁止 finished 放行。\n"
        "这类情况应优先尝试用局部 action 修复；无法修复或无法解释差异时输出 assert_fail。\n\n"
        "动态内容注意：\n"
        "实时画面、视频/直播帧、动画、倒计时、价格、运营资源位、推荐列表、轮播图、加载骨架、临时状态等，"
        "可能导致截图差异很大。这些差异不一定代表轨迹偏航。\n"
        "你必须优先判断页面结构、页面层级、关键控件、当前 action_i 的业务结果、以及 action_{i+1} 是否仍可执行。\n"
        "如果只是动态内容变化，且稳定上下文一致、后续 action 仍可执行，应输出 finished 放行。\n"
        "但不要把稳定上下文变化误判为动态内容。\n"
        "如果动态内容遮挡或隐藏了后续控件，但可以通过少量 action 恢复，应执行修复 action。\n\n"
        "输出格式必须与主执行 VLM 保持一致，不要输出 JSON：\n"
        "Thought: <中文描述当前画面分析与局部恢复计划>\n"
        "Action: <一个动作调用>\n\n"
        "Action 行只能写一个动作调用，禁止尾部加注释 / 装饰；解释一律写到 Thought。\n"
        "**严格约束（针对 Claude / GPT 系模型常见误用）**：\n"
        "- 禁止用 markdown 加粗（如 **Action:**、**Thought:**），直接写纯文本前缀。\n"
        "- 禁止用反引号或代码块（` 或 ```python ... ```）包装 Action 行；\n"
        "  Action 行必须是裸文本，例如：``Action: click(point='<point>540 1024</point>')``。\n"
        "- 禁止把 Thought / Action 写在 JSON / YAML / list 里。\n"
        "- 动作名（click / type / scroll / wait / finished / assert_fail 等）保持英文。\n\n"
        + coord_block +
        "可用动作：\n"
        "1. click(point='<point>x y</point>')\n"
        "2. long_press(point='<point>x y</point>')\n"
        "3. type(content='文本')\n"
        "4. scroll(point='<point>x y</point>', direction='up|down|left|right')\n"
        "5. drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')\n"
        "6. open_app(app_name='应用名') / close_app(name='应用名')\n"
        "7. press_home() / press_back()\n"
        "8. double_tap(point='<point>x y</point>')\n"
        "9. wait(seconds=N)\n"
        "10. finished(content='放行原因')\n"
        "11. assert_fail(content='失败原因')\n\n"
        "输出含义：\n"
        "- finished(content=...)：当前差异可接受，缓存回放可以继续。\n"
        "- wait(seconds=...)：页面可能还在加载或过渡，等待后重新截图对比。\n"
        "- assert_fail(content=...)：功能不可用、路径偏离过大、case 不健康、或无法恢复。\n"
        "- 其他 action：执行一个局部修复动作。系统会执行后重新截图，并再次与 handoff 页面比对。\n\n"
        "重要约束：\n"
        "- 不要重跑完整 case。\n"
        "- 不要为了完成最终 goal 而跳过缓存轨迹。\n"
        "- 不要连续规划很多步；每次只输出一个最合适的 action 或终止动作。\n"
        "- 当前目标只是恢复到“action_i 已完成，并且 action_{i+1} 可以继续衔接”的状态。\n"
        "- 只有当你能说明当前页面已经满足 handoff 语义，且差异不影响后续衔接或最终断言时，才可 finished 放行。\n"
        "- 如果你判断当前页面无法恢复到可衔接状态，应 assert_fail。\n\n"
        f"【用户目标】\n{goal.strip() or '(无)'}\n\n"
        f"【当前 action】\n{_compact(action)}\n\n"
        f"【当前 landmark】\n{_compact(landmark)}\n\n"
        f"【对齐指标】\n{_compact(metrics)}\n"
        f"elapsed_ms={elapsed_ms}, max_wait_ms={max_wait_ms}\n\n"
        f"【缓存动作摘要】\n{_format_actions(trajectory)}\n"
    )


def _format_actions(trajectory: Dict[str, Any]) -> str:
    actions = list(trajectory.get("actions") or [])
    if not actions:
        return "(无)"
    lines = []
    for item in actions[-12:]:
        action_id = item.get("action_id") or item.get("index")
        action_type = item.get("type")
        intent = item.get("intent") or item.get("label") or ""
        lines.append(f"{action_id}: type={action_type} intent={intent}")
    return "\n".join(lines)


def _compact(value: Any) -> str:
    text = str(value)
    return text[:3000]


__all__ = [
    "CacheReplayRecoveryVerifier",
    "RecoveryDecision",
    "VERDICT_CONTINUE",
    "VERDICT_WAIT_MORE",
    "VERDICT_ASSERT_FAIL",
    "VERDICT_REPAIR_ACTION",
    "_build_labeled_image_canvas",
    "_project_parsed_action_to_pane",
    "build_recovery_prompt",
    "parse_recovery_response",
]
