"""Regeneration is syntax-only and never an extra physical/visual step."""
import copy
import pytest
from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache import v3_replay as v3
from ai_phone.shared.seed_gui_actions import parse_actions
from ai_phone.shared.prompt import OUTPUT_PROTOCOL
from tests.test_vlm_runner import FakeDriver, ScriptedStep, ScriptedVLMClient, VLMRunner, _collect_events


# Parser fixtures only; never injected into production prompts.
XML_CLICK_EXAMPLE = '<seed:tool_call><function name="click"><parameter name="point" string="true"><point>500 800</point></parameter><parameter name="action_summary" string="true">点击页面底部的继续按钮</parameter></function></seed:tool_call>'
XML_DRAG_EXAMPLE = '<seed:tool_call><function name="drag"><parameter name="start_point" string="true"><point>500 800</point></parameter><parameter name="end_point" string="true"><point>500 300</point></parameter><parameter name="action_summary" string="true">向上拖动当前区域</parameter></function></seed:tool_call>'

def test_original_main_click_example_is_unchanged():
    assert parse_actions(XML_CLICK_EXAMPLE)[0].point == [500, 800]
    drag = parse_actions(XML_DRAG_EXAMPLE)[0]
    assert drag.start_point == [500, 800] and drag.end_point == [500, 300]
    from ai_phone.agent.runner.long_rescue_prompts.doubao import OUTPUT_PROTOCOL as LONG_PROTOCOL
    assert XML_CLICK_EXAMPLE in OUTPUT_PROTOCOL and XML_CLICK_EXAMPLE in LONG_PROTOCOL
    assert "拖动格式示例" not in OUTPUT_PROTOCOL
    assert "拖动格式示例" not in LONG_PROTOCOL


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaust", [False, True])
@pytest.mark.parametrize("runner_kind", ["main", "long"])
async def test_runner_three_generations_same_frame_and_one_executed_step(exhaust, runner_kind, monkeypatch):
    import ai_phone.agent.runner.vlm_loop as loop
    import ai_phone.agent.runner.long_rescue as long_loop
    from ai_phone.agent.trajectory_cache.restart import V3TakeoverRequest
    settings = Settings(_env_file=None, vlm_backend="doubao_responses")
    monkeypatch.setattr(loop, "get_settings", lambda: settings)
    monkeypatch.setattr(long_loop, "get_settings", lambda: settings)
    bad = ScriptedStep("格式错误", "assert_fail(content='无法解析决策输出: bad XML')")
    vlm = ScriptedVLMClient([bad, bad, bad if exhaust else ScriptedStep("真实业务不能继续", "assert_fail(content='目标不可用')")])
    driver = FakeDriver()
    events, emit = _collect_events()
    cls = VLMRunner if runner_kind == "main" else long_loop.LongRescueRunner
    extra = {} if runner_kind == "main" else {"takeover": V3TakeoverRequest(reason="测试格式恢复", step_offset=0, elapsed_ms=0)}
    runner = cls("bounded-xml", driver, "查看目标", emit=emit, vlm_client=vlm, **extra)
    result = await runner.run()
    assert not result.ok
    assert vlm._idx == 3
    assert vlm.received_screenshots[0] == vlm.received_screenshots[1] == vlm.received_screenshots[2]
    assert result.steps == 1
    assert len([e for e in events if e.get("title") == "VLM 输出解析失败"]) == 2
    if not exhaust:
        assert "目标不可用" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("responses, expected_error, expected_count", [
    (["bad", "bad", XML_CLICK_EXAMPLE], None, 3),
    (["bad", "bad", "bad"], "invalid_seed_xml", 3),
    (['<seed:tool_call><function name="assert_fail"><parameter name="content" string="true">无可恢复路线</parameter></function></seed:tool_call>'], None, 1),
])
async def test_single_rescue_bounded_regeneration(responses, expected_error, expected_count, monkeypatch):
    calls = []
    async def call(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return responses[len(calls)-1]
    monkeypatch.setattr(v3, "_call_vlm_with_images", call)
    s = Settings(_env_file=None, trajectory_cache_v3_rescue_enabled=True,
        trajectory_cache_v3_rescue_use_recovery_vlm_config=False,
        trajectory_cache_v3_rescue_backend="doubao_responses",
        trajectory_cache_v3_rescue_api_url="https://test.invalid/chat/completions",
        trajectory_cache_v3_rescue_api_key="test", trajectory_cache_v3_rescue_model="test")
    result = await v3.V3RescueVerifier(settings=s).decide(goal="原始目标", trajectory={}, action={"type":"click"}, current_bytes=b"same-frame")
    assert (result.error or None) == expected_error
    assert len(calls) == expected_count
    assert all(c["images"] == [("current_replay", b"same-frame")] for c in calls)
    assert all("原始目标" in c["prompt"] for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaust", [False, True])
async def test_legacy_recovery_uses_three_generations_with_same_two_images(exhaust, monkeypatch):
    from ai_phone.agent.trajectory_cache.recovery import CacheReplayRecoveryVerifier
    settings = Settings(_env_file=None, trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_backend="doubao_responses",
        trajectory_cache_recovery_vlm_api_url="https://test.invalid/responses",
        trajectory_cache_recovery_vlm_api_key="test", trajectory_cache_recovery_vlm_model="test")
    verifier = CacheReplayRecoveryVerifier(settings=settings)
    calls = []
    async def chat(**kw):
        calls.append(kw)
        return "bad" if exhaust or len(calls) < 3 else XML_CLICK_EXAMPLE
    monkeypatch.setattr(verifier, "_chat_double_image", chat)
    result = await verifier.verify_alignment_miss(goal="原目标", trajectory={}, action={"type":"click"},
        landmark={}, current_bytes=b"current", landmark_bytes=b"landmark", metrics={}, elapsed_ms=0, max_wait_ms=5000)
    assert len(calls) == 3
    assert bool(result.error) == exhaust
    assert all(c["current_bytes"] == b"current" and c["landmark_bytes"] == b"landmark" for c in calls)
    assert "string=" in calls[1]["followup_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("runner_kind", ["main", "long"])
async def test_third_generation_executes_once_then_continues(runner_kind, monkeypatch):
    from ai_phone.agent.runner import vlm_loop, long_rescue
    from ai_phone.agent.trajectory_cache.restart import V3TakeoverRequest
    settings = Settings(_env_file=None, vlm_backend="doubao_responses")
    monkeypatch.setattr(vlm_loop, "get_settings", lambda: settings)
    monkeypatch.setattr(long_rescue, "get_settings", lambda: settings)
    bad = ScriptedStep("无效格式", "assert_fail(content='无法解析决策输出: bad XML')")
    client = ScriptedVLMClient([
        bad, bad, ScriptedStep("点击目标", "click(point='<point>500 500</point>')"),
        ScriptedStep("完成", "finished(content='已点击目标')"),
    ])
    driver = FakeDriver()
    _, emit = _collect_events()
    cls = VLMRunner if runner_kind == "main" else long_rescue.LongRescueRunner
    extra = {} if runner_kind == "main" else {
        "takeover": V3TakeoverRequest(reason="测试现场", step_offset=0, elapsed_ms=0),
    }
    result = await cls("third-generation", driver, "点击目标", emit=emit, vlm_client=client, **extra).run()
    assert result.ok and result.steps == 2
    assert driver.calls.count(("click", (540, 960))) == 1
    assert client._idx == 4
    assert client.received_screenshots[0] == client.received_screenshots[1] == client.received_screenshots[2]
