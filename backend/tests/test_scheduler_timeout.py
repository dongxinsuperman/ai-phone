from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Run, Submission, SubmissionItem
from ai_phone.server.scheduler import service as scheduler_service
from ai_phone.server.scheduler.service import SubmissionScheduler, _RunTrack
from ai_phone.server.ws import agent_ws


class _RecordingDispatch:
    def __init__(self) -> None:
        self.stop_calls: list[str] = []
        self.dispatch_calls: list[Dict[str, Any]] = []

    async def stop(self, run_id: str) -> bool:
        self.stop_calls.append(run_id)
        return False

    async def dispatch(self, **kwargs: Any) -> Dict[str, Any]:
        self.dispatch_calls.append(dict(kwargs))
        return {"dispatched": True, "execution_mode": "agent_brain"}

    async def wait_until_not_running(self, _run_id: str) -> bool:
        return True


class _InspectingWS:
    def __init__(self, factory, item_id: str) -> None:
        self._factory = factory
        self._item_id = item_id
        self.messages: list[Dict[str, Any]] = []
        self.reason_seen_during_send: str | None = None

    async def send_json(self, payload: Dict[str, Any]) -> None:
        async with self._factory() as session:
            item = await session.get(SubmissionItem, self._item_id)
            self.reason_seen_during_send = item.status_reason if item else None
        self.messages.append(dict(payload))

    async def close(self, *args: Any, **kwargs: Any) -> None:
        return None


