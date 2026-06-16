"""轨迹缓存瞬态弹窗动作标记与按需回放 gate。

本模块是 V2 轨迹缓存的窄口增强：保存阶段只给 action 打语义角色，
回放阶段只在 ``optional_ephemeral`` action 前做一次独立 gate 判断。关闭
配置后不改变现有 V2 回放路径。

调用协议设计（见 docs/executable-logic-contract.md §14 海外辅 vlm 协议对齐）：
所有海外辅助 vlm（标签 vlm = ephemeral gate / 辅助 vlm = recovery / 定位 vlm =
v3 plan locator）一律走"同主 vlm 模型 + chat 单次协议"，**不进 Computer Use
agent loop**。原因：CU 协议训练目标是 agent 持续干活，被叫一次让它给 verdict
经常用 thinking + 自然语言敷衍，既不调 tool 也不写关键字 → parsed_actions 空 →
ESCALATE → ASSERT_FAIL → 整个 cache 回放卡死。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from ai_phone.config import Settings, get_settings
from ai_phone.agent.trajectory_cache._overseas_chat import (
    main_vlm_is_overseas_cu,
    overseas_cu_to_chat_config as _overseas_cu_to_chat_config,
)
from ai_phone.agent.trajectory_cache.recovery import (
    _extract_messages_text,
    _extract_responses_text,
)

ROLE_BUSINESS_REQUIRED = "business_required"
ROLE_OPTIONAL_EPHEMERAL = "optional_ephemeral"

EPHEMERAL_CATEGORIES = {
    "marketing_popup",
    "upgrade_popup",
    "system_notice",
    "eye_protection",
    "guide_overlay",
    "non_business_blocker",
}
BLOCKED_CATEGORIES = {
    "business_required_modal",
    "confirm_modal",
    "payment_or_trade_confirm",
    "login_or_security",
    "permission_required",
    "case_goal_related",
    "uncertain",
}

GATE_SKIP = "SKIP"
GATE_EXECUTE_ORIGINAL = "EXECUTE_ORIGINAL"
GATE_EXECUTE_REPAIR = "EXECUTE_REPAIR"
GATE_ESCALATE = "ESCALATE"
GATE_ASSERT_FAIL = "ASSERT_FAIL"
GATE_VERDICTS = {
    GATE_SKIP,
    GATE_EXECUTE_ORIGINAL,
    GATE_EXECUTE_REPAIR,
    GATE_ESCALATE,
    GATE_ASSERT_FAIL,
}


@dataclass
class EphemeralClassification:
    role: str
    category: str
    confidence: float
    skip_if_absent: bool
    reason: str
    raw: str = ""
    business_risk: str = ""

    @property
    def is_optional(self) -> bool:
        return self.role == ROLE_OPTIONAL_EPHEMERAL


@dataclass
class EphemeralGateDecision:
    verdict: str
    reason: str
    repair_action: Optional[Dict[str, Any]] = None
    raw: str = ""
    elapsed_ms: int = 0
    error: str = ""
    coord_space: str = "normalized"


class CacheEphemeralActionClassifier:
    """保存阶段的 action 语义清洗器。"""

    def __init__(self, *, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    def is_enabled(self) -> bool:
        s = self.settings
        return bool(
            s.trajectory_cache_ephemeral_action_enabled
            and s.trajectory_cache_ephemeral_classify_enabled
        )

    def is_configured(self) -> bool:
        backend, api_url, api_key, model, _timeout = self._config()
        return bool(
            self.is_enabled()
            and backend
            and api_url
            and api_key
            and model
        )

    def _config(self) -> Tuple[str, str, str, str, float]:
        s = self.settings
        explicit_url = str(s.trajectory_cache_ephemeral_classifier_api_url or "").strip()
        explicit_key = str(s.trajectory_cache_ephemeral_classifier_api_key or "").strip()
        explicit_model = str(s.trajectory_cache_ephemeral_classifier_model or "").strip()
        if explicit_url or explicit_key or explicit_model:
            return (
                s.trajectory_cache_ephemeral_classifier_backend,
                explicit_url,
                explicit_key,
                explicit_model,
                float(s.trajectory_cache_ephemeral_classifier_timeout_sec),
            )
        return (
            _assistant_backend_to_ephemeral_backend(s.assistant_backend),
            str(s.assistant_api_url or "").strip(),
            str(s.assistant_api_key or s.vlm_api_key or "").strip(),
            str(s.assistant_model or "").strip(),
            float(s.trajectory_cache_ephemeral_classifier_timeout_sec),
        )

    def configuration_problem(self) -> str:
        s = self.settings
        if not s.trajectory_cache_ephemeral_action_enabled:
            return "ephemeral action 总开关未启用"
        if not s.trajectory_cache_ephemeral_classify_enabled:
            return "ephemeral classifier 未启用"
        backend, api_url, api_key, model, _timeout = self._config()
        missing: List[str] = []
        if not backend:
            missing.append("backend")
        if not api_url:
            missing.append("api_url")
        if not api_key:
            missing.append("api_key")
        if not model:
            missing.append("model")
        if missing:
            return f"ephemeral classifier 配置缺失：{','.join(missing)}"
        return ""

    async def classify_action(
        self,
        *,
        goal: str,
        action: Dict[str, Any],
        before_bytes: bytes,
        after_bytes: bytes,
        prev_action: Optional[Dict[str, Any]] = None,
        next_action: Optional[Dict[str, Any]] = None,
    ) -> EphemeralClassification:
        if not self.is_configured():
            return EphemeralClassification(
                role=ROLE_BUSINESS_REQUIRED,
                category="uncertain",
                confidence=0.0,
                skip_if_absent=False,
                reason=self.configuration_problem() or "ephemeral classifier 不可用",
            )
        prompt = build_ephemeral_classifier_prompt(
            goal=goal,
            action=action,
            prev_action=prev_action,
            next_action=next_action,
            min_confidence=float(
                self.settings.trajectory_cache_ephemeral_classify_min_confidence
            ),
        )
        backend, api_url, api_key, model, timeout_sec = self._config()
        text = await _call_vlm_with_images(
            backend=backend,
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system=(
                "你是轨迹缓存的瞬态弹窗动作 classifier。"
                "只输出 JSON，不要输出 markdown。"
            ),
            prompt=prompt,
            images=[("action_before", before_bytes), ("action_after", after_bytes)],
        )
        return parse_ephemeral_classification_response(
            text,
            min_confidence=float(
                self.settings.trajectory_cache_ephemeral_classify_min_confidence
            ),
        )


class CacheEphemeralGateVerifier:
    """回放阶段的 optional_ephemeral action gate。"""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        main_vlm_backend: Optional[str] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._main_vlm_backend = (main_vlm_backend or "").strip().lower()

    def is_enabled(self) -> bool:
        s = self.settings
        return bool(
            s.trajectory_cache_ephemeral_action_enabled
            and s.trajectory_cache_ephemeral_gate_enabled
        )

    def _config(self) -> Tuple[str, str, str, str, float]:
        s = self.settings
        timeout_sec = float(s.trajectory_cache_ephemeral_gate_timeout_sec)
        if self._main_vlm_is_overseas_cu():
            backend, api_url, api_key, model = _overseas_cu_to_chat_config(
                main_backend=str(s.vlm_backend or ""),
                main_api_url=str(s.vlm_api_url or ""),
                main_api_key=str(s.vlm_api_key or ""),
                main_model=str(s.vlm_model or ""),
            )
            return (backend, api_url, api_key, model, timeout_sec)
        if s.trajectory_cache_ephemeral_gate_use_recovery_vlm_config:
            return (
                s.trajectory_cache_recovery_vlm_backend,
                s.trajectory_cache_recovery_vlm_api_url,
                s.trajectory_cache_recovery_vlm_api_key,
                s.trajectory_cache_recovery_vlm_model,
                float(s.trajectory_cache_recovery_vlm_timeout_sec),
            )
        return (
            s.trajectory_cache_ephemeral_gate_backend,
            s.trajectory_cache_ephemeral_gate_api_url,
            s.trajectory_cache_ephemeral_gate_api_key,
            s.trajectory_cache_ephemeral_gate_model,
            timeout_sec,
        )

    def is_configured(self) -> bool:
        backend, api_url, api_key, model, _timeout = self._config()
        return bool(self.is_enabled() and backend and api_url and api_key and model)

    def configuration_problem(self) -> str:
        s = self.settings
        if not s.trajectory_cache_ephemeral_action_enabled:
            return "ephemeral action 总开关未启用"
        if not s.trajectory_cache_ephemeral_gate_enabled:
            return "ephemeral gate 未启用"
        backend, api_url, api_key, model, _timeout = self._config()
        missing: List[str] = []
        if not backend:
            missing.append("backend")
        if not api_url:
            missing.append("api_url")
        if not api_key:
            missing.append("api_key")
        if not model:
            missing.append("model")
        if missing:
            if self._main_vlm_is_overseas_cu():
                source = "海外主 vlm chat 协议翻译"
            elif s.trajectory_cache_ephemeral_gate_use_recovery_vlm_config:
                source = "recovery_vlm 配置"
            else:
                source = "ephemeral gate 配置"
            return f"{source}缺失：{','.join(missing)}"
        return ""

    @property
    def coord_space(self) -> str:
        backend = self._main_vlm_backend
        if backend in {"claude_cu", "gpt_cu"}:
            return "absolute"
        if "claude" in backend or backend.startswith("gpt"):
            return "absolute"
        return "normalized"

    def _main_vlm_is_overseas_cu(self) -> bool:
        """主 vlm 是否是海外 Computer Use 链路（claude_cu / gpt_cu）。

        True 时 ephemeral gate 不再走 CU 通道，而是用主 vlm 的 model + key +
        url，按"chat 单次协议"调（见 _overseas_chat.overseas_cu_to_chat_config）。
        这样既复用主 vlm 的视觉能力，又避免 CU agent loop 让模型对"单次
        verdict"任务产生 agent 反射。
        """
        return main_vlm_is_overseas_cu(
            main_vlm_backend=self._main_vlm_backend,
            configured_vlm_backend=str(getattr(self.settings, "vlm_backend", "") or ""),
        )

    async def decide(
        self,
        *,
        goal: str,
        action: Dict[str, Any],
        current_bytes: bytes,
        cached_popup_before_bytes: bytes,
        cached_after_bytes: bytes,
        next_action: Optional[Dict[str, Any]] = None,
    ) -> EphemeralGateDecision:
        if not self.is_configured():
            return EphemeralGateDecision(
                verdict=GATE_ESCALATE,
                reason=self.configuration_problem() or "ephemeral gate 不可用",
                error="not_configured",
            )
        backend, api_url, api_key, model, timeout_sec = self._config()
        prompt = build_ephemeral_gate_prompt(
            goal=goal,
            action=action,
            next_action=next_action,
            coord_space=self.coord_space,
        )
        started = time.monotonic()
        try:
            text = await asyncio.wait_for(
                _call_vlm_with_images(
                    backend=backend,
                    api_url=api_url,
                    api_key=api_key,
                    model=model,
                    timeout_sec=timeout_sec,
                    system=(
                        "你是轨迹缓存回放的瞬态弹窗 gate。"
                        "只判断这个 optional_ephemeral 清障动作当前是否需要执行。"
                        "只输出 JSON，不要输出 markdown。"
                    ),
                    prompt=prompt,
                    images=[
                        ("current_replay", current_bytes),
                        ("cached_popup_before", cached_popup_before_bytes),
                        ("cached_after", cached_after_bytes),
                    ],
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return _ephemeral_call_failure_fallback(
                reason="ephemeral gate 调用超时，按 EXECUTE_ORIGINAL 兜底执行原 action",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error="timeout",
                coord_space=self.coord_space,
            )
        except Exception as exc:  # noqa: BLE001
            return _ephemeral_call_failure_fallback(
                reason=(
                    f"ephemeral gate 调用失败：{type(exc).__name__}: {str(exc)[:160]}，"
                    "按 EXECUTE_ORIGINAL 兜底执行原 action"
                ),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
                coord_space=self.coord_space,
            )
        decision = parse_ephemeral_gate_response(text)
        decision.elapsed_ms = int((time.monotonic() - started) * 1000)
        decision.coord_space = self.coord_space
        if decision.error == "parse_error":
            return _ephemeral_call_failure_fallback(
                reason="ephemeral gate 输出不可解析，按 EXECUTE_ORIGINAL 兜底执行原 action",
                elapsed_ms=decision.elapsed_ms,
                error="parse_error",
                raw=decision.raw,
                coord_space=self.coord_space,
            )
        return decision

def _ephemeral_call_failure_fallback(
    *,
    reason: str,
    elapsed_ms: int,
    error: str,
    coord_space: str,
    raw: str = "",
) -> EphemeralGateDecision:
    """ephemeral gate 调用 / 解析失败时的兜底裁决。

    optional_ephemeral 本来就是"在就关，不在就空点一下"的低风险动作；
    与其升级到 recovery（recovery 也可能失败 → ASSERT_FAIL → 整个回放卡死），
    不如直接保底执行原 action（最坏 = 当前页面没那个弹窗，空点一下，不影响业务）。
    """
    return EphemeralGateDecision(
        verdict=GATE_EXECUTE_ORIGINAL,
        reason=reason,
        elapsed_ms=elapsed_ms,
        error=error,
        raw=raw,
        coord_space=coord_space,
    )


# _overseas_cu_to_chat_config 已抽到共享 helper
# ``_overseas_chat`` 模块（recovery / v3 locator 也复用），见上方 import。


def parse_ephemeral_classification_response(
    text: str,
    *,
    min_confidence: float = 0.85,
) -> EphemeralClassification:
    raw = (text or "").strip()
    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        return EphemeralClassification(
            role=ROLE_BUSINESS_REQUIRED,
            category="uncertain",
            confidence=0.0,
            skip_if_absent=False,
            reason="classifier 输出不是 JSON，按 business_required 兜底",
            raw=raw,
        )

    role = str(data.get("role") or ROLE_BUSINESS_REQUIRED).strip().lower()
    category = str(data.get("category") or "uncertain").strip().lower()
    confidence = _clamp_float(data.get("confidence"), 0.0, 1.0)
    skip_if_absent = bool(data.get("skip_if_absent") is True)
    reason = str(data.get("reason") or "").strip()[:500]
    business_risk = str(data.get("business_risk") or "").strip().lower()

    if role != ROLE_OPTIONAL_EPHEMERAL:
        return EphemeralClassification(
            role=ROLE_BUSINESS_REQUIRED,
            category=category or "uncertain",
            confidence=confidence,
            skip_if_absent=False,
            reason=reason or "classifier 判定为业务必需动作",
            raw=raw,
            business_risk=business_risk,
        )

    veto_reason = ""
    if category not in EPHEMERAL_CATEGORIES or category in BLOCKED_CATEGORIES:
        veto_reason = f"category={category or 'uncertain'} 不允许标记 optional_ephemeral"
    elif confidence < float(min_confidence):
        veto_reason = f"confidence={confidence:.2f} 低于阈值 {float(min_confidence):.2f}"
    elif not skip_if_absent:
        veto_reason = "skip_if_absent=false，不能作为可跳过瞬态动作"
    elif business_risk and business_risk not in {"low", "none", "无", "低"}:
        veto_reason = f"business_risk={business_risk} 非低风险"

    if veto_reason:
        return EphemeralClassification(
            role=ROLE_BUSINESS_REQUIRED,
            category=category or "uncertain",
            confidence=confidence,
            skip_if_absent=False,
            reason=f"{veto_reason}；{reason}".strip("；"),
            raw=raw,
            business_risk=business_risk,
        )

    return EphemeralClassification(
        role=ROLE_OPTIONAL_EPHEMERAL,
        category=category,
        confidence=confidence,
        skip_if_absent=True,
        reason=reason or "非业务瞬态遮挡清障动作",
        raw=raw,
        business_risk=business_risk or "low",
    )


def parse_ephemeral_gate_response(text: str) -> EphemeralGateDecision:
    raw = (text or "").strip()
    data = _extract_json_object(raw)
    if isinstance(data, dict):
        verdict = str(data.get("verdict") or GATE_ESCALATE).strip().upper()
        reason = str(data.get("reason") or "").strip()[:500]
        repair_action = data.get("repair_action")
        if verdict not in GATE_VERDICTS:
            verdict = GATE_ESCALATE
            reason = reason or "gate verdict 未知，转入保守路径"
        if verdict == GATE_EXECUTE_REPAIR and not isinstance(repair_action, dict):
            return EphemeralGateDecision(
                verdict=GATE_ESCALATE,
                reason=reason or "EXECUTE_REPAIR 缺少 repair_action，转入保守路径",
                raw=raw,
                error="missing_repair_action",
            )
        return EphemeralGateDecision(
            verdict=verdict,
            reason=reason or _default_gate_reason(verdict),
            repair_action=repair_action if isinstance(repair_action, dict) else None,
            raw=raw,
        )

    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    upper = first_line.upper()
    for verdict in GATE_VERDICTS:
        if upper == verdict or upper.startswith(verdict + ":"):
            return EphemeralGateDecision(
                verdict=verdict,
                reason=_split_after_colon(first_line) or _default_gate_reason(verdict),
                raw=raw,
            )
    return EphemeralGateDecision(
        verdict=GATE_ESCALATE,
        reason="gate 输出不可解析，转入保守路径",
        raw=raw,
        error="parse_error",
    )


def build_ephemeral_classifier_prompt(
    *,
    goal: str,
    action: Dict[str, Any],
    prev_action: Optional[Dict[str, Any]] = None,
    next_action: Optional[Dict[str, Any]] = None,
    min_confidence: float = 0.85,
) -> str:
    return (
        "你要判断一次成功轨迹中的某个 action 是否只是“非业务瞬态遮挡清障”。\n"
        "只有同时满足：阻挡证据明确、非业务证据明确、没有任何高风险一票否决，"
        "才允许输出 optional_ephemeral。\n\n"
        f"用户目标 / case：\n{goal}\n\n"
        f"当前 action：\n{_json_dumps_compact(_action_brief(action))}\n"
        f"上一业务 action：\n{_json_dumps_compact(_action_brief(prev_action))}\n"
        f"下一业务 action：\n{_json_dumps_compact(_action_brief(next_action))}\n\n"
        "附图 1 是该 action 执行前截图；附图 2 是该 action 执行后/下一步前截图。\n\n"
        "强约束：\n"
        "- thought 没有明确表达“本来要执行业务动作 A，但非业务弹窗/浮层挡住，所以先关闭后继续 A”，必须 business_required。\n"
        "- 不确定必须 business_required。\n"
        "- 涉及交易、支付、下单、提交、保存、授权、登录、安全、验证码、业务确认、二次确认，必须 business_required。\n"
        "- goal 提到或强相关的弹窗，必须 business_required。\n"
        "- 不能因为 case 没写这个弹窗就判定 optional_ephemeral。\n"
        f"- optional_ephemeral 的 confidence 必须 >= {float(min_confidence):.2f}。\n\n"
        "只输出 JSON：\n"
        "{\n"
        '  "role": "business_required | optional_ephemeral",\n'
        '  "category": "marketing_popup | upgrade_popup | system_notice | eye_protection | guide_overlay | non_business_blocker | business_required_modal | confirm_modal | payment_or_trade_confirm | login_or_security | permission_required | case_goal_related | uncertain",\n'
        '  "confidence": 0.0,\n'
        '  "skip_if_absent": true,\n'
        '  "business_risk": "low | medium | high",\n'
        '  "reason": "一句话说明"\n'
        "}\n"
    )


def build_ephemeral_gate_prompt(
    *,
    goal: str,
    action: Dict[str, Any],
    next_action: Optional[Dict[str, Any]] = None,
    coord_space: str = "normalized",
) -> str:
    coord_rule = (
        "repair_action 的 point 使用 0-1000 归一化坐标。"
        if coord_space == "normalized"
        else "repair_action 的 point 使用附图 1（当前回放截图）的像素坐标。"
    )
    return (
        "你要判断缓存回放中的 optional_ephemeral 清障动作当前是否还需要执行。\n"
        "附图 1：当前回放截图；附图 2：首次成功轨迹中该弹窗出现时截图；"
        "附图 3：首次成功轨迹中该弹窗关闭后的业务状态截图。\n\n"
        f"用户目标 / case：\n{goal}\n\n"
        f"optional_ephemeral action：\n{_json_dumps_compact(_action_brief(action))}\n"
        f"下一步业务 action：\n{_json_dumps_compact(_action_brief(next_action))}\n\n"
        "强约束：\n"
        "- 只有当前确认没有同类弹窗，并且页面能衔接下一步业务，才允许 SKIP。\n"
        "- 只有当前确认存在同类弹窗，才允许 EXECUTE_ORIGINAL / EXECUTE_REPAIR。\n"
        "- 同类弹窗位置或关闭入口变化时，优先 EXECUTE_REPAIR。\n"
        "- 不确定必须 ESCALATE，不能冒险跳过。\n"
        "- 禁止输出业务新步骤，只能处理这个瞬态清障动作。\n"
        f"- {coord_rule}\n\n"
        "只输出 JSON：\n"
        "{\n"
        '  "verdict": "SKIP | EXECUTE_ORIGINAL | EXECUTE_REPAIR | ESCALATE | ASSERT_FAIL",\n'
        '  "reason": "一句话说明",\n'
        '  "repair_action": {"type": "click", "point": {"x": 0, "y": 0}}\n'
        "}\n"
    )


async def _call_vlm_with_images(
    *,
    backend: str,
    api_url: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    system: str,
    prompt: str,
    images: Sequence[Tuple[str, bytes]],
) -> str:
    normalized_backend = (backend or "doubao_responses").strip().lower()
    if normalized_backend == "doubao_responses":
        return await _responses_images(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system=system,
            prompt=prompt,
            images=images,
        )
    if normalized_backend == "openai_compatible":
        return await _chat_completions_images(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system=system,
            prompt=prompt,
            images=images,
        )
    if normalized_backend == "openai_responses":
        return await _openai_responses_images(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system=system,
            prompt=prompt,
            images=images,
        )
    if normalized_backend == "claude_messages":
        return await _messages_images(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system=system,
            prompt=prompt,
            images=images,
        )
    raise RuntimeError(
        f"ephemeral VLM 暂不支持 backend={normalized_backend}，"
        "当前支持 doubao_responses / openai_compatible / openai_responses / claude_messages"
    )


async def _responses_images(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    system: str,
    prompt: str,
    images: Sequence[Tuple[str, bytes]],
) -> str:
    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for _label, data in images:
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{_b64(data)}",
            }
        )
    payload: Dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "store": True,
        "caching": {"type": "enabled"},
        "thinking": {"type": "disabled"},
    }
    data = await _post_json(api_url, api_key, payload, timeout_sec)
    text = _extract_responses_text(data)
    if not text:
        raise RuntimeError("ephemeral responses 未返回可解析文本")
    return text


async def _chat_completions_images(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    system: str,
    prompt: str,
    images: Sequence[Tuple[str, bytes]],
) -> str:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for _label, data in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{_b64(data)}"},
            }
        )
    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "top_p": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    # classifier/gate 都属于高风险裁决：宁可慢一点，也不要“扫一眼”误标。
    # 但 chat-completions 兼容层里各家字段并不一致：
    # - 火山方舟 / 豆包：thinking.type=enabled
    # - OpenAI o 系列：reasoning_effort=medium
    # - 其它 OpenAI-compatible 代理：不强塞私有字段，避免 400 后降级。
    if _is_doubao_chat_url(api_url):
        payload["thinking"] = {"type": "enabled"}
    elif _is_openai_chat_url(api_url):
        payload["reasoning_effort"] = "medium"
    data = await _post_json(api_url, api_key, payload, timeout_sec)
    message = (data.get("choices") or [{}])[0].get("message") or {}
    value = message.get("content")
    if isinstance(value, list):
        text = "".join(
            block.get("text", "") for block in value if isinstance(block, dict)
        ).strip()
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ""
    if not text:
        raise RuntimeError("ephemeral chat 未返回可解析文本")
    return text


async def _openai_responses_images(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    system: str,
    prompt: str,
    images: Sequence[Tuple[str, bytes]],
) -> str:
    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for _label, data in images:
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{_b64(data)}",
            }
        )
    payload: Dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "reasoning": {"effort": "medium"},
    }
    data = await _post_json(api_url, api_key, payload, timeout_sec)
    text = _extract_responses_text(data)
    if not text:
        raise RuntimeError("ephemeral openai_responses 未返回可解析文本")
    return text


async def _messages_images(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout_sec: float,
    system: str,
    prompt: str,
    images: Sequence[Tuple[str, bytes]],
) -> str:
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for _label, data in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": _b64(data),
                },
            }
        )
    # max_tokens=8192：见 recovery.py 同位置注释。anthropic /v1/messages 是硬
    # 上限不是目标值，调高只是防截断，不会让模型多输出。瞬态 UI 标签判定的
    # 长尾同样可能写"附图1有 X，附图2有 Y，所以放行/拒绝/接管" 完整 thought，
    # 4096 偶发也能压到边缘，统一拉到 8192 给所有辅助 vlm 一致 buffer。
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    settings = get_settings()
    if int(settings.vlm_main_thinking_budget or 0) > 0:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": int(settings.vlm_main_thinking_budget),
        }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(timeout_sec, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(
            f"ephemeral messages 失败: status={resp.status_code} body={resp.text[:200]}"
        )
    text = _extract_messages_text(resp.json())
    if not text:
        raise RuntimeError("ephemeral messages 未返回可解析文本")
    return text


async def _post_json(
    api_url: str,
    api_key: str,
    payload: Dict[str, Any],
    timeout_sec: float,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(timeout_sec, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(
            f"ephemeral VLM 失败: status={resp.status_code} body={resp.text[:200]}"
        )
    return resp.json()


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    raw = _strip_json_fence(raw)
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if not lines:
        return text
    if lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _is_doubao_chat_url(api_url: str) -> bool:
    url = (api_url or "").lower()
    return "volces.com" in url or "ark.cn-" in url


def _is_openai_chat_url(api_url: str) -> bool:
    return "api.openai.com" in (api_url or "").lower()


def _assistant_backend_to_ephemeral_backend(assistant_backend: str) -> str:
    backend = (assistant_backend or "").strip().lower()
    if backend == "claude":
        return "claude_messages"
    # doubao_chat 与 openai 都是 chat-completions 形态；具体 thinking/reasoning
    # 字段由 URL 再细分。
    if backend in {"doubao_chat", "openai", "openai_compatible"}:
        return "openai_compatible"
    if backend in {"doubao_responses", "claude_messages"}:
        return backend
    return "openai_compatible"


def _action_brief(action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not action:
        return {}
    keys = (
        "action_id",
        "index",
        "type",
        "intent",
        "label",
        "thought",
        "point",
        "start",
        "end",
        "content",
        "role",
        "ephemeral_meta",
    )
    return {key: action.get(key) for key in keys if action.get(key) not in (None, "")}


def _json_dumps_compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:4000]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _clamp_float(value: Any, lo: float, hi: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, num))


def _split_after_colon(line: str) -> str:
    if ":" in line:
        return line.split(":", 1)[1].strip()
    if "：" in line:
        return line.split("：", 1)[1].strip()
    return ""


def _default_gate_reason(verdict: str) -> str:
    return {
        GATE_SKIP: "当前无同类瞬态弹窗，跳过清障动作",
        GATE_EXECUTE_ORIGINAL: "当前存在同类瞬态弹窗，执行原缓存动作",
        GATE_EXECUTE_REPAIR: "当前存在同类瞬态弹窗，执行 gate 修复动作",
        GATE_ESCALATE: "gate 无法确认，转入保守路径",
        GATE_ASSERT_FAIL: "gate 判定当前状态不健康",
    }.get(verdict, "gate 裁决")
