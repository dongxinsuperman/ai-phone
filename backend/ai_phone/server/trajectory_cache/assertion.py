"""缓存回放通道的最终断言器。

缓存回放没有主 VLM 的逐步感知，所以最终必须重新截图验收。本模块只负责
“截图 + goal + replay 摘要 -> PASS/FAIL/SKIP”裁决，不执行动作、不查缓存。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai_phone.agent.runner.vlm_loop import (
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
    structured = is_structured_goal(goal) if is_structured is None else is_structured
    if structured:
        return (
            "你是手机自动化任务的最终断言系统，只负责裁决缓存轨迹回放后的"
            "最终页面是否满足结构化测试用例。你不能继续执行步骤，也不能输出新的"
            "动作建议。\n\n"
            f"{img_index_intro}\n\n"
            "当前是结构化测试用例。你的唯一职责：根据用户输入中的“预期结果”"
            "做最终验收。\n\n"
            "缓存通道说明：\n"
            "- 本次执行是历史成功轨迹的回放，不是 VLM 实时决策。\n"
            "- 你不能相信历史轨迹一定仍然正确，必须以当前截图为准。\n"
            "- replay action 摘要只用于理解本次做过什么，不能替代视觉证据。\n\n"
            "裁决规则：\n"
            "1. “预期结果”是唯一验收标准，优先级最高；你必须逐条检查预期结果"
            "是否被附图 2 可靠支持。\n"
            "2. 你做的是语义验收，不是逐字匹配；允许同义表达、界面别名、"
            "常见产品话术变体。\n"
            "3. 禁止脑补：截图中证据不足、被遮挡、过小、无法可靠判断时，该条"
            "预期结果不成立。\n"
            "4. 只要有任一关键预期结果未被附图 2 可靠支持，就必须 FAIL。\n"
            "5. 如果 replay 摘要与截图明显矛盾，也必须 FAIL。\n"
            "6. 不允许输出 UNSURE，也不允许建议继续执行；只能做最终裁决。\n\n"
            "输出协议：只输出第一行，且只能是以下两种之一：\n"
            "PASS: <一句话原因>\n"
            "FAIL: <一句话原因>\n\n"
            "FAIL 时必须指出是哪一条预期结果没有被截图可靠支持。\n\n"
            f"【用户目标】\n{goal.strip()}\n\n"
            f"【缓存回放摘要】\n{replay_summary}\n"
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
