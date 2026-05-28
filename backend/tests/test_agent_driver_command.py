from __future__ import annotations

import base64
from typing import Any, Dict, List

import pytest

from ai_phone.agent import main as agent_main
from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.runner.stability import StabilityResult


class FakeClient:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send(self, payload: Dict[str, Any]) -> bool:
        self.sent.append(payload)
        return True


class FakeDriver:
    platform = "android"

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def prepare_for_run(self, **kwargs):
        self.calls.append(("prepare_for_run", kwargs))

    def window_size(self):
        return (1080, 2400)

    def screenshot_jpeg(self, quality: int = 25, max_side=None):
        self.calls.append(("screenshot_jpeg", quality, max_side))
        return b"\xff\xd8fake-jpeg"

    def click(self, x: int, y: int):
        self.calls.append(("click", x, y))

    def device_info(self):
        return DeviceInfo(
            serial="S1",
            platform="android",
            brand="Test",
            model="M1",
            screen_width=1080,
            screen_height=2400,
        )

    def scroll(self, direction: str, center=None, amount: int = 1):
        self.calls.append(("scroll", direction, center, amount))


@pytest.mark.asyncio
async def test_handle_driver_command_window_size(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m1",
            "run_id": "r1",
            "serial": "S1",
            "method": "window_size",
            "params": {},
            "deadline_ms": 3000,
        },
    )

    assert len(client.sent) == 1
    msg = client.sent[0]
    assert msg["type"] == "driver_result"
    assert msg["message_id"] == "m1"
    assert msg["method"] == "window_size"
    assert msg["ok"] is True
    assert msg["result"] == [1080, 2400]


@pytest.mark.asyncio
async def test_handle_driver_command_prepare_for_run(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m0",
            "run_id": "r1",
            "serial": "S1",
            "method": "prepare_for_run",
            "params": {},
            "deadline_ms": 3000,
        },
    )

    assert client.sent[0]["ok"] is True
    assert driver.calls == [("prepare_for_run", {})]


@pytest.mark.asyncio
async def test_handle_driver_command_android_prepare_ignores_wake_policy(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m0p",
            "run_id": "r1",
            "serial": "S1",
            "method": "prepare_for_run",
            "params": {"wake_policy": {"wake_swipe": True}},
            "deadline_ms": 3000,
        },
    )

    assert client.sent[0]["ok"] is True
    assert driver.calls == [("prepare_for_run", {})]


@pytest.mark.asyncio
async def test_handle_driver_command_harmony_prepare_runs_before_open(monkeypatch):
    calls: List[str] = []
    agent_main._serial_platform["H1"] = "harmony"
    monkeypatch.setattr(
        agent_main,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {"harmony_wake_before_run": True},
        )(),
    )
    monkeypatch.setattr(
        agent_main,
        "_prepare_harmony_before_open",
        lambda serial, **kwargs: calls.append((serial, kwargs)),
    )
    monkeypatch.setattr(
        agent_main,
        "_get_or_open_driver",
        lambda serial: (_ for _ in ()).throw(AssertionError("should not open driver")),
    )
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m0h",
            "run_id": "r1",
            "serial": "H1",
            "method": "prepare_for_run",
            "params": {"wake_policy": {"wake_swipe": True}},
            "deadline_ms": 3000,
        },
    )

    assert client.sent[0]["ok"] is True
    assert calls == [("H1", {"wake_policy": {"wake_swipe": True}})]


@pytest.mark.asyncio
async def test_handle_driver_command_serializes_bytes(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m2",
            "run_id": "r1",
            "serial": "S1",
            "method": "screenshot_jpeg",
            "params": {"quality": 40, "max_side": 800},
        },
    )

    result = client.sent[0]["result"]
    assert result["encoding"] == "base64"
    assert result["mime"] == "image/jpeg"
    assert base64.b64decode(result["data"]) == b"\xff\xd8fake-jpeg"
    assert driver.calls == [("screenshot_jpeg", 40, 800)]


