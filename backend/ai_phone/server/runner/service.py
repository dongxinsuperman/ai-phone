"""ServerRunnerService：在 Server 进程内运行 VLMRunner。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_phone.agent.runner.events import log_event
from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.config import get_settings
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Run, RunCommand, RunLog
from ai_phone.shared import protocol as P

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
        self._run_agents: Dict[str, str] = {}
        self._agent_runs: Dict[str, set[str]] = {}

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
                        params=dict(payload.get("params") or {}),
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
        self._run_agents[run_id] = agent_id
        self._agent_runs.setdefault(agent_id, set()).add(run_id)
        return True

    async def stop_run(self, run_id: str, *, reason: str = "stopped_by_user") -> bool:
        return await self._force_finish_run(
            run_id,
            result="cancelled",
            message=reason,
            cancel_reason=reason,
        )

    async def handle_agent_disconnected(self, agent_id: str) -> int:
        """Agent WS 断开时，立刻终结其承载的 server_brain Run。

        老 agent_brain 路径会等 Agent 自己回 ``run_done`` 或 API stop；Server 大脑
        的 runner task 在 Server 进程里，若 Agent 掉线后不主动收口，可能会卡在
        下一次 driver RPC / VLM 调用之前。这里按 agent_id 精确取消。
        """
        run_ids = list(self._agent_runs.get(agent_id) or [])
        finished = 0
        for run_id in run_ids:
            ok = await self._force_finish_run(
                run_id,
                result="error",
                message=f"agent_offline: {agent_id}",
                error_class="AgentOffline",
                error_category="agent_offline",
                cancel_reason="agent disconnected",
            )
            if ok:
                finished += 1
        return finished

    async def recover_stale_runs(self, *, reason: str = "server_restarted") -> int:
        """Server 启动时把上次进程遗留的 server_brain running/pending Run 归位。

        这些 Run 的执行 task 已随旧进程消失，不可能继续完成；如果不显式落终态，
        Web / scheduler 会长期看到 running。这里按 failed 收口，并复用
        ``on_run_done`` 回调让 SubmissionItem 也同步落位。
        """
        async with self._session_factory() as session:
            res = await session.execute(
                select(Run).where(
                    Run.execution_mode == "server_brain",
                    Run.status.in_(("pending", "running")),
                )
            )
            runs = list(res.scalars().all())
            now = datetime.now(timezone.utc)
            for run in runs:
                run.status = "failed"
                run.reason = reason
                run.finished_at = now
                session.add(
                    RunLog(
                        run_id=run.id,
                        level=3,
                        title="Run failed",
                        content=reason,
                        error_class="ServerRestarted",
                        error_category="network",
                    )
                )
            await session.commit()

        for run in runs:
            payload = {
                "type": P.MSG_RUN_DONE,
                "run_id": run.id,
                "serial": run.device_serial,
                "result": "error",
                "message": reason,
                "steps": run.steps or 0,
                "elapsed_ms": run.elapsed_ms or 0,
                "token_stats": run.token_summary or {},
            }
            await self._hub.unbind_run(run.id)
            await self._hub.broadcast_to_serial(run.device_serial, payload)
            if self._on_run_done is not None:
                try:
                    await self._on_run_done(run.id, payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stale run 回调 scheduler 失败 run_id={}: {}", run.id, exc)
        if runs:
            logger.warning("server_brain stale runs recovered: {}", len(runs))
        return len(runs)

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
            replay_done = await self._maybe_run_trajectory_cache(
                run_id=run_id,
                goal=goal,
                driver=driver,
                emitter=emitter,
            )
            if replay_done:
                return
            # 首跑用 emitter.aemit（顺序保序版）而非 emitter.emit：
            # emit 把 EVT_LOG/EVT_STEP_END/EVT_SCREENSHOT 丢进后台 ensure_future，
            # 调度无序会让"#1 第 1 步完成"被甩到"#2 ━━ 第 2 步 ━━"之后；
            # aemit 用 _serial_lock 串行 await，保证调用顺序就是 DB/WS 写入顺序。
            # VLMRunner._emit_event 已经 `await _maybe_await(result)`，
            # callback 返回 coroutine 即可被 await 透明衔接。
            runner = self._runner_factory(
                run_id=run_id,
                driver=driver,
                goal=goal,
                emit=emitter.aemit,
            )
            await runner.run()
        except asyncio.CancelledError:
            await emitter.force_finish(result="cancelled", message="stopped_by_server")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ServerRunnerService run 异常 run_id={}: {}", run_id, exc)
            error_class = exc.__class__.__name__
            error_category = "network"
            message = f"server_runner_crash: {exc}"
            if isinstance(exc, RemoteDriverError):
                error_class = exc.error_class
                error_category = exc.category
                message = exc.message
            await emitter.force_finish(
                result="error",
                message=message,
                error_class=error_class,
                error_category=error_category,
            )
        finally:
            await emitter.aclose()
            self._tasks.pop(run_id, None)
            self._emitters.pop(run_id, None)
            agent_id = self._run_agents.pop(run_id, None)
            if agent_id is not None:
                runs = self._agent_runs.get(agent_id)
                if runs is not None:
                    runs.discard(run_id)
                    if not runs:
                        self._agent_runs.pop(agent_id, None)

    async def _maybe_run_trajectory_cache(
        self,
        *,
        run_id: str,
        goal: str,
        driver: RemoteDriver,
        emitter: ServerRunEmitter,
    ) -> bool:
        settings = get_settings()
        if not bool(getattr(settings, "trajectory_cache_enabled", False)):
            return False
        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            cache_mode = str(getattr(run, "effective_cache_mode", "") or "off")
        if cache_mode == "off":
            return False
        if cache_mode == "v3":
            return await self._run_trajectory_cache_v3(
                run_id=run_id,
                goal=goal,
                driver=driver,
                emitter=emitter,
                settings=settings,
            )
        if cache_mode not in {"v1", "v2"}:
            emitter.emit(log_event(run_id, 1, "轨迹缓存", f"cacheMode={cache_mode} 不支持，继续走 VLMRunner"))
            return False

        from ai_phone.server.trajectory_cache import (  # noqa: PLC0415
            CacheEphemeralGateVerifier,
            CacheReplayAssertionVerifier,
            V1ReplayRunner,
            V2ReplayRunner,
            get_active_trajectory_cache_v1,
            get_active_trajectory_cache_v2,
        )
        from ai_phone.server.trajectory_cache.recovery import (  # noqa: PLC0415
            CacheReplayRecoveryVerifier,
        )
        from ai_phone.shared.llm import TokenCounter  # noqa: PLC0415

        # 缓存通道在最终断言里会调一次断言 VLM；这里准备一个独立 counter，
        # 用完后通过 force_finish 透传给 emitter，让"任务总耗时""Token 统计"
        # 这两条历史在缓存通道也能露出（recovery 走 httpx 自调，不在此口径）。
        replay_started_at = time.monotonic()
        assertion_counter = TokenCounter()

        def _build_token_stats() -> Dict[str, Any]:
            if assertion_counter.call_count <= 0:
                return {}
            stats = assertion_counter.summary()
            stats["vlm_backend"] = settings.vlm_backend or ""
            return stats

        def _elapsed_ms() -> int:
            return int((time.monotonic() - replay_started_at) * 1000)

        async def _log(level: int, title: str, content: str) -> None:
            # 缓存回放日志是用户排查执行链路的主线，必须按 await 顺序落库/广播。
            # 不能走 emitter.emit() 的后台队列，否则步骤开始/完成在 UI 上会重排。
            event = log_event(run_id, level, title, content)
            forward_log = getattr(emitter, "_forward_log", None)
            if callable(forward_log):
                await forward_log(event)
            else:
                emitter.emit(event)

        get_cache = (
            get_active_trajectory_cache_v1
            if cache_mode == "v1"
            else get_active_trajectory_cache_v2
        )
        cache = await get_cache(
            self._session_factory,
            device_code=driver.serial,
            run_semantic_text=goal,
        )
        if cache is None:
            await _log(1, "轨迹缓存", "未命中，继续走现有 VLMRunner")
            return False

        trajectory = dict(cache.get("trajectory_json") or {})
        cache_key = str(cache.get("cache_key") or "")
        await _log(
            1,
            "轨迹缓存",
            f"命中轨迹回放 cache_key={cache_key[:12]}",
        )

        recovery_verifier: Optional[CacheReplayRecoveryVerifier] = None
        if cache_mode == "v2" and settings.trajectory_cache_recovery_vlm_enabled:
            # recovery 是实时重新问当前配置的 VLM 输出局部修复动作；坐标空间
            # 必须跟当前 recovery/gate 使用的模型配置走，而不是跟历史缓存来源走。
            recovery_verifier = CacheReplayRecoveryVerifier(
                settings=settings,
                main_vlm_backend=getattr(settings, "vlm_backend", ""),
            )
            problem = recovery_verifier.configuration_problem()
            if problem:
                await _log(2, "轨迹缓存", f"recovery_vlm 已开启但配置不完整：{problem}")
            else:
                recovery_model = (
                    settings.vlm_model
                    if str(getattr(settings, "vlm_backend", "") or "").strip().lower()
                    in {"claude_cu", "gpt_cu"}
                    else getattr(settings, "trajectory_cache_recovery_vlm_model", "")
                )
                recovery_backend = (
                    settings.vlm_backend
                    if str(getattr(settings, "vlm_backend", "") or "").strip().lower()
                    in {"claude_cu", "gpt_cu"}
                    else getattr(settings, "trajectory_cache_recovery_vlm_backend", "")
                )
                await _log(
                    1,
                    "轨迹缓存",
                    (
                        "recovery_vlm 专线已启用，"
                        f"backend={recovery_backend} "
                        f"model={recovery_model} "
                        f"max_wait_more={settings.trajectory_cache_recovery_vlm_max_wait_more} "
                        f"max_calls={settings.trajectory_cache_recovery_vlm_max_calls_per_replay} "
                        f"timeout={settings.trajectory_cache_recovery_vlm_timeout_sec:.0f}s"
                    ),
                )

        ephemeral_gate_verifier: Optional[CacheEphemeralGateVerifier] = None
        if (
            bool(getattr(settings, "trajectory_cache_ephemeral_action_enabled", False))
            and cache_mode == "v2"
            and bool(getattr(settings, "trajectory_cache_ephemeral_gate_enabled", True))
        ):
            ephemeral_gate_verifier = CacheEphemeralGateVerifier(
                settings=settings,
                main_vlm_backend=getattr(settings, "vlm_backend", ""),
            )
            problem = ephemeral_gate_verifier.configuration_problem()
            if problem:
                await _log(2, "轨迹缓存", f"ephemeral gate 已开启但配置不完整：{problem}")
            else:
                await _log(
                    1,
                    "轨迹缓存",
                    (
                        "ephemeral gate 已启用，"
                        f"max_calls={getattr(settings, 'trajectory_cache_ephemeral_gate_max_calls', 3)} "
                        f"use_recovery_config="
                        f"{getattr(settings, 'trajectory_cache_ephemeral_gate_use_recovery_vlm_config', True)}"
                    ),
                )

        runner_cls = V1ReplayRunner if cache_mode == "v1" else V2ReplayRunner
        runner = runner_cls(
            driver=driver,
            trajectory=trajectory,
            run_id=run_id,
            log=_log,
            emit=emitter.emit,
            capture_after_each_action=True,
            recovery_verifier=recovery_verifier,
            ephemeral_gate_verifier=ephemeral_gate_verifier,
            goal=goal,
        )
        replay_result = await runner.run()
        # 注意：alignment_miss / replay_failed 都发生在调用断言 VLM 之前，
        # token_stats 多半为空；但 elapsed_ms / steps 仍要传，保持单 case
        # 报告的"任务总耗时""执行步数"在失败分支也能正确归档。
        if not replay_result.success:
            error = str(replay_result.error or "")
            common_kwargs: Dict[str, Any] = {
                "elapsed_ms": replay_result.elapsed_ms or _elapsed_ms(),
                "steps": replay_result.actions_executed,
                "token_stats": _build_token_stats(),
            }
            if "alignment_miss" in error:
                await emitter.force_finish(
                    result="assert_fail",
                    message=f"trajectory_cache_alignment_fail: {error}",
                    error_class="TrajectoryCacheAlignmentError",
                    error_category="model",
                    **common_kwargs,
                )
                return True
            await emitter.force_finish(
                result="error",
                message=f"trajectory_replay_failed: {error}",
                error_class="TrajectoryReplayError",
                error_category="device",
                **common_kwargs,
            )
            return True

        # 断言入口：runner.capture_final_frame() 会强制等到最后一帧稳定
        # 再返回，避免最后一击触发跳转、断言拿到动画态空白图导致误判。
        # 详见 docs/缓存回放步骤化日志改造方案.md。
        await _log(1, "缓存稳定", "断言入口：等待最后一帧稳定后再交给断言系统…")
        final_frame = await runner.capture_final_frame()
        assertion = await CacheReplayAssertionVerifier(
            settings=settings,
            counter=assertion_counter,
        ).verify(
            goal=goal,
            final_bytes=final_frame,
            trajectory=trajectory,
            prev_before_bytes=replay_result.final_before_bytes,
        )
        emitter.emit(
            log_event(
                run_id,
                1 if assertion.verdict == "PASS" else 3,
                "轨迹缓存断言",
                f"{assertion.verdict}: {assertion.reason}",
            )
        )
        # 关掉断言后才知道整段缓存通道总耗时；优先用 ReplayRunner 内部计时，
        # 兜底再用 service 这一层的 wall-clock，保证至少有一个非零值。
        finish_kwargs: Dict[str, Any] = {
            "elapsed_ms": _elapsed_ms(),
            "steps": replay_result.actions_executed,
            "token_stats": _build_token_stats(),
            "token_summary_note": "仅缓存断言通道",
        }
        if assertion.passed:
            await emitter.force_finish(
                result="pass",
                message=f"trajectory_cache_pass: {assertion.reason}",
                **finish_kwargs,
            )
        else:
            await emitter.force_finish(
                result="assert_fail",
                message=f"trajectory_cache_assertion_{assertion.verdict.lower()}: {assertion.reason}",
                error_class="TrajectoryCacheAssertionError",
                error_category="model",
                **finish_kwargs,
            )
        return True

    async def _run_trajectory_cache_v3(
        self,
        *,
        run_id: str,
        goal: str,
        driver: RemoteDriver,
        emitter: ServerRunEmitter,
        settings: Any,
    ) -> bool:
        from ai_phone.server.trajectory_cache import (  # noqa: PLC0415
            CacheReplayAssertionVerifier,
            V3ReplayRunner,
            get_active_trajectory_cache_v3,
            mark_trajectory_cache_v3_suspect,
        )
        from ai_phone.shared.llm import TokenCounter  # noqa: PLC0415

        replay_started_at = time.monotonic()
        assertion_counter = TokenCounter()

        def _build_token_stats() -> Dict[str, Any]:
            if assertion_counter.call_count <= 0:
                return {}
            stats = assertion_counter.summary()
            stats["vlm_backend"] = settings.vlm_backend or ""
            return stats

        def _elapsed_ms() -> int:
            return int((time.monotonic() - replay_started_at) * 1000)

        async def _log(level: int, title: str, content: str) -> None:
            # V3 回放同样要求步骤边界严格有序，定位/辅助/完成块不能被后台日志重排。
            event = log_event(run_id, level, title, content)
            forward_log = getattr(emitter, "_forward_log", None)
            if callable(forward_log):
                await forward_log(event)
            else:
                emitter.emit(event)

        cache = await get_active_trajectory_cache_v3(
            self._session_factory,
            device_code=driver.serial,
            run_semantic_text=goal,
        )
        if cache is None:
            await _log(1, "V3缓存回放", "未命中缓存，继续走现有 VLMRunner")
            return False

        trajectory = dict(cache)
        cache_key = str(cache.get("cache_key") or "")
        await _log(
            1,
            "V3缓存回放",
            f"命中缓存：复用上次成功路线 cache_key={cache_key[:12]}",
        )

        runner = V3ReplayRunner(
            driver=driver,
            trajectory=trajectory,
            run_id=run_id,
            log=_log,
            emit=emitter.emit,
            capture_after_each_action=True,
            goal=goal,
            main_vlm_backend=trajectory.get("source_vlm_backend")
            or getattr(settings, "vlm_backend", ""),
        )
        replay_result = await runner.run()
        if not replay_result.success:
            error = str(replay_result.error or "")
            await mark_trajectory_cache_v3_suspect(
                self._session_factory,
                cache_key=cache_key,
                run_id=run_id,
                reason=f"replay_failed: {error}",
            )
            await emitter.force_finish(
                result="error",
                message=f"trajectory_cache_v3_replay_failed: {error}",
                error_class="TrajectoryCacheV3ReplayError",
                error_category="model" if "locator" in error else "device",
                elapsed_ms=replay_result.elapsed_ms or _elapsed_ms(),
                steps=replay_result.actions_executed,
                token_stats=_build_token_stats(),
            )
            return True

        # 断言入口：同 V1/V2，强制等到最后一帧稳定再交给断言（V3 用版本3
        # 稳定）。详见 docs/缓存回放步骤化日志改造方案.md。
        await _log(1, "缓存稳定", "断言入口：等待最后一帧稳定后再交给断言系统…")
        final_frame = await runner.capture_final_frame()
        assertion = await CacheReplayAssertionVerifier(
            settings=settings,
            counter=assertion_counter,
        ).verify(
            goal=goal,
            final_bytes=final_frame,
            trajectory=trajectory,
            prev_before_bytes=replay_result.final_before_bytes,
        )
        emitter.emit(
            log_event(
                run_id,
                1 if assertion.verdict == "PASS" else 3,
                "V3最终校验",
                f"{assertion.verdict}: {assertion.reason}",
            )
        )
        finish_kwargs: Dict[str, Any] = {
            "elapsed_ms": _elapsed_ms(),
            "steps": replay_result.actions_executed,
            "token_stats": _build_token_stats(),
            "token_summary_note": "仅 V3 缓存断言通道",
        }
        if assertion.passed:
            await emitter.force_finish(
                result="pass",
                message=f"trajectory_cache_v3_pass: {assertion.reason}",
                **finish_kwargs,
            )
        else:
            await mark_trajectory_cache_v3_suspect(
                self._session_factory,
                cache_key=cache_key,
                run_id=run_id,
                reason=f"assertion_{assertion.verdict.lower()}: {assertion.reason}",
            )
            await emitter.force_finish(
                result="assert_fail",
                message=f"trajectory_cache_v3_assertion_{assertion.verdict.lower()}: {assertion.reason}",
                error_class="TrajectoryCacheV3AssertionError",
                error_category="model",
                **finish_kwargs,
            )
        return True

    async def _write_run_log(
        self,
        run_id: str,
        *,
        level: int,
        title: str,
        content: str,
    ) -> None:
        async with self._session_factory() as session:
            session.add(RunLog(run_id=run_id, level=level, title=title, content=content))
            await session.commit()

    async def _force_finish_run(
        self,
        run_id: str,
        *,
        result: str,
        message: str,
        cancel_reason: str,
        error_class: str = "",
        error_category: str = "",
    ) -> bool:
        self._waiter.cancel_run(run_id, reason=cancel_reason)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        emitter = self._emitters.get(run_id)
        if emitter is not None:
            await emitter.force_finish(
                result=result,
                message=message,
                error_class=error_class,
                error_category=error_category,
            )
        return task is not None or emitter is not None


__all__ = ["ServerRunnerService"]
