from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Run, Submission, SubmissionItem
from ai_phone.server.scheduler.service import SubmissionScheduler


class _CompletingDispatch:
    """收到 stop 后模拟 Agent 回一条晚到的 error 终态。"""

    def __init__(self) -> None:
        self.scheduler: Optional[SubmissionScheduler] = None
        self.stop_calls: list[str] = []
        self.dispatch_calls: list[Dict[str, Any]] = []

    async def stop(self, run_id: str) -> bool:
        self.stop_calls.append(run_id)
        assert self.scheduler is not None
        asyncio.create_task(
            self.scheduler.on_run_done(
                run_id,
                {
                    "type": "run_done",
                    "run_id": run_id,
                    "attempt": 1,
                    "result": "error",
                    "message": "late_error_after_cancel",
                },
            )
        )
        return True

    async def wait_until_not_running(self, _run_id: str) -> bool:
        return True

    async def dispatch(self, **kwargs: Any) -> Dict[str, Any]:
        self.dispatch_calls.append(dict(kwargs))
        return {"dispatched": True, "execution_mode": "agent_brain"}


@pytest.mark.asyncio
async def test_running_item_cancel_waits_for_terminal_and_never_retries(_test_engine) -> None:
    factory = db_module.get_session_factory()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(Submission(id="sub-cancel", state="accepted"))
        session.add(
            Run(
                id="run-cancel",
                device_serial="S1",
                goal="执行任务",
                status="running",
                effective_retry_max=3,
                attempts=1,
                last_attempt=1,
            )
        )
        session.add(
            SubmissionItem(
                id="item-cancel",
                submission_id="sub-cancel",
                case_id="case-cancel",
                platform="android",
                run_content="执行任务",
                state="running",
                run_id="run-cancel",
                device_serial="S1",
                effective_retry_max=3,
                attempts=1,
                started_at=now,
            )
        )
        await session.commit()

    lock_store = DeviceLockStore()
    await lock_store.acquire(
        "S1",
        holder="sched-item-cancel",
        holder_type="auto",
        ttl_seconds=600,
        meta={"item_id": "item-cancel"},
    )
    dispatch = _CompletingDispatch()
    scheduler = SubmissionScheduler(
        hub=Hub(),
        lock_store=lock_store,
        session_factory=factory,
        dispatch_service=dispatch,  # type: ignore[arg-type]
    )
    dispatch.scheduler = scheduler

    async def _no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    scheduler._finalize_and_publish = _no_publish  # type: ignore[method-assign]

    result = await scheduler.cancel_item("sub-cancel", "case-cancel", "android")

    assert result == {
        "submissionId": "sub-cancel",
        "caseId": "case-cancel",
        "platform": "android",
        "state": "cancelled",
        "stoppedRunId": "run-cancel",
    }
    assert dispatch.stop_calls == ["run-cancel"]
    assert dispatch.dispatch_calls == []
    assert lock_store.peek("S1") is None

    async with factory() as session:
        item = await session.get(SubmissionItem, "item-cancel")
        run = await session.get(Run, "run-cancel")
        assert item is not None and item.state == "cancelled"
        assert item.status_reason == "cancelled_by_request"
        assert item.finished_at is not None
        assert run is not None and run.status == "stopped"
        assert run.reason == "cancelled_by_request"


@pytest.mark.asyncio
async def test_batch_cancel_stops_running_and_removes_queued(_test_engine) -> None:
    factory = db_module.get_session_factory()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(Submission(id="sub-batch-cancel", state="accepted"))
        session.add(
            Run(
                id="run-batch-cancel",
                device_serial="S2",
                goal="执行中的任务",
                status="running",
            )
        )
        session.add_all(
            [
                SubmissionItem(
                    id="item-running",
                    submission_id="sub-batch-cancel",
                    case_id="case-running",
                    platform="android",
                    run_content="执行中的任务",
                    state="running",
                    run_id="run-batch-cancel",
                    device_serial="S2",
                    started_at=now,
                ),
                SubmissionItem(
                    id="item-queued",
                    submission_id="sub-batch-cancel",
                    case_id="case-queued",
                    platform="android",
                    run_content="排队中的任务",
                    state="queued",
                ),
            ]
        )
        await session.commit()

    lock_store = DeviceLockStore()
    await lock_store.acquire(
        "S2",
        holder="sched-item-running",
        holder_type="auto",
        ttl_seconds=600,
        meta={"item_id": "item-running"},
    )
    dispatch = _CompletingDispatch()
    scheduler = SubmissionScheduler(
        hub=Hub(),
        lock_store=lock_store,
        session_factory=factory,
        dispatch_service=dispatch,  # type: ignore[arg-type]
    )
    dispatch.scheduler = scheduler
    scheduler._queues["android"] = ["item-queued"]

    async def _no_publish(*_args: Any, **_kwargs: Any) -> None:
        return None

    scheduler._finalize_and_publish = _no_publish  # type: ignore[method-assign]

    result = await scheduler.cancel_submission("sub-batch-cancel")

    assert result == {
        "submissionId": "sub-batch-cancel",
        "cancelledQueued": ["item-queued"],
        "stoppedRunning": ["run-batch-cancel"],
    }
    assert scheduler._queues["android"] == []
    assert lock_store.peek("S2") is None

    async with factory() as session:
        running = await session.get(SubmissionItem, "item-running")
        queued = await session.get(SubmissionItem, "item-queued")
        submission = await session.get(Submission, "sub-batch-cancel")
        assert running is not None and running.state == "cancelled"
        assert queued is not None and queued.state == "cancelled"
        assert submission is not None and submission.state == "cancelled"
