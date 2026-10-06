import pytest

from ai_phone.shared import actions as A
from ai_phone.shared.prompt import build_system_prompt
from ai_phone.shared.seed_gui_actions import (
    ACTION_SCHEMAS,
    extract_thought,
    parse_actions,
    schemas_prompt_text,
)


def test_seed_xml_preserves_thought_and_maps_action() -> None:
    raw = """Thought: 子步骤1未满足，依据是目标仍在下方。
<seed:tool_call><function name="scroll"><parameter name="point" string="true"><point>500 800</point></parameter><parameter name="direction" string="true">down</parameter></function></seed:tool_call>"""
    parsed = parse_actions(raw)
    assert extract_thought(raw).startswith("子步骤1未满足")
    assert len(parsed) == 1
    assert parsed[0].action == A.ACTION_SCROLL
    assert parsed[0].point == [500, 800]
    assert parsed[0].direction == "down"
    assert parsed[0].raw == "scroll(point='<point>500 800</point>', direction='down', scroll_type='singleAction')"


def test_seed_xml_preserves_model_scroll_direction_for_next_frame_feedback() -> None:
    raw = """Thought: 目标卡片在当前视野下方，手指向上滑动以显示下方更多内容。
<seed:tool_call><function name="scroll"><parameter name="point" string="true"><point>620 600</point></parameter><parameter name="direction" string="true">up</parameter></function></seed:tool_call>"""
    parsed = parse_actions(raw)
    assert parsed[0].direction == "up"
    assert parsed[0].raw == "scroll(point='<point>620 600</point>', direction='up', scroll_type='singleAction')"
    assert parsed[0].extra == {}


def test_seed_xml_never_rewrites_drag_endpoints() -> None:
    cases = (
        (
            "下一步显示列表下方更多内容。",
            [500, 250],
            [500, 750],
        ),
        (
            "下一步返回列表顶部。",
            [620, 850],
            [620, 300],
        ),
        (
            "把当前卡片拖到下方区域完成排序。",
            [500, 250],
            [500, 750],
        ),
        (
            "查看列表下方更多内容。",
            [800, 500],
            [200, 500],
        ),
        (
            "返回列表顶部。",
            [500, 500],
            [500, 550],
        ),
    )
    for thought, start, end in cases:
        raw = f"""Thought: {thought}
<seed:tool_call><function name="drag"><parameter name="start_point" string="true"><point>{start[0]} {start[1]}</point></parameter><parameter name="end_point" string="true"><point>{end[0]} {end[1]}</point></parameter></function></seed:tool_call>"""
        parsed = parse_actions(raw)[0]
        assert parsed.start_point == start, thought
        assert parsed.end_point == end, thought
        assert parsed.extra == {}, thought


def test_seed_xml_supports_aiphone_extensions_and_multiple_functions() -> None:
    raw = """判断完成。
<seed:tool_call><function name="open_app"><parameter name="app_name" string="true">洋葱学园</parameter></function><function name="finished"><parameter name="content" string="true">完成</parameter></function></seed:tool_call>"""
    parsed = parse_actions(raw)
    assert extract_thought(raw) == "判断完成。"
    assert [item.action for item in parsed] == [A.ACTION_OPEN_APP, A.ACTION_FINISHED]
    assert parsed[0].name == "洋葱学园"
    assert parsed[1].content == "完成"


def test_seed_xml_without_literal_thought_prefix_keeps_decision_text() -> None:
    raw = """子步骤2『打开目标』 → 当前截图：[未满足]，依据：按钮仍可见。
<seed:tool_call><function name="click"><parameter name="point" string="true"><point>320 640</point></parameter></function></seed:tool_call>"""
    parsed = parse_actions(raw)
    assert extract_thought(raw).startswith("子步骤2")
    assert parsed[0].to_dict() == {"action": "click", "point": [320, 640]}


def test_seed_xml_rejects_missing_required_parameters() -> None:
    raw = """Thought: 继续滚动。
<seed:tool_call><function name="scroll"><parameter name="direction" string="true">down</parameter></function></seed:tool_call>"""
    assert parse_actions(raw) == []


def test_seed_xml_contains_unknown_function_without_crashing() -> None:
    raw = '<seed:tool_call><function name="right_single"><parameter name="point" string="true"><point>1 2</point></parameter></function></seed:tool_call>'
    assert parse_actions(raw) == []


def test_seed_xml_rejects_bad_json_parameter_without_crashing() -> None:
    raw = '<seed:tool_call><function name="scroll"><parameter name="point" string="true"><point>1 2</point></parameter><parameter name="direction" string="false">down</parameter></function></seed:tool_call>'
    assert parse_actions(raw) == []


def test_seed_xml_rejects_empty_terminal_and_type_content() -> None:
    for name in ("type", "finished", "assert_fail"):
        raw = f'<seed:tool_call><function name="{name}"><parameter name="content" string="true"></parameter></function></seed:tool_call>'
        assert parse_actions(raw) == []


