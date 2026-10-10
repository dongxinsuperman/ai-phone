"""V3 assertion evidence uses this replay's operations, not the cached plan."""
from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image

from ai_phone.agent.trajectory_cache.assertion import (
    CacheReplayAssertionVerifier, build_cache_assertion_prompt,
)
from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache.v3_replay import (
    V3LocatorMiss, V3ReplayRunner, V3RescueDecision,
)


class Dispatcher:
    last_screenshot_error = None

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def execute(self, action):
        self.calls.append(deepcopy(action))
        if self.fail:
            raise RuntimeError("driver failed")


class Driver:
    def window_size(self):
        return 1000, 2000


class Rescue:
    coord_space = "normalized"

    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def is_configured(self):
        return True

    async def decide(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.decision


@pytest.mark.asyncio
async def test_rescue_request_contains_full_context_and_only_latest_image(monkeypatch):
    from ai_phone.agent.trajectory_cache import v3_replay as module

    goal = "完整 Case 原文\n前置、操作过程和预期结果均保留"
    map_text = "  Map 开头\n" + "场景说明" * 1200 + "\nMap 结尾  "
    history = [{"sequence": 1, "index": 7, "source": "rescue_repair",
                "runtime_status": "completed_without_exception",
                "action": {"type": "drag", "start": {"x": 500, "y": 1500},
                           "end": {"x": 500, "y": 500}}, "reason": "向下找按钮"}]
    original = deepcopy(history)
    captured = {}

    async def call(**kwargs):
        captured.update(kwargs)
        return '{"verdict":"WAIT","reason":"列表加载中","wait_ms":100}'

    verifier = module.V3RescueVerifier(settings=Settings(_env_file=None))
    monkeypatch.setattr(verifier, "is_configured", lambda: True)
    monkeypatch.setattr(verifier, "_config", lambda: (
        "openai_compatible", "https://unit.invalid/chat/completions", "unit-key", "unit-model", 300,
    ))
    monkeypatch.setattr(module, "_call_vlm_with_images", call)
    decision = await verifier.decide(
        goal=goal, trajectory={"run_semantic_text": "历史缓存语义不能替代完整原文"},
        action=action(7), previous_action=action(6), next_action=action(8),
        current_bytes=b"latest-image", miss_reason="第二次仍未找到按钮",
        function_map_context=map_text, rescue_history=history,
    )
    assert decision.verdict == "WAIT"
    assert goal in captured["prompt"]
    assert map_text in captured["prompt"]
    assert "第二次仍未找到按钮" in captured["prompt"]
    assert '"source": "rescue_repair"' in captured["prompt"]
    assert '"y": 1500' in captured["prompt"]
    assert "设备像素" in captured["prompt"]
    assert captured["images"] == [("current_replay", b"latest-image")]
    assert history == original


def test_rescue_prompt_accepts_legacy_call_without_map_or_history():
    from ai_phone.agent.trajectory_cache.v3_replay import build_v3_rescue_prompt

    prompt = build_v3_rescue_prompt(
        goal="原目标", trajectory={}, action=action(), previous_action=None,
        next_action=None, miss_reason="无", coord_space="normalized",
    )
    assert "整体目标：原目标" in prompt
    assert "（未提供）" in prompt
    assert "空列表表示尚未救援" in prompt
    assert "CONTINUE_REPLAY | GIVE_UP" in prompt


def test_v3_rescue_map_respects_existing_disable_switch(monkeypatch):
    from ai_phone.agent.trajectory_cache import v3_replay as module

    settings = Settings(_env_file=None, function_map_context_enabled=False)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    runner = module.V3ReplayRunner(
        driver=Driver(), trajectory={"actions": []}, function_map_context="不应发送的Map",
    )
    assert runner.function_map_context is None


@pytest.mark.parametrize("explicit_limit,expected", [(None, 5), (3, 3), (0, 0)])
def test_v3_rescue_default_is_five_and_explicit_limits_still_win(monkeypatch, explicit_limit, expected):
    from ai_phone.agent.trajectory_cache import v3_replay as module

    overrides = {} if explicit_limit is None else {
        "trajectory_cache_v3_rescue_max_calls_per_replay": explicit_limit,
    }
    settings = Settings(_env_file=None, **overrides)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    runner = module.V3ReplayRunner(driver=Driver(), trajectory={"actions": []})
    assert runner._v3_rescue_max_calls == expected


@pytest.fixture
def make_runner(monkeypatch):
    def create(actions, *, dispatcher=None, rescue=None, goal=None, function_map_context=None):
        stream = BytesIO()
        Image.new("RGB", (64, 128), "white").save(stream, "JPEG")
        frame = stream.getvalue()
        runner = V3ReplayRunner(
            driver=Driver(), trajectory={"actions": deepcopy(actions)},
            dispatcher=dispatcher or Dispatcher(), rescue_verifier=rescue,
            goal=goal, function_map_context=function_map_context,
        )

        async def stable(*args, **kwargs):
            return frame

        async def observe(*args, **kwargs):
            return None

        monkeypatch.setattr(runner, "_wait_stable_for_step", stable)
        monkeypatch.setattr(runner, "_screenshot_jpeg", stable)
        monkeypatch.setattr(runner, "_observe_after_action", observe)
        runner._v3_rescue_max_calls = 3
        return runner

    return create


@pytest.mark.asyncio
async def test_rescue_receives_map_and_previous_repairs_without_cross_action_leak(make_runner, monkeypatch):
    rescue = Rescue(V3RescueDecision(
        verdict="REPAIR_ACTION", reason="向下浏览寻找当前按钮",
        repair_action={"type": "drag", "start": {"x": 500, "y": 750},
                       "end": {"x": 500, "y": 250}},
    ))
    goal = "完整原文：进入列表，选择指定卡片，再打开详情"
    map_text = "新版列表的目标卡片在底部\n" + "业务解释" * 1200 + "\nMap尾部"
    runner = make_runner([action(), action(2)], rescue=rescue, goal=goal,
                         function_map_context=map_text)
    original = deepcopy(runner.trajectory)
    attempts = {}

    async def locate(a, frame):
        index = a["index"]
        attempts[index] = attempts.get(index, 0) + 1
        if attempts[index] <= (2 if index == 1 else 1):
            raise V3LocatorMiss(f"目标{index}在本轮截图中仍不可见")
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    assert (await runner.run()).success
    assert len(rescue.calls) == 3  # 本测试显式限制为 3，仍可按原配额工作。
    first, second, next_target = rescue.calls
    assert all(c["goal"] == goal and c["function_map_context"] == map_text for c in rescue.calls)
    assert first["rescue_history"] == []
    previous_repair = second["rescue_history"][0]
    assert previous_repair["source"] == "rescue_repair"
    assert previous_repair["action"]["type"] == "drag"
    assert previous_repair["action"]["start"] == {"x": 500, "y": 1500}
    assert previous_repair["runtime_status"] == "completed_without_exception"
    assert second["miss_reason"] == "目标1在本轮截图中仍不可见"
    assert next_target["rescue_history"] == []
    assert runner.trajectory == original
    # 传入模型的历史是独立快照，不能随本轮后续操作变化。
    assert len(second["rescue_history"]) == 1


@pytest.mark.asyncio
async def test_five_rescue_calls_are_shared_across_steps_and_never_execute_sixth(make_runner, monkeypatch):
    rescue = Rescue(V3RescueDecision(
        verdict="REPAIR_ACTION", reason="局部修复",
        repair_action={"type": "press_back"},
    ))
    runner = make_runner([action(), action(2)], rescue=rescue)
    runner._v3_rescue_max_calls = Settings(_env_file=None).trajectory_cache_v3_rescue_max_calls_per_replay
    attempts = {}

    async def locate(a, frame):
        index = a["index"]
        attempts[index] = attempts.get(index, 0) + 1
        if index == 1 and attempts[index] > 4:
            return {**a, "point": {"x": 100, "y": 200}}
        raise V3LocatorMiss("仍需修复")

    monkeypatch.setattr(runner, "_locate_action", locate)
    result = await runner.run()
    assert not result.success
    assert "v3_rescue_limit_exceeded limit=5" in result.error
    assert result.restart_required is True
    assert [c["action"]["index"] for c in rescue.calls] == [1] * 4 + [2]
    assert len([a for a in runner.dispatcher.calls if a["type"] == "press_back"]) == 5
    assert result.takeover_required is True
    assert result.actions_executed == 1  # 第一个目标在第4次修复后接回缓存；第二个未完成。


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [("", True), ("timeout", False), ("unknown_verdict", False), ("invalid_seed_xml", False), ("ReadError", False)])
async def test_explicit_rescue_give_up_requests_long_rescue_before_budget_exhaustion(make_runner, monkeypatch, error, expected):
    rescue = Rescue(V3RescueDecision(verdict="GIVE_UP", reason="无法找回目标", error=error))
    runner = make_runner([action()], rescue=rescue)

    async def miss(*args, **kwargs):
        raise V3LocatorMiss("找不到")

    monkeypatch.setattr(runner, "_locate_action", miss)
    result = await runner.run()
    assert not result.success
    assert result.restart_required is expected
    assert result.takeover_required is expected
    assert len(rescue.calls) == 1
    assert runner.dispatcher.calls == []


