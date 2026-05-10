from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from ai_phone.agent.runner.events import EVT_RUN_FINISH, make_event, log_event
from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Device, Run, RunCommand, Submission, SubmissionItem
from ai_phone.server.runner.dispatch import RunDispatchService
from ai_phone.server.runner.emitter import ServerRunEmitter
from ai_phone.server.runner.rpc import DriverRpcWaiter
from ai_phone.server.runner.service import ServerRunnerService
from ai_phone.server.scheduler.service import SubmissionScheduler
from ai_phone.server.submissions import ResultPublisher
from ai_phone.shared.protocol import MSG_DRIVER_RESULT


class FakeWS:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent.append(payload)


class MalformedScreenshotWS(FakeWS):
    def __init__(self, waiter: DriverRpcWaiter) -> None:
        super().__init__()
        self.waiter = waiter

    async def send_json(self, payload: Dict[str, Any]) -> None:
        await super().send_json(payload)
        self.waiter.resolve(
            {
                "type": MSG_DRIVER_RESULT,
                "message_id": payload["message_id"],
                "run_id": payload["run_id"],
                "serial": payload["serial"],
                "ok": True,
                "result": {"encoding": "base64", "mime": "image/jpeg"},
                "elapsed_ms": 1,
            }
        )


class SlowBroadcastHub(Hub):
    async def broadcast_to_serial(self, serial: str, payload: Dict[str, Any]) -> int:
        await asyncio.sleep(0.03)
        return await super().broadcast_to_serial(serial, payload)


class FastSuccessRunner:
    def __init__(self, *, run_id, driver, goal, emit):  # noqa: ANN001
        self.run_id = run_id
        self.emit = emit

    async def run(self):
        self.emit(log_event(self.run_id, 1, "fake runner", "server brain ok"))
        self.emit(
            make_event(
                EVT_RUN_FINISH,
                self.run_id,
                ok=True,
                reason="finished: done",
                steps=1,
                elapsed_ms=123,
                token_stats={"total_tokens": 7},
            )
        )


class HangingRunner:
    def __init__(self, *, run_id, driver, goal, emit):  # noqa: ANN001
        self.run_id = run_id
        self.emit = emit
        self._event = asyncio.Event()

    async def run(self):
        self.emit(log_event(self.run_id, 1, "hanging runner", "started"))
        await self._event.wait()


class ScreenshotRunner:
    def __init__(self, *, run_id, driver, goal, emit):  # noqa: ANN001
        self.run_id = run_id
        self.driver = driver

    async def run(self):
        await asyncio.to_thread(self.driver.screenshot_jpeg)


class MemoryPublisher(ResultPublisher):
    name = "memory"

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_api_run_dispatches_to_server_brain(app, client, session):
    hub = Hub()
    fake_ws = FakeWS()
    await hub.register_agent("agent-server-brain", "agent", "test", fake_ws)
    await hub.set_devices("agent-server-brain", {"S1"})
    app.state.hub = hub
    waiter = DriverRpcWaiter()
    app.state.driver_rpc_waiter = waiter
    app.state.server_runner_service = ServerRunnerService(
        hub=hub,
        lock_store=app.state.lock_store,
        session_factory=db_module.get_session_factory(),
        waiter=waiter,
        runner_factory=FastSuccessRunner,
    )
    app.state.run_dispatch_service = RunDispatchService(
        hub=hub,
        server_runner=app.state.server_runner_service,
    )

    session.add(
        Device(
            serial="S1",
            platform="android",
            status="online",
            agent_id="agent-server-brain",
        )
    )
    await session.commit()

    resp = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["dispatched"] is True
    assert body["execution_mode"] == "server_brain"
    assert fake_ws.sent == []

    run_id = body["id"]
    deadline = time.time() + 2.0
    detail = None
    while time.time() < deadline:
        await asyncio.sleep(0.02)
        got = await client.get(f"/api/runs/{run_id}")
        detail = got.json()
        if detail["status"] == "success":
            break

    assert detail is not None
    assert detail["status"] == "success"
    assert detail["reason"] == "done"
    assert detail["steps"] == 1
    assert detail["elapsed_ms"] == 123
    assert detail["token_summary"]["total_tokens"] == 7

    logs = await client.get(f"/api/runs/{run_id}/logs")
    assert logs.status_code == 200
    assert any(item["title"] == "fake runner" for item in logs.json()["items"])


