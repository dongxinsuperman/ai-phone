"""Action metadata contracts; no external models, devices or database writes."""
from copy import deepcopy
import io
import json
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_phone.config import Settings
from ai_phone.shared import actions as A
from ai_phone.shared.action_summary import extract_action_summaries, summaries_for_actions
from ai_phone.shared.seed_gui_actions import parse_actions, schemas_prompt_text
from ai_phone.shared.llm.prompts import build_system_prompt_for_backend
from ai_phone.shared.llm.main.claude_cu import ClaudeComputerUseClient, _parse_claude_response
from ai_phone.shared.llm.main.gpt_cu import GPTComputerUseClient, _parse_gpt_response
from ai_phone.agent.runner.events import EVT_ACTION, EVT_THOUGHT, make_event
from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.agent.trajectory_cache.recorder import TrajectoryRecorder
from ai_phone.agent.trajectory_cache import archive


def xml(summary="", name="click", params=None):
    params = params or '<parameter name="point" string="true"><point>805 75</point></parameter>'
    return f'<seed:tool_call><function name="{name}">{params}{summary}</function></seed:tool_call>'


@pytest.mark.parametrize("parameter,expected", [
    ('<parameter name="action_summary" string="true">点击返回按钮</parameter>', "点击返回按钮"),
    ('<parameter name="action_summary" string="true">点击 A &amp; B &lt;标签&gt;</parameter>', "点击 A & B <标签>"),
    ('<parameter name="action_summary" string="true">  </parameter>', None),
    ('<parameter name="action_summary" string="false">broken json</parameter>', None),
    ('<parameter name="action_summary" string="false">42</parameter>', None),
    ('<parameter name="action_summary" string="true">' + 'x' * 301 + '</parameter>', None),
    ('<parameter name="action_summary" string="true">one</parameter>' * 2, None),
    ('', None),
])
def test_seed_metadata_cannot_change_or_invalidate_legal_action(parameter, expected):
    baseline = parse_actions(xml())[0]
    parsed = parse_actions(xml(parameter))[0]
    assert parsed.action_summary == expected
    assert parsed.raw == baseline.raw
    result = parsed.to_dict()
    result.pop("action_summary", None)
    assert result == baseline.to_dict()


def test_seed_chain_binds_summary_to_each_function():
    raw = xml('<parameter name="action_summary" string="true">第一下</parameter>')
    raw += xml('<parameter name="action_summary" string="true">第二下</parameter>')
    assert [a.action_summary for a in parse_actions(raw)] == ["第一下", "第二下"]


def test_main_prompts_request_metadata_without_changing_recovery_schema():
    original = schemas_prompt_text()
    for backend in ["doubao_responses", "claude_cu", "gpt_cu"]:
        for substeps in [None, "1. 点击返回按钮"]:
            prompt = build_system_prompt_for_backend("返回", backend=backend, substeps_text=substeps)
            assert ("action_summary" if backend == "doubao_responses" else "ACTION_SUMMARY:") in prompt
    assert "action_summary" not in schemas_prompt_text({"click", "wait"})
    assert schemas_prompt_text() == original


def metadata_line(native="click", summary="点击返回按钮"):
    return "ACTION_SUMMARY: " + json.dumps({
        "computer": [{"action": native, "summary": summary}],
        "platform": [{"action": "open_app", "summary": "打开测试应用"}],
    }, ensure_ascii=False)


