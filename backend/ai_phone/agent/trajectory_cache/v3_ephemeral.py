"""V3 独立的整批语义标记与回放弹窗 gate。

业务规则由 V2 复制后独立维护；不调用 V2 classifier/gate/prompt/parser。
仅沿用既有底层多协议 HTTP 传输与配置，不增加模型请求链路。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ai_phone.config import Settings, get_settings
from ._overseas_chat import main_vlm_is_overseas_cu, overseas_cu_to_chat_config as _overseas_cu_to_chat_config
from .ephemeral import _call_vlm_with_images

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
class EphemeralGateDecision:
    verdict: str
    reason: str
    repair_action: Optional[Dict[str, Any]] = None
    raw: str = ""
    elapsed_ms: int = 0
    error: str = ""
    coord_space: str = "normalized"


class V3EphemeralGateVerifier:
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
        return bool(s.trajectory_cache_ephemeral_action_enabled and s.trajectory_cache_ephemeral_gate_enabled)

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
        cached_popup_before_bytes: Optional[bytes] = None,
        cached_after_bytes: Optional[bytes] = None,
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
            has_cached_images=bool(cached_popup_before_bytes and cached_after_bytes),
        )
        images = [("current_replay", current_bytes)]
        if cached_popup_before_bytes and cached_after_bytes:
            images.extend(
                [
                    ("cached_popup_before", cached_popup_before_bytes),
                    ("cached_after", cached_after_bytes),
                ]
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
                    images=images,
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

    保留既有 V3 的原动作路径；原动作仍须当前截图重定位，不能在旧坐标空点。
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


def build_ephemeral_gate_prompt(
    *,
    goal: str,
    action: Dict[str, Any],
    next_action: Optional[Dict[str, Any]] = None,
    coord_space: str = "normalized",
    has_cached_images: bool = True,
) -> str:
    coord_rule = (
        "repair_action 的 point 使用 0-1000 归一化坐标。"
        if coord_space == "normalized"
        else "repair_action 的 point 使用附图 1（当前回放截图）的像素坐标。"
    )
    image_intro = (
        (
            "附图 1：当前回放截图；附图 2：首次成功轨迹中该弹窗出现时截图；"
            "附图 3：首次成功轨迹中该弹窗关闭后的业务状态截图。\n\n"
        )
        if has_cached_images
        else (
            "本次只附一张当前回放截图，没有首跑对照图片。历史语义标记只是候选，"
            "不是本轮弹窗存在/缺席或动作成功的证据，不能假称看到了历史图片。\n\n"
        )
    )
    return (
        "你要判断缓存回放中的 optional_ephemeral 清障动作当前是否还需要执行。\n"
        + image_intro
        + f"用户目标 / case：\n{goal}\n\n"
        f"optional_ephemeral action：\n{_json_dumps_compact(_action_brief(action))}\n"
        f"下一步业务 action：\n{_json_dumps_compact(_action_brief(next_action))}\n\n"
        "强约束：\n"
        "- 只有当前确认没有同类弹窗，并且页面能衔接下一步业务，才允许 SKIP。\n"
        "- 只有当前确认存在同类弹窗，才允许 EXECUTE_ORIGINAL / EXECUTE_REPAIR。\n"
        "- 同类弹窗位置或关闭入口变化时，优先 EXECUTE_REPAIR。\n"
        "- 不确定必须 ESCALATE，不能冒险跳过。\n"
        "- 若该动作与原始 Case 的业务条件、权限、登录安全或确认要求有关，"
        "不能依据历史 optional 标记跳过，必须 ESCALATE。\n"
        "- 禁止输出业务新步骤，只能处理这个瞬态清障动作。\n"
        f"- {coord_rule}\n\n"
        "只输出 JSON：\n"
        "{\n"
        '  "verdict": "SKIP | EXECUTE_ORIGINAL | EXECUTE_REPAIR | ESCALATE | ASSERT_FAIL",\n'
        '  "reason": "一句话说明",\n'
        '  "repair_action": {"type": "click", "point": {"x": 0, "y": 0}}\n'
        "}\n"
    )


_BATCH_EPHEMERAL_FIELDS = frozenset(
    {
        "role",
        "category",
        "confidence",
        "skip_if_absent",
        "business_risk",
        "reason",
    }
)
_V3_POPUP_ACTIONS = frozenset({"click", "double_tap", "long_press", "press_back"})


def validate_batch_ephemeral_fields(value: Any) -> List[str]:
    """V3 专用批量分类结构校验，错误交由同批修正，不调用 V2 解析器。"""
    if not isinstance(value, dict):
        return ["ephemeral 必须是完整对象"]
    errors = []
    if set(value) != _BATCH_EPHEMERAL_FIELDS:
        errors.append("ephemeral 字段必须完整且不得包含执行参数")
    if not isinstance(value.get("role"), str) or value["role"] not in {
        ROLE_BUSINESS_REQUIRED,
        ROLE_OPTIONAL_EPHEMERAL,
    }:
        errors.append("ephemeral.role 非法")
    if (
        not isinstance(value.get("category"), str)
        or value["category"] not in EPHEMERAL_CATEGORIES | BLOCKED_CATEGORIES
    ):
        errors.append("ephemeral.category 非法")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        errors.append("ephemeral.confidence 必须是0到1的有限数字")
    if not isinstance(value.get("skip_if_absent"), bool):
        errors.append("ephemeral.skip_if_absent 必须是布尔值")
    if not isinstance(value.get("business_risk"), str) or value["business_risk"] not in {
        "low",
        "medium",
        "high",
    }:
        errors.append("ephemeral.business_risk 非法")
    if not isinstance(value.get("reason"), str) or len(value["reason"]) > 300:
        errors.append("ephemeral.reason 必须是最多300字的字符串")
    return errors


def batch_ephemeral_metadata(
    action: Dict[str, Any],
    value: Dict[str, Any],
    *,
    min_confidence: float,
) -> Optional[Dict[str, Any]]:
    """复制 V2 的保守接受门槛，独立维护；不改变原动作与定位描述。"""
    if validate_batch_ephemeral_fields(value):
        return None
    if (
        action.get("type") not in _V3_POPUP_ACTIONS
        or value["role"] != ROLE_OPTIONAL_EPHEMERAL
        or value["category"] not in EPHEMERAL_CATEGORIES
        or value["confidence"] < min_confidence
        or value["skip_if_absent"] is not True
        or value["business_risk"] != "low"
    ):
        return None
    return {
        "enabled": True,
        "category": value["category"],
        "skip_if_absent": True,
        "confidence": value["confidence"],
        "business_risk": value["business_risk"],
        "reason": value["reason"],
        "classification_source": "v3_batch_semantic",
    }


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


def _action_brief(action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not action:
        return {}
    keys = (
        "action_id",
        "index",
        "type",
        "intent",
        "plan_intent",
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