@pytest.mark.asyncio
async def test_cancelled_rescue_does_not_request_full_restart(make_runner, monkeypatch):
    import asyncio

    class CancelledRescue(Rescue):
        async def decide(self, **kwargs):
            raise asyncio.CancelledError

    runner = make_runner([action()], rescue=CancelledRescue(None))

    async def miss(*args, **kwargs):
        raise V3LocatorMiss("找不到")

    monkeypatch.setattr(runner, "_locate_action", miss)
    with pytest.raises(asyncio.CancelledError):
        await runner.run()


@pytest.mark.parametrize("text", ['{}', '{"verdict":"UNKNOWN"}', 'not json'])
def test_malformed_rescue_output_is_not_an_explicit_restart_decision(text):
    from ai_phone.agent.trajectory_cache.v3_replay import parse_v3_rescue_response

    decision = parse_v3_rescue_response(text)
    assert decision.verdict == "GIVE_UP"
    assert decision.error


@pytest.mark.asyncio
async def test_rescue_receives_previous_wait_when_target_still_missing(make_runner, monkeypatch):
    rescue = Rescue(V3RescueDecision(verdict="WAIT", reason="等待列表加载", wait_ms=100))
    runner = make_runner([action()], rescue=rescue)
    attempts = 0

    async def locate(a, frame):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise V3LocatorMiss("列表仍未加载目标")
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    assert (await runner.run()).success
    first, second = rescue.calls
    assert first["function_map_context"] is None
    assert first["rescue_history"] == []
    wait = second["rescue_history"][0]
    assert wait["action"] == {"type": "wait", "wait_ms": 100}
    assert wait["reason"] == "等待列表加载"
    assert wait["runtime_status"] == "completed_without_exception"


