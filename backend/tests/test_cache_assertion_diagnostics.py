"""Failure diagnostics must be useful without changing cache assertion policy."""
import asyncio

import httpx
import pytest

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache.assertion import CacheReplayAssertionVerifier


def settings():
    return Settings(_env_file=None, assistant_api_key="test-key",
                    assistant_api_url="https://example.test", assistant_model="test-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout(""), RuntimeError("upstream unavailable")])
async def test_cache_assertion_failure_includes_exception_type_and_remains_skip(error):
    class Assistant:
        async def verify_finished(self, **kwargs):
            raise error

    result = await CacheReplayAssertionVerifier(settings=settings(), assistant=Assistant()).verify(
        goal="查看结果", final_bytes=b"final",
    )
    assert result.verdict == "SKIP"
    assert not result.passed
    assert type(error).__name__ in result.reason
    if str(error):
        assert str(error) in result.reason


@pytest.mark.asyncio
async def test_cache_assertion_outer_timeout_is_identifiable_and_cancels_call():
    cancelled = False

    class Assistant:
        async def verify_finished(self, **kwargs):
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True

    config = settings().model_copy(update={"assertion_timeout_sec": 0.01})
    result = await CacheReplayAssertionVerifier(settings=config, assistant=Assistant()).verify(
        goal="查看结果", final_bytes=b"final",
    )
    assert result.verdict == "SKIP"
    assert "TimeoutError" in result.reason
    assert cancelled


@pytest.mark.asyncio
async def test_cache_assertion_does_not_swallow_task_cancellation():
    class Assistant:
        async def verify_finished(self, **kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await CacheReplayAssertionVerifier(settings=settings(), assistant=Assistant()).verify(
            goal="查看结果", final_bytes=b"final",
        )
