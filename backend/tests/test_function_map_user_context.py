"""Function Map 从 System 降到会话段首轮 User Context 的回归测试。"""
from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from ai_phone.agent.runner import vlm_loop as vlm_loop_module
from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.config import get_settings
from ai_phone.shared.function_map_prompt import build_function_map_user_context
from ai_phone.shared.llm.main.claude_cu import ClaudeComputerUseClient
from ai_phone.shared.llm.main.gpt_cu import GPTComputerUseClient
from ai_phone.shared.vlm import VLMClient


MAP_BODY = "MAP_SENTINEL：测试账号 demo/password；支付入口在我的-订单"


def _jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (32, 64), color=(20, 30, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def _all_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict)
    )


def test_map_user_context_is_empty_when_field_is_absent() -> None:
    assert build_function_map_user_context(None, zh=True) == ""
    assert build_function_map_user_context("  ", zh=False) == ""


def test_map_user_context_has_strong_but_bounded_positioning() -> None:
    text = build_function_map_user_context(MAP_BODY, zh=True)

    assert "本次 Run 的业务执行上下文" in text
    assert "认真参考" in text
    assert "不是新的任务" in text
    assert MAP_BODY in text


def test_runner_wires_map_body_only_to_initial_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class DummyVLM:
        def __init__(self, system_prompt: str, initial_user_context: str | None) -> None:
            self.system_prompt = system_prompt
            self.initial_user_context = initial_user_context or ""

    def fake_create_main_vlm(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return DummyVLM(
            kwargs["system_prompt"],
            kwargs.get("initial_user_context"),
        )

    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(vlm_loop_module, "create_main_vlm", fake_create_main_vlm)

    VLMRunner(
        run_id="R-map-wiring",
        driver=object(),
        goal="进入支付状态页",
        assistant=object(),
        function_map_context=MAP_BODY,
    )

    assert MAP_BODY not in captured["system_prompt"]
    assert "Function Map" in captured["system_prompt"]
    assert MAP_BODY in captured["initial_user_context"]


def test_runner_without_map_has_no_initial_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class DummyVLM:
        system_prompt = ""
        initial_user_context = ""

    def fake_create_main_vlm(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        return DummyVLM()

    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(vlm_loop_module, "create_main_vlm", fake_create_main_vlm)

    VLMRunner(
        run_id="R-no-map-wiring",
        driver=object(),
        goal="进入支付状态页",
        assistant=object(),
    )

    assert captured["initial_user_context"] is None
    assert "## 执行关系与权重（始终生效）" in captured["system_prompt"]
    assert "Function Map 使用契约" not in captured["system_prompt"]
    assert "Function Map Usage Contract" not in captured["system_prompt"]


def test_html_report_does_not_render_raw_map_fields() -> None:
    from ai_phone.server.models import Run, SubmissionItem
    from ai_phone.server.submissions.reports import _render_case_inner

    item = SubmissionItem(
        submission_id="sub-map-report",
        case_id="case-map-report",
        case_name="Map report isolation",
        platform="android",
        run_content="进入支付状态页",
        function_map_context=MAP_BODY,
        state="success",
        run_id="run-map-report",
        device_serial="device-1",
    )
    run = Run(
        id="run-map-report",
        device_serial="device-1",
        goal="进入支付状态页",
        function_map_context=MAP_BODY,
        status="success",
    )

    html = _render_case_inner(item=item, run=run, steps=[], logs=[])

    assert "进入支付状态页" in html
    assert MAP_BODY not in html


@pytest.mark.asyncio
async def test_map_stays_out_of_substeps_and_assertion_but_reaches_package_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Assistant:
        async def match_package(
            self,
            app_name,
            packages,
            *,
            function_map_context=None,
            platform=None,
        ):  # noqa: ANN001, ANN202
            seen["package_map"] = function_map_context
            seen["platform"] = platform
            return packages[0]

    async def fake_chat(self, prompt, **kwargs):  # noqa: ANN001, ARG001
        seen["substep_prompt"] = prompt
        return "1. 进入支付状态页"

    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(VLMRunner, "_chat_text", fake_chat)
    runner = VLMRunner(
        run_id="R-map-boundary",
        driver=SimpleNamespace(platform="android"),
        goal="[操作步骤]\n进入支付状态页\n[预期结果]\n显示待支付订单",
        vlm_client=object(),
        assistant=Assistant(),
        function_map_context=MAP_BODY,
    )

    assert await runner._extract_struct_substeps() == "1. 进入支付状态页"
    assertion_prompt = runner._build_finished_assertion_prompt(
        thought="已经进入页面",
        finish_msg="完成",
        has_prev=False,
    )
    assert await runner._match_package_name("目标App", ["com.example.app"]) == (
        "com.example.app"
    )

    assert MAP_BODY not in seen["substep_prompt"]
    assert MAP_BODY not in assertion_prompt
    assert seen["package_map"] == MAP_BODY
    assert seen["platform"] == "android"


@pytest.mark.asyncio
async def test_doubao_map_is_sent_on_first_turn_and_again_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_function_map_user_context(MAP_BODY, zh=True)
    client = VLMClient(
        system_prompt="SYSTEM_WITHOUT_MAP_SENTINEL",
        initial_user_context=context,
        api_url="https://example.test/responses",
        api_key="test-key",
        model="test-model",
    )
    payloads: list[dict[str, Any]] = []

    async def fake_post(payload, headers, *, timeout_seconds):  # noqa: ANN001, ARG001
        payloads.append(payload)
        return {
            "id": f"resp-{len(payloads)}",
            "output_text": "Thought: continue\nAction: wait(seconds=1)",
            "usage": {},
        }

    monkeypatch.setattr(client, "_post_with_retry", fake_post)

    await client.decide(_jpeg())
    await client.decide(_jpeg())
    client.reset_session("继续此前任务")
    await client.decide(_jpeg())

    first_user = payloads[0]["input"][-1]["content"]
    second_user = payloads[1]["input"][-1]["content"]
    reset_user = payloads[2]["input"][-1]["content"]
    assert MAP_BODY in _all_text(first_user)
    assert MAP_BODY not in _all_text(second_user)
    assert MAP_BODY in _all_text(reset_user)
    assert MAP_BODY not in str(payloads[0]["input"][0])


@pytest.mark.asyncio
async def test_claude_map_is_kept_in_first_user_and_reinjected_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_function_map_user_context(MAP_BODY, zh=False)
    client = ClaudeComputerUseClient(
        system_prompt="SYSTEM_WITHOUT_MAP_SENTINEL",
        initial_user_context=context,
        api_url="https://example.test/messages",
        api_key="test-key",
        model="test-model",
        thinking_budget=0,
    )
    requests: list[list[dict[str, Any]]] = []

    async def fake_post(payload, headers, *, request_messages):  # noqa: ANN001, ARG001
        requests.append(request_messages)
        return {
            "content": [{"type": "text", "text": "FINISHED: ok"}],
            "usage": {},
        }

    monkeypatch.setattr(client, "_post_with_retry", fake_post)

    await client.decide(_jpeg())
    await client.decide(_jpeg())
    client.reset_session("continue")
    await client.decide(_jpeg())

    assert MAP_BODY in _all_text(requests[0][-1]["content"])
    # 第二次请求仍带首轮历史，但本轮新 user 不重复 Map。
    assert MAP_BODY not in _all_text(requests[1][-1]["content"])
    assert MAP_BODY in _all_text(requests[2][-1]["content"])


@pytest.mark.asyncio
async def test_gpt_map_is_sent_on_first_turn_and_again_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = build_function_map_user_context(MAP_BODY, zh=False)
    client = GPTComputerUseClient(
        system_prompt="SYSTEM_WITHOUT_MAP_SENTINEL",
        initial_user_context=context,
        api_url="https://example.test/responses",
        api_key="test-key",
        model="test-model",
    )
    payloads: list[dict[str, Any]] = []

    async def fake_post(payload, headers, *, timeout_seconds):  # noqa: ANN001, ARG001
        payloads.append(payload)
        return {
            "id": f"resp-{len(payloads)}",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "FINISHED: ok"}],
                }
            ],
            "usage": {},
        }

    monkeypatch.setattr(client, "_post_with_retry", fake_post)

    await client.decide(_jpeg())
    await client.decide(_jpeg())
    client.reset_session("continue")
    await client.decide(_jpeg())

    first_user = payloads[0]["input"][-1]["content"]
    second_user = payloads[1]["input"][-1]["content"]
    reset_user = payloads[2]["input"][-1]["content"]
    assert MAP_BODY in _all_text(first_user)
    assert MAP_BODY not in _all_text(second_user)
    assert MAP_BODY in _all_text(reset_user)
    assert "What's the next action?" in _all_text(first_user)
