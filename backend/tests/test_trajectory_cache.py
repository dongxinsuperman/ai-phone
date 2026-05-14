from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ai_phone.config import Settings
from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import (
    Device,
    Run,
    RunCommand,
    RunLog,
    RunStep,
    VlmTrajectoryCache,
    VlmTrajectoryCacheV3,
)
from ai_phone.server.runner.rpc import DriverRpcWaiter
from ai_phone.server.runner.service import ServerRunnerService
from ai_phone.server.trajectory_cache import (
    CacheReplayAssertionVerifier,
    CacheReplayRecoveryVerifier,
    EphemeralGateDecision,
    GATE_EXECUTE_REPAIR,
    GATE_SKIP,
    RecoveryDecision,
    ReplayActionDispatcher,
    V3LocateResult,
    V3PlanLocator,
    V3ReplayRunner,
    V3RescueDecision,
    V3RescueVerifier,
    VERDICT_ASSERT_FAIL,
    VERDICT_CONTINUE,
    VERDICT_REPAIR_ACTION,
    VERDICT_WAIT_MORE,
    build_cache_assertion_prompt,
    build_cache_key,
    build_v3_locator_prompt,
    build_v3_cache_payload,
    build_recovery_prompt,
    delete_trajectory_cache_for_run,
    get_active_trajectory_cache_v3,
    get_active_trajectory_cache,
    mark_trajectory_cache_v3_suspect,
    normalize_run_semantic,
    normalize_requested_cache_mode,
    parse_cache_assertion_response,
    parse_ephemeral_classification_response,
    parse_ephemeral_gate_response,
    parse_recovery_response,
    parse_v3_locator_response,
    parse_v3_rescue_response,
    resolve_effective_cache_mode,
    save_trajectory_cache_after_success,
    save_trajectory_cache_v3_after_success,
)
from ai_phone.server.trajectory_cache.recovery import (
    _extract_messages_text,
    _extract_responses_text,
)
from ai_phone.server.trajectory_cache.v3_replay import V3LocatorMiss
from ai_phone.server.trajectory_cache import ephemeral as ephemeral_module


def test_normalize_run_semantic_is_strict_and_deterministic():
    assert normalize_run_semantic("  打开　微信\n\n发送  hello  ") == "打开 微信 发送 hello"


def test_cache_mode_resolution_is_tolerant_and_env_gated():
    assert normalize_requested_cache_mode(None) == "off"
    assert normalize_requested_cache_mode(" V3 ") == "v3"
    assert normalize_requested_cache_mode("bad") == "off"
    assert (
        resolve_effective_cache_mode(
            env_cache_enabled=False,
            requested_cache_mode="v3",
        )
        == "off"
    )
    assert (
        resolve_effective_cache_mode(
            env_cache_enabled=True,
            requested_cache_mode="v3",
        )
        == "v3"
    )


def test_parse_v3_locator_response_accepts_markdown_action():
    parsed = parse_v3_locator_response(
        "**Thought:** 当前按钮在右下角\n**Action:** click(point='<point>500 250</point>')",
        coord_space="normalized",
    )

    assert parsed is not None
    assert parsed.action == "click"
    assert parsed.point == [500, 250]
    assert parsed.coord_space == "normalized"


def test_build_v3_locator_prompt_is_minimal_and_action_specific():
    prompt = build_v3_locator_prompt(
        goal="不要进入 prompt",
        trajectory={"run_semantic_text": "也不要进入 prompt"},
        action={"index": 2, "type": "click", "plan_intent": "点击开始挑战"},
        coord_space="normalized",
    )

    assert "目标：点击开始挑战" in prompt
    assert "动作：click" in prompt
    assert "Action: click(point='<point>x y</point>')" in prompt
    assert "不要复用缓存旧坐标" in prompt
    assert "只能猜测" in prompt
    assert "不要进入 prompt" not in prompt
    assert "也不要进入 prompt" not in prompt
    assert "V3" not in prompt
    assert "Thought" not in prompt

    drag_prompt = build_v3_locator_prompt(
        goal="",
        trajectory={},
        action={"index": 1, "type": "drag", "plan_intent": "向上拖动列表"},
        coord_space="absolute",
    )
    assert "动作：drag" in drag_prompt
    assert "Action: drag(start='<point>x1 y1</point>', end='<point>x2 y2</point>')" in drag_prompt


def test_parse_v3_rescue_response_accepts_popup_close_json():
    decision = parse_v3_rescue_response(
        '{"verdict":"POPUP_CLOSE","reason":"弹窗遮挡",'
        '"repair_action":{"type":"click","point":{"x":900,"y":100}}}',
        coord_space="normalized",
    )

    assert decision.verdict == "POPUP_CLOSE"
    assert decision.repair_action["point"] == {"x": 900, "y": 100}


def test_parse_v3_rescue_response_accepts_continue_and_repair_action():
    cont = parse_v3_rescue_response('{"verdict":"CONTINUE","reason":"已到下一步页面"}')
    repair = parse_v3_rescue_response(
        '{"verdict":"REPAIR_ACTION","reason":"需要点确认",'
        '"repair_action":{"type":"click","point":{"x":500,"y":500}}}'
    )

    assert cont.verdict == "CONTINUE_REPLAY"
    assert repair.verdict == "REPAIR_ACTION"
    assert repair.repair_action["point"] == {"x": 500, "y": 500}


def test_v3_coord_space_follows_actual_locator_backend_family():
    settings = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        trajectory_cache_v3_coord_use_recovery_vlm_config=True,
        trajectory_cache_recovery_vlm_backend="claude_messages",
        trajectory_cache_recovery_vlm_api_url="https://example.test/messages",
        trajectory_cache_recovery_vlm_api_key="key",
        trajectory_cache_recovery_vlm_model="claude-sonnet",
        trajectory_cache_v3_rescue_use_recovery_vlm_config=True,
        trajectory_cache_v3_rescue_enabled=True,
    )

    locator = V3PlanLocator(settings=settings, main_vlm_backend="claude_cu")
    rescue = V3RescueVerifier(settings=settings, main_vlm_backend="claude_cu")

    assert locator.coord_space == "absolute"
    assert rescue.coord_space == "absolute"
    assert locator.is_configured() is False

    main_cu_settings = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        trajectory_cache_v3_coord_use_recovery_vlm_config=True,
        trajectory_cache_recovery_vlm_backend="doubao_responses",
        trajectory_cache_recovery_vlm_api_url="",
        trajectory_cache_recovery_vlm_api_key="",
        trajectory_cache_recovery_vlm_model="",
        vlm_api_url="https://api.anthropic.com/v1/messages",
        vlm_api_key="key",
        vlm_model="claude-sonnet",
    )
    main_cu_locator = V3PlanLocator(settings=main_cu_settings, main_vlm_backend="claude_cu")

    assert main_cu_locator.coord_space == "absolute"
    assert main_cu_locator.is_configured() is True

    doubao_settings = Settings(
        _env_file=None,
        trajectory_cache_v3_coord_use_recovery_vlm_config=True,
        trajectory_cache_recovery_vlm_backend="openai_compatible",
        trajectory_cache_recovery_vlm_api_url="https://example.test/chat",
        trajectory_cache_recovery_vlm_api_key="key",
        trajectory_cache_recovery_vlm_model="doubao-seed",
        trajectory_cache_v3_rescue_use_recovery_vlm_config=True,
        trajectory_cache_v3_rescue_enabled=True,
    )
    doubao_locator = V3PlanLocator(settings=doubao_settings, main_vlm_backend="claude_cu")

    assert doubao_locator.coord_space == "normalized"

    generic_settings = Settings(
        _env_file=None,
        trajectory_cache_v3_coord_use_recovery_vlm_config=True,
        trajectory_cache_recovery_vlm_backend="openai_compatible",
        trajectory_cache_recovery_vlm_api_url="https://example.test/chat",
        trajectory_cache_recovery_vlm_api_key="key",
        trajectory_cache_recovery_vlm_model="generic-vision-model",
        trajectory_cache_v3_rescue_use_recovery_vlm_config=True,
        trajectory_cache_v3_rescue_enabled=True,
    )
    generic_locator = V3PlanLocator(settings=generic_settings, main_vlm_backend="doubao_responses")

    assert generic_locator.coord_space == "absolute"


def test_v3_locator_rejects_screen_edge_and_repeated_points_for_different_targets():
    runner = V3ReplayRunner(driver=FakeDriver(), trajectory={"actions": []})

    with pytest.raises(V3LocatorMiss, match="屏幕边缘"):
        runner._validate_located_action(
            {"type": "click", "plan_intent": "点击应用图标"},
            {"type": "click", "point": {"x": 1079, "y": 1667}},
            window_size=(1080, 2400),
        )

    runner._validate_located_action(
        {"type": "click", "plan_intent": "点击关闭按钮"},
        {"type": "click", "point": {"x": 500, "y": 500}},
        window_size=(1080, 2400),
    )

    with pytest.raises(V3LocatorMiss, match="不同目标返回同一坐标"):
        runner._validate_located_action(
            {"type": "click", "plan_intent": "点击底部标签"},
            {"type": "click", "point": {"x": 500, "y": 500}},
            window_size=(1080, 2400),
        )


def test_intent_from_thought_supports_chinese_and_english_verbs():
    """trajectory 写库阶段的 intent 抽取要兼容三家主 VLM backend：

    - 豆包系：thought 是中文，已有；
    - claude_cu / gpt_cu：thought 是英文（含 thinking + cleaned_text 拼接），
      包含 click / type / swipe / open / close / scroll / drag / tap 等英文动词。
    """
    from ai_phone.server.trajectory_cache.service import _intent_from_thought

    # 豆包系（中文）：第一句无动词、第二句含动词，应抽第二句
    assert (
        _intent_from_thought("当前页面显示得很完整。需要点击右上角的菜单按钮。")
        == "需要点击右上角的菜单按钮"
    )

    # claude_cu 系（英文 + 多句）
    claude_thought = (
        "I need to verify the user's request and then close the target app. "
        "I'll click the home button to return to the launcher."
    )
    intent = _intent_from_thought(claude_thought)
    assert "click" in intent.lower(), f"claude thought 未抽到含 click 的句子：{intent!r}"

    # gpt_cu 系（英文 + 不同动词）
    gpt_thought = (
        "First I observe the screen state. "
        "Then I will swipe up from the bottom to open the app switcher."
    )
    intent = _intent_from_thought(gpt_thought)
    assert any(v in intent.lower() for v in ("swipe", "open")), (
        f"gpt thought 未抽到含 swipe/open 的句子：{intent!r}"
    )

    # 各家可能用 long_press / double_click / press_back 等带下划线/连字符的动词
    long_press_thought = "I will perform a long_press on the icon to open the menu."
    intent = _intent_from_thought(long_press_thought)
    assert "long_press" in intent.lower(), (
        f"long_press 类动词应被识别：{intent!r}"
    )


def test_parse_cache_assertion_response():
    assert parse_cache_assertion_response("PASS: ok").verdict == "PASS"
    assert parse_cache_assertion_response("FAIL: bad").reason == "bad"
    assert parse_cache_assertion_response("MAYBE").verdict == "SKIP"


def test_parse_ephemeral_classification_response_is_conservative():
    optional = parse_ephemeral_classification_response(
        '{"role":"optional_ephemeral","category":"marketing_popup",'
        '"confidence":0.91,"skip_if_absent":true,"business_risk":"low",'
        '"reason":"营销弹窗遮挡，关闭后回到业务页"}',
        min_confidence=0.85,
    )
    assert optional.role == "optional_ephemeral"
    assert optional.is_optional is True

    low_conf = parse_ephemeral_classification_response(
        '{"role":"optional_ephemeral","category":"marketing_popup",'
        '"confidence":0.50,"skip_if_absent":true,"reason":"不够确定"}',
        min_confidence=0.85,
    )
    assert low_conf.role == "business_required"

    high_risk = parse_ephemeral_classification_response(
        '{"role":"optional_ephemeral","category":"payment_or_trade_confirm",'
        '"confidence":0.99,"skip_if_absent":true,"reason":"像确认弹窗"}',
        min_confidence=0.85,
    )
    assert high_risk.role == "business_required"


def test_parse_ephemeral_gate_response_requires_repair_action():
    skip = parse_ephemeral_gate_response(
        '{"verdict":"SKIP","reason":"当前无同类弹窗，下一步按钮可见"}'
    )
    assert skip.verdict == "SKIP"

    repair = parse_ephemeral_gate_response(
        '{"verdict":"EXECUTE_REPAIR","reason":"关闭按钮换位置",'
        '"repair_action":{"type":"click","point":{"x":500,"y":500}}}'
    )
    assert repair.verdict == "EXECUTE_REPAIR"
    assert repair.repair_action["type"] == "click"

    missing = parse_ephemeral_gate_response(
        '{"verdict":"EXECUTE_REPAIR","reason":"没给动作"}'
    )
    assert missing.verdict == "ESCALATE"


