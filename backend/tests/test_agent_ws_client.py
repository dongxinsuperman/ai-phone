import pytest
import json
import asyncio
import threading
from types import SimpleNamespace

from ai_phone.agent import ws_client as ws_client_module
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.shared import protocol as P


def make_client(**kwargs):
    client = AgentWSClient(
        ws_url="ws://unused/ws/agent", token="test", agent_id="test-agent",
        agent_name="test", **kwargs,
    )
    # Tests exercising an already-open session opt into its hello-complete
    # state. run_forever always resets this on each actual connection.
    client._hello_sent = True
    return client


class MessageWs:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return json.dumps(next(self.messages))
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, message):
        self.sent.append(json.loads(message))


@pytest.mark.asyncio
async def test_rescan_loop_sends_periodic_device_snapshot(monkeypatch):
    class StopLoop(Exception):
        pass

    sleeps = []
    sent = []

    async def fake_sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) > 6:
            raise StopLoop

    async def fake_send(payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(ws_client_module.asyncio, "sleep", fake_sleep)

    client = AgentWSClient(
        ws_url="ws://server/ws/agent",
        token="t",
        agent_id="agent-1",
        agent_name="mac",
        rescan_interval=1.0,
        device_snapshot_refresh_sec=3.0,
        device_provider=lambda: [{"serial": "S1", "platform": "android"}],
    )
    client._ws = object()
    client._hello_sent = True
    client._last_serials = {"S1"}
    client.send = fake_send

    with pytest.raises(StopLoop):
        await client._rescan_loop()

    hello_payloads = [msg for msg in sent if msg.get("type") == P.MSG_HELLO]
    assert len(hello_payloads) == 2
    assert [msg["devices"][0]["serial"] for msg in hello_payloads] == ["S1", "S1"]
    assert all(P.CAP_V3_PLATFORM_CACHE in msg["capabilities"] for msg in hello_payloads)


@pytest.mark.asyncio
async def test_pre_hello_runs_before_first_hello(monkeypatch):
    order = []
    ready = {"value": False}
    sent = []

    class _FakeWs:
        async def send(self, raw):
            sent.append(json.loads(raw))

        async def close(self):
            return None

    class _FakeConnect:
        async def __aenter__(self):
            return _FakeWs()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def fake_ws_connect(*args, **kwargs):  # noqa: ANN001
        return _FakeConnect()

    monkeypatch.setattr(ws_client_module, "ws_connect", fake_ws_connect)

    def provider():
        order.append("provider")
        serial = "VM1" if ready["value"] else "MISS"
        return [{"serial": serial, "platform": "android"}]

    client = AgentWSClient(
        ws_url="ws://server/ws/agent",
        token="t",
        agent_id="agent-1",
        agent_name="mac",
        device_provider=provider,
    )

    async def pre_hello(_client):
        order.append("pre")
        ready["value"] = True

    async def on_connect(_client):
        order.append("connect")

    async def one_session():
        order.append("session")
        await client.stop()

    client.on_pre_hello(pre_hello)
    client.on_connect(on_connect)
    client._session_loop = one_session  # type: ignore[method-assign]

    await client.run_forever()

    assert order == ["pre", "provider", "connect", "session"]
    hello = [payload for payload in sent if payload["type"] == P.MSG_HELLO][0]
    assert hello["devices"][0]["serial"] == "VM1"
    assert P.CAP_V3_PLATFORM_CACHE in hello["capabilities"]


@pytest.mark.asyncio
async def test_initial_provider_does_not_block_event_loop():
    entered, release = threading.Event(), threading.Event()

    def provider():
        entered.set()
        assert release.wait(2)
        return [{"serial": "S1"}]

    client = make_client(device_provider=provider, host_id="host-test")
    client._ws = MessageWs([])
    task = asyncio.create_task(client._on_connected())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # This coroutine can run while discovery is still blocked.
        assert not task.done()
    finally:
        release.set()
        await task
    assert client._ws.sent[0]["host_id"] == "host-test"


@pytest.mark.asyncio
async def test_platform_reclaims_overlap_but_all_finish_before_first_scan(monkeypatch):
    client = make_client()
    both_entered = asyncio.Event()
    entered = []
    completed = []

    async def reclaim(_client):
        entered.append(True)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 1)
        completed.append(True)

    def provider():
        assert len(completed) == 2
        return []

    class Connect:
        async def __aenter__(self):
            return MessageWs([])

        async def __aexit__(self, *_):
            return None

    async def session():
        client._stop.set()

    monkeypatch.setattr(ws_client_module, "ws_connect", lambda *_, **__: Connect())
    client.device_provider = provider
    client.on_pre_hello(reclaim)
    client.on_pre_hello(reclaim)
    client._session_loop = session
    await client.run_forever()
    assert len(completed) == 2