@pytest.mark.asyncio
async def test_scheduler_dispatches_to_server_brain_and_finalizes(app, session):
    hub = Hub()
    fake_ws = FakeWS()
    await hub.register_agent("agent-server-brain", "agent", "test", fake_ws)
    await hub.set_devices("agent-server-brain", {"S1"})
    hub.set_device_readiness("S1", {"ready": True, "platform": "android"})

    lock_store = DeviceLockStore()
    waiter = DriverRpcWaiter()
    runner = ServerRunnerService(
        hub=hub,
        lock_store=lock_store,
        session_factory=db_module.get_session_factory(),
        waiter=waiter,
        runner_factory=FastSuccessRunner,
    )
    dispatch = RunDispatchService(hub=hub, server_runner=runner)
    publisher = MemoryPublisher()
    scheduler = SubmissionScheduler(
        hub=hub,
        lock_store=lock_store,
        session_factory=db_module.get_session_factory(),
        publisher=publisher,
        dispatch_service=dispatch,
    )
    runner.set_on_run_done(scheduler.on_run_done)

    now = datetime.now(timezone.utc)
    sub = Submission(
        origin="internal",
        submission_name="server-brain-submission",
        state="accepted",
        raw_body={},
        accepted_at=now,
        expire_at=now + timedelta(hours=1),
    )
    item = SubmissionItem(
        submission=sub,
        case_id="case-1",
        case_name="case-1",
        platform="android",
        run_content="do it",
        state="queued",
    )
    session.add_all(
        [
            Device(
                serial="S1",
                platform="android",
                status="online",
                agent_id="agent-server-brain",
            ),
            sub,
            item,
        ]
    )
    await session.commit()
    item_id = item.id

    dispatched = await scheduler._try_dispatch("android", item_id)
    assert dispatched is True

    deadline = time.time() + 2.0
    run_id = None
    detail = None
    while time.time() < deadline:
        await asyncio.sleep(0.02)
        async with db_module.get_session_factory()() as s:
            refreshed = await s.get(SubmissionItem, item_id)
            if refreshed is not None and refreshed.state == "success":
                run_id = refreshed.run_id
                detail = refreshed
                break

    assert detail is not None
    assert detail.status_reason == "completed"
    assert run_id is not None
    assert lock_store.peek("S1") is None
    assert fake_ws.sent == []

    async with db_module.get_session_factory()() as s:
        run = await s.get(Run, run_id)
        assert run is not None
        assert run.status == "success"
        assert run.execution_mode == "server_brain"
        assert run.dispatch_source == "scheduler"
        assert run.agent_id_at_start == "agent-server-brain"

    terminal_events = [e for e in publisher.events if e.get("event") == "submission.item.terminal"]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_server_emitter_finalizes_run_once_under_stop_race(app, session):
    run = Run(
        id="race-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="race-run",
        serial="S1",
        hub=SlowBroadcastHub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )

    force_task = asyncio.create_task(
        emitter.force_finish(result="cancelled", message="stopped_by_user")
    )
    await asyncio.sleep(0)
    runner_finish_task = asyncio.create_task(
        emitter._forward_run_finish(  # noqa: SLF001
            make_event(
                EVT_RUN_FINISH,
                "race-run",
                ok=False,
                reason="cancelled: cancelled",
                steps=4,
                elapsed_ms=33333,
            )
        )
    )
    await asyncio.gather(force_task, runner_finish_task)

    async with db_module.get_session_factory()() as s:
        got = await s.get(Run, "race-run")
        assert got is not None
        assert got.status == "stopped"
        assert got.reason == "stopped_by_user"
        assert got.steps == 0
        assert got.elapsed_ms == 0


