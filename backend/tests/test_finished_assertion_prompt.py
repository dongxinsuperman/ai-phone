"""finished 二次断言的证据职责与提示词回归测试。"""
from __future__ import annotations

from typing import Any
from pathlib import Path

import pytest

from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.agent.trajectory_cache.assertion import build_cache_assertion_prompt
from ai_phone.config import Settings
from ai_phone.shared.llm.assertion_policy import (
    FINISHED_ASSERTION_SYSTEM_EN,
    FINISHED_ASSERTION_SYSTEM_ZH,
)
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
            "action_type": "click",
            "runtime_status": "completed_without_exception",
        }
        for step in range(1, steps + 1)
    ]
    return runner


def test_structured_assertion_requires_history_and_assigns_evidence_roles() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="已经返回列表页", finish_msg="已显示列表页", has_prev=True,
    )
    assert "【用户 Case】" in prompt
    assert "【执行过程，按时间顺序】" in prompt
    assert "动作前观察与子步骤判断" in prompt
    assert prompt.index("第 1 步完整思考") < prompt.index("随后实际动作：action_1()")
    assert "执行状态：调用完成且无异常" in prompt
    assert "不得单独作证" not in prompt
    assert "不证明 UI" not in prompt
    assert "裁决两层流程" not in prompt

def test_structured_assertion_does_not_treat_identical_images_as_automatic_fail() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="状态已变化", finish_msg="完成", has_prev=True,
    )
    assert "图1：最后一个实际动作之前的画面" in prompt
    assert "图2：当前最终画面" in prompt
    assert "FAIL" not in prompt  # 裁决规则只在 System 中维护。

def test_single_image_assertion_still_requires_action_history() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="完成", finish_msg="完成", has_prev=False,
    )
    assert "本次没有动作前对照图" in prompt
    assert "【执行过程，按时间顺序】" in prompt
    assert "图2" not in prompt

def test_freeform_template_uses_history_without_expanding_order_checks() -> None:
    free = _runner(structured=False)._build_finished_assertion_prompt(
        thought="已经返回", finish_msg="完成", has_prev=True,
    )
    structured = _runner(structured=True)._build_finished_assertion_prompt(
        thought="已经返回", finish_msg="完成", has_prev=True,
    )
    assert free == structured
    assert "【用户 Case】" in free

def test_default_max_length_history_is_present_without_per_entry_truncation() -> None:
    prompt = _runner(structured=True, steps=100)._build_finished_assertion_prompt(
        thought="完成",
        finish_msg="完成",
        has_prev=False,
    )

    assert "第 1 步\n" in prompt
    assert "第 100 步\n" in prompt
    assert "第 1 步完整思考" in prompt
    assert "第 100 步完整思考" in prompt


def test_finished_declaration_is_not_reused_as_action_evidence() -> None:
    runner = _runner(structured=True, steps=1)
    runner._action_log.append(
        {
            "step": 2,
            "thought": "我已经成功，申请完成",
            "action_str": "finished(content='成功')",
            "action_type": "finished",
            "runtime_status": "terminal_declaration",
        }
    )

    prompt = runner._build_finished_assertion_prompt(
        thought="我已经成功，申请完成",
        finish_msg="成功",
        has_prev=True,
    )

    history = prompt.split("【执行过程，按时间顺序】", 1)[1].split(
        "【主模型最终说明】", 1
    )[0]
    assert "finished(content='成功')" not in history
    assert "第 1 步\n" in history


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

    assert "【执行过程，按时间顺序】" in prompt
    assert "本次缓存回放的动作序列" in prompt
    assert "裁决两层流程" not in prompt
    assert "【用户 Case】" in prompt
    assert "FAIL" not in prompt


def test_cache_freeform_does_not_expand_into_intermediate_step_audit() -> None:
    prompt = build_cache_assertion_prompt(
        goal="打开详情后返回列表页",
        trajectory={
            "actions": [
                {"index": 1, "type": "click", "point": [1, 2]},
                {"index": 2, "type": "key_event", "keycode": 4},
            ],
        },
        has_prev=True,
        is_structured=False,
    )

    assert "打开详情后返回列表页" in prompt
    assert "裁决规则" not in prompt
    assert "【执行过程，按时间顺序】" in prompt


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("has_prev", [False, True])
def test_v3_assertion_uses_only_current_evidence_and_describes_real_image_span(structured, has_prev):
    from copy import deepcopy

    goal = "[预期结果]\n显示正确账号的列表页" if structured else "返回正确账号的列表页"
    trajectory = {"cache_mode": "v3", "source_completion": {
        "run_reason": "旧终态唯一标识", "task_done": "旧完成说明唯一标识",
        "final_thought": "旧思考唯一标识", "assertion_pass": "旧通过理由唯一标识",
    }}
    original = deepcopy(trajectory)
    history = [{"sequence": 1, "index": 1, "source": "rescue_repair",
                "runtime_status": "completed_without_exception", "action": {"type": "press_back"}}]
    prompt = build_cache_assertion_prompt(
        goal=goal, trajectory=trajectory, has_prev=has_prev, is_structured=structured,
        execution_history=history,
    )
    assert goal in prompt
    assert "历史通过结论不能证明本轮成功" in prompt
    assert "优先采纳锚点" not in prompt
    assert "优先采用首次成功" not in prompt
    assert "【首次成功语义锚点】" not in prompt
    assert "首次成功语义锚点说明：" not in prompt
    assert all(value not in prompt for value in trajectory["source_completion"].values())
    assert "source=rescue_repair" in prompt
    assert "只跨越缓存回放的最后一个动作" not in prompt
    if has_prev:
        assert "最后一个缓存步骤开始前" in prompt
        assert "可能包含局部修复、等待、该缓存动作或跳过" in prompt
    else:
        assert "唯一最终截图" in prompt
    if structured:
        assert "裁决两层流程" not in prompt
    else:
        assert "裁决规则" not in prompt
    assert trajectory == original  # 只移除断言输入里的旧说明，不改已有缓存数据。