@pytest.mark.asyncio
async def test_refreshes_coalesce_and_publish_in_scan_order():
    entered, release = threading.Event(), threading.Event()
    calls = 0
    active = 0

    def provider():
        nonlocal calls, active
        calls += 1
        active += 1
        assert active == 1
        if calls == 1:
            entered.set()
            assert release.wait(2)
            serial = "old"
        else:
            serial = "new"
        active -= 1
        return [{"serial": serial}]

    client = make_client(device_provider=provider)
    client._ws = MessageWs([])
    first = asyncio.create_task(client.refresh_devices())
    following = []
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        following = [asyncio.create_task(client.refresh_devices()) for _ in range(6)]
        await asyncio.sleep(0)
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(first, *following)
    assert calls == 2
    assert [m["devices"][0]["serial"] for m in client._ws.sent] == ["old", "new"]
    assert client._last_serials == {"new"}


@pytest.mark.asyncio
async def test_cancelled_refresh_does_not_abandon_running_provider():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider():
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return [{"serial": str(len(calls))}]

    client = make_client(device_provider=provider)
    client._ws = MessageWs([])
    first = asyncio.create_task(client.refresh_devices())
    next_refresh = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        next_refresh = asyncio.create_task(client.refresh_devices())
        await asyncio.sleep(0)
        assert len(calls) == 1
    finally:
        release.set()
        if next_refresh is not None:
            await next_refresh
    assert client._last_serials == {"2"}


@pytest.mark.asyncio
async def test_old_session_scan_is_not_published_on_reconnected_transport():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider():
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return [{"serial": "old"}]
        return [{"serial": "new"}]

    client = make_client(device_provider=provider)
    client._ws = MessageWs([])
    first = asyncio.create_task(client.refresh_devices())
    following = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        client._ws = MessageWs([])
        following = asyncio.create_task(client.refresh_devices())
        await asyncio.sleep(0)
    finally:
        release.set()
        await first
        if following is not None:
            await following
    assert [m["devices"][0]["serial"] for m in client._ws.sent] == ["new"]


@pytest.mark.asyncio
async def test_slow_vm_probe_does_not_block_run_cancel_or_pong():
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    client = make_client()

    async def probe(_client, _msg):
        entered.set()
        await release.wait()

    async def stop_run(_client, msg):
        cancelled.append(msg["run_id"])

    client.on(P.MSG_VM_CAPABILITY_PROBE, probe)
    client.on(P.MSG_STOP_RUN, stop_run)
    client._ws = MessageWs([
        {"type": P.MSG_VM_CAPABILITY_PROBE, "request_id": "probe"},
        {"type": P.MSG_STOP_RUN, "run_id": "run-1"},
        {"type": P.MSG_PING, "ts": 10},
    ])
    try:
        await client._recv_loop()
        await asyncio.wait_for(entered.wait(), 1)
        assert cancelled == ["run-1"]
        assert {"type": P.MSG_PONG, "ts": 10} in client._ws.sent
    finally:
        release.set()
        await asyncio.gather(*client._vm_tasks)


