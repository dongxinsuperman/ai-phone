"""AUX effort is opt-in, keeps thinking switches, and never leaks into phone calls."""
import pytest
from pydantic import ValidationError

from ai_phone.config import Settings
from ai_phone.shared.llm.assistants import doubao
from ai_phone.agent.trajectory_cache import ephemeral


def settings(effort=""):
    return Settings(
        _env_file=None, aux_reasoning_effort=effort,
        assistant_api_key="unit-key", assistant_api_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        assistant_model="doubao-seed-2-1-turbo-260628",
        trajectory_cache_ephemeral_classifier_backend="openai_compatible",
        trajectory_cache_ephemeral_classifier_api_key="unit-key",
        trajectory_cache_ephemeral_classifier_api_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        trajectory_cache_ephemeral_classifier_model="doubao-seed-2-1-turbo-260628",
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_classify_enabled=True,
    )


@pytest.mark.parametrize("effort", ["", "low", "medium", "high"])
@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.asyncio
async def test_doubao_post_only_sends_explicit_effort_when_thinking_is_enabled(monkeypatch, effort, thinking):
    monkeypatch.setattr(doubao, "get_settings", lambda: settings(effort))
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}], "usage": {}}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    monkeypatch.setattr(doubao.httpx, "AsyncClient", Client)
    assert await doubao.DoubaoAssistant()._post(messages=[], thinking=thinking, scene="断言系统") == "OK"
    assert captured["thinking"]["type"] == ("enabled" if thinking else "disabled")
    if thinking and effort:
        assert captured["reasoning_effort"] == effort
    else:
        assert "reasoning_effort" not in captured


@pytest.mark.asyncio
async def test_analysis_uses_the_same_aux_effort(monkeypatch):
    monkeypatch.setattr(doubao, "get_settings", lambda: settings("medium"))
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}], "usage": {}}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    monkeypatch.setattr(doubao.httpx, "AsyncClient", Client)
    await doubao.DoubaoAssistant().analyze_text(system="分析", user="输入", thinking=True)
    assert captured["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_popup_classifier_forwards_aux_effort(monkeypatch):
    captured = []

    async def call(**kwargs):
        captured.append(kwargs)
        return '{"plan_intent":"点击按钮","role":"business_required","confidence":0.9}'

    monkeypatch.setattr(ephemeral, "_call_vlm_with_images", call)
    s = settings("low")
    await ephemeral.CacheEphemeralActionClassifier(settings=s).classify_action(
        goal="进入页面", action={"type": "click"}, before_bytes=b"before", after_bytes=b"after",
    )
    assert [row["aux_reasoning_effort"] for row in captured] == ["low"]


@pytest.mark.parametrize("aux_effort,expected", [(None, None), ("", None), ("low", "low"), ("high", "high")])
@pytest.mark.asyncio
async def test_shared_image_helper_only_applies_effort_explicitly_from_aux_caller(monkeypatch, aux_effort, expected):
    captured = {}
    monkeypatch.setattr(ephemeral, "get_settings", lambda: settings("low"))

    async def post(url, key, payload, timeout):
        captured.update(payload)
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(ephemeral, "_post_json", post)
    text = await ephemeral._call_vlm_with_images(
        backend="openai_compatible", api_url=settings().assistant_api_url, api_key="unit-key",
        model="doubao-seed-2-1-turbo-260628", timeout_sec=300, system="判断", prompt="输入", images=[],
        aux_reasoning_effort=aux_effort,
    )
    assert text == "OK"
    assert captured.get("reasoning_effort") == expected


def test_aux_effort_defaults_to_high_and_rejects_unknown_values():
    assert Settings.model_fields["aux_reasoning_effort"].default == "high"
    assert Settings(_env_file=None).aux_reasoning_effort == "high"
    assert settings("").aux_reasoning_effort == ""
    with pytest.raises(ValidationError):
        settings("invalid-effort")
