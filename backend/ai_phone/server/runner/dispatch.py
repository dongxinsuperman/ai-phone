"""RunDispatchService：API / scheduler 共用的 Run 派发入口。"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from ai_phone.shared import protocol as P

from ..hub import Hub
from .service import ServerRunnerService


class RunDispatchService:
    def __init__(
        self,
        *,
        hub: Hub,
        server_runner: Optional[ServerRunnerService] = None,
    ) -> None:
        self._hub = hub
        self._server_runner = server_runner

    async def dispatch(
        self,
        *,
        run_id: str,
        serial: str,
        agent_id: Optional[str],
        goal: str,
        engine: str,
        dispatch_source: str,
        platform: str = "android",
        attempt: int = 1,
    ) -> Dict[str, Any]:
        if agent_id is None:
            return {"dispatched": False, "execution_mode": "agent_brain"}

        if engine == "vlm" and self._server_runner is not None:
            ok = await self._server_runner.start_run(
                run_id=run_id,
                serial=serial,
                agent_id=agent_id,
                goal=goal,
                dispatch_source=dispatch_source,
                platform=platform,
                attempt=attempt,
            )
            return {"dispatched": ok, "execution_mode": "server_brain"}

        await self._hub.bind_run(run_id, agent_id)
        dispatched = await self._hub.send_to_agent(
            agent_id,
            {
                "type": P.MSG_START_RUN,
                "run_id": run_id,
                "device_serial": serial,
                "goal": goal,
                "engine": engine,
                "attempt": max(1, int(attempt or 1)),
            },
        )
        if not dispatched:
            await self._hub.unbind_run(run_id)
        return {"dispatched": dispatched, "execution_mode": "agent_brain"}

    async def wait_until_not_running(self, run_id: str, *, timeout_sec: float = 2.0) -> bool:
        if self._server_runner is None:
            return True
        deadline = time.monotonic() + timeout_sec
        while self._server_runner.is_running(run_id):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def stop(self, run_id: str, *, execution_mode: str) -> bool:
        if execution_mode == "server_brain" and self._server_runner is not None:
            return await self._server_runner.stop_run(run_id)
        return False


__all__ = ["RunDispatchService"]