@pytest.mark.asyncio
async def test_slow_probes_do_not_take_lifecycle_execution_slots():
    release, stopped = asyncio.Event(), asyncio.Event()
    client = make_client(vm_command_concurrency=1)

    async def probe(_client, _msg):
        await release.wait()

    async def stop(_client, _msg):
        stopped.set()

    for kind in (P.MSG_VM_CAPABILITY_PROBE, P.MSG_HARMONY_VM_CAPABILITY_PROBE):
        client.on(kind, probe)
    client.on(P.MSG_VM_STOP, stop)
    client._ws = MessageWs([
        {"type": P.MSG_VM_CAPABILITY_PROBE},
        {"type": P.MSG_HARMONY_VM_CAPABILITY_PROBE},
        {"type": P.MSG_VM_STOP, "vm_id": "A"},
    ])
    try:
        await client._recv_loop()
        await asyncio.wait_for(stopped.wait(), 1)
    finally:
        release.set()
        await asyncio.gather(*client._vm_tasks)


@pytest.mark.asyncio
async def test_vm_commands_order_same_machine_and_allow_other_machine():
    started, release = asyncio.Event(), asyncio.Event()
    other_done = asyncio.Event()
    operations = []
    client = make_client(vm_command_concurrency=2)

    async def handler(_client, msg):
        operations.append((msg["vm_id"], msg["type"]))
        if msg["type"] == P.MSG_VM_START:
            started.set()
            await release.wait()
        elif msg["vm_id"] == "B":
            other_done.set()

    for kind in (P.MSG_VM_START, P.MSG_VM_STOP, P.MSG_VM_DELETE):
        client.on(kind, handler)
    client._ws = MessageWs([
        {"type": P.MSG_VM_START, "vm_id": "A"},
        {"type": P.MSG_VM_STOP, "vm_id": "A"},
        {"type": P.MSG_VM_DELETE, "vm_id": "B"},
    ])
    try:
        await client._recv_loop()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(other_done.wait(), 1)
        assert ("A", P.MSG_VM_STOP) not in operations
    finally:
        release.set()
        await asyncio.gather(*client._vm_tasks)
    assert operations[-1] == ("A", P.MSG_VM_STOP)


@pytest.mark.asyncio
async def test_vm_command_admission_rejects_without_claiming_cleanup():
    release = asyncio.Event()
    active = 0
    peak = 0
    client = make_client(vm_command_concurrency=1, vm_command_pending_limit=2)

    async def handler(_client, _msg):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1

    client.on(P.MSG_HARMONY_VM_STOP, handler)
    client._ws = MessageWs([
        {"type": P.MSG_HARMONY_VM_STOP, "vm_id": str(i), "request_id": str(i), "lease_token": "lease"}
        for i in range(3)
    ])
    try:
        await client._recv_loop()
        assert len(client._vm_tasks) == 2
        rejected = client._ws.sent[0]
        assert rejected["type"] == P.MSG_HARMONY_VM_STATUS
        assert rejected["request_id"] == "2"
        assert rejected["lease_token"] == "lease"
        assert rejected["ok"] is False
        assert rejected["details"]["cleanup_confirmed"] is False
    finally:
        release.set()
        await asyncio.gather(*client._vm_tasks)
    assert peak == 1


@pytest.mark.asyncio
async def test_vm_handler_exception_reports_failure_and_does_not_break_lane():
    client = make_client()
    operations = []

    async def handler(_client, msg):
        operations.append(msg["type"])
        if msg["type"] == P.MSG_VM_STOP:
            raise RuntimeError("native operation failed")

    client.on(P.MSG_VM_STOP, handler)
    client.on(P.MSG_VM_DELETE, handler)
    client._ws = MessageWs([
        {"type": P.MSG_VM_STOP, "vm_id": "A", "request_id": "stop"},
        {"type": P.MSG_VM_DELETE, "vm_id": "A", "request_id": "delete"},
    ])
    await client._recv_loop()
    await asyncio.gather(*client._vm_tasks)
    assert operations == [P.MSG_VM_STOP, P.MSG_VM_DELETE]
    assert client._ws.sent[0]["request_id"] == "stop"
    assert client._ws.sent[0]["reason"] == "agent_handler_failed"
    assert client._ws.sent[0]["ok"] is False


