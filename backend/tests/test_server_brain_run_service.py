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
from ai_phone.server.models import Device, Run, Submission, SubmissionItem
from ai_phone.server.runner.dispatch import RunDispatchService
from ai_phone.server.runner.emitter import ServerRunEmitter
from ai_phone.server.runner.rpc import DriverRpcWaiter
from ai_phone.server.runner.service import ServerRunnerService
from ai_phone.server.scheduler.service import SubmissionScheduler
from ai_phone.server.submissions import ResultPublisher


class FakeWS:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent.append(payload)


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
    assert logs.json()["items"][0]["title"] == "fake runner"


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
