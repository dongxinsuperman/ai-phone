"""Auxiliary request defaults agree across adapters, outer guards and project defaults."""
import inspect
from pathlib import Path

import pytest

from ai_phone.config import Settings
from ai_phone.server.analytics.ai import AnalyticsAIClient
from ai_phone.shared.llm.assistants.claude import ClaudeAssistant
from ai_phone.shared.llm.assistants.doubao import DoubaoAssistant
from ai_phone.shared.llm.assistants.openai import OpenAIAssistant
from ai_phone.shared.llm.base import BaseAssistant


TIMEOUT_FIELDS = (
    "assertion_timeout_sec", "audit_timeout_sec",
    "trajectory_cache_recovery_vlm_timeout_sec", "trajectory_cache_v3_coord_timeout_sec",
    "trajectory_cache_v3_rescue_timeout_sec", "trajectory_cache_ephemeral_classifier_timeout_sec",
    "trajectory_cache_ephemeral_gate_timeout_sec",
)
ADAPTERS = (
    (DoubaoAssistant, "doubao"), (ClaudeAssistant, "claude"), (OpenAIAssistant, "openai"),
)


@pytest.mark.parametrize("field", TIMEOUT_FIELDS)
def test_auxiliary_model_timeout_defaults_are_five_minutes(field):
    assert Settings.model_fields[field].default == 300.0
    defaults = Path(__file__).resolve().parents[1] / ".env.defaults"
    assert getattr(Settings(_env_file=defaults), field) == 300.0


@pytest.mark.parametrize("adapter,module", ADAPTERS)
@pytest.mark.asyncio
async def test_package_and_text_helpers_use_five_minute_http_default(monkeypatch, adapter, module):
    assistant = adapter()
    default_timeout = inspect.signature(adapter._post).parameters["timeout"].default
    assert default_timeout == 300.0
    calls = []

    async def post(*, timeout=default_timeout, **kwargs):
        calls.append({"timeout": timeout, **kwargs})
        return "com.demo"

    monkeypatch.setattr(assistant, "_post", post)
    assert await assistant.match_package(app_name="demo", packages=["com.demo"]) == "com.demo"
    await assistant.chat_text("判断", label="子步骤拆解", thinking=True)
    assert [c["timeout"] for c in calls] == [300.0, 300.0]
    assert all(c["thinking"] for c in calls)


@pytest.mark.parametrize("adapter,module", ADAPTERS)
@pytest.mark.asyncio
async def test_all_finished_assertion_adapters_share_the_outer_timeout(monkeypatch, adapter, module):
    settings = Settings(_env_file=None, assertion_timeout_sec=300.0)
    monkeypatch.setattr(f"ai_phone.shared.llm.assistants.{module}.get_settings", lambda: settings)
    assistant = adapter()
    captured = {}

    async def post(**kwargs):
        captured.update(kwargs)
        return "PASS: ok"

    monkeypatch.setattr(assistant, "_post", post)
    await assistant.verify_finished(prompt="验收", prev_before_bytes=b"before", final_bytes=b"final", thinking=True)
    assert captured["timeout"] == 300.0
    assert captured["thinking"] is True


def test_analysis_helpers_do_not_reintroduce_a_shorter_timeout():
    for adapter in (BaseAssistant, DoubaoAssistant, ClaudeAssistant, OpenAIAssistant):
        assert inspect.signature(adapter.analyze_text).parameters["timeout"].default == 300.0
    assert inspect.signature(AnalyticsAIClient).parameters["timeout_seconds"].default == 300.0
