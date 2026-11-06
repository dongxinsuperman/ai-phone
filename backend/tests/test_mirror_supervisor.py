from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ai_phone.agent import main as agent


@pytest.fixture
def mirrors(monkeypatch):
    sessions = []
    entered = Event()
    release = Event()

    class Session:
        _stopped = False

        def __init__(self, serial, ws, loop, *, orientation_generation=None):
            self.serial = serial
            self.generation = orientation_generation
            self.replays = 0
            sessions.append(self)

        def start(self):
            if self.generation == "turn":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test never released the startup barrier")

        def replay_init(self):
            self.replays += 1

        def stop(self):
            self._stopped = True

    supervisor = agent._MirrorSupervisor(object())
    supervisor._loop = object()
    monkeypatch.setattr(agent, "_MirrorSession", Session)
    return supervisor, sessions, entered, release


def test_rehello_during_orientation_does_not_leave_a_second_live_mirror(mirrors):
    supervisor, sessions, entered, release = mirrors
    serial = "emulator-5554"
    supervisor.start(serial)
    submitted = Event()

    def rehello():
        submitted.set()
        supervisor.start(serial)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rotation = pool.submit(supervisor.restart, serial, orientation_generation="turn")
        try:
            assert entered.wait(2)
            replay = pool.submit(rehello)
            assert submitted.wait(2)
            with pytest.raises(TimeoutError):
                replay.result(timeout=0.1)
        finally:
            release.set()
        assert rotation.result(timeout=2)
        replay.result(timeout=2)

    assert len(sessions) == 2
    assert sessions[0]._stopped
    assert supervisor.get_session(serial) is sessions[1]
    assert sessions[1].replays == 1
    supervisor.stop(serial)
    assert supervisor.get_session(serial) is None
    assert all(session._stopped for session in sessions)


def test_stop_waits_for_in_flight_start_without_blocking_other_devices(mirrors):
    supervisor, sessions, entered, release = mirrors
    serial = "emulator-5554"
    submitted = Event()

    def stop():
        submitted.set()
        supervisor.stop(serial)

    with ThreadPoolExecutor(max_workers=3) as pool:
        start = pool.submit(supervisor.start, serial, orientation_generation="turn")
        try:
            assert entered.wait(2)
            stopped = pool.submit(stop)
            assert submitted.wait(2)
            with pytest.raises(TimeoutError):
                stopped.result(timeout=0.1)
            pool.submit(supervisor.start, "emulator-5556").result(timeout=2)
        finally:
            release.set()
        start.result(timeout=2)
        stopped.result(timeout=2)

    assert supervisor.get_session(serial) is None
    assert sessions[0]._stopped
    assert supervisor.get_session("emulator-5556") is sessions[1]
    supervisor.stop_all()
    assert all(session._stopped for session in sessions)


async def test_retired_android_session_drops_queued_payload_and_new_session_keeps_identity():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    ws = SimpleNamespace(agent_id="agent-1", send=AsyncMock())
    loop = asyncio.get_running_loop()
    old = agent._MirrorSession("emulator-5554", ws, loop)
    current = agent._MirrorSession("emulator-5554", ws, loop, orientation_generation="turn")

    old._dispatch({"type": "video_init", "data": "old"})
    old.stop()
    current._dispatch({"type": "video_init", "data": "init"})
    current._dispatch({"type": "video_segment", "data": "segment"})
    # run_coroutine_threadsafe first schedules task creation on the loop.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    messages = [call.args[0] for call in ws.send.await_args_list]
    assert [message["data"] for message in messages] == ["init", "segment"]
    assert all(message["mirror_agent_id"] == "agent-1" for message in messages)
    assert messages[0]["mirror_stream_seq"] == messages[1]["mirror_stream_seq"]
    assert messages[0]["mirror_stream_seq"] > old._stream_seq
