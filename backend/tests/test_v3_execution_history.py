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

    def is_configured(self):
        return True

    async def decide(self, **kwargs):
        return self.decision


@pytest.fixture
def make_runner(monkeypatch):
    def create(actions, *, dispatcher=None, rescue=None):
        stream = BytesIO()
        Image.new("RGB", (64, 128), "white").save(stream, "JPEG")
        frame = stream.getvalue()
        runner = V3ReplayRunner(
            driver=Driver(), trajectory={"actions": deepcopy(actions)},
            dispatcher=dispatcher or Dispatcher(), rescue_verifier=rescue,
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
async def test_v3_history_marks_driver_error_without_claiming_completion(make_runner):
    runner = make_runner([action(type="press_home")], dispatcher=Dispatcher(fail=True))
    assert not (await runner.run()).success
    assert runner.execution_history[0]["runtime_status"] == "execution_error"
    assert "driver failed" in runner.execution_history[0]["error"]


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
        }],
    )
    assert "{'x': 100, 'y': 200}" in prompt
    assert "{'x': 999, 'y': 888}" not in prompt
    assert "plan_intent=点击当前目标" in prompt
    assert "status=completed_without_exception" in prompt
    assert "skipped 表示该缓存动作本轮没有执行" in prompt


def test_v3_empty_runtime_history_never_falls_back_to_cached_plan():
    prompt = build_cache_assertion_prompt(
        goal="点击当前目标", has_prev=False,
        trajectory={"actions": [action()]}, execution_history=[],
    )
    assert "不以缓存计划替代执行记录" in prompt
    assert "{'x': 999, 'y': 888}" not in prompt


def test_v3_history_window_keeps_all_operations_in_last_twenty_cache_steps():
    history = []
    for step in range(1, 22):
        for source in ("input_focus", "cache"):
            history.append({
                "sequence": len(history) + 1, "index": step, "source": source,
                "runtime_status": "completed_without_exception", "action": {"type": "press_home"},
            })
    prompt = build_cache_assertion_prompt(
        goal="操作", has_prev=False, trajectory={}, execution_history=history,
    )
    assert "record 1 step 1:" not in prompt
    assert "record 3 step 2:" in prompt
    assert "record 42 step 21:" in prompt
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
async def test_v3_verifier_passes_real_history_and_keeps_images_thinking_and_anchor():
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
    assert "历史语义锚点" in received["prompt"]
    assert received["final_bytes"] == b"new-final"
    assert received["prev_before_bytes"] == b"new-before"
    assert received["thinking"] is True
