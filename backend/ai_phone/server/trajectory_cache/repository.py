"""接收 Agent 回传的成品轨迹缓存并写库（Server 薄存储侧 · M4 片2）。

归档下沉 Agent 后，Agent 用执行第一手数据整理出与 next **同 schema** 的成品缓存，
经 M3 可靠通道（``MSG_CACHE_ARCHIVE``）回传 Server；本模块把成品 upsert 到对应的
``vlm_trajectory_cache_v*`` 表。

约定：``cache_key`` 由 Server 这里**统一计算**（``device_code`` + ``run_semantic_text``
+ ``schema_version``），不信任 Agent 传来的 key，避免两端实现漂移导致命中/写入对不上。

成品载荷（archive）约定字段：
- ``cache_mode``: ``"v1"`` | ``"v2"`` | ``"v3"``
- ``device_code`` / ``run_semantic_text`` / ``source_run_id`` / ``case_id``
- ``platform`` / ``resolution`` / ``app_package_or_bundle``
- V1/V2：``trajectory_json``（含 ``actions`` / ``state_landmarks`` / ``source_completion``）
- V3：``actions`` / ``source_completion`` / ``meta`` / ``source_vlm_backend``
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import (
    VlmTrajectoryCache,
    VlmTrajectoryCacheV2,
    VlmTrajectoryCacheV3,
)
from ai_phone.server.trajectory_cache.service import (
    CACHE_SCHEMA_VERSION_V1,
    CACHE_SCHEMA_VERSION_V2,
    build_cache_key,
)
from ai_phone.server.trajectory_cache.v3_service import V3_CACHE_SCHEMA_VERSION

_MODE_TO_SCHEMA = {
    "v1": CACHE_SCHEMA_VERSION_V1,
    "v2": CACHE_SCHEMA_VERSION_V2,
    "v3": V3_CACHE_SCHEMA_VERSION,
}


async def store_trajectory_cache_archive(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    archive: Dict[str, Any],
) -> Optional[str]:
    """把 Agent 回传的成品缓存 upsert 到对应 V 表，返回 cache_key；非法/空则返回 None。

    幂等：按 Server 计算的 cache_key upsert，重复回传只刷新同一行。
    """
    mode = str(archive.get("cache_mode") or "").strip().lower()
    schema_version = _MODE_TO_SCHEMA.get(mode)
    if schema_version is None:
        logger.warning("成品缓存回传 cache_mode 非法，丢弃：{}", mode or "<empty>")
        return None

    device_code = str(archive.get("device_code") or "").strip()
    run_semantic_text = str(archive.get("run_semantic_text") or "")
    if not device_code or not run_semantic_text:
        logger.warning("成品缓存回传缺 device_code/run_semantic_text，丢弃 mode={}", mode)
        return None

    cache_key, normalized_goal, semantic_hash = build_cache_key(
        device_code=device_code,
        run_semantic_text=run_semantic_text,
        schema_version=schema_version,
    )
    now = datetime.now(timezone.utc)

    if mode == "v3":
        return await _upsert_v3(
            session_factory,
            archive,
            cache_key=cache_key,
            normalized_goal=normalized_goal,
            semantic_hash=semantic_hash,
            now=now,
        )
    model = VlmTrajectoryCache if mode == "v1" else VlmTrajectoryCacheV2
    return await _upsert_v1_v2(
        session_factory,
        archive,
        model=model,
        schema_version=schema_version,
        cache_key=cache_key,
        normalized_goal=normalized_goal,
        semantic_hash=semantic_hash,
        now=now,
    )


async def _upsert_v1_v2(
    session_factory: async_sessionmaker[AsyncSession],
    archive: Dict[str, Any],
    *,
    model: Any,
    schema_version: int,
    cache_key: str,
    normalized_goal: str,
    semantic_hash: str,
    now: datetime,
) -> Optional[str]:
    trajectory = archive.get("trajectory_json") or {}
    actions = trajectory.get("actions") or []
    if not actions:
        logger.info("成品缓存回传无 action，跳过 upsert schema=v{}", schema_version)
        return None
    async with session_factory() as session:
        row = (
            await session.execute(select(model).where(model.cache_key == cache_key))
        ).scalars().first()
        if row is None:
            row = model(cache_key=cache_key)
            session.add(row)
        row.device_code = str(archive.get("device_code") or "")
        row.run_semantic_hash = semantic_hash
        row.run_semantic_text = normalized_goal
        row.case_id = archive.get("case_id")
        row.platform = str(archive.get("platform") or "")
        row.resolution = str(archive.get("resolution") or "")
        row.app_package_or_bundle = str(archive.get("app_package_or_bundle") or "")
        row.schema_version = schema_version
        row.status = "active"
        row.source_run_id = str(archive.get("source_run_id") or "")
        row.trajectory_json = trajectory
        row.updated_at = now
        row.last_success_at = now
        await session.commit()
    logger.info(
        "成品缓存已写库 schema=v{} cache_key={} actions={}",
        schema_version,
        cache_key,
        len(actions),
    )
    return cache_key


async def _upsert_v3(
    session_factory: async_sessionmaker[AsyncSession],
    archive: Dict[str, Any],
    *,
    cache_key: str,
    normalized_goal: str,
    semantic_hash: str,
    now: datetime,
) -> Optional[str]:
    actions = archive.get("actions") or []
    if not actions:
        logger.info("V3 成品缓存回传无 action，跳过 upsert")
        return None
    async with session_factory() as session:
        row = (
            await session.execute(
                select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
            )
        ).scalars().first()
        if row is None:
            row = VlmTrajectoryCacheV3(cache_key=cache_key)
            session.add(row)
        row.device_code = str(archive.get("device_code") or "")
        row.run_semantic_hash = semantic_hash
        row.run_semantic_text = normalized_goal
        row.case_id = archive.get("case_id")
        row.platform = str(archive.get("platform") or "")
        row.resolution = str(archive.get("resolution") or "")
        row.app_package_or_bundle = str(archive.get("app_package_or_bundle") or "")
        row.schema_version = V3_CACHE_SCHEMA_VERSION
        row.status = "active"
        row.source_run_id = str(archive.get("source_run_id") or "")
        row.source_vlm_backend = str(archive.get("source_vlm_backend") or "")
        row.actions_json = actions
        row.source_completion = archive.get("source_completion") or {}
        row.meta_json = archive.get("meta") or {}
        row.updated_at = now
        row.last_success_at = now
        await session.commit()
    logger.info("V3 成品缓存已写库 cache_key={} actions={}", cache_key, len(actions))
    return cache_key


__all__ = ["store_trajectory_cache_archive"]
