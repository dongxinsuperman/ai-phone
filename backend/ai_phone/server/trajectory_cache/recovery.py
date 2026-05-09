"""轨迹缓存状态路标 MISS 后的独立 VLM 局部恢复。

通道独立：与辅助系统 / 断言系统 / 主 VLMRunner 完全分离。配置只读
``trajectory_cache_recovery_vlm_*``，不 fallback 任何其他通道，防止把
recovery 决策耦合进辅助系统的 token / 限流统计。

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
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

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

    def __init__(self, *, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # 配置 / 可用性
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        s = self.settings
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

        prompt = build_recovery_prompt(
            goal=goal,
            trajectory=trajectory,
            action=action,
            landmark=landmark,
            metrics=metrics,
            elapsed_ms=elapsed_ms,
            max_wait_ms=max_wait_ms,
            default_wait_ms=self.default_wait_ms,
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

        decision = parse_recovery_response(text, default_wait_ms=self.default_wait_ms)
        decision.elapsed_ms = int((loop.time() - started_at) * 1000)
        return decision

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
        raise RuntimeError(
            f"recovery_vlm 暂不支持 backend={backend}，"
            "当前支持 openai_compatible / doubao_responses"
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
) -> RecoveryDecision:
    """recovery VLM 输出解析。

    doubao 系新协议复用主 VLM DSL：``Thought: ...`` + ``Action: ...``。
    为兼容旧测试和灰度现场，也保留旧三态纯文本协议：

        CONTINUE_REPLAY: <一句话原因>
        WAIT_MORE: <ms>: <一句话原因>
        ASSERT_FAIL: <一句话原因>
    """
    raw = text or ""
    action_texts = A.extract_actions(raw) if "Action:" in raw else []
    if action_texts:
        parsed_actions = [A.parse_action(item) for item in action_texts]
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
) -> str:
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
        "你必须优先判断 goal 对应的功能是否仍然可用：\n"
        "- 如果功能确实不可用、入口不存在、点击无反应、业务结果无法达成，输出 assert_fail。\n"
        "- 如果功能仍然可用，只是控件位置、文案、布局发生小范围变化，应优先尝试修复，而不是失败。\n"
        "- 如果页面仍在同一业务路径，且业务上下文与 handoff 语义一致、后续缓存 action 仍可衔接，输出 finished 表示放行继续回放。\n"
        "- 如果页面还在加载或过渡中，输出 wait。\n"
        "- 如果当前页面被弹窗、浮层、权限框、遮罩、键盘、临时提示等阻挡，可以用合适的 action 处理。\n"
        "- 如果当前 action_i 没有真正触发结果，可以重新执行或调整 action_i。\n"
        "- 如果当前页面进入了错误页面，可以尝试返回或用少量动作恢复。\n"
        "- 如果当前是第一步失败，且起跑页面不符合缓存轨迹，可以尝试恢复到可执行 action_i 的起点。\n"
        "- 如果偏离过大、无法判断如何恢复、或者修复后仍无法衔接后续缓存 action，输出 assert_fail。\n\n"
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
    "build_recovery_prompt",
    "parse_recovery_response",
]