@pytest.mark.asyncio
async def test_send_failure_closes_transport_and_wakes_session():
    never = asyncio.Event()
    closed = []

    class FailedWs:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await never.wait()

        async def send(self, _raw):
            raise OSError("broken write")

        async def close(self):
            closed.append(True)
            raise OSError("close also failed")

    client = make_client(device_provider=lambda: [], ping_interval=999, rescan_interval=999)
    client._ws = FailedWs()
    session = asyncio.create_task(client._session_loop())
    await asyncio.sleep(0)
    assert await client.send({"type": P.MSG_VM_STATUS}) is False
    await asyncio.wait_for(session, 1)
    assert closed == [True]
    assert client._send_failed.is_set()


@pytest.mark.asyncio
async def test_failed_initial_send_reconnects_and_republishes_known_device(monkeypatch):
    connections = []
    closed = []

    class FailedWs(MessageWs):
        async def send(self, _raw):
            raise OSError("write failed")

        async def close(self):
            closed.append(True)

    class Connect:
        async def __aenter__(self):
            ws = FailedWs([]) if not connections else MessageWs([])
            connections.append(ws)
            return ws

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(ws_client_module, "ws_connect", lambda *_, **__: Connect())
    monkeypatch.setattr(ws_client_module, "_BACKOFF_STEPS", (0.0,))
    client = make_client(device_provider=lambda: [{"serial": "known-vm", "platform": "android"}])

    async def session():
        client._stop.set()

    client._session_loop = session
    await asyncio.wait_for(client.run_forever(), 1)
    assert len(connections) == 2
    assert closed == [True]
    assert connections[1].sent[0]["devices"][0]["serial"] == "known-vm"


@pytest.mark.asyncio
async def test_retained_operation_cannot_send_before_reconnect_hello(monkeypatch):
    ws = MessageWs([])
    gated_results = []
    recovered = {"value": False}

    class Connect:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(ws_client_module, "ws_connect", lambda *_, **__: Connect())
    client = make_client(device_provider=lambda: [{
        "serial": "restored" if recovered["value"] else "before-reclaim",
    }])

    async def old_operation():
        gated_results.append(await client.send({"type": P.MSG_VM_STATUS, "state": "stopped"}))
        gated_results.append(await client.refresh_devices())

    async def reclaim(_client):
        assert not client._connected.is_set()
        await asyncio.create_task(old_operation())
        assert ws.sent == []
        assert not client._send_failed.is_set()
        recovered["value"] = True

    async def report(_client):
        assert client._connected.is_set()
        await client.send({"type": P.MSG_VM_STATUS, "state": "running"})

    async def session():
        client._stop.set()

    client.on_pre_hello(reclaim)
    client.on_connect(report)
    client._session_loop = session
    await asyncio.wait_for(client.run_forever(), 1)
    assert gated_results == [False, False]
    assert [message["type"] for message in ws.sent] == [P.MSG_HELLO, P.MSG_VM_STATUS]
    assert ws.sent[0]["devices"][0]["serial"] == "restored"