def action(index=1, **extra):
    return {"index": index, "action_id": f"a{index}", "type": "click",
            "point": {"x": 999, "y": 888}, "plan_intent": "点击当前目标", **extra}


@pytest.mark.parametrize("action_type", ["click", "double_tap", "long_press", "drag"])
def test_relocation_preserves_source_parameters_and_only_replaces_coordinates(action_type):
    from ai_phone.agent.trajectory_cache.v3_replay import _replay_action_from_parsed
    from ai_phone.shared.actions import ParsedAction

    source = action(
        type=action_type, interval_ms=150, duration_ms=800,
        start={"x": 1, "y": 2}, end={"x": 3, "y": 4},
    )
    original = deepcopy(source)
    parsed = ParsedAction(
        action=action_type, point=[100, 200], start_point=[100, 200],
        end_point=[300, 400], coord_space="absolute",
    )
    result = _replay_action_from_parsed(
        parsed, source_action=source, image_size=None, window_size=(1000, 2000),
    )
    for key, value in source.items():
        if key not in {"point", "start", "end"}:
            assert result[key] == value
    assert source == original
    if action_type == "drag":
        assert result["start"] == {"x": 100, "y": 200}
        assert result["end"] == {"x": 300, "y": 400}
    else:
        assert result["point"] == {"x": 100, "y": 200}


@pytest.mark.asyncio
async def test_v3_history_records_relocated_action_without_mutating_plan(make_runner, monkeypatch):
    cached = action()
    runner = make_runner([cached])

    async def locate(a, frame):
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    result = await runner.run()
    assert result.success
    row = runner.execution_history[0]
    assert row["action"]["point"] == {"x": 100, "y": 200}
    assert row["action"]["plan_intent"] == "点击当前目标"
    assert row["runtime_status"] == "completed_without_exception"
    assert runner.trajectory["actions"][0] == cached
    row["action"]["point"]["x"] = 0
    assert runner.execution_history[0]["action"]["point"]["x"] == 100