def test_parse_ephemeral_gate_response_accepts_fenced_nested_json():
    decision = parse_ephemeral_gate_response(
        """```json
{
  "verdict": "EXECUTE_REPAIR",
  "reason": "close button moved",
  "repair_action": {
    "type": "click",
    "point": {"x": 540, "y": 1024}
  }
}
```"""
    )

    assert decision.verdict == "EXECUTE_REPAIR"
    assert decision.repair_action["point"] == {"x": 540, "y": 1024}


def test_ephemeral_classifier_falls_back_to_assistant_config():
    settings = Settings(
        _env_file=None,
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_classify_enabled=True,
        trajectory_cache_ephemeral_classifier_api_url="",
        trajectory_cache_ephemeral_classifier_api_key="",
        trajectory_cache_ephemeral_classifier_model="",
        assistant_backend="claude",
        assistant_api_url="https://api.anthropic.com/v1/messages",
        assistant_api_key="sk-ant-test",
        assistant_model="claude-sonnet-4-5",
    )
    classifier = ephemeral_module.CacheEphemeralActionClassifier(settings=settings)

    assert classifier.is_configured() is True
    backend, api_url, api_key, model, _timeout = classifier._config()
    assert backend == "claude_messages"
    assert api_url == "https://api.anthropic.com/v1/messages"
    assert api_key == "sk-ant-test"
    assert model == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_ephemeral_chat_payload_uses_provider_reasoning_fields(monkeypatch):
    captured = []

    async def fake_post_json(api_url, api_key, payload, timeout_sec):
        captured.append((api_url, payload))
        return {"choices": [{"message": {"content": "{\"verdict\":\"SKIP\"}"}}]}

    monkeypatch.setattr(ephemeral_module, "_post_json", fake_post_json)

    await ephemeral_module._chat_completions_images(
        api_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        api_key="key",
        model="doubao",
        timeout_sec=30,
        system="sys",
        prompt="prompt",
        images=[("current", b"jpeg")],
    )
    await ephemeral_module._chat_completions_images(
        api_url="https://api.openai.com/v1/chat/completions",
        api_key="key",
        model="o4-mini",
        timeout_sec=30,
        system="sys",
        prompt="prompt",
        images=[("current", b"jpeg")],
    )

    doubao_payload = captured[0][1]
    openai_payload = captured[1][1]
    assert doubao_payload["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in doubao_payload
    assert openai_payload["reasoning_effort"] == "medium"
    assert "thinking" not in openai_payload


@pytest.mark.asyncio
async def test_ephemeral_claude_messages_payload_enables_thinking(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"content": [{"type": "text", "text": "{\"verdict\":\"SKIP\"}"}]}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, api_url, json, headers):
            captured["api_url"] = api_url
            captured["payload"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(ephemeral_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        ephemeral_module,
        "get_settings",
        lambda: Settings(_env_file=None, vlm_main_thinking_budget=1024),
    )

    text = await ephemeral_module._messages_images(
        api_url="https://api.anthropic.com/v1/messages",
        api_key="sk-ant-test",
        model="claude-sonnet-4-5",
        timeout_sec=30,
        system="sys",
        prompt="prompt",
        images=[("current", b"jpeg")],
    )

    assert text == '{"verdict":"SKIP"}'
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["payload"]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 1024,
    }
    assert captured["payload"]["max_tokens"] == 4096


def test_build_cache_assertion_prompt_contains_replay_summary():
    prompt = build_cache_assertion_prompt(
        goal="打开应用并确认首页展示",
        trajectory={
            "actions": [
                {"index": 1, "type": "open_app", "app_name": "com.demo"},
                {"index": 2, "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "source_completion": {
                "run_reason": "已到达目标落点，任务完成。",
                "assertion_pass": "附图2显示的最终落点与用户目标语义一致。",
            },
        },
        has_prev=False,
    )

    assert "缓存轨迹回放后的最终页面" in prompt
    assert "step 1: open_app app=com.demo" in prompt
    assert "step 2: click point={'x': 1, 'y': 2}" in prompt
    assert "首次成功语义锚点" in prompt
    assert "目标落点" in prompt


def test_cache_key_does_not_include_vlm_backend(monkeypatch):
    key_a, normalized_a, hash_a = build_cache_key(
        device_code="D1",
        run_semantic_text="点击我的",
    )
    monkeypatch.setenv("AI_PHONE_VLM_BACKEND", "claude_cu")
    key_b, normalized_b, hash_b = build_cache_key(
        device_code="D1",
        run_semantic_text="点击我的",
    )

    assert key_a == key_b
    assert normalized_a == normalized_b
    assert hash_a == hash_b


class FakeDriver:
    serial = "D1"
    platform = "android"

    def __init__(self):
        self.calls = []

    def window_size(self):
        return (1000, 2000)

    def rotation(self):
        return 0

    def screenshot_png(self):
        return b"png"

    def screenshot_jpeg(self, quality=25, max_side=None):
        self.calls.append(("screenshot_jpeg", quality, max_side))
        return b"jpeg"

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def double_click(self, x, y, interval_ms=100):
        self.calls.append(("double_click", x, y, interval_ms))

    def long_press(self, x, y, duration_ms=1000):
        self.calls.append(("long_press", x, y, duration_ms))

    def swipe(self, sx, sy, ex, ey, duration_ms=500):
        self.calls.append(("swipe", sx, sy, ex, ey, duration_ms))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def press_home(self):
        self.calls.append(("press_home",))

    def press_back(self):
        self.calls.append(("press_back",))

    def press_keycode(self, code):
        self.calls.append(("press_keycode", code))

    def list_third_party_packages(self):
        return []

    def list_all_packages(self):
        return []

    def activate_app(self, package_name):
        self.calls.append(("activate_app", package_name))

    def terminate_app(self, package_name):
        self.calls.append(("terminate_app", package_name))

    def current_app(self):
        return ""

    def device_info(self):
        return None

    def scroll(self, direction, center=None, amount=1):
        self.calls.append(("scroll", direction, center, amount))


class FakeAssistant:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def match_package(self, app_name, packages):
        return ""

    async def chat_text(self, prompt, *, label="辅助", thinking=False):
        return ""

    async def verify_finished(
        self,
        *,
        prompt,
        prev_before_bytes,
        final_bytes,
        thinking=True,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "prev_before_bytes": prev_before_bytes,
                "final_bytes": final_bytes,
                "thinking": thinking,
            }
        )
        return self.text


class FakeEmitter:
    def __init__(self):
        self.finishes = []
        self.events = []

    def emit(self, evt):
        self.events.append(evt)

    async def force_finish(self, **kwargs):
        self.finishes.append(kwargs)


class FakeReplayRunner:
    def __init__(self, *, driver, trajectory, log, **kwargs):
        self.driver = driver
        self.trajectory = trajectory
        self.log = log

    async def run(self):
        await self.log(1, "fake replay", "ok")
        return SimpleNamespace(
            success=True,
            error="",
            final_before_bytes=b"before",
            actions_executed=1,
            elapsed_ms=42,
            to_dict=lambda: {},
        )

    async def capture_final_frame(self):
        return b"jpeg"


class FakeEphemeralGate:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def is_configured(self):
        return True

    def configuration_problem(self):
        return ""

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision


class FakeAlignmentMissReplayRunner(FakeReplayRunner):
    async def run(self):
        await self.log(3, "轨迹缓存状态路标", "轨迹偏航，终止缓存回放")
        return SimpleNamespace(
            success=False,
            error="index=1 type=click error=alignment_miss action_id=a001 elapsed=1000/1000ms",
            final_before_bytes=None,
            actions_executed=0,
            elapsed_ms=11,
            to_dict=lambda: {},
        )


class FakeCacheVerifier:
    def __init__(self, *, settings, counter=None):
        self.settings = settings
        self.counter = counter

    async def verify(self, *, goal, final_bytes, trajectory, prev_before_bytes=None):
        return SimpleNamespace(verdict="PASS", reason="fake assertion", passed=True)


class FakeV3Locator:
    def __init__(self):
        self.calls = []

    @property
    def coord_space(self):
        return "normalized"

    async def locate_action(self, **kwargs):
        self.calls.append(kwargs)
        return V3LocateResult(
            action={
                "index": kwargs["action"].get("index"),
                "type": "click",
                "point": {"x": 111, "y": 222},
                "plan_intent": kwargs["action"].get("plan_intent"),
            },
            reason="found target",
        )


class FakeV3LocatorMissThenHit:
    def __init__(self, *, miss_count: int = 1):
        self.calls = []
        self.miss_count = miss_count

    @property
    def coord_space(self):
        return "normalized"

    async def locate_action(self, **kwargs):
        from ai_phone.server.trajectory_cache.v3_replay import V3LocatorMiss

        self.calls.append(kwargs)
        if len(self.calls) <= self.miss_count:
            raise V3LocatorMiss("无")
        return V3LocateResult(
            action={
                "index": kwargs["action"].get("index"),
                "type": kwargs["action"].get("type"),
                "point": {"x": 333, "y": 444},
                "plan_intent": kwargs["action"].get("plan_intent"),
            },
            reason="found after rescue",
        )


class FakeV3Rescue:
    def __init__(self, decision):
        self.decisions = list(decision) if isinstance(decision, list) else [decision]
        self.calls = []

    def is_configured(self):
        return True

    def configuration_problem(self):
        return ""

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls), len(self.decisions)) - 1
        return self.decisions[index]