@pytest.mark.asyncio
async def test_terminal_after_reclaim_snapshot_replays_immediately_after_hello(monkeypatch):
    snapshot_taken, terminal_buffered = asyncio.Event(), asyncio.Event()
    ws = MessageWs([])

    class Connect:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(ws_client_module, "ws_connect", lambda *_, **__: Connect())
    client = make_client(device_provider=lambda: [])

    async def old_start_finishes():
        await snapshot_taken.wait()
        assert await client.send({
            "type": P.MSG_VM_STATUS, "vm_id": "A", "request_id": "start-1",
            "state": "running", "ok": True,
        }) is False
        terminal_buffered.set()

    async def reclaim(_client):
        # The platform snapshot skipped an in-flight start and won't include
        # it in report_reclaimed(rescan=False). Replay must not depend on it.
        snapshot_taken.set()
        await terminal_buffered.wait()

    async def session():
        client._stop.set()

    client.on_pre_hello(reclaim)
    client._session_loop = session
    old = asyncio.create_task(old_start_finishes())
    await asyncio.wait_for(client.run_forever(), 1)
    await old
    assert [message["type"] for message in ws.sent] == [P.MSG_HELLO, P.MSG_VM_STATUS]
    assert ws.sent[1]["request_id"] == "start-1"
    assert ws.sent[1]["state"] == "running"
    assert not client._vm_status_outbox


@pytest.mark.asyncio
async def test_terminal_replaces_progress_and_late_progress_never_regresses_it():
    client = make_client()
    base = {"type": P.MSG_IOS_SIM_VM_STATUS, "vm_id": "A", "request_id": "start-1"}
    assert await client.send({**base, "state": "starting"}) is False
    terminal = {**base, "state": "running", "details": {"instance_created": True}}
    assert await client.send(terminal) is False
    terminal["details"]["instance_created"] = False
    assert await client.send({**base, "state": "starting"}) is False
    client._ws = MessageWs([])
    client._hello_sent = False
    await client._on_connected()
    assert [message.get("state") for message in client._ws.sent] == [None, "running"]
    assert client._ws.sent[1]["details"]["instance_created"] is True
    # Suppress the same stale progress even after its terminal result drained.
    assert await client.send({**base, "state": "starting"}) is False
    assert len(client._ws.sent) == 2


@pytest.mark.asyncio
async def test_failed_vm_status_is_retained_until_reconnected_hello():
    client = make_client()

    class FailedWs:
        async def send(self, _raw):
            raise OSError("failed status send")

        async def close(self):
            pass

    client._ws = FailedWs()
    status = {
        "type": P.MSG_HARMONY_VM_STATUS, "vm_id": "A", "request_id": "stop-1",
        "lease_token": "lease-A", "state": "stopped", "ok": True,
        "details": {"cleanup_confirmed": True},
    }
    assert await client.send(status) is False
    assert len(client._vm_status_outbox) == 1
    client._ws = MessageWs([])
    client._send_failed.clear()
    await client._on_connected()
    assert client._ws.sent[1] == status
    assert not client._vm_status_outbox


@pytest.mark.asyncio
async def test_vm_status_outbox_keeps_distinct_operations_and_has_a_hard_bound():
    client = make_client()
    for index in range(ws_client_module._VM_OUTBOX_LIMIT + 2):
        await client.send({
            "type": P.MSG_VM_STATUS, "vm_id": "A", "request_id": str(index),
            "state": "stopped", "ok": True,
        })
    assert len(client._vm_status_outbox) == ws_client_module._VM_OUTBOX_LIMIT
    assert len(client._vm_terminal_operations) == ws_client_module._VM_OUTBOX_LIMIT
    assert {key[2] for key in client._vm_status_outbox} == {
        str(index) for index in range(2, ws_client_module._VM_OUTBOX_LIMIT + 2)
    }


@pytest.mark.asyncio
async def test_old_drain_completion_does_not_remove_newer_queued_terminal():
    entered, release = asyncio.Event(), asyncio.Event()
    client = make_client()
    base = {"type": P.MSG_VM_STATUS, "vm_id": "A", "request_id": "start-1"}
    await client.send({**base, "state": "starting"})

    class SlowWs:
        async def send(self, _raw):
            entered.set()
            await release.wait()

    client._ws = SlowWs()
    flush = asyncio.create_task(client._flush_vm_statuses())
    await entered.wait()
    client._hello_sent = False  # Connection changed while the old send drained.
    assert await client.send({**base, "state": "running"}) is False
    release.set()
    assert await flush is False
    assert list(client._vm_status_outbox.values())[0]["state"] == "running"
    client._ws = MessageWs([])
    await client._on_connected()
    assert client._ws.sent[1]["state"] == "running"
    assert not client._vm_status_outbox


