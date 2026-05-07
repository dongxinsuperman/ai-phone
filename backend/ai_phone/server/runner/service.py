"""ServerRunnerService：在 Server 进程内运行 VLMRunner。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Run, RunCommand

from .emitter import ServerRunEmitter
from .remote_driver import RemoteDriver
from .rpc import DriverRpcWaiter, RemoteDriverError


RunnerFactory = Callable[..., Any]
RunDoneCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class ServerRunnerService:
    def __init__(
        self,
        *,
        hub: Hub,
        lock_store: DeviceLockStore,
        session_factory: async_sessionmaker,
        waiter: DriverRpcWaiter,
        runner_factory: Optional[RunnerFactory] = None,
        on_run_done: Optional[RunDoneCallback] = None,
    ) -> None:
        self._hub = hub
        self._lock_store = lock_store
        self._session_factory = session_factory
        self._waiter = waiter
        self._runner_factory = runner_factory or VLMRunner
        self._on_run_done = on_run_done
        self._tasks: Dict[str, asyncio.Task] = {}
        self._emitters: Dict[str, ServerRunEmitter] = {}

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())

    def set_on_run_done(self, callback: Optional[RunDoneCallback]) -> None:
        """把 Server 大脑 Run 终态回调接回 scheduler。

        app lifespan 里 ``ServerRunnerService`` 与 ``SubmissionScheduler`` 存在
        轻微互相依赖：dispatcher 需要 runner，scheduler 又需要 dispatcher。
        用 setter 在两者创建完之后补线，避免构造顺序绕成环。
        """
        self._on_run_done = callback

    async def start_run(
        self,
        *,
        run_id: str,
        serial: str,
        agent_id: str,
        goal: str,
        dispatch_source: str,
        platform: str = "android",
    ) -> bool:
        if self.is_running(run_id):
            return True

        loop = asyncio.get_running_loop()

        async def _send(payload: Dict[str, Any]) -> bool:
            return await self._hub.send_to_agent(agent_id, payload)

        async def _command_sent(payload: Dict[str, Any]) -> None:
            async with self._session_factory() as session:
                session.add(
                    RunCommand(
                        run_id=run_id,
                        message_id=str(payload.get("message_id") or ""),
                        method=str(payload.get("method") or ""),
                        agent_id=agent_id,
                        serial=serial,
                    )
                )
                await session.commit()

        async def _command_finished(
            payload: Dict[str, Any],
            result: Optional[Dict[str, Any]],
            error: Optional[BaseException],
            elapsed_ms: int,
        ) -> None:
            message_id = str(payload.get("message_id") or "")
            async with self._session_factory() as session:
                from sqlalchemy import select

                res = await session.execute(
                    select(RunCommand).where(RunCommand.message_id == message_id)
                )
                row = res.scalars().first()
                if row is None:
                    return
                row.rpc_elapsed_ms = elapsed_ms
                row.finished_at = datetime.now(timezone.utc)
                if error is None:
                    row.ok = bool(result.get("ok")) if result is not None else True
                else:
                    row.ok = False
                    if isinstance(error, RemoteDriverError):
                        row.error_class = error.error_class
                        row.error_category = error.category
                        row.error_msg = error.message
                    else:
                        row.error_class = error.__class__.__name__
                        row.error_category = "network"
                        row.error_msg = str(error)
                await session.commit()

        driver = RemoteDriver(
            serial=serial,
            agent_id=agent_id,
            waiter=self._waiter,
            send_fn=_send,
            loop=loop,
            run_id=run_id,
            platform=platform,
            on_command_sent=_command_sent,
            on_command_finished=_command_finished,
        )
        emitter = ServerRunEmitter(
            run_id=run_id,
            serial=serial,
            hub=self._hub,
            lock_store=self._lock_store,
            session_factory=self._session_factory,
            loop=loop,
            on_run_done=self._on_run_done,
        )

        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return False
            run.status = "running"
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.execution_mode = "server_brain"
            run.dispatch_source = dispatch_source
            run.agent_id_at_start = agent_id
            await session.commit()

        await self._hub.bind_run(run_id, agent_id)
        task = asyncio.create_task(
            self._run_task(run_id, goal, driver, emitter),
            name=f"server-brain-run-{run_id}",
        )
        self._tasks[run_id] = task
        self._emitters[run_id] = emitter
        return True

    async def stop_run(self, run_id: str, *, reason: str = "stopped_by_user") -> bool:
        self._waiter.cancel_run(run_id, reason=reason)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        emitter = self._emitters.get(run_id)
        if emitter is not None:
            await emitter.force_finish(result="cancelled", message=reason)
        return task is not None

    async def shutdown(self) -> None:
        for run_id in list(self._tasks):
            await self.stop_run(run_id, reason="server shutting down")

    async def _run_task(
        self,
        run_id: str,
        goal: str,
        driver: RemoteDriver,
        emitter: ServerRunEmitter,
    ) -> None:
        try:
            runner = self._runner_factory(
                run_id=run_id,
                driver=driver,
                goal=goal,
                emit=emitter.emit,
            )
            await runner.run()
        except asyncio.CancelledError:
            await emitter.force_finish(result="cancelled", message="stopped_by_server")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ServerRunnerService run 异常 run_id={}: {}", run_id, exc)
            await emitter.force_finish(
                result="error",
                message=f"server_runner_crash: {exc}",
                error_class=exc.__class__.__name__,
                error_category="network",
            )
        finally:
            await emitter.aclose()
            self._tasks.pop(run_id, None)
            self._emitters.pop(run_id, None)


__all__ = ["ServerRunnerService"]
