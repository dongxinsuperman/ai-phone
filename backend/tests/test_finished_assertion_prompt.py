"""finished 二次断言的证据职责与提示词回归测试。"""
from __future__ import annotations

from typing import Any

import pytest

from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.agent.trajectory_cache.assertion import build_cache_assertion_prompt
from ai_phone.shared.llm.assistants.claude import ClaudeAssistant
from ai_phone.shared.llm.assistants.doubao import DoubaoAssistant
from ai_phone.shared.llm.assistants.openai import OpenAIAssistant


def _runner(*, structured: bool, steps: int = 3) -> VLMRunner:
    runner = object.__new__(VLMRunner)
    runner.goal = (
        "[测试标题]\n验证返回上一页\n[操作步骤]\n进入详情页；点击返回"
        "\n[预期结果]\n显示列表页"
    )
    runner._is_structured = structured
    runner._action_log = [
        {
            "step": step,
            "thought": f"第 {step} 步完整思考，已执行历史操作",
            "action_str": f"action_{step}()",
        }
        for step in range(1, steps + 1)
    ]
    return runner


def test_structured_assertion_requires_history_and_assigns_evidence_roles() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="已经返回列表页",
        finish_msg="已显示列表页",
        has_prev=True,
    )

    assert "最近动作历史：**必须阅读**" in prompt
    assert "当前最终画面：主要证明当前可见状态" in prompt
    assert "动作前对照画面：只描述最后一个动作之前的状态" in prompt
    assert "不能单独证明完成" in prompt
    assert "直接可见事实冲突时，以最终画面为准" in prompt
    assert "最终画面没有展示某段历史过程，不等于该过程没有发生" in prompt
    assert "你只验「预期结果」，不验前置条件、不验操作过程、不验历史顺序" not in prompt
    assert "step   1 思考:第 1 步完整思考" in prompt


def test_structured_assertion_does_not_treat_identical_images_as_automatic_fail() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="状态已变化",
        finish_msg="完成",
        has_prev=True,
    )

    assert "两图相同本身不能直接判 FAIL" in prompt
    assert "只有同时满足以下三项才允许据此 FAIL" in prompt
    assert "等待、截图保存、保持当前状态" in prompt
    assert "双图只辅助判断最后一个动作" in prompt


def test_single_image_assertion_still_requires_action_history() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="完成",
        finish_msg="完成",
        has_prev=False,
    )

    assert "请综合该图、动作历史与主 VLM 最后说明判断" in prompt
    assert "最近动作历史：**必须阅读**" in prompt


def test_freeform_template_uses_history_without_expanding_order_checks() -> None:
    prompt = _runner(structured=False)._build_finished_assertion_prompt(
        thought="已经返回",
        finish_msg="完成",
        has_prev=True,
    )

    assert "必须先阅读用户目标和最近动作历史" in prompt
    assert "已发生但不会持续显示的过程，可以由动作历史证明" in prompt
    assert "除非用户明确把操作顺序作为要求" in prompt
    assert "前面的动作、顺序、中间过渡、是否真的点过某个前置按钮，一律不审查" not in prompt


def test_default_max_length_history_is_present_without_per_entry_truncation() -> None:
    prompt = _runner(structured=True, steps=100)._build_finished_assertion_prompt(
        thought="完成",
        finish_msg="完成",
        has_prev=False,
    )

    assert "step   1 思考:第 1 步完整思考" in prompt
    assert "step 100 思考:第 100 步完整思考" in prompt


def test_cache_assertion_uses_replay_for_history_but_screenshot_for_visible_facts() -> None:
    prompt = build_cache_assertion_prompt(
        goal=(
            "[测试标题]\n验证返回上一页\n[操作步骤]\n点击返回"
            "\n[预期结果]\n显示列表页"
        ),
        trajectory={
            "actions": [{"index": 1, "type": "key_event", "keycode": 4}],
        },
        has_prev=True,
        is_structured=True,
    )

    assert "必须阅读缓存回放摘要" in prompt
    assert "用于证明本次执行过的操作" in prompt
    assert "不能推翻截图里的直接可见事实" in prompt
    assert "两图相同本身不能直接判 FAIL" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assistant", "language"),
    [
        (DoubaoAssistant(), "zh"),
        (ClaudeAssistant(), "en"),
        (OpenAIAssistant(), "en"),
    ],
)
async def test_assistant_system_is_result_oriented_and_evidence_aware(
    monkeypatch: pytest.MonkeyPatch,
    assistant: Any,
    language: str,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "PASS: ok"

    monkeypatch.setattr(assistant, "_post", fake_post)
    await assistant.verify_finished(
        prompt="USER_ASSERTION_PROMPT",
        prev_before_bytes=b"previous",
        final_bytes=b"final",
    )

    if isinstance(assistant, ClaudeAssistant):
        system = captured["system"]
    else:
        system = captured["messages"][0]["content"]

    if language == "zh":
        assert "结果导向，不默认挑错" in system
        assert "不同证据负责不同事实，不做简单的全局优先级排序" in system
        assert "动作历史" in system
        assert "严格保守" not in system
    else:
        assert "Be result-oriented" in system
        assert "do not apply one global ranking" in system
        assert "action history" in system
        assert "strict, conservative" not in system