@pytest.mark.asyncio
async def test_save_trajectory_cache_from_run_steps(monkeypatch, _test_engine, session):
    from ai_phone.server.trajectory_cache import service as service_module

    settings = Settings(_env_file=None, trajectory_cache_ephemeral_action_enabled=False)
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-cache-ok",
        device_serial="D1",
        goal="  打开　微信\n发送 hello  ",
        status="success",
        engine="vlm",
    )
    session.add(run)
    session.add_all(
        [
            RunStep(
                run_id=run.id,
                step=1,
                action="click(point='<point>500 250</point>')",
                action_type="click",
            ),
            RunStep(
                run_id=run.id,
                step=2,
                action="type(content='hello')",
                action_type="type",
            ),
            RunStep(
                run_id=run.id,
                step=3,
                action="finished(content='done')",
                action_type="finished",
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    assert cache_key
    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    assert row.device_code == "D1"
    assert row.run_semantic_text == "打开 微信 发送 hello"
    actions = row.trajectory_json["actions"]
    assert actions == [
        {
            "index": 1,
            "source": "run_step",
            "raw": "click(point='<point>500 250</point>')",
            "type": "click",
            "point": {"x": 500, "y": 500},
            "coord_mode": "absolute",
            "intent": "打开 微信 发送 hello",
            "label": "微信 发送 hello",
            "source_step": 1,
            "action_id": "a001",
            "chain_index": 1,
        },
        {
            "index": 2,
            "source": "run_step",
            "raw": "type(content='hello')",
            "type": "type",
            "content": "hello",
            "source_step": 2,
            "action_id": "a002",
            "chain_index": 1,
        },
    ]
    assert row.trajectory_json["schema_version"] == 2
    landmarks = row.trajectory_json["state_landmarks"]
    assert landmarks[0]["action_id"] == "a001"
    assert landmarks[0]["before_action_id"] == "a002"
    assert landmarks[0]["status"] == "unavailable"
    assert landmarks[0]["missing_reason"] == "image_url_empty"

    hit = await get_active_trajectory_cache(
        db_module.get_session_factory(),
        device_code="D1",
        run_semantic_text="打开 微信 发送 hello",
    )
    miss = await get_active_trajectory_cache(
        db_module.get_session_factory(),
        device_code="D2",
        run_semantic_text="打开 微信 发送 hello",
    )
    assert hit and hit["cache_key"] == cache_key
    assert miss is None


def test_build_v3_cache_payload_adds_plan_intent_and_preserves_optional_role():
    payload = build_v3_cache_payload(
        {
            "schema_version": 2,
            "cache_key": "k3",
            "device_code": "D1",
            "run_semantic_hash": "h",
            "run_semantic_text": "点击教材同步",
            "source_run_id": "run-v2",
            "source_vlm_backend": "doubao_responses",
            "actions": [
                {
                    "index": 1,
                    "action_id": "a001",
                    "type": "click",
                    "label": "推荐意愿调查弹窗关闭按钮",
                    "role": "optional_ephemeral",
                    "ephemeral_meta": {"category": "marketing_popup"},
                    "point": {"x": 900, "y": 1200},
                },
                {
                    "index": 2,
                    "action_id": "a002",
                    "type": "click",
                    "label": "教材同步",
                    "point": {"x": 700, "y": 800},
                },
                {
                    "index": 3,
                    "action_id": "a003",
                    "type": "click",
                    "thought": "现在弹出了选择难度的窗口，要进入习题页，需要点击“开始挑战”按钮，这样就能进入对应的习题挑战页面了。",
                },
                {
                    "index": 4,
                    "action_id": "a004",
                    "type": "click",
                    "label": "底部标签页面",
                    "intent": "点击底部标签页面",
                    "thought": "当前存在遮挡层，需要点击「关闭」按钮。",
                },
                {
                    "index": 5,
                    "action_id": "a005",
                    "type": "click",
                    "label": "Bottom Tab",
                    "thought": "当前遮挡层已关闭，需要点击底部标签。",
                },
                {
                    "index": 6,
                    "action_id": "a006",
                    "type": "type",
                    "content": "hello",
                },
            ],
            "source_completion": {"assertion_pass": "已进入教材同步"},
        }
    )

    assert payload["mode"] == "v3"
    assert payload["schema_version"] == 3
    assert payload["actions"][0]["role"] == "optional_ephemeral"
    assert payload["actions"][0]["plan_intent"] == "点击推荐意愿调查弹窗关闭按钮"
    assert payload["actions"][1]["role"] == "business_required"
    assert payload["actions"][1]["plan_intent"] == "点击教材同步"
    assert payload["actions"][2]["plan_intent"] == (
        "现在弹出了选择难度的窗口，要进入习题页，需要点击“开始挑战”按钮，"
        "这样就能进入对应的习题挑战页面了"
    )
    assert payload["actions"][3]["plan_intent"] == "当前存在遮挡层，需要点击「关闭」按钮"
    assert payload["actions"][4]["plan_intent"] == "当前遮挡层已关闭，需要点击底部标签"
    assert payload["actions"][5]["plan_intent"] == "输入hello"
    assert payload["source_completion"]["assertion_pass"] == "已进入教材同步"


@pytest.mark.asyncio
async def test_save_v3_trajectory_cache_from_run_steps(monkeypatch, _test_engine, session):
    from ai_phone.server.trajectory_cache import service as service_module

    settings = Settings(_env_file=None, trajectory_cache_ephemeral_action_enabled=False)
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    session.add(Device(serial="D3", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-cache-v3-ok",
        device_serial="D3",
        goal="点击教材同步",
        status="success",
        engine="vlm",
        reason="已成功进入教材同步页面",
    )
    session.add(run)
    session.add_all(
        [
            RunStep(
                run_id=run.id,
                step=1,
                action="click(point='<point>721 806</point>')",
                action_type="click",
            ),
            RunStep(
                run_id=run.id,
                step=2,
                action="finished(content='done')",
                action_type="finished",
            ),
            RunLog(
                run_id=run.id,
                step=1,
                level=1,
                title="思考",
                content="现在需要点击教材同步卡片进入页面",
            ),
            RunLog(
                run_id=run.id,
                step=2,
                level=1,
                title="断言系统 · 通过",
                content="附图显示已进入教材同步页面",
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_v3_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    assert cache_key
    row = (
        await session.execute(
            select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
        )
    ).scalars().one()
    assert row.schema_version == 3
    assert row.device_code == "D3"
    assert row.run_semantic_text == "点击教材同步"
    assert row.source_vlm_backend == "doubao_responses"
    assert row.actions_json[0]["plan_intent"] == "点击教材同步"
    assert row.actions_json[0]["role"] == "business_required"
    assert row.actions_json[0]["point"] == {"x": 721, "y": 1612}
    assert row.source_completion["assertion_pass"] == "附图显示已进入教材同步页面"

    hit = await get_active_trajectory_cache_v3(
        db_module.get_session_factory(),
        device_code="D3",
        run_semantic_text="点击教材同步",
    )
    assert hit and hit["cache_key"] == cache_key
    assert hit["actions"][0]["plan_intent"] == "点击教材同步"


@pytest.mark.asyncio
async def test_save_v3_trajectory_cache_skips_v3_cache_pass(_test_engine, session):
    run = Run(
        id="run-cache-v3-pass",
        device_serial="D3",
        goal="点击教材同步",
        status="success",
        reason="trajectory_cache_v3_pass: ok",
        requested_cache_mode="v3",
        effective_cache_mode="v3",
    )
    session.add(run)
    await session.commit()

    cache_key = await save_trajectory_cache_v3_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    assert cache_key is None
    rows = (
        await session.execute(
            select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.source_run_id == run.id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_mark_v3_cache_suspect_hides_it_from_active_lookup(_test_engine, session):
    run = Run(id="run-v3-suspect", device_serial="D3", goal="点击教材同步", status="running")
    cache_key, normalized, semantic_hash = build_cache_key(
        device_code="D3",
        run_semantic_text="点击教材同步",
        schema_version=3,
    )
    session.add(run)
    session.add(
        VlmTrajectoryCacheV3(
            cache_key=cache_key,
            device_code="D3",
            run_semantic_hash=semantic_hash,
            run_semantic_text=normalized,
            status="active",
            actions_json=[{"index": 1, "type": "click", "plan_intent": "点击教材同步"}],
        )
    )
    await session.commit()

    changed = await mark_trajectory_cache_v3_suspect(
        db_module.get_session_factory(),
        cache_key=cache_key,
        run_id=run.id,
        reason="assertion_fail",
    )

    assert changed == 1
    row = (
        await session.execute(
            select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == cache_key)
        )
    ).scalars().one()
    await session.refresh(row)
    assert row.status == "suspect"
    hit = await get_active_trajectory_cache_v3(
        db_module.get_session_factory(),
        device_code="D3",
        run_semantic_text="点击教材同步",
    )
    assert hit is None


@pytest.mark.asyncio
async def test_v3_replay_runner_relocates_click_by_plan_intent(monkeypatch):
    from ai_phone.server.trajectory_cache import v3_replay as v3_replay_module

    monkeypatch.setattr(
        v3_replay_module,
        "get_settings",
        lambda: SimpleNamespace(
            trajectory_cache_observe_delay_ms=0,
            trajectory_cache_ephemeral_gate_max_calls=3,
            trajectory_cache_v3_rescue_max_calls_per_replay=3,
        ),
    )
    stable_calls = []

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        stable_calls.append({"frame_a": frame_a_bytes, **kwargs})
        return SimpleNamespace(bytes_=b"stable-jpeg")

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_pixel", fake_wait_stable)
    driver = FakeDriver()
    locator = FakeV3Locator()
    logs = []

    async def log(level, title, content):
        logs.append((level, title, content))

    runner = V3ReplayRunner(
        driver=driver,
        trajectory={
            "run_semantic_text": "点击教材同步",
            "source_vlm_backend": "doubao_responses",
            "actions": [
                {
                    "index": 1,
                    "action_id": "a001",
                    "type": "click",
                    "point": {"x": 999, "y": 999},
                    "plan_intent": "点击教材同步",
                }
            ],
        },
        locator=locator,
        log=log,
        capture_after_each_action=True,
        goal="点击教材同步",
    )

    result = await runner.run()

    assert result.success is True
    assert result.actions_executed == 1
    assert ("click", 111, 222) in driver.calls
    assert len(stable_calls) == 2
    assert stable_calls[0]["use_cache_settings"] is True
    assert locator.calls[0]["action"]["plan_intent"] == "点击教材同步"
    assert any(title == "V3寻找目标" and "点击教材同步" in content for _level, title, content in logs)


@pytest.mark.asyncio
async def test_v3_replay_runner_skips_optional_ephemeral_when_gate_says_skip(
    monkeypatch,
    tmp_path,
):
    from ai_phone.server.trajectory_cache import v3_replay as v3_replay_module

    monkeypatch.setattr(
        v3_replay_module,
        "get_settings",
        lambda: SimpleNamespace(
            trajectory_cache_observe_delay_ms=0,
            trajectory_cache_ephemeral_gate_max_calls=3,
            trajectory_cache_v3_rescue_max_calls_per_replay=3,
        ),
    )

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        return SimpleNamespace(bytes_=b"stable-jpeg")

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_pixel", fake_wait_stable)
    popup_before = tmp_path / "popup-before.jpg"
    cached_after = tmp_path / "cached-after.jpg"
    popup_before.write_bytes(b"before")
    cached_after.write_bytes(b"after")
    gate = FakeEphemeralGate(EphemeralGateDecision(verdict=GATE_SKIP, reason="弹窗不存在"))
    driver = FakeDriver()

    runner = V3ReplayRunner(
        driver=driver,
        trajectory={
            "run_semantic_text": "点击教材同步",
            "source_vlm_backend": "doubao_responses",
            "actions": [
                {
                    "index": 1,
                    "action_id": "a001",
                    "type": "click",
                    "role": "optional_ephemeral",
                    "plan_intent": "关闭推荐弹窗",
                    "ephemeral_meta": {
                        "category": "marketing_popup",
                        "cached_popup_before_path": str(popup_before),
                        "cached_after_path": str(cached_after),
                    },
                }
            ],
        },
        locator=FakeV3Locator(),
        ephemeral_gate_verifier=gate,
    )

    result = await runner.run()

    assert result.success is True
    assert result.actions_executed == 0
    assert not any(call[0] == "click" for call in driver.calls)
    assert gate.calls[0]["action"]["plan_intent"] == "关闭推荐弹窗"


@pytest.mark.asyncio
async def test_v3_replay_runner_locates_type_input_before_typing(monkeypatch):
    from ai_phone.server.trajectory_cache import v3_replay as v3_replay_module

    monkeypatch.setattr(
        v3_replay_module,
        "get_settings",
        lambda: SimpleNamespace(
            trajectory_cache_observe_delay_ms=0,
            trajectory_cache_ephemeral_gate_max_calls=3,
            trajectory_cache_v3_rescue_max_calls_per_replay=3,
        ),
    )

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        return SimpleNamespace(bytes_=b"stable-jpeg")

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_pixel", fake_wait_stable)
    driver = FakeDriver()
    locator = FakeV3Locator()
    runner = V3ReplayRunner(
        driver=driver,
        trajectory={
            "run_semantic_text": "搜索咖啡",
            "source_vlm_backend": "doubao_responses",
            "actions": [{"index": 1, "type": "type", "content": "咖啡", "plan_intent": "输入咖啡"}],
        },
        locator=locator,
    )

    result = await runner.run()

    assert result.success is True
    assert locator.calls[0]["action"]["type"] == "click"
    assert ("click", 111, 222) in driver.calls
    assert ("type_text", "咖啡") in driver.calls


@pytest.mark.asyncio
async def test_v3_replay_runner_uses_rescue_wait_after_locator_miss(monkeypatch):
    from ai_phone.server.trajectory_cache import v3_replay as v3_replay_module

    monkeypatch.setattr(
        v3_replay_module,
        "get_settings",
        lambda: SimpleNamespace(
            trajectory_cache_observe_delay_ms=0,
            trajectory_cache_ephemeral_gate_max_calls=3,
            trajectory_cache_v3_rescue_max_calls_per_replay=3,
        ),
    )

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        return SimpleNamespace(bytes_=b"stable-jpeg")

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_pixel", fake_wait_stable)
    locator = FakeV3LocatorMissThenHit()
    rescue = FakeV3Rescue(V3RescueDecision(verdict="WAIT", reason="页面加载中", wait_ms=100))
    driver = FakeDriver()
    runner = V3ReplayRunner(
        driver=driver,
        trajectory={
            "run_semantic_text": "点击全部功能",
            "source_vlm_backend": "doubao_responses",
            "actions": [{"index": 1, "type": "click", "plan_intent": "点击全部功能"}],
        },
        locator=locator,
        rescue_verifier=rescue,
    )

    result = await runner.run()

    assert result.success is True
    assert len(locator.calls) == 2
    assert len(rescue.calls) == 1
    assert ("click", 333, 444) in driver.calls


@pytest.mark.asyncio
async def test_v3_replay_runner_keeps_rescuing_until_locator_hits(monkeypatch):
    from ai_phone.server.trajectory_cache import v3_replay as v3_replay_module

    monkeypatch.setattr(
        v3_replay_module,
        "get_settings",
        lambda: SimpleNamespace(
            trajectory_cache_observe_delay_ms=0,
            trajectory_cache_ephemeral_gate_max_calls=3,
            trajectory_cache_v3_rescue_max_calls_per_replay=3,
        ),
    )

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        return SimpleNamespace(bytes_=b"stable-jpeg")

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_pixel", fake_wait_stable)
    locator = FakeV3LocatorMissThenHit(miss_count=2)
    rescue = FakeV3Rescue(
        [
            V3RescueDecision(verdict="WAIT", reason="页面加载中", wait_ms=100),
            V3RescueDecision(verdict="WAIT", reason="继续加载", wait_ms=100),
        ]
    )
    driver = FakeDriver()
    runner = V3ReplayRunner(
        driver=driver,
        trajectory={
            "run_semantic_text": "点击全部功能",
            "source_vlm_backend": "doubao_responses",
            "actions": [{"index": 1, "type": "click", "plan_intent": "点击全部功能"}],
        },
        locator=locator,
        rescue_verifier=rescue,
    )

    result = await runner.run()

    assert result.success is True
    assert len(locator.calls) == 3
    assert len(rescue.calls) == 2
    assert ("click", 333, 444) in driver.calls


@pytest.mark.asyncio
async def test_v3_rescue_continue_replay_skips_current_action(monkeypatch):
    from ai_phone.server.trajectory_cache import v3_replay as v3_replay_module

    monkeypatch.setattr(
        v3_replay_module,
        "get_settings",
        lambda: SimpleNamespace(
            trajectory_cache_observe_delay_ms=0,
            trajectory_cache_ephemeral_gate_max_calls=3,
            trajectory_cache_v3_rescue_max_calls_per_replay=3,
        ),
    )

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        return SimpleNamespace(bytes_=b"stable-jpeg")

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_pixel", fake_wait_stable)
    locator = FakeV3LocatorMissThenHit(miss_count=1)
    rescue = FakeV3Rescue(
        V3RescueDecision(verdict="CONTINUE_REPLAY", reason="已在下一步页面")
    )
    driver = FakeDriver()
    runner = V3ReplayRunner(
        driver=driver,
        trajectory={
            "run_semantic_text": "点击卡片进入习题页",
            "source_vlm_backend": "doubao_responses",
            "actions": [{"index": 1, "type": "click", "plan_intent": "点击卡片"}],
        },
        locator=locator,
        rescue_verifier=rescue,
    )

    result = await runner.run()

    assert result.success is True
    assert result.actions_executed == 0
    assert len(locator.calls) == 1
    assert len(rescue.calls) == 1
    assert driver.calls == []


@pytest.mark.asyncio
async def test_save_trajectory_cache_prefers_command_params(_test_engine, session):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(id="run-cache-command", device_serial="D1", goal="tap real point", status="success")
    session.add(run)
    session.add(
        RunStep(
            run_id=run.id,
            step=1,
            action="click(point='<point>100 100</point>')",
            action_type="click",
            command_id="cmd-1",
        )
    )
    session.add(
        RunCommand(
            run_id=run.id,
            step=1,
            message_id="cmd-1",
            method="click",
            params={"x": 123, "y": 456},
            ok=True,
        )
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    assert row.trajectory_json["actions"][0]["source"] == "run_command"
    assert row.trajectory_json["actions"][0]["point"] == {"x": 123, "y": 456}
    assert row.trajectory_json["actions"][0]["intent"] == "tap real point"
    assert row.trajectory_json["actions"][0]["source_step"] == 1


@pytest.mark.asyncio
async def test_save_trajectory_cache_from_unlinked_run_commands(
    monkeypatch,
    _test_engine,
    session,
):
    from ai_phone.server.trajectory_cache import service as service_module

    settings = Settings(_env_file=None, trajectory_cache_ephemeral_action_enabled=False)
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(id="run-cache-unlinked-command", device_serial="D1", goal="tap sequence", status="success")
    session.add(run)
    session.add_all(
        [
            RunStep(run_id=run.id, step=1),
            RunStep(run_id=run.id, step=2),
            RunCommand(
                run_id=run.id,
                message_id="cmd-a",
                method="click",
                params={"x": 11, "y": 22},
                ok=True,
            ),
            RunCommand(
                run_id=run.id,
                message_id="cmd-b",
                method="click",
                params={"x": 33, "y": 44},
                ok=True,
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    assert row.trajectory_json["actions"] == [
        {
            "index": 1,
            "source": "run_command",
            "driver_method": "click",
            "message_id": "cmd-a",
            "type": "click",
            "point": {"x": 11, "y": 22},
            "coord_mode": "absolute",
            "intent": "tap sequence",
            "label": "tap sequence",
            "action_id": "a001",
            "chain_index": 1,
        },
        {
            "index": 2,
            "source": "run_command",
            "driver_method": "click",
            "message_id": "cmd-b",
            "type": "click",
            "point": {"x": 33, "y": 44},
            "coord_mode": "absolute",
            "action_id": "a002",
            "chain_index": 1,
        },
    ]


@pytest.mark.asyncio
async def test_save_trajectory_cache_labels_structured_precondition_commands(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    goal = (
        "测试标题：验证我的页面是否存在我的学校功能入口\n"
        "前置条件：关闭 App「洋葱学园」后重新打开 App「洋葱学园」\n"
        "操作步骤：点击底部【我的】tab，查看我的页面是否存在我的学校功能入口\n"
        "预期结果：我的页面存在我的学园功能入口"
    )
    run = Run(id="run-cache-structured-command", device_serial="D1", goal=goal, status="success")
    session.add(run)
    session.add_all(
        [
            RunStep(run_id=run.id, step=1),
            RunStep(run_id=run.id, step=2),
            RunCommand(
                run_id=run.id,
                message_id="cmd-close",
                method="terminate_app",
                params={"package_name": "com.yangcong345.android.phone"},
                ok=True,
            ),
            RunCommand(
                run_id=run.id,
                message_id="cmd-open",
                method="activate_app",
                params={"package_name": "com.yangcong345.android.phone"},
                ok=True,
            ),
            RunCommand(
                run_id=run.id,
                message_id="cmd-click",
                method="click",
                params={"x": 946, "y": 2284},
                ok=True,
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    actions = row.trajectory_json["actions"]
    assert actions[0]["intent"] == "关闭App（系统起跑线）"
    assert actions[0]["label"] == "com.yangcong345.android.phone"
    assert actions[1]["intent"] == "打开App（系统起跑线）"
    assert actions[1]["label"] == "com.yangcong345.android.phone"
    assert actions[2]["intent"] == "点击底部【我的】tab"
    assert actions[2]["label"] == "底部【我的】tab"


@pytest.mark.asyncio
async def test_save_trajectory_cache_uses_run_log_timeline_for_wait_and_commands(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-cache-timeline",
        device_serial="D1",
        goal="操作步骤：点击我的\n预期结果：显示我的页面",
        status="success",
    )
    session.add(run)
    session.add_all(
        [
            RunLog(run_id=run.id, step=1, level=1, title="动作", content="wait(seconds=3)"),
            RunLog(run_id=run.id, step=1, level=1, title="思考", content="应用正在启动，需要等待页面加载"),
            RunLog(run_id=run.id, step=1, level=1, title="执行完成", content="动作: wait, 耗时: 3001ms"),
            RunLog(
                run_id=run.id,
                step=2,
                level=1,
                title="动作",
                content="click(point='<point>876 952</point>')",
            ),
            RunLog(run_id=run.id, step=2, level=1, title="思考", content="下一步点击底部【我的】tab"),
            RunLog(run_id=run.id, step=2, level=1, title="执行完成", content="动作: click, 耗时: 994ms"),
            RunCommand(
                run_id=run.id,
                message_id="cmd-click",
                method="click",
                params={"x": 946, "y": 2284},
                ok=True,
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    actions = row.trajectory_json["actions"]
    assert actions[0]["type"] == "wait"
    assert actions[0]["seconds"] == 3
    assert actions[0]["source"] == "run_log"
    assert actions[0]["intent"] == "应用正在启动，需要等待页面加载"
    assert actions[1]["type"] == "click"
    assert actions[1]["source"] == "run_command"
    assert actions[1]["point"] == {"x": 946, "y": 2284}
    assert actions[1]["intent"] == "点击我的"
    assert actions[1]["thought"] == "下一步点击底部【我的】tab"


@pytest.mark.asyncio
async def test_save_trajectory_cache_landmark_missing_image_does_not_fail(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-cache-missing-landmark",
        device_serial="D1",
        goal="点击我的，点击学习",
        status="success",
    )
    session.add(run)
    session.add_all(
        [
            RunStep(
                run_id=run.id,
                step=1,
                action="click(point='<point>876 952</point>')",
                action_type="click",
            ),
            RunStep(
                run_id=run.id,
                step=2,
                action="click(point='<point>309 952</point>')",
                action_type="click",
                screenshot_before="/files/not-found/step2-before.jpg",
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    landmark = row.trajectory_json["state_landmarks"][0]
    assert landmark["action_id"] == "a001"
    assert landmark["image_url"] == "/files/not-found/step2-before.jpg"
    assert landmark["status"] == "unavailable"
    assert landmark["missing_reason"] == "image_not_found"


@pytest.mark.asyncio
async def test_save_trajectory_cache_does_not_use_action_after_as_final_handoff(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-final-after-unsafe",
        device_serial="D1",
        goal="点击入口并断言结果",
        status="success",
    )
    session.add(run)
    session.add(
        RunStep(
            run_id=run.id,
            step=1,
            action="click(point='<point>500 500</point>')",
            action_type="click",
            screenshot_after="/files/unsafe-action-after.jpg",
        )
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    landmark = row.trajectory_json["state_landmarks"][0]
    assert landmark["action_id"] == "a001"
    assert landmark["snapshot_step"] is None
    assert landmark["image_url"] == ""
    assert landmark["status"] == "unavailable"
    assert landmark["missing_reason"] == "final_handoff_snapshot_not_found"


@pytest.mark.asyncio
async def test_save_trajectory_cache_uses_timeline_for_system_prelude(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    goal = (
        "测试标题：验证入口\n"
        "前置条件：关闭 App「洋葱学园」后重新打开 App「洋葱学园」\n"
        "操作步骤：点击底部【我的】tab\n"
        "预期结果：我的页面展示"
    )
    run = Run(id="run-cache-timeline-prelude", device_serial="D1", goal=goal, status="success")
    session.add(run)
    session.add_all(
        [
            RunLog(run_id=run.id, step=1, level=1, title="关闭App（系统起跑线）", content="应用: 洋葱学园"),
            RunLog(run_id=run.id, step=1, level=1, title="执行完成", content="动作: close_app, 耗时: 1ms"),
            RunLog(run_id=run.id, step=2, level=1, title="打开App（系统起跑线）", content="应用: 洋葱学园"),
            RunLog(run_id=run.id, step=2, level=1, title="执行完成", content="动作: open_app, 耗时: 1ms"),
            RunCommand(
                run_id=run.id,
                message_id="cmd-close",
                method="terminate_app",
                params={"package_name": "com.yangcong345.android.phone"},
                ok=True,
            ),
            RunCommand(
                run_id=run.id,
                message_id="cmd-open",
                method="activate_app",
                params={"package_name": "com.yangcong345.android.phone"},
                ok=True,
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    actions = row.trajectory_json["actions"]
    assert actions[0]["type"] == "close_app"
    assert actions[0]["intent"] == "关闭App（系统起跑线）"
    assert actions[1]["type"] == "open_app"
    assert actions[1]["intent"] == "打开App（系统起跑线）"


@pytest.mark.asyncio
async def test_save_trajectory_cache_parses_claude_computer_actions(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1080, screen_height=2400))
    run = Run(
        id="run-cache-claude",
        device_serial="D1",
        goal="点击 Order Type",
        status="success",
        token_summary={"vlm_backend": "claude_cu"},
    )
    session.add(run)
    session.add_all(
        [
            RunLog(
                run_id=run.id,
                step=1,
                level=1,
                title="动作",
                content='computer.left_click({"action": "left_click", "coordinate": [181, 662]})',
            ),
            RunLog(
                run_id=run.id,
                step=1,
                level=1,
                title="思考",
                content="Click the Order Type selection area.",
            ),
            RunLog(run_id=run.id, step=1, level=1, title="执行完成", content="动作: click, 耗时: 368ms"),
            RunCommand(
                run_id=run.id,
                message_id="cmd-click",
                method="click",
                params={"x": 333, "y": 1333},
                ok=True,
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    actions = row.trajectory_json["actions"]
    assert row.trajectory_json["source_vlm_backend"] == "claude_cu"
    assert actions[0]["source"] == "run_command"
    assert actions[0]["type"] == "click"
    assert actions[0]["point"] == {"x": 333, "y": 1333}
    assert actions[0]["intent"] == "点击 Order Type"
    assert actions[0]["thought"] == "Click the Order Type selection area."


@pytest.mark.asyncio
async def test_save_trajectory_cache_parses_gpt_computer_actions_without_command(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1080, screen_height=2400))
    run = Run(
        id="run-cache-gpt",
        device_serial="D1",
        goal="输入金额并回车",
        status="success",
        token_summary={"vlm_backend": "gpt_cu"},
    )
    session.add(run)
    session.add_all(
        [
            RunLog(
                run_id=run.id,
                step=1,
                level=1,
                title="动作",
                content='computer.type({"type": "type", "text": "100"})',
            ),
            RunLog(run_id=run.id, step=1, level=1, title="执行完成", content="动作: type, 耗时: 100ms"),
            RunLog(
                run_id=run.id,
                step=2,
                level=1,
                title="动作",
                content='computer.keypress({"type": "keypress", "keys": ["Return"]})',
            ),
            RunLog(run_id=run.id, step=2, level=1, title="执行完成", content="动作: key_event, 耗时: 100ms"),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    actions = row.trajectory_json["actions"]
    assert row.trajectory_json["source_vlm_backend"] == "gpt_cu"
    assert actions[0]["type"] == "type"
    assert actions[0]["content"] == "100"
    assert actions[1]["type"] == "key_event"
    assert actions[1]["keycode"] == 66


@pytest.mark.asyncio
async def test_save_trajectory_cache_falls_back_when_command_params_missing(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(id="run-cache-old-command", device_serial="D1", goal="tap fallback", status="success")
    session.add(run)
    session.add(
        RunStep(
            run_id=run.id,
            step=1,
            action="click(point='<point>500 250</point>')",
            action_type="click",
            command_id="cmd-old",
        )
    )
    session.add(
        RunCommand(
            run_id=run.id,
            step=1,
            message_id="cmd-old",
            method="click",
            params={},
            ok=True,
        )
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    assert row.trajectory_json["actions"][0]["source"] == "run_step"
    assert row.trajectory_json["actions"][0]["point"] == {"x": 500, "y": 500}


@pytest.mark.asyncio
async def test_delete_trajectory_cache_for_run_is_idempotent(_test_engine, session):
    session.add(Device(serial="D1", platform="android"))
    run = Run(id="run-cache-fail", device_serial="D1", goal="same goal", status="failed")
    session.add(run)
    cache_key, normalized, semantic_hash = build_cache_key(
        device_code="D1",
        run_semantic_text="same goal",
    )
    session.add(
        VlmTrajectoryCache(
            cache_key=cache_key,
            device_code="D1",
            run_semantic_hash=semantic_hash,
            run_semantic_text=normalized,
            status="active",
            trajectory_json={"actions": []},
        )
    )
    await session.commit()

    deleted = await delete_trajectory_cache_for_run(db_module.get_session_factory(), run.id)
    deleted_again = await delete_trajectory_cache_for_run(db_module.get_session_factory(), run.id)

    assert deleted == 1
    assert deleted_again == 0


@pytest.mark.asyncio
async def test_replay_action_dispatcher_calls_driver_methods():
    driver = FakeDriver()
    dispatcher = ReplayActionDispatcher(driver)

    await dispatcher.execute({"type": "click", "point": {"x": 1, "y": 2}})
    await dispatcher.execute({"type": "double_tap", "point": {"x": 3, "y": 4}})
    await dispatcher.execute(
        {"type": "long_press", "point": {"x": 5, "y": 6}, "duration_ms": 700}
    )
    await dispatcher.execute({"type": "type", "content": "hello"})
    await dispatcher.execute(
        {"type": "scroll", "direction": "down", "center": {"x": 7, "y": 8}, "amount": 2}
    )
    await dispatcher.execute(
        {"type": "drag", "start": {"x": 9, "y": 10}, "end": {"x": 11, "y": 12}}
    )
    await dispatcher.execute({"type": "open_app", "package_name": "com.demo"})
    await dispatcher.execute({"type": "close_app", "app_name": "com.demo"})
    await dispatcher.execute({"type": "press_home"})
    await dispatcher.execute({"type": "press_back"})
    await dispatcher.execute({"type": "key_event", "keycode": 66})

    assert driver.calls == [
        ("click", 1, 2),
        ("double_click", 3, 4, 100),
        ("long_press", 5, 6, 700),
        ("type_text", "hello"),
        ("scroll", "down", (7, 8), 2),
        ("swipe", 9, 10, 11, 12, 500),
        ("activate_app", "com.demo"),
        ("terminate_app", "com.demo"),
        ("press_home",),
        ("press_back",),
        ("press_keycode", 66),
    ]


@pytest.mark.asyncio
async def test_replay_action_log_includes_intent():
    logs = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {
                    "index": 1,
                    "type": "click",
                    "intent": "点击学习",
                    "point": {"x": 333, "y": 2284},
                }
            ]
        },
        log=log,
        observe_delay_ms=0,
    )
    async def stable():
        return SimpleNamespace(bytes_=b"jpeg")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert any("intent=点击学习" in content for _level, _title, content in logs)


@pytest.mark.asyncio
async def test_replay_runner_logs_observe_delay(monkeypatch):
    logs = []
    sleeps = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    runner = ReplayRunner(
        driver=driver,
        trajectory={"actions": [{"index": 1, "type": "click", "point": {"x": 1, "y": 2}}]},
        log=log,
        observe_delay_ms=500,
    )

    async def stable():
        return SimpleNamespace(bytes_=b"jpeg")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert sleeps == [0.5]
    assert any(title == "轨迹缓存观察延迟" and "500ms" in content for _level, title, content in logs)


@pytest.mark.asyncio
async def test_replay_runner_ephemeral_gate_skip_does_not_click(tmp_path):
    logs = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import ReplayRunner

    before_path = tmp_path / "popup_before.jpg"
    after_path = tmp_path / "after.jpg"
    before_path.write_bytes(b"popup")
    after_path.write_bytes(b"after")

    async def log(level, title, content):
        logs.append((level, title, content))

    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {
                    "index": 1,
                    "action_id": "a001",
                    "type": "click",
                    "point": {"x": 10, "y": 20},
                    "role": "optional_ephemeral",
                    "ephemeral_meta": {
                        "category": "marketing_popup",
                        "cached_popup_before_path": str(before_path),
                        "cached_after_path": str(after_path),
                    },
                }
            ]
        },
        log=log,
        observe_delay_ms=0,
        ephemeral_gate_verifier=FakeEphemeralGate(
            EphemeralGateDecision(verdict=GATE_SKIP, reason="当前无同类弹窗")
        ),
    )

    async def stable():
        return SimpleNamespace(bytes_=b"current")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert result.actions_executed == 0
    assert not any(call[0] == "click" for call in driver.calls)
    assert any(
        title == "轨迹缓存瞬态动作" and "verdict=SKIP" in content
        for _level, title, content in logs
    )


@pytest.mark.asyncio
async def test_replay_runner_ephemeral_gate_repair_executes_new_click(tmp_path):
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import ReplayRunner

    before_path = tmp_path / "popup_before.jpg"
    after_path = tmp_path / "after.jpg"
    before_path.write_bytes(b"popup")
    after_path.write_bytes(b"after")

    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {
                    "index": 1,
                    "action_id": "a001",
                    "type": "click",
                    "point": {"x": 10, "y": 20},
                    "role": "optional_ephemeral",
                    "ephemeral_meta": {
                        "category": "marketing_popup",
                        "cached_popup_before_path": str(before_path),
                        "cached_after_path": str(after_path),
                    },
                }
            ]
        },
        observe_delay_ms=0,
        ephemeral_gate_verifier=FakeEphemeralGate(
            EphemeralGateDecision(
                verdict=GATE_EXECUTE_REPAIR,
                reason="关闭按钮位置变化",
                repair_action={"type": "click", "point": {"x": 500, "y": 500}},
                coord_space="normalized",
            )
        ),
    )

    async def stable():
        return SimpleNamespace(bytes_=b"current")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert result.actions_executed == 1
    assert ("click", 500, 1000) in driver.calls
    assert ("click", 10, 20) not in driver.calls


@pytest.mark.asyncio
async def test_replay_runner_alignment_match_skips_stability(monkeypatch):
    logs = []
    sleeps = []
    stable_calls = 0
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": True,
            "global_diff": 0.0,
            "center_mae": 0.0,
            "black_ratio_diff": 0.0,
            "reason": "match",
        },
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
                {"index": 2, "action_id": "a002", "type": "click", "point": {"x": 3, "y": 4}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": 2,
                    "status": "available",
                    "image_phash": "01",
                },
                {
                    "action_id": "a002",
                    "before_action_index": None,
                    "status": "available",
                    "image_phash": "01",
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
    )
    runner.alignment_enabled = True
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert stable_calls == 1
    assert sleeps == [0.5, 0.5]
    assert any("对齐成功 action_id=a001" in content for _level, title, content in logs if title == "轨迹缓存状态路标")
    assert any("复用上一 action 路标帧作为 #2 before" in content for _level, title, content in logs if title == "轨迹缓存状态路标")


@pytest.mark.asyncio
async def test_replay_runner_alignment_also_handles_wait_action(monkeypatch):
    sleeps = []
    stable_calls = 0
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": True,
            "global_diff": 0.0,
            "center_mae": 0.0,
            "black_ratio_diff": 0.0,
            "reason": "match",
        },
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "wait", "seconds": 3},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": None,
                    "status": "available",
                    "image_phash": "01",
                },
            ],
        },
        capture_after_each_action=True,
        observe_delay_ms=500,
    )
    runner.alignment_enabled = True
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert stable_calls == 1
    assert sleeps == [3, 0.5]


@pytest.mark.asyncio
async def test_replay_runner_alignment_miss_retries_then_stops_replay(monkeypatch):
    logs = []
    sleeps = []
    stable_calls = 0
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": False,
            "global_diff": 0.01,
            "center_mae": 0.84,
            "black_ratio_diff": 0.0,
            "reason": "center>0.2500",
        },
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": None,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is False
    assert result.error and "alignment_miss action_id=a001" in result.error
    assert stable_calls == 1
    assert sleeps == [0.5, 0.3, 0.2]
    assert any("开始对比 action_id=a001" in content and "历史间隔=600ms" in content for _level, title, content in logs if title == "轨迹缓存状态路标")
    assert any("与缓存路标不一致 action_id=a001" in content and "开始按历史窗口等待" in content for _level, title, content in logs if title == "轨迹缓存状态路标")
    assert any("轨迹偏航，终止缓存回放" in content for _level, title, content in logs if title == "轨迹缓存状态路标")


@pytest.mark.asyncio
async def test_cache_assertion_verifier_uses_assistant():
    assistant = FakeAssistant("PASS: 首页展示正确")
    settings = Settings(
        assistant_api_key="key",
        assistant_api_url="https://example.test",
        assistant_model="model",
        assistant_thinking_assertion=True,
    )
    verifier = CacheReplayAssertionVerifier(settings=settings, assistant=assistant)

    result = await verifier.verify(
        goal="打开应用并确认首页展示",
        final_bytes=b"jpeg",
        prev_before_bytes=b"before",
        trajectory={"actions": [{"index": 1, "type": "click", "point": {"x": 1, "y": 2}}]},
    )

    assert result.verdict == "PASS"
    assert assistant.calls
    assert assistant.calls[0]["final_bytes"] == b"jpeg"
    assert assistant.calls[0]["prev_before_bytes"] == b"before"
    assert assistant.calls[0]["thinking"] is True


def test_cache_assertion_prompt_has_free_and_structured_modes():
    free_prompt = build_cache_assertion_prompt(
        goal="点击我的，点击学习",
        trajectory={"actions": [{"index": 1, "type": "click", "intent": "点击学习"}]},
        has_prev=True,
    )
    structured_prompt = build_cache_assertion_prompt(
        goal="测试标题：验证入口\n操作步骤：点击我的\n预期结果：显示我的页面",
        trajectory={"actions": [{"index": 1, "type": "click", "intent": "点击我的"}]},
        has_prev=True,
    )

    assert "最后一个动作" in free_prompt
    assert "结构化测试用例" in structured_prompt
    assert "intent=点击学习" in free_prompt
    assert "首次成功语义锚点" in structured_prompt


@pytest.mark.asyncio
async def test_cache_assertion_verifier_missing_config_skips_without_assistant_call():
    assistant = FakeAssistant("PASS: should not call")
    settings = Settings(
        vlm_api_key="",
        assistant_api_key="",
        assistant_api_url="",
        assistant_model="",
    )
    verifier = CacheReplayAssertionVerifier(settings=settings, assistant=assistant)

    result = await verifier.verify(goal="goal", final_bytes=b"jpeg", trajectory={})

    assert result.verdict == "SKIP"
    assert assistant.calls == []


@pytest.mark.asyncio
async def test_server_runner_cache_replay_is_disabled_by_default(monkeypatch, _test_engine):
    import ai_phone.server.runner.service as service_module

    monkeypatch.setattr(
        service_module,
        "get_settings",
        lambda: SimpleNamespace(vlm_trajectory_cache_replay_enabled=False),
    )
    service = ServerRunnerService(
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
        waiter=DriverRpcWaiter(),
    )

    handled = await service._maybe_run_trajectory_cache(
        run_id="run-disabled",
        goal="goal",
        driver=FakeDriver(),
        emitter=FakeEmitter(),
    )

    assert handled is False


@pytest.mark.asyncio
async def test_server_runner_cache_replay_finishes_when_assertion_passes(
    monkeypatch,
    _test_engine,
    session,
):
    import ai_phone.server.runner.service as service_module
    import ai_phone.server.trajectory_cache as trajectory_cache_module

    settings = SimpleNamespace(
        vlm_trajectory_cache_replay_enabled=True,
        trajectory_cache_enabled=True,
        assistant_api_key="key",
        assistant_api_url="https://example.test",
        assistant_model="model",
        vlm_api_key="",
        assistant_thinking_assertion=True,
        assertion_timeout_sec=1,
        trajectory_cache_recovery_vlm_enabled=False,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(trajectory_cache_module, "ReplayRunner", FakeReplayRunner)
    monkeypatch.setattr(
        trajectory_cache_module,
        "CacheReplayAssertionVerifier",
        FakeCacheVerifier,
    )

    run = Run(
        id="run-replay-ok",
        device_serial="D1",
        goal="cached goal",
        status="running",
        requested_cache_mode="v2",
        effective_cache_mode="v2",
    )
    cache_key, normalized, semantic_hash = build_cache_key(
        device_code="D1",
        run_semantic_text="cached goal",
    )
    session.add(run)
    session.add(
        VlmTrajectoryCache(
            cache_key=cache_key,
            device_code="D1",
            run_semantic_hash=semantic_hash,
            run_semantic_text=normalized,
            status="active",
            trajectory_json={"actions": [{"index": 1, "type": "click", "point": {"x": 1, "y": 2}}]},
        )
    )
    await session.commit()

    emitter = FakeEmitter()
    service = ServerRunnerService(
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
        waiter=DriverRpcWaiter(),
    )

    handled = await service._maybe_run_trajectory_cache(
        run_id=run.id,
        goal="cached goal",
        driver=FakeDriver(),
        emitter=emitter,
    )

    assert handled is True
    assert len(emitter.finishes) == 1
    finish = emitter.finishes[0]
    assert finish["result"] == "pass"
    assert finish["message"] == "trajectory_cache_pass: fake assertion"
    # 缓存通道收口必须把 elapsed_ms / steps 透传给 emitter，避免历史 bug：
    # force_finish 硬编码 0 → "任务总耗时" / "执行步数" 在缓存回放归零。
    assert finish["elapsed_ms"] >= 0
    assert finish["steps"] == 1
    # FakeCacheVerifier 没真正调 LLM，counter 全 0，token_stats 应为空 dict。
    assert finish["token_stats"] == {}
    assert [event.get("title") for event in emitter.events] == [
        "轨迹缓存",
        "fake replay",
        "轨迹缓存断言",
    ]


@pytest.mark.asyncio
async def test_server_runner_v3_cache_replay_finishes_when_assertion_passes(
    monkeypatch,
    _test_engine,
    session,
):
    import ai_phone.server.runner.service as service_module
    import ai_phone.server.trajectory_cache as trajectory_cache_module

    settings = SimpleNamespace(
        trajectory_cache_enabled=True,
        vlm_backend="doubao_responses",
        assistant_api_key="key",
        assistant_api_url="https://example.test",
        assistant_model="model",
        assistant_thinking_assertion=True,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(trajectory_cache_module, "V3ReplayRunner", FakeReplayRunner)
    monkeypatch.setattr(
        trajectory_cache_module,
        "CacheReplayAssertionVerifier",
        FakeCacheVerifier,
    )

    run = Run(
        id="run-v3-replay-ok",
        device_serial="D1",
        goal="cached goal",
        status="running",
        requested_cache_mode="v3",
        effective_cache_mode="v3",
    )
    cache_key, normalized, semantic_hash = build_cache_key(
        device_code="D1",
        run_semantic_text="cached goal",
        schema_version=3,
    )
    session.add(run)
    session.add(
        VlmTrajectoryCacheV3(
            cache_key=cache_key,
            device_code="D1",
            run_semantic_hash=semantic_hash,
            run_semantic_text=normalized,
            status="active",
            actions_json=[
                {
                    "index": 1,
                    "type": "click",
                    "plan_intent": "点击缓存目标",
                }
            ],
        )
    )
    await session.commit()

    emitter = FakeEmitter()
    service = ServerRunnerService(
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
        waiter=DriverRpcWaiter(),
    )

    handled = await service._maybe_run_trajectory_cache(
        run_id=run.id,
        goal="cached goal",
        driver=FakeDriver(),
        emitter=emitter,
    )

    assert handled is True
    assert len(emitter.finishes) == 1
    assert emitter.finishes[0]["result"] == "pass"
    assert emitter.finishes[0]["message"] == "trajectory_cache_v3_pass: fake assertion"
    assert [event.get("title") for event in emitter.events] == [
        "V3缓存回放",
        "fake replay",
        "V3最终校验",
    ]


@pytest.mark.asyncio
async def test_server_runner_cache_alignment_miss_finishes_as_assert_fail(
    monkeypatch,
    _test_engine,
    session,
):
    import ai_phone.server.runner.service as service_module
    import ai_phone.server.trajectory_cache as trajectory_cache_module

    settings = SimpleNamespace(
        vlm_trajectory_cache_replay_enabled=True,
        trajectory_cache_enabled=True,
        assistant_api_key="key",
        assistant_api_url="https://example.test",
        assistant_model="model",
        vlm_api_key="",
        assistant_thinking_assertion=True,
        assertion_timeout_sec=1,
        trajectory_cache_recovery_vlm_enabled=False,
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(trajectory_cache_module, "ReplayRunner", FakeAlignmentMissReplayRunner)

    run = Run(
        id="run-replay-align-miss",
        device_serial="D1",
        goal="cached goal",
        status="running",
        requested_cache_mode="v2",
        effective_cache_mode="v2",
    )
    cache_key, normalized, semantic_hash = build_cache_key(
        device_code="D1",
        run_semantic_text="cached goal",
    )
    session.add(run)
    session.add(
        VlmTrajectoryCache(
            cache_key=cache_key,
            device_code="D1",
            run_semantic_hash=semantic_hash,
            run_semantic_text=normalized,
            status="active",
            trajectory_json={"actions": [{"index": 1, "type": "click", "point": {"x": 1, "y": 2}}]},
        )
    )
    await session.commit()

    emitter = FakeEmitter()
    service = ServerRunnerService(
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=db_module.get_session_factory(),
        waiter=DriverRpcWaiter(),
    )

    handled = await service._maybe_run_trajectory_cache(
        run_id=run.id,
        goal="cached goal",
        driver=FakeDriver(),
        emitter=emitter,
    )

    assert handled is True
    assert len(emitter.finishes) == 1
    finish = emitter.finishes[0]
    assert finish["result"] == "assert_fail"
    assert (
        finish["message"]
        == "trajectory_cache_alignment_fail: index=1 type=click error=alignment_miss action_id=a001 elapsed=1000/1000ms"
    )
    assert finish["error_class"] == "TrajectoryCacheAlignmentError"
    assert finish["error_category"] == "model"
    # 失败分支也要带上 elapsed_ms / steps，避免 RunLog 报告归零；
    # 断言 VLM 没被调用，token_stats 应为空。
    assert finish["elapsed_ms"] >= 0
    assert finish["steps"] == 0
    assert finish["token_stats"] == {}


# ---------------------------------------------------------------------------
# v2 缓存回放 · recovery_vlm 三态裁决专线
# ---------------------------------------------------------------------------


class FakeRecoveryVerifier:
    """测试用 verifier。

    构造时塞一串预设 ``RecoveryDecision``，``verify_alignment_miss`` 按顺序
    弹出。``configured`` 控制是否被 ReplayRunner 视为可用通道。
    """

    def __init__(
        self,
        decisions,
        *,
        configured: bool = True,
        max_wait_more: int = 1,
        default_wait_ms: int = 1500,
    ):
        self._decisions = list(decisions)
        self._configured = configured
        self.max_wait_more = max_wait_more
        self.default_wait_ms = default_wait_ms
        self.calls: list[dict] = []

    def is_configured(self) -> bool:
        return self._configured

    def configuration_problem(self) -> str:
        return "" if self._configured else "fake_not_configured"

    async def verify_alignment_miss(self, **kwargs):
        self.calls.append(kwargs)
        if not self._decisions:
            return RecoveryDecision(
                verdict=VERDICT_ASSERT_FAIL,
                reason="fake decisions exhausted",
            )
        return self._decisions.pop(0)


def test_parse_recovery_response_continue():
    decision = parse_recovery_response("CONTINUE_REPLAY: 资源位变化，主结构一致")
    assert decision.verdict == VERDICT_CONTINUE
    assert "资源位" in decision.reason


def test_parse_recovery_response_assert_fail():
    decision = parse_recovery_response("ASSERT_FAIL: 跳错页面")
    assert decision.verdict == VERDICT_ASSERT_FAIL
    assert decision.reason == "跳错页面"


def test_parse_recovery_response_wait_more_with_ms():
    decision = parse_recovery_response("WAIT_MORE: 800: 仍在加载骨架屏")
    assert decision.verdict == VERDICT_WAIT_MORE
    assert decision.wait_ms == 800
    assert "骨架屏" in decision.reason


def test_parse_recovery_response_wait_more_default_when_no_ms():
    decision = parse_recovery_response("WAIT_MORE: 页面未稳定", default_wait_ms=1234)
    assert decision.verdict == VERDICT_WAIT_MORE
    assert decision.wait_ms == 1234
    assert decision.reason == "页面未稳定"


def test_parse_recovery_response_wait_more_clamps_extreme_ms():
    decision = parse_recovery_response("WAIT_MORE: 99999: too long")
    assert decision.verdict == VERDICT_WAIT_MORE
    assert decision.wait_ms == 10_000


def test_parse_recovery_response_protocol_violation_falls_back_to_fail():
    decision = parse_recovery_response("继续就行")
    assert decision.verdict == VERDICT_ASSERT_FAIL
    assert decision.error == "protocol_violation"
    assert "继续就行" in decision.reason


def test_parse_recovery_response_doubao_finished_maps_to_continue():
    decision = parse_recovery_response(
        "Thought: 页面结构一致，只是动态内容变化，可以继续回放。\n"
        "Action: finished(content='当前差异可接受')"
    )

    assert decision.verdict == VERDICT_CONTINUE
    assert decision.reason == "当前差异可接受"
    assert decision.parsed_actions[0].action == "finished"


def test_parse_recovery_response_doubao_click_maps_to_repair_action():
    decision = parse_recovery_response(
        "Thought: 当前入口位置变化，重新点击当前步骤目标。\n"
        "Action: click(point='<point>500 600</point>')"
    )

    assert decision.verdict == VERDICT_REPAIR_ACTION
    assert decision.thought == "当前入口位置变化，重新点击当前步骤目标。"
    assert decision.parsed_actions[0].action == "click"
    assert decision.parsed_actions[0].point == [500, 600]


def test_parse_recovery_response_strips_claude_markdown_bold():
    """Claude 偶发会用 markdown 加粗 ``**Action:**``，预清洗后应能命中。"""
    decision = parse_recovery_response(
        "**Thought:** 入口位置变化，重新点击。\n"
        "**Action:** click(point='<point>520 740</point>')"
    )
    assert decision.verdict == VERDICT_REPAIR_ACTION
    assert decision.parsed_actions[0].action == "click"
    assert decision.parsed_actions[0].point == [520, 740]


def test_parse_recovery_response_strips_inline_code_keyword():
    """GPT 偶发会用 inline code 包关键字 ``\u0060Action:\u0060``，预清洗后应能命中。"""
    decision = parse_recovery_response(
        "`Thought:` 页面仍在加载骨架屏，需要再等一段。\n"
        "`Action:` wait(seconds=2)"
    )
    assert decision.verdict == VERDICT_WAIT_MORE
    assert decision.wait_ms == 2000


def test_parse_recovery_response_strips_code_fence_wrapper():
    """三家偶发会把 Thought / Action 放在 ```` ```python ```` 代码块里。"""
    decision = parse_recovery_response(
        "```python\n"
        "Thought: 跳错页面了，本次轨迹无法继续。\n"
        "Action: assert_fail(content='页面跳到了登录页')\n"
        "```"
    )
    assert decision.verdict == VERDICT_ASSERT_FAIL
    assert "登录" in decision.reason


def test_parse_recovery_response_overrides_coord_space_for_absolute():
    """recovery 在 claude_cu / gpt_cu 下应把 ParsedAction.coord_space 覆写为
    absolute，否则下游 ReplayRunner 会按 0-1000 反算，坐标全错。"""
    decision = parse_recovery_response(
        "Thought: 重新点击当前控件。\n"
        "Action: click(point='<point>540 1024</point>')",
        coord_space="absolute",
    )
    assert decision.verdict == VERDICT_REPAIR_ACTION
    assert decision.parsed_actions[0].coord_space == "absolute"
    assert decision.parsed_actions[0].point == [540, 1024]


def test_parse_recovery_response_default_coord_space_is_normalized():
    """未传 coord_space 时保持豆包系默认行为（normalized）。"""
    decision = parse_recovery_response(
        "Thought: 重新点击。\nAction: click(point='<point>500 500</point>')"
    )
    assert decision.parsed_actions[0].coord_space == "normalized"


@pytest.mark.parametrize(
    "raw, expected_verdict, extra_check",
    [
        (
            "Thought: 当前差异可接受。\nAction: finished(content='ok')",
            VERDICT_CONTINUE,
            None,
        ),
        (
            "Thought: 还在加载。\nAction: wait(seconds=2)",
            VERDICT_WAIT_MORE,
            lambda d: d.wait_ms == 2000,
        ),
        (
            "Thought: 入口位置变化。\nAction: click(point='<point>540 1024</point>')",
            VERDICT_REPAIR_ACTION,
            lambda d: d.parsed_actions[0].coord_space == "absolute"
            and d.parsed_actions[0].point == [540, 1024],
        ),
        (
            "Thought: 跳错页面。\nAction: assert_fail(content='wrong page')",
            VERDICT_ASSERT_FAIL,
            None,
        ),
    ],
    ids=["CONTINUE", "WAIT_MORE", "REPAIR_ACTION", "ASSERT_FAIL"],
)
def test_parse_recovery_response_absolute_covers_all_four_verdicts(
    raw, expected_verdict, extra_check
):
    """Layer D：absolute 坐标空间下，CONTINUE / WAIT_MORE / REPAIR / ASSERT_FAIL
    四种 verdict 都必须解析正确——确保 C-3 的 coord_space 覆写不会误改非
    REPAIR 路径的语义。三家海外 backend (claude_cu / gpt_cu) 共用此路径。
    """
    decision = parse_recovery_response(raw, coord_space="absolute")
    assert decision.verdict == expected_verdict
    if extra_check is not None:
        assert extra_check(decision), f"extra_check failed for {expected_verdict}"


def test_recovery_verifier_coord_space_dispatch_by_backend():
    """verifier.coord_space 必须按主 VLM backend 推断。"""
    s = Settings(trajectory_cache_recovery_vlm_enabled=False)
    assert (
        CacheReplayRecoveryVerifier(settings=s, main_vlm_backend="doubao_responses").coord_space
        == "normalized"
    )
    assert (
        CacheReplayRecoveryVerifier(settings=s, main_vlm_backend="claude_cu").coord_space
        == "absolute"
    )
    assert (
        CacheReplayRecoveryVerifier(settings=s, main_vlm_backend="gpt_cu").coord_space
        == "absolute"
    )
    # 未知 / 自部署 backend → 兜底 normalized，保护现网豆包行为
    assert CacheReplayRecoveryVerifier(settings=s, main_vlm_backend="").coord_space == "normalized"
    assert (
        CacheReplayRecoveryVerifier(settings=s, main_vlm_backend="custom_proxy").coord_space
        == "normalized"
    )


def test_build_recovery_prompt_coord_space_block_switches():
    """prompt 里坐标系说明段必须按 coord_space 切换文案。"""
    common_kwargs = dict(
        goal="g",
        trajectory={"actions": []},
        action={"action_id": "a001", "type": "click"},
        landmark={"action_id": "a001"},
        metrics={"global_diff": 0.04},
        elapsed_ms=1300,
        max_wait_ms=1300,
        default_wait_ms=1500,
    )
    p_norm = build_recovery_prompt(coord_space="normalized", **common_kwargs)
    p_abs = build_recovery_prompt(coord_space="absolute", **common_kwargs)
    assert "0-1000 归一化" in p_norm
    assert "整数像素绝对坐标" in p_abs
    assert "禁止" in p_abs and "归一化" in p_abs


def test_parse_recovery_response_strips_list_prefix():
    """Claude 偶发会把 Thought / Action 写成 markdown 列表项 ``- Action: ...``。"""
    decision = parse_recovery_response(
        "- Thought: 当前差异可接受。\n"
        "- Action: finished(content='放行')"
    )
    assert decision.verdict == VERDICT_CONTINUE


def test_build_recovery_prompt_forbids_actions_and_lists_three_verbs():
    prompt = build_recovery_prompt(
        goal="点击我的，点击学习",
        trajectory={
            "actions": [
                {"action_id": "a001", "type": "click", "intent": "点击我的"},
                {"action_id": "a002", "type": "click", "intent": "点击学习"},
            ]
        },
        action={"action_id": "a001", "type": "click"},
        landmark={"action_id": "a001", "image_phash": "abc"},
        metrics={"global_diff": 0.04, "center_mae": 0.30},
        elapsed_ms=1300,
        max_wait_ms=1300,
        default_wait_ms=1500,
    )
    assert "局部恢复 VLM" in prompt
    assert "不要输出 JSON" in prompt
    assert "Thought:" in prompt
    assert "Action:" in prompt
    assert "finished(content='放行原因')" in prompt
    assert "wait(seconds=N)" in prompt
    assert "assert_fail(content='失败原因')" in prompt


@pytest.mark.asyncio
async def test_recovery_verifier_disabled_returns_assert_fail():
    settings = Settings(trajectory_cache_recovery_vlm_enabled=False)
    verifier = CacheReplayRecoveryVerifier(settings=settings)

    decision = await verifier.verify_alignment_miss(
        goal="g",
        trajectory={},
        action={},
        landmark={},
        current_bytes=b"a",
        landmark_bytes=b"b",
        metrics={},
        elapsed_ms=0,
        max_wait_ms=0,
    )

    assert decision.verdict == VERDICT_ASSERT_FAIL
    assert decision.error == "not_configured"
    assert "未启用" in decision.reason


@pytest.mark.asyncio
async def test_recovery_verifier_enabled_but_missing_credentials_returns_assert_fail():
    settings = Settings(
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_api_url="",
        trajectory_cache_recovery_vlm_api_key="",
        trajectory_cache_recovery_vlm_model="",
    )
    verifier = CacheReplayRecoveryVerifier(settings=settings)

    assert verifier.is_configured() is False
    problem = verifier.configuration_problem()
    assert "api_url" in problem and "api_key" in problem and "model" in problem


@pytest.mark.asyncio
async def test_recovery_verifier_chat_failure_falls_back_to_assert_fail(monkeypatch):
    settings = Settings(
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_api_url="https://example.test/chat",
        trajectory_cache_recovery_vlm_api_key="key",
        trajectory_cache_recovery_vlm_model="vlm-x",
    )
    verifier = CacheReplayRecoveryVerifier(settings=settings)
    assert verifier.is_configured() is True

    async def _boom(**kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(verifier, "_chat_double_image", _boom)

    decision = await verifier.verify_alignment_miss(
        goal="g",
        trajectory={"actions": []},
        action={"action_id": "a001", "type": "click"},
        landmark={"action_id": "a001"},
        current_bytes=b"current",
        landmark_bytes=b"landmark",
        metrics={"global_diff": 0.5, "center_mae": 0.5, "black_ratio_diff": 0.0},
        elapsed_ms=2000,
        max_wait_ms=1500,
    )

    assert decision.verdict == VERDICT_ASSERT_FAIL
    assert decision.error == "RuntimeError"
    assert "network unreachable" in decision.reason


def test_recovery_extract_responses_text_reads_output_content():
    data = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "WAIT_MORE: 800: 页面仍在加载"}
                ],
            }
        ]
    }

    assert _extract_responses_text(data) == "WAIT_MORE: 800: 页面仍在加载"


@pytest.mark.asyncio
async def test_recovery_verifier_supports_doubao_responses_backend(monkeypatch):
    settings = Settings(
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_backend="doubao_responses",
        trajectory_cache_recovery_vlm_api_url="https://example.test/responses",
        trajectory_cache_recovery_vlm_api_key="key",
        trajectory_cache_recovery_vlm_model="vlm-x",
    )
    verifier = CacheReplayRecoveryVerifier(settings=settings)

    async def _fake_responses(**kwargs):
        return "CONTINUE_REPLAY: 页面主结构一致，仅资源位变化"

    monkeypatch.setattr(verifier, "_responses_double_image", _fake_responses)

    decision = await verifier.verify_alignment_miss(
        goal="g",
        trajectory={"actions": []},
        action={"action_id": "a001", "type": "click"},
        landmark={"action_id": "a001"},
        current_bytes=b"current",
        landmark_bytes=b"landmark",
        metrics={"global_diff": 0.5, "center_mae": 0.5, "black_ratio_diff": 0.0},
        elapsed_ms=2000,
        max_wait_ms=1500,
    )

    assert decision.verdict == VERDICT_CONTINUE
    assert "资源位变化" in decision.reason


def test_recovery_extract_messages_text_concatenates_text_blocks():
    """Anthropic Messages 响应的 content 是块数组：text / thinking 块都要拼起来。"""
    data = {
        "content": [
            {"type": "thinking", "text": "(internal reasoning)"},
            {"type": "text", "text": "Thought: 当前差异可接受。"},
            {"type": "text", "text": "Action: finished(content='ok')"},
        ]
    }
    out = _extract_messages_text(data)
    assert "Thought:" in out
    assert "Action: finished" in out
    # 空 / 损坏 / 缺 content 字段都应安全返回 ""
    assert _extract_messages_text({}) == ""
    assert _extract_messages_text({"content": []}) == ""
    assert _extract_messages_text({"content": [{"type": "text"}]}) == ""


@pytest.mark.asyncio
async def test_recovery_verifier_supports_claude_messages_backend(monkeypatch):
    """claude_cu 主 VLM 用户的 recovery 通道走 Anthropic /v1/messages 协议。

    backend 路由必须能命中 _messages_double_image 而不是误用其它两条
    路径（chat completions / responses）——它们的 headers / payload 完全
    不兼容 anthropic API，发上去会被 401/400 直接 reject。
    """
    settings = Settings(
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_backend="claude_messages",
        trajectory_cache_recovery_vlm_api_url="https://api.anthropic.com/v1/messages",
        trajectory_cache_recovery_vlm_api_key="sk-ant-fake",
        trajectory_cache_recovery_vlm_model="claude-sonnet-4-5",
    )
    verifier = CacheReplayRecoveryVerifier(
        settings=settings, main_vlm_backend="claude_cu"
    )

    called: dict = {}

    async def _fake_messages(*, prompt, landmark_bytes, current_bytes):
        called["prompt_head"] = prompt[:60]
        called["landmark_bytes"] = landmark_bytes
        called["current_bytes"] = current_bytes
        return "Thought: 重新点击当前控件。\nAction: click(point='<point>540 1024</point>')"

    # 同时把另外两条路径换成"被调用就 raise"，确保路由真的命中 messages
    async def _should_not_be_called(**_kwargs):  # pragma: no cover - 防御断言
        raise AssertionError(
            "claude_messages backend 路由错了！不应该走 chat / responses"
        )

    monkeypatch.setattr(verifier, "_messages_double_image", _fake_messages)
    monkeypatch.setattr(
        verifier, "_chat_completions_double_image", _should_not_be_called
    )
    monkeypatch.setattr(verifier, "_responses_double_image", _should_not_be_called)

    decision = await verifier.verify_alignment_miss(
        goal="g",
        trajectory={"actions": []},
        action={"action_id": "a001", "type": "click"},
        landmark={"action_id": "a001"},
        current_bytes=b"current_jpeg",
        landmark_bytes=b"landmark_jpeg",
        metrics={"global_diff": 0.5, "center_mae": 0.5, "black_ratio_diff": 0.0},
        elapsed_ms=2000,
        max_wait_ms=1500,
    )

    # _messages_double_image 必须被调用，且双图按顺序传入
    assert called.get("landmark_bytes") == b"landmark_jpeg"
    assert called.get("current_bytes") == b"current_jpeg"
    # Claude 主 VLM → coord_space 应被推断成 absolute（C-3 派发逻辑）
    assert decision.verdict == VERDICT_REPAIR_ACTION
    assert decision.parsed_actions[0].coord_space == "absolute"
    assert decision.parsed_actions[0].point == [540, 1024]


@pytest.mark.asyncio
async def test_recovery_verifier_unknown_backend_raises_with_three_options():
    """未知 backend 时报错信息必须列出三家可选 backend，方便用户自查 .env。"""
    settings = Settings(
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_backend="some_typo_backend",
        trajectory_cache_recovery_vlm_api_url="https://example.test",
        trajectory_cache_recovery_vlm_api_key="key",
        trajectory_cache_recovery_vlm_model="x",
    )
    verifier = CacheReplayRecoveryVerifier(settings=settings)

    decision = await verifier.verify_alignment_miss(
        goal="g",
        trajectory={"actions": []},
        action={"action_id": "a001", "type": "click"},
        landmark={"action_id": "a001"},
        current_bytes=b"c",
        landmark_bytes=b"l",
        metrics={"global_diff": 0.5, "center_mae": 0.5, "black_ratio_diff": 0.0},
        elapsed_ms=2000,
        max_wait_ms=1500,
    )

    # 走 verify_alignment_miss 内 try/except 通道，verdict=ASSERT_FAIL
    assert decision.verdict == VERDICT_ASSERT_FAIL
    assert "doubao_responses" in decision.reason
    assert "openai_compatible" in decision.reason
    assert "claude_messages" in decision.reason


@pytest.mark.asyncio
async def test_replay_runner_recovery_continue_accepts_current_frame(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": False,
            "global_diff": 0.04,
            "center_mae": 0.30,
            "black_ratio_diff": 0.0,
            "reason": "global>0.0300",
        },
    )

    verifier = FakeRecoveryVerifier(
        [
            RecoveryDecision(
                verdict=VERDICT_CONTINUE,
                reason="资源位变化，主结构一致",
                raw="CONTINUE_REPLAY: 资源位变化，主结构一致",
            )
        ]
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": 2,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="点击我的",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert len(verifier.calls) == 1
    assert verifier.calls[0]["goal"] == "点击我的"
    assert any(
        title == "轨迹缓存 VLM 介入" and "verdict=CONTINUE_REPLAY" in content
        for _level, title, content in logs
    )
    # CONTINUE 后允许 carry 帧给下一 action（这里只有一个 action，主要校验流程不报错）
    assert runner._carry_before_index == 2


@pytest.mark.asyncio
async def test_replay_runner_recovery_repair_action_then_match(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": True, "global_diff": 0.01, "center_mae": 0.05, "black_ratio_diff": 0.0, "reason": "match"},
    ]
    compare_calls = {"i": 0}

    def fake_compare(**_kwargs):
        i = compare_calls["i"]
        compare_calls["i"] += 1
        return compare_results[min(i, len(compare_results) - 1)]

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(replay_module, "_compare_alignment", fake_compare)

    verifier = FakeRecoveryVerifier(
        [
            parse_recovery_response(
                "Thought: 当前目标位置变化，重新点击当前步骤目标。\n"
                "Action: click(point='<point>500 600</point>')"
            )
        ]
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": 2,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="点击入口",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert ("click", 1, 2) in driver.calls
    assert ("click", 500, 1200) in driver.calls
    assert any(
        title == "轨迹缓存 VLM 介入" and "verdict=REPAIR_ACTION" in content
        for _level, title, content in logs
    )
    assert any(
        title == "轨迹缓存状态路标" and "修复后对齐成功" in content
        for _level, title, content in logs
    )


def test_decode_image_size_handles_jpeg_and_garbage():
    """recovery 路径专用：必须能从 JPEG 字节流读出真实 (w, h)；
    解码失败时返回 None，让 _parsed_point_to_abs(absolute) 退化为兜底 clamp。"""
    import io
    from PIL import Image
    from ai_phone.server.trajectory_cache.replay import _decode_image_size

    buf = io.BytesIO()
    Image.new("RGB", (640, 360), color=(255, 0, 0)).save(buf, format="JPEG", quality=70)
    assert _decode_image_size(buf.getvalue()) == (640, 360)
    assert _decode_image_size(b"") is None
    assert _decode_image_size(None) is None
    assert _decode_image_size(b"\x00\x01\x02not a real image") is None


@pytest.mark.asyncio
async def test_replay_runner_recovery_repair_action_absolute_rescales_to_device(monkeypatch):
    """Bug 修复回归：claude_cu / gpt_cu 路径下，模型看到的"附图 2"是 720 max-edge
    JPEG（来自 driver.screenshot_jpeg(25, 720)）。模型按这个尺寸输出 absolute
    坐标 e.g. (320, 480)，**必须**按 (model_image_size 720x360 → device 1000x2000)
    等比缩回设备坐标 (444, 2666→1999clamp)，不能直接 clamp。

    场景：模型送图 720x360（横屏 720 max-edge），device 1000x2000，模型回 (320, 100)：
      - 旧实现：直接 clamp → (320, 100) ← 错位（落在屏幕中上区）
      - 新实现：等比 → (320*1000/720=444, 100*2000/360=555) ← 正确落点
    """
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": True, "global_diff": 0.01, "center_mae": 0.05, "black_ratio_diff": 0.0, "reason": "match"},
    ]
    compare_calls = {"i": 0}

    def fake_compare(**_kwargs):
        i = compare_calls["i"]
        compare_calls["i"] += 1
        return compare_results[min(i, len(compare_results) - 1)]

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(replay_module, "_compare_alignment", fake_compare)

    # 让 _screenshot_jpeg 返回一张已知尺寸的 JPEG（模拟 driver.screenshot_jpeg
    # 输出 720 max-edge 压缩图），_decode_image_size 应该读到 (720, 360)
    import io as _io
    from PIL import Image as _Image
    img_buf = _io.BytesIO()
    _Image.new("RGB", (720, 360), color=(0, 0, 0)).save(img_buf, format="JPEG", quality=25)
    fake_jpeg_bytes = img_buf.getvalue()

    decision_abs = parse_recovery_response(
        "Thought: 重新点击中部控件。\nAction: click(point='<point>320 100</point>')",
        coord_space="absolute",
    )
    verifier = FakeRecoveryVerifier([decision_abs])
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": 2,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="点击入口",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]
    # 强制 _screenshot_jpeg 返回我们准备好的 720x360 JPEG
    async def fake_shot():
        return fake_jpeg_bytes
    runner._screenshot_jpeg = fake_shot  # type: ignore[method-assign]

    result = await runner.run()

    # device 1000x2000，模型送图 720x360 上的 (320, 100)
    # 等比缩回设备坐标：
    #   x = round(320 * 1000 / 720) = 444
    #   y = round(100 * 2000 / 360)  = 556
    expected_x = round(320 * 1000 / 720)
    expected_y = round(100 * 2000 / 360)
    assert result.success is True
    assert ("click", expected_x, expected_y) in driver.calls, (
        f"absolute 坐标必须按 (720x360 -> 1000x2000) 等比缩回设备坐标，"
        f"期望 ({expected_x}, {expected_y})，实际 driver.calls={driver.calls}"
    )


@pytest.mark.asyncio
async def test_replay_runner_recovery_repair_action_absolute_coord_space(monkeypatch):
    """C-4 端到端：claude_cu / gpt_cu backend 下，recovery 产出的 REPAIR_ACTION
    坐标是 ``coord_space="absolute"`` 的设备像素，ReplayRunner 必须按 absolute
    分支直接 clamp，**不能**走 vlm_point_to_abs(0-1000) 反算，否则坐标全错。

    断言点：driver.calls 含 ("click", 540, 1024)；如果错误地走了 normalized 反
    算，FakeDriver(window_size=1000x2000) 下 540*1000/1000=540, 1024*2000/1000
    =2048（被 clamp 到 1999），断言会失败暴露问题。
    """
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": False, "global_diff": 0.04, "center_mae": 0.30, "black_ratio_diff": 0.0, "reason": "global>0.0300"},
        {"match": True, "global_diff": 0.01, "center_mae": 0.05, "black_ratio_diff": 0.0, "reason": "match"},
    ]
    compare_calls = {"i": 0}

    def fake_compare(**_kwargs):
        i = compare_calls["i"]
        compare_calls["i"] += 1
        return compare_results[min(i, len(compare_results) - 1)]

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(replay_module, "_compare_alignment", fake_compare)

    decision_abs = parse_recovery_response(
        "Thought: 重新点击当前控件。\nAction: click(point='<point>540 1024</point>')",
        coord_space="absolute",
    )
    assert decision_abs.parsed_actions[0].coord_space == "absolute", (
        "前置：parse 必须把 coord_space 覆写为 absolute"
    )
    verifier = FakeRecoveryVerifier([decision_abs])
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": 2,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="点击入口",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert ("click", 1, 2) in driver.calls, "首条缓存 action 应正常下发"
    # 关键：absolute 路径 → 直接 clamp，坐标就是模型输出的 (540, 1024)
    assert ("click", 540, 1024) in driver.calls, (
        f"absolute 坐标应被原样 clamp，driver.calls={driver.calls}"
    )


@pytest.mark.asyncio
async def test_replay_runner_recovery_wait_more_then_match(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    # alignment 主循环 max_wait=1000 / observe=500 / retry=300：跑 3 次 MISS
    # 后耗尽窗口，进入 _handle_alignment_miss → verifier(WAIT_MORE) → sleep →
    # 第 4 次 _compare_alignment（recheck）才命中 MATCH。
    compare_results = [
        {  # attempt 1：observe 后立刻比，MISS
            "match": False, "global_diff": 0.04, "center_mae": 0.30,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
        {  # attempt 2：retry 后比，MISS
            "match": False, "global_diff": 0.04, "center_mae": 0.30,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
        {  # attempt 3：再 retry 后比，MISS（达到 max_wait 后 break）
            "match": False, "global_diff": 0.04, "center_mae": 0.30,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
        {  # WAIT_MORE 等待结束后的 recheck：MATCH
            "match": True, "global_diff": 0.01, "center_mae": 0.05,
            "black_ratio_diff": 0.0, "reason": "match",
        },
    ]
    compare_calls = {"i": 0}

    def fake_compare(**_kwargs):
        i = compare_calls["i"]
        compare_calls["i"] += 1
        return compare_results[min(i, len(compare_results) - 1)]

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(replay_module, "_compare_alignment", fake_compare)

    verifier = FakeRecoveryVerifier(
        [
            RecoveryDecision(
                verdict=VERDICT_WAIT_MORE,
                reason="还在加载",
                wait_ms=400,
                raw="WAIT_MORE: 400: 还在加载",
            )
        ],
        max_wait_more=1,
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": 2,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="g",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert len(verifier.calls) == 1
    # WAIT_MORE 等待 400ms 后必须有 sleep(0.4) 出现
    assert 0.4 in sleeps
    assert any(
        title == "轨迹缓存 VLM 介入" and "verdict=WAIT_MORE" in content
        for _level, title, content in logs
    )
    assert any(
        title == "轨迹缓存状态路标" and "MATCH-after-WAIT_MORE" in content
        for _level, title, content in logs
    )


@pytest.mark.asyncio
async def test_replay_runner_recovery_wait_more_exhausts_quota(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": False, "global_diff": 0.04, "center_mae": 0.30,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
    )

    verifier = FakeRecoveryVerifier(
        [
            RecoveryDecision(VERDICT_WAIT_MORE, "再等等", wait_ms=300),
            RecoveryDecision(VERDICT_WAIT_MORE, "再等等2", wait_ms=300),
        ],
        max_wait_more=1,
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": None,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="g",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is False
    assert "WAIT_MORE_EXHAUSTED" in (result.error or "")
    # 第一次 WAIT_MORE 用掉，第二次 verifier 调用时配额已耗尽，按 ASSERT_FAIL 兜底
    assert len(verifier.calls) == 2
    assert any(
        title == "轨迹缓存 VLM 介入" and "WAIT_MORE 配额已耗尽" in content
        for _level, title, content in logs
    )


@pytest.mark.asyncio
async def test_replay_runner_recovery_assert_fail_terminates(monkeypatch):
    logs: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": False, "global_diff": 0.5, "center_mae": 0.6,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
    )

    verifier = FakeRecoveryVerifier(
        [RecoveryDecision(VERDICT_ASSERT_FAIL, "跳错页面")],
        max_wait_more=1,
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": None,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="g",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is False
    assert "recovery=ASSERT_FAIL" in (result.error or "")
    assert "跳错页面" in (result.error or "")
    assert any(
        title == "轨迹缓存 VLM 介入" and "verdict=ASSERT_FAIL" in content
        for _level, title, content in logs
    )


@pytest.mark.asyncio
async def test_replay_runner_recovery_call_limit_marks_case_unhealthy(monkeypatch):
    logs: list = []
    driver = FakeDriver()
    from ai_phone.server.trajectory_cache import replay as replay_module
    from ai_phone.server.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        replay_module,
        "_compare_alignment",
        lambda **_kwargs: {
            "match": False, "global_diff": 0.5, "center_mae": 0.6,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
    )

    verifier = FakeRecoveryVerifier(
        [
            RecoveryDecision(VERDICT_WAIT_MORE, "还在加载", wait_ms=100),
            RecoveryDecision(VERDICT_CONTINUE, "不应被调用"),
        ],
        max_wait_more=2,
    )
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "action_id": "a001", "type": "click", "point": {"x": 1, "y": 2}},
            ],
            "state_landmarks": [
                {
                    "action_id": "a001",
                    "before_action_index": None,
                    "status": "available",
                    "image_phash": "01",
                    "timing": {"gap_to_next_action_ms": 600},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        recovery_verifier=verifier,
        goal="g",
    )
    runner.alignment_enabled = True
    runner.alignment_min_wait_ms = 1000
    runner.alignment_retry_interval_ms = 300
    runner.alignment_max_wait_ratio = 1.3
    runner.recovery_max_calls_per_replay = 1
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is False
    assert "CALL_LIMIT_EXCEEDED" in (result.error or "")
    assert len(verifier.calls) == 1
    assert any(
        title == "轨迹缓存 VLM 兜底" and "case/cache 不健康" in content
        for _level, title, content in logs
    )
