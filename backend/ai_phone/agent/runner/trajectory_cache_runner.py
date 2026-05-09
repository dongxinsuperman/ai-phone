"""Agent 本地轨迹缓存回放 runner。

main 分支仍是 Agent 本地 driver 架构，缓存命中后不能套用 next/server-brain 的
Server runner 挂点。这里保持独立 runner：只消费 server 下发的 trajectory_json，
通过本地 BaseDriver 回放动作，最后走缓存专用断言。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.runner.events import EVT_LOG, EVT_RUN_FINISH, EVT_TOKEN_SUMMARY, make_event
from ai_phone.config import Settings, get_settings
from ai_phone.server.trajectory_cache import CacheReplayAssertionVerifier, ReplayRunner


class TrajectoryCacheRunner:
    """缓存回放 runner，接口与 VLMRunner/MidsceneRunner 对齐。"""

    def __init__(
        self,
        *,
        run_id: str,
        serial: str,
        driver: BaseDriver,
        goal: str,
        trajectory: Dict[str, Any],
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.run_id = run_id
        self.serial = serial
        self.driver = driver
        self.goal = goal
        self.trajectory = trajectory
        self.emit = emit
        self.settings = settings or get_settings()

    async def run(self) -> None:
        started = time.monotonic()
        actions = list(self.trajectory.get("actions") or [])
        await self._log(1, "轨迹缓存回放", f"开始回放 actions={len(actions)}")
        runner = ReplayRunner(
            driver=self.driver,
            trajectory=self.trajectory,
            run_id=self.run_id,
            log=self._log,
            emit=self.emit,
            capture_after_each_action=True,
        )
        replay_result = await runner.run()
        if not replay_result.success:
            await self._finish(
                ok=False,
                reason=f"error: trajectory_replay_failed: {replay_result.error}",
                steps=replay_result.actions_executed,
                started=started,
            )
            return

        final_frame = await runner.capture_final_frame()
        verifier = CacheReplayAssertionVerifier(settings=self.settings)
        assertion = await verifier.verify(
            goal=self.goal,
            final_bytes=final_frame,
            trajectory=self.trajectory,
            prev_before_bytes=replay_result.final_before_bytes,
        )
        await self._log(
            1 if assertion.verdict == "PASS" else 3,
            "轨迹缓存断言",
            f"{assertion.verdict}: {assertion.reason}",
        )
        summary = verifier.counter.summary()
        summary["vlm_backend"] = self.settings.vlm_backend
        self._emit(make_event(EVT_TOKEN_SUMMARY, self.run_id, **summary))
        if assertion.passed:
            await self._finish(
                ok=True,
                reason=f"finished: trajectory_cache_pass: {assertion.reason}",
                steps=replay_result.actions_executed,
                started=started,
                token_stats=summary,
            )
            return
        await self._finish(
            ok=False,
            reason=(
                "assert_fail: trajectory_cache_assertion_"
                f"{assertion.verdict.lower()}: {assertion.reason}"
            ),
            steps=replay_result.actions_executed,
            started=started,
            token_stats=summary,
        )

    async def _log(self, level: int, title: str, content: str) -> None:
        self._emit(
            make_event(
                EVT_LOG,
                self.run_id,
                level=level,
                title=title,
                content=content,
            )
        )

    async def _finish(
        self,
        *,
        ok: bool,
        reason: str,
        steps: int,
        started: float,
        token_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._emit(
            make_event(
                EVT_RUN_FINISH,
                self.run_id,
                ok=ok,
                reason=reason,
                steps=steps,
                elapsed_ms=elapsed_ms,
                token_stats=token_stats or {},
            )
        )

    def _emit(self, evt: Dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(evt)
