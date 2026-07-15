from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from ai_phone.agent import main as agent_main
from ai_phone.agent.async_utils import run_blocking


@pytest.mark.asyncio
async def test_run_blocking_waits_for_inflight_device_call_before_cancel() -> None:
    started = threading.Event()
    release = threading.Event()

    def _device_call() -> None:
        started.set()
        release.wait(timeout=2.0)

    task = asyncio.create_task(run_blocking(_device_call))
    assert await asyncio.to_thread(started.wait, 1.0)

    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done(), "当前设备调用未退出前不能把 Run 当作已经停止"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_during_prepare_reports_cancelled_after_prepare_exits(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    class _Driver:
        platform = "android"

        def prepare_for_run(self) -> None:
            started.set()
            release.wait(timeout=2.0)

    class _Reporter:
        def __init__(self) -> None:
            self.messages: List[Dict[str, Any]] = []

        async def enqueue(self, message: Dict[str, Any]) -> None:
            self.messages.append(dict(message))

    class _Client:
        server_http_base = "http://test"

        async def send(self, _message: Dict[str, Any]) -> bool:
            return True

    settings = SimpleNamespace(
        android_wake_before_run=True,
        harmony_wake_before_run=False,
        ios_wake_before_run=False,
    )
    monkeypatch.setattr(agent_main, "has_runtime_override", lambda: True)
    monkeypatch.setattr(agent_main, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda _serial: _Driver())
    monkeypatch.setitem(agent_main._serial_platform, "S-CANCEL", "android")

    supervisor = agent_main._RunSupervisor()
    reporter = _Reporter()
    supervisor.reporter = reporter  # type: ignore[assignment]
    client = _Client()

    await agent_main._handle_start_run(
        client,  # type: ignore[arg-type]
        supervisor,
        {
            "type": "start_run",
            "run_id": "run-cancel-prepare",
            "device_serial": "S-CANCEL",
            "goal": "打开设置",
            "engine": "vlm",
        },
    )
    assert await asyncio.to_thread(started.wait, 1.0)
    entry = supervisor.get("run-cancel-prepare")
    assert entry is not None
    task = entry["task"]

    await agent_main._handle_stop_run(
        client,  # type: ignore[arg-type]
        supervisor,
        {"type": "stop_run", "run_id": "run-cancel-prepare"},
    )
    await asyncio.sleep(0.02)
    assert not task.done()
    assert not any(m.get("type") == "run_done" for m in reporter.messages)

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    done = [m for m in reporter.messages if m.get("type") == "run_done"]
    assert len(done) == 1
    assert done[0]["result"] == "cancelled"
    assert supervisor.get("run-cancel-prepare") is None
