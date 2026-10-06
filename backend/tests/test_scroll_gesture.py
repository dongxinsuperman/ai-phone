"""Versioned start-point scrolls, with legacy replay isolation."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.trajectory_cache.archive import _actions_from_steps
from ai_phone.agent.trajectory_cache.replay import ReplayActionDispatcher, ReplayActionError
from ai_phone.agent.trajectory_cache.v3_replay import _replay_action_from_parsed, build_v3_locator_prompt
from ai_phone.shared.actions import ParsedAction, parse_action
from ai_phone.shared.scroll_gesture import build_scroll_gesture
from ai_phone.shared.seed_gui_actions import parse_actions


def xml_scroll(extra="", direction="down"):
    return '<seed:tool_call><function name="scroll"><parameter name="point" string="true"><point>500 800</point></parameter><parameter name="direction" string="true">'+direction+'</parameter>'+extra+'</function></seed:tool_call>'


class Driver:
    scroll = BaseDriver.scroll
    scroll_seed = BaseDriver.scroll_seed

    def __init__(self):
        self.swipe = Mock()

    def window_size(self):
        return 720, 1280


def test_normal_from_model_point_has_axis_distance_not_short_edge():
    g = build_scroll_gesture((720, 1280), (360, 1024), "down")
    assert g.start == (360, 1024)
    assert g.end == (360, 256)
    assert g.duration_ms == 1000 and g.repeat == 1


@pytest.mark.parametrize("direction,start,expected", [
    ("down", (360, 1024), (360, 38)),
    ("up", (360, 256), (360, 1241)),
    ("right", (576, 640), (21, 640)),
    ("left", (144, 640), (698, 640)),
])
def test_edge_direction_and_fast_budget(direction, start, expected):
    g = build_scroll_gesture((720, 1280), start, direction, scroll_type="toEdge")
    assert g.start == start and g.end == expected
    assert g.duration_ms == 100 and g.repeat == 10


@pytest.mark.parametrize("size", [(720, 1280), (1280, 720), (1080, 2400)])
@pytest.mark.parametrize("direction", ["down", "up", "left", "right"])
def test_normal_distance_scales_with_direction_axis(size, direction):
    w, h = size
    point = (int(w*.8), int(h*.8)) if direction in ("down", "right") else (int(w*.2), int(h*.2))
    g = build_scroll_gesture(size, point, direction, distance=200)
    travel = abs(g.end[1]-g.start[1]) if direction in ("down", "up") else abs(g.end[0]-g.start[0])
    assert travel == int((h if direction in ("down", "up") else w)*.2)


@pytest.mark.parametrize("direction,point", [("down", (360, 0)), ("up", (360, 1279)), ("right", (0, 640)), ("left", (719, 640))])
def test_boundary_never_reverses_gesture(direction, point):
    g = build_scroll_gesture((720, 1280), point, direction)
    assert g.start == g.end == point


def test_repeat_only_does_not_select_fast_mode():
    g = build_scroll_gesture((720, 1280), (360, 1024), "down", amount=3)
    assert g.duration_ms == 1000 and g.repeat == 3


@pytest.mark.parametrize("parameter", [
    '<parameter name="distance" string="false">0</parameter>',
    '<parameter name="distance" string="false">1001</parameter>',
    '<parameter name="distance" string="false">true</parameter>',
    '<parameter name="distance" string="false">200.5</parameter>',
    '<parameter name="scroll_type" string="true">unknown</parameter>',
    '<parameter name="scroll_type" string="true">toEdge</parameter><parameter name="distance" string="false">600</parameter>',
    '<parameter name="scroll_type" string="true">toEdge</parameter><parameter name="amount" string="false">3</parameter>',
])
def test_seed_rejects_invalid_or_ambiguous_scroll_options(parameter):
    assert parse_actions(xml_scroll(parameter)) == []


def test_seed_default_and_explicit_mode_survive_serialization_and_raw():
    for extra, mode, distance in [("", "singleAction", None), ('<parameter name="distance" string="false">200</parameter>', "singleAction", 200), ('<parameter name="scroll_type" string="true">toEdge</parameter>', "toEdge", None)]:
        p = parse_actions(xml_scroll(extra))[0]
        assert p.scroll_gesture_version == 1 and p.scroll_type == mode
        assert p.scroll_distance == distance
        assert p.to_dict()["scroll_gesture_version"] == 1
        rebuilt = parse_action(p.raw)
        assert rebuilt.scroll_gesture_version == 1
        assert rebuilt.scroll_type == mode and rebuilt.scroll_distance == distance


def test_seed_dsl_is_marked_but_native_cu_actions_remain_unmarked():
    assert parse_action("scroll(point='<point>500 800</point>', direction='down')").scroll_gesture_version == 1
    assert ParsedAction(action="scroll", coord_space="absolute").scroll_gesture_version == 0
    assert "scroll_gesture_version" not in ParsedAction(action="click", point=[500, 500]).to_dict()


@pytest.mark.asyncio
async def test_new_archive_locator_dispatch_preserves_parameters_and_current_start(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    p = parse_actions(xml_scroll('<parameter name="distance" string="false">200</parameter>'))[0]
    source = _actions_from_steps([{"step": 1, "actions": [p.to_dict()]}], screen_size=(720, 1280))[0]
    assert source["point"] == {"x": 360, "y": 1024} and "center" not in source
    original = deepcopy(source)
    located = _replay_action_from_parsed(ParsedAction(action="scroll", point=[250, 800]), source_action=source, image_size=None, window_size=(720, 1280))
    assert located["point"] == {"x": 180, "y": 1024}
    assert located["scroll_distance"] == 200
    d = Driver()
    await ReplayActionDispatcher(d).execute(located)
    d.swipe.assert_called_once_with(180, 1024, 180, 768, duration_ms=1000)
    assert source == original


@pytest.mark.asyncio
async def test_new_fast_archive_dispatch_uses_fast_passes_and_settle(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    p = parse_actions(xml_scroll('<parameter name="scroll_type" string="true">toEdge</parameter>'))[0]
    source = _actions_from_steps([{"step": 1, "actions": [p.to_dict()]}], screen_size=(720, 1280))[0]
    d = Driver()
    await ReplayActionDispatcher(d).execute(source)
    assert d.swipe.call_count == 10
    d.swipe.assert_called_with(360, 1024, 360, 38, duration_ms=100)
    assert sleeps == [1]


@pytest.mark.asyncio
async def test_old_cache_is_rejected_without_any_device_action():
    source = {"type": "scroll", "direction": "down", "amount": 3, "center": {"x": 360, "y": 1024}}
    d = Driver()
    with pytest.raises(ReplayActionError, match="Obsolete"):
        await ReplayActionDispatcher(d).execute(source)
    d.swipe.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_cache_gesture_version_fails_closed():
    d = Driver()
    with pytest.raises(ReplayActionError, match="unsupported scroll cache"):
        await ReplayActionDispatcher(d).execute({"type": "scroll", "scroll_gesture_version": 2})
    d.swipe.assert_not_called()
