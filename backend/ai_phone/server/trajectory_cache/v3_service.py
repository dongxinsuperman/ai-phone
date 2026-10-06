"""V3 语义坐标缓存：命中查询 / 删除 / mark suspect（Server 薄存储侧）。

Distributed Agent Brain（M4 片2）后，V3 的**回放与归档下沉 Agent**
（``ai_phone.agent.trajectory_cache``）；Server 只留命中查询（随 start_run 下发）、
run 失败删、以及把命中回放失败的缓存标 suspect。成品写库见 ``repository.py``。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import Run, RunLog, VlmTrajectoryCacheV3
from ai_phone.server.retry import current_attempt
from ai_phone.config import get_settings
from ai_phone.shared.scroll_gesture import scroll_cache_candidates, has_incompatible_scroll
from ai_phone.server.trajectory_cache.service import _write_log, build_cache_key
from ai_phone.server.trajectory_cache.v3_identity import build_v3_platform_cache_key, resolve_v3_platform

V3_CACHE_SCHEMA_VERSION = 3
V3_BINDING_LOG_TITLE = "V3缓存绑定"


async def get_active_trajectory_cache_v3(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
    platform: str = "",
    allow_platform_cache: bool = False,
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
        if allow_platform_cache:
            family = await resolve_v3_platform(session, device_code=normalized_device, hint=platform)
            if family:
                shared_key, _normalized, _semantic_hash = build_v3_platform_cache_key(
                    platform=family, run_semantic_text=run_semantic_text,
                )
                for candidate in scroll_cache_candidates(shared_key, get_settings().vlm_backend):
                    shared = (await session.execute(select(VlmTrajectoryCacheV3).where(
                        VlmTrajectoryCacheV3.cache_key == candidate,
                        VlmTrajectoryCacheV3.status == "active",
                        VlmTrajectoryCacheV3.platform == family,
                    ))).scalars().first()
                    if shared is not None:
                        meta = shared.meta_json or {}
                        if meta.get("cache_scope") == "platform" and isinstance(meta.get("cache_revision"), str) and meta["cache_revision"]:
                            from .service import active_cache_payload
                            hit = await active_cache_payload(session, shared)
                            if hit is not None:
                                return hit
        # 不提升历史设备缓存为跨设备缓存；旧 Agent / 旧缓存仍走本设备 key。
        from .service import active_cache_payload
        for candidate in scroll_cache_candidates(cache_key, get_settings().vlm_backend):
            row = (await session.execute(select(VlmTrajectoryCacheV3).where(
                VlmTrajectoryCacheV3.cache_key == candidate,
                VlmTrajectoryCacheV3.status == "active",
            ))).scalars().first()
            hit = await active_cache_payload(session, row)
            if hit is not None:
                return hit
        return None


async def record_v3_cache_binding(session_factory, *, run_id: str, hit, attempt: int = 1) -> None:
    """在既有 RunLog 保存本次派发的 key/版本；含 miss，重启 Server 后仍能安全收尾。"""
    async with session_factory() as session:
        if await session.get(Run, run_id) is None:
            return
        binding = {
            "cache_key": (hit or {}).get("cache_key") or "",
            "cache_revision": ((hit or {}).get("meta") or {}).get("cache_revision") or "",
            "updated_at": (hit or {}).get("updated_at") or "",
            "source_run_id": (hit or {}).get("source_run_id") or "",
        }
        session.add(RunLog(
            run_id=run_id, attempt=max(1, int(attempt)), level=1, title=V3_BINDING_LOG_TITLE,
            content=json.dumps(binding, ensure_ascii=False),
        ))
        await session.commit()


async def _failure_conditions(session, *, run_id: str, cache_key: str = "", attempt: Optional[int] = None):
    """只失效本 attempt 实际使用的版本；无绑定时保留旧设备缓存处理。"""
    attempt = current_attempt() if attempt is None else max(1, int(attempt))
    binding_row = (await session.execute(select(RunLog).where(
        RunLog.run_id == run_id, RunLog.attempt == attempt, RunLog.title == V3_BINDING_LOG_TITLE,
    ).order_by(RunLog.id.desc()).limit(1))).scalars().first()
    if binding_row is not None:
        try:
            binding = json.loads(binding_row.content)
        except (ValueError, TypeError):
            return []
        if not isinstance(binding, dict):
            return []
        key = binding.get("cache_key")
        if not key or (cache_key and cache_key != key):
            return []
        conditions = [VlmTrajectoryCacheV3.cache_key == key]
        revision = binding.get("cache_revision")
        if revision:
            conditions.append(VlmTrajectoryCacheV3.meta_json["cache_revision"].as_string() == revision)
        elif binding.get("source_run_id"):
            conditions.append(VlmTrajectoryCacheV3.source_run_id == binding["source_run_id"])
        else:
            try:
                stamp = datetime.fromisoformat(binding.get("updated_at") or "")
            except (ValueError, TypeError):
                return []
            conditions.append(VlmTrajectoryCacheV3.updated_at == stamp)
        return conditions
    run = await session.get(Run, run_id)
    if run is not None:
        legacy_key, _normalized, _semantic_hash = build_cache_key(
            device_code=run.device_serial, run_semantic_text=run.goal, schema_version=3,
        )
        if cache_key and cache_key != legacy_key:
            return []
        return [VlmTrajectoryCacheV3.cache_key == legacy_key]
    # 兼容历史内部调用，但不能在无绑定时失效共享缓存。
    if cache_key:
        row = (await session.execute(select(VlmTrajectoryCacheV3).where(
            VlmTrajectoryCacheV3.cache_key == cache_key,
        ))).scalars().first()
        if row is not None and (row.meta_json or {}).get("cache_scope") != "platform":
            return [VlmTrajectoryCacheV3.cache_key == cache_key]
    return []


async def delete_trajectory_cache_v3_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    attempt: Optional[int] = None,
) -> int:
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return 0
        conditions = await _failure_conditions(session, run_id=run_id, attempt=attempt)
        if not conditions:
            return 0
        backend = (run.token_summary or {}).get("vlm_backend") or get_settings().vlm_backend
        rows = (await session.execute(select(VlmTrajectoryCacheV3).where(*conditions))).scalars().all()
        targets = [r.id for r in rows if not has_incompatible_scroll(r.actions_json, backend)]
        result = await session.execute(delete(VlmTrajectoryCacheV3).where(*conditions, VlmTrajectoryCacheV3.id.in_(targets)))
        deleted = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=1,
            title="V3轨迹缓存",
            content=f"case 失败已触发本次绑定 V3 缓存删除 deleted={deleted}",
            attempt=attempt,
        )
        await session.commit()
        return deleted


async def mark_trajectory_cache_v3_suspect(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cache_key: str,
    run_id: str,
    reason: str,
    attempt: Optional[int] = None,
) -> int:
    """把命中但复跑/断言失败的 V3 cache 标成 suspect，避免继续被命中。"""

    normalized_key = str(cache_key or "").strip()
    if not normalized_key:
        return 0
    async with session_factory() as session:
        conditions = await _failure_conditions(
            session, run_id=run_id, cache_key=normalized_key, attempt=attempt,
        )
        if not conditions:
            return 0
        now = datetime.now(timezone.utc)
        run = await session.get(Run, run_id)
        backend = (run.token_summary or {}).get("vlm_backend") if run is not None else None
        backend = backend or get_settings().vlm_backend
        rows = (await session.execute(select(VlmTrajectoryCacheV3).where(*conditions))).scalars().all()
        targets = [r.id for r in rows if not has_incompatible_scroll(r.actions_json, backend)]
        result = await session.execute(
            update(VlmTrajectoryCacheV3)
            .where(*conditions, VlmTrajectoryCacheV3.id.in_(targets))
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
            attempt=attempt,
        )
        await session.commit()
        return changed


__all__ = [
    "V3_CACHE_SCHEMA_VERSION",
    "delete_trajectory_cache_v3_for_run",
    "get_active_trajectory_cache_v3",
    "mark_trajectory_cache_v3_suspect",
]
