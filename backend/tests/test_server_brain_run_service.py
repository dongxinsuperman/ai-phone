from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from ai_phone.agent.runner.events import (
    EVT_LOG,
    EVT_RUN_FINISH,
    EVT_STEP_END,
    log_event,
    make_event,
)
from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Device, Run, RunCommand, RunStep, Submission, SubmissionItem
from ai_phone.server.runner.dispatch import RunDispatchService
from ai_phone.server.runner.emitter import ServerRunEmitter
from ai_phone.server.runner.rpc import DriverRpcWaiter
from ai_phone.server.runner.service import ServerRunnerService
from ai_phone.server.scheduler.service import SubmissionScheduler, parse_and_validate
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
        # 服务层把 ``emit`` 切到 ``emitter.aemit`` 之后，emit 返回的是
        # coroutine，必须 await——和真实 VLMRunner._emit_event 行为一致。
        await _emit_compat(self.emit, log_event(self.run_id, 1, "fake runner", "server brain ok"))
        await _emit_compat(
            self.emit,
            make_event(
                EVT_RUN_FINISH,
                self.run_id,
                ok=True,
                reason="finished: done",
                steps=1,
                elapsed_ms=123,
                token_stats={"total_tokens": 7},
            ),
        )


async def _emit_compat(emit, evt: Dict[str, Any]) -> None:
    """测试用 emit 兼容辅助：支持同步 ``emitter.emit`` 和异步
    ``emitter.aemit`` 两种 callback 形态，与生产代码
    ``VLMRunner._emit_event`` 的 ``_maybe_await`` 语义对齐。"""
    if emit is None:
        return
    result = emit(evt)
    if asyncio.iscoroutine(result):
        await result


class HangingRunner:
    def __init__(self, *, run_id, driver, goal, emit):  # noqa: ANN001
        self.run_id = run_id
        self.emit = emit
        self._event = asyncio.Event()

    async def run(self):
        await _emit_compat(self.emit, log_event(self.run_id, 1, "hanging runner", "started"))
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


def test_submission_parse_accepts_top_level_and_item_cache_mode():
    _name, _callback, drafts = parse_and_validate(
        {
            "submissionName": "cache-mode",
            "cacheMode": "v2",
            "items": [
                {
                    "caseId": "case-a",
                    "runContent": "点击教材同步",
                    "platforms": ["android"],
                },
                {
                    "caseId": "case-b",
                    "runContent": "点击开始挑战",
                    "platforms": ["ios"],
                    "cacheMode": "v3",
                },
                {
                    "caseId": "case-c",
                    "runContent": "普通执行",
                    "platforms": ["android"],
                    "cacheMode": "wrong",
                },
            ],
        }
    )

    assert [d.cache_mode for d in drafts] == ["v2", "v3", "off"]


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
async def test_server_emitter_step_uses_event_timestamp(app, session):
    """Step 卡片按事件真实时间排序，不受截图上传/入库延迟影响。"""
    run = Run(
        id="step-ts-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="step-ts-run",
        serial="S1",
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )
    event_ts_ms = 1_700_000_000_123
    evt = make_event(
        EVT_STEP_END,
        "step-ts-run",
        step=1,
        thought="done",
        action="click(point='<point>1 1</point>')",
        action_type="click",
        elapsed_ms=12,
    )
    evt["ts"] = event_ts_ms

    await emitter._forward_step_end(evt)  # noqa: SLF001

    async with db_module.get_session_factory()() as s:
        row = (
            await s.execute(select(RunStep).where(RunStep.run_id == "step-ts-run"))
        ).scalars().one()
        expected = datetime.fromtimestamp(
            event_ts_ms / 1000, tz=timezone.utc
        ).replace(tzinfo=None)
        assert row.created_at.replace(tzinfo=None) == expected


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


@pytest.mark.asyncio
async def test_emitter_aemit_serializes_log_writes_in_call_order(app, session):
    """``aemit`` 必须保证：调用方按顺序 ``await aemit(A)`` → ``await aemit(B)``
    时，DB / WS 写入顺序就是 A → B，哪怕 broadcast 自带 yield 让出 event loop。

    回归用：曾经 VLMRunner 走 ``emit=emitter.emit`` 时，三类顺序敏感事件
    （EVT_LOG / EVT_STEP_END / EVT_SCREENSHOT）都被 ``ensure_future`` 丢
    后台并发跑，导致「#1 第 1 步完成」会出现在「#2 ━━ 第 2 步 ━━」之后、
    时间戳还相同。修复用 ``_serial_lock`` 串行；本测试钉住这把锁的行为。
    """
    from ai_phone.server.models import RunLog

    run = Run(
        id="aemit-order-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="aemit-order-run",
        serial="S1",
        hub=SlowBroadcastHub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )

    # 并发触发：把 N 个 aemit 包成 task 一次性丢出去，最大化乱序压力。
    # 没有 _serial_lock 时，broadcast 自带 sleep 会让 task 调度乱序，
    # DB commit 顺序就跟 ``asyncio.create_task`` 调度顺序无关；
    # 有锁时即使 task 并发抢调度，DB 写入仍按 ``create_task`` 顺序串行。
    titles = [f"step-{i:02d}" for i in range(8)]
    tasks = [
        asyncio.create_task(
            emitter.aemit(log_event("aemit-order-run", 1, title, "x"))
        )
        for title in titles
    ]
    await asyncio.gather(*tasks)

    async with db_module.get_session_factory()() as s:
        rows = (
            await s.execute(
                select(RunLog)
                .where(RunLog.run_id == "aemit-order-run")
                .order_by(RunLog.id.asc())
            )
        ).scalars().all()
        assert [row.title for row in rows] == titles


