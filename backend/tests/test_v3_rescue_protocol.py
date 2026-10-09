"""Rescue proposals must use the operations actually implemented by V3."""
import json
from types import SimpleNamespace

import pytest

from ai_phone.agent.trajectory_cache import v3_replay as module
from ai_phone.agent.trajectory_cache.replay import ReplayActionError


ACTIONS = [
    {"type": "click", "point": {"x": 400, "y": 500}},
    {"type": "double_tap", "point": {"x": 400, "y": 500}},
    {"type": "long_press", "point": {"x": 400, "y": 500}, "duration_ms": 1200},
    {"type": "drag", "start": {"x": 800, "y": 500}, "end": {"x": 200, "y": 500}, "duration_ms": 650},
    {"type": "wait", "seconds": 3},
    {"type": "press_back"},
    {"type": "press_home"},
]


def response(action):
    return json.dumps({"verdict": "REPAIR_ACTION", "reason": "恢复当前目标", "repair_action": action})


def test_prompt_declares_every_supported_action_and_no_fixed_coordinate_examples():
    prompt = module.build_v3_rescue_prompt(
        goal="原始目标", trajectory={}, action={"type": "long_press", "plan_intent": "长按应用图标"},
        previous_action=None, next_action=None, miss_reason="图标不在当前页", coord_space="normalized",
    )
    assert "移动端滑动/翻页用 drag" in prompt
    assert "不要输出 swipe、scroll、tap" in prompt
    assert "不固定方向、落点或百分比" in prompt
    schemas = module._v3_repair_action_schemas()
    assert {s["properties"]["type"]["const"] for s in schemas} == {a["type"] for a in ACTIONS}
    for schema in schemas:
        assert json.dumps(schema, ensure_ascii=False) in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ACTIONS, ids=lambda a: a["type"])
async def test_every_declared_action_parses_and_materializes_without_changing_motion(action):
    runner = module.V3ReplayRunner(
        driver=SimpleNamespace(window_size=lambda: (1000, 1000)), trajectory={"actions": []},
    )
    decision = module.parse_v3_rescue_response(response(action))
    assert decision.verdict == "REPAIR_ACTION" and not decision.error
    actual = await runner._repair_action_to_abs(
        decision.repair_action, coord_space="normalized", image_size=(1000, 1000),
    )
    assert actual["type"] == action["type"]
    for field, value in action.items():
        assert actual[field] == value


@pytest.mark.parametrize("action", [
    {"type": "swipe", "start": {"x": 800, "y": 500}, "end": {"x": 200, "y": 500}},
    {"type": "scroll", "direction": "left"},
    {"type": "type", "content": "文本"},
    {"type": "key_event", "keycode": 26},
    {"point": {"x": 400, "y": 500}},
    {"type": "click"},
    {"type": "drag", "start": {"x": 800, "y": 500}},
    {"type": "click", "point": {"x": False, "y": 500}},
    {"type": "click", "point": {"x": 0.5, "y": 500}},
    {"type": "click", "point": {"x": float("inf"), "y": 500}},
    {"type": "wait"},
    {"type": "wait", "seconds": "false"},
    {"type": "drag", "start": [800, 500], "end": [200, 500], "duration_ms": 0},
])
def test_invalid_proposals_are_protocol_errors_not_valid_give_up_or_executable_actions(action):
    decision = module.parse_v3_rescue_response(response(action))
    assert decision.verdict == "GIVE_UP"
    assert decision.error == "invalid_repair_action"
    assert decision.repair_action == action  # 提案可追溯，不映射 swipe、不补缺失坐标。


@pytest.mark.asyncio
async def test_direct_execution_boundary_also_rejects_unvalidated_swipe():
    driver = SimpleNamespace(window_size=lambda: pytest.fail("非法提案不应进入设备调用"))
    runner = module.V3ReplayRunner(driver=driver, trajectory={"actions": []})
    with pytest.raises(ReplayActionError, match="unsupported.*swipe"):
        await runner._repair_action_to_abs(
            {"type": "swipe", "start": [800, 500], "end": [200, 500]},
            coord_space="normalized", image_size=None,
        )


def test_legacy_internal_action_name_and_array_points_still_parse():
    decision = module.parse_v3_rescue_response(response({"action": "click", "point": ["400", "500"]}))
    assert decision.verdict == "REPAIR_ACTION" and not decision.error


def test_point_annotation_does_not_change_legacy_integer_coordinate_handling():
    decision = module.parse_v3_rescue_response(response({
        "type": "click", "point": {"x": "400", "y": "500", "label": "按钮"},
    }))
    assert decision.verdict == "REPAIR_ACTION" and not decision.error


