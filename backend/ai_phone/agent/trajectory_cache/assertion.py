"""缓存回放通道的最终断言器。

缓存回放没有主 VLM 的逐步感知，所以最终必须重新截图验收。本模块只负责
“截图 + goal + replay 摘要 -> PASS/FAIL/SKIP”裁决，不执行动作、不查缓存。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai_phone.agent.runner.vlm_loop import (
    STRUCT_AUDIT_HISTORY_LIMIT,
    _classify_structured_local,
    _compute_structured_signal,
)
from ai_phone.shared.llm.assertion_policy import parse_finished_verdict
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
        execution_history: Optional[List[Dict[str, Any]]] = None,
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
            execution_history=execution_history,
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
            # TimeoutError / ReadTimeout can have an empty message; keep their type
            # while preserving the existing SKIP policy.
            error_detail = type(exc).__name__
            if str(exc):
                error_detail += f": {exc}"
            return CacheAssertionResult(
                "SKIP",
                f"断言系统调用失败，缓存通道不能确认成功：{error_detail}",
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
    parsed = parse_finished_verdict(text)
    if parsed is not None:
        verdict, reason = parsed
        return CacheAssertionResult(verdict, reason, raw=text)
    reason = f"断言系统返回非协议内容：{first_line[:80]}"
    return CacheAssertionResult("SKIP", reason, raw=text)


def build_cache_assertion_prompt(
    *,
    goal: str,
    trajectory: Dict[str, Any],
    has_prev: bool,
    is_structured: Optional[bool] = None,
    execution_history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    # 裁决规则由共享 System 提供；保留各缓存版本自己的材料来源与摘要窗口。
    is_v3 = (
        execution_history is not None
        or str(trajectory.get("cache_mode") or "") == "v3"
        or trajectory.get("schema_version") == 3
    )
    img_index_intro = (
        "本提示词附带两张图（按消息顺序）：\n"
        "- 附图 1：缓存回放最后一个动作之前的画面（动作前对照帧）\n"
        "- 附图 2：当前最终落点画面（断言要验收的对象）\n"
        "两张图之间只跨越缓存回放的最后一个动作。"
    ) if has_prev else (
        "本提示词附带一张图：\n"
        "- 附图：当前最终落点画面（断言要验收的对象）\n"
        "本次没有动作前对照帧，请综合该图、缓存回放摘要与用户目标判断。"
    )
    if is_v3:
        img_index_intro = (
            "本提示词附带两张图（按消息顺序）：\n"
            "- 附图 1：最后一个缓存步骤开始前的画面（步骤前对照帧）\n"
            "- 附图 2：回放结束后的当前最终落点画面（断言要验收的对象）\n"
            "两图之间可能包含局部修复、等待、该缓存动作或跳过；"
            "必须结合本轮 Runtime 记录理解跨度，不能假定只跨一个物理动作，"
            "也不能把画面变化全部归因于原缓存动作。"
        ) if has_prev else (
            "本提示词附带一张图：\n"
            "- 附图：回放结束后的当前最终落点画面（断言要验收的对象）\n"
            "本次没有步骤前对照帧，请综合该图、本轮 Runtime 记录与用户目标判断；"
            "下文的『附图 2』均指这张唯一最终截图。"
        )
    replay_summary = (
        _format_execution_history(execution_history)
        if execution_history is not None else _format_replay_summary(
            trajectory, limit=STRUCT_AUDIT_HISTORY_LIMIT if is_v3 else 20,
        )
    )
    provenance = (
        "摘要来自本轮实际执行，包含修复、等待与跳过；skipped 表示未执行，"
        "pending/interrupted/execution_error 不表示完成。"
        if execution_history is not None else
        "本次没有本轮 Runtime 记录，摘要仅来自历史缓存计划，不证明这些动作本轮已经执行。"
        if is_v3 else "摘要来自本次缓存回放的动作序列。"
    )
    source = (
        "本轮不提供历史成功结论；历史通过结论不能证明本轮成功。"
        if is_v3 else "【首次成功语义锚点，仅供理解历史业务别名】\n" + _format_source_completion(trajectory)
    )
    return (
        f"【用户 Case】\n{goal}\n\n"
        f"【缓存执行材料说明】\n{provenance}\n{source}\n\n"
        f"【执行过程，按时间顺序】\n{replay_summary}\n\n"
        f"【附图】\n{img_index_intro}"
    )


def _format_replay_summary(trajectory: Dict[str, Any], *, limit: int = 20) -> str:
    actions = list(trajectory.get("actions") or [])
    if not actions:
        return "(无 action 摘要)"
    lines: List[str] = []
    for action in actions[-limit:]:
        index = action.get("index")
        action_type = action.get("type")
        detail = _action_detail(action)
        intent = action.get("intent") or action.get("label") or ""
        intent_text = f" intent={intent}" if intent else ""
        lines.append(f"step {index}: {action_type}{intent_text}{detail}")
    if len(actions) > limit:
        lines.insert(0, f"... 前面还有 {len(actions) - limit} 个 action")
    return "\n".join(lines)


def _format_execution_history(history: List[Dict[str, Any]]) -> str:
    """V3 本轮事实摘要；不把缓存计划或历史首跑坐标冒充执行记录。"""
    if not history:
        return "(本轮无已记录的回放操作；不以缓存计划替代执行记录)"
    # 使用首跑现有的 100 步覆盖上限；按缓存步骤分组，修复/等待不能挤掉该步其它操作。
    steps = list(dict.fromkeys(row.get("index") for row in history))
    kept_steps = set(steps[-STRUCT_AUDIT_HISTORY_LIMIT:])
    rows = [row for row in history if row.get("index") in kept_steps]
    lines: List[str] = []
    for row in rows:
        action = row.get("action") or {}
        status = str(row.get("runtime_status") or "not_recorded")
        intent = str(action.get("plan_intent") or "")
        detail = "" if status == "skipped" else _action_detail(action)
        if "wait_ms" in action:
            detail += f" wait_ms={action['wait_ms']}"
        line = (
            f"record {row.get('sequence')} step {row.get('index')}: "
            f"{action.get('type')} source={row.get('source')} status={status}"
            f"{detail}"
        )
        if intent:
            line += f" plan_intent={intent}"
        if row.get("reason"):
            line += f" reason={row['reason']}"
        if row.get("error"):
            line += f" error={row['error']}"
        lines.append(line)
    if len(steps) > STRUCT_AUDIT_HISTORY_LIMIT:
        lines.insert(0, f"... 前面还有 {len(steps) - STRUCT_AUDIT_HISTORY_LIMIT} 个缓存步骤的 {len(history) - len(rows)} 条本轮 Runtime 记录")
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
