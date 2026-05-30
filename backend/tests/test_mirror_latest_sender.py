import asyncio
import threading
import time

from ai_phone.agent import main as agent_main


def _start_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop_loop(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


def test_latest_mirror_sender_keeps_in_flight_and_only_sends_latest_pending():
    loop, thread = _start_loop()
    first_started = threading.Event()
    release_first = threading.Event()

    class FakeWs:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload["seq"])
            if payload["seq"] == 1:
                first_started.set()
                while not release_first.is_set():
                    await asyncio.sleep(0.01)
            return True

    ws = FakeWs()
    sender = agent_main._LatestMirrorPayloadSender("S1", ws, loop, "test")

    try:
        sender.send_latest({"seq": 1})
        assert first_started.wait(timeout=2)

        sender.send_latest({"seq": 2})
        sender.send_latest({"seq": 3})
        sender.send_latest({"seq": 4})
        release_first.set()

        deadline = time.time() + 2
        while time.time() < deadline and ws.sent != [1, 4]:
            time.sleep(0.01)
    finally:
        sender.close()
        _stop_loop(loop, thread)

    assert ws.sent == [1, 4]
