"""首跑成功后用执行第一手数据整理 V3 成品缓存（Distributed Agent Brain · M4 片3b）。

归档下沉 Agent 后，不再从 DB 文字反推：``recorder`` 旁路收集的每步第一手数据
（结构化动作 ``ParsedAction.to_dict()`` + thought + 时序）在这里整理成与 next
**同 schema** 的 V3 成品（actions + plan_intent / source_completion / meta），交由
编排经 M3 可靠通道（``MSG_CACHE_ARCHIVE``）后台回传 Server upsert。

``plan_intent`` 生成逻辑（``_plan_intent_for_action`` 等）从旧 ``server.trajectory_cache.
v3_service`` 整段迁来——纯文本加工、不依赖 DB，只换数据来源（DB 反推 → 执行第一手）。
模型清洗（旧 V3PlanIntentCleaner）暂不接入，先用规则兜底（与 next 未配 cleaner 时一致）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from ai_phone.config import Settings, get_settings
from ai_phone.shared import actions as A

from .ephemeral import (
    ROLE_BUSINESS_REQUIRED,
    ROLE_OPTIONAL_EPHEMERAL,
    CacheEphemeralActionClassifier,
    EphemeralClassification,
    _assistant_backend_to_ephemeral_backend,
    _call_vlm_with_images,
    _extract_json_object,
    _json_dumps_compact,
)
from .text_norm import normalize_run_semantic

V3_CACHE_SCHEMA_VERSION = 3

_WS_RE = re.compile(r"\s+")
_ACTION_VERB_RE = re.compile(
    r"点击|轻点|输入|打开|关闭|选择|切换|返回|滑动|上滑|下滑|左滑|右滑|长按|双击|"
    r"等待|勾选|取消|进入|tap|click|type|enter|swipe|scroll|drag|"
    r"long[_\s-]?press|double[_\s-]?(?:click|tap)|press|launch|open|close|"
    r"wait|select|toggle|back|home|navigate",
    re.IGNORECASE,
)
_CLICK_LIKE_VERB_RE = re.compile(
    r"点击|轻点|选择|按下|勾选|取消|tap|click|press|select|toggle",
    re.IGNORECASE,
)
_LEADING_ACTION_VERB_RE = re.compile(
    r"^(点击|轻点|输入|打开|关闭|选择|切换|返回|滑动|上滑|下滑|左滑|右滑|长按|双击|"
    r"等待|勾选|取消|进入|tap|click|type|enter|swipe|scroll|drag|"
    r"long[_\s-]?press|double[_\s-]?(?:click|tap)|press|launch|open|close|"
    r"wait|select|toggle|back|home|navigate)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[\n。；;.!?！？]+")
_NOISY_PLAN_TEXT_RE = re.compile(
    r"let me analyze|current screenshot|i can see|appears to|forced verdict|"
    r"substep|target state|assert_fail|continue_replay|give_up|locator|"
    r"verdict|traceback|exception|error=|raw=|thought:|action:|"
    r"\b(?:has|have|had)\s+been\b|"
    r"\b(?:has|have|had)\s+(?:opened|appeared|loaded|shown|displayed|entered|"
    r"selected|closed|been\s+opened)\b|"
    r"\bappeared\b|\bopened\s+but\b|\bnot\s+yet\b|"
    r"\bi['\u2019]?ve\b|"
    r"\bi['\u2019]?m\s+(?:at|in|on|not|now|already|currently|still)\b|"
    r"\bi\s+am\s+(?:at|in|on|not|now|already|currently|still)\b|"
    r"\b(?:is|are|was|were)\s+(?:already|currently|now|still)\b|"
    r"\bindicating\b|\bsuggesting\b|\bshowing\s+that\b|"
    r"\bcurrent(?:ly)?\s+(?:page|state|screen|view)\s+(?:shows|is|displays|has)\b|"
    r"\bthe\s+(?:app|page|screen|dialog|window|popup|view)\s+(?:has|is|was)\b|"
    r"\b\d+\s*%\b",
    re.IGNORECASE,
)
_ACTION_TARGET_PATTERNS = (
    re.compile(r"(?:需要|应该|下一步|现在|当前|先)?(?:点击|轻点|按下)(?P<target>.+)", re.IGNORECASE),
    re.compile(
        r"(?:need to|should|will|next|now|currently)?\s*"
        r"(?:click|tap|press)(?:\s+on)?(?:\s+the)?\s+(?P<target>.+)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:需要|应该|下一步|现在|当前|先)?(?:选择|勾选|取消)(?P<target>.+)", re.IGNORECASE),
    re.compile(
        r"(?:need to|should|will|next|now|currently)?\s*"
        r"(?:select|toggle)(?:\s+on)?(?:\s+the)?\s+(?P<target>.+)",
        re.IGNORECASE,
    ),
)
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*")
_PLAN_STOPWORDS = {
    "the", "a", "an", "to", "on", "in", "of", "and", "or",
    "button", "tab", "page", "target", "click", "tap", "press", "select",
}

# ---------------------------------------------------------------------------
# 第一手数据 → V3 成品（新方向：旁路 recorder steps 直接整理，不反推 DB）
# ---------------------------------------------------------------------------
async def build_v3_archive(
    *,
    goal: str,
    device_serial: str,
    source_run_id: str,
    source_vlm_backend: str = "",
    platform: str = "",
    resolution: str = "",
    screen_size: Tuple[int, int] = (0, 0),
    run_reason: str = "",
    completion_logs: Optional[Dict[str, str]] = None,
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """从首跑 recorder 的每步第一手数据整理 V3 成品（archive 格式，喂 repository）。

    ``screen_size``=(w,h) 用于把 normalized 坐标（doubao 系）转绝对像素；absolute 系
    （claude/gpt CU）按 next 口径直接存原值。返回 None-safe 的 dict；``actions`` 为空时
    由调用方决定是否丢弃（不回传空缓存）。
    """
    raw_actions = _actions_from_steps(
        steps, source_vlm_backend=source_vlm_backend, screen_size=screen_size
    )
    actions = [
        _normalize_v3_action(action, source_vlm_backend=source_vlm_backend)
        for action in raw_actions
    ]
    payload: Dict[str, Any] = {
        "cache_mode": "v3",
        "device_code": device_serial,
        "run_semantic_text": goal,
        "source_run_id": source_run_id,
        "source_vlm_backend": source_vlm_backend,
        "platform": platform,
        "resolution": resolution,
        "app_package_or_bundle": _first_app_target(actions),
        "actions": actions,
        "source_completion": _source_completion_from_steps(
            steps, run_reason=run_reason, completion_logs=completion_logs
        ),
        "meta": {
            "source_schema_version": V3_CACHE_SCHEMA_VERSION,
            "plan_intent_cleaner": "rule",
        },
    }
    # 模型清洗 plan_intent（next 默认启用、复用 .env classifier 配置）：覆盖规则候选，
    # 提升 locator 定位语义质量。在归档后台 task 内 await（不阻塞 case 完成）；未配置 /
    # 调用失败 / 输出冲突时回退保留规则候选。
    await _clean_v3_plan_intents(payload=payload, goal=goal)
    return payload


async def build_v2_archive(
    *,
    goal: str,
    device_serial: str,
    source_run_id: str,
    source_vlm_backend: str = "",
    platform: str = "",
    resolution: str = "",
    screen_size: Tuple[int, int] = (0, 0),
    run_reason: str = "",
    completion_logs: Optional[Dict[str, str]] = None,
    steps: List[Dict[str, Any]],
    upload_image,
) -> Dict[str, Any]:
    """从首跑第一手数据整理 V2 成品（与 next 同 schema：``trajectory_json`` 含 actions +
    state_landmarks + source_completion）。

    state_landmarks 用每步执行后截图（recorder 收的 after bytes）算 phash/sha256，并经
    ``upload_image``（async (bytes)->url）上传 Server 得 image_url（供回放预取）；timing 用
    第一手时序。ephemeral 动作分类（标 optional_ephemeral）留片6 收口，这里 role 默认
    business_required（与 next 关 ephemeral 开关时等价）。
    """
    raw_actions = _actions_from_steps(
        steps, source_vlm_backend=source_vlm_backend, screen_size=screen_size
    )
    actions = [_normalize_v2_action(a) for a in raw_actions]
    state_landmarks = await _build_state_landmarks_from_steps(
        actions, steps, upload_image=upload_image
    )
    # ephemeral 动作分类（标 optional_ephemeral + 瞬态证据图）：.env 开 ephemeral_action_enabled
    # 时生效，供回放 gate 跳过偶现遮挡动作；未启用/未配置则全部 business_required（都执行）。
    await _classify_ephemeral_actions(
        actions, steps, state_landmarks, goal=goal, upload_image=upload_image
    )
    trajectory_json = {
        "schema_version": 2,
        "actions": actions,
        "state_landmarks": state_landmarks,
        "source_completion": _source_completion_from_steps(
            steps, run_reason=run_reason, completion_logs=completion_logs
        ),
    }
    return {
        "cache_mode": "v2",
        "device_code": device_serial,
        "run_semantic_text": goal,
        "source_run_id": source_run_id,
        "source_vlm_backend": source_vlm_backend,
        "platform": platform,
        "resolution": resolution,
        "app_package_or_bundle": _first_app_target(actions),
        "trajectory_json": trajectory_json,
    }


async def build_v1_archive(
    *,
    goal: str,
    device_serial: str,
    source_run_id: str,
    source_vlm_backend: str = "",
    platform: str = "",
    resolution: str = "",
    screen_size: Tuple[int, int] = (0, 0),
    run_reason: str = "",
    completion_logs: Optional[Dict[str, str]] = None,
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """从首跑第一手数据整理 V1 成品（最朴素：固定动作 + 绝对坐标，无 state_landmarks）。

    与 next 同 schema：``trajectory_json`` 含 actions + state_landmarks(空) + source_completion。
    V1 不做 landmark 截图 / phash / 上传 / ephemeral 分类（回放用页面像素稳定 + 绝对坐标）。
    签名 async 仅为与 V2/V3 归档统一调用，无实际 await。
    """
    actions = _actions_from_steps(
        steps, source_vlm_backend=source_vlm_backend, screen_size=screen_size
    )
    trajectory_json = {
        "schema_version": 1,
        "actions": actions,
        "state_landmarks": [],
        "source_completion": _source_completion_from_steps(
            steps, run_reason=run_reason, completion_logs=completion_logs
        ),
    }
    return {
        "cache_mode": "v1",
        "device_code": device_serial,
        "run_semantic_text": goal,
        "source_run_id": source_run_id,
        "source_vlm_backend": source_vlm_backend,
        "platform": platform,
        "resolution": resolution,
        "app_package_or_bundle": _first_app_target(actions),
        "trajectory_json": trajectory_json,
    }


def _normalize_v2_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """V2 action：保留 _action_from_parsed_raw 的回放字段 + 默认 role（可被 ephemeral 分类覆盖）。"""
    out = dict(action)
    out.setdefault("role", ROLE_BUSINESS_REQUIRED)
    return out


def _is_ephemeral_candidate_action(action: Dict[str, Any]) -> bool:
    """仅点击类动作（click/double_tap/long_press/press_back）才可能是瞬态遮挡动作。"""
    return str(action.get("type") or "") in {
        A.ACTION_CLICK,
        A.ACTION_DOUBLE_TAP,
        A.ACTION_LONG_PRESS,
        A.ACTION_PRESS_BACK,
    }


async def _classify_ephemeral_actions(
    actions: List[Dict[str, Any]],
    steps: List[Dict[str, Any]],
    state_landmarks: List[Dict[str, Any]],
    *,
    goal: str,
    upload_image,
) -> None:
    """给 V2 action 补 role / ephemeral_meta（自 next service 迁来，Agent 直连 VLM）。

    用每步第一手 before/after 截图调 classifier；判为 optional_ephemeral 的动作上传
    popup_before 证据图（cached_after 复用 state_landmark 已上传的 url），写 ephemeral_meta
    供回放 gate。总开关/配置缺失 / 证据缺失时按 business_required（不影响回放正确性）。
    """
    settings = get_settings()
    if not bool(getattr(settings, "trajectory_cache_ephemeral_action_enabled", False)):
        return
    classifier = CacheEphemeralActionClassifier(settings=settings)
    if not classifier.is_enabled() or not classifier.is_configured():
        logger.info(
            "ephemeral classifier 未启用/配置不全（{}），动作按 business_required",
            classifier.configuration_problem() or "disabled",
        )
        return
    steps_by_no = {int(s.get("step")): s for s in steps if s.get("step") is not None}
    landmark_url_by_id = {
        str(lm.get("action_id")): str(lm.get("image_url") or "")
        for lm in state_landmarks
        if str(lm.get("action_id") or "")
    }
    for idx, action in enumerate(actions):
        if not _is_ephemeral_candidate_action(action):
            continue
        cur_step = action.get("source_step")
        step_data = steps_by_no.get(int(cur_step)) if cur_step is not None else None
        before_bytes = step_data.get("before_bytes") if step_data else None
        after_bytes = step_data.get("after_bytes") if step_data else None
        if not before_bytes or not after_bytes:
            continue
        try:
            result = await classifier.classify_action(
                goal=goal,
                action=action,
                before_bytes=before_bytes,
                after_bytes=after_bytes,
                prev_action=actions[idx - 1] if idx > 0 else None,
                next_action=actions[idx + 1] if idx + 1 < len(actions) else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ephemeral classify 失败 action_id={}: {}", action.get("action_id"), exc)
            continue
        if not isinstance(result, EphemeralClassification) or not result.is_optional:
            continue
        try:
            popup_url = await upload_image(before_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ephemeral popup_before 上传失败 action_id={}: {}", action.get("action_id"), exc
            )
            continue
        if not popup_url:
            continue
        action["role"] = ROLE_OPTIONAL_EPHEMERAL
        action["ephemeral_meta"] = {
            "enabled": True,
            "category": result.category,
            "skip_if_absent": True,
            "confidence": float(result.confidence),
            "reason": result.reason,
            "business_risk": result.business_risk or "low",
            "cached_popup_before_snapshot": popup_url,
            "cached_after_snapshot": landmark_url_by_id.get(str(action.get("action_id")), ""),
        }


async def _build_state_landmarks_from_steps(
    actions: List[Dict[str, Any]],
    steps: List[Dict[str, Any]],
    *,
    upload_image,
) -> List[Dict[str, Any]]:
    """每个 action 后页面截图 → state landmark（phash/sha256 + 上传 url + 第一手 timing）。

    链内动作（同 source_step 且非末）无独立 handoff 截图 → status=unavailable（回放回落
    页面稳定，与 next same_step_action_chain_no_handoff 一致）；上传失败同样降级 unavailable。
    """
    import hashlib as _hashlib

    from ai_phone.agent.runner.phash import compute_phash

    steps_by_no = {int(s.get("step")): s for s in steps if s.get("step") is not None}
    landmarks: List[Dict[str, Any]] = []
    for idx, action in enumerate(actions):
        action_id = str(action.get("action_id") or f"a{idx + 1}")
        next_action = actions[idx + 1] if idx + 1 < len(actions) else None
        cur_step = action.get("source_step")
        next_step = next_action.get("source_step") if next_action else None
        same_step_chain = next_action is not None and next_step == cur_step
        step_data = steps_by_no.get(int(cur_step)) if cur_step is not None else None
        after_bytes = step_data.get("after_bytes") if step_data else None
        base_lm: Dict[str, Any] = {
            "landmark_id": f"lm_{action_id}",
            "action_id": action_id,
            "after_action_index": int(action.get("index") or idx + 1),
            "before_action_id": next_action.get("action_id") if next_action else None,
            "before_action_index": (
                int(next_action.get("index"))
                if next_action and next_action.get("index")
                else None
            ),
            "timing": _landmark_timing(step_data, next_action, steps_by_no),
        }
        if same_step_chain or not after_bytes:
            landmarks.append(
                {
                    **base_lm,
                    "status": "unavailable",
                    "image_url": "",
                    "image_phash": "",
                    "image_sha256": "",
                    "missing_reason": (
                        "same_step_action_chain_no_handoff"
                        if same_step_chain
                        else "no_after_screenshot"
                    ),
                }
            )
            continue
        phash = compute_phash(after_bytes)
        sha = _hashlib.sha256(after_bytes).hexdigest()
        url = ""
        try:
            url = await upload_image(after_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("V2 landmark 图上传失败 action_id={}: {}", action_id, exc)
        if not url:
            landmarks.append(
                {
                    **base_lm,
                    "status": "unavailable",
                    "image_url": "",
                    "image_phash": "",
                    "image_sha256": "",
                    "missing_reason": "upload_failed",
                }
            )
            continue
        landmarks.append(
            {
                **base_lm,
                "status": "available",
                "image_url": url,
                "image_phash": f"{phash:064x}" if phash is not None else "",
                "image_sha256": sha,
                "image_size_bytes": len(after_bytes),
            }
        )
    return landmarks


def _landmark_timing(
    step_data: Optional[Dict[str, Any]],
    next_action: Optional[Dict[str, Any]],
    steps_by_no: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    """V2 landmark 时序（第一手）：action 起止 + 到下一动作的间隔（回放等待窗口用）。"""
    action_start = step_data.get("ts") if step_data else None
    elapsed = step_data.get("elapsed_ms") if step_data else None
    action_end = (
        int(action_start) + int(elapsed)
        if action_start is not None and elapsed is not None
        else None
    )
    next_start = None
    if next_action is not None and next_action.get("source_step") is not None:
        ns = steps_by_no.get(int(next_action.get("source_step")))
        next_start = ns.get("ts") if ns else None
    handoff_ts = step_data.get("after_ts") if step_data else None
    gap = (
        max(0, int(next_start) - action_end)
        if next_start is not None and action_end is not None
        else None
    )
    handoff_wait = (
        max(0, int(handoff_ts) - action_end)
        if handoff_ts is not None and action_end is not None
        else None
    )
    return {
        "action_start_ts_ms": action_start,
        "action_end_ts_ms": action_end,
        "handoff_snapshot_ts_ms": handoff_ts,
        "handoff_wait_ms": handoff_wait,
        "next_action_start_ts_ms": next_start,
        "gap_to_next_action_ms": gap,
    }


def _actions_from_steps(
    steps: List[Dict[str, Any]],
    *,
    source_vlm_backend: str = "",
    screen_size: Tuple[int, int] = (0, 0),
) -> List[Dict[str, Any]]:
    """把 recorder 每步的结构化动作转成 V3 source action（规范化字段 + abs 坐标）。

    **拆链**：一步多击展开成多条 action（与 next ``_build_actions_from_timeline`` 一致），
    不再合并。每条经 ``_action_from_parsed_raw`` 规范化成回放执行器认的字段
    （click/long_press 的 ``point{x,y}``、drag 的 ``start``/``end``、scroll 的
    ``amount``/``center``、open_app 的 ``app_name`` 等），并按 ``coord_space`` 转 abs
    （normalized→按屏幕换算 / absolute 原值，doubao/gpt/claude 三协议一致）。终止 /
    未知动作丢弃。``thought`` 挂在动作上供 ``_plan_intent_for_action`` 生成 plan_intent。
    """
    out: List[Dict[str, Any]] = []
    for s in steps:
        thought = str(s.get("thought") or "")
        raw_step = str(s.get("display_action") or "")
        vlm_screenshot_size = s.get("vlm_screenshot_size")
        for parsed_dict in s.get("actions") or []:
            if not isinstance(parsed_dict, dict):
                continue
            parsed = _rebuild_parsed(parsed_dict, raw=raw_step)
            action = _action_from_parsed_raw(
                len(out) + 1,
                parsed=parsed,
                raw=parsed.raw,
                screen_size=screen_size,
                vlm_screenshot_size=vlm_screenshot_size,
                source="agent_first_hand",
            )
            if action is None:  # 终止 / 未知动作不入缓存
                continue
            action["thought"] = thought
            action["action_id"] = f"a{s.get('step')}_{len(out) + 1}"
            action["source_step"] = s.get("step")  # V2 state_landmark 映射 after 截图用
            out.append(action)
    return out


def _rebuild_parsed(d: Dict[str, Any], *, raw: str = "") -> A.ParsedAction:
    """从 ``ParsedAction.to_dict()`` 重建 ParsedAction（埋点传的就是 to_dict）。

    注意 to_dict 的动作名字段是 ``action``（不是 ``type``），``coord_space`` 缺省即
    normalized（豆包系）、claude/gpt 的 absolute 会显式带上。
    """
    return A.ParsedAction(
        action=str(d.get("action") or d.get("type") or ""),
        point=d.get("point"),
        start_point=d.get("start_point"),
        end_point=d.get("end_point"),
        content=d.get("content"),
        direction=d.get("direction"),
        name=d.get("name"),
        seconds=d.get("seconds"),
        keycode=d.get("keycode"),
        scroll_amount=int(d.get("scroll_amount") or 1),
        raw=str(d.get("raw") or raw or ""),
        coord_space=str(d.get("coord_space") or "normalized"),
    )


def _action_from_parsed_raw(
    index: int,
    *,
    parsed: A.ParsedAction,
    raw: str,
    screen_size: Tuple[int, int],
    source: str,
    vlm_screenshot_size: Optional[Tuple[int, int]] = None,
) -> Optional[Dict[str, Any]]:
    """自 next ``service._action_from_parsed_raw`` 迁来：parsed → 回放执行器认的规范化
    action。三协议坐标统一经 ``_parsed_point_to_abs``，字段名与 ReplayActionDispatcher 对齐。
    """
    action = parsed.action
    base = {"index": index, "source": source, "raw": raw}
    if action in (A.ACTION_FINISHED, A.ACTION_ASSERT_FAIL):
        return None
    if not parsed.is_known:
        return None
    if action in (A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS):
        if not parsed.point:
            return None
        out = {
            **base,
            "type": action,
            "point": _parsed_point_to_abs(parsed.point, parsed.coord_space, screen_size, vlm_screenshot_size),
            "coord_mode": "absolute",
        }
        if action == A.ACTION_LONG_PRESS:
            out["duration_ms"] = 1000
        return out
    if action == A.ACTION_TYPE:
        return {**base, "type": action, "content": parsed.content or ""}
    if action == A.ACTION_WAIT:
        return {**base, "type": action, "seconds": int(parsed.seconds or 1)}
    if action == A.ACTION_SCROLL:
        out = {
            **base,
            "type": action,
            "direction": parsed.direction or "down",
            "amount": int(parsed.scroll_amount or 1),
        }
        if parsed.point:
            out["center"] = _parsed_point_to_abs(parsed.point, parsed.coord_space, screen_size, vlm_screenshot_size)
        return out
    if action == A.ACTION_DRAG:
        if not (parsed.start_point and parsed.end_point):
            return None
        return {
            **base,
            "type": action,
            "start": _parsed_point_to_abs(parsed.start_point, parsed.coord_space, screen_size, vlm_screenshot_size),
            "end": _parsed_point_to_abs(parsed.end_point, parsed.coord_space, screen_size, vlm_screenshot_size),
            "coord_mode": "absolute",
        }
    if action == A.ACTION_OPEN_APP:
        return {**base, "type": action, "app_name": parsed.name or ""}
    if action == A.ACTION_CLOSE_APP:
        return {**base, "type": action, "app_name": parsed.name or ""}
    if action in (A.ACTION_PRESS_HOME, A.ACTION_PRESS_BACK):
        return {**base, "type": action}
    if action == A.ACTION_KEY_EVENT:
        if parsed.keycode is None:
            return None
        return {**base, "type": action, "keycode": int(parsed.keycode)}
    if action == A.ACTION_TAKE_SCREENSHOT:
        # 无坐标/无文本的效果动作；仅携带 save_to_album 意图，回放时按原步骤
        # 再次保存一张新截图（方案 §9：每次回放存一张新图是预期行为）。
        return {**base, "type": action, "save_to_album": bool(parsed.save_to_album)}
    return None


def _parsed_point_to_abs(
    point: Iterable[int],
    coord_space: str,
    screen_size: Tuple[int, int],
    vlm_screenshot_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, int]:
    """坐标 → 设备像素。

    - normalized（豆包）：0-1000 按屏幕换算。
    - absolute（claude/gpt CU）：模型给的是相对「被 max_long_edge 缩过的截图」的像素，
      必须按 设备/截图 比例缩回设备坐标（与首跑 ``_vlm_point_to_abs`` 同源）；拿不到截图
      尺寸时回退原值。此前对 absolute 直接存原值，回放会把截图坐标当设备坐标、点偏约 35%。
    """
    x, y = [int(v) for v in list(point)[:2]]
    w, h = screen_size
    if coord_space == "absolute":
        if vlm_screenshot_size:
            sw = int(vlm_screenshot_size[0] or 0)
            sh = int(vlm_screenshot_size[1] or 0)
            if sw > 0 and sh > 0 and w > 0 and h > 0:
                return {"x": int(x * (w / sw)), "y": int(y * (h / sh))}
        return {"x": x, "y": y}
    if w > 0 and h > 0:
        ax, ay = A.vlm_point_to_abs(x, y, w, h)
        return {"x": int(ax), "y": int(ay)}
    return {"x": x, "y": y}


def _source_completion_from_steps(
    steps: List[Dict[str, Any]],
    *,
    run_reason: str = "",
    completion_logs: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """对齐 next ``_build_source_completion`` 口径：run_reason + task_done +
    final_thought + assertion_pass（供缓存断言消解业务别名）。数据来源换成第一手——
    run 终态 reason + 收尾日志（任务完成 / 断言系统·通过）+ 最后一步 thought，不读 DB。
    """
    logs = completion_logs or {}
    reason = normalize_run_semantic(run_reason)
    task_done = normalize_run_semantic(logs.get("任务完成", ""))
    final_thought = normalize_run_semantic(steps[-1].get("thought") if steps else "")
    assertion_pass = normalize_run_semantic(logs.get("断言系统 · 通过", ""))
    completion: Dict[str, str] = {}
    if reason:
        completion["run_reason"] = reason[:1200]
    if task_done and task_done != reason:
        completion["task_done"] = task_done[:1200]
    if final_thought:
        completion["final_thought"] = final_thought[:1200]
    if assertion_pass:
        completion["assertion_pass"] = assertion_pass[:1200]
    return completion


def _first_app_target(actions: List[Dict[str, Any]]) -> str:
    # 字段口径对齐 next：_action_from_parsed_raw 产出 open_app/close_app 用 app_name。
    for action in actions:
        if str(action.get("type") or "") in (A.ACTION_OPEN_APP, A.ACTION_CLOSE_APP):
            return str(action.get("app_name") or action.get("package") or "")
    return ""


# ---------------------------------------------------------------------------
# plan_intent 生成（自 server.trajectory_cache.v3_service 迁来，纯文本加工）
# ---------------------------------------------------------------------------
def _normalize_v3_action(action: Dict[str, Any], *, source_vlm_backend: str = "") -> Dict[str, Any]:
    normalized = dict(action)
    normalized.setdefault("role", "business_required")
    normalized["plan_intent"] = _plan_intent_for_action(
        normalized,
        source_vlm_backend=source_vlm_backend,
    )
    return _strip_v3_action_for_cache(normalized)


def _strip_v3_action_for_cache(action: Dict[str, Any]) -> Dict[str, Any]:
    """V3 cache 只落"实际 action 行为"+ plan_intent + audit；剔除 V2 业务标签 label。"""
    action.pop("label", None)
    return action


def _plan_intent_for_action(action: Dict[str, Any], *, source_vlm_backend: str = "") -> str:
    action_type = str(action.get("type") or "").strip()
    actual_target = _action_target_from_text(action.get("thought")) or _action_sentence_from_text(
        action.get("thought")
    )
    if action_type == A.ACTION_TYPE:
        target = actual_target or _candidate_text(action)
        if target:
            return _ensure_action_statement(target, "输入", fallback="输入文本")
        content = _clean_text(action.get("content") or action.get("text") or "")
        return f"输入{content}" if content else "输入文本"
    if action_type == A.ACTION_WAIT:
        if actual_target:
            return _ensure_action_statement(actual_target, "等待", fallback="等待页面稳定")
        seconds = action.get("seconds")
        return f"等待{seconds}秒" if seconds is not None else "等待页面稳定"
    if action_type == A.ACTION_OPEN_APP:
        if actual_target:
            return _ensure_action_statement(actual_target, "打开", fallback="打开应用")
        target = _clean_text(action.get("app") or action.get("name") or action.get("bundle_id") or "")
        return f"打开{target}" if target else "打开应用"
    if action_type == A.ACTION_CLOSE_APP:
        if actual_target:
            return _ensure_action_statement(actual_target, "关闭", fallback="关闭应用")
        target = _clean_text(action.get("app") or action.get("name") or action.get("bundle_id") or "")
        return f"关闭{target}" if target else "关闭应用"
    if action_type == A.ACTION_PRESS_BACK:
        if actual_target:
            return _ensure_action_statement(actual_target, "返回", fallback="返回")
        return "返回"
    if action_type == A.ACTION_PRESS_HOME:
        if actual_target:
            return _ensure_action_statement(actual_target, "返回", fallback="返回桌面")
        return "返回桌面"
    if action_type in {A.ACTION_SCROLL, A.ACTION_DRAG}:
        if actual_target:
            return _ensure_action_statement(actual_target, "滑动", fallback="滑动页面")
        direction = _clean_text(action.get("direction") or "")
        target = _candidate_text(action)
        if direction:
            return f"向{direction}滑动{target}".strip()
        return _ensure_verb(target, "滑动", fallback="滑动页面")
    if action_type in {A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS}:
        verb = {
            A.ACTION_CLICK: "点击",
            A.ACTION_DOUBLE_TAP: "双击",
            A.ACTION_LONG_PRESS: "长按",
        }[action_type]
        return _click_plan_intent(action, verb, source_vlm_backend=source_vlm_backend)
    return _ensure_verb(_candidate_text(action), "点击", fallback=f"执行{action_type or '动作'}")


def _candidate_text(action: Dict[str, Any], *, prefer_thought: bool = False) -> str:
    keys = ("thought", "label", "intent", "raw") if prefer_thought else ("label", "intent", "thought", "raw")
    for key in keys:
        text = _usable_semantic_text(action.get(key))
        if text:
            return text
    return ""


def _click_plan_intent(action: Dict[str, Any], verb: str, *, source_vlm_backend: str = "") -> str:
    """生成给 V3 定位器使用的短动作语义。首跑 thought 里的动作短句最接近"实际点了什么"。"""
    label = _usable_semantic_text(action.get("label"))
    intent = _usable_semantic_text(action.get("intent"))
    thought_action = _action_target_from_text(action.get("thought")) or _action_sentence_from_text(
        action.get("thought")
    )
    raw = _usable_semantic_text(action.get("raw"))
    if thought_action:
        return _compose_action_statement(verb, thought_action, fallback=f"{verb}目标元素")

    target = label or intent or raw
    purpose = intent if label and intent and not _same_semantic(label, intent) else ""
    return _compose_action_statement(verb, target, purpose=purpose, fallback=f"{verb}目标元素")


def _usable_semantic_text(value: Any) -> str:
    text = _clean_text(value)
    if not text or _is_noisy_plan_text(text):
        return ""
    action_sentence = _action_sentence_from_text(text)
    if len(text) > 140 and action_sentence:
        return action_sentence
    if len(text) > 180:
        return ""
    return text


def _action_sentence_from_text(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        candidate = _clean_text(sentence)
        if not candidate or _is_noisy_plan_text(candidate):
            continue
        if _ACTION_VERB_RE.search(candidate):
            return candidate[:140]
    if (
        len(text) <= 80
        and _ACTION_VERB_RE.search(text)
        and not _is_noisy_plan_text(text)
    ):
        return text[:140]
    return ""


def _action_target_from_text(value: Any) -> str:
    sentence = _action_sentence_from_text(value)
    if not sentence:
        return ""
    for pattern in _ACTION_TARGET_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        target = _clean_text(match.group("target"))
        target = re.split(
            r"(?:，|。|；|;|,|\bto\b|\bin order to\b|\bso that\b|\bso\b|\bthen\b|\bfirst\b|这样|从而|然后|才能|就能|以便|来)",
            target,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        target = _clean_text(target)
        if target and not _is_noisy_plan_text(target):
            return target[:120]
    return ""


def _is_noisy_plan_text(text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return True
    return bool(_NOISY_PLAN_TEXT_RE.search(cleaned))


def _compose_action_statement(verb: str, target: str, *, purpose: str = "", fallback: str) -> str:
    base = _ensure_action_statement(target, verb, fallback=fallback)
    purpose = _usable_semantic_text(purpose)
    if not purpose or _same_semantic(base, purpose):
        return base
    if len(base) + len(purpose) + 1 > 140:
        return base
    return f"{base}，{purpose}"[:140]


def _same_semantic(left: str, right: str) -> bool:
    left_norm = _semantic_fingerprint(left)
    right_norm = _semantic_fingerprint(right)
    if not left_norm or not right_norm:
        return False
    return left_norm in right_norm or right_norm in left_norm


def _semantic_fingerprint(text: str) -> str:
    text = _clean_text(text).lower()
    for token in ("点击", "轻点", "选择", "按下", "tap", "click", "press", "select"):
        text = text.replace(token, "")
    return re.sub(r"[\s\"'“”‘’「」《》()（）\[\]{}。，,;；:：.!?！？_-]+", "", text)


def _ensure_verb(text: str, verb: str, *, fallback: str) -> str:
    text = _clean_text(text)
    if not text:
        return fallback
    if text.startswith(("点击", "关闭", "打开", "选择", "输入", "滑动", "长按", "双击", "返回")):
        return text[:80]
    return f"{verb}{text}"[:80]


def _ensure_action_statement(text: str, verb: str, *, fallback: str) -> str:
    text = _clean_text(text)
    if not text:
        return fallback
    sentence = _action_sentence_from_text(text)
    if sentence and len(text) > 140:
        text = sentence
    if _CLICK_LIKE_VERB_RE.search(text) or _LEADING_ACTION_VERB_RE.search(text):
        return text[:160]
    return f"{verb}{text}"[:160]


def _clean_text(value: Any) -> str:
    text = _WS_RE.sub(" ", str(value or "").replace("\u3000", " ")).strip()
    return text.strip(" ，。；;:：.!?！？")


# ---------------------------------------------------------------------------
# plan_intent 模型清洗（自 server.trajectory_cache.v3_service 迁来，Agent 直连 VLM）。
# next 默认启用（.env 实配 ephemeral_classifier=doubao-seed-1-6）；本侧在归档后台 task
# 内 await，不阻塞 case 完成。未配置 / 调用失败 / 与规则候选冲突时回退保留规则候选。
# ---------------------------------------------------------------------------
async def _clean_v3_plan_intents(*, payload: Dict[str, Any], goal: str) -> None:
    cleaner = V3PlanIntentCleaner()
    if not cleaner.is_configured():
        logger.info(
            "V3 plan cleaner 未配置，plan_intent 用规则兜底：{}", cleaner.configuration_problem()
        )
        return
    actions = list(payload.get("actions") or [])
    cleaned = 0
    rejected = 0
    for action in actions:
        rule_plan_intent = _clean_text(action.get("plan_intent") or "")
        try:
            result = await cleaner.clean_action(action=action, goal=goal)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "V3 plan cleaner 调用失败，规则兜底 action_id={}: {}",
                action.get("action_id"),
                exc,
            )
            continue
        plan_intent = _clean_text(result.get("plan_intent") or "")
        if not plan_intent:
            continue
        if not _should_accept_cleaned_plan_intent(action, plan_intent, rule_plan_intent):
            action["plan_intent_meta"] = {
                "source": "v3_plan_cleaner_rejected",
                "rejected_plan_intent": plan_intent[:120],
                "kept_plan_intent": rule_plan_intent[:120],
                "reason": str(result.get("reason") or "")[:300],
                "confidence": _safe_float(result.get("confidence"), default=0.0),
            }
            rejected += 1
            continue
        action["plan_intent"] = plan_intent[:120]
        action["plan_intent_meta"] = {
            "source": "v3_plan_cleaner",
            "reason": str(result.get("reason") or "")[:300],
            "confidence": _safe_float(result.get("confidence"), default=0.0),
        }
        cleaned += 1
    meta = payload.setdefault("meta", {})
    if cleaned:
        meta["plan_intent_cleaner"] = "model"
        meta["plan_intent_cleaned_actions"] = cleaned
    if rejected:
        meta["plan_intent_cleaner_rejected_actions"] = rejected


class V3PlanIntentCleaner:
    """保存阶段的 V3 plan_intent 模型清洗器（自 next 迁来，Agent 直连 VLM）。"""

    def __init__(self, *, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    def _config(self) -> "tuple[str, str, str, str, float]":
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

    def is_configured(self) -> bool:
        backend, api_url, api_key, model, _timeout = self._config()
        return bool(backend and api_url and api_key and model)

    def configuration_problem(self) -> str:
        backend, api_url, api_key, model, _timeout = self._config()
        missing = []
        if not backend:
            missing.append("backend")
        if not api_url:
            missing.append("api_url")
        if not api_key:
            missing.append("api_key")
        if not model:
            missing.append("model")
        return f"plan cleaner 配置缺失：{','.join(missing)}" if missing else ""

    async def clean_action(self, *, action: Dict[str, Any], goal: str = "") -> Dict[str, Any]:
        backend, api_url, api_key, model, timeout_sec = self._config()
        started = time.monotonic()
        text = await _call_vlm_with_images(
            backend=backend,
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            system="你是 V3 轨迹缓存的动作语义清洗器。只输出 JSON，不要 markdown。",
            prompt=build_v3_plan_cleaner_prompt(action=action, goal=goal),
            images=[],
        )
        data = _extract_json_object(text)
        if not isinstance(data, dict):
            raise ValueError(f"plan cleaner 输出不是 JSON: {text[:160]}")
        data["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return data


def build_v3_plan_cleaner_prompt(*, action: Dict[str, Any], goal: str = "") -> str:
    """V3 cleaner 极简 prompt（自 next 迁来，逐字保留以对齐清洗效果）。"""
    goal_text = (goal or "").strip() or "（未提供，按 thought 自身决定泛化粒度）"
    return (
        "请把一次成功轨迹中的当前 action 清洗成 V3 回放用的 plan_intent。\n"
        "plan_intent 是下次回放时给定位模型使用的目标短语，定位模型会拿当前截图 + 这条短语去找控件。\n"
        "因此 plan_intent 只描述「当前 action 这一步真正在做什么」，不写下一步、不写业务结果、不写页面状态。\n\n"
        f"用户原始目标：{goal_text}\n"
        f"当前 action：{_json_dumps_compact(_v3_action_brief(action))}\n\n"
        "生成规则：\n"
        "1. plan_intent 必须以中文动词开头：点击 / 输入 / 关闭 / 打开 / 选择 / 切换 / 滑动 / 长按 / 双击 / 返回 / 等待。\n"
        "2. thought 是英文时必须翻译为中文动词短语，禁止把英文整句照搬到 plan_intent。\n"
        "3. 截图上稳定可见的 UI 原文（按钮文字、标签名、输入框 placeholder、菜单项、品牌/产品名）\n"
        "   无论中英文都按原文照写，不翻译、不意译、不大小写改写，以便定位模型逐字符搜索。\n"
        "4. 状态 / 反思 / 完成时态描述（典型标记：has been / have been / I've / I'm / appeared /\n"
        "   not yet / opened but / is already / indicating / the page shows / the dialog has /\n"
        "   现在屏幕 / 已经 / 刚刚），说明模型在描述「屏幕现状」或「刚做了什么」，\n"
        "   不是下一步动作；必须从中识别真正被点按 / 被输入 / 被关闭的控件后用中文动词重写。\n"
        "5. 是否保留 thought 里出现的具体文案，按「文案稳定性 × goal 粒度」两维度联合判断：\n"
        "   维度 A（最高优先级）：用户原始目标已直接给出某个具体控件文案 →\n"
        "     plan_intent 用 goal 的具体文案；忽略 thought 里出现的不同文案。\n"
        "   维度 B：用户原始目标是泛化指代（序号 / 位置 / 数量 / 语义化指代）时，\n"
        "     按 thought 里这段文案的「屏幕稳定性」分流：\n"
        "     B1. 稳定 UI 锚点（保留 thought 这段具体文案）\n"
        "         典型形态：应用自带的固定 UI 元素文字 ——\n"
        "           · 顶 / 底 / 侧 导航栏 tab 名；\n"
        "           · 系统级或应用级的标准动作按钮（确定 / 取消 / 返回 / 发送 等）；\n"
        "           · 模态 / 弹窗 / 输入框的固定标题或 placeholder；\n"
        "           · 应用内固定的功能名 / 版块名 / 产品代号。\n"
        "         判定特征：每次启动应用都在同一位置出现，不依赖当时屏幕数据。\n"
        "     B2. 动态屏幕内容（不保留，按 goal 的泛化粒度写，例如「点击第一个 X」）\n"
        "         典型形态：当时屏幕上恰好显示的数据 ——\n"
        "           · 列表条目 / 卡片标题 / feed 流条目；\n"
        "           · 用户生成内容（消息 / 评论 / 订单号 / 搜索历史）；\n"
        "           · 随时间或后端数据变化的展示文字。\n"
        "         判定特征：换设备 / 换日期 / 换用户进来这段文字会变。\n"
        "     B3. 不确定时 → 按 B2 处理，宁可保守泛化，避免下次回放因屏幕内容变化而无法定位。\n"
        "6. thought 决定操作的控件（次优先级）：plan_intent 描述哪个控件，由 thought\n"
        "   决定（哪个按钮 / 哪个卡片 / 哪个输入框）；用户原始目标只决定描述粒度，\n"
        "   不决定操作哪个控件。如果 thought 描述的控件和用户原始目标无关\n"
        "   （首跑可能多了清障 / 中转动作），按 thought 写真实操作的控件即可，\n"
        "   不要硬把用户原始目标塞进 plan_intent。\n"
        "7. 不输出下一步、不输出业务结果、不输出原因分析、不输出页面状态、不输出坐标。\n"
        "8. 不确定时按 thought 里能识别到的「控件类型 + 大致位置」保守输出，不要加戏；\n"
        "   thought 完全无法识别任何控件信息时返回空字符串，由系统兜底，禁止凭空捏造或输出占位短语。\n\n"
        "只输出 JSON：\n"
        "{\n"
        '  "plan_intent": "目标控件短语",\n'
        '  "confidence": 0.0,\n'
        '  "reason": "一句话说明为什么这样洗"\n'
        "}\n"
    )


def _v3_action_brief(action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """V3 cleaner 最小输入：只暴露"实际 action 行为"（type + thought），避免噪点引偏。"""
    if not action:
        return {}
    brief: Dict[str, Any] = {}
    if action.get("type") not in (None, ""):
        brief["type"] = action.get("type")
    if action.get("thought") not in (None, ""):
        brief["thought"] = action.get("thought")
    return brief


def _should_accept_cleaned_plan_intent(
    action: Dict[str, Any],
    cleaned: str,
    rule_candidate: str,
) -> bool:
    cleaned = _clean_text(cleaned)
    rule_candidate = _clean_text(rule_candidate)
    if not cleaned:
        return False
    if not rule_candidate:
        return True
    # 规则候选自己就是垃圾（典型：海外模型英文陈述句被规则误抓），不能用它否决 cleaner。
    if not _rule_candidate_quality_ok(rule_candidate):
        return True
    if _same_semantic(cleaned, rule_candidate):
        return True
    cleaned_tokens = _latin_tokens(cleaned)
    rule_tokens = _latin_tokens(rule_candidate)
    if cleaned_tokens and rule_tokens and cleaned_tokens.isdisjoint(rule_tokens):
        return False
    return True


def _rule_candidate_quality_ok(rule_candidate: str) -> bool:
    if not rule_candidate:
        return False
    if _is_noisy_plan_text(rule_candidate):
        return False
    if len(rule_candidate) > 60:
        return False
    if re.search(
        r"\b(?:has|have|had)\s+been\b|\bi['\u2019]?ve\b|\bi['\u2019]?m\s+(?:at|in|on|not)\b|"
        r"\bappeared\b|\bopened\s+but\b|\bnot\s+yet\b|\bis\s+already\b|\bindicating\b",
        rule_candidate,
        re.IGNORECASE,
    ):
        return False
    return True


def _latin_tokens(text: str) -> set:
    return {
        token.lower()
        for token in _LATIN_TOKEN_RE.findall(_clean_text(text))
        if len(token) >= 2 and token.lower() not in _PLAN_STOPWORDS
    }


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "V3_CACHE_SCHEMA_VERSION",
    "build_v1_archive",
    "build_v2_archive",
    "build_v3_archive",
]
