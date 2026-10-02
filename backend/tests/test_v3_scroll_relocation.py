"""V3 scrolls locate current regions; source pixels are provenance, not replay inputs."""
from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image

from ai_phone.config import Settings
from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.trajectory_cache import v3_replay as module
from ai_phone.agent.trajectory_cache.archive import _v3_plan_cleaner_rules
from ai_phone.agent.trajectory_cache.replay import ReplayActionDispatcher, ReplayActionError
from ai_phone.shared.actions import ParsedAction


def frame(size, color="white"):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, "JPEG")
    return stream.getvalue()


def cached_scroll(direction="down", **extra):
    return {
        "index": 1, "action_id": "scroll-1", "type": "scroll",
        "direction": direction, "amount": 3, "center": {"x": 360, "y": 640},
        "plan_intent": "滑动左侧设置列表", "raw": "首跑原始动作，仅留档", **extra,
    }


class Driver:
    def __init__(self, size):
        self.size = size
        self.calls = []

    def window_size(self):
        return self.size

    def scroll(self, direction, center=None, amount=1):
        self.calls.append((direction, center, amount))


@pytest.fixture
def make_runner(monkeypatch):
    settings = Settings(_env_file=None, trajectory_cache_observe_delay_ms=0)
    monkeypatch.setattr(module, "get_settings", lambda: settings)

    def create(actions, *, size=(1080, 2400), image_size=(540, 1200), outputs=None,
               coord_space="normalized", rescue=None):
        driver = Driver(size)
        locator = module.V3PlanLocator(settings=settings)
        monkeypatch.setattr(locator, "is_configured", lambda: True)
        backend = "doubao_responses" if coord_space == "normalized" else "openai_compatible"
        monkeypatch.setattr(locator, "_config", lambda: (
            backend, "https://unit.invalid", "unit-key", "unit-model", 300,
        ))
        calls = []
        responses = iter(outputs or ["<point>250 400</point>"] * len(actions))

        async def chat(*, prompt, image_bytes):
            calls.append({"prompt": prompt, "image": image_bytes})
            return next(responses)

        monkeypatch.setattr(locator, "_chat_single_image", chat)
        runner = module.V3ReplayRunner(
            driver=driver, trajectory={"actions": deepcopy(actions)}, locator=locator,
            rescue_verifier=rescue, goal="在设置列表中向下浏览",
        )
        screenshot = frame(image_size)

        async def stable(*args, **kwargs):
            return screenshot

        async def observe(*args, **kwargs):
            pass

        monkeypatch.setattr(runner, "_wait_stable_for_step", stable)
        monkeypatch.setattr(runner, "_screenshot_jpeg", stable)
        monkeypatch.setattr(runner, "_observe_after_action", observe)
        return runner, driver, calls, screenshot

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
@pytest.mark.parametrize("with_old_center", [True, False])
@pytest.mark.parametrize("size", [(720, 1280), (1080, 2400)])
async def test_scroll_relocates_on_current_device_without_changing_direction_or_amount(
    make_runner, direction, with_old_center, size,
):
    source = cached_scroll(direction)
    if not with_old_center:
        source.pop("center")
    original = deepcopy(source)
    runner, driver, calls, screenshot = make_runner([source], size=size)

    result = await runner.run()

    expected_center = (round(size[0] * .25), round(size[1] * .4))
    assert result.success and result.actions_executed == 1
    assert driver.calls == [(direction, expected_center, 3)]
    assert calls[0]["image"] == screenshot
    assert "滑动左侧设置列表" in calls[0]["prompt"]
    assert f"缓存浏览方向：{direction}" in calls[0]["prompt"]
    assert "360" not in calls[0]["prompt"] and "640" not in calls[0]["prompt"]
    actual = runner.execution_history[0]["action"]
    assert actual["center"] == {"x": expected_center[0], "y": expected_center[1]}
    assert actual["direction"] == direction and actual["amount"] == 3
    assert runner.trajectory["actions"] == [original] and source == original


@pytest.mark.asyncio
async def test_absolute_locator_coordinates_use_current_image_size(make_runner):
    runner, driver, calls, _ = make_runner(
        [cached_scroll()], outputs=["<point>100 300</point>"], coord_space="absolute",
        size=(1080, 2400), image_size=(540, 1200),
    )
    result = await runner.run()
    assert result.success
    assert driver.calls == [("down", (200, 600), 3)]
    assert "截图实际像素坐标" in calls[0]["prompt"]


@pytest.mark.asyncio
async def test_same_center_is_valid_for_consecutive_scrolls(make_runner):
    actions = [cached_scroll("down"), cached_scroll(
        "up", index=2, action_id="scroll-2", plan_intent="滑动左侧列表返回顶部",
    )]
    runner, driver, calls, _ = make_runner(actions)
    result = await runner.run()
    assert result.success and len(calls) == 2
    assert driver.calls == [("down", (270, 960), 3), ("up", (270, 960), 3)]
    assert runner._last_locator_point is None


