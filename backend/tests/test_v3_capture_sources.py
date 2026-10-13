import base64
import io
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache.capture_sources.android import AndroidJpegSource
from ai_phone.agent.trajectory_cache.capture_sources.factory import create_source


def jpeg(size=(1080, 2400)):
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "JPEG")
    return out.getvalue()


def device(data):
    calls = []

    def capture(scale, quality):
        calls.append((scale, quality))
        return base64.b64encode(data).decode()

    return SimpleNamespace(jsonrpc=SimpleNamespace(takeScreenshot=capture), calls=calls)


def driver(platform="android"):
    return SimpleNamespace(platform=platform, serial="unit", window_size=lambda: (1080, 2400))


def test_borrowed_connection_is_not_initialized_or_closed_again():
    raw = jpeg()
    d = device(raw)
    source = AndroidJpegSource(driver(), d)
    source.initialize()
    assert source.capture(95, None) == raw
    assert d.calls == [(1, 95), (1, 95)]
    source.close()
    assert source.device is None  # borrowed d is otherwise unchanged, has no stop/close method


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["ios", "ios_sim", "harmony"])
async def test_other_platforms_keep_original_without_new_sessions(platform):
    source, label = await create_source(driver(platform), enabled=False)
    assert source is None and "原截图通道" in label


@pytest.mark.asyncio
async def test_failed_preflight_keeps_original_and_does_not_close_borrowed_client():
    d = SimpleNamespace(jsonrpc=SimpleNamespace(takeScreenshot=lambda *a: None))
    source, label = await create_source(driver(), borrowed_device=d, enabled=True)
    assert source is None and "启动检查未通过" in label


@pytest.mark.asyncio
async def test_bad_orientation_at_startup_is_not_selected():
    source, label = await create_source(driver(), borrowed_device=device(jpeg((2400, 1080))), enabled=True)
    assert source is None and "ValueError" in label


@pytest.mark.asyncio
async def test_independent_source_initializes_once_and_owns_only_its_client(monkeypatch):
    from adbutils import adb

    monkeypatch.setattr(adb, "device_list", lambda: [SimpleNamespace(serial="unit")])
    calls = []
    raw = jpeg()

    class Device:
        def __init__(self, serial):
            calls.append(("init", serial))
            self.jsonrpc = SimpleNamespace(takeScreenshot=lambda *a: base64.b64encode(raw).decode())

        def stop_uiautomator(self, **kw):
            calls.append(("stop", kw))

    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(Device=Device))
    source, _ = await create_source(driver(), enabled=True)
    source.capture(95, None)
    source.capture(95, None)
    source.close()
    source.close()
    assert calls == [("init", "unit"), ("stop", {"wait": False})]


@pytest.mark.asyncio
async def test_runtime_error_is_not_a_hidden_adb_second_capture():
    d = device(jpeg())
    source, _ = await create_source(driver(), borrowed_device=d, enabled=True)
    d.jsonrpc.takeScreenshot = lambda *a: None
    with pytest.raises(RuntimeError, match="unavailable"):
        source.capture(95, None)
    source.close()


@pytest.mark.asyncio
async def test_shared_locator_connection_survives_final_capture(monkeypatch):
    from ai_phone.agent.trajectory_cache import orchestrate, v3_replay, assertion
    from ai_phone.agent.trajectory_cache.jev_click import factory as locator_factory
    from ai_phone.agent.trajectory_cache.capture_sources import factory as capture_factory

    loc = SimpleNamespace(adapter=SimpleNamespace(device=object()), initialization_error="", closed=False)

    async def loc_close():
        loc.closed = True

    loc.aclose = loc_close
    source = SimpleNamespace(closed=False)

    def source_capture(*args):
        assert not source.closed and not loc.closed
        return b"final"

    def source_close():
        source.closed = True

    source.capture, source.close = source_capture, source_close
    monkeypatch.setattr(locator_factory, "create_locator", AsyncMock(return_value=loc))
    create = AsyncMock(return_value=(source, "unit-fast"))
    monkeypatch.setattr(capture_factory, "create_source", create)

    class Runner:
        execution_history = []

        def __init__(self, **kwargs):
            self.provider = kwargs["screenshot_provider"]

        async def run(self):
            return SimpleNamespace(
                success=True, actions_executed=2, final_before_bytes=b"before", elapsed_ms=1
            )

        async def capture_final_frame(self):
            return self.provider(95, None)

    monkeypatch.setattr(v3_replay, "V3ReplayRunner", Runner)
    verify = AsyncMock(return_value=assertion.CacheAssertionResult("PASS", "unit"))
    monkeypatch.setattr(assertion.CacheReplayAssertionVerifier, "verify", verify)
    bridge = SimpleNamespace(emit=lambda e: None, send_run_done=AsyncMock())
    await orchestrate.run_v3_replay(
        run_id="unit",
        serial="unit",
        goal="goal",
        attempt=1,
        driver=driver(),
        bridge=bridge,
        snapshot={"cache_mode": "v3"},
        settings=Settings(_env_file=None, jev_enabled=True),
    )
    assert create.call_args.kwargs["borrowed_device"] is loc.adapter.device
    assert verify.call_args.kwargs["final_bytes"] == b"final"
    assert loc.closed and source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["android", "harmony", "ios"])
async def test_jev_disabled_never_initializes_fast_channel(platform):
    source, label = await create_source(driver(platform), enabled=False)
    assert source is None
    assert "JEV关闭" in label
