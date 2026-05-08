from __future__ import annotations

import pytest

from ai_phone.config import get_settings
from ai_phone.agent.runner.stability import wait_page_stable_pixel


@pytest.mark.asyncio
async def test_wait_page_stable_disabled_takes_fresh_frame(monkeypatch):
    monkeypatch.setenv("AI_PHONE_VLM_PAGE_STABLE_ENABLED", "false")
    get_settings.cache_clear()
    frames = [b"fresh"]
    logs = []

    async def screenshot():
        return frames.pop(0)

    result = await wait_page_stable_pixel(
        screenshot,
        frame_a_bytes=b"tail",
        log=lambda level, title, content: logs.append((level, title, content)),
    )

    assert result.bytes_ == b"fresh"
    assert result.stable is False
    assert result.checks == 0
    assert not frames
    assert any("未开启" in content for _level, _title, content in logs)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_wait_page_stable_cache_can_be_disabled_independently(monkeypatch):
    monkeypatch.setenv("AI_PHONE_VLM_PAGE_STABLE_ENABLED", "true")
    monkeypatch.setenv("AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_ENABLED", "false")
    get_settings.cache_clear()
    frames = [b"cache-fresh"]
    logs = []

    async def screenshot():
        return frames.pop(0)

    result = await wait_page_stable_pixel(
        screenshot,
        frame_a_bytes=b"tail",
        use_cache_settings=True,
        log=lambda level, title, content: logs.append((level, title, content)),
    )

    assert result.bytes_ == b"cache-fresh"
    assert result.checks == 0
    assert any("未开启" in content for _level, _title, content in logs)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_wait_page_stable_uses_cache_channel_settings(monkeypatch):
    monkeypatch.setenv("AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_TIMEOUT_S", "0.1")
    monkeypatch.setenv("AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_POLL_S", "0.1")
    monkeypatch.setenv("AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_THRESHOLD", "0")
    get_settings.cache_clear()
    logs = []

    async def screenshot():
        return b"same"

    result = await wait_page_stable_pixel(
        screenshot,
        use_cache_settings=True,
        log=lambda level, title, content: logs.append((level, title, content)),
    )

    assert result.bytes_ == b"same"
    assert any(
        "总超时=0.1s" in content and "阈值=0.0" in content
        for _level, title, content in logs
        if title == "页面稳定检测"
    )
    get_settings.cache_clear()