@pytest.mark.asyncio
async def test_handle_driver_command_wait_stable_screenshot_jpeg(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    agent_main._clear_stable_frame_cache("", "")

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        assert frame_a_bytes is None
        assert kwargs["total_timeout_s"] == 6.0
        assert kwargs["poll_interval_s"] == 0.5
        assert kwargs["threshold"] == 0.03
        assert kwargs["enabled"] is True
        data = await screenshot()
        assert data == b"\xff\xd8fake-jpeg"
        return StabilityResult(
            b"\xff\xd8stable",
            True,
            1234,
            2,
        )

    monkeypatch.setattr(agent_main, "wait_page_stable_pixel", fake_wait_stable)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m-stable",
            "run_id": "r-stable",
            "serial": "S1",
            "method": "wait_stable_screenshot_jpeg",
            "params": {
                "quality": 40,
                "max_side": 800,
                "enabled": True,
                "total_timeout_s": 6.0,
                "poll_interval_s": 0.5,
                "threshold": 0.03,
                "strategy": "cache_phash",
            },
        },
    )

    msg = client.sent[0]
    assert msg["ok"] is True
    result = msg["result"]
    assert result["stable"] is True
    assert result["elapsed_ms"] == 1234
    assert result["checks"] == 2
    assert result["strategy"] == "cache_phash"
    assert result["reused_frame"] is False
    assert base64.b64decode(result["image"]["data"]) == b"\xff\xd8stable"
    assert driver.calls == [("screenshot_jpeg", 40, 800)]


@pytest.mark.asyncio
async def test_handle_driver_command_wait_stable_screenshot_jpeg_v3(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    agent_main._clear_stable_frame_cache("", "")

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        assert frame_a_bytes is None
        assert kwargs["phash_threshold"] == 0.02
        assert kwargs["roi_threshold"] == 0.12
        assert kwargs["black_threshold"] == 0.08
        assert kwargs["enabled"] is True
        await screenshot()
        return StabilityResult(b"\xff\xd8v3", True, 2222, 3)

    monkeypatch.setattr(agent_main, "wait_page_stable_v2_compare", fake_wait_stable)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m-stable-v3",
            "run_id": "r-stable-v3",
            "serial": "S1",
            "method": "wait_stable_screenshot_jpeg",
            "params": {
                "quality": 90,
                "max_side": 1568,
                "enabled": True,
                "total_timeout_s": 7.0,
                "poll_interval_s": 0.6,
                "threshold": 0.02,
                "roi_threshold": 0.12,
                "black_threshold": 0.08,
                "strategy": "v3_compare",
            },
        },
    )

    result = client.sent[0]["result"]
    assert result["strategy"] == "v3_compare"
    assert result["checks"] == 3
    assert base64.b64decode(result["image"]["data"]) == b"\xff\xd8v3"
    assert driver.calls == [("screenshot_jpeg", 90, 1568)]


@pytest.mark.asyncio
async def test_handle_driver_command_unknown_method_returns_error(monkeypatch):
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: FakeDriver())
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m3",
            "run_id": "r1",
            "serial": "S1",
            "method": "wipe_device",
            "params": {},
        },
    )

    msg = client.sent[0]
    assert msg["ok"] is False
    assert msg["error"]["category"] == "device"
    assert msg["error"]["error_class"] == "UnknownDriverMethod"


@pytest.mark.asyncio
async def test_handle_driver_command_scroll_center_list_to_tuple(monkeypatch):
    driver = FakeDriver()
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    client = FakeClient()

    await agent_main._handle_driver_command(
        client,
        {
            "type": "driver_command",
            "message_id": "m4",
            "run_id": "r1",
            "serial": "S1",
            "method": "scroll",
            "params": {"direction": "down", "center": [100, 200], "amount": 2},
        },
    )

    assert client.sent[0]["ok"] is True
    assert driver.calls == [("scroll", "down", (100, 200), 2)]
