"""Three-platform candidate API contracts; fake Agents and a private SQLite DB.

Run from an empty temporary working directory so no repository dotenv is read.
No Server lifespan, native device command, socket connection, or global waiter
is used by these tests.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_phone.config import Settings, get_settings
from ai_phone.server.android_vm import api as android_api, service as android_service
from ai_phone.server.api._deps import db_session
from ai_phone.server.db import Base
from ai_phone.server.harmony_vm import api as harmony_api, service as harmony_service
from ai_phone.server.harmony_vm.models import HarmonyVmInstance
from ai_phone.server.hub import Hub
from ai_phone.server.ios_sim import api as ios_api, service as ios_service
from ai_phone.server.ios_sim.models import IosSimVmInstance
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import AndroidVmInstance
from ai_phone.shared import protocol as P


AUTH = {"Authorization": "Bearer probe-test"}
AGENTS = [
    {"agent_id": "agent-b", "agent_name": "Bravo", "host_os": "Windows"},
    {"agent_id": "agent-c", "agent_name": "Zulu", "host_os": "Darwin"},
    {"agent_id": "agent-a", "agent_name": "Alpha", "host_os": "Linux"},
]


@pytest.fixture(autouse=True)
def isolated_probe_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("AI_PHONE_SUBMISSION_INTERNAL_TOKEN", "probe-test")
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(params=("android", "ios", "harmony"))
def probe_platform(request):
    return {
        "android": SimpleNamespace(
            name="android", api=android_api, model=AndroidVmInstance,
            waiter_type=android_service.VmCapabilityWaiter,
            probe_type=P.MSG_VM_CAPABILITY_PROBE,
            prefix="/api/internal/vm/instances",
        ),
        "ios": SimpleNamespace(
            name="ios", api=ios_api, model=IosSimVmInstance,
            waiter_type=ios_service.IosSimCapabilityWaiter,
            probe_type=P.MSG_IOS_SIM_VM_CAPABILITY_PROBE,
            prefix="/api/internal/ios-sim/instances",
        ),
        "harmony": SimpleNamespace(
            name="harmony", api=harmony_api, model=HarmonyVmInstance,
            waiter_type=harmony_service.HarmonyVmCapabilityWaiter,
            probe_type=P.MSG_HARMONY_VM_CAPABILITY_PROBE,
            prefix="/api/internal/harmony-vm/instances",
        ),
    }[request.param]


class ProbeHub(Hub):
    def __init__(self, waiter, agents=AGENTS):
        super().__init__()
        self.waiter = waiter
        self.agents = [dict(agent) for agent in agents]
        self.sent = []
        self.blocked_agents = set()
        self.release = asyncio.Event()
        self.sends_started = asyncio.Event()
        self.active_sends = set()
        self.cancelled_sends = set()
        self.observe_database = None
        self.database_at_send = []

    def snapshot(self):
        return {"agents": [dict(agent) for agent in self.agents]}

    def has_agent(self, agent_id):
        return any(agent["agent_id"] == agent_id for agent in self.agents)

    async def send_to_agent(self, agent_id, payload):
        if self.observe_database is not None:
            self.database_at_send.append(self.observe_database())
        self.sent.append((agent_id, dict(payload)))
        task = asyncio.current_task()
        self.active_sends.add(task)
        try:
            if agent_id in self.blocked_agents:
                self.sends_started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled_sends.add(agent_id)
                    raise
            self.waiter.resolve(
                agent_id,
                {"request_id": payload["request_id"], "ok": agent_id != "agent-b",
                 "reason": "available" if agent_id != "agent-b" else "missing runtime",
                 "warning": "test warning", "details": {"source": agent_id}},
            )
            return True
        finally:
            self.active_sends.discard(task)


@pytest_asyncio.fixture
async def probe_app(probe_platform, monkeypatch, tmp_path):
    """Route-only app with a local session dependency and fresh waiter."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(conn, tables=[probe_platform.model.__table__])
        )
    fields = {"name": "probe-vm", "alias": "probe-vm", "state": "draft"}
    if probe_platform.name == "android":
        fields.update(api_level=33, abi="arm64")
    async with factory() as session:
        vm = probe_platform.model(**fields)
        session.add(vm)
        await session.commit()
        vm_id = vm.id

    pool = {"checked_out": 0, "checkouts": 0, "checkins": 0}
    request_sessions = []

    def checkout(_connection, _record, _proxy):
        pool["checked_out"] += 1
        pool["checkouts"] += 1

    def checkin(_connection, _record):
        pool["checked_out"] -= 1
        pool["checkins"] += 1

    event.listen(engine.sync_engine, "checkout", checkout)
    event.listen(engine.sync_engine, "checkin", checkin)

    async def local_session():
        async with factory() as session:
            request_sessions.append(session)
            yield session

    waiter = probe_platform.waiter_type()
    hub = ProbeHub(waiter)
    hub.observe_database = lambda: {
        **pool, "transactions": [session.in_transaction() for session in request_sessions]
    }
    app = FastAPI()
    app.state.hub = hub
    app.state.lock_store = DeviceLockStore()
    app.dependency_overrides[db_session] = local_session
    monkeypatch.setattr(probe_platform.api, "get_capability_waiter", lambda: waiter)
    app.include_router(probe_platform.api.router)
    fixture = SimpleNamespace(
        app=app, hub=hub, waiter=waiter, vm_id=vm_id,
        path=f"{probe_platform.prefix}/{vm_id}/dispatch-candidates",
        pool=pool, request_sessions=request_sessions,
    )
    try:
        yield fixture
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