def test_seed_xml_rejects_invalid_amount_and_boolean_string() -> None:
    amount = '<seed:tool_call><function name="scroll"><parameter name="point" string="true"><point>1 2</point></parameter><parameter name="direction" string="true">down</parameter><parameter name="amount" string="false">0</parameter></function></seed:tool_call>'
    boolean = '<seed:tool_call><function name="take_screenshot"><parameter name="save_to_album" string="true">false</parameter></function></seed:tool_call>'
    assert parse_actions(amount) == []
    assert parse_actions(boolean) == []


def test_seed_xml_prompt_contains_copyable_official_examples() -> None:
    prompt = build_system_prompt("点击目标")
    assert 'string="true|false"' not in prompt
    assert (
        '<function name="click"><parameter name="point" string="true">'
        '<point>500 800</point></parameter></function>'
    ) in prompt
    assert '<parameter name="seconds" string="false">3</parameter>' in prompt
    assert '<function name="wait" seconds="3">' in prompt
    assert "禁止" in prompt


def test_seed_xml_prompt_preserves_action_behavior_rules() -> None:
    prompt = build_system_prompt("测试滚动、等待和截图")
    assert "禁止按手指移动方向填写 direction" in prompt
    assert "scroll 只用于浏览页面或列表" in prompt
    assert "既可拖动具体对象" in prompt
    assert "根据下一帧截图判断结果" in prompt
    assert "禁止不看反馈原样重复" in prompt
    assert "scroll.point 是手指按下的起始位置" in prompt
    assert "1000ms" in prompt and "100ms" in prompt
    assert "每次看图后再决定下一步" in prompt
    assert "不得使用toEdge" in prompt
    assert "一次等待完成，不要拆成多次wait" in prompt
    assert "不要再点击系统截图按钮" in prompt
    assert "long_press 长按约1秒" in prompt


@pytest.mark.parametrize("substeps", [None, "1. 打开应用抽屉\n2. 查看应用列表"])
def test_seed_xml_prompt_separates_system_gestures_from_content_scroll(substeps) -> None:
    prompt = build_system_prompt("从Android桌面打开应用抽屉", substeps_text=substeps)
    assert "scroll 只用于浏览页面或列表" in prompt
    assert "手机桌面、通知面板等系统手势不按内容浏览方向解释" in prompt
    assert "上划打开应用抽屉" in prompt
    assert "下拉通知面板" in prompt
    assert "上划收起通知面板" in prompt
    assert "根据当前截图自主选择起止坐标" in prompt
    assert "不要把手指方向填进 scroll.direction" in prompt
    assert "drag 的起止点按原样执行，不做方向转义" in prompt
    assert "终点 y 小于起点 y 是手指向上" in prompt
    assert "终点 y 大于起点 y 是手指向下" in prompt
    assert "两者不能互相替代" not in prompt


@pytest.mark.parametrize(
    "thought,start,end",
    [
        ("从桌面上划打开应用抽屉。", [500, 800], [500, 300]),
        ("下拉打开通知面板。", [500, 100], [500, 600]),
        ("上划收起通知面板。", [500, 700], [500, 200]),
        ("把卡片向下拖动进行排序。", [500, 250], [500, 750]),
    ],
)
def test_seed_xml_gestures_and_object_drags_preserve_physical_endpoints(thought, start, end) -> None:
    raw = f"""Thought: {thought}
<seed:tool_call><function name="drag"><parameter name="start_point" string="true"><point>{start[0]} {start[1]}</point></parameter><parameter name="end_point" string="true"><point>{end[0]} {end[1]}</point></parameter></function></seed:tool_call>"""
    parsed = parse_actions(raw)
    assert len(parsed) == 1
    assert parsed[0].action == A.ACTION_DRAG
    assert parsed[0].start_point == start
    assert parsed[0].end_point == end
    assert parsed[0].extra == {}


def test_seed_xml_rejects_out_of_range_points_and_waits() -> None:
    bad_point = '<seed:tool_call><function name="click"><parameter name="point" string="true"><point>1001 2</point></parameter></function></seed:tool_call>'
    wait_zero = '<seed:tool_call><function name="wait"><parameter name="seconds" string="false">0</parameter></function></seed:tool_call>'
    wait_too_long = '<seed:tool_call><function name="wait"><parameter name="seconds" string="false">61</parameter></function></seed:tool_call>'
    assert parse_actions(bad_point) == []
    assert parse_actions(wait_zero) == []
    assert parse_actions(wait_too_long) == []


def test_seed_xml_key_event_is_internal_only() -> None:
    assert "key_event" not in {schema["name"] for schema in ACTION_SCHEMAS}
    assert '"name": "key_event"' not in schemas_prompt_text()
    legacy_internal = '<seed:tool_call><function name="key_event"><parameter name="keycode" string="false">66</parameter></function></seed:tool_call>'
    assert parse_actions(legacy_internal) == []
    parsed = parse_actions(legacy_internal, allow_internal_actions=True)
    assert parsed[0].action == A.ACTION_KEY_EVENT
    assert parsed[0].keycode == 66


def test_seed_xml_rejects_function_attribute_shortcut() -> None:
    malformed = '<seed:tool_call><function name="wait" seconds="3"></function></seed:tool_call>'
    assert parse_actions(malformed) == []