@pytest.mark.asyncio
async def test_emitter_aemit_step_end_waits_for_prior_log(app, session):
    """``aemit`` 必须保证：``EVT_LOG`` → ``EVT_STEP_END`` 串行——即便
    broadcast 慢，``EVT_STEP_END`` 的 ``RunStep`` 入库时机也不会越过前一条
    ``EVT_LOG``。回归"#1 第 1 步完成"被甩到"#2 ━━ 第 2 步 ━━"之后那个 bug。
    """
    from ai_phone.server.models import RunLog

    run = Run(
        id="aemit-mixed-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="aemit-mixed-run",
        serial="S1",
        hub=SlowBroadcastHub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )

    # 并发触发：把三个 aemit 包成 task 一次性丢出去，最大化乱序压力。
    # 没有 _serial_lock 时这三个 task 的 broadcast/commit 顺序由 asyncio
    # 调度决定，不保证调用顺序；有锁时即使并发也按调用顺序串行。
    tasks = [
        asyncio.create_task(
            emitter.aemit(log_event("aemit-mixed-run", 1, "before-step-end", "a"))
        ),
        asyncio.create_task(
            emitter.aemit(
                make_event(
                    EVT_STEP_END,
                    "aemit-mixed-run",
                    step=1,
                    thought="t",
                    action="click(point='<point>1 1</point>')",
                    action_type="click",
                    elapsed_ms=1,
                )
            )
        ),
        asyncio.create_task(
            emitter.aemit(log_event("aemit-mixed-run", 1, "after-step-end", "b"))
        ),
    ]
    await asyncio.gather(*tasks)

    async with db_module.get_session_factory()() as s:
        logs = (
            await s.execute(
                select(RunLog)
                .where(RunLog.run_id == "aemit-mixed-run")
                .order_by(RunLog.id.asc())
            )
        ).scalars().all()
        steps = (
            await s.execute(
                select(RunStep)
                .where(RunStep.run_id == "aemit-mixed-run")
                .order_by(RunStep.id.asc())
            )
        ).scalars().all()
        # 同表 id 反映 commit 顺序：before-step-end 必须先入库，after-step-end
        # 必须后入库；中间夹一个 RunStep。如果 EVT_LOG 和 EVT_STEP_END 走的不是
        # 同一把锁，after-step-end 会先 commit（broadcast 比 step_end 短），顺序
        # 就乱了。这条断言钉住"同锁"语义。
        assert [r.title for r in logs] == ["before-step-end", "after-step-end"]
        assert len(steps) == 1


@pytest.mark.asyncio
async def test_emitter_emit_serial_returns_immediately_without_blocking_caller(
    app, session
):
    """``emit_serial`` 必须保证：调用方主流程瞬时返回（put_nowait），DB
    commit + WS 广播在后台 worker 异步完成。

    回归用：曾经 ``_log`` 直接 ``await emitter._forward_log``，每条日志
    同步阻塞主流程几十~几百毫秒；缓存回放打 7 拍×N 步累计每步多 5–9 秒
    （V2 缓存回放每步从 ~3s 拖成 ~12s 那个性能 bug）。修复后调用方调
    ``emit_serial`` 立刻返回，N 条日志的连续调用应在 ~ms 级完成。
    """
    from ai_phone.server.models import RunLog

    run = Run(
        id="emit-serial-fast-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="emit-serial-fast-run",
        serial="S1",
        hub=SlowBroadcastHub(),  # broadcast 自带 30ms 延迟
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )

    # 假设走 await aemit：8 条 × (DB commit + 30ms broadcast) > 240ms。
    # emit_serial 是同步入队，即便 broadcast 慢也不阻塞调用方，应该 < 100ms。
    titles = [f"step-{i:02d}" for i in range(8)]
    started = time.monotonic()
    for title in titles:
        emitter.emit_serial(log_event("emit-serial-fast-run", 1, title, "x"))
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 50, (
        f"emit_serial 调用方主流程必须零阻塞，实际 8 次连续调用耗时={elapsed_ms:.1f}ms"
    )

    # 后台 worker 处理完后日志全部落库（顺序由下一条测试钉死，这里只验完整性）。
    await emitter._drain_serial_queue()
    async with db_module.get_session_factory()() as s:
        rows = (
            await s.execute(
                select(RunLog)
                .where(RunLog.run_id == "emit-serial-fast-run")
                .order_by(RunLog.id.asc())
            )
        ).scalars().all()
        assert [r.title for r in rows] == titles