class InMemoryStream:
    """Drive the real ASGI route and its http.disconnect handling without a socket."""

    def __init__(self, app, path, *, block_snapshot_send=False):
        self.app = app
        self.path = path
        self.block_snapshot_send = block_snapshot_send
        self.disconnect = asyncio.Event()
        self.never_sent = asyncio.Event()
        self.request_received = False
        self.events = []
        self.available = asyncio.Queue()
        self.status = None
        self.headers = {}
        self.buffer = b""

    async def receive(self):
        if not self.request_received:
            self.request_received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {key.decode().lower(): value.decode() for key, value in message["headers"]}
            return
        if message["type"] != "http.response.body":
            return
        self.buffer += message.get("body", b"")
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line:
                continue
            event = json.loads(line)
            self.events.append(event)
            self.available.put_nowait(event)
            if self.block_snapshot_send and event["type"] == "snapshot":
                await self.never_sent.wait()

    async def run(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": self.path, "raw_path": self.path.encode(), "root_path": "",
            "query_string": b"stream=true", "headers": [(b"authorization", b"Bearer probe-test")],
            "server": ("asgi-test", 80), "client": ("asgi-test", 1234),
        }
        await self.app(scope, self.receive, self.send)


def assert_database_released_before_probe_send(probe_app):
    assert probe_app.hub.database_at_send, "应当真正执行探查发送"
    for observation in probe_app.hub.database_at_send:
        assert observation["checkouts"] >= 1, "必须覆盖真实配置查询，不能绕过 DB"
        assert observation["checkins"] == observation["checkouts"]
        assert observation["checked_out"] == 0, "等待 Agent 探查时不得占用数据库连接"
        assert observation["transactions"] and not any(observation["transactions"])


@pytest.mark.asyncio
async def test_default_json_keeps_legacy_fields_and_platform_sorting(probe_app, probe_platform):
    async with AsyncClient(transport=ASGITransport(app=probe_app.app), base_url="http://asgi-test") as client:
        response = await asyncio.wait_for(client.post(probe_app.path, headers=AUTH), timeout=2)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"request_id", "agents"}
    assert body["request_id"]
    expected_order = ["agent-b", "agent-c", "agent-a"] if probe_platform.name == "ios" else [
        "agent-a", "agent-c", "agent-b"
    ]
    assert [row["agent_id"] for row in body["agents"]] == expected_order
    for row in body["agents"]:
        assert set(row) == {"agent_id", "agent_name", "host_os", "ok", "reason", "warning", "details"}
        assert "probe_state" not in row
        assert row["ok"] is (row["agent_id"] != "agent-b")
        assert row["details"] == {"source": row["agent_id"]}
    assert len(probe_app.hub.sent) == 3
    assert {payload["type"] for _, payload in probe_app.hub.sent} == {probe_platform.probe_type}
    assert_database_released_before_probe_send(probe_app)
    assert not probe_app.waiter._pending