_LIFECYCLE_MESSAGES = [
    (command, response)
    for commands, response in (
        ((P.MSG_VM_START, P.MSG_VM_STOP, P.MSG_VM_DELETE), P.MSG_VM_STATUS),
        ((P.MSG_HARMONY_VM_START, P.MSG_HARMONY_VM_STOP, P.MSG_HARMONY_VM_DELETE), P.MSG_HARMONY_VM_STATUS),
        ((P.MSG_IOS_SIM_VM_START, P.MSG_IOS_SIM_VM_STOP, P.MSG_IOS_SIM_VM_DELETE), P.MSG_IOS_SIM_VM_STATUS),
    )
    for command in commands
]


@pytest.mark.parametrize("command,response", _LIFECYCLE_MESSAGES)
@pytest.mark.asyncio
async def test_expired_vm_lane_attempt_never_invokes_handler(monkeypatch, command, response):
    clock = [100.0]
    monkeypatch.setattr(ws_client_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []
    client = make_client()
    client._ws = MessageWs([])

    async def handler(_client, msg):
        executed.append(msg["request_id"])
        if msg["request_id"] == "first":
            entered.set()
            await release.wait()

    await client._dispatch_vm_handler(handler, {
        "type": command, "vm_id": "A", "request_id": "first",
    })
    await entered.wait()
    await client._dispatch_vm_handler(handler, {
        "type": command, "vm_id": "A", "request_id": "expired", "lease_token": "lease-A",
        "operation_timeout_sec": 5,
    })
    clock[0] = 106.0
    release.set()
    await asyncio.gather(*client._vm_tasks)
    assert executed == ["first"]
    assert client._ws.sent == [{
        "type": response, "agent_id": "test-agent", "vm_id": "A", "request_id": "expired",
        "lease_token": "lease-A", "ok": False, "state": "error", "reason": "operation_timeout",
        "error": "排队已超过操作时限，本次操作未执行",
        "details": {"operation_not_started": True, "cleanup_confirmed": False},
    }]


@pytest.mark.asyncio
async def test_expiry_while_waiting_for_host_capacity_never_invokes_handler(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ws_client_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []
    client = make_client(vm_command_concurrency=1)
    client._ws = MessageWs([])

    async def handler(_client, msg):
        executed.append(msg["vm_id"])
        if msg["vm_id"] == "A":
            entered.set()
            await release.wait()

    await client._dispatch_vm_handler(handler, {"type": P.MSG_VM_START, "vm_id": "A"})
    await entered.wait()
    await client._dispatch_vm_handler(handler, {
        "type": P.MSG_VM_START, "vm_id": "B", "request_id": "expired", "operation_timeout_sec": 5,
    })
    await asyncio.sleep(0)  # B has passed its lane and is waiting for A's host slot.
    clock[0] = 106.0
    release.set()
    await asyncio.gather(*client._vm_tasks)
    assert executed == ["A"]
    assert client._ws.sent[0]["reason"] == "operation_timeout"
    assert client._ws.sent[0]["details"] == {
        "operation_not_started": True, "cleanup_confirmed": False,
    }


@pytest.mark.asyncio
async def test_handler_receives_only_remaining_budget_and_original_message_is_unchanged(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ws_client_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    received = []
    client = make_client()

    async def handler(_client, msg):
        if msg["request_id"] == "first":
            entered.set()
            await release.wait()
        else:
            received.append(msg)

    await client._dispatch_vm_handler(handler, {
        "type": P.MSG_HARMONY_VM_START, "vm_id": "A", "request_id": "first",
    })
    await entered.wait()
    original = {
        "type": P.MSG_HARMONY_VM_STOP, "vm_id": "A", "request_id": "second",
        "lease_token": "lease-A", "operation_timeout_sec": 30.0,
    }
    await client._dispatch_vm_handler(handler, original)
    clock[0] = 107.5
    release.set()
    await asyncio.gather(*client._vm_tasks)
    assert original["operation_timeout_sec"] == 30.0
    assert received == [{**original, "operation_timeout_sec": 22.5}]
    assert received[0] is not original


@pytest.mark.asyncio
async def test_lifecycle_budget_does_not_change_capability_probe_contract():
    received = []
    client = make_client()

    async def handler(_client, msg):
        received.append(msg)

    message = {"type": P.MSG_VM_CAPABILITY_PROBE, "operation_timeout_sec": 0}
    await client._dispatch_vm_handler(handler, message)
    await asyncio.gather(*client._vm_tasks)
    assert received == [message]


@pytest.mark.parametrize("start,stop", [
    (P.MSG_VM_START, P.MSG_VM_STOP),
    (P.MSG_HARMONY_VM_START, P.MSG_HARMONY_VM_STOP),
    (P.MSG_IOS_SIM_VM_START, P.MSG_IOS_SIM_VM_STOP),
])
@pytest.mark.parametrize("action,flags,cleanup_confirmed", [
    ("start", {"rollback_new_instance": True, "require_existing_instance": False}, True),
    ("start", {"rollback_new_instance": False, "require_existing_instance": True}, False),
    ("stop", {"rollback_new_instance": True, "require_existing_instance": False}, False),
    ("start", {}, False),
    ("start", {"rollback_new_instance": True}, False),
])
@pytest.mark.parametrize("rejection", ["operation_timeout", "agent_busy"])
@pytest.mark.asyncio
async def test_only_certified_first_bind_preexecution_rejection_can_release_new_reservation(
    monkeypatch, start, stop, action, flags, cleanup_confirmed, rejection,
):
    clock = [100.0]
    monkeypatch.setattr(ws_client_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []
    client = make_client(vm_command_pending_limit=1 if rejection == "agent_busy" else 32)
    client._ws = MessageWs([])

    async def handler(_client, msg):
        executed.append(msg["request_id"])
        if msg["request_id"] == "first":
            entered.set()
            await release.wait()

    await client._dispatch_vm_handler(handler, {"type": start, "vm_id": "A", "request_id": "first"})
    await entered.wait()
    await client._dispatch_vm_handler(handler, {
        "type": start if action == "start" else stop,
        "vm_id": "A", "request_id": "expired", "operation_timeout_sec": 5,
        **flags,
    })
    clock[0] = 106.0
    release.set()
    await asyncio.gather(*client._vm_tasks)
    assert executed == ["first"]  # Neither recovery nor an automatic retry ran.
    assert client._ws.sent[0]["reason"] == rejection
    expected = {"operation_not_started": True, "cleanup_confirmed": cleanup_confirmed}
    if cleanup_confirmed:
        expected["instance_created"] = False
    assert client._ws.sent[0]["details"] == expected


@pytest.mark.parametrize("start", [P.MSG_VM_START, P.MSG_HARMONY_VM_START, P.MSG_IOS_SIM_VM_START])
@pytest.mark.asyncio
async def test_first_bind_handler_exception_cannot_claim_no_work_or_cleanup(start):
    executed = []
    client = make_client()
    client._ws = MessageWs([])

    async def handler(_client, msg):
        executed.append(msg["request_id"])
        raise OSError("handler already entered; cleanup is unknown")

    await client._dispatch_vm_handler(handler, {
        "type": start, "vm_id": "A", "request_id": "attempt-1",
        "rollback_new_instance": True, "require_existing_instance": False,
    })
    await asyncio.gather(*client._vm_tasks)
    assert executed == ["attempt-1"]
    assert client._ws.sent[0]["reason"] == "agent_handler_failed"
    assert client._ws.sent[0]["details"] == {"cleanup_confirmed": False}
