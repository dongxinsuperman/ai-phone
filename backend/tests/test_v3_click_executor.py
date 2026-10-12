import asyncio
import threading
from types import SimpleNamespace

import pytest

from ai_phone.agent.trajectory_cache.v3_replay import V3ReplayRunner
from ai_phone.agent.trajectory_cache.jev_click.adapters.android import AndroidAdapter


class Dispatcher:
    def __init__(self):
        self.actions = []
        self.last_screenshot_error = "previous screenshot failed"

    async def execute(self, action):
        self.actions.append(action)


def runner(click):
    dispatch = Dispatcher()
    return V3ReplayRunner(
        driver=object(), trajectory={"actions": []}, dispatcher=dispatch, click_executor=click
    ), dispatch


def action(kind="click"):
    return {"type": kind, "point": {"x": 100, "y": 200}}


@pytest.mark.asyncio
async def test_cached_click_uses_same_coordinates_and_does_not_touch_original_driver():
    calls = []
    r, d = runner(lambda x, y: calls.append((x, y)))
    await r._execute_recorded_action(action(), index=1, source="cache")
    assert calls == [(100, 200)] and d.actions == []
    assert d.last_screenshot_error is None
    assert r.execution_history[0]["runtime_status"] == "completed_without_exception"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,kind,origin",
    [
        ("rescue_repair", "click", None),
        ("ephemeral_gate_repair", "click", None),
        ("cache", "double_tap", None),
        ("cache", "scroll", None),
        ("cache", "click", "type"),
    ],
)
async def test_other_actions_and_rescue_do_not_use_fast_click(source, kind, origin):
    def forbidden(*args):
        raise AssertionError("Not an ordinary cached click")

    r, d = runner(forbidden)
    a = action(kind)
    if origin:
        a["locator_source_type"] = origin
    await r._execute_recorded_action(a, index=1, source=source)
    assert d.actions == [a]


@pytest.mark.asyncio
async def test_failed_fast_click_never_silently_replays_over_adb():
    def broken(*args):
        raise RuntimeError("RPC failed")

    r, d = runner(broken)
    with pytest.raises(RuntimeError, match="RPC failed"):
        await r._execute_recorded_action(action(), index=1, source="cache")
    assert d.actions == [] and r.execution_history[0]["runtime_status"] == "execution_error"


@pytest.mark.asyncio
async def test_completion_is_not_recorded_before_device_reply():
    started, release = threading.Event(), threading.Event()

    def slow(*args):
        started.set()
        release.wait(2)

    r, _ = runner(slow)
    task = asyncio.create_task(r._execute_recorded_action(action(), index=1, source="cache"))
    try:
        await asyncio.to_thread(started.wait, 1)
        assert not task.done() and r.execution_history[0]["runtime_status"] == "pending"
    finally:
        release.set()
        await task
    assert r.execution_history[0]["runtime_status"] == "completed_without_exception"


def test_android_adapter_reuses_existing_client_and_rejects_false_reply():
    calls = []
    a = AndroidAdapter(object())
    a.device = SimpleNamespace(click=lambda x, y: calls.append((x, y)))
    a.click(100, 200)
    assert calls == [(100, 200)]
    a.device = SimpleNamespace(click=lambda *args: False)
    with pytest.raises(RuntimeError, match="rejected"):
        a.click(100, 200)


@pytest.mark.asyncio
async def test_cancellation_waits_for_outstanding_device_click():
    started, release = threading.Event(), threading.Event()

    def pending(*args):
        started.set()
        release.wait(2)

    r, _ = runner(pending)
    task = asyncio.create_task(r._execute_recorded_action(action(), index=1, source="cache"))
    try:
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert r.execution_history[0]["runtime_status"] == "interrupted"
