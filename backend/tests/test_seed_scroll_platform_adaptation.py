"""Platform timing semantics for Seed scrolls; legacy drag/CU stays unchanged."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.drivers.harmony import HarmonyDriver, _scroll_pointer_points
from ai_phone.agent.drivers.ios import IosDriver
from ai_phone.agent.drivers.ios_simulator_driver import IosSimulatorDriver
from ai_phone.agent.drivers.wda_client import WdaClient, WdaError


@pytest.mark.parametrize("duration", [100, 1000])
def test_wda_scroll_duration_is_on_move_not_press(duration):
    wda = WdaClient.__new__(WdaClient)
    wda._ensure_session = Mock(return_value="scope-session")
    wda._request = Mock()
    wda.timed_swipe(100, 600, 100, 400, duration_ms=duration)
    args = wda._request.call_args.args
    assert args[:2] == ("POST", "/session/scope-session/actions")
    pointer = args[2]["actions"][0]
    assert pointer["parameters"] == {"pointerType": "touch"}
    assert pointer["actions"] == [
        {"type": "pointerMove", "duration": 0, "origin": "viewport", "x": 100.0, "y": 600.0},
        {"type": "pointerDown", "button": 0},
        {"type": "pointerMove", "duration": duration, "origin": "viewport", "x": 100.0, "y": 400.0},
        {"type": "pointerUp", "button": 0},
    ]


@pytest.mark.parametrize("driver_cls", [IosDriver, IosSimulatorDriver])
@pytest.mark.parametrize("duration", [100, 1000])
def test_ios_scroll_scales_coordinates_but_preserves_movement_ms(driver_cls, duration):
    driver = SimpleNamespace(_wda=Mock(), _px_to_pt=lambda x, y: (x/3, y/3))
    driver_cls.swipe_for_scroll(driver, 300, 1800, 300, 1200, duration)
    driver._wda.timed_swipe.assert_called_once_with(100.0, 600.0, 100.0, 400.0, duration_ms=duration)
    driver._wda.swipe.assert_not_called()


@pytest.mark.parametrize("driver_cls", [IosDriver, IosSimulatorDriver])
def test_ios_legacy_swipe_keeps_existing_endpoint(driver_cls):
    driver = SimpleNamespace(_wda=Mock(), _px_to_pt=lambda x, y: (x/3, y/3))
    driver_cls.swipe(driver, 300, 1800, 300, 1200, 400)
    driver._wda.swipe.assert_called_once_with(100.0, 600.0, 100.0, 400.0, duration_s=.4)
    driver._wda.timed_swipe.assert_not_called()


def test_ios_scroll_does_not_fall_back_to_press_duration_on_failure():
    driver = SimpleNamespace(_wda=Mock(), _px_to_pt=lambda x, y: (x/3, y/3))
    driver._wda.timed_swipe.side_effect = WdaError("actions unsupported")
    with pytest.raises(WdaError):
        IosDriver.swipe_for_scroll(driver, 300, 1800, 300, 1200, 1000)
    driver._wda.swipe.assert_not_called()


@pytest.mark.parametrize("end", [(500, 356), (500, 644), (356, 500), (644, 500), (500, 499)])
@pytest.mark.parametrize("duration", [100, 1000, 123])
def test_harmony_timed_points_keep_exact_end_and_total_duration(end, duration):
    points = _scroll_pointer_points(500, 500, *end, duration)
    decoded = [(p["x"] % 65536, p["y"]) for p in points]
    intervals = [p["x"] // 65536 for p in points]
    assert decoded[0] == (500, 500)
    assert decoded[-1] == end
    assert sum(intervals) == duration
    assert intervals[0] == 0 and intervals[-1] > 0
    assert all(0 <= ms <= 50 for ms in intervals)


def test_harmony_scroll_does_not_use_minimum_speed_swipe():
    client = Mock()
    client.invoke.side_effect = lambda api, **kw: SimpleNamespace(result="matrix" if api == "PointerMatrix.create" else True)
    raw = SimpleNamespace(_client=client, swipe=Mock())
    driver = SimpleNamespace(_raw=raw, _call_with_reconnect=lambda f: f())
    HarmonyDriver.swipe_for_scroll(driver, 500, 500, 500, 356, 1000)
    calls = client.invoke.call_args_list
    assert calls[0].args == ("PointerMatrix.create",)
    assert calls[-1].args == ("Driver.injectMultiPointerAction",)
    raw.swipe.assert_not_called()
    assert calls[-1].kwargs["args"] == ["matrix", 40000]
    points = [call.kwargs["args"][2] for call in calls[1:-1]]
    assert sum(p["x"] // 65536 for p in points) == 1000
    assert points[-1]["x"] % 65536 == 500 and points[-1]["y"] == 356


@pytest.mark.parametrize("duration", [0, -1, True, 1.5])
def test_timed_scroll_rejects_invalid_duration(duration):
    with pytest.raises(ValueError): _scroll_pointer_points(1, 1, 2, 2, duration)
    wda = WdaClient.__new__(WdaClient)
    with pytest.raises(ValueError): wda.timed_swipe(1, 1, 2, 2, duration)


@pytest.mark.parametrize("end,duration", [((500, 356), 50000), ((500, 356), 1)])
def test_harmony_scroll_rejects_unrepresentable_timing(end, duration):
    with pytest.raises(ValueError): _scroll_pointer_points(500, 500, *end, duration)


def test_harmony_scroll_injection_false_is_not_reported_as_success():
    client = Mock()
    client.invoke.side_effect = lambda api, **kw: SimpleNamespace(result="matrix" if api == "PointerMatrix.create" else False)
    driver = SimpleNamespace(_raw=SimpleNamespace(_client=client), _call_with_reconnect=lambda f: f())
    with pytest.raises(RuntimeError, match="not acknowledged"):
        HarmonyDriver.swipe_for_scroll(driver, 500, 500, 500, 356, 1000)


def test_seed_scroll_uses_platform_hook_but_cu_uses_legacy_swipe(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    driver = SimpleNamespace(window_size=lambda:(720,1280), swipe=Mock(), swipe_for_scroll=Mock())
    BaseDriver.scroll_seed(driver, "down", (360, 1024))
    driver.swipe_for_scroll.assert_called_once_with(360, 1024, 360, 256, duration_ms=1000)
    driver.swipe.assert_not_called()
    BaseDriver.scroll(driver, "down", (360, 1024), 1)
    driver.swipe.assert_called_once_with(360, 1132, 360, 916, duration_ms=400)