@pytest.mark.asyncio
async def test_server_emitter_force_finish_with_explicit_metrics(app, session):
    """缓存通道显式传 elapsed_ms/steps/token_stats 时必须按原样写入。

    防止 force_finish 又把可知的"任务总耗时""执行步数""Token 统计"硬编码
    成 0 / 空：单 case 报告、批次累计耗时、缓存加速度量化都依赖这三个字段。
    """
    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    run = Run(
        id="finish-metrics",
        device_serial="S1",
        goal="g",
        status="running",
        execution_mode="server_brain",
        started_at=started,
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="finish-metrics",
        serial="S1",
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )
    await emitter.force_finish(
        result="pass",
        message="ok",
        elapsed_ms=4321,
        steps=7,
        token_stats={
            "call_count": 1,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 0,
        },
        token_summary_note="仅缓存断言通道",
    )
    await emitter.aclose()

    async with db_module.get_session_factory()() as s:
        got = await s.get(Run, "finish-metrics")
        assert got is not None
        assert got.status == "success"
        assert got.steps == 7
        assert got.elapsed_ms == 4321
        assert got.token_summary.get("total_tokens") == 120


@pytest.mark.asyncio
async def test_server_emitter_force_finish_falls_back_to_wallclock(app, session):
    """没传 elapsed_ms 时用 run.started_at 兜底，避免归零。"""
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    run = Run(
        id="finish-fallback",
        device_serial="S1",
        goal="g",
        status="running",
        execution_mode="server_brain",
        started_at=started,
        steps=3,
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="finish-fallback",
        serial="S1",
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )
    await emitter.force_finish(result="error", message="oops")
    await emitter.aclose()

    async with db_module.get_session_factory()() as s:
        got = await s.get(Run, "finish-fallback")
        assert got is not None
        assert got.status == "failed"
        # steps 没传 → 沿用 DB 里已经累计的 3
        assert got.steps == 3
        # elapsed_ms 没传 → 至少要 ≥ 1500ms（started 在 2s 前），避免归零
        assert got.elapsed_ms >= 1500


@pytest.mark.asyncio
async def test_agent_disconnect_fails_active_server_brain_run(app, session):
    hub = Hub()
    fake_ws = FakeWS()
    await hub.register_agent("agent-offline", "agent", "test", fake_ws)
    waiter = DriverRpcWaiter()
    service = ServerRunnerService(
        hub=hub,
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
        waiter=waiter,
        runner_factory=HangingRunner,
    )
    session.add(
        Run(
            id="agent-offline-run",
            device_serial="S1",
            agent_id="agent-offline",
            goal="g",
            status="pending",
        )
    )
    await session.commit()

    assert await service.start_run(
        run_id="agent-offline-run",
        serial="S1",
        agent_id="agent-offline",
        goal="g",
        dispatch_source="api",
    )
    assert service.is_running("agent-offline-run")

    finished = await service.handle_agent_disconnected("agent-offline")
    assert finished == 1

    deadline = time.time() + 2.0
    got = None
    while time.time() < deadline:
        await asyncio.sleep(0.02)
        async with db_module.get_session_factory()() as s:
            got = await s.get(Run, "agent-offline-run")
            if got is not None and got.status == "failed":
                break

    assert got is not None
    assert got.status == "failed"
    assert got.reason == "agent_offline: agent-offline"
    assert got.agent_offline_at is not None
    assert waiter.in_flight == 0