def native_response(backend, metadata):
    # Native click precedes platform action in the response. Existing adapter
    # ordering still puts the platform action first; metadata must follow its action.
    text = "原Thought\n" + metadata + "\nPLATFORM_ACTION: open_app(app_name='demo')"
    if backend == "claude":
        return {"id": "reply", "content": [
            {"type": "tool_use", "id": "call-1", "name": "computer",
             "input": {"action": "left_click", "coordinate": [805, 75]}},
            {"type": "text", "text": text},
        ], "usage": {}}
    return {"id": "reply", "output": [
        {"type": "computer_call", "call_id": "call-1", "pending_safety_checks": [],
         "action": {"type": "click", "x": 805, "y": 75, "button": "left"}},
        {"type": "message", "content": [{"type": "output_text", "text": text}]},
    ], "usage": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude", "gpt"])
@pytest.mark.parametrize("valid", [True, False])
async def test_native_clients_preserve_actions_history_and_ack(monkeypatch, backend, valid):
    cls = ClaudeComputerUseClient if backend == "claude" else GPTComputerUseClient
    name = "left_click" if backend == "claude" else "click"
    response = native_response(backend, metadata_line(name) if valid else "ACTION_SUMMARY: {broken}")
    original = deepcopy(response)
    client = cls("test", api_url="https://unit.invalid", api_key="test", model="test")
    requests = []

    async def post(payload, *args, **kwargs):
        requests.append(deepcopy(payload))
        return deepcopy(response)

    monkeypatch.setattr(client, "_post_with_retry", post)
    image = io.BytesIO()
    Image.new("RGB", (1000, 1000)).save(image, "JPEG")
    decision = await client.decide(image.getvalue())
    assert [a.action for a in decision.parsed_actions] == ["open_app", "click"]
    assert [a.action_summary for a in decision.parsed_actions] == (
        ["打开测试应用", "点击返回按钮"] if valid else [None, None]
    )
    assert decision.parsed_actions[1].point == [805, 75]
    assert "ACTION_SUMMARY" not in decision.thought and "原Thought" in decision.thought
    assert response == original
    assert "action_summary" not in json.dumps(requests[0].get("tools", []))
    await client.decide(image.getvalue())
    second = json.dumps(requests[1], ensure_ascii=False)
    assert ("tool_result" if backend == "claude" else "computer_call_output") in second
    assert "call-1" in second
    assert "_action_summary" not in second


@pytest.mark.parametrize("parser,backend", [(_parse_claude_response, "claude"), (_parse_gpt_response, "gpt")])
def test_summary_is_not_a_terminal_declaration(parser, backend):
    name = "left_click" if backend == "claude" else "click"
    data = native_response(backend, metadata_line(name, "点击写有 FINISHED: 的按钮"))
    assert parser(data)[3] is None


@pytest.mark.parametrize("line", [
    'ACTION_SUMMARY: {"computer":[]}',
    'ACTION_SUMMARY: {"computer":[{"action":"type","summary":"wrong"}]}',
    'ACTION_SUMMARY: {"computer":[],"computer":[]}',
    'ACTION_SUMMARY: {}\nACTION_SUMMARY: {}',
])
def test_unmatched_metadata_never_guesses_action_binding(line):
    text, data = extract_action_summaries("original\n" + line)
    assert summaries_for_actions(data, "computer", ["click"]) == [None]
    assert "original" in text and "ACTION_SUMMARY" not in text


@pytest.mark.asyncio
async def test_actual_app_and_wait_corrections_preserve_summary(monkeypatch):
    rec = TrajectoryRecorder("unit")
    runner = VLMRunner.__new__(VLMRunner)
    runner.run_id = "unit"
    runner._current_step = 1
    runner._log = AsyncMock()

    async def emit(event):
        rec.feed(event)

    runner._emit_event = emit
    rec.feed(make_event(EVT_ACTION, "unit", step=1, actions=[{
        "action": "open_app", "name": "demo", "action_summary": "打开测试应用",
    }]))
    await runner._emit_cache_app_action("open_app", "com.example.demo")
    assert rec.steps()[0]["actions"] == [{
        "action": "open_app", "name": "com.example.demo", "action_summary": "打开测试应用",
    }]
    runner._decide_wait_seconds = lambda p: {"seconds": 5, "source": "explicit"}
    monkeypatch.setattr("ai_phone.agent.runner.vlm_loop.asyncio.sleep", AsyncMock())
    await runner._execute_action(A.ParsedAction(
        action="wait", seconds=5, action_summary="等待页面加载",
    ), step=2, settle_ms=0)
    assert rec.steps()[1]["actions"][0]["action_summary"] == "等待页面加载"
    rec.feed(make_event(EVT_ACTION, "unit", step=3, actions=[{
        "action": "wait", "seconds": 60, "action_summary": "等待60秒",
    }]))
    runner._decide_wait_seconds = lambda p: {
        "seconds": 10, "requested_seconds": 60, "source": "explicit", "clipped": True,
    }
    await runner._execute_action(A.ParsedAction(
        action="wait", seconds=60, action_summary="等待60秒",
    ), step=3, settle_ms=0)
    assert rec.steps()[2]["actions"][0]["seconds"] == 10
    assert rec.steps()[2]["actions"][0].get("action_summary") is None
    # A different source action must not inherit metadata just because step matches.
    rec.feed(make_event(EVT_ACTION, "unit", step=1, actions=[{"action": "close_app"}]))
    assert "action_summary" not in rec.steps()[0]["actions"][0]


@pytest.mark.asyncio
async def test_v3_only_consumes_summary_and_keeps_old_archive_parameters(monkeypatch):
    monkeypatch.setattr(archive.V3PlanIntentCleaner, "is_configured", lambda self: False)
    monkeypatch.setattr(archive.CacheEphemeralActionClassifier, "is_configured", lambda self: False)
    rec = TrajectoryRecorder("unit")
    rec.feed(make_event(EVT_THOUGHT, "unit", step=1, text="点击 Cancel 按钮"))
    rec.feed(make_event(EVT_ACTION, "unit", step=1, actions=[{
        "action": "click", "point": [805, 75], "action_summary": "点击 Continue 按钮",
    }]))
    args = dict(goal="goal", device_serial="unit", source_run_id="unit", steps=rec.steps(), screen_size=(1000, 1000))
    v3 = await archive.build_v3_archive(**args)
    v1 = await archive.build_v1_archive(**args)
    v2 = await archive.build_v2_archive(**args, upload_image=AsyncMock())
    action = v3["actions"][0]
    assert action["plan_intent"] == "点击 Continue 按钮"
    assert action["thought"] == "点击 Cancel 按钮"  # preserved, not used as cleaner input
    assert archive._v3_action_brief(action) == {"type": "click", "action_summary": "点击 Continue 按钮"}
    assert "action_summary" not in v1["trajectory_json"]["actions"][0]
    assert "action_summary" not in v2["trajectory_json"]["actions"][0]
    assert action["point"] == v1["trajectory_json"]["actions"][0]["point"]


@pytest.mark.asyncio
async def test_cleaner_exclusive_input_survives_repair_and_keeps_popup_classification(monkeypatch):
    settings = Settings(_env_file=None, trajectory_cache_ephemeral_action_enabled=True,
                        trajectory_cache_ephemeral_classify_enabled=True)
    monkeypatch.setattr(archive, "get_settings", lambda: settings)
    monkeypatch.setattr(archive.V3PlanIntentCleaner, "is_configured", lambda self: True)
    actions = [
        {"action_id": "a1", "source_step": 1, "type": "click", "point": {"x": 10, "y": 20},
         "thought": "THOUGHT_MUST_NOT_LEAK", "action_summary": "点击升级提示关闭按钮，清除遮挡", "plan_intent": "点击升级提示关闭按钮"},
        {"action_id": "a2", "source_step": 2, "type": "click", "point": {"x": 30, "y": 40},
         "thought": "LEGACY_FALLBACK", "plan_intent": "点击提交"},
    ]
    original = deepcopy(actions)
    result = {"actions": [{
        "action_id": a["action_id"], "plan_intent": a["plan_intent"], "confidence": .95, "reason": "test",
        "ephemeral": {"role": "optional_ephemeral" if i == 0 else "business_required",
                      "category": "upgrade_popup" if i == 0 else "case_goal_related", "confidence": .95,
                      "skip_if_absent": i == 0, "business_risk": "low", "reason": "test"},
    } for i, a in enumerate(actions)]}
    requests = []

    async def call(**kwargs):
        requests.append(kwargs)
        return '{"actions": []}' if len(requests) == 1 else json.dumps(result)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    payload = {"actions": actions}
    await archive._clean_v3_plan_intents(payload=payload, goal="UNCHANGED_GOAL")
    assert len(requests) == 2
    for request in requests:
        prompt = request["prompt"]
        assert "THOUGHT_MUST_NOT_LEAK" not in prompt
        assert "LEGACY_FALLBACK" in prompt and "UNCHANGED_GOAL" in prompt
        assert "点击升级提示关闭按钮，清除遮挡" in prompt
        assert "瞬态清障标记" in prompt
        assert request["images"] == []
    assert actions[0]["role"] == "optional_ephemeral"
    assert actions[0]["ephemeral_meta"]["skip_if_absent"] is True
    assert actions[1]["role"] == "business_required"
    for after, before in zip(actions, original):
        for key in ["type", "point", "action_id", "thought"]:
            assert after[key] == before[key]
