"""完整重跑边界测试；不操作真机，不调用外部模型。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ai_phone.agent.runner.events import (
    EVT_ACTION, EVT_LOG, EVT_RUN_FINISH, EVT_SCREENSHOT, EVT_STEP_END, EVT_STEP_START,
    EVT_THOUGHT, make_event,
)
from ai_phone.agent.trajectory_cache import orchestrate, v3_replay
from ai_phone.agent.trajectory_cache.restart import V3RestartRequest, restart_report_event


class Bridge:
    def __init__(self):
        self.events, self.done, self.suspects = [], [], []
        self.closed = False

    def emit(self, event):
        self.events.append(deepcopy(event))
        if event.get("type") == EVT_RUN_FINISH:
            self.done.append(event)

    async def send_run_done(self, payload):
        self.done.append(payload)

    async def send_cache_suspect(self, payload):
        self.suspects.append(payload)

    async def aclose(self):
        self.closed = True


SNAPSHOT = {"cache_mode": "v3", "schema_version": 3, "cache_key": "unit-cache",
            "actions": [{"index": 1, "type": "click", "plan_intent": "旧缓存动作"}]}


def runner_result(monkeypatch, result):
    class Runner:
        def __init__(self, **kwargs):
            self.emit = kwargs["emit"]

        async def run(self):
            self.emit(make_event(EVT_STEP_START, "unit", step=5))
            return result

    monkeypatch.setattr(v3_replay, "V3ReplayRunner", Runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_rescue_restart_invalidates_cache_without_emitting_terminal(monkeypatch, attempt):
    runner_result(monkeypatch, v3_replay.V3ReplayResult(
        success=False, actions_total=5, actions_executed=2, failed_index=3,
        error="rescue exhausted", restart_required=True,
    ))
    bridge = Bridge()
    request = await orchestrate.run_v3_replay(
        run_id="unit", serial="unit-device", goal="完整原任务", attempt=attempt,
        driver=object(), bridge=bridge, snapshot=SNAPSHOT,
        settings=SimpleNamespace(vlm_backend="doubao_responses"), restart_on_rescue_failure=True,
    )
    assert isinstance(request, V3RestartRequest)
    assert request.step_offset == 5
    assert request.elapsed_ms >= 0
    assert bridge.suspects[0]["cache_key"] == "unit-cache"
    assert bridge.suspects[0]["attempt"] == attempt
    assert bridge.done == []


@pytest.mark.asyncio
async def test_real_bridge_does_not_sleep_or_send_done_between_phases(monkeypatch):
    from ai_phone.agent.runner_bridge import RunnerBridge
    from ai_phone.shared import protocol as P

    runner_result(monkeypatch, v3_replay.V3ReplayResult(
        success=False, actions_total=5, actions_executed=2, failed_index=3,
        error="rescue exhausted", restart_required=True,
    ))
    messages, sleeps = [], []

    async def enqueue(message):
        messages.append(message)

    async def send(message):
        return True

    async def sleep_hook():
        sleeps.append(True)

    bridge = RunnerBridge(
        run_id="unit", serial="unit-device", ws_send=send, server_http_base="http://unit.invalid",
        reporter=SimpleNamespace(enqueue=enqueue), before_run_done=sleep_hook,
    )
    request = await orchestrate.run_v3_replay(
        run_id="unit", serial="unit-device", goal="完整原任务", attempt=1,
        driver=object(), bridge=bridge, snapshot=SNAPSHOT,
        settings=SimpleNamespace(vlm_backend="doubao_responses"), restart_on_rescue_failure=True,
    )
    assert sleeps == []
    assert not any(m["type"] == P.MSG_RUN_DONE for m in messages)
    bridge.emit(restart_report_event(make_event(
        EVT_RUN_FINISH, "unit", ok=True, reason="finished: 完整首跑完成", steps=2, elapsed_ms=9,
    ), request))
    await bridge.aclose()
    done = [m for m in messages if m["type"] == P.MSG_RUN_DONE]
    assert len(done) == 1 and sleeps == [True]
    assert done[0]["result"] == "finished"
    assert done[0]["steps"] == 7
    assert done[0]["elapsed_ms"] >= 9
    assert any(m["type"] == P.MSG_CACHE_SUSPECT for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,eligible", [(False, True), (True, False)])
async def test_old_call_or_non_rescue_error_keeps_original_terminal(monkeypatch, enabled, eligible):
    runner_result(monkeypatch, v3_replay.V3ReplayResult(
        success=False, actions_total=5, actions_executed=2, failed_index=3,
        error="unit failure", restart_required=eligible,
    ))
    bridge = Bridge()
    request = await orchestrate.run_v3_replay(
        run_id="unit", serial="unit-device", goal="完整原任务", attempt=1,
        driver=object(), bridge=bridge, snapshot=SNAPSHOT,
        settings=SimpleNamespace(vlm_backend="doubao_responses"), restart_on_rescue_failure=enabled,
    )
    assert request is None
    assert len(bridge.done) == 1
    assert bridge.done[0]["result"] == "error"


def test_restart_report_mapping_preserves_original_model_event():
    request = V3RestartRequest(reason="failed", step_offset=5, elapsed_ms=120)
    event = make_event(EVT_RUN_FINISH, "unit", step=2, steps=2, elapsed_ms=30,
                       ok=False, reason="assert_fail: 不满足原用例", token_stats={"call_count": 1})
    original = deepcopy(event)
    mapped = restart_report_event(event, request)
    assert event == original
    assert mapped["step"] == mapped["steps"] == 7
    assert mapped["elapsed_ms"] == 150
    assert mapped["reason"] == event["reason"]
    assert mapped["token_stats"] == event["token_stats"]
    log = restart_report_event(make_event(EVT_LOG, "unit", title="任务总耗时", content="30ms"), request)
    assert log["title"] == "完整首跑阶段耗时"


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("cache_mode_present", [True, False])
async def test_agent_restarts_once_with_fresh_records_and_only_success_can_archive(monkeypatch, ok, cache_mode_present):
    from ai_phone.agent import main as agent_main
    from ai_phone.agent.trajectory_cache import archive

    bridge, scheduled, builds, cache_calls = Bridge(), [], [], []
    original_goal = "[前置条件]\n原始前置\n[操作步骤]\n原始完整步骤\n[预期结果]\n原始验收"
    original_map = "原始业务Map"

    class Client:
        server_http_base = "http://unit.invalid"

        async def send(self, message):
            return True

    class CacheRunner:
        def __init__(self, **kwargs):
            cache_calls.append(kwargs)
            self.emit = kwargs["emit"]

        async def run(self):
            self.emit(make_event(EVT_STEP_START, "unit", step=2))
            self.emit(make_event(EVT_THOUGHT, "unit", step=2, text="旧缓存语义不能入新缓存"))
            self.emit(make_event(EVT_ACTION, "unit", step=2, actions=[{"action": "click", "point": [9, 9]}]))
            self.emit(make_event(EVT_STEP_END, "unit", step=2))
            return v3_replay.V3ReplayResult(success=False, actions_total=2, actions_executed=1,
                                           failed_index=2, error="rescue exhausted", restart_required=True)

    class FirstRun:
        async def run(self):
            emit = builds[0]["emit"]
            emit(make_event(EVT_STEP_START, "unit", step=1))
            emit(make_event(EVT_THOUGHT, "unit", step=1, text="新首跑独立行为"))
            emit(make_event(EVT_ACTION, "unit", step=1, actions=[{"action": "press_home"}]))
            emit(make_event(EVT_SCREENSHOT, "unit", step=1, phase="before", bytes=b"new-before"))
            emit(make_event(EVT_SCREENSHOT, "unit", step=1, phase="after", bytes=b"new-after"))
            emit(make_event(EVT_STEP_END, "unit", step=1))
            emit(make_event(EVT_RUN_FINISH, "unit", steps=1, elapsed_ms=9, ok=ok,
                            reason="finished: 新首跑完成" if ok else "assert_fail: 新首跑失败"))

    def build(**kwargs):
        builds.append(kwargs)
        return FirstRun()

    monkeypatch.setattr(v3_replay, "V3ReplayRunner", CacheRunner)
    monkeypatch.setattr(agent_main, "RunnerBridge", lambda **kwargs: bridge)
    monkeypatch.setattr(agent_main, "has_runtime_override", lambda: True)
    monkeypatch.setattr(agent_main, "get_settings", lambda: SimpleNamespace(android_wake_before_run=False))
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: SimpleNamespace(platform="android"))
    monkeypatch.setattr(agent_main, "build_runner", build)
    monkeypatch.setattr(agent_main, "_schedule_cache_archive", lambda **kwargs: scheduled.append(kwargs))
    supervisor = agent_main._RunSupervisor()
    message = {"run_id": "unit", "device_serial": "unit-device", "goal": original_goal,
               "function_map_context": original_map, "cache_snapshot": deepcopy(SNAPSHOT),
               "should_sleep_after_run": False, "engine": "vlm", "attempt": 1}
    if cache_mode_present:
        message["cache_mode"] = "v3"
    await agent_main._handle_start_run(Client(), supervisor, message)
    task = supervisor.get("unit")["task"]
    await task
    assert len(cache_calls) == len(builds) == 1
    assert builds[0]["goal"] == original_goal
    assert builds[0]["function_map_context"] == original_map
    assert len(bridge.suspects) == 1
    assert len(bridge.done) == 1
    assert bridge.done[0]["ok"] is ok
    assert bridge.done[0]["steps"] == 3
    assert bridge.done[0]["elapsed_ms"] >= 9
    assert bridge.closed and supervisor.get("unit") is None
    assert len(scheduled) == (1 if ok else 0)
    if ok:
        record = scheduled[0]["recorder"]
        assert scheduled[0]["cache_mode"] == "v3"
        assert [s["step"] for s in record.steps()] == [3]
        assert record.steps()[0]["actions"] == [{"action": "press_home"}]
        assert record.steps()[0]["before_bytes"] == b"new-before"
        monkeypatch.setattr(archive.V3PlanIntentCleaner, "is_configured", lambda self: False)
        payload = await archive.build_v3_archive(
            goal=original_goal, device_serial="unit-device", source_run_id="unit", steps=record.steps(),
        )
        assert [a["type"] for a in payload["actions"]] == ["press_home"]
        assert payload["actions"][0]["index"] == 1
        assert payload["actions"][0]["source_step"] == 3
        assert "旧缓存语义" not in str(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["init", "crash", "cancel"])
async def test_restart_failure_or_cancel_has_one_terminal_and_never_archives(monkeypatch, failure):
    import asyncio
    from ai_phone.agent import main as agent_main

    bridge, scheduled, builds = Bridge(), [], []
    started = asyncio.Event()

    class Client:
        server_http_base = "http://unit.invalid"

        async def send(self, message):
            return True

    async def replay(**kwargs):
        return V3RestartRequest(reason="rescue exhausted", step_offset=4, elapsed_ms=7)

    class FirstRun:
        async def run(self):
            builds[0]["emit"](make_event(EVT_STEP_START, "unit", step=1))
            if failure == "cancel":
                started.set()
                await asyncio.Event().wait()
            raise RuntimeError("unit runner crash")

    def build(**kwargs):
        builds.append(kwargs)
        if failure == "init":
            raise RuntimeError("unit initialization failure")
        return FirstRun()

    monkeypatch.setattr(orchestrate, "run_v3_replay", replay)
    monkeypatch.setattr(agent_main, "RunnerBridge", lambda **kwargs: bridge)
    monkeypatch.setattr(agent_main, "get_settings", lambda: SimpleNamespace(android_wake_before_run=False))
    monkeypatch.setattr(agent_main, "has_runtime_override", lambda: True)
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: SimpleNamespace(platform="android"))
    monkeypatch.setattr(agent_main, "build_runner", build)
    monkeypatch.setattr(agent_main, "_schedule_cache_archive", lambda **kwargs: scheduled.append(kwargs))
    supervisor = agent_main._RunSupervisor()
    await agent_main._handle_start_run(Client(), supervisor, {
        "run_id": "unit", "device_serial": "unit-device", "goal": "完整原任务", "cache_mode": "v3",
        "cache_snapshot": deepcopy(SNAPSHOT), "should_sleep_after_run": False,
    })
    task = supervisor.get("unit")["task"]
    if failure == "cancel":
        await asyncio.wait_for(started.wait(), timeout=1)
        assert supervisor.is_busy("unit-device")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert len(bridge.done) == 1
    assert bridge.done[0]["result"] == ("cancelled" if failure == "cancel" else "error")
    assert bridge.done[0]["steps"] == (4 if failure == "init" else 5)
    assert bridge.done[0]["elapsed_ms"] >= 7
    assert len(builds) == 1 and scheduled == []
    assert bridge.closed and supervisor.get("unit") is None


@pytest.mark.asyncio
async def test_agent_uses_actual_first_run_executor_with_a_fresh_model_session(monkeypatch):
    from ai_phone.agent import main as agent_main
    from ai_phone.agent.runner import vlm_loop
    from ai_phone.config import Settings
    from tests.test_vlm_runner import FakeDriver, ScriptedStep, ScriptedVLMClient

    bridge, scheduled, models = Bridge(), [], []
    driver = FakeDriver()
    settings = Settings(_env_file=None, android_wake_before_run=False,
                        vlm_page_stable_enabled=False, transient_ui_enabled=False)

    class Client:
        server_http_base = "http://unit.invalid"

        async def send(self, message):
            return True

    async def replay(**kwargs):
        return V3RestartRequest(reason="rescue exhausted", step_offset=3, elapsed_ms=7)

    def model(**kwargs):
        client = ScriptedVLMClient([ScriptedStep("重新返回桌面", "press_home()"),
                                   ScriptedStep("完整任务已完成", "finished(content='完成')")])
        models.append(client)
        return client

    monkeypatch.setattr(orchestrate, "run_v3_replay", replay)
    monkeypatch.setattr(agent_main, "RunnerBridge", lambda **kwargs: bridge)
    monkeypatch.setattr(agent_main, "has_runtime_override", lambda: True)
    monkeypatch.setattr(agent_main, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    monkeypatch.setattr(vlm_loop, "get_settings", lambda: settings)
    monkeypatch.setattr(vlm_loop, "create_main_vlm", model)
    monkeypatch.setattr(agent_main, "_schedule_cache_archive", lambda **kwargs: scheduled.append(kwargs))
    supervisor = agent_main._RunSupervisor()
    await agent_main._handle_start_run(Client(), supervisor, {
        "run_id": "unit", "device_serial": "unit-device", "goal": "按Home回到桌面",
        "function_map_context": "业务上下文保留", "cache_mode": "v3",
        "cache_snapshot": deepcopy(SNAPSHOT), "should_sleep_after_run": False,
    })
    await supervisor.get("unit")["task"]
    assert len(models) == 1 and len(models[0].received_screenshots) == 2
    assert any(call[0] == "press_home" for call in driver.calls)
    assert len(bridge.done) == 1 and bridge.done[0]["ok"]
    assert bridge.done[0]["steps"] == 5
    assert len(scheduled) == 1
    steps = scheduled[0]["recorder"].steps()
    assert steps[0]["step"] == 4
    assert steps[0]["actions"][0]["action"] == "press_home"
    assert all("旧缓存" not in row.get("thought", "") for row in steps)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_mode", [None, "off", "v1", "v2", "v3"])
async def test_actual_agent_cache_miss_enters_first_run_for_every_cache_mode(monkeypatch, cache_mode):
    """走真实 start_run/执行器；不能用缓存命中或重跑替身掩盖首跑入口的作用域错误。"""
    from ai_phone.agent import main as agent_main
    from ai_phone.agent.runner import vlm_loop
    from ai_phone.config import Settings
    from tests.test_vlm_runner import FakeDriver, ScriptedStep, ScriptedVLMClient

    bridge, scheduled, models = Bridge(), [], []
    driver = FakeDriver()
    settings = Settings(_env_file=None, android_wake_before_run=False,
                        vlm_page_stable_enabled=False, transient_ui_enabled=False)

    class Client:
        server_http_base = "http://unit.invalid"

        async def send(self, message):
            return True

    def model(**kwargs):
        client = ScriptedVLMClient([
            ScriptedStep("返回桌面", "press_home()"),
            ScriptedStep("已到桌面", "finished(content='完成')"),
        ])
        models.append(client)
        return client

    async def unexpected_replay(**kwargs):
        pytest.fail("未命中缓存的任务不应进入回放")

    for name in ("run_v1_replay", "run_v2_replay", "run_v3_replay"):
        monkeypatch.setattr(orchestrate, name, unexpected_replay)
    monkeypatch.setattr(agent_main, "RunnerBridge", lambda **kwargs: bridge)
    monkeypatch.setattr(agent_main, "has_runtime_override", lambda: True)
    monkeypatch.setattr(agent_main, "get_settings", lambda: settings)
    monkeypatch.setattr(agent_main, "_get_or_open_driver", lambda serial: driver)
    monkeypatch.setattr(vlm_loop, "get_settings", lambda: settings)
    monkeypatch.setattr(vlm_loop, "create_main_vlm", model)
    monkeypatch.setattr(agent_main, "_schedule_cache_archive", lambda **kwargs: scheduled.append(kwargs))
    supervisor = agent_main._RunSupervisor()
    message = {
        "run_id": "unit", "device_serial": "unit-device", "goal": "按Home回到桌面",
        "function_map_context": "原始Map不变", "should_sleep_after_run": False,
    }
    if cache_mode is not None:
        message["cache_mode"] = cache_mode
    await agent_main._handle_start_run(Client(), supervisor, message)
    await supervisor.get("unit")["task"]
    assert len(models) == 1 and len(models[0].received_screenshots) == 2
    assert any(call[0] == "press_home" for call in driver.calls)
    assert len(bridge.done) == 1 and bridge.done[0]["ok"]
    assert bridge.done[0]["steps"] == 2
    assert bridge.closed and supervisor.get("unit") is None
    assert len(scheduled) == (1 if cache_mode in {"v1", "v2", "v3"} else 0)
    if scheduled:
        assert scheduled[0]["cache_mode"] == cache_mode
        assert scheduled[0]["goal"] == message["goal"]
        assert scheduled[0]["recorder"].steps()[0]["step"] == 1
