"""轨迹缓存保存 / 删除 / 清洗。

第一阶段原则：
- key = 设备唯一码 + run 语义强匹配；
- 成功 run 才保存；
- 任意失败路径都尝试删除，空删除不报错；
- 优先从 RunCommand.params 拿真实 driver 参数，拿不到再解析 RunStep.action。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.shared import actions as A
from ai_phone.config import get_settings
from ai_phone.agent.runner.phash import compute_phash
from ai_phone.server.models import Device, Run, RunCommand, RunLog, RunStep, VlmTrajectoryCache
from ai_phone.server.trajectory_cache.action_adapters import parse_cache_action
from ai_phone.server.trajectory_cache.ephemeral import (
    ROLE_BUSINESS_REQUIRED,
    ROLE_OPTIONAL_EPHEMERAL,
    CacheEphemeralActionClassifier,
    EphemeralClassification,
)

CACHE_SCHEMA_VERSION = 2
_WS_RE = re.compile(r"\s+")
_EXEC_ACTION_RE = re.compile(r"动作\s*[:：]\s*([^,，]+)")
_STRUCTURED_FIELD_RE = re.compile(
    r"(测试标题|前置条件|操作步骤|预期结果|期望结果)\s*[:：]"
)
_ACTION_VERB_RE = re.compile(
    # 中英文双语动词集——豆包系 thought 是中文，claude_cu / gpt_cu 系 thought
    # 是英文（含 thinking + cleaned_text 拼接），需要同时覆盖才能从 claude/gpt
    # thought 里抽出 intent 句子。英文动词来自 Claude / OpenAI computer-use
    # 工具的常见动作名（click / left_click / type / swipe / scroll / drag / 
    # long_press / double_click / press_back / press_home / launch / wait 等）。
    r"("
    r"点击|轻点|输入|打开|关闭|选择|切换|返回|滑动|上滑|下滑|左滑|右滑|长按|双击|等待|勾选|取消|进入|"
    r"tap|click|type|enter|swipe|scroll|drag|long[_\s-]?press|double[_\s-]?(?:click|tap)|"
    r"press|launch|open|close|wait|select|toggle|back|home|navigate"
    r")",
    re.IGNORECASE,
)


def normalize_run_semantic(text: str | None) -> str:
    """确定性语义归一化：保守强匹配，不做同义改写。"""
    raw = "" if text is None else str(text)
    raw = raw.replace("\u3000", " ")
    return _WS_RE.sub(" ", raw.strip())


def run_semantic_hash(text: str | None) -> str:
    normalized = normalize_run_semantic(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_cache_key(
    *,
    device_code: str,
    run_semantic_text: str,
    schema_version: int = CACHE_SCHEMA_VERSION,
) -> Tuple[str, str, str]:
    """返回 ``(cache_key, normalized_text, semantic_hash)``。"""
    normalized = normalize_run_semantic(run_semantic_text)
    semantic_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    material = f"{device_code}:{semantic_hash}:v{schema_version}"
    cache_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return cache_key, normalized, semantic_hash


async def save_trajectory_cache_after_success(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> Optional[str]:
    """成功 run 结束后清洗并保存轨迹缓存。

    返回 cache_key；如果 run 不存在、非 success、没有可回放动作则返回 None。
    调用方应捕获异常，本函数内部也尽量只抛真正的数据库异常。
    """
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return None
        if run.status != "success":
            return None
        if str(run.reason or "").startswith("trajectory_cache_pass:"):
            await _write_log(
                session,
                run_id,
                level=1,
                title="轨迹缓存",
                content="缓存通道成功，不覆盖已有轨迹缓存",
            )
            await session.commit()
            return None

        device_code = str(run.device_serial or "").strip()
        if not device_code:
            return None

        cache_key, normalized_goal, semantic_hash = build_cache_key(
            device_code=device_code,
            run_semantic_text=run.goal,
        )
        trajectory = await _build_trajectory(session, run, cache_key, normalized_goal, semantic_hash)
        actions = trajectory.get("actions") or []
        if not actions:
            await _write_log(
                session,
                run_id,
                level=2,
                title="轨迹缓存",
                content="成功 Run 未清洗出可回放 action，跳过缓存保存",
            )
            await session.commit()
            return None

        now = datetime.now(timezone.utc)
        row = (
            await session.execute(
                select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
            )
        ).scalars().first()
        if row is None:
            row = VlmTrajectoryCache(cache_key=cache_key)
            session.add(row)

        row.device_code = device_code
        row.run_semantic_hash = semantic_hash
        row.run_semantic_text = normalized_goal
        row.case_id = run.case_id
        row.platform = trajectory.get("platform") or ""
        row.resolution = trajectory.get("resolution") or ""
        row.app_package_or_bundle = trajectory.get("app_package_or_bundle") or ""
        row.schema_version = CACHE_SCHEMA_VERSION
        row.status = "active"
        row.source_run_id = run.id
        row.trajectory_json = trajectory
        row.updated_at = now
        row.last_success_at = now

        await _write_log(
            session,
            run_id,
            level=1,
            title="轨迹缓存",
            content=(
                f"已保存轨迹缓存 cache_key={cache_key[:12]} "
                f"actions={len(actions)} device_code={device_code}"
            ),
        )
        await session.commit()
        logger.info(
            "轨迹缓存已保存 run_id={} cache_key={} actions={}",
            run_id,
            cache_key,
            len(actions),
        )
        return cache_key


async def get_active_trajectory_cache(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
) -> Optional[Dict[str, Any]]:
    """按 device_code + run 语义强匹配查询 active 轨迹缓存。"""
    normalized_device = str(device_code or "").strip()
    if not normalized_device:
        return None
    cache_key, _normalized, _semantic_hash = build_cache_key(
        device_code=normalized_device,
        run_semantic_text=run_semantic_text,
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(VlmTrajectoryCache).where(
                    VlmTrajectoryCache.cache_key == cache_key,
                    VlmTrajectoryCache.status == "active",
                )
            )
        ).scalars().first()
        return row.to_dict() if row is not None else None


async def delete_trajectory_cache_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> int:
    """按 run 的 device_code + run_semantic_hash 删除缓存。

    删除允许为空；run 不存在也返回 0。失败路径调用方不需要区分原因。
    """
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
        )
        result = await session.execute(
            delete(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
        deleted = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=1,
            title="轨迹缓存",
            content=(
                f"case 失败已触发缓存删除 cache_key={cache_key[:12]} "
                f"deleted={deleted}"
            ),
        )
        await session.commit()
        if deleted:
            logger.info("轨迹缓存已删除 run_id={} cache_key={} deleted={}", run_id, cache_key, deleted)
        return deleted


async def _build_trajectory(
    session: AsyncSession,
    run: Run,
    cache_key: str,
    normalized_goal: str,
    semantic_hash: str,
) -> Dict[str, Any]:
    device = await session.get(Device, run.device_serial)
    screen_w = int(getattr(device, "screen_width", 0) or 0)
    screen_h = int(getattr(device, "screen_height", 0) or 0)
    platform = str(getattr(device, "platform", "") or "")
    resolution = f"{screen_w}x{screen_h}" if screen_w and screen_h else ""

    steps = (
        await session.execute(
            select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.step, RunStep.id)
        )
    ).scalars().all()
    logs = (
        await session.execute(
            select(RunLog).where(RunLog.run_id == run.id).order_by(RunLog.ts, RunLog.id)
        )
    ).scalars().all()
    commands = (
        await session.execute(
            select(RunCommand).where(RunCommand.run_id == run.id).order_by(RunCommand.id)
        )
    ).scalars().all()
    intents = _split_goal_intents(run.goal)
    structured_goal = _is_structured_goal_text(run.goal)
    source_vlm_backend = _source_vlm_backend(run)

    actions = _build_actions_from_timeline(
        logs=logs,
        commands=commands,
        screen_size=(screen_w, screen_h),
        intents=intents,
        structured_goal=structured_goal,
        source_vlm_backend=source_vlm_backend,
    )
    if not actions:
        actions = _build_actions_from_steps(
            steps=steps,
            commands=commands,
            screen_size=(screen_w, screen_h),
            intents=intents,
            structured_goal=structured_goal,
            source_vlm_backend=source_vlm_backend,
        )
    _assign_action_identity(actions)
    state_landmarks = _build_state_landmarks(
        actions=actions,
        steps=steps,
        logs=logs,
        commands=commands,
    )
    await _classify_ephemeral_actions(
        session=session,
        run=run,
        actions=actions,
        steps=steps,
        state_landmarks=state_landmarks,
    )
    source_completion = _build_source_completion(run, logs)

    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "device_code": run.device_serial,
        "run_semantic_hash": semantic_hash,
        "run_semantic_text": normalized_goal,
        "case_id": run.case_id,
        "platform": platform,
        "resolution": resolution,
        "app_package_or_bundle": _first_app_target(actions),
        "source_run_id": run.id,
        "source_vlm_backend": source_vlm_backend,
        "actions": actions,
        "state_landmarks": state_landmarks,
        "source_completion": source_completion,
    }


async def _classify_ephemeral_actions(
    *,
    session: AsyncSession,
    run: Run,
    actions: List[Dict[str, Any]],
    steps: Sequence[RunStep],
    state_landmarks: Sequence[Dict[str, Any]],
) -> None:
    """给新保存的 V2 action 补 ``role`` / ``ephemeral_meta``。

    关闭总开关时完全不写新字段，保证旧 V2 行为和缓存结构不变。开启后所有
    action 默认都是 ``business_required``，只有 classifier 高置信度通过的
    非业务瞬态遮挡动作才标为 ``optional_ephemeral``。
    """
    settings = get_settings()
    if not bool(settings.trajectory_cache_ephemeral_action_enabled):
        return

    for action in actions:
        action.setdefault("role", ROLE_BUSINESS_REQUIRED)

    classifier = CacheEphemeralActionClassifier(settings=settings)
    if not classifier.is_enabled():
        await _write_log(
            session,
            run.id,
            level=1,
            title="轨迹缓存瞬态标记",
            content="ephemeral classifier 未启用，仅写入 business_required 默认角色",
        )
        return
    if not classifier.is_configured():
        await _write_log(
            session,
            run.id,
            level=2,
            title="轨迹缓存瞬态标记",
            content=f"classifier 配置不完整：{classifier.configuration_problem()}",
        )
        return

    steps_by_no = {int(step.step): step for step in steps}
    landmarks_by_action_id = {
        str(item.get("action_id")): item
        for item in state_landmarks
        if str(item.get("action_id") or "")
    }
    for idx, action in enumerate(actions):
        if not _is_ephemeral_candidate_action(action):
            continue
        action_id = str(action.get("action_id") or "")
        before_url = _action_before_image_url(action, steps_by_no)
        after_landmark = landmarks_by_action_id.get(action_id) or {}
        before_bytes = _read_image_url_bytes(before_url)
        after_bytes = _read_landmark_bytes(after_landmark)
        if not before_bytes or not after_bytes:
            await _write_log(
                session,
                run.id,
                level=1,
                title="轨迹缓存瞬态标记",
                content=(
                    f"action_id={action_id} role=business_required "
                    "reason=缺少 before/after 截图证据，跳过 classifier"
                ),
            )
            continue
        try:
            result = await classifier.classify_action(
                goal=str(run.goal or ""),
                action=action,
                before_bytes=before_bytes,
                after_bytes=after_bytes,
                prev_action=actions[idx - 1] if idx > 0 else None,
                next_action=actions[idx + 1] if idx + 1 < len(actions) else None,
            )
        except Exception as exc:  # noqa: BLE001
            result = EphemeralClassification(
                role=ROLE_BUSINESS_REQUIRED,
                category="uncertain",
                confidence=0.0,
                skip_if_absent=False,
                reason=f"classifier 调用失败：{type(exc).__name__}: {str(exc)[:160]}",
            )
        if result.is_optional:
            action["role"] = ROLE_OPTIONAL_EPHEMERAL
            action["ephemeral_meta"] = {
                "enabled": True,
                "category": result.category,
                "skip_if_absent": True,
                "confidence": float(result.confidence),
                "reason": result.reason,
                "business_risk": result.business_risk or "low",
                "cached_popup_before_snapshot": before_url,
                "cached_popup_before_path": str(_resolve_file_url(before_url) or ""),
                "cached_after_snapshot": str(after_landmark.get("image_url") or ""),
                "cached_after_path": str(after_landmark.get("image_path") or ""),
            }
        await _write_log(
            session,
            run.id,
            level=1,
            title="轨迹缓存瞬态标记",
            content=(
                f"action_id={action_id} role={action.get('role')} "
                f"category={result.category} confidence={result.confidence:.2f} "
                f"reason={result.reason}"
            ),
        )


def _is_ephemeral_candidate_action(action: Dict[str, Any]) -> bool:
    return str(action.get("type") or "") in {
        A.ACTION_CLICK,
        A.ACTION_DOUBLE_TAP,
        A.ACTION_LONG_PRESS,
        A.ACTION_PRESS_BACK,
    }


def _action_before_image_url(
    action: Dict[str, Any],
    steps_by_no: Dict[int, RunStep],
) -> str:
    source_step = _safe_int(action.get("source_step"))
    if source_step is None:
        return ""
    step = steps_by_no.get(source_step)
    return str(getattr(step, "screenshot_before", "") or "") if step is not None else ""


def _read_image_url_bytes(image_url: str) -> Optional[bytes]:
    path = _resolve_file_url(image_url)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except Exception:  # noqa: BLE001
        return None


def _read_landmark_bytes(landmark: Dict[str, Any]) -> Optional[bytes]:
    path_text = str(landmark.get("image_path") or "").strip()
    if path_text:
        try:
            return Path(path_text).expanduser().read_bytes()
        except Exception:  # noqa: BLE001
            return None
    return _read_image_url_bytes(str(landmark.get("image_url") or ""))


def _build_actions_from_steps(
    *,
    steps: Sequence[RunStep],
    commands: Sequence[RunCommand],
    screen_size: Tuple[int, int],
    intents: Sequence[str],
    structured_goal: bool,
    source_vlm_backend: str,
) -> List[Dict[str, Any]]:
    command_ids = [s.command_id for s in steps if s.command_id]
    commands_by_id: Dict[str, RunCommand] = {}
    if command_ids:
        commands_by_id = {c.message_id: c for c in commands if c.message_id in command_ids}

    actions: List[Dict[str, Any]] = []
    for step in steps:
        if step.unknown:
            continue
        command = commands_by_id.get(step.command_id or "")
        cleaned = _clean_step_to_actions(
            step,
            command=command,
            screen_size=screen_size,
            intent=_intent_for_index(intents, int(step.step)),
            source_vlm_backend=source_vlm_backend,
        )
        actions.extend(cleaned)
    if not actions:
        action_index = 1
        business_action_index = 1
        for command in commands:
            action = _action_from_command(action_index, command)
            if action is not None:
                intent, label, consumes_business_intent = _fallback_action_metadata(
                    action,
                    intents=intents,
                    business_action_index=business_action_index,
                    structured_goal=structured_goal,
                )
                _enrich_action_metadata(
                    action,
                    intent=intent,
                    label=label,
                    thought="",
                    source_step=None,
                )
                actions.append(action)
                action_index += 1
                if consumes_business_intent:
                    business_action_index += 1
    return actions


def _build_actions_from_timeline(
    *,
    logs: Sequence[RunLog],
    commands: Sequence[RunCommand],
    screen_size: Tuple[int, int],
    intents: Sequence[str],
    structured_goal: bool,
    source_vlm_backend: str,
) -> List[Dict[str, Any]]:
    grouped = _group_logs_by_step(logs)
    replay_commands = [command for command in commands if _is_replay_command(command)]
    command_cursor = 0
    actions: List[Dict[str, Any]] = []
    business_action_index = 1

    for step, step_logs in grouped:
        thought = _last_log_content(step_logs, "思考")
        raw_action = _first_action_log_content(step_logs)
        raw_parts = [part.strip() for part in raw_action.split("→") if part.strip()]
        if not raw_parts:
            system_action = _system_action_from_step_logs(step_logs)
            if system_action:
                raw_parts = [system_action]
        if not raw_parts:
            continue

        for raw_part in raw_parts:
            parsed = parse_cache_action(raw_part, backend=source_vlm_backend)
            if parsed.action in (A.ACTION_FINISHED, A.ACTION_ASSERT_FAIL) or not parsed.is_known:
                continue

            command = None
            if parsed.action != A.ACTION_WAIT:
                command, command_cursor = _consume_matching_command(
                    replay_commands,
                    start=command_cursor,
                    action_type=parsed.action,
                )
            action = _action_from_command(len(actions) + 1, command)
            if action is None:
                action = _action_from_parsed_raw(
                    len(actions) + 1,
                    parsed=parsed,
                    raw=raw_part,
                    screen_size=screen_size,
                    source="run_log",
                )
            if action is None:
                continue

            intent, label, consumes_business_intent = _timeline_action_metadata(
                action,
                raw_action=raw_part,
                thought=thought,
                intents=intents,
                business_action_index=business_action_index,
                structured_goal=structured_goal,
            )
            _enrich_action_metadata(
                action,
                intent=intent,
                label=label,
                thought=thought,
                source_step=step,
            )
            actions.append(action)
            if consumes_business_intent:
                business_action_index += 1

    return actions


def _group_logs_by_step(logs: Sequence[RunLog]) -> List[Tuple[int, List[RunLog]]]:
    grouped: Dict[int, List[RunLog]] = {}
    for log in logs:
        if log.step is None:
            continue
        grouped.setdefault(int(log.step), []).append(log)
    return [(step, grouped[step]) for step in sorted(grouped)]


def _last_log_content(logs: Sequence[RunLog], title: str) -> str:
    for log in reversed(logs):
        if str(log.title or "") == title:
            return str(log.content or "")
    return ""


def _build_source_completion(run: Run, logs: Sequence[RunLog]) -> Dict[str, str]:
    """保留首跑成功时的最终语义，供缓存断言消解业务别名。

    这份信息不能替代当前截图证据，但能避免缓存断言把“主页/返回页/目标页”
    这类自由目标里的口语化表达解释成与首跑不同的页面。
    """

    reason = normalize_run_semantic(str(run.reason or ""))
    task_done = normalize_run_semantic(_last_log_content(logs, "任务完成"))
    final_thought = normalize_run_semantic(_last_log_content(logs, "思考"))
    assertion_pass = normalize_run_semantic(_last_log_content(logs, "断言系统 · 通过"))
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


def _first_action_log_content(logs: Sequence[RunLog]) -> str:
    for log in logs:
        title = str(log.title or "")
        if title == "动作" or title.startswith("动作链"):
            return str(log.content or "").strip()
    return ""


def _system_action_from_step_logs(logs: Sequence[RunLog]) -> str:
    title_text = " ".join(str(log.title or "") for log in logs)
    if "关闭App（系统起跑线）" in title_text:
        return "close_app()"
    if "打开App（系统起跑线）" in title_text:
        return "open_app()"
    for log in logs:
        if str(log.title or "") != "执行完成":
            continue
        match = _EXEC_ACTION_RE.search(str(log.content or ""))
        if match is None:
            continue
        action_name = match.group(1).strip()
        if action_name in (A.ACTION_CLOSE_APP, A.ACTION_OPEN_APP):
            return f"{action_name}()"
    return ""


def _is_replay_command(command: RunCommand) -> bool:
    return _action_from_command(1, command) is not None


def _consume_matching_command(
    commands: Sequence[RunCommand],
    *,
    start: int,
    action_type: str,
) -> Tuple[Optional[RunCommand], int]:
    for idx in range(start, len(commands)):
        command = commands[idx]
        if _command_matches_action(command, action_type):
            return command, idx + 1
    return None, start


def _command_matches_action(command: RunCommand, action_type: str) -> bool:
    method = str(command.method or "")
    expected = {
        A.ACTION_CLICK: {"click"},
        A.ACTION_DOUBLE_TAP: {"double_click"},
        A.ACTION_LONG_PRESS: {"long_press"},
        A.ACTION_TYPE: {"type_text"},
        A.ACTION_DRAG: {"swipe"},
        A.ACTION_SCROLL: {"scroll"},
        A.ACTION_OPEN_APP: {"activate_app"},
        A.ACTION_CLOSE_APP: {"terminate_app"},
        A.ACTION_PRESS_HOME: {"press_home"},
        A.ACTION_PRESS_BACK: {"press_back"},
        A.ACTION_KEY_EVENT: {"press_keycode"},
    }.get(action_type, set())
    return method in expected


def _timeline_action_metadata(
    action: Dict[str, Any],
    *,
    raw_action: str,
    thought: str,
    intents: Sequence[str],
    business_action_index: int,
    structured_goal: bool,
) -> Tuple[str, str, bool]:
    if str(action.get("type") or "") == A.ACTION_WAIT:
        return _intent_from_thought(thought) or raw_action, "", False
    system_intent, system_label, consumes = _fallback_action_metadata(
        action,
        intents=intents,
        business_action_index=business_action_index,
        structured_goal=structured_goal,
    )
    if not consumes:
        return system_intent, system_label, consumes
    intent = system_intent or _intent_from_thought(thought) or raw_action
    return intent, "", True


def _intent_from_thought(thought: str) -> str:
    text = normalize_run_semantic(thought)
    if not text:
        return ""
    parts = [
        part.strip(" ，,。；;、\t\r\n")
        for part in re.split(r"[。；;\n]+", text)
        if part.strip(" ，,。；;、\t\r\n")
    ]
    for part in parts:
        if _ACTION_VERB_RE.search(part):
            return part[:160]
    return parts[0][:160] if parts else ""


def _clean_step_to_actions(
    step: RunStep,
    *,
    command: Optional[RunCommand],
    screen_size: Tuple[int, int],
    intent: str,
    source_vlm_backend: str,
) -> List[Dict[str, Any]]:
    command_action = _action_from_command(int(step.step), command)
    if command_action is not None:
        _enrich_action_metadata(
            command_action,
            intent=intent,
            thought=str(step.thought or ""),
            source_step=int(step.step),
        )
        return [command_action]

    raw = str(step.action or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split("→") if p.strip()]
    out: List[Dict[str, Any]] = []
    for part in parts:
        parsed = parse_cache_action(part, backend=source_vlm_backend)
        action = _action_from_parsed(parsed, step=step, screen_size=screen_size)
        if action is not None:
            _enrich_action_metadata(
                action,
                intent=intent,
                thought=str(step.thought or ""),
                source_step=int(step.step),
            )
            out.append(action)
    return out


def _action_from_command(index: int, command: Optional[RunCommand]) -> Optional[Dict[str, Any]]:
    if command is None or not command.ok:
        return None
    params = dict(command.params or {})
    method = str(command.method or "")
    base = {
        "index": index,
        "source": "run_command",
        "driver_method": method,
        "message_id": command.message_id,
    }
    if method == "click":
        if not _has_keys(params, "x", "y"):
            return None
        return {**base, "type": A.ACTION_CLICK, "point": _point(params, "x", "y"), "coord_mode": "absolute"}
    if method == "double_click":
        if not _has_keys(params, "x", "y"):
            return None
        return {
            **base,
            "type": A.ACTION_DOUBLE_TAP,
            "point": _point(params, "x", "y"),
            "coord_mode": "absolute",
        }
    if method == "long_press":
        if not _has_keys(params, "x", "y"):
            return None
        return {
            **base,
            "type": A.ACTION_LONG_PRESS,
            "point": _point(params, "x", "y"),
            "duration_ms": int(params.get("duration_ms") or 1000),
            "coord_mode": "absolute",
        }
    if method == "type_text":
        if "text" not in params:
            return None
        return {**base, "type": A.ACTION_TYPE, "content": str(params.get("text") or "")}
    if method == "swipe":
        if not _has_keys(params, "sx", "sy", "ex", "ey"):
            return None
        return {
            **base,
            "type": A.ACTION_DRAG,
            "start": _point(params, "sx", "sy"),
            "end": _point(params, "ex", "ey"),
            "duration_ms": int(params.get("duration_ms") or 500),
            "coord_mode": "absolute",
        }
    if method == "scroll":
        if not params:
            return None
        action = {**base, "type": A.ACTION_SCROLL, "direction": str(params.get("direction") or "down")}
        center = params.get("center")
        if isinstance(center, (list, tuple)) and len(center) >= 2:
            action["center"] = {"x": int(center[0]), "y": int(center[1])}
        action["amount"] = int(params.get("amount") or 1)
        return action
    if method == "activate_app":
        if "package_name" not in params:
            return None
        return {**base, "type": A.ACTION_OPEN_APP, "app_name": str(params.get("package_name") or "")}
    if method == "terminate_app":
        if "package_name" not in params:
            return None
        return {**base, "type": A.ACTION_CLOSE_APP, "app_name": str(params.get("package_name") or "")}
    if method == "press_home":
        return {**base, "type": A.ACTION_PRESS_HOME}
    if method == "press_back":
        return {**base, "type": A.ACTION_PRESS_BACK}
    if method == "press_keycode":
        if "code" not in params:
            return None
        return {**base, "type": A.ACTION_KEY_EVENT, "keycode": int(params.get("code") or 0)}
    return None


def _action_from_parsed(
    parsed: A.ParsedAction,
    *,
    step: RunStep,
    screen_size: Tuple[int, int],
) -> Optional[Dict[str, Any]]:
    return _action_from_parsed_raw(
        int(step.step),
        parsed=parsed,
        raw=parsed.raw,
        screen_size=screen_size,
        source="run_step",
    )


def _action_from_parsed_raw(
    index: int,
    *,
    parsed: A.ParsedAction,
    raw: str,
    screen_size: Tuple[int, int],
    source: str,
) -> Optional[Dict[str, Any]]:
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
            "point": _parsed_point_to_abs(parsed.point, parsed.coord_space, screen_size),
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
            out["center"] = _parsed_point_to_abs(parsed.point, parsed.coord_space, screen_size)
        return out
    if action == A.ACTION_DRAG:
        if not (parsed.start_point and parsed.end_point):
            return None
        return {
            **base,
            "type": action,
            "start": _parsed_point_to_abs(parsed.start_point, parsed.coord_space, screen_size),
            "end": _parsed_point_to_abs(parsed.end_point, parsed.coord_space, screen_size),
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
    return None


def _parsed_point_to_abs(
    point: Iterable[int],
    coord_space: str,
    screen_size: Tuple[int, int],
) -> Dict[str, int]:
    x, y = [int(v) for v in list(point)[:2]]
    if coord_space == "absolute":
        return {"x": x, "y": y}
    w, h = screen_size
    if w > 0 and h > 0:
        ax, ay = A.vlm_point_to_abs(x, y, w, h)
        return {"x": int(ax), "y": int(ay)}
    return {"x": x, "y": y}


def _point(params: Dict[str, Any], x_key: str, y_key: str) -> Dict[str, int]:
    return {"x": int(params.get(x_key) or 0), "y": int(params.get(y_key) or 0)}


def _has_keys(params: Dict[str, Any], *keys: str) -> bool:
    return all(key in params and params[key] is not None for key in keys)


def _first_app_target(actions: List[Dict[str, Any]]) -> str:
    for action in actions:
        if action.get("type") in (A.ACTION_OPEN_APP, A.ACTION_CLOSE_APP):
            return str(action.get("app_name") or action.get("package") or "")
    return ""


def _split_goal_intents(goal: str | None) -> List[str]:
    text = normalize_run_semantic(goal)
    if not text:
        return []
    action_text = _structured_operation_text(text) or text
    parts = [
        _clean_intent_part(p)
        for p in re.split(r"[，,。；;、\n]+", action_text)
        if _clean_intent_part(p)
    ]
    action_parts = [part for part in parts if _ACTION_VERB_RE.search(part)]
    if action_parts:
        return action_parts
    return parts


def _intent_for_index(intents: Sequence[str], index: int) -> str:
    if index <= 0:
        return ""
    if index <= len(intents):
        return intents[index - 1]
    return ""


def _structured_operation_text(text: str) -> str:
    match = re.search(r"操作步骤\s*[:：]", text)
    if match is None:
        return ""
    start = match.end()
    next_match = re.search(r"(预期结果|期望结果)\s*[:：]", text[start:])
    end = start + next_match.start() if next_match is not None else len(text)
    return text[start:end].strip()


def _is_structured_goal_text(goal: str | None) -> bool:
    text = normalize_run_semantic(goal)
    if not text:
        return False
    fields = {match.group(1) for match in _STRUCTURED_FIELD_RE.finditer(text)}
    return len(fields) >= 2


def _clean_intent_part(part: str) -> str:
    cleaned = normalize_run_semantic(part).strip(" ，,。；;、\t\r\n")
    cleaned = re.sub(r"^(?:\d+[\.、)]|第[一二三四五六七八九十]+步\s*[:：]?)\s*", "", cleaned)
    return cleaned


def _fallback_action_metadata(
    action: Dict[str, Any],
    *,
    intents: Sequence[str],
    business_action_index: int,
    structured_goal: bool,
) -> Tuple[str, str, bool]:
    action_type = str(action.get("type") or "")
    if structured_goal and action_type == A.ACTION_CLOSE_APP:
        target = _action_app_target(action)
        return "关闭App（系统起跑线）", target, False
    if structured_goal and action_type == A.ACTION_OPEN_APP:
        target = _action_app_target(action)
        return "打开App（系统起跑线）", target, False
    return _intent_for_index(intents, business_action_index), "", True


def _action_app_target(action: Dict[str, Any]) -> str:
    return str(
        action.get("app_name")
        or action.get("package_name")
        or action.get("package")
        or action.get("bundle_id")
        or ""
    ).strip()


def _enrich_action_metadata(
    action: Dict[str, Any],
    *,
    intent: str,
    label: str = "",
    thought: str,
    source_step: Optional[int],
) -> None:
    normalized_intent = normalize_run_semantic(intent)
    normalized_label = normalize_run_semantic(label)
    normalized_thought = normalize_run_semantic(thought)
    if normalized_intent:
        action["intent"] = normalized_intent
        action["label"] = normalized_label or _intent_label(normalized_intent)
    if normalized_thought:
        action["thought"] = normalized_thought[:1000]
    if source_step is not None:
        action["source_step"] = source_step


def _assign_action_identity(actions: List[Dict[str, Any]]) -> None:
    """给清洗后的 replay action 补稳定内部身份。

    ``index`` 继续作为回放顺序展示字段使用；v2 额外补 ``action_id`` 和
    ``chain_index``，后续状态路标 / recovery 专线只依赖这些内部字段，不要求
    业务 case 侧提供任何 id。
    """
    per_step_count: Dict[int, int] = {}
    for idx, action in enumerate(actions, start=1):
        action["index"] = idx
        action["action_id"] = f"a{idx:03d}"
        source_step = _safe_int(action.get("source_step"))
        if source_step is None:
            action["chain_index"] = 1
            continue
        chain_index = per_step_count.get(source_step, 0) + 1
        per_step_count[source_step] = chain_index
        action["chain_index"] = chain_index


def _build_state_landmarks(
    *,
    actions: Sequence[Dict[str, Any]],
    steps: Sequence[RunStep],
    logs: Sequence[RunLog],
    commands: Sequence[RunCommand],
) -> List[Dict[str, Any]]:
    """生成 v2 状态路标。

    阶段 A 只记录，不改变 replay。缺图、链式动作无独立截图、日志时间缺失都只
    进入 ``status`` / ``missing_reason``，不能影响缓存保存。
    """
    if not actions:
        return []

    steps_by_no = {int(step.step): step for step in steps}
    logs_by_step = _group_logs_dict(logs)
    commands_by_id = {
        str(command.message_id): command
        for command in commands
        if str(command.message_id or "")
    }
    landmarks: List[Dict[str, Any]] = []

    for idx, action in enumerate(actions):
        next_action = actions[idx + 1] if idx + 1 < len(actions) else None
        current_step = _safe_int(action.get("source_step"))
        next_step = _safe_int(next_action.get("source_step")) if next_action else None
        snapshot_step: Optional[RunStep] = None
        phase = "before"
        meaning = (
            f"action {action.get('action_id')} 完成后，"
            f"action {next_action.get('action_id')} 执行前的页面状态"
            if next_action is not None
            else f"action {action.get('action_id')} 完成后，最终断言前的页面状态"
        )
        missing_reason = ""

        if next_action is not None:
            if next_step is None:
                missing_reason = "next_action_without_source_step"
            elif current_step is not None and next_step == current_step:
                missing_reason = "same_step_action_chain_no_handoff"
            else:
                snapshot_step = steps_by_no.get(next_step)
        else:
            snapshot_step, phase, missing_reason = _final_handoff_step(
                steps=steps,
                current_step=current_step,
            )

        if next_action is None and not snapshot_step and not missing_reason:
            missing_reason = "final_snapshot_not_found"

        image_url = _step_screenshot_url(snapshot_step, phase) if snapshot_step else ""
        snapshot_meta = _snapshot_meta(image_url)
        if not image_url and not missing_reason:
            missing_reason = "image_url_empty"
        if snapshot_meta["status"] != "available" and not missing_reason:
            missing_reason = str(snapshot_meta.get("missing_reason") or "image_unavailable")

        action_start_ms = _action_start_ms(action, logs_by_step, commands_by_id)
        action_end_ms = _action_end_ms(action, logs_by_step, commands_by_id)
        next_action_start_ms = (
            _action_start_ms(next_action, logs_by_step, commands_by_id)
            if next_action is not None
            else None
        )
        gap_ms = (
            max(0, next_action_start_ms - action_end_ms)
            if action_end_ms is not None and next_action_start_ms is not None
            else None
        )

        landmark = {
            "landmark_id": f"lm_{str(action.get('action_id') or idx + 1)}",
            "action_id": action.get("action_id"),
            "after_action_index": int(action.get("index") or idx + 1),
            "before_action_id": next_action.get("action_id") if next_action else None,
            "before_action_index": (
                int(next_action.get("index") or idx + 2) if next_action else None
            ),
            "source_step": current_step,
            "snapshot_step": int(snapshot_step.step) if snapshot_step is not None else None,
            "snapshot_phase": phase,
            "meaning": meaning,
            **snapshot_meta,
            "timing": {
                "action_start_ts_ms": action_start_ms,
                "action_end_ts_ms": action_end_ms,
                "handoff_snapshot_ts_ms": _snapshot_ts_ms(
                    snapshot_step=snapshot_step,
                    phase=phase,
                    logs_by_step=logs_by_step,
                )
                if snapshot_step is not None
                else None,
                "next_action_start_ts_ms": next_action_start_ms,
                "gap_to_next_action_ms": gap_ms,
            },
        }
        if missing_reason and landmark["status"] != "available":
            landmark["missing_reason"] = missing_reason
        landmarks.append(landmark)

    return landmarks


def _group_logs_dict(logs: Sequence[RunLog]) -> Dict[int, List[RunLog]]:
    grouped: Dict[int, List[RunLog]] = {}
    for log in logs:
        if log.step is None:
            continue
        grouped.setdefault(int(log.step), []).append(log)
    return grouped


def _final_handoff_step(
    *,
    steps: Sequence[RunStep],
    current_step: Optional[int],
) -> Tuple[Optional[RunStep], str, str]:
    ordered = sorted(steps, key=lambda item: (int(item.step), int(item.id or 0)))
    if current_step is not None:
        later = [step for step in ordered if int(step.step) > current_step]
        for step in later:
            action_text = f"{step.action_type} {step.action}".lower()
            if "finished" in action_text and step.screenshot_before:
                return step, "before", ""
        for step in later:
            if step.screenshot_before:
                return step, "before", ""
    return None, "before", "final_handoff_snapshot_not_found"


def _step_screenshot_url(step: Optional[RunStep], phase: str) -> str:
    if step is None:
        return ""
    if phase == "after":
        return str(step.screenshot_after or "")
    return str(step.screenshot_before or "")


def _snapshot_ts_ms(
    *,
    snapshot_step: RunStep,
    phase: str,
    logs_by_step: Dict[int, List[RunLog]],
) -> Optional[int]:
    step_logs = logs_by_step.get(int(snapshot_step.step), [])
    matched: List[int] = []
    for log in step_logs:
        if str(log.title or "") != "截图":
            continue
        content = str(log.content or "")
        if f"phase={phase}" in content or content.strip() == phase:
            ts_ms = _dt_to_epoch_ms(log.ts)
            if ts_ms is not None:
                matched.append(ts_ms)
    if matched:
        return matched[-1] if phase != "before" else matched[0]
    return _dt_to_epoch_ms(snapshot_step.created_at)


def _snapshot_meta(image_url: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "status": "unavailable",
        "image_url": image_url or "",
        "image_path": "",
        "image_sha256": "",
        "image_phash": "",
        "image_size_bytes": 0,
        "missing_reason": "image_url_empty" if not image_url else "",
    }
    if not image_url:
        return meta
    path = _resolve_file_url(image_url)
    if path is None:
        meta["missing_reason"] = "unsupported_image_url"
        return meta
    meta["image_path"] = str(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        meta["missing_reason"] = "image_not_found"
        return meta
    except Exception as exc:  # noqa: BLE001
        meta["missing_reason"] = f"image_read_failed:{type(exc).__name__}"
        return meta
    meta["status"] = "available"
    meta["missing_reason"] = ""
    meta["image_sha256"] = hashlib.sha256(data).hexdigest()
    phash = compute_phash(data)
    meta["image_phash"] = f"{phash:064x}" if phash is not None else ""
    meta["image_size_bytes"] = len(data)
    return meta


def _resolve_file_url(image_url: str) -> Optional[Path]:
    raw = str(image_url or "").strip()
    if not raw:
        return None
    if raw.startswith("/files/"):
        rel = raw[len("/files/") :].lstrip("/")
        root = Path(get_settings().storage_dir).expanduser().resolve()
        return root / rel
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return None


def _action_start_ms(
    action: Optional[Dict[str, Any]],
    logs_by_step: Dict[int, List[RunLog]],
    commands_by_id: Dict[str, RunCommand],
) -> Optional[int]:
    if not action:
        return None
    command = commands_by_id.get(str(action.get("message_id") or ""))
    if command is not None:
        return _dt_to_epoch_ms(command.sent_at)
    step_no = _safe_int(action.get("source_step"))
    if step_no is None:
        return None
    return _first_log_ts_ms(logs_by_step.get(step_no, []), {"动作", "动作链"})


def _action_end_ms(
    action: Optional[Dict[str, Any]],
    logs_by_step: Dict[int, List[RunLog]],
    commands_by_id: Dict[str, RunCommand],
) -> Optional[int]:
    if not action:
        return None
    command = commands_by_id.get(str(action.get("message_id") or ""))
    if command is not None:
        return _dt_to_epoch_ms(command.finished_at) or _dt_to_epoch_ms(command.sent_at)
    step_no = _safe_int(action.get("source_step"))
    if step_no is None:
        return None
    return _last_log_ts_ms(logs_by_step.get(step_no, []), {"执行完成"})


def _first_log_ts_ms(logs: Sequence[RunLog], titles: set[str]) -> Optional[int]:
    for log in logs:
        title = str(log.title or "")
        if title in titles or any(title.startswith(prefix) for prefix in titles):
            ts = _dt_to_epoch_ms(log.ts)
            if ts is not None:
                return ts
    return None


def _last_log_ts_ms(logs: Sequence[RunLog], titles: set[str]) -> Optional[int]:
    for log in reversed(list(logs)):
        title = str(log.title or "")
        if title in titles or any(title.startswith(prefix) for prefix in titles):
            ts = _dt_to_epoch_ms(log.ts)
            if ts is not None:
                return ts
    return None


def _dt_to_epoch_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _intent_label(intent: str) -> str:
    for prefix in ("点击", "进入", "打开", "选择", "切换", "返回", "输入"):
        if intent.startswith(prefix) and len(intent) > len(prefix):
            return intent[len(prefix):].strip(" 「」'\"")
    return intent


def _source_vlm_backend(run: Run) -> str:
    summary = run.token_summary or {}
    if isinstance(summary, dict):
        backend = str(summary.get("vlm_backend") or summary.get("backend") or "").strip()
        if backend:
            return backend
    return str(get_settings().vlm_backend or "doubao_responses").strip() or "doubao_responses"


async def _write_log(
    session: AsyncSession,
    run_id: str,
    *,
    level: int,
    title: str,
    content: str,
) -> None:
    session.add(RunLog(run_id=run_id, level=level, title=title, content=content))
