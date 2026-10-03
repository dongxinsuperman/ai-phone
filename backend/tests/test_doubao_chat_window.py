"""Strict window/transport tests; no real API or device calls."""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from ai_phone.config import Settings
from ai_phone.shared.llm import create_main_vlm
from ai_phone.shared.llm.main.doubao_chat_window import DoubaoChatWindowClient
from ai_phone.shared.vlm import TokenCounter, VLMClient


def settings(**overrides):
    values = dict(
        vlm_backend="doubao_responses",
        vlm_context_mode="sliding_window",
        vlm_history_window_rounds=5,
        vlm_chat_api_url="https://phone.invalid/api/v3/chat/completions",
        vlm_api_key="phone-test-key",
        vlm_model="phone-model",
        assistant_api_url="https://aux.invalid/chat/completions",
        assistant_api_key="different-aux-key",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def reply(raw="", *, n=1):
    if not raw:
        raw = (
            f'Thought: response-{n}\n<seed:tool_call><function name="press_home"></function></seed:tool_call>'
        )
    return {
        "id": f"unreferenced-{n}",
        "choices": [{"message": {"content": raw}}],
        "usage": {
            "prompt_tokens": 3000,
            "completion_tokens": 20,
            "total_tokens": 3020,
            "prompt_tokens_details": {"cached_tokens": 2000},
        },
    }


def users(payload):
    return [m for m in payload["messages"] if m["role"] == "user"]


def images(payload):
    return [b["image_url"]["url"] for m in users(payload) for b in m["content"] if b["type"] == "image_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [1, 3, 5])
async def test_strict_window_discards_old_images_responses_and_hints(rounds):
    client = DoubaoChatWindowClient(
        "original-system",
        initial_user_context="original-map",
        settings=settings(vlm_history_window_rounds=rounds),
    )
    requests = []

    async def post(payload, headers, **kwargs):
        requests.append(copy.deepcopy(payload))
        assert headers["Authorization"] == "Bearer phone-test-key"
        return reply(n=len(requests))

    client._post_with_retry = post
    for n in range(1, 10):
        client.add_hint(f"hint-{n}")
        await client.decide(f"screen-{n}".encode())
        p = requests[-1]
        assert len(images(p)) == min(n, rounds)
        expected = [
            r["messages"][-1]["content"][-2]["image_url"]["url"] for r in requests[max(0, n - rounds) : n]
        ]
        assert images(p) == expected
        texts = [b["text"] for u in users(p) for b in u["content"] if b["type"] == "text"]
        assert [t for t in texts if t.startswith("hint-")] == [
            f"hint-{j}" for j in range(max(1, n - rounds + 1), n + 1)
        ]
        assert texts.count("original-map") == 1
        assert users(p)[0]["content"][0] == {"type": "text", "text": "original-map"}
        history = [m["content"] for m in p["messages"] if m["role"] == "assistant"]
        assert history == [
            reply(n=j)["choices"][0]["message"]["content"] for j in range(max(1, n - rounds + 1), n)
        ]
        assert p["messages"][0] == {"role": "system", "content": "original-system"}
        assert len(client._history) <= rounds - 1
        assert not client.pending_hints
        assert not (set(p) & {"input", "previous_response_id", "caching", "store", "tools", "max_tokens"})
    assert len({p["prompt_cache_key"] for p in requests}) == 1


@pytest.mark.asyncio
async def test_empty_map_and_late_system_injection_preserve_original_contract():
    client = DoubaoChatWindowClient("before-substeps", settings=settings())
    client.system_prompt = "full-case-and-injected-substeps"
    seen = []

    async def post(p, h, **kw):
        seen.append(copy.deepcopy(p))
        return reply()

    client._post_with_retry = post
    decision = await client.decide(b"frame", mime="image/png")
    assert seen[0]["messages"][0]["content"] == client.system_prompt
    assert users(seen[0])[0]["content"][0]["type"] == "image_url"
    assert images(seen[0])[0].startswith("data:image/png;base64,")
    assert decision.parsed_actions[0].action == "press_home"
    assert decision.thought == "response-1"
    assert decision.raw_content == reply()["choices"][0]["message"]["content"]
    assert client.counter.summary()["cached_tokens"] == 2000
    assert client.counter.summary()["by_scene"][0]["scene"] == "VLM决策"


@pytest.mark.asyncio
async def test_no_legacy_reset_and_independent_run_cache_keys():
    a = DoubaoChatWindowClient("case-a", settings=settings(vlm_session_reset_prompt_threshold=1))
    b = DoubaoChatWindowClient("case-b", settings=settings())
    assert a._prompt_cache_key != b._prompt_cache_key
    assert a._history is not b._history
    a.counter.last_prompt_tokens = 999999
    assert not a.should_reset_session()
    assert a.reset_session("old-count-only-hint") is None
    assert a.segment_count == 1 and not a.pending_hints


@pytest.mark.asyncio
async def test_request_failure_does_not_commit_history_or_lose_hints():
    client = DoubaoChatWindowClient("system", settings=settings())
    client.add_hint("old-hint")

    async def fail(*args, **kwargs):
        client.add_hint("hint-added-while-awaiting")
        raise httpx.ReadTimeout("probe")

    client._post_with_retry = fail
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        await client.decide(b"frame")
    assert list(client._history) == []
    assert client.counter.call_count == 0
    assert client.pending_hints == ["old-hint", "hint-added-while-awaiting"]


@pytest.mark.asyncio
async def test_invalid_xml_correction_retains_only_the_actual_model_rounds():
    client = DoubaoChatWindowClient("unchanged-system", settings=settings())
    requests = []

    async def post(p, h, **kw):
        requests.append(copy.deepcopy(p))
        return reply("Action: click(1, 2)") if len(requests) == 1 else reply(n=2)

    client._post_with_retry = post
    first = await client.decide(b"same-frame")
    assert first.parsed_actions[0].action == "assert_fail"
    assert "无法解析决策输出" in first.parsed_actions[0].content
    client.add_hint("same-image-protocol-correction")
    second = await client.decide(b"same-frame")
    assert second.parsed_actions[0].action == "press_home"
    assert images(requests[1])[0] == images(requests[1])[1]
    assert requests[1]["messages"][2] == {"role": "assistant", "content": "Action: click(1, 2)"}
    assert any(b.get("text") == "same-image-protocol-correction" for b in users(requests[1])[-1]["content"])
    assert client.counter.call_count == 2


@pytest.mark.asyncio
async def test_mutating_wire_copy_does_not_pollute_saved_history():
    client = DoubaoChatWindowClient("system", initial_user_context="map", settings=settings())
    calls = 0

    async def post(p, h, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            p["messages"][-1]["content"].append({"type": "text", "text": "wire-only"})
        else:
            assert "wire-only" not in json.dumps(p)
        return reply(n=calls)

    client._post_with_retry = post
    await client.decide(b"first")
    await client.decide(b"second")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_status", [429, 500, 503, "network"])
async def test_transport_retries_same_payload_once(monkeypatch, first_status):
    client = DoubaoChatWindowClient("system", settings=settings())
    seen = []

    class FakeHTTP:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 120.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, json, headers):
            seen.append(copy.deepcopy(json))
            assert url == "https://phone.invalid/api/v3/chat/completions"
            if first_status == "network" and len(seen) == 1:
                raise httpx.ReadTimeout("transport probe")
            return httpx.Response(first_status if len(seen) == 1 else 200, json=reply())

    async def no_sleep(*args):
        pass

    monkeypatch.setattr("ai_phone.shared.llm.main.doubao_chat_window.httpx.AsyncClient", FakeHTTP)
    monkeypatch.setattr("ai_phone.shared.llm.main.doubao_chat_window.asyncio.sleep", no_sleep)
    await client.decide(b"frame")
    assert seen[0] == seen[1]
    assert len(client._history) == 1
    assert client.counter.call_count == 1


@pytest.mark.asyncio
async def test_transport_does_not_retry_bad_request(monkeypatch):
    client = DoubaoChatWindowClient("system", settings=settings())
    calls = []

    class FakeHTTP:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kw):
            calls.append(1)
            return httpx.Response(400, text="invalid payload")

    monkeypatch.setattr("ai_phone.shared.llm.main.doubao_chat_window.httpx.AsyncClient", FakeHTTP)
    with pytest.raises(RuntimeError, match="status=400"):
        await client.decide(b"frame")
    assert len(calls) == 1 and not client._history


def test_session_factory_preserves_original_client_and_arguments(monkeypatch):
    cfg = settings(vlm_context_mode="session")
    calls = []

    def init(self, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(VLMClient, "__init__", init)
    counter = TokenCounter()
    client = create_main_vlm("system", initial_user_context="map", counter=counter, settings=cfg)
    assert type(client) is VLMClient
    assert calls == [{"system_prompt": "system", "initial_user_context": "map", "counter": counter}]


def test_factory_uses_phone_settings_not_auxiliary_settings():
    cfg = settings()
    client = create_main_vlm("system", settings=cfg)
    assert isinstance(client, DoubaoChatWindowClient)
    assert client.api_url == cfg.vlm_chat_api_url
    assert client.api_key == cfg.vlm_api_key and client.api_key != cfg.assistant_api_key
    assert cfg.vlm_backend == "doubao_responses"


@pytest.mark.parametrize(
    "backend,module,name",
    [
        ("claude_cu", "ai_phone.shared.llm.main.claude_cu", "ClaudeComputerUseClient"),
        ("gpt_cu", "ai_phone.shared.llm.main.gpt_cu", "GPTComputerUseClient"),
    ],
)
def test_window_switch_does_not_change_overseas_factory(monkeypatch, backend, module, name):
    sentinel = object()
    monkeypatch.setattr(f"{module}.{name}", lambda **kwargs: sentinel)
    assert create_main_vlm("system", settings=settings(vlm_backend=backend)) is sentinel


@pytest.mark.parametrize("rounds", [0, -1, 65])
def test_settings_reject_invalid_window(rounds):
    with pytest.raises(ValidationError):
        settings(vlm_history_window_rounds=rounds)


@pytest.mark.parametrize("rounds", [0, 65, "5", True])
def test_client_also_validates_unvalidated_runtime_overrides(rounds):
    cfg = settings().model_copy(update={"vlm_history_window_rounds": rounds})
    with pytest.raises(RuntimeError, match="1-64"):
        DoubaoChatWindowClient("system", settings=cfg)


def test_invalid_mode_is_not_silently_changed_to_session():
    with pytest.raises(ValidationError):
        settings(vlm_context_mode="typo")
    with pytest.raises(RuntimeError, match="只支持"):
        create_main_vlm("system", settings=settings().model_copy(update={"vlm_context_mode": "typo"}))