async def _seed_running_item(
    factory,
    *,
    run_id: str,
    item_id: str,
    serial: str,
    agent_id: str = "agent-current",
    status_reason: str | None = None,
    run_status: str = "running",
    run_finished: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(
            Submission(
                id=f"sub-{item_id}",
                state="accepted",
                expire_at=now + timedelta(hours=3),
            )
        )
        session.add(
            Run(
                id=run_id,
                device_serial=serial,
                agent_id=agent_id,
                agent_id_at_start="agent-at-start",
                goal="执行任务",
                status=run_status,
                finished_at=now if run_finished else None,
                dispatch_source="scheduler",
                effective_retry_max=3,
                attempts=1,
                last_attempt=1,
            )
        )
        session.add(
            SubmissionItem(
                id=item_id,
                submission_id=f"sub-{item_id}",
                case_id=f"case-{item_id}",
                platform="android",
                run_content="执行任务",
                state="running",
                status_reason=status_reason,
                run_id=run_id,
                device_serial=serial,
                effective_retry_max=3,
                attempts=1,
                started_at=now - timedelta(hours=2),
            )
        )
        await session.commit()


async def _track_running_item(
    scheduler: SubmissionScheduler,
    lock_store: DeviceLockStore,
    *,
    run_id: str,
    item_id: str,
    serial: str,
) -> None:
    lock = await lock_store.acquire(
        serial,
        holder=f"sched-{item_id}",
        holder_type="auto",
        ttl_seconds=7200,
        meta={"item_id": item_id},
    )
    scheduler._runs[run_id] = _RunTrack(
        item_id=item_id,
        submission_id=f"sub-{item_id}",
        platform="android",
        serial=serial,
        lock_token=lock.token,
        started_at_mono=time.monotonic() - scheduler_service.DEFAULT_ITEM_TTL_SEC - 1,
    )


@pytest.mark.asyncio
async def test_timeout_marks_reason_before_fallback_to_current_agent(_test_engine) -> None:
    """路由丢失时按 Run.agent_id 补投；发送前必须已经落 run_timeout。"""

    factory = db_module.get_session_factory()
    await _seed_running_item(
        factory,
        run_id="run-timeout-route",
        item_id="item-timeout-route",
        serial="S-TIMEOUT-ROUTE",
    )

    hub = Hub()
    ws = _InspectingWS(factory, "item-timeout-route")
    await hub.register_agent("agent-current", "current", "test", ws)  # type: ignore[arg-type]
    dispatch = _RecordingDispatch()
    lock_store = DeviceLockStore()
    scheduler = SubmissionScheduler(
        hub=hub,
        lock_store=lock_store,
        session_factory=factory,
        dispatch_service=dispatch,  # type: ignore[arg-type]
    )
    await _track_running_item(
        scheduler,
        lock_store,
        run_id="run-timeout-route",
        item_id="item-timeout-route",
        serial="S-TIMEOUT-ROUTE",
    )

    await scheduler._scan_timeouts()

    assert dispatch.stop_calls == ["run-timeout-route"]
    assert ws.messages == [{"type": "stop_run", "run_id": "run-timeout-route"}]
    assert ws.reason_seen_during_send == "run_timeout"
    # 没有 Agent 终态回复时不暗中清理：保持可见、保持锁，等待下轮再投。
    assert "run-timeout-route" in scheduler._runs
    assert lock_store.peek("S-TIMEOUT-ROUTE") is not None


@pytest.mark.asyncio
async def test_timeout_uses_configured_item_ttl_not_literal_hour(
    _test_engine,
    monkeypatch,
) -> None:
    """扫描边界读取既有配置常量，不能在实现里另写死 3600。"""

    monkeypatch.setattr(scheduler_service, "DEFAULT_ITEM_TTL_SEC", 90)
    factory = db_module.get_session_factory()
    await _seed_running_item(
        factory,
        run_id="run-expired-by-config",
        item_id="item-expired-by-config",
        serial="S-EXPIRED-CONFIG",
    )
    await _seed_running_item(
        factory,
        run_id="run-fresh-by-config",
        item_id="item-fresh-by-config",
        serial="S-FRESH-CONFIG",
    )
    dispatch = _RecordingDispatch()
    lock_store = DeviceLockStore()
    scheduler = SubmissionScheduler(
        hub=Hub(),
        lock_store=lock_store,
        session_factory=factory,
        dispatch_service=dispatch,  # type: ignore[arg-type]
    )
    await _track_running_item(
        scheduler,
        lock_store,
        run_id="run-expired-by-config",
        item_id="item-expired-by-config",
        serial="S-EXPIRED-CONFIG",
    )
    await _track_running_item(
        scheduler,
        lock_store,
        run_id="run-fresh-by-config",
        item_id="item-fresh-by-config",
        serial="S-FRESH-CONFIG",
    )
    scheduler._runs["run-fresh-by-config"].started_at_mono = time.monotonic() - 89

    await scheduler._scan_timeouts()

    assert dispatch.stop_calls == ["run-expired-by-config"]
    async with factory() as session:
        expired = await session.get(SubmissionItem, "item-expired-by-config")
        fresh = await session.get(SubmissionItem, "item-fresh-by-config")
        assert expired is not None and expired.status_reason == "run_timeout"
        assert fresh is not None and not fresh.status_reason


@pytest.mark.asyncio
async def test_timeout_terminal_never_retries_even_for_unknown_result(_test_engine) -> None:
    """run_timeout 是显式终态闸；不能依赖 result 恰好拼成 cancelled。"""

    factory = db_module.get_session_factory()
    await _seed_running_item(
        factory,
        run_id="run-timeout-terminal",
        item_id="item-timeout-terminal",
        serial="S-TIMEOUT-TERMINAL",
        status_reason="run_timeout",
        run_status="failed",
        run_finished=True,
    )
    dispatch = _RecordingDispatch()
    lock_store = DeviceLockStore()
    scheduler = SubmissionScheduler(
        hub=Hub(),
        lock_store=lock_store,
        session_factory=factory,
        dispatch_service=dispatch,  # type: ignore[arg-type]
    )
    await _track_running_item(
        scheduler,
        lock_store,
        run_id="run-timeout-terminal",
        item_id="item-timeout-terminal",
        serial="S-TIMEOUT-TERMINAL",
    )

    async def _no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    scheduler._finalize_and_publish = _no_publish  # type: ignore[method-assign]

    await scheduler.on_run_done(
        "run-timeout-terminal",
        {
            "type": "run_done",
            "run_id": "run-timeout-terminal",
            "attempt": 1,
            "result": "not_found",
            "message": "stop_run_not_found",
        },
    )

    assert dispatch.dispatch_calls == []
    assert "run-timeout-terminal" not in scheduler._runs
    assert lock_store.peek("S-TIMEOUT-TERMINAL") is None
    async with factory() as session:
        item = await session.get(SubmissionItem, "item-timeout-terminal")
        run = await session.get(Run, "run-timeout-terminal")
        assert item is not None and item.state == "cancelled"
        assert item.status_reason == "run_timeout"
        assert item.finished_at is not None
        assert run is not None and run.status == "stopped"
        assert run.reason == "run_timeout"


@pytest.mark.asyncio
async def test_real_success_before_timeout_reply_remains_success(_test_engine) -> None:
    """真实成功先到即收口成功；后到的“不存在”终态必须幂等跳过。"""

    factory = db_module.get_session_factory()
    await _seed_running_item(
        factory,
        run_id="run-timeout-success",
        item_id="item-timeout-success",
        serial="S-TIMEOUT-SUCCESS",
        status_reason="run_timeout",
    )
    lock_store = DeviceLockStore()
    scheduler = SubmissionScheduler(
        hub=Hub(),
        lock_store=lock_store,
        session_factory=factory,
    )
    await _track_running_item(
        scheduler,
        lock_store,
        run_id="run-timeout-success",
        item_id="item-timeout-success",
        serial="S-TIMEOUT-SUCCESS",
    )

    async def _no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    scheduler._finalize_and_publish = _no_publish  # type: ignore[method-assign]
    success = {
        "type": "run_done",
        "run_id": "run-timeout-success",
        "attempt": 1,
        "result": "finished",
        "message": "assertion passed",
    }
    assert await agent_ws._finalize_run("run-timeout-success", success) is True
    await scheduler.on_run_done("run-timeout-success", success)

    late_missing = {
        "type": "run_done",
        "run_id": "run-timeout-success",
        "attempt": 1,
        "result": "cancelled",
        "message": "stop_run_not_found",
    }
    assert await agent_ws._finalize_run("run-timeout-success", late_missing) is False

    async with factory() as session:
        item = await session.get(SubmissionItem, "item-timeout-success")
        run = await session.get(Run, "run-timeout-success")
        assert item is not None and item.state == "success"
        assert item.status_reason == "completed"
        assert run is not None and run.status == "success"
        assert run.reason == "assertion passed"
    assert lock_store.peek("S-TIMEOUT-SUCCESS") is None
