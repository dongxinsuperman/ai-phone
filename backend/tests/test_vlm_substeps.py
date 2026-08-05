"""结构化子步骤按完整 goal 拆解测试。"""
from __future__ import annotations

import pytest

from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.shared.vlm import VLMClient


def _runner(goal: str) -> VLMRunner:
    return VLMRunner(
        run_id="R-substeps",
        driver=object(),  # _extract_struct_substeps 不访问 driver
        goal=goal,
        vlm_client=object(),
        assistant=object(),
    )


@pytest.mark.asyncio
async def test_extract_substeps_sends_complete_goal_and_uses_model_list(monkeypatch) -> None:
    goal = (
        "[测试标题]\n验证个人页\n"
        "[前置条件]\n已登录\n"
        "[操作步骤]\n1. 打开App\n2. 点击我的\n"
        "[预期结果]\n显示个人页"
    )
    captured: list[str] = []

    async def fake_chat(self, prompt, **kwargs):  # noqa: ANN001, ARG001
        captured.append(prompt)
        return "1. 打开App\n2. 点击我的"

    monkeypatch.setattr(VLMRunner, "_chat_text", fake_chat)

    result = await _runner(goal)._extract_struct_substeps()

    assert result == "1. 打开App\n2. 点击我的"
    assert "1. 1." not in result
    assert goal in captured[0]
    assert "[操作步骤]" in captured[0]
    assert "任何一种具体符号都不是必须切分的硬边界" in captured[0]
    assert "不得优化、润色、概括、改写" in captured[0]
    assert "1. 1." in captured[0]
    assert "AI_PHONE_SUBSTEP_BOUNDARY" not in captured[0]


@pytest.mark.asyncio
async def test_extract_substeps_strips_outer_whitespace(monkeypatch) -> None:
    async def fake_chat(self, prompt, **kwargs):  # noqa: ANN001, ARG001
        return "  \n1. 打开App\n2. 点击我的\n  "

    monkeypatch.setattr(VLMRunner, "_chat_text", fake_chat)

    result = await _runner("[操作步骤]\n打开App，点击我的")._extract_struct_substeps()

    assert result == "1. 打开App\n2. 点击我的"


@pytest.mark.asyncio
async def test_extract_substeps_rejects_blank_output(monkeypatch) -> None:
    async def fake_chat(self, prompt, **kwargs):  # noqa: ANN001, ARG001
        return " \n\t "

    monkeypatch.setattr(VLMRunner, "_chat_text", fake_chat)

    result = await _runner("[操作步骤]\n打开App")._extract_struct_substeps()

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "```text\n1. 打开App\n2. 点击我的\n```",
        "下面是拆解结果：\n1. 打开App\n2. 点击我的",
    ],
)
async def test_extract_substeps_rejects_non_list_prefix(monkeypatch, raw: str) -> None:
    async def fake_chat(self, prompt, **kwargs):  # noqa: ANN001, ARG001
        return raw

    monkeypatch.setattr(VLMRunner, "_chat_text", fake_chat)

    result = await _runner("[操作步骤]\n打开App，点击我的")._extract_struct_substeps()

    assert result is None


@pytest.mark.asyncio
async def test_extract_substeps_call_error_keeps_original_goal_path(monkeypatch) -> None:
    async def fake_chat(self, prompt, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("assistant unavailable")

    monkeypatch.setattr(VLMRunner, "_chat_text", fake_chat)

    result = await _runner("[操作步骤]\n打开App")._extract_struct_substeps()

    assert result is None


def test_doubao_session_reset_keeps_injected_substeps_system_prompt() -> None:
    prompt = "system\n## 操作步骤子步骤清单\n1. 点击入口\n2. 进入详情页"
    client = VLMClient(
        system_prompt=prompt,
        api_url="https://example.invalid/v1/responses",
        api_key="test-key",
        model="test-model",
        session_reset_prompt_threshold=1,
    )
    client.previous_response_id = "resp-old"
    client.counter.last_prompt_tokens = 10

    assert client.should_reset_session() is True
    assert client.reset_session("继续执行") == "resp-old"
    assert client.system_prompt == prompt