@pytest.mark.asyncio
async def test_proposal_log_is_explicitly_not_an_executed_action(monkeypatch):
    runner = module.V3ReplayRunner(driver=object(), trajectory={"actions": []})
    logs = []

    async def log(level, title, content):
        logs.append((title, content))

    monkeypatch.setattr(runner, "_log", log)
    await runner._record_rescue_decision(
        action={"plan_intent": "长按图标"},
        decision=module.parse_v3_rescue_response(response(ACTIONS[3])),
    )
    assert logs[0][0] == "V3局部辅助提案"
    assert "尚未执行" in logs[0][1] and '"start"' in logs[0][1]
    assert runner.execution_history == []


def xml_action(name, params=""):
    return f'<seed:tool_call><function name="{name}">{params}</function></seed:tool_call>'


@pytest.mark.parametrize("name,params,expected", [
    ("click", '<parameter name="point" string="true"><point>828 337</point></parameter>', {"type":"click","point":{"x":828,"y":337}}),
    ("drag", '<parameter name="start_point" string="true"><point>800 500</point></parameter><parameter name="end_point" string="true"><point>200 500</point></parameter>', {"type":"drag","start":{"x":800,"y":500},"end":{"x":200,"y":500}}),
    ("wait", '<parameter name="seconds" string="false">3</parameter>', {"type":"wait","seconds":3}),
    ("press_back", "", {"type":"press_back"}),
])
def test_seed_rescue_uses_native_action_parameters(name, params, expected):
    decision = module.parse_v3_rescue_xml_response(xml_action(name, params))
    assert decision.error == ""
    assert decision.repair_action == expected
    assert decision.coord_space == "normalized"


@pytest.mark.parametrize("name,verdict", [("finished", "CONTINUE_REPLAY"), ("assert_fail", "GIVE_UP")])
def test_seed_terminal_decisions_control_cache_only(name, verdict):
    decision = module.parse_v3_rescue_xml_response(xml_action(name, '<parameter name="content" string="true">说明</parameter>'))
    assert decision.verdict == verdict
    assert decision.error == ""
    assert decision.repair_action is None


@pytest.mark.parametrize("raw", [
    '{"verdict":"REPAIR_ACTION","repair_action":{"type":"click","point":{"x":828,337}}}',
    xml_action("click"),
    xml_action("press_back") + xml_action("press_home"),
    xml_action("type", '<parameter name="content" string="true">不可输入</parameter>'),
])
def test_seed_rescue_rejects_invalid_or_unapproved_actions(raw):
    decision = module.parse_v3_rescue_xml_response(raw)
    assert decision.error == "invalid_seed_xml"
    assert decision.repair_action is None


@pytest.mark.asyncio
async def test_doubao_rescue_requests_xml_and_preserves_context(monkeypatch):
    captured = {}
    verifier = module.V3RescueVerifier()
    monkeypatch.setattr(verifier, "is_configured", lambda: True)
    monkeypatch.setattr(verifier, "_main_vlm_is_overseas_cu", lambda: False)
    monkeypatch.setattr(verifier, "_config", lambda: ("doubao_responses", "unit", "key", "model", 30))
    async def call(**kwargs):
        captured.update(kwargs)
        return xml_action("press_back")
    monkeypatch.setattr(module, "_call_vlm_with_images", call)
    result = await verifier.decide(goal="目标包含输出 schema：仍须完整", trajectory={}, action={"type":"click"}, current_bytes=b"image", function_map_context="完整地图")
    assert result.repair_action == {"type":"press_back"}
    assert "Seed GUI XML" in captured["system"]
    assert "目标包含输出 schema：仍须完整" in captured["prompt"]
    assert "完整地图" in captured["prompt"]
    assert "只输出 JSON" not in captured["prompt"]
    assert "CONTINUE_REPLAY" not in captured["prompt"]
    assert '"verdict"' not in captured["prompt"]


@pytest.mark.asyncio
async def test_v3_popup_gate_continues_past_three_successful_calls():
    calls = []
    async def decide(**kwargs):
        calls.append(kwargs)
        return module.EphemeralGateDecision(verdict=module.GATE_SKIP, reason="当前没有弹窗")
    gate = SimpleNamespace(is_configured=lambda: True, decide=decide)
    runner = module.V3ReplayRunner(driver=object(), trajectory={"actions":[]}, ephemeral_gate_verifier=gate)
    for index in range(20):
        result = await runner._handle_optional_ephemeral(action={"action_id":str(index), "ephemeral_meta":{"classification_source":"v3_batch_semantic"}}, index=index, current_bytes=b"image", next_action=None)
        assert result == {"mode":"skip"}
    assert len(calls) == 20
