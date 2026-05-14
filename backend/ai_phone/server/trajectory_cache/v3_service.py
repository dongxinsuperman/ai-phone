"""V3 语义坐标回放缓存保存 / 查询。

V3 复用 V2 的 source action 清洗与瞬态角色标记，但在独立表中保存
``plan_intent``。回放时旧坐标只作审计，正常路径用 ``plan_intent`` 重新识别坐标。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import Run, VlmTrajectoryCacheV3
from ai_phone.server.trajectory_cache.ephemeral import ROLE_BUSINESS_REQUIRED
from ai_phone.server.trajectory_cache.service import (
    _build_trajectory,
    _write_log,
    build_cache_key,
)
from ai_phone.shared import actions as A

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
    r"verdict|traceback|exception|error=|raw=|thought:|action:",
    re.IGNORECASE,
)
_COMPUTER_USE_BACKEND_RE = re.compile(r"(claude|gpt).*_cu|computer[_-]?use", re.IGNORECASE)


async def save_trajectory_cache_v3_after_success(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> Optional[str]:
    """成功 Run 结束后保存 V3 cache。

    V3 与 V2 分表，不覆盖 V2 cache。返回 V3 cache_key；不可保存时返回 None。
    """
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None or run.status != "success":
            return None
        reason = str(run.reason or "")
        if reason.startswith(("trajectory_cache_pass:", "trajectory_cache_v3_pass:")):
            await _write_log(
                session,
                run_id,
                level=1,
                title="V3轨迹缓存",
                content="缓存通道成功，不覆盖 V3 轨迹缓存",
            )
            await session.commit()
            return None

        device_code = str(run.device_serial or "").strip()
        if not device_code:
            return None

        cache_key, normalized_goal, semantic_hash = build_cache_key(
            device_code=device_code,
            run_semantic_text=run.goal,
            schema_version=V3_CACHE_SCHEMA_VERSION,
        )
        source = await _build_trajectory(
            session,
            run,
            cache_key,
            normalized_goal,
            semantic_hash,
            cache_mode="v3",
            schema_version=V3_CACHE_SCHEMA_VERSION,
        )
        v3_payload = build_v3_cache_payload(source)
        actions = v3_payload.get("actions") or []
        if not actions:
            await _write_log(
                session,
                run_id,
                level=2,
                title="V3轨迹缓存",
                content="成功 Run 未清洗出可回放 action，跳过 V3 缓存保存",
            )
            await session.commit()
            return None

        now = datetime.now(timezone.utc)
        row = (
            await session.execute(
                select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
            )
        ).scalars().first()
        if row is None:
            row = VlmTrajectoryCacheV3(cache_key=cache_key)
            session.add(row)

        row.device_code = device_code
        row.run_semantic_hash = semantic_hash
        row.run_semantic_text = normalized_goal
        row.case_id = run.case_id
        row.platform = str(v3_payload.get("platform") or "")
        row.resolution = str(v3_payload.get("resolution") or "")
        row.app_package_or_bundle = str(v3_payload.get("app_package_or_bundle") or "")
        row.schema_version = V3_CACHE_SCHEMA_VERSION
        row.status = "active"
        row.source_run_id = run.id
        row.source_vlm_backend = str(v3_payload.get("source_vlm_backend") or "")
        row.actions_json = actions
        row.source_completion = v3_payload.get("source_completion") or {}
        row.meta_json = v3_payload.get("meta") or {}
        row.updated_at = now
        row.last_success_at = now

        await _write_log(
            session,
            run_id,
            level=1,
            title="V3轨迹缓存",
            content=(
                f"已保存 V3 轨迹缓存 cache_key={cache_key[:12]} "
                f"actions={len(actions)} device_code={device_code}"
            ),
        )
        await session.commit()
        logger.info(
            "V3 轨迹缓存已保存 run_id={} cache_key={} actions={}",
            run_id,
            cache_key,
            len(actions),
        )
        return cache_key


async def get_active_trajectory_cache_v3(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
) -> Optional[Dict[str, Any]]:
    normalized_device = str(device_code or "").strip()
    if not normalized_device:
        return None
    cache_key, _normalized, _semantic_hash = build_cache_key(
        device_code=normalized_device,
        run_semantic_text=run_semantic_text,
        schema_version=V3_CACHE_SCHEMA_VERSION,
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(VlmTrajectoryCacheV3).where(
                    VlmTrajectoryCacheV3.cache_key == cache_key,
                    VlmTrajectoryCacheV3.status == "active",
                )
            )
        ).scalars().first()
        return row.to_dict() if row is not None else None


async def delete_trajectory_cache_v3_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> int:
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return 0
        device_code = str(run.device_serial or "").strip()
        if not device_code:
            return 0
        cache_key, _normalized, _semantic_hash = build_cache_key(
            device_code=device_code,
            run_semantic_text=run.goal,
            schema_version=V3_CACHE_SCHEMA_VERSION,
        )
        result = await session.execute(
            delete(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
        )
        deleted = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=1,
            title="V3轨迹缓存",
            content=f"case 失败已触发 V3 缓存删除 cache_key={cache_key[:12]} deleted={deleted}",
        )
        await session.commit()
        return deleted


async def mark_trajectory_cache_v3_suspect(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cache_key: str,
    run_id: str,
    reason: str,
) -> int:
    """把命中但复跑/断言失败的 V3 cache 标成 suspect，避免继续被命中。"""

    normalized_key = str(cache_key or "").strip()
    if not normalized_key:
        return 0
    async with session_factory() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            update(VlmTrajectoryCacheV3)
            .where(VlmTrajectoryCacheV3.cache_key == normalized_key)
            .values(
                status="suspect",
                last_failed_at=now,
                updated_at=now,
            )
        )
        changed = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=2,
            title="V3轨迹缓存",
            content=(
                f"已标记 V3 cache suspect cache_key={normalized_key[:12]} "
                f"changed={changed} reason={reason[:160]}"
            ),
        )
        await session.commit()
        return changed


def build_v3_cache_payload(source: Dict[str, Any]) -> Dict[str, Any]:
    source_vlm_backend = str(source.get("source_vlm_backend") or "")
    actions = [
        _normalize_v3_action(action, source_vlm_backend=source_vlm_backend)
        for action in list(source.get("actions") or [])
    ]
    return {
        "mode": "v3",
        "schema_version": V3_CACHE_SCHEMA_VERSION,
        "cache_key": source.get("cache_key") or "",
        "device_code": source.get("device_code") or "",
        "run_semantic_hash": source.get("run_semantic_hash") or "",
        "run_semantic_text": source.get("run_semantic_text") or "",
        "case_id": source.get("case_id"),
        "platform": source.get("platform") or "",
        "resolution": source.get("resolution") or "",
        "app_package_or_bundle": source.get("app_package_or_bundle") or "",
        "source_run_id": source.get("source_run_id") or "",
        "source_vlm_backend": source.get("source_vlm_backend") or "",
        "actions": actions,
        "source_completion": source.get("source_completion") or {},
        "meta": {
            "source_schema_version": source.get("schema_version"),
            "state_landmarks_available": bool(source.get("state_landmarks")),
        },
    }


def _normalize_v3_action(action: Dict[str, Any], *, source_vlm_backend: str = "") -> Dict[str, Any]:
    normalized = dict(action)
    normalized.setdefault("role", ROLE_BUSINESS_REQUIRED)
    normalized["plan_intent"] = _plan_intent_for_action(
        normalized,
        source_vlm_backend=source_vlm_backend,
    )
    return normalized


def _plan_intent_for_action(action: Dict[str, Any], *, source_vlm_backend: str = "") -> str:
    action_type = str(action.get("type") or "").strip()
    if action_type == A.ACTION_TYPE:
        content = _clean_text(action.get("content") or action.get("text") or "")
        return f"输入{content}" if content else "输入文本"
    if action_type == A.ACTION_WAIT:
        seconds = action.get("seconds")
        return f"等待{seconds}秒" if seconds is not None else "等待页面稳定"
    if action_type == A.ACTION_OPEN_APP:
        target = _clean_text(action.get("app") or action.get("name") or action.get("bundle_id") or "")
        return f"打开{target}" if target else "打开应用"
    if action_type == A.ACTION_CLOSE_APP:
        target = _clean_text(action.get("app") or action.get("name") or action.get("bundle_id") or "")
        return f"关闭{target}" if target else "关闭应用"
    if action_type == A.ACTION_PRESS_BACK:
        return "返回"
    if action_type == A.ACTION_PRESS_HOME:
        return "返回桌面"
    if action_type in {A.ACTION_SCROLL, A.ACTION_DRAG}:
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
    """生成给 V3 定位器使用的短动作语义。

    V3 保存的是下一次可执行的目标，不是首跑模型的完整推理记录。Computer Use
    后端的 thought 常包含长英文观察/裁决文本，所以只在缺少 label/intent 时把
    thought 中的动作短句作为兜底；普通后端也会先过滤掉非动作元信息。
    """
    label = _usable_semantic_text(action.get("label"))
    intent = _usable_semantic_text(action.get("intent"))
    thought_action = _action_sentence_from_text(action.get("thought"))
    raw = _usable_semantic_text(action.get("raw"))
    computer_use = bool(_COMPUTER_USE_BACKEND_RE.search(source_vlm_backend or ""))

    if computer_use:
        target = label or intent or thought_action or raw
        purpose = intent if label and intent and not _same_semantic(label, intent) else ""
        return _compose_action_statement(verb, target, purpose=purpose, fallback=f"{verb}目标元素")

    if thought_action and _should_prefer_thought_action(thought_action, label=label, intent=intent):
        return _ensure_action_statement(thought_action, verb, fallback=f"{verb}目标元素")

    target = label or intent or thought_action or raw
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
    if _ACTION_VERB_RE.search(text) and not _is_noisy_plan_text(text):
        return text[:140]
    return ""


def _is_noisy_plan_text(text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return True
    return bool(_NOISY_PLAN_TEXT_RE.search(cleaned))


def _should_prefer_thought_action(thought_action: str, *, label: str, intent: str) -> bool:
    if not thought_action:
        return False
    if not label and not intent:
        return True
    if len(thought_action) > 90:
        return False
    if _same_semantic(thought_action, label) or _same_semantic(thought_action, intent):
        return False
    # 短 thought 明确描述了清障/切换等实际动作时，优先保留首跑真实动作语义。
    return bool(_CLICK_LIKE_VERB_RE.search(thought_action) and re.search(r"关闭|遮挡|弹窗|close|dismiss", thought_action, re.IGNORECASE))


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
