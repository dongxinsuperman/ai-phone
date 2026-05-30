"""轨迹缓存失败删除的后台调度（Server 薄存储侧）。

Distributed Agent Brain（M4 片2）后，**成功后的归档下沉 Agent**——Agent 用执行
第一手数据整理成品缓存并回传 Server 存储（见 ``repository.py``）。Server 这里只
保留 run 失败时删除命中/旧缓存的后台动作，不再 ``_build_trajectory`` 反推归档，
因此也不必再等 RunStep / 截图落库。
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_phone.server.models import Run, RunLog
from ai_phone.server.retry import current_attempt

_BACKGROUND_TASKS: set[asyncio.Task[Optional[str]]] = set()


def schedule_trajectory_cache_finalize(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    final_status: str,
) -> None:
    """Schedule cache cleanup without blocking the Run done path.

    成功路径不再触发 Server 归档（归档下沉 Agent）；失败路径删命中/旧缓存。
    """
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
    """Run 结束后的缓存收尾：失败删缓存；成功不在 Server 归档（由 Agent 回传）。"""
    from ai_phone.server.trajectory_cache.service import (  # noqa: PLC0415
        delete_trajectory_cache_v1_for_run,
        delete_trajectory_cache_v2_for_run,
    )
    from ai_phone.server.trajectory_cache.v3_service import (  # noqa: PLC0415
        delete_trajectory_cache_v3_for_run,
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

    if final_status == "success":
        # 成功后的归档已下沉 Agent（执行第一手数据整理后经 M3 可靠通道回传，见
        # repository.py）；Server 不再从 DB 反推归档，这里只记一行、不写缓存。
        await _write_background_log(
            session_factory,
            run_id,
            level=1,
            title="轨迹缓存后台整理",
            content=f"cacheMode={cache_mode} status=success：归档由 Agent 回传，Server 不反推",
        )
        return None

    await _write_background_log(
        session_factory,
        run_id,
        level=1,
        title="轨迹缓存后台整理",
        content=f"已进入后台处理 cacheMode={cache_mode} status={final_status}，不阻塞 Run 结束",
    )
    try:
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
        return None
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
                attempt=current_attempt(),
                level=level,
                title=title[:255],
                content=content,
            )
        )
        await session.commit()