@pytest.mark.parametrize("mode", ["v1", "v2"])
def test_legacy_cache_assertion_keeps_original_history_and_anchor(mode):
    prompt = build_cache_assertion_prompt(
        goal="返回列表", has_prev=True, is_structured=False,
        trajectory={"cache_mode": mode, "actions": [
            {"index": step, "type": "press_back"} for step in range(1, 22)
        ], "source_completion": {"assertion_pass": "旧缓存的历史语义解释"}},
    )
    assert "step 1:" not in prompt
    assert "step 2:" in prompt
    assert "首次成功语义锚点" in prompt
    assert "旧缓存的历史语义解释" in prompt
    assert "两张图之间只跨越缓存回放的最后一个动作" in prompt


def test_v3_under_hundred_steps_retains_early_required_operation():
    history = [{"sequence": i, "index": i, "source": "cache",
                "runtime_status": "completed_without_exception",
                "action": {"type": "click", "plan_intent": "清空旧账号数据" if i == 1 else "正常步骤"}}
               for i in range(1, 101)]
    prompt = build_cache_assertion_prompt(
        goal="先清空旧账号数据，最终显示正确账号", trajectory={}, has_prev=True,
        execution_history=history,
    )
    assert "record 1 step 1:" in prompt
    assert "plan_intent=清空旧账号数据" in prompt
    assert "record 100 step 100:" in prompt
    assert "前面还有" not in prompt


@pytest.mark.parametrize("identity", [{"cache_mode": "v3"}, {"schema_version": 3}])
def test_old_v3_internal_call_without_runtime_history_is_not_execution_proof(identity):
    prompt = build_cache_assertion_prompt(
        goal="返回列表", has_prev=True, is_structured=False,
        trajectory={**identity, "actions": [{"index": i, "type": "press_back"} for i in range(1, 102)],
                    "source_completion": {"assertion_pass": "旧 PASS 唯一标识"}},
    )
    assert "step 1:" not in prompt
    assert "step 2:" in prompt
    assert "step 101:" in prompt
    assert "旧 PASS 唯一标识" not in prompt
    assert "不证明这些动作本轮已经执行" in prompt


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
        assert system == FINISHED_ASSERTION_SYSTEM_ZH
        assert "以整体语义是否达成为准" in system
        assert "不预设最终截图高于过程信息" in system
        assert "实际动作记录" in system
        assert "动作前状态" in system
        assert "没有发现明确矛盾" in system
        assert "严格保守" not in system
    else:
        assert system == FINISHED_ASSERTION_SYSTEM_EN
        assert "Judge overall semantic completion" in system
        assert "without automatically ranking" in system
        assert "actual action records" in system
        assert "state before its action" in system
        assert "no clear contradiction" in system
        assert "strict, conservative" not in system


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [300.0, 240.0])
async def test_doubao_finished_assertion_http_timeout_matches_existing_setting(monkeypatch, timeout):
    settings = Settings(_env_file=None, assertion_timeout_sec=timeout)
    monkeypatch.setattr("ai_phone.shared.llm.assistants.doubao.get_settings", lambda: settings)
    captured = {}

    async def post(**kwargs):
        captured.update(kwargs)
        return "PASS: ok"

    assistant = DoubaoAssistant()
    monkeypatch.setattr(assistant, "_post", post)
    await assistant.verify_finished(
        prompt="原断言内容", prev_before_bytes=b"before", final_bytes=b"final", thinking=True,
    )
    assert captured["timeout"] == timeout
    assert captured["thinking"] is True
    assert captured["scene"] == "断言系统"


def test_finished_assertion_timeout_default_is_five_minutes():
    assert Settings.model_fields["assertion_timeout_sec"].default == 300.0
    defaults = Path(__file__).resolve().parents[1] / ".env.defaults"
    assert Settings(_env_file=defaults).assertion_timeout_sec == 300.0


@pytest.mark.parametrize("text,expected", [
    ("PASS: 完成", ("PASS", "完成")),
    ("FAIL：必需操作被跳过", ("FAIL", "必需操作被跳过")),
    ("FAIL: 账号错误\n附加内容", ("FAIL", "账号错误")),
    ("看起来 PASS", None),
    ("UNSURE: 缺少截图", None),
])
def test_verdict_parser_preserves_explicit_failure_with_fullwidth_colon(text, expected):
    from ai_phone.shared.llm.assertion_policy import parse_finished_verdict
    assert parse_finished_verdict(text) == expected


def test_cache_parser_preserves_fullwidth_failure():
    from ai_phone.agent.trajectory_cache.assertion import parse_cache_assertion_response
    result = parse_cache_assertion_response("FAIL：清数据操作实际被跳过")
    assert result.verdict == "FAIL"
    assert result.reason == "清数据操作实际被跳过"
