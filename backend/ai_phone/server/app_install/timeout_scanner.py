from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .service import mark_startup_unknown, mark_timeouts


class AppInstallTimeoutScanner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        interval_sec: float = 10.0,
    ) -> None:
        self._session_factory = session_factory
        self._interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self._run_once(mark_startup_unknown, "启动重置")
        self._task = asyncio.create_task(self._loop(), name="app-install-timeout-scanner")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_sec)
            await self._run_once(mark_timeouts, "超时扫描")

    async def _run_once(
        self,
        fn: Callable[[AsyncSession], Awaitable[int]],
        label: str,
    ) -> None:
        try:
            async with self._session_factory() as session:
                count = await fn(session)
            if count:
                logger.info("app_install {} 处理 {} 条 item", label, count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("app_install {} 失败（忽略）：{}", label, exc)