@pytest.mark.asyncio
async def test_emitter_emit_serial_processes_events_in_call_order(app, session):
    """``emit_serial`` 必须保证：调用顺序 = 后台 worker 处理顺序 = DB
    commit 顺序，混合 EVT_LOG / EVT_STEP_END 也成立。

    顺序保序是改造后唯一保留的硬约束："`#N 第 N 步完成 · click`" 不能被
    甩到 "`#N+1 缓存步骤`" 之后；首跑 / 缓存通道都依赖这条不变量。
    """
    from ai_phone.server.models import RunLog

    run = Run(
        id="emit-serial-order-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="emit-serial-order-run",
        serial="S1",
        hub=SlowBroadcastHub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )

    # 仿真"两步缓存回放"：日志 → STEP_END(1) → 下一步日志 → STEP_END(2)。
    # 没有保序时，broadcast 慢会让 STEP_END 的 commit 晚于 after-step 的日志。
    emitter.emit_serial(log_event("emit-serial-order-run", 1, "step-1-log-a", "a"))
    emitter.emit_serial(
        make_event(
            EVT_STEP_END,
            "emit-serial-order-run",
            step=1,
            thought="t1",
            action="click",
            action_type="click",
            elapsed_ms=1,
        )
    )
    emitter.emit_serial(log_event("emit-serial-order-run", 1, "step-2-log-b", "b"))
    emitter.emit_serial(
        make_event(
            EVT_STEP_END,
            "emit-serial-order-run",
            step=2,
            thought="t2",
            action="click",
            action_type="click",
            elapsed_ms=1,
        )
    )
    emitter.emit_serial(log_event("emit-serial-order-run", 1, "step-2-log-c", "c"))

    await emitter._drain_serial_queue()
    async with db_module.get_session_factory()() as s:
        logs = (
            await s.execute(
                select(RunLog)
                .where(RunLog.run_id == "emit-serial-order-run")
                .order_by(RunLog.id.asc())
            )
        ).scalars().all()
        steps = (
            await s.execute(
                select(RunStep)
                .where(RunStep.run_id == "emit-serial-order-run")
                .order_by(RunStep.id.asc())
            )
        ).scalars().all()
        # 保序硬约束：step-1-log-a / step-2-log-b / step-2-log-c 三条 RunLog
        # 的 id 必然递增（id 反映 commit 顺序）；STEP_END 入 RunStep 表，但
        # 时序上 step-1 的 STEP_END commit 必然在 step-2-log-b 之前。
        assert [r.title for r in logs] == [
            "step-1-log-a",
            "step-2-log-b",
            "step-2-log-c",
        ]
        assert [s_.step for s_ in steps] == [1, 2]


@pytest.mark.asyncio
async def test_emitter_finalize_run_drains_emit_serial_queue_before_run_done(
    app, session
):
    """``_finalize_run``（被 ``force_finish`` / ``_forward_run_finish`` 调用）
    必须先 ``_drain_serial_queue()`` 再写 Run 终态 + 广播 RUN_DONE。

    否则会出现："Run.status = success" 已写完但 RunLog/RunStep 还在
    后台 worker 队列里没 commit，前端报告查接口会看到「任务已结束但
    缺最后几条日志」的窗口。这是改造的核心承诺之一：不要求实时性，
    但 Run 结束时所有日志都要齐全。
    """
    from ai_phone.server.models import RunLog

    run = Run(
        id="emit-serial-drain-run",
        device_serial="S1",
        agent_id="agent-server-brain",
        goal="g",
        status="running",
        execution_mode="server_brain",
    )
    session.add(run)
    await session.commit()

    emitter = ServerRunEmitter(
        run_id="emit-serial-drain-run",
        serial="S1",
        hub=SlowBroadcastHub(),  # broadcast 慢，让队列里有"未处理"事件压力
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
    )

    titles = [f"final-step-{i:02d}" for i in range(5)]
    for title in titles:
        emitter.emit_serial(log_event("emit-serial-drain-run", 1, title, "x"))

    # force_finish → _log_token_summary（也走 emit_serial）→ _finalize_run
    # → _drain_serial_queue → 写 Run 终态。drain 必须排空到 Token 统计 那条。
    await emitter.force_finish(
        result="finished",
        message="ok",
        elapsed_ms=100,
        steps=5,
        token_stats={"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2, "call_count": 1},
    )

    async with db_module.get_session_factory()() as s:
        run_after = (
            await s.execute(select(Run).where(Run.id == "emit-serial-drain-run"))
        ).scalar_one()
        rows = (
            await s.execute(
                select(RunLog)
                .where(RunLog.run_id == "emit-serial-drain-run")
                .order_by(RunLog.id.asc())
            )
        ).scalars().all()
        # Run 已 finished 时，emit_serial 投入的 5 条 step 日志 + force_finish
        # 内部 _log_token_summary 投入的 1 条"Token 统计"必须全部在 DB 里。
        # 顺序：5 条 step 在前，Token 统计在 _finalize_run 收尾时入队所以在最后。
        assert run_after.status == "success"
        assert [r.title for r in rows] == titles + ["Token 统计"]
