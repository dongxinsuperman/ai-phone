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
        thought="已经返回列表页",
        finish_msg="已显示列表页",
        has_prev=True,
    )

    assert "【最近动作历史】必须阅读" in prompt
    assert "Runtime 动作记录" in prompt
    assert "模型当时判断" in prompt
    assert "不证明 UI 业务结果" in prompt
    assert "不得单独作证" in prompt
    assert "证据权限与冲突规则以 System 为唯一准则" in prompt
    assert "你只验「预期结果」，不验前置条件、不验操作过程、不验历史顺序" not in prompt
    assert "step   1 Runtime 动作记录（Runtime 调用完成且无异常" in prompt
    assert "模型当时判断（主 VLM 自述；不得单独作证）:第 1 步完整思考" in prompt


def test_structured_assertion_does_not_treat_identical_images_as_automatic_fail() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="状态已变化",
        finish_msg="完成",
        has_prev=True,
    )

    assert "两图相同本身不能直接判 FAIL" in prompt
    assert "不需要主 VLM 先声称变化已经发生" in prompt
    assert "动作历史或主 VLM 明确声称变化已经发生" not in prompt
    assert "等待、截图保存、保持当前状态" in prompt
    assert "双图只辅助判断最后一个动作" in prompt


def test_single_image_assertion_still_requires_action_history() -> None:
    prompt = _runner(structured=True)._build_finished_assertion_prompt(
        thought="完成",
        finish_msg="完成",
        has_prev=False,
    )

    assert "请综合该图、动作历史与主 VLM 最后说明判断" in prompt
    assert "【最近动作历史】必须阅读" in prompt


def test_freeform_template_uses_history_without_expanding_order_checks() -> None:
    prompt = _runner(structured=False)._build_finished_assertion_prompt(
        thought="已经返回",
        finish_msg="完成",
        has_prev=True,
    )

    assert "验收范围保持为用户最后一个 action 步骤对应的结果" in prompt
    assert "不能把历史自动扩展成逐步验收清单" in prompt
    assert "不单独检查前面的动作是否执行过，也不检查其顺序" in prompt
    assert "只有最后动作/最终状态要求与有效证据明确矛盾" in prompt


def test_default_max_length_history_is_present_without_per_entry_truncation() -> None:
    prompt = _runner(structured=True, steps=100)._build_finished_assertion_prompt(
        thought="完成",
        finish_msg="完成",
        has_prev=False,
    )

    assert "step   1 Runtime 动作记录" in prompt
    assert "step 100 Runtime 动作记录" in prompt
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

    history = prompt.split("【最近动作历史】", 1)[1].split(
        "【主VLM最后思考】", 1
    )[0]
    assert "finished(content='成功')" not in history
    assert "step   1 Runtime 动作记录" in history


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
    assert "Runtime 记录" in prompt
    assert "不单独证明 UI 产生了预期业务结果" in prompt
    assert "不能推翻截图里的直接可见事实" in prompt
    assert "两图相同本身不能直接判 FAIL" in prompt


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

    assert "只验用户最后一个 action 步骤对应的结果" in prompt
    assert "不能把摘要自动扩展成逐步验收清单" in prompt
    assert "不单独检查前面的动作是否执行过，也不检查其顺序" in prompt


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("has_prev", [False, True])
def test_v3_assertion_uses_only_current_evidence_and_describes_real_image_span(structured, has_prev):
    from copy import deepcopy
    from ai_phone.agent.runner.vlm_loop import STRUCTURED_ASSERTION_TWO_LAYER_BLOCK

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
        assert STRUCTURED_ASSERTION_TWO_LAYER_BLOCK in prompt  # 首跑核心规则仍为同一原文。
    else:
        assert "不能把摘要自动扩展成逐步验收清单" in prompt
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
        assert "结果导向，不默认挑错" in system
        assert "不同证据负责不同事实，不做简单的全局优先级排序" in system
        assert "Runtime 动作记录" in system
        assert "模型当时判断" in system
        assert "不能与模型自述互相印证" in system
        assert "严格保守" not in system
    else:
        assert system == FINISHED_ASSERTION_SYSTEM_EN
        assert "Be result-oriented" in system
        assert "do not apply one global ranking" in system
        assert "Runtime action record" in system
        assert "model judgment at the time" in system
        assert "must not self-corroborate" in system
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
