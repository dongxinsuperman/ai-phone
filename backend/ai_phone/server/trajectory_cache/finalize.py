"""Background finalization for trajectory cache rows."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import Run, RunLog, RunStep

_BACKGROUND_TASKS: set[asyncio.Task[Optional[str]]] = set()


def schedule_trajectory_cache_finalize(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    final_status: str,
) -> None:
    """Schedule cache save/delete without blocking the Run done path."""
    try:
        task = asyncio.create_task(
            finalize_trajectory_cache_for_run(
                session_factory=session_factory,
                run_id=run_id,
                final_status=final_status,
            ),
            name=f"trajectory-cache-finalize-{run_id}",
        )
    except RuntimeError as exc:
        logger.warning("轨迹缓存后台整理调度失败 run_id={} status={}: {}", run_id, final_status, exc)
        return
    _BACKGROUND_TASKS.add(task)

    def _done(done: asyncio.Task[Optional[str]]) -> None:
        _BACKGROUND_TASKS.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            logger.warning("轨迹缓存后台整理被取消 run_id={} status={}", run_id, final_status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("轨迹缓存后台整理失败 run_id={} status={}: {}", run_id, final_status, exc)

    task.add_done_callback(_done)


async def finalize_trajectory_cache_for_run(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    final_status: str,
) -> Optional[str]:
    """Save or delete trajectory cache after a run has already finished."""
    from ai_phone.server.trajectory_cache.service import (  # noqa: PLC0415
        delete_trajectory_cache_v1_for_run,
        delete_trajectory_cache_v2_for_run,
        save_trajectory_cache_v1_after_success,
        save_trajectory_cache_v2_after_success,
    )
    from ai_phone.server.trajectory_cache.v3_service import (  # noqa: PLC0415
        delete_trajectory_cache_v3_for_run,
        save_trajectory_cache_v3_after_success,
    )

    started = time.monotonic()
    cache_mode = "off"
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return None
        cache_mode = str(getattr(run, "effective_cache_mode", "") or "off")
    if cache_mode == "off":
        return None

    await _write_background_log(
        session_factory,
        run_id,
        level=1,
        title="轨迹缓存后台整理",
        content=f"已进入后台处理 cacheMode={cache_mode} status={final_status}，不阻塞 Run 结束",
    )
    try:
        if final_status == "success":
            await _wait_for_run_steps_ready(session_factory, run_id)
        result: Optional[str] = None
        if final_status == "success":
            if cache_mode == "v3":
                result = await save_trajectory_cache_v3_after_success(session_factory, run_id)
            elif cache_mode == "v1":
                result = await save_trajectory_cache_v1_after_success(session_factory, run_id)
            elif cache_mode == "v2":
                result = await save_trajectory_cache_v2_after_success(session_factory, run_id)
        else:
            if cache_mode == "v3":
                await delete_trajectory_cache_v3_for_run(session_factory, run_id)
            elif cache_mode == "v1":
                await delete_trajectory_cache_v1_for_run(session_factory, run_id)
            elif cache_mode == "v2":
                await delete_trajectory_cache_v2_for_run(session_factory, run_id)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await _write_background_log(
            session_factory,
            run_id,
            level=1,
            title="轨迹缓存后台整理",
            content=f"处理完成 cacheMode={cache_mode} status={final_status} elapsed_ms={elapsed_ms}",
        )
        return result
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await _write_background_log(
            session_factory,
            run_id,
            level=2,
            title="轨迹缓存后台整理",
            content=(
                f"处理失败 cacheMode={cache_mode} status={final_status} "
                f"elapsed_ms={elapsed_ms} error={type(exc).__name__}: {str(exc)[:180]}"
            ),
        )
        raise


async def _wait_for_run_steps_ready(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    timeout_sec: float = 8.0,
    poll_sec: float = 0.2,
) -> None:
    """Wait for async screenshot/RunStep writes before saving cache.

    Run finish and screenshot/step persistence are both asynchronous in the server runner.
    Cache saving is already a background job, so it can afford to wait briefly for the
    final ``finished`` step to land. Without this, the last real action loses its final
    comparison image and V2 can only rely on final assertion.
    """

    started = time.monotonic()
    while True:
        async with session_factory() as session:
            run = await session.get(Run, run_id)
            expected_steps = int(getattr(run, "steps", 0) or 0) if run is not None else 0
            if expected_steps <= 0:
                return
            rows = (
                await session.execute(
                    select(RunStep.step, RunStep.screenshot_before).where(
                        RunStep.run_id == run_id
                    )
                )
            ).all()
            seen_steps = {int(step or 0) for step, _before in rows}
            final_before_ready = any(
                int(step or 0) == expected_steps and str(before or "")
                for step, before in rows
            )
            if len(seen_steps) >= expected_steps and expected_steps in seen_steps:
                if final_before_ready:
                    return
                # The last row exists but its before screenshot is empty. This is unusual,
                # but waiting longer will not help once the row is committed.
                return
        if time.monotonic() - started >= timeout_sec:
            logger.warning(
                "轨迹缓存后台整理等待 RunStep 落库超时 run_id={} timeout={}s",
                run_id,
                timeout_sec,
            )
            return
        await asyncio.sleep(poll_sec)


async def _write_background_log(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    level: int,
    title: str,
    content: str,
) -> None:
    async with session_factory() as session:
        session.add(
            RunLog(
                run_id=run_id,
                level=level,
                title=title[:255],
                content=content,
            )
        )
        await session.commit()
