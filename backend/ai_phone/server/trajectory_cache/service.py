"""轨迹缓存保存 / 删除 / 清洗。

第一阶段原则：
- key = 设备唯一码 + run 语义强匹配；
- 成功 run 才保存；
- 任意失败路径都尝试删除，空删除不报错；
- main 分支仍是 Agent 本地 driver 架构，没有 server-brain 的 RunCommand 时间线，
  因此优先从 RunLog 时间线清洗动作，再兜底 RunStep.action。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.shared import actions as A
from ai_phone.config import get_settings
from ai_phone.server.models import Device, Run, RunLog, RunStep, VlmTrajectoryCache
from ai_phone.server.trajectory_cache.action_adapters import parse_cache_action

CACHE_SCHEMA_VERSION = 1
_WS_RE = re.compile(r"\s+")
_EXEC_ACTION_RE = re.compile(r"动作\s*[:：]\s*([^,，]+)")
_STRUCTURED_FIELD_RE = re.compile(
    r"(测试标题|前置条件|操作步骤|预期结果|期望结果)\s*[:：]"
)
_ACTION_VERB_RE = re.compile(
    r"(点击|轻点|tap|输入|打开|关闭|选择|切换|返回|滑动|上滑|下滑|左滑|右滑|长按|双击|等待|勾选|取消|进入)",
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


async def get_dispatch_trajectory_cache(
    session: AsyncSession,
    *,
    run_id: str,
    device_code: str,
    run_semantic_text: str,
) -> Optional[Dict[str, Any]]:
    """派发前查询缓存并写入可见日志。

    main 分支的回放发生在 Agent 侧，因此 Server 在下发 start_run 前只负责判断
    是否命中，并把 trajectory_json 带给 Agent。
    """
    if not get_settings().vlm_trajectory_cache_replay_enabled:
        await _write_log(
            session,
            run_id,
            level=1,
            title="轨迹缓存",
            content="未开启，继续走现有 VLMRunner",
        )
        return None

    normalized_device = str(device_code or "").strip()
    if not normalized_device:
        return None
    cache_key, _normalized, _semantic_hash = build_cache_key(
        device_code=normalized_device,
        run_semantic_text=run_semantic_text,
    )
    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(
                VlmTrajectoryCache.cache_key == cache_key,
                VlmTrajectoryCache.status == "active",
            )
        )
    ).scalars().first()
    if row is None:
        await _write_log(
            session,
            run_id,
            level=1,
            title="轨迹缓存",
            content="未命中，继续走现有 VLMRunner",
        )
        return None
    await _write_log(
        session,
        run_id,
        level=1,
        title="轨迹缓存",
        content=f"命中轨迹回放 cache_key={row.cache_key[:12]}",
    )
    return row.to_dict()


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
    commands: List[Any] = []
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
    }


def _build_actions_from_steps(
    *,
    steps: Sequence[RunStep],
    commands: Sequence[Any],
    screen_size: Tuple[int, int],
    intents: Sequence[str],
    structured_goal: bool,
    source_vlm_backend: str,
) -> List[Dict[str, Any]]:
    command_ids = [getattr(s, "command_id", "") for s in steps if getattr(s, "command_id", "")]
    commands_by_id: Dict[str, Any] = {}
    if command_ids:
        commands_by_id = {c.message_id: c for c in commands if c.message_id in command_ids}

    actions: List[Dict[str, Any]] = []
    for step in steps:
        if step.unknown:
            continue
        command = commands_by_id.get(getattr(step, "command_id", "") or "")
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
    commands: Sequence[Any],
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


def _first_action_log_content(logs: Sequence[RunLog]) -> str:
    for log in logs:
        title = str(log.title or "")
        if title == "动作" or title.startswith("动作链"):
            return str(log.content or "").strip()
    return ""


def _system_action_from_step_logs(logs: Sequence[RunLog]) -> str:
    title_text = " ".join(str(log.title or "") for log in logs)
    if "关闭App（系统起跑线）" in title_text:
        target = _system_app_target_from_logs(logs)
        return f"close_app(app_name='{target}')" if target else "close_app()"
    if "打开App（系统起跑线）" in title_text:
        target = _system_app_target_from_logs(logs)
        return f"open_app(app_name='{target}')" if target else "open_app()"
    for log in logs:
        if str(log.title or "") != "执行完成":
            continue
        match = _EXEC_ACTION_RE.search(str(log.content or ""))
        if match is None:
            continue
        action_name = match.group(1).strip()
        if action_name in (A.ACTION_CLOSE_APP, A.ACTION_OPEN_APP):
            target = _system_app_target_from_logs(logs)
            return f"{action_name}(app_name='{target}')" if target else f"{action_name}()"
    return ""


def _system_app_target_from_logs(logs: Sequence[RunLog]) -> str:
    for log in logs:
        title = str(log.title or "")
        content = str(log.content or "")
        if title in {"关闭App", "打开App"} and "成功:" in content:
            return content.split("成功:", 1)[1].strip().split()[0]
    for log in logs:
        content = str(log.content or "")
        if "→" in content:
            return content.rsplit("→", 1)[1].strip().split()[0]
    for log in logs:
        content = str(log.content or "")
        if content.startswith("应用:"):
            return content.split(":", 1)[1].strip()
    return ""


def _is_replay_command(command: Any) -> bool:
    return _action_from_command(1, command) is not None


def _consume_matching_command(
    commands: Sequence[Any],
    *,
    start: int,
    action_type: str,
) -> Tuple[Optional[Any], int]:
    for idx in range(start, len(commands)):
        command = commands[idx]
        if _command_matches_action(command, action_type):
            return command, idx + 1
    return None, start


def _command_matches_action(command: Any, action_type: str) -> bool:
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
    command: Optional[Any],
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


def _action_from_command(index: int, command: Optional[Any]) -> Optional[Dict[str, Any]]:
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
