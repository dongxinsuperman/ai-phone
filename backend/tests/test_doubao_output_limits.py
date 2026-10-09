"""Capture actual Chat payloads without model calls; keep other providers isolated."""
import pytest

from ai_phone.agent.trajectory_cache import ephemeral, v3_replay


@pytest.mark.parametrize("url,expected", [
    ("https://ark.cn-beijing.volces.com/api/v3/chat/completions", 65536),
    ("https://api.openai.com/v1/chat/completions", None),
    ("https://proxy.example/v1/chat/completions", None),
])
@pytest.mark.asyncio
async def test_image_chat_output_budget_is_ark_only(monkeypatch, url, expected):
    captured = {}

    async def post(api_url, api_key, payload, timeout_sec):
        captured.update(payload)
        assert timeout_sec == 300
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(ephemeral, "_post_json", post)
    assert await ephemeral._call_vlm_with_images(
        backend="openai_compatible", api_url=url, api_key="test", model="test",
        timeout_sec=300, system="test", prompt="test", images=[],
    ) == "OK"
    assert captured.get("max_completion_tokens") == expected
    assert "max_tokens" not in captured


@pytest.mark.parametrize("url,expected", [
    ("https://ark.cn-beijing.volces.com/api/v3/chat/completions", 65536),
    ("https://api.openai.com/v1/chat/completions", None),
])
@pytest.mark.asyncio
async def test_locator_chat_uses_same_output_budget(monkeypatch, url, expected):
    captured = {}

    async def post(**kwargs):
        captured.update(kwargs["payload"])
        assert kwargs["timeout_sec"] == 300
        return "<point>500 500</point>"

    monkeypatch.setattr(v3_replay, "_post_chat_payload", post)
    await v3_replay.V3PlanLocator()._chat_completions_single_image(
        prompt="target", image_bytes=b"test", api_url=url, api_key="test",
        model="test", timeout_sec=300,
    )
    assert captured.get("max_completion_tokens") == expected
    assert "max_tokens" not in captured