@pytest.mark.asyncio
async def test_v3_type_replays_original_input_without_adding_focus_click(make_runner, monkeypatch):
    runner = make_runner([action(type="type", content="1111", plan_intent="输入验证码")])

    async def locate(a, frame):
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    result = await runner.run()
    assert result.success
    history = runner.execution_history
    assert [r["source"] for r in history] == ["cache"]
    assert [r["action"]["type"] for r in history] == ["type"]
    assert history[0]["action"]["content"] == "1111"
    assert len(runner.dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_v3_wait_keeps_source_seconds_instead_of_capping_at_60(make_runner):
    runner = make_runner([action(type="wait", seconds=120, plan_intent="等待120秒")])
    assert (await runner.run()).success
    assert runner.dispatcher.calls[0]["seconds"] == 120
    assert runner.execution_history[0]["action"]["seconds"] == 120


@pytest.mark.asyncio
async def test_v3_dispatcher_uses_first_run_wait_limit_without_changing_v1_v2(monkeypatch):
    from ai_phone.agent.trajectory_cache import replay as replay_mod
    from ai_phone.agent.trajectory_cache import v3_replay as v3_mod

    settings = Settings(run_max_wait_sec=1800)
    monkeypatch.setattr(v3_mod, "get_settings", lambda: settings)
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(replay_mod.asyncio, "sleep", sleep)
    runner = V3ReplayRunner(driver=Driver(), trajectory={"actions": []})
    await runner.dispatcher.execute({"type": "wait", "seconds": 120})
    await replay_mod.ReplayActionDispatcher(Driver()).execute({"type": "wait", "seconds": 120})
    assert waits == [120, 60]


@pytest.mark.asyncio
async def test_v3_history_records_rescue_repair_before_cached_action(make_runner, monkeypatch):
    runner = make_runner([action()], rescue=Rescue(V3RescueDecision(
        verdict="POPUP_CLOSE", reason="关闭新增弹窗",
        repair_action={"type": "click", "point": {"x": 500, "y": 500}},
    )))
    attempts = 0

    async def locate(a, frame):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise V3LocatorMiss("目标被遮挡")
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    assert (await runner.run()).success
    history = runner.execution_history
    assert [r["source"] for r in history] == ["rescue_repair", "cache"]
    assert history[0]["reason"] == "关闭新增弹窗"
    assert history[0]["action"]["point"] == {"x": 500, "y": 1000}
    assert history[1]["action"]["point"] == {"x": 100, "y": 200}


@pytest.mark.asyncio
async def test_v3_history_marks_skipped_action_as_not_executed(make_runner, monkeypatch):
    runner = make_runner([action(), action(2)], rescue=Rescue(V3RescueDecision(
        verdict="CONTINUE_REPLAY", reason="页面已能承接下一条",
    )))

    async def locate(a, frame):
        if a["index"] == 1:
            raise V3LocatorMiss("本步骤已完成")
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    assert (await runner.run()).success
    history = runner.execution_history
    assert [r["runtime_status"] for r in history] == ["skipped", "completed_without_exception"]
    assert "point" not in history[0]["action"]
    assert len(runner.dispatcher.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_verdict", ["REPAIR_ACTION", "POPUP_CLOSE", "WAIT"])
@pytest.mark.parametrize("has_next", [True, False])
async def test_rescue_continue_hands_latest_frame_to_next_step_and_final_evidence(
    make_runner, monkeypatch, repair_verdict, has_next,
):
    frames = []
    for color in ("white", "black"):
        stream = BytesIO()
        Image.new("RGB", (64, 128), color).save(stream, "JPEG")
        frames.append(stream.getvalue())
    before, repaired = frames
    rescue = Rescue(V3RescueDecision(
        verdict=repair_verdict, reason="关闭遮挡或等待页面更新", wait_ms=100,
        repair_action={"type": "click", "point": {"x": 500, "y": 500}},
    ))
    runner = make_runner([action(), action(2)] if has_next else [action()], rescue=rescue)
    seen, emitted = [], []

    async def stable(*args, **kwargs):
        return before if kwargs.get("phase") == "执行前" else repaired

    async def locate(a, frame):
        seen.append((a["index"], frame))
        if a["index"] == 1:
            if frame == repaired:
                rescue.decision = V3RescueDecision(
                    verdict="CONTINUE_REPLAY", reason="本步骤已完成，继续下一步",
                )
            raise V3LocatorMiss("原按钮不可见")
        assert frame == repaired
        return {**a, "point": {"x": 100, "y": 200}}

    async def emit(index, phase, frame):
        emitted.append((index, phase, frame))

    monkeypatch.setattr(runner, "_wait_stable_for_step", stable)
    monkeypatch.setattr(runner, "_locate_action", locate)
    monkeypatch.setattr(runner, "_emit_screenshot", emit)
    runner.capture_after_each_action = True
    assert (await runner.run()).success
    assert [c["current_bytes"] for c in rescue.calls] == [before, repaired]
    assert (1, "after", repaired) in emitted
    assert (1, "after", before) not in emitted
    if has_next:
        assert seen[-1] == (2, repaired)
        assert (2, "before", repaired) in emitted
    else:
        assert runner._final_after_bytes == repaired


@pytest.mark.asyncio
async def test_v3_history_marks_driver_error_without_claiming_completion(make_runner):
    runner = make_runner([action(type="press_home")], dispatcher=Dispatcher(fail=True))
    assert not (await runner.run()).success
    assert runner.execution_history[0]["runtime_status"] == "execution_error"
    assert "driver failed" in runner.execution_history[0]["error"]
    assert runner.execution_history[0]["runtime_status"] == "execution_error"


@pytest.mark.asyncio
async def test_v3_history_records_rescue_wait(make_runner, monkeypatch):
    runner = make_runner([action()], rescue=Rescue(V3RescueDecision(
        verdict="WAIT", reason="等页面加载", wait_ms=100,
    )))
    attempts = 0

    async def locate(a, frame):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise V3LocatorMiss("加载中")
        return {**a, "point": {"x": 100, "y": 200}}

    monkeypatch.setattr(runner, "_locate_action", locate)
    assert (await runner.run()).success
    assert runner.execution_history[0]["source"] == "rescue_wait"
    assert runner.execution_history[0]["action"]["wait_ms"] == 100


def test_v3_assertion_uses_runtime_coordinates_and_semantics_not_cached_plan():
    prompt = build_cache_assertion_prompt(
        goal="点击当前目标", has_prev=True,
        trajectory={"actions": [action()]},
        execution_history=[{
            "sequence": 1, "index": 1, "source": "cache",
            "runtime_status": "completed_without_exception",
            "action": {"type": "click", "point": {"x": 100, "y": 200},
                       "plan_intent": "点击当前目标"},
        }, {
            "sequence": 2, "index": 2, "source": "cache",
            "runtime_status": "skipped",
            "action": {"type": "click", "point": {"x": 999, "y": 888},
                       "plan_intent": "关闭未出现的辅助弹窗"},
        }],
    )
    assert "{'x': 100, 'y': 200}" in prompt
    assert "{'x': 999, 'y': 888}" not in prompt
    assert "plan_intent=点击当前目标" in prompt
    assert "status=completed_without_exception" in prompt
    assert "record 2 step 2: click source=cache status=skipped" in prompt
    assert "plan_intent=关闭未出现的辅助弹窗" in prompt


def test_v3_empty_runtime_history_never_falls_back_to_cached_plan():
    prompt = build_cache_assertion_prompt(
        goal="点击当前目标", has_prev=False,
        trajectory={"actions": [action()]}, execution_history=[],
    )
    assert "不以缓存计划替代执行记录" in prompt
    assert "{'x': 999, 'y': 888}" not in prompt


def test_v3_history_window_keeps_all_operations_in_last_hundred_cache_steps():
    history = []
    for step in range(1, 102):
        for source in ("rescue_repair", "cache"):
            history.append({
                "sequence": len(history) + 1, "index": step, "source": source,
                "runtime_status": "completed_without_exception", "action": {"type": "press_home"},
            })
    prompt = build_cache_assertion_prompt(
        goal="操作", has_prev=False, trajectory={}, execution_history=history,
    )
    assert "record 1 step 1:" not in prompt
    assert "record 3 step 2:" in prompt
    assert "record 202 step 101:" in prompt
    assert "前面还有 1 个缓存步骤的 2 条本轮 Runtime 记录" in prompt


@pytest.mark.asyncio
async def test_v3_history_records_ephemeral_gate_repair_not_original_action(make_runner, monkeypatch):
    runner = make_runner([action(role="optional_ephemeral")])

    async def gate(**kwargs):
        return {"mode": "execute_repair", "action": {"type": "press_back"}}

    monkeypatch.setattr(runner, "_handle_optional_ephemeral", gate)
    assert (await runner.run()).success
    row = runner.execution_history[0]
    assert row["source"] == "ephemeral_gate_repair"
    assert row["action"]["type"] == "press_back"
    assert "point" not in row["action"]


@pytest.mark.asyncio
async def test_v3_verifier_keeps_images_thinking_but_excludes_historical_success():
    received = {}

    class Assistant:
        async def verify_finished(self, **kwargs):
            received.update(kwargs)
            return "PASS: 当前结果成立"

    settings = Settings(_env_file=None, assistant_api_key="test-key",
                        assistant_api_url="https://example.test", assistant_model="test-model",
                        assistant_thinking_assertion=True)
    result = await CacheReplayAssertionVerifier(settings=settings, assistant=Assistant()).verify(
        goal="点击当前目标", final_bytes=b"new-final", prev_before_bytes=b"new-before",
        trajectory={"actions": [action()], "source_completion": {"assertion_pass": "历史语义锚点"}},
        execution_history=[{
            "sequence": 1, "index": 1, "source": "cache", "runtime_status": "completed_without_exception",
            "action": {"type": "click", "point": {"x": 100, "y": 200}, "plan_intent": "点击当前目标"},
        }],
    )
    assert result.passed
    assert "{'x': 100, 'y': 200}" in received["prompt"]
    assert "{'x': 999, 'y': 888}" not in received["prompt"]
    assert "历史语义锚点" not in received["prompt"]
    assert "首次成功语义锚点" not in received["prompt"]
    assert received["final_bytes"] == b"new-final"
    assert received["prev_before_bytes"] == b"new-before"
    assert received["thinking"] is True


@pytest.mark.asyncio
async def test_fifth_successful_rescue_can_finish_cache_without_handoff(make_runner, monkeypatch):
    rescue = Rescue(V3RescueDecision(verdict="REPAIR_ACTION", reason="关闭遮挡",
                                     repair_action={"type":"press_back"}))
    runner = make_runner([action()], rescue=rescue)
    runner._v3_rescue_max_calls = 5
    calls = 0
    async def locate(a, frame):
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise V3LocatorMiss("仍需修复")
        return {**a, "point":{"x":100,"y":200}}
    monkeypatch.setattr(runner,"_locate_action",locate)
    result=await runner.run()
    assert result.success and len(rescue.calls)==5
    assert not getattr(result,"takeover_required",False)


@pytest.mark.asyncio
async def test_last_budget_call_give_up_transfers_to_long_rescue(make_runner, monkeypatch):
    rescue=Rescue(V3RescueDecision(verdict="GIVE_UP",reason="仍无法恢复"))
    runner=make_runner([action()],rescue=rescue)
    runner._v3_rescue_max_calls=5
    runner._v3_rescue_calls_used=4
    async def locate(*args):raise V3LocatorMiss("不可见")
    monkeypatch.setattr(runner,"_locate_action",locate)
    result=await runner.run()
    assert result.takeover_required and len(rescue.calls)==1


@pytest.mark.asyncio
async def test_first_give_up_is_handed_off_without_full_restart_or_terminal(make_runner, monkeypatch):
    from types import SimpleNamespace
    from ai_phone.agent.trajectory_cache import orchestrate, v3_replay
    from ai_phone.agent.trajectory_cache.restart import V3TakeoverRequest
    from tests.test_v3_restart import Bridge

    rescue = Rescue(V3RescueDecision(verdict="GIVE_UP", reason="局部无法恢复，交给长程救援"))
    current = action()
    runner = make_runner([current], rescue=rescue)
    runner._v3_rescue_max_calls = 5

    async def miss(*args):
        raise V3LocatorMiss("当前目标不可见")

    monkeypatch.setattr(runner, "_locate_action", miss)
    monkeypatch.setattr(v3_replay, "V3ReplayRunner", lambda **kwargs: runner)
    bridge = Bridge()
    request = await orchestrate.run_v3_replay(
        run_id="unit", serial="unit-device", goal="完整原始 Case", attempt=1,
        driver=runner.driver, bridge=bridge,
        snapshot={"cache_key": "unit-cache", "actions": [current]},
        settings=SimpleNamespace(vlm_backend="doubao_responses"),
        restart_on_rescue_failure=True,
    )
    assert isinstance(request, V3TakeoverRequest)
    assert len(rescue.calls) == 1
    assert request.failed_action == current
    assert "局部无法恢复" in request.reason
    assert bridge.done == [] and runner.dispatcher.calls == []
    titles = [event.get("title") for event in bridge.events]
    assert "V3缓存 · 长程救援接管" in titles
    assert "V3缓存失效 · 完整重跑" not in titles
