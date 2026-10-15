"""Install/uninstall ownership follows native work, not its asyncio waiter."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from ai_phone.agent.app_install import handler as install_handler
from ai_phone.agent.app_uninstall import handler as uninstall_handler
from ai_phone.agent.device_activity import DeviceActivity
from ai_phone.agent import native_operation


async def _wait_released(activity, serial):
    async def wait():
        while not activity.acquire(serial, maintenance=True):
            await asyncio.sleep(0.001)
        activity.release(serial, maintenance=True)

    await asyncio.wait_for(wait(), 1)


def _package_setup(monkeypatch, operation, native, *, timeout=None, platform="android"):
    module = install_handler if operation == "install" else uninstall_handler
    activity = DeviceActivity()
    sent, downloaded = [], []
    tasks = set()
    monkeypatch.setattr(module, "device_activity", activity)
    monkeypatch.setattr(module, "_TASKS", tasks)

    class Client:
        async def send(self, payload):
            sent.append(payload)
            return True

    if timeout is not None:
        async def wait_for(awaitable, *, timeout):
            return await asyncio.wait_for(awaitable, timeout=0.05)

        monkeypatch.setattr(module, "asyncio", SimpleNamespace(
            create_task=asyncio.create_task, wait_for=wait_for, TimeoutError=asyncio.TimeoutError,
        ))

    if operation == "install":
        async def download(_client, _url, path):
            path.write_bytes(b"isolated package fixture")
            downloaded.append(path)

        monkeypatch.setattr(module, "_download_package", download)
        installer_name = {
            "android": "install_apk", "harmony": "install_hap",
            "ios": "install_ipa", "ios_sim": "install_sim_app",
        }[platform]
        monkeypatch.setattr(module, installer_name, native)
        handler = module.handle_app_install_start
    else:
        monkeypatch.setattr(module, "uninstall_by_platform", native)
        handler = module.handle_app_uninstall_start
    message = {
        "serial": "package-device", "platform": platform, "request_id": "request-1",
        "task_id": "task-1", "item_id": "item-1", "package_name": "example.personal.fixture",
        "filename": "fixture.package", "timeout_sec": 10,
    }
    return SimpleNamespace(
        handler=handler, client=Client(), message=message, activity=activity,
        sent=sent, downloaded=downloaded, tasks=tasks,
    )


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_timeout_keeps_native_lease_and_temp_package_until_actual_exit(monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def native(*args):
        calls.append(args)
        entered.set()
        assert release.wait(2)
        if operation == "install":
            assert args[1].read_bytes() == b"isolated package fixture"
        return True, "", "late success"

    f = _package_setup(monkeypatch, operation, native, timeout=0.05)
    pending = []
    try:
        await f.handler(f.client, f.message)
        pending = list(f.tasks)
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.gather(*pending)
        assert len(f.sent) == 1 and f.sent[0]["reason"] == "timeout"
        assert not f.activity.acquire("package-device", maintenance=True)
        assert f.activity.acquire("other-device", maintenance=True)
        f.activity.release("other-device", maintenance=True)
        if f.downloaded:
            assert f.downloaded[0].is_file()
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await _wait_released(f.activity, "package-device")
    assert len(calls) == 1
    assert len(f.sent) == 1 and f.sent[0]["success"] is False
    assert all(not path.exists() for path in f.downloaded)


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_cancelled_waiter_does_not_release_running_native_work(monkeypatch, operation):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def native(*args):
        calls.append(args)
        entered.set()
        assert release.wait(2)
        if operation == "install":
            assert args[1].is_file()
        return True, "", "late success"

    f = _package_setup(monkeypatch, operation, native)
    pending = []
    try:
        await f.handler(f.client, f.message)
        pending = list(f.tasks)
        assert await asyncio.to_thread(entered.wait, 1)
        pending[0].cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        assert not f.activity.acquire("package-device", maintenance=True)
        assert all(path.is_file() for path in f.downloaded)
        assert f.sent == []
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await _wait_released(f.activity, "package-device")
    assert len(calls) == 1 and f.sent == []
    assert all(not path.exists() for path in f.downloaded)


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_cancelled_queued_worker_releases_lease_and_never_starts_later(monkeypatch, operation):
    executor = ThreadPoolExecutor(max_workers=1)
    occupied, release = threading.Event(), threading.Event()
    queued = asyncio.Event()
    calls = []

    def occupy_executor():
        occupied.set()
        assert release.wait(2)

    blocker = executor.submit(occupy_executor)
    assert await asyncio.to_thread(occupied.wait, 1)

    async def to_thread(call):
        future = asyncio.get_running_loop().run_in_executor(executor, call)
        queued.set()
        return await future

    monkeypatch.setattr(native_operation, "asyncio", SimpleNamespace(to_thread=to_thread))
    f = _package_setup(monkeypatch, operation, lambda *args: calls.append(args) or (True, "", "ok"))
    pending = []
    try:
        await f.handler(f.client, f.message)
        pending = list(f.tasks)
        await queued.wait()
        assert not f.activity.acquire("package-device", maintenance=True)
        pending[0].cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await _wait_released(f.activity, "package-device")
        assert calls == []
        assert all(not path.exists() for path in f.downloaded)
    finally:
        release.set()
        await asyncio.to_thread(blocker.result, 1)
        executor.shutdown(wait=True)
        await asyncio.gather(*pending, return_exceptions=True)
    assert calls == [] and f.sent == []


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_cancel_between_thread_pickup_and_native_entry_has_no_late_side_effect(monkeypatch, operation):
    executor = ThreadPoolExecutor(max_workers=1)
    picked_up, release = threading.Event(), threading.Event()
    futures, calls = [], []

    async def to_thread(call):
        def before_native_entry():
            picked_up.set()
            assert release.wait(2)
            return call()

        future = executor.submit(before_native_entry)
        futures.append(future)
        return await asyncio.wrap_future(future)

    monkeypatch.setattr(native_operation, "asyncio", SimpleNamespace(to_thread=to_thread))
    f = _package_setup(monkeypatch, operation, lambda *args: calls.append(args) or (True, "", "ok"))
    pending = []
    try:
        await f.handler(f.client, f.message)
        pending = list(f.tasks)
        assert await asyncio.to_thread(picked_up.wait, 1)
        pending[0].cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await _wait_released(f.activity, "package-device")
        assert all(not path.exists() for path in f.downloaded)
    finally:
        release.set()
        for future in futures:
            await asyncio.to_thread(future.result, 1)
        executor.shutdown(wait=True)
        await asyncio.gather(*pending, return_exceptions=True)
    assert calls == [] and f.sent == []


@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_timeout_before_native_coroutine_starts_does_not_leak_ownership(monkeypatch, operation):
    calls = []
    f = _package_setup(monkeypatch, operation, lambda *args: calls.append(args) or (True, "", "ok"))
    module = install_handler if operation == "install" else uninstall_handler

    async def never_start(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, wait_for=never_start, TimeoutError=asyncio.TimeoutError,
    ))
    await f.handler(f.client, f.message)
    await asyncio.gather(*list(f.tasks))
    await _wait_released(f.activity, "package-device")
    assert calls == []
    assert len(f.sent) == 1 and f.sent[0]["reason"] == "timeout"
    assert all(not path.exists() for path in f.downloaded)


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("failed", [False, True])
async def test_native_completion_or_exception_releases_once_and_reports_once(monkeypatch, operation, failed):
    calls = []

    def native(*args):
        calls.append(args)
        if failed:
            raise OSError("fixture native failure")
        return True, "", "fixture success"

    f = _package_setup(monkeypatch, operation, native)
    await f.handler(f.client, f.message)
    await asyncio.gather(*list(f.tasks))
    await _wait_released(f.activity, "package-device")
    assert len(calls) == 1 and len(f.sent) == 1
    assert f.sent[0]["success"] is (not failed)
    assert all(not path.exists() for path in f.downloaded)


@pytest.mark.parametrize("platform", ["android", "harmony", "ios", "ios_sim"])
async def test_install_preserves_each_platform_native_entry(monkeypatch, platform):
    calls = []
    f = _package_setup(
        monkeypatch, "install", lambda *args: calls.append(args) or (True, "", "ok"), platform=platform,
    )
    await f.handler(f.client, f.message)
    await asyncio.gather(*list(f.tasks))
    await _wait_released(f.activity, "package-device")
    assert len(calls) == 1
    # Installation's existing minimum timeout is 30 seconds; keep it unchanged.
    assert calls[0][0] == "package-device" and calls[0][2] == 30
    assert f.sent[0]["success"] is True


pytestmark = pytest.mark.asyncio
