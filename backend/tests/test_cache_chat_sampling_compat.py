"""Sampling compatibility for cache helpers' Chat Completions fallback."""

import httpx
import pytest

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache.recovery import CacheReplayRecoveryVerifier
from ai_phone.agent.trajectory_cache.v3_replay import V3PlanLocator


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["locator", "recovery"])
@pytest.mark.parametrize(
    "model,expected_top_p",
    [("deepseek-v4-1-flash-261017", 1), ("doubao-seed-2-1-turbo-260628", 0)],
)
async def test_cache_chat_sampling_accepted_by_provider(
    monkeypatch, role, model, expected_top_p
):
    payloads = []

    class ProviderClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, json, headers):
            payloads.append(json)
            # DeepSeek rejects top_p=0; keep Seed's existing sampling unchanged.
            rejected = model.startswith("deepseek") and json.get("top_p") == 0
            return httpx.Response(
                400 if rejected else 200,
                json={"choices": [{"message": {"content": "accepted"}}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", ProviderClient)
    common = dict(
        prompt="locate", api_url="https://example.test/chat/completions",
        api_key="test", model=model, timeout_sec=10,
    )
    settings = Settings(_env_file=None)
    if role == "locator":
        helper = V3PlanLocator(settings=settings)
        text = await helper._chat_completions_single_image(image_bytes=b"image", **common)
    else:
        helper = CacheReplayRecoveryVerifier(settings=settings)
        text = await helper._chat_completions_double_image(
            landmark_bytes=b"landmark", current_bytes=b"current", **common
        )
    assert text == "accepted"
    assert payloads[0]["top_p"] == expected_top_p