@pytest.mark.asyncio
async def test_relocated_center_reaches_existing_native_swipe_generation(make_runner, monkeypatch):
    runner, driver, _, _ = make_runner([cached_scroll(amount=1)])
    swipes = []
    monkeypatch.setattr(driver, "swipe", lambda *args, **kwargs: swipes.append((args, kwargs)), raising=False)
    monkeypatch.setattr(driver, "scroll", lambda *args: BaseDriver.scroll(driver, *args))
    result = await runner.run()
    assert result.success
    # 当前定位中心(270,960)，使用未修改的驱动手势生成逻辑，而非旧中心(360,640)。
    assert swipes == [((270, 1122, 270, 798), {"duration_ms": 400})]


@pytest.mark.asyncio
async def test_each_scroll_uses_its_own_current_frame_and_center(make_runner, monkeypatch):
    actions = [cached_scroll(), cached_scroll(index=2, action_id="scroll-2")]
    runner, driver, calls, first = make_runner(
        actions, outputs=["<point>250 400</point>", "<point>600 700</point>"],
    )
    second = frame((540, 1200), "green")

    async def stable(index, **kwargs):
        return first if index == 1 else second

    monkeypatch.setattr(runner, "_wait_stable_for_step", stable)
    result = await runner.run()
    assert result.success
    assert [c["image"] for c in calls] == [first, second]
    assert driver.calls == [("down", (270, 960), 3), ("down", (648, 1680), 3)]


class Rescue:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def is_configured(self):
        return True

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


@pytest.mark.asyncio
async def test_scroll_miss_then_wait_relocates_using_updated_screenshot(make_runner, monkeypatch):
    rescue = Rescue(module.V3RescueDecision(verdict="WAIT", reason="列表加载中", wait_ms=100))
    runner, driver, calls, screenshot = make_runner(
        [cached_scroll()], outputs=["无", "<point>300 600</point>"], rescue=rescue,
    )
    updated = frame((540, 1200), "blue")

    async def current(*args, **kwargs):
        return updated

    async def stable(*args, phase="", **kwargs):
        return updated if phase == "辅助等待后" else screenshot

    async def no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(runner, "_wait_stable", current)
    monkeypatch.setattr(runner, "_wait_stable_for_step", stable)
    monkeypatch.setattr(runner, "_screenshot_jpeg", current)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    result = await runner.run()
    assert result.success
    assert [c["image"] for c in calls] == [screenshot, updated]
    assert len(rescue.calls) == 1
    assert driver.calls == [("down", (324, 1440), 3)]
    assert runner.execution_history[-1]["action"]["center"] == {"x": 324, "y": 1440}


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["无", "<point>0 0</point>", "Action: scroll(direction='down')"])
async def test_scroll_location_failure_never_executes_old_center(make_runner, output):
    rescue = Rescue(module.V3RescueDecision(verdict="GIVE_UP", reason="当前无目标列表"))
    runner, driver, calls, _ = make_runner([cached_scroll()], outputs=[output], rescue=rescue)
    result = await runner.run()
    assert not result.success and result.restart_required
    assert driver.calls == [] and len(calls) == 1
    assert len(rescue.calls) == 1


@pytest.mark.asyncio
async def test_scroll_miss_without_rescue_does_not_use_historical_or_default_center(make_runner):
    runner, driver, _, _ = make_runner([cached_scroll()], outputs=["无"])
    runner.rescue_verifier = None
    result = await runner.run()
    assert not result.success
    assert driver.calls == []


def test_non_locator_scroll_is_rejected_and_missing_point_cannot_fall_back():
    with pytest.raises(ReplayActionError, match="必须先定位"):
        module._non_locator_action(cached_scroll())
    with pytest.raises(module.V3LocatorMiss, match="缺少当前滑动中心"):
        module._replay_action_from_parsed(
            ParsedAction(action="scroll"), source_action=cached_scroll(),
            image_size=None, window_size=(1080, 2400),
        )


def test_scroll_center_does_not_poison_click_duplicate_check():
    runner = module.V3ReplayRunner(driver=Driver((1080, 2400)), trajectory={"actions": []})
    runner._validate_located_action(
        cached_scroll(), {"type": "scroll", "center": {"x": 300, "y": 400}},
        window_size=(1080, 2400),
    )
    runner._validate_located_action(
        {"type": "click", "plan_intent": "点击列表条目"},
        {"type": "click", "point": {"x": 300, "y": 400}}, window_size=(1080, 2400),
    )


def test_cleaning_preserves_scroll_region_without_inventing_coordinates():
    rules = _v3_plan_cleaner_rules()
    assert "保留 thought 已明确的滚动对象/区域" in rules
    assert "未明确时不要凭空添加区域" in rules
    assert "不把首跑中心坐标改写成固定位置或百分比" in rules


@pytest.mark.asyncio
async def test_shared_dispatcher_still_executes_v1_v2_stored_centers():
    driver = Driver((1080, 2400))
    await ReplayActionDispatcher(driver).execute(cached_scroll("left"))
    assert driver.calls == [("left", (360, 640), 3)]