@pytest.mark.asyncio
async def test_recover_stale_server_brain_run_finalizes_scheduler_item(app, session):
    hub = Hub()
    lock_store = DeviceLockStore()
    waiter = DriverRpcWaiter()
    publisher = MemoryPublisher()
    dispatch = RunDispatchService(hub=hub, server_runner=None)
    scheduler = SubmissionScheduler(
        hub=hub,
        lock_store=lock_store,
        session_factory=db_module.get_session_factory(),
        publisher=publisher,
        dispatch_service=dispatch,
    )
    service = ServerRunnerService(
        hub=hub,
        lock_store=lock_store,
        session_factory=db_module.get_session_factory(),
        waiter=waiter,
        runner_factory=HangingRunner,
        on_run_done=scheduler.on_run_done,
    )

    now = datetime.now(timezone.utc)
    sub = Submission(
        origin="internal",
        submission_name="stale-submission",
        state="accepted",
        raw_body={},
        accepted_at=now,
        expire_at=now + timedelta(hours=1),
    )
    item = SubmissionItem(
        submission=sub,
        case_id="case-stale",
        case_name="case-stale",
        platform="android",
        run_content="do it",
        state="running",
        run_id="stale-run",
        device_serial="S1",
        started_at=now,
    )
    run = Run(
        id="stale-run",
        device_serial="S1",
        agent_id="agent-gone",
        goal="do it",
        status="running",
        execution_mode="server_brain",
        dispatch_source="scheduler",
    )
    session.add_all([sub, item, run])
    await session.commit()

    recovered = await service.recover_stale_runs(reason="server_restarted")
    assert recovered == 1

    async with db_module.get_session_factory()() as s:
        got_run = await s.get(Run, "stale-run")
        got_item = await s.get(SubmissionItem, item.id)
        assert got_run is not None
        assert got_run.status == "failed"
        assert got_run.reason == "server_restarted"
        assert got_item is not None
        assert got_item.state == "failed"
        assert got_item.status_reason == "executor_error"

    terminal_events = [e for e in publisher.events if e.get("event") == "submission.item.terminal"]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_scheduler_on_run_done_treats_cache_replay_pass_as_success(app, session):
    """缓存通道（trajectory cache replay）断言 PASS 时 emitter 上报
    ``result="pass"``，scheduler.on_run_done 必须把它视作成功（state=success
    + status_reason=completed），否则 SubmissionItem 会被错误归类成
    executor_error，UI 显示矛盾："轨迹缓存断言 PASS" 但 Run 失败。
    """
    hub = Hub()
    lock_store = DeviceLockStore()
    publisher = MemoryPublisher()
    scheduler = SubmissionScheduler(
        hub=hub,
        lock_store=lock_store,
        session_factory=db_module.get_session_factory(),
        publisher=publisher,
    )

    now = datetime.now(timezone.utc)
    sub = Submission(
        origin="internal",
        submission_name="cache-pass-submission",
        state="accepted",
        raw_body={},
        accepted_at=now,
        expire_at=now + timedelta(hours=1),
    )
    item = SubmissionItem(
        submission=sub,
        case_id="case-cache-pass",
        case_name="case-cache-pass",
        platform="android",
        run_content="do it",
        state="running",
        run_id="cache-pass-run",
        device_serial="S1",
        started_at=now,
    )
    run = Run(
        id="cache-pass-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="do it",
        status="success",
        reason="trajectory_cache_pass: 最终页面满足用户目标",
        execution_mode="server_brain",
        dispatch_source="scheduler",
    )
    session.add_all([sub, item, run])
    await session.commit()
    item_id = item.id

    await scheduler.on_run_done(
        "cache-pass-run",
        {
            "result": "pass",
            "message": "trajectory_cache_pass: 最终页面满足用户目标",
            "steps": 2,
            "elapsed_ms": 27000,
            "token_stats": {"total_tokens": 0},
        },
    )

    async with db_module.get_session_factory()() as s:
        got_item = await s.get(SubmissionItem, item_id)
        assert got_item is not None
        assert got_item.state == "success"
        assert got_item.status_reason == "completed"


@pytest.mark.asyncio
async def test_malformed_driver_result_marks_command_failed(app, session):
    hub = Hub()
    waiter = DriverRpcWaiter()
    fake_ws = MalformedScreenshotWS(waiter)
    await hub.register_agent("agent-malformed", "agent", "test", fake_ws)
    service = ServerRunnerService(
        hub=hub,
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
        waiter=waiter,
        runner_factory=ScreenshotRunner,
    )
    session.add(
        Run(
            id="malformed-run",
            device_serial="S1",
            agent_id="agent-malformed",
            goal="g",
            status="pending",
        )
    )
    await session.commit()

    assert await service.start_run(
        run_id="malformed-run",
        serial="S1",
        agent_id="agent-malformed",
        goal="g",
        dispatch_source="api",
    )

    deadline = time.time() + 2.0
    got = None
    while time.time() < deadline:
        await asyncio.sleep(0.02)
        async with db_module.get_session_factory()() as s:
            got = await s.get(Run, "malformed-run")
            if got is not None and got.status == "failed":
                break

    assert got is not None
    assert got.status == "failed"

    from sqlalchemy import select

    async with db_module.get_session_factory()() as s:
        rows = (
            await s.execute(
                select(RunCommand).where(RunCommand.run_id == "malformed-run")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].ok is False
        assert rows[0].error_class == "MalformedResult"
        assert rows[0].error_category == "network"