@pytest.mark.asyncio
async def test_stream_flushes_snapshot_and_fast_agent_before_slow_agents(probe_app):
    probe_app.hub.blocked_agents = {"agent-b", "agent-c"}
    stream = InMemoryStream(probe_app.app, probe_app.path)
    task = asyncio.create_task(stream.run())
    try:
        snapshot = await asyncio.wait_for(stream.available.get(), timeout=2)
        assert snapshot["type"] == "snapshot"
        assert snapshot["request_id"]
        assert {row["agent_id"] for row in snapshot["agents"]} == {row["agent_id"] for row in AGENTS}
        assert all(row["probe_state"] == "probing" for row in snapshot["agents"])
        assert probe_app.pool["checkouts"] >= 1
        assert probe_app.pool["checked_out"] == 0, "首批行发给客户端前就应结束只读 DB 会话"
        assert not any(session.in_transaction() for session in probe_app.request_sessions)
        fast = await asyncio.wait_for(stream.available.get(), timeout=2)
        assert fast["type"] == "agent" and fast["agent"]["agent_id"] == "agent-a"
        await asyncio.wait_for(probe_app.hub.sends_started.wait(), timeout=2)
        assert not task.done(), "快 Agent 的结果必须在其他探查尚未结束时发到客户端"
        assert probe_app.waiter._pending
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("application/x-ndjson")
        assert stream.headers["cache-control"] == "no-cache"
        assert stream.headers["x-accel-buffering"] == "no"
        probe_app.hub.release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stream.buffer == b"", "每条 NDJSON 事件都必须由换行结束"
    assert [event["type"] for event in stream.events] == ["snapshot", "agent", "agent", "agent", "done"]
    assert {event["request_id"] for event in stream.events} == {snapshot["request_id"]}
    terminals = [event["agent"] for event in stream.events if event["type"] == "agent"]
    assert {row["agent_id"] for row in terminals} == {row["agent_id"] for row in AGENTS}
    for row in terminals:
        assert row["probe_state"] and row["probe_state"] != "probing"
        assert row["ok"] is (row["agent_id"] != "agent-b")
        assert row["details"] == {"source": row["agent_id"]}
    assert not probe_app.waiter._pending
    assert not probe_app.hub.active_sends
    assert_database_released_before_probe_send(probe_app)


@pytest.mark.asyncio
async def test_empty_stream_emits_empty_snapshot_then_done(probe_app):
    probe_app.hub.agents = []
    async with AsyncClient(transport=ASGITransport(app=probe_app.app), base_url="http://asgi-test") as client:
        response = await asyncio.wait_for(client.post(f"{probe_app.path}?stream=true", headers=AUTH), timeout=2)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["snapshot", "done"]
    assert events[0]["agents"] == []
    assert events[0]["request_id"] == events[1]["request_id"]
    assert not probe_app.hub.sent and not probe_app.waiter._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_query", ["", "?stream=true"])
async def test_missing_vm_returns_json_404_without_probe(probe_app, probe_platform, stream_query):
    path = f"{probe_platform.prefix}/missing-vm/dispatch-candidates{stream_query}"
    async with AsyncClient(transport=ASGITransport(app=probe_app.app), base_url="http://asgi-test") as client:
        response = await client.post(path, headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()
    assert not probe_app.hub.sent and not probe_app.waiter._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_at", ["snapshot_send", "probe_send"])
async def test_http_disconnect_clears_pending_and_cancels_tasks(probe_app, disconnect_at):
    probe_app.hub.blocked_agents = {row["agent_id"] for row in AGENTS}
    baseline = set(asyncio.all_tasks())
    stream = InMemoryStream(
        probe_app.app, probe_app.path, block_snapshot_send=disconnect_at == "snapshot_send"
    )
    task = asyncio.create_task(stream.run())
    try:
        snapshot = await asyncio.wait_for(stream.available.get(), timeout=2)
        assert snapshot["type"] == "snapshot"
        if disconnect_at == "probe_send":
            await asyncio.wait_for(probe_app.hub.sends_started.wait(), timeout=2)
            assert probe_app.hub.active_sends
        assert probe_app.waiter._pending
        stream.disconnect.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not probe_app.waiter._pending
    assert not probe_app.hub.active_sends
    if disconnect_at == "probe_send":
        assert probe_app.hub.cancelled_sends
    assert not [current for current in asyncio.all_tasks() - baseline if not current.done()]
    assert not any(event["type"] == "done" for event in stream.events)
    assert not probe_app.waiter.resolve("agent-a", {"request_id": snapshot["request_id"], "ok": True})
