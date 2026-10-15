"""Native install work must retain maintenance exclusion after async timeout.

Every native operation is a local Event-controlled stub. No package, device,
network, subprocess, or real platform driver is invoked.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from ai_phone.agent.device_activity import DeviceActivity
from ai_phone.agent.app_install import handler as install_handler
from ai_phone.agent.app_uninstall import handler as uninstall_handler


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.asyncio
async def test_native_package_work_keeps_device_occupied_after_async_timeout(monkeypatch, operation):
    module = install_handler if operation == "install" else uninstall_handler
    activity = DeviceActivity()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    sent = []
    tasks = set()

    class Client:
        async def send(self, payload):
            sent.append(payload)
            return True

    def native_work(*_args):
        entered.set()
        try:
            assert release.wait(2)
            return True, "", "stub native work completed"
        finally:
            finished.set()

    async def short_wait_for(awaitable, *, timeout):
        # Fault injection of the existing outer async timeout, not a change to
        # native behavior: the native stub deliberately remains in-flight.
        return await asyncio.wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(module, "device_activity", activity)
    monkeypatch.setattr(module, "_TASKS", tasks)
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, to_thread=asyncio.to_thread,
        wait_for=short_wait_for, TimeoutError=asyncio.TimeoutError,
    ))
    message = {
        "serial": "isolated-physical-or-vm", "platform": "android",
        "request_id": "fixture-request", "task_id": "fixture-task", "item_id": "fixture-item",
        "package_name": "example.personal.fixture", "filename": "fixture.apk", "timeout_sec": 10,
    }
    if operation == "install":
        async def download(_client, _url, local_path):
            local_path.write_bytes(b"fixture bytes; never passed to a native installer")

        monkeypatch.setattr(module, "_download_package", download)
        monkeypatch.setattr(module, "install_apk", native_work)
        start = module.handle_app_install_start
    else:
        monkeypatch.setattr(module, "uninstall_by_platform", native_work)
        start = module.handle_app_uninstall_start

    pending = []
    try:
        await start(Client(), message)
        pending = list(tasks)
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.gather(*pending)
        await asyncio.sleep(0)
        assert sent and sent[-1]["reason"] == "timeout"
        assert not finished.is_set(), "the test requires native work to still be in-flight"
        acquired = activity.acquire(message["serial"], maintenance=True)
        if acquired:
            activity.release(message["serial"], maintenance=True)
        assert not acquired, "native install/uninstall is still running but VM stop/delete can now acquire maintenance"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.gather(*pending, return_exceptions=True)
