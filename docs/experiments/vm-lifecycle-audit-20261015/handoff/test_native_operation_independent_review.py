"""Independent package-occupancy checks: fake native calls only, exact release counts."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from ai_phone.agent.app_install import handler as install
from ai_phone.agent.app_uninstall import handler as uninstall
from ai_phone.agent.app_uninstall import platforms
from ai_phone.agent.device_activity import DeviceActivity
from ai_phone.agent.native_operation import NativeOperation


class CountingActivity(DeviceActivity):
    def __init__(self):
        super().__init__()
        self.count_lock = threading.Lock()
        self.acquired = 0
        self.released = 0

    def acquire(self, serial, *, maintenance=False):
        with self.count_lock:
            result = super().acquire(serial, maintenance=maintenance)
            if result and not maintenance:
                self.acquired += 1
            return result

    def release(self, serial, *, maintenance=False):
        with self.count_lock:
            if not maintenance:
                assert self._users.get(serial, 0) > 0, "a lease was released twice"
                self.released += 1
            super().release(serial, maintenance=maintenance)


def fixture(monkeypatch, operation, native, *, timeout=False, block_send=False, platform="android"):
    module = install if operation == "install" else uninstall
    activity = CountingActivity()
    tasks, sent, paths, cleaned = set(), [], [], []
    sending, permit_send = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(module, "device_activity", activity)
    monkeypatch.setattr(module, "_TASKS", tasks)

    class Client:
        async def send(self, payload):
            sent.append(payload)
            sending.set()
            if block_send:
                await permit_send.wait()
            return True

    if timeout:
        async def short_wait(awaitable, *, timeout):
            return await asyncio.wait_for(awaitable, timeout=0.02)

        monkeypatch.setattr(module, "asyncio", SimpleNamespace(
            create_task=asyncio.create_task, wait_for=short_wait, TimeoutError=asyncio.TimeoutError,
        ))
    if operation == "install":
        directory = module.tempfile.TemporaryDirectory

        class CountedDirectory:
            def __init__(self, *args, **kwargs):
                self.real = directory(*args, **kwargs)
                self.name = self.real.name

            def cleanup(self):
                cleaned.append(self.name)
                self.real.cleanup()

        async def download(_client, _url, path):
            path.write_bytes(b"independent-review-package")
            paths.append(path)

        monkeypatch.setattr(module, "tempfile", SimpleNamespace(TemporaryDirectory=CountedDirectory))
        monkeypatch.setattr(module, "_download_package", download)
        monkeypatch.setattr(module, "install_apk", native)
    else:
        monkeypatch.setattr(module, "uninstall_by_platform", native)
    message = {"serial": "review-device", "platform": platform, "request_id": "review-request",
               "task_id": "review-task", "item_id": "review-item", "package_name": "example.review",
               "filename": "review.package", "timeout_sec": 30}
    handler = module.handle_app_install_start if operation == "install" else module.handle_app_uninstall_start
    return SimpleNamespace(module=module, activity=activity, tasks=tasks, sent=sent, paths=paths,
                           cleaned=cleaned, sending=sending, permit_send=permit_send,
                           message=message, client=Client(), handler=handler)


async def released(activity):
    async def wait():
        while not activity.acquire("review-device", maintenance=True):
            await asyncio.sleep(0.001)
        activity.release("review-device", maintenance=True)

    await asyncio.wait_for(wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("failed", [False, True])
async def test_native_finishes_first_then_handler_releases_exactly_two_leases(monkeypatch, operation, failed):
    def native(*args):
        if operation == "install":
            assert args[1].read_bytes() == b"independent-review-package"
        if failed:
            raise OSError("independent native error")
        return True, "", "ok"

    f = fixture(monkeypatch, operation, native, block_send=True)
    await f.handler(f.client, f.message)
    pending = list(f.tasks)
    try:
        await asyncio.wait_for(f.sending.wait(), 2)
        assert f.activity.acquired == 2 and f.activity.released == 1
        assert not f.activity.acquire("review-device", maintenance=True)
        assert all(not path.exists() for path in f.paths)
        assert len(f.cleaned) == (1 if operation == "install" else 0)
    finally:
        f.permit_send.set()
        await asyncio.gather(*pending)
    await released(f.activity)
    assert f.activity.acquired == f.activity.released == 2
    assert len(f.sent) == 1 and f.sent[0]["success"] is (not failed)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("failed", [False, True])
async def test_timeout_releases_handler_first_then_native_once(monkeypatch, operation, failed):
    entered, permit_native, native_exited = threading.Event(), threading.Event(), threading.Event()

    def native(*args):
        entered.set()
        try:
            assert permit_native.wait(2)
            if operation == "install":
                assert args[1].is_file()
            if failed:
                raise OSError("late native error")
            return True, "", "late success"
        finally:
            native_exited.set()

    f = fixture(monkeypatch, operation, native, timeout=True)
    pending = []
    try:
        await f.handler(f.client, f.message)
        pending = list(f.tasks)
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.gather(*pending)
        assert f.activity.acquired == 2 and f.activity.released == 1
        assert not f.activity.acquire("review-device", maintenance=True)
        assert f.cleaned == []
        assert all(path.is_file() for path in f.paths)
        assert len(f.sent) == 1 and f.sent[0]["reason"] == "timeout"
    finally:
        permit_native.set()
        assert await asyncio.to_thread(native_exited.wait, 1)
        await asyncio.gather(*pending, return_exceptions=True)
        await released(f.activity)
    assert f.activity.acquired == f.activity.released == 2
    assert len(f.cleaned) == (1 if operation == "install" else 0)
    assert all(not path.exists() for path in f.paths)
    assert len(f.sent) == 1 and f.sent[0]["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("native_fails", [False, True])
async def test_native_and_cleanup_exception_do_not_double_cleanup_or_release(tmp_path, native_fails):
    activity = CountingActivity()
    path = tmp_path / "package"
    path.write_bytes(b"fixture")
    cleanup_calls = []

    def cleanup():
        cleanup_calls.append(True)
        assert not activity.acquire("review-device", maintenance=True)
        path.unlink()
        raise OSError("cleanup failure after unlink")

    def native():
        assert path.is_file()
        if native_fails:
            raise ValueError("original native failure")
        return "ok"

    operation = NativeOperation("review-device", activity, native, cleanup=cleanup)
    if native_fails:
        with pytest.raises(ValueError, match="original native failure"):
            await operation.run()
    else:
        assert await operation.run() == "ok"
    operation.abandon_if_pending()
    operation.abandon_if_pending()
    await released(activity)
    assert activity.acquired == activity.released == 1
    assert cleanup_calls == [True] and not path.exists()


def test_abandon_and_worker_entry_race_has_one_cleanup_and_one_release():
    # Exercise the real lock at the thread pickup boundary with both competitors.
    with ThreadPoolExecutor(max_workers=2) as executor:
        for _ in range(200):
            activity = CountingActivity()
            barrier = threading.Barrier(3)
            calls, cleanups = [], []
            operation = NativeOperation("review-device", activity,
                                        lambda: calls.append(True), cleanup=lambda: cleanups.append(True))

            def enter():
                barrier.wait(timeout=2)
                return operation._invoke()

            def abandon():
                barrier.wait(timeout=2)
                operation.abandon_if_pending()

            worker = executor.submit(enter)
            canceller = executor.submit(abandon)
            barrier.wait(timeout=2)
            worker.result(timeout=2)
            canceller.result(timeout=2)
            operation.abandon_if_pending()
            assert len(calls) in {0, 1}
            assert cleanups == [True]
            assert activity.acquired == activity.released == 1
            assert activity.acquire("review-device", maintenance=True)
            activity.release("review-device", maintenance=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["install", "uninstall"])
async def test_cancel_before_outer_handler_task_starts_does_not_leak(monkeypatch, operation):
    native_calls = []
    f = fixture(monkeypatch, operation, lambda *args: native_calls.append(args))
    await f.handler(f.client, f.message)
    pending = list(f.tasks)
    assert len(pending) == 1
    pending[0].cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await released(f.activity)
    assert f.activity.acquired == f.activity.released == 1
    assert native_calls == [] and f.paths == [] and f.sent == []


@pytest.mark.asyncio
async def test_unsupported_install_transfers_and_cleans_temp_directory_once(monkeypatch):
    f = fixture(monkeypatch, "install", lambda *a: pytest.fail("native must not start"), platform="unsupported")
    await f.handler(f.client, f.message)
    await asyncio.gather(*list(f.tasks))
    await released(f.activity)
    assert f.activity.acquired == f.activity.released == 1
    assert len(f.cleaned) == 1 and not f.paths[0].exists()
    assert f.sent[0]["reason"] == "platform_unsupported"


@pytest.mark.parametrize("platform,expected", [
    ("android", "uninstall_android"), ("harmony", "uninstall_harmony"),
    ("ios", "uninstall_ios"), ("ios_sim", "uninstall_ios_sim"),
])
def test_uninstall_true_device_and_simulator_native_routes_stay_separate(monkeypatch, platform, expected):
    calls = []
    for name in ("uninstall_android", "uninstall_harmony", "uninstall_ios", "uninstall_ios_sim"):
        def native(*args, selected=name):
            calls.append((selected, args))
            return True, "", selected
        monkeypatch.setattr(platforms, name, native)
    assert platforms.uninstall_by_platform(platform, "review-device", "example.review", 35)[0] is True
    assert calls == [(expected, ("review-device", "example.review", 35))]
