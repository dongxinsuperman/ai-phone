"""缓存回放通道的最终断言器。

缓存回放没有主 VLM 的逐步感知，所以最终必须重新截图验收。本模块只负责
“截图 + goal + replay 摘要 -> PASS/FAIL/SKIP”裁决，不执行动作、不查缓存。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai_phone.agent.runner.vlm_loop import (
    STRUCTURED_ASSERTION_TWO_LAYER_BLOCK,
    _classify_structured_local,
    _compute_structured_signal,
)
from ai_phone.config import Settings, get_settings
from ai_phone.shared.llm import BaseAssistant, TokenCounter, create_assistant


@dataclass
class CacheAssertionResult:
    verdict: str
    reason: str
    raw: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "raw": self.raw}


class CacheReplayAssertionVerifier:
    """缓存通道最终断言。

    与 VLMRunner 的断言系统共享 assistant 协议，但不依赖 VLMRunner 实例。
    """

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        assistant: Optional[BaseAssistant] = None,
        counter: Optional[TokenCounter] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.counter = counter or TokenCounter()
        self.assistant = assistant or create_assistant(
            counter=self.counter,
            settings=self.settings,
        )

    async def verify(
        self,
        *,
        goal: str,
        final_bytes: bytes,
        trajectory: Optional[Dict[str, Any]] = None,
        prev_before_bytes: Optional[bytes] = None,
    ) -> CacheAssertionResult:
        if not final_bytes:
            return CacheAssertionResult("FAIL", "缓存断言缺少最终截图")
        if not self._configured():
            return CacheAssertionResult("SKIP", "断言系统配置缺失，缓存通道不能确认成功")

        prompt = build_cache_assertion_prompt(
            goal=goal,
            trajectory=trajectory or {},
            has_prev=prev_before_bytes is not None,
            is_structured=is_structured_goal(goal),
        )
        try:
            text = await asyncio.wait_for(
                self.assistant.verify_finished(
                    prompt=prompt,
                    prev_before_bytes=prev_before_bytes,
                    final_bytes=final_bytes,
                    thinking=self.settings.assistant_thinking_assertion,
                ),
                timeout=self.settings.assertion_timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            return CacheAssertionResult(
                "SKIP",
                f"断言系统调用失败，缓存通道不能确认成功：{exc}",
            )

        return parse_cache_assertion_response(text)

    def _configured(self) -> bool:
        return bool(
            (self.settings.assistant_api_key or self.settings.vlm_api_key)
            and self.settings.assistant_api_url
            and self.settings.assistant_model
        )


def parse_cache_assertion_response(text: str) -> CacheAssertionResult:
    first_line = text.splitlines()[0].strip() if text else ""
    upper = first_line.upper()
    if upper.startswith("PASS:"):
        reason = first_line.split(":", 1)[1].strip() or "截图足以支持完成"
        return CacheAssertionResult("PASS", reason, raw=text)
    if upper.startswith("FAIL:"):
        reason = first_line.split(":", 1)[1].strip() or "截图不足以支持完成"
        return CacheAssertionResult("FAIL", reason, raw=text)
    reason = f"断言系统返回非协议内容：{first_line[:80]}"
    return CacheAssertionResult("SKIP", reason, raw=text)


def build_cache_assertion_prompt(
    *,
    goal: str,
    trajectory: Dict[str, Any],
    has_prev: bool,
    is_structured: Optional[bool] = None,
) -> str:
    img_index_intro = (
        "本提示词附带两张图（按消息顺序）：\n"
        "- 附图 1：缓存回放最后一个动作之前的画面（动作前对照帧）\n"
        "- 附图 2：当前最终落点画面（断言要验收的对象）\n"
        "两张图之间只跨越缓存回放的最后一个动作。"
    ) if has_prev else (
        "本提示词附带一张图：\n"
        "- 附图：当前最终落点画面（断言要验收的对象）\n"
        "本次没有动作前对照帧，请仅基于该图与用户目标判断。"
    )
    replay_summary = _format_replay_summary(trajectory)
    source_completion = _format_source_completion(trajectory)
    structured = is_structured_goal(goal) if is_structured is None else is_structured
    if structured:
        return (
            "你是手机自动化任务的最终断言系统，只负责裁决缓存轨迹回放后的"
            "最终页面是否满足结构化测试用例。你不能继续执行步骤，也不能输出新的"
            "动作建议。\n\n"
            f"{img_index_intro}\n\n"
            "当前是结构化测试用例。你的唯一职责：根据用户输入中的「预期结果」"
            "做最终验收。\n"
            "你只验「预期结果」，不验前置条件、不验操作过程、不验历史顺序。\n\n"
            "缓存通道说明：\n"
            "- 本次执行是历史成功轨迹的回放，不是 VLM 实时决策。\n"
            "- 你不能相信历史轨迹一定仍然正确，必须以当前截图为准。\n"
            "- replay action 摘要只用于理解本次做过什么，不能替代视觉证据。\n\n"
            "首次成功语义锚点说明：\n"
            "- 这部分来自生成缓存的成功 Run，可用于理解业务别名、页面别名和"
            "首跑对用户目标的解释；遇到口语歧义时优先采纳锚点的解释，避免"
            "因口语称呼与页面文案对不上而误判。\n"
            "- 它不能替代当前截图证据；最终仍必须由附图 2 支持。\n\n"
            f"{STRUCTURED_ASSERTION_TWO_LAYER_BLOCK}\n"
            "额外约束（缓存通道独有）：\n"
            "- 如果 replay 摘要与附图 2 明显矛盾（如摘要走完了 5 步但截图仍停在第 1 步页面），"
            "按第二层「页面归属客观事实矛盾」判 FAIL。\n"
            "- 不允许输出 UNSURE，也不允许建议继续执行；只能做最终裁决。\n\n"
            "输出协议：只输出第一行，且只能是以下两种之一：\n"
            "PASS: <一句话原因>\n"
            "FAIL: <一句话原因>\n\n"
            "FAIL 时必须明确指出：是哪一条预期结果走到了第二层、被哪个客观事实证伪——"
            "禁止以「文案不一致 / 看起来不像 / 不能 100% 确认」作为 FAIL 理由。\n\n"
            f"【用户目标】\n{goal.strip()}\n\n"
            f"【缓存回放摘要】\n{replay_summary}\n"
            f"\n\n【首次成功语义锚点】\n{source_completion}\n"
        )

    return (
        "你是手机自动化任务的最终断言系统，只负责裁决缓存轨迹回放后的"
        "最终页面是否满足用户目标。你不能继续执行步骤，也不能输出新的"
        "动作建议。\n\n"
        f"{img_index_intro}\n\n"
        "缓存通道说明：\n"
        "- 本次执行是历史成功轨迹的回放，不是 VLM 实时决策。\n"
        "- 你不能相信历史轨迹一定仍然正确，必须以当前截图为准。\n"
        "- replay action 摘要只用于理解本次做过什么，不能替代视觉证据。\n\n"
        "首次成功语义锚点说明：\n"
        "- 这部分来自生成缓存的成功 Run，可用于理解用户目标里的业务别名、"
        "页面别名和首跑对目标的解释。\n"
        "- 它不能替代当前截图证据；最终仍必须由附图 2 支持。\n"
        "- 如果用户目标存在口语歧义，应优先采用首次成功语义锚点中的解释。"
        "只要附图 2 显示的最终落点与首跑语义锚点中的目标解释一致，就不应"
        "因为页面文案未逐字等于用户目标中的口语称呼而 FAIL。\n\n"
        "裁决规则：\n"
        "1. 先从用户目标中抽取最后一个动作或最终状态，再判断附图 2 是否支持"
        "这个结果已经成立。\n"
        "2. 如果存在附图 1 / 附图 2，要优先用两图差异验证最后一个动作结果。"
        "例如返回、关闭、切换 tab、进入页面等动作，可以通过两图变化判断。\n"
        "3. 不要审查更早的过程顺序；回放摘要已经说明本次执行过哪些 action。\n"
        "4. 页面标题/模块名不必逐字等于按钮文案。只要截图显示已进入该按钮对应"
        "的结果页、功能区或目标 tab，就应 PASS。\n"
        "5. 对数值、比例、选中态、开关态、页面名称、弹窗状态等要求，"
        "必须有截图证据。\n"
        "6. 如果截图证据不足、被遮挡、页面明显不对、或 replay 摘要与"
        "截图矛盾，必须 FAIL。\n"
        "7. 不允许输出 UNSURE，也不允许建议继续执行；只能做最终裁决。\n\n"
        "输出协议：只输出第一行，且只能是以下两种之一：\n"
        "PASS: <一句话原因>\n"
        "FAIL: <一句话原因>\n\n"
        "FAIL 时必须说明是哪一条目标/预期没有被截图可靠支持。\n\n"
        f"【用户目标】\n{goal.strip()}\n\n"
        f"【缓存回放摘要】\n{replay_summary}\n"
        f"\n\n【首次成功语义锚点】\n{source_completion}\n"
    )


def _format_replay_summary(trajectory: Dict[str, Any]) -> str:
    actions = list(trajectory.get("actions") or [])
    if not actions:
        return "(无 action 摘要)"
    lines: List[str] = []
    for action in actions[-20:]:
        index = action.get("index")
        action_type = action.get("type")
        detail = _action_detail(action)
        intent = action.get("intent") or action.get("label") or ""
        intent_text = f" intent={intent}" if intent else ""
        lines.append(f"step {index}: {action_type}{intent_text}{detail}")
    if len(actions) > 20:
        lines.insert(0, f"... 前面还有 {len(actions) - 20} 个 action")
    return "\n".join(lines)


def _format_source_completion(trajectory: Dict[str, Any]) -> str:
    completion = trajectory.get("source_completion") or {}
    if not isinstance(completion, dict) or not completion:
        return "(无首跑完成语义)"
    labels = {
        "run_reason": "Run 结束原因",
        "task_done": "任务完成日志",
        "final_thought": "首跑最后思考",
        "assertion_pass": "首跑断言通过理由",
    }
    lines: List[str] = []
    for key in ("run_reason", "task_done", "final_thought", "assertion_pass"):
        value = str(completion.get(key) or "").strip()
        if value:
            lines.append(f"{labels[key]}: {value[:1200]}")
    return "\n".join(lines) if lines else "(无首跑完成语义)"


def _action_detail(action: Dict[str, Any]) -> str:
    action_type = str(action.get("type") or "")
    if action_type in {"click", "double_tap", "long_press"}:
        return f" point={action.get('point')}"
    if action_type == "type":
        return f" content={action.get('content')!r}"
    if action_type == "scroll":
        return f" direction={action.get('direction')} amount={action.get('amount')}"
    if action_type == "drag":
        return f" start={action.get('start')} end={action.get('end')}"
    if action_type in {"open_app", "close_app"}:
        return f" app={action.get('app_name') or action.get('package_name')}"
    if action_type == "wait":
        return f" seconds={action.get('seconds')}"
    if action_type == "key_event":
        return f" keycode={action.get('keycode')}"
    return ""


def is_structured_goal(goal: str) -> bool:
    signal = _compute_structured_signal(goal)
    verdict, _reason = _classify_structured_local(signal)
    return bool(verdict)


__all__ = [
    "CacheAssertionResult",
    "CacheReplayAssertionVerifier",
    "build_cache_assertion_prompt",
    "parse_cache_assertion_response",
]
