"""轨迹缓存 key / 命中查询 / 删除（Server 薄存储侧）。

Distributed Agent Brain（M4 片2）后，缓存的**回放与归档下沉到 Agent**
（``ai_phone.agent.trajectory_cache``）；Server 只保留薄薄一层控制面：

- ``build_cache_key`` / ``normalize_run_semantic``：派发命中查询 + 成品 upsert 用；
- ``get_active_trajectory_cache_v1/v2``：派发前查命中（随 start_run 下发）；
- ``delete_trajectory_cache_v1/v2_for_run``：run 失败删缓存。

原本从 DB 文字反推动作 / 坐标 / 时序的归档链路（``_build_trajectory`` 等）已移除——
成品改由 Agent 用执行第一手数据整理后回传 Server 存储（见 ``repository.py``）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import (
    Run,
    RunLog,
    VlmTrajectoryCache,
    VlmTrajectoryCacheV2,
)
from ai_phone.server.retry import current_attempt

CACHE_SCHEMA_VERSION_V1 = 1
CACHE_SCHEMA_VERSION_V2 = 2
CACHE_SCHEMA_VERSION = CACHE_SCHEMA_VERSION_V2
_WS_RE = re.compile(r"\s+")


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


async def get_active_trajectory_cache_v1(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
) -> Optional[Dict[str, Any]]:
    """按 device_code + run 语义强匹配查询 V1 active 缓存。"""
    return await _get_active_trajectory_cache(
        session_factory,
        device_code=device_code,
        run_semantic_text=run_semantic_text,
        model=VlmTrajectoryCache,
        schema_version=CACHE_SCHEMA_VERSION_V1,
    )


async def get_active_trajectory_cache_v2(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
) -> Optional[Dict[str, Any]]:
    """按 device_code + run 语义强匹配查询 V2 active 缓存。"""
    return await _get_active_trajectory_cache(
        session_factory,
        device_code=device_code,
        run_semantic_text=run_semantic_text,
        model=VlmTrajectoryCacheV2,
        schema_version=CACHE_SCHEMA_VERSION_V2,
    )


async def _get_active_trajectory_cache(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    device_code: str,
    run_semantic_text: str,
    model: Any,
    schema_version: int,
) -> Optional[Dict[str, Any]]:
    """按 device_code + run 语义强匹配查询 active 轨迹缓存。"""
    normalized_device = str(device_code or "").strip()
    if not normalized_device:
        return None
    cache_key, _normalized, _semantic_hash = build_cache_key(
        device_code=normalized_device,
        run_semantic_text=run_semantic_text,
        schema_version=schema_version,
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                select(model).where(
                    model.cache_key == cache_key,
                    model.status == "active",
                )
            )
        ).scalars().first()
        return row.to_dict() if row is not None else None


async def delete_trajectory_cache_v1_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> int:
    """按 run 的 device_code + run_semantic_hash 删除 V1 缓存。"""
    return await _delete_trajectory_cache_for_run(
        session_factory,
        run_id,
        cache_mode="v1",
        model=VlmTrajectoryCache,
        schema_version=CACHE_SCHEMA_VERSION_V1,
    )


async def delete_trajectory_cache_v2_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
) -> int:
    """按 run 的 device_code + run_semantic_hash 删除 V2 缓存。"""
    return await _delete_trajectory_cache_for_run(
        session_factory,
        run_id,
        cache_mode="v2",
        model=VlmTrajectoryCacheV2,
        schema_version=CACHE_SCHEMA_VERSION_V2,
    )


async def _delete_trajectory_cache_for_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    cache_mode: str,
    model: Any,
    schema_version: int,
) -> int:
    """按 run 的 device_code + run_semantic_hash 删除指定模式缓存。

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
            schema_version=schema_version,
        )
        result = await session.execute(
            delete(model).where(model.cache_key == cache_key)
        )
        deleted = int(result.rowcount or 0)
        await _write_log(
            session,
            run_id,
            level=1,
            title="轨迹缓存",
            content=(
                f"case 失败已触发 {cache_mode.upper()} 缓存删除 "
                f"cache_key={cache_key[:12]} deleted={deleted}"
            ),
        )
        await session.commit()
        if deleted:
            logger.info(
                "{} 轨迹缓存已删除 run_id={} cache_key={} deleted={}",
                cache_mode.upper(),
                run_id,
                cache_key,
                deleted,
            )
        return deleted


async def _write_log(
    session: AsyncSession,
    run_id: str,
    *,
    level: int,
    title: str,
    content: str,
) -> None:
    session.add(
        RunLog(
            run_id=run_id,
            attempt=current_attempt(),
            level=level,
            title=title,
            content=content,
        )
    )


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CACHE_SCHEMA_VERSION_V1",
    "CACHE_SCHEMA_VERSION_V2",
    "build_cache_key",
    "delete_trajectory_cache_v1_for_run",
    "delete_trajectory_cache_v2_for_run",
    "get_active_trajectory_cache_v1",
    "get_active_trajectory_cache_v2",
    "normalize_run_semantic",
    "run_semantic_hash",
]
