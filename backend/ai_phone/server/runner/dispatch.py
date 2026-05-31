"""RunDispatchService：API / scheduler 共用的 Run 派发入口。

Distributed Agent Brain：所有 engine（含 vlm）都派发给 Agent 本地执行，
Server 不再在进程内跑 VLMRunner。派发只是把 ``MSG_START_RUN`` 发给目标 Agent；
取消通过 ``MSG_STOP_RUN`` 下发，由 Agent 本地收口。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger

from ai_phone.shared import protocol as P

from ..db import get_session_factory
from ..device_config.resolver import resolve_wake_decision
from ..hub import Hub


class RunDispatchService:
    def __init__(
        self,
        *,
        hub: Hub,
        session_factory=None,
    ) -> None:
        self._hub = hub
        self._session_factory = session_factory

    async def dispatch(
        self,
        *,
        run_id: str,
        serial: str,
        agent_id: Optional[str],
        goal: str,
        function_map_context: Optional[str] = None,
        engine: str,
        dispatch_source: str,
        platform: str = "android",
        attempt: int = 1,
    ) -> Dict[str, Any]:
        if agent_id is None:
            return {"dispatched": False, "execution_mode": "agent_brain"}

        wake_policy: Dict[str, bool] = {}
        if str(platform or "").strip().lower() == "harmony":
            wake_policy = await self._resolve_wake_policy(serial=serial, platform=platform)
        payload: Dict[str, Any] = {
            "type": P.MSG_START_RUN,
            "run_id": run_id,
            "device_serial": serial,
            "goal": goal,
            "engine": engine,
            "attempt": max(1, int(attempt or 1)),
        }
        if function_map_context:
            payload["function_map_context"] = function_map_context
            payload["functionMapContext"] = function_map_context
        if wake_policy:
            payload["wake_policy"] = wake_policy
        # 注：执行配置不随 start_run 下发——它在 Agent 连接时由 MSG_AGENT_CONFIG
        # 统一下发并覆盖本机 Settings（配置集中分发，全局一份；见 M2）。
        # M4 片3b：下发本 run 的 effective_cache_mode，Agent 首跑（未命中）据此决定
        # 成功后是否归档成品缓存并回传（off 不下发、不归档）。
        effective_cache_mode = await self._resolve_effective_cache_mode(run_id)
        if effective_cache_mode and effective_cache_mode != "off":
            payload["cache_mode"] = effective_cache_mode
        # M4：命中缓存随 start_run 下发回放快照（只下发命中那条），未命中照常首跑。
        snapshot = await self._maybe_build_cache_snapshot(run_id=run_id, serial=serial, goal=goal)
        if snapshot:
            payload["cache_snapshot"] = snapshot

        await self._hub.bind_run(run_id, agent_id)
        dispatched = await self._hub.send_to_agent(
            agent_id,
            payload,
        )
        if not dispatched:
            await self._hub.unbind_run(run_id)
        return {"dispatched": dispatched, "execution_mode": "agent_brain"}

    async def _resolve_wake_policy(self, *, serial: str, platform: str) -> Dict[str, bool]:
        session_factory = self._session_factory or get_session_factory()
        async with session_factory() as session:
            return await resolve_wake_decision(session, serial, platform)

    async def _maybe_build_cache_snapshot(
        self, *, run_id: str, serial: str, goal: str
    ) -> Optional[Dict[str, Any]]:
        """命中缓存则返回下发快照；off / 未命中 / 异常一律返回 None（降级首跑）。

        命中查询留 Server（成品存储见 repository、归档在 Agent）；这里只在派发前查一次、把命中那条随 start_run 带走。
        """
        from ..models import Run  # noqa: PLC0415
        from ..trajectory_cache.snapshot import build_cache_snapshot  # noqa: PLC0415

        session_factory = self._session_factory or get_session_factory()
        try:
            async with session_factory() as session:
                run = await session.get(Run, run_id)
                mode = str(getattr(run, "effective_cache_mode", "") or "off") if run else "off"
            return await build_cache_snapshot(
                session_factory, device_serial=serial, goal=goal, effective_cache_mode=mode
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("构建缓存快照失败（降级为首跑）run_id={}: {}", run_id, exc)
            return None

    async def _resolve_effective_cache_mode(self, run_id: str) -> str:
        """查本 run 的 effective_cache_mode（off/v1/v2/v3）；查不到 / 异常返回 off。"""
        from ..models import Run  # noqa: PLC0415

        session_factory = self._session_factory or get_session_factory()
        try:
            async with session_factory() as session:
                run = await session.get(Run, run_id)
                return str(getattr(run, "effective_cache_mode", "") or "off") if run else "off"
        except Exception:  # noqa: BLE001
            return "off"

    async def wait_until_not_running(self, run_id: str, *, timeout_sec: float = 2.0) -> bool:
        """Distributed Agent Brain 下恒为可派发。

        历史 server_brain 下这里要等 Server 进程内 VLMRunner 停下再派发；执行脑
        下沉回 Agent 后，Server 不再持有进程内 run，无需等待。保留方法签名是为了
        兼容 scheduler / API 既有调用点（retry 的资源占用保护分支因此恒通过）。
        """
        return True

    async def stop(self, run_id: str, *, execution_mode: str = "agent_brain") -> bool:
        """下发停止指令给承载该 Run 的 Agent，由 Agent 本地收口。

        ``execution_mode`` 入参保留是为兼容旧调用签名；Distributed Agent Brain
        下统一走 Agent 本地 stop，不再有 Server 进程内 runner 需要区分。
        """
        return await self._hub.send_to_run(
            run_id,
            {"type": P.MSG_STOP_RUN, "run_id": run_id},
        )


__all__ = ["RunDispatchService"]
