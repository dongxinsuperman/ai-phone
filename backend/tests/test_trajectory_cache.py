from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ai_phone.agent.runner.stability import StabilityResult
from ai_phone.config import Settings
from ai_phone.server import db as db_module
from ai_phone.server.models import (
    Device,
    Run,
    VlmTrajectoryCacheV2,
    VlmTrajectoryCacheV3,
)
# [M4 TODO] 本文件的缓存 replay 测试历史上通过 Server 进程内 ServerRunnerService +
# DriverRpcWaiter 驱动（server_brain）。Distributed Agent Brain 已删除该执行脑，
# 缓存 replay 将在 M4 下沉到 Agent 侧并重建测试。在此之前整文件跳过，避免 collect
# 阶段引用已删除的 server_brain 执行链路。
import pytest as _pytest  # noqa: F401  # 历史保留，部分用例用 _pytest

# M4 片7：缓存 replay/归档已下沉 Agent（ai_phone.agent.trajectory_cache），Server 转薄存储。
# 本文件保留与具体存储无关的纯逻辑测试（三协议 fallback、parse/prompt、coord_space、
# locator/recovery/ephemeral、V3 回放执行器）并改指 Agent 模块；Server 侧只测仍保留的
# 控制面（cache_key / cache mode / mark suspect / 删除）。旧归档反推（_build_trajectory /
# build_v3_cache_payload / save_*）与 server_brain replay（ServerRunnerService）已删，
# 对应集成测试由 Agent 侧 recorder→archive 测试（test_cache_archive_build /
# test_cache_archive / test_cache_replay_orchestrate）覆盖。
from ai_phone.server.retry import normalize_requested_retry_max, resolve_effective_retry_max

from ai_phone.agent.trajectory_cache import (
    CacheReplayAssertionVerifier,
    CacheReplayRecoveryVerifier,
    EphemeralGateDecision,
    GATE_EXECUTE_ORIGINAL,
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
    build_recovery_prompt,
    build_v3_locator_prompt,
    normalize_run_semantic,
    parse_cache_assertion_response,
    parse_ephemeral_classification_response,
    parse_ephemeral_gate_response,
    parse_recovery_response,
    parse_v3_locator_response,
    parse_v3_rescue_response,
)
from ai_phone.agent.trajectory_cache import ephemeral as ephemeral_module
from ai_phone.agent.trajectory_cache.archive import build_v3_plan_cleaner_prompt
from ai_phone.agent.trajectory_cache.recovery import (
    _extract_messages_text,
    _extract_responses_text,
)
from ai_phone.agent.trajectory_cache.v3_replay import V3LocatorMiss
from ai_phone.server.trajectory_cache import (
    build_cache_key,
    delete_trajectory_cache_v2_for_run,
    get_active_trajectory_cache_v3,
    mark_trajectory_cache_v3_suspect,
    normalize_requested_cache_mode,
    resolve_effective_cache_mode,
)


@pytest.fixture(autouse=True)
def _isolate_aiphone_env(monkeypatch):
    """本文件用例都用 ``Settings(_env_file=None)`` 构造、期望取代码默认值。但其他测试
    ``import ai_phone.agent.main`` 时其顶部 ``load_dotenv()`` 会把 ``.env``（含
    ``AI_PHONE_VLM_*`` / ``AI_PHONE_TRAJECTORY_CACHE_*`` 等 160+ 项）注入 ``os.environ``，
    pydantic 仍会读到，污染如 v3 coord 的 overseas 配置判定。清掉 ``AI_PHONE_`` 前缀
    保证用例不受运行顺序影响、可独立复现（next 时本文件整体 skip，故从未暴露）。"""
    import os

    for _key in list(os.environ):
        if _key.startswith("AI_PHONE_"):
            monkeypatch.delenv(_key, raising=False)


def _test_jpeg(width: int = 80, height: int = 120, color=(30, 60, 90)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="JPEG")
    return buf.getvalue()


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


def test_retry_max_resolution_is_env_capped_and_tolerant():
    assert normalize_requested_retry_max(None) is None
    assert normalize_requested_retry_max("2") == 2
    assert normalize_requested_retry_max("bad") == 0
    assert normalize_requested_retry_max(-1) == 0
    assert normalize_requested_retry_max(True) == 0
    assert resolve_effective_retry_max(
        env_retry_enabled=False,
        env_retry_max=3,
        payload_retry_max=2,
    ) == 0
    assert resolve_effective_retry_max(
        env_retry_enabled=True,
        env_retry_max=0,
        payload_retry_max=2,
    ) == 0
    assert resolve_effective_retry_max(
        env_retry_enabled=True,
        env_retry_max=1,
        payload_retry_max=3,
    ) == 1


def test_parse_v3_locator_response_accepts_point_only_contract():
    parsed = parse_v3_locator_response(
        "<point>500 250</point>",
        coord_space="normalized",
        expected_action_type="click",
    )

    assert parsed is not None
    assert parsed.action == "click"
    assert parsed.point == [500, 250]
    assert parsed.coord_space == "normalized"
    assert (
        parse_v3_locator_response(
            "Action: click(point='<point>500 250</point>')",
            coord_space="normalized",
            expected_action_type="click",
        )
        is None
    )


@pytest.mark.parametrize(
    "raw",
    [
        "<point>500 250</point>",       # 半角空格分隔（基线）
        "<point>500,250</point>",       # 仅半角逗号
        "<point>500, 250</point>",      # 半角逗号+空格（Claude / GPT 常见）
        "<point>500 , 250</point>",     # 空格+逗号+空格
        "<point>500，250</point>",      # 全角逗号（中文上下文偶发）
        "<point>500， 250</point>",     # 全角逗号+空格
    ],
)
def test_parse_v3_locator_response_accepts_comma_and_full_width_separators(raw):
    parsed = parse_v3_locator_response(
        raw,
        coord_space="normalized",
        expected_action_type="click",
    )

    assert parsed is not None
    assert parsed.action == "click"
    assert parsed.point == [500, 250]


def test_parse_v3_locator_response_accepts_drag_with_comma_separators():
    parsed = parse_v3_locator_response(
        "<start>100, 200</start><end>300，400</end>",
        coord_space="normalized",
        expected_action_type="drag",
    )

    assert parsed is not None
    assert parsed.action == "drag"
    assert parsed.start_point == [100, 200]
    assert parsed.end_point == [300, 400]


def test_build_v3_locator_prompt_is_minimal_and_action_specific():
    prompt = build_v3_locator_prompt(
        goal="不要进入 prompt",
        trajectory={"run_semantic_text": "也不要进入 prompt"},
        action={"index": 2, "type": "click", "plan_intent": "点击开始挑战"},
        coord_space="normalized",
    )

    assert "目标描述：点击开始挑战" in prompt
    assert "缓存动作类型：click" in prompt
    assert "输出：<point>x y</point>" in prompt
    assert "不负责决定动作类型" in prompt
    assert "不要复用缓存旧坐标" in prompt
    assert "需要猜测" in prompt
    assert "不要进入 prompt" not in prompt
    assert "也不要进入 prompt" not in prompt
    assert "V3" not in prompt
    assert "Thought" not in prompt
    assert "Action:" not in prompt

    drag_prompt = build_v3_locator_prompt(
        goal="",
        trajectory={},
        action={"index": 1, "type": "drag", "plan_intent": "向上拖动列表"},
        coord_space="absolute",
    )
    assert "缓存动作类型：drag" in drag_prompt
    assert "<start>x1 y1</start>" in drag_prompt
    assert "<end>x2 y2</end>" in drag_prompt


def test_build_v3_plan_cleaner_prompt_classifies_text_stability_in_three_tiers():
    """plan cleaner prompt 必须按「文案稳定性 × goal 粒度」三档判断是否保留 thought 文案。

    这样才能既避免把"屏幕动态条目"硬记成具体文字（下次屏幕变就命中不了），
    又不至于把"应用自带稳定 UI 锚点"也一刀泛化掉（导致海外 VLM 定位失语境）。
    """

    prompt = build_v3_plan_cleaner_prompt(
        action={"type": "click", "thought": "占位 thought"},
        goal="占位 goal",
    )

    assert "维度 A" in prompt and "维度 B" in prompt
    assert "B1" in prompt and "B2" in prompt and "B3" in prompt


def test_build_v3_plan_cleaner_prompt_keeps_stable_ui_anchor_categories():
    prompt = build_v3_plan_cleaner_prompt(
        action={"type": "click", "thought": "占位 thought"},
        goal="占位 goal",
    )

    assert "稳定 UI 锚点" in prompt
    assert "导航栏" in prompt
    assert "placeholder" in prompt
    assert "保留" in prompt and "thought" in prompt


def test_build_v3_plan_cleaner_prompt_generalizes_dynamic_screen_content():
    prompt = build_v3_plan_cleaner_prompt(
        action={"type": "click", "thought": "占位 thought"},
        goal="占位 goal",
    )

    assert "动态屏幕内容" in prompt
    assert "列表条目" in prompt or "卡片标题" in prompt
    assert "用户生成内容" in prompt
    assert "换设备" in prompt or "换日期" in prompt
    assert "保守泛化" in prompt


def test_build_v3_plan_cleaner_prompt_stays_business_neutral():
    """规则正文不应带任何业务专有名词，避免污染通用性。"""

    prompt = build_v3_plan_cleaner_prompt(
        action={"type": "click", "thought": "占位 thought"},
        goal="占位 goal",
    )

    forbidden_business_terms = [
        "Futures",
        "BAND",
        "BTC",
        "USDT",
        "Search for Pairs",
        "有理数",
        "习题",
    ]
    for term in forbidden_business_terms:
        assert term not in prompt, f"plan cleaner prompt 不应出现业务专有名词：{term}"


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


@pytest.mark.asyncio
async def test_ephemeral_gate_overseas_claude_cu_falls_back_to_chat_messages(monkeypatch):
    """主 vlm = claude_cu 时，ephemeral gate 不再走 Computer Use 通道，
    而是用主 vlm 同 model + 同 key + 同 url 走普通 messages chat 协议
    （不挂 computer 工具、不开 anthropic-beta），避免 CU agent 反射导致 verdict 解析失效。
    """
    seen = {}

    async def fake_call(*, backend, api_url, api_key, model, timeout_sec,
                        system, prompt, images):
        seen["backend"] = backend
        seen["api_url"] = api_url
        seen["api_key"] = api_key
        seen["model"] = model
        seen["images"] = list(images)
        return (
            "{\"verdict\": \"SKIP\","
            " \"reason\": \"current popup absent, safe to skip\"}"
        )

    monkeypatch.setattr(ephemeral_module, "_call_vlm_with_images", fake_call)

    # 即使没有任何阻塞，确保不会被旧 CU 客户端拦截
    import ai_phone.shared.llm.main.claude_cu as claude_cu_module

    class _ShouldNotBeCalled:
        def __init__(self, **kwargs):
            raise AssertionError(
                "ephemeral gate 不应再调 ClaudeComputerUseClient（已切到 chat 协议）"
            )

    monkeypatch.setattr(claude_cu_module, "ClaudeComputerUseClient", _ShouldNotBeCalled)

    settings = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        vlm_api_url="https://api.anthropic.com/v1/messages",
        vlm_api_key="main-key",
        vlm_model="claude-sonnet-4-5",
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_gate_enabled=True,
        trajectory_cache_ephemeral_gate_timeout_sec=30,
        trajectory_cache_ephemeral_gate_use_recovery_vlm_config=True,
        trajectory_cache_recovery_vlm_api_url="",
        trajectory_cache_recovery_vlm_api_key="",
        trajectory_cache_recovery_vlm_model="",
    )
    gate = ephemeral_module.CacheEphemeralGateVerifier(
        settings=settings,
        main_vlm_backend="claude_cu",
    )

    decision = await gate.decide(
        goal="g",
        action={"action_id": "a001", "type": "click"},
        current_bytes=_test_jpeg(80, 120, (20, 20, 20)),
        cached_popup_before_bytes=_test_jpeg(80, 120, (220, 220, 220)),
        cached_after_bytes=_test_jpeg(80, 120, (120, 120, 120)),
        next_action={"action_id": "a002", "type": "click"},
    )

    assert seen["backend"] == "claude_messages"
    assert seen["api_url"] == "https://api.anthropic.com/v1/messages"
    assert seen["api_key"] == "main-key"
    assert seen["model"] == "claude-sonnet-4-5"
    assert [label for label, _ in seen["images"]] == [
        "current_replay",
        "cached_popup_before",
        "cached_after",
    ]
    assert decision.verdict == GATE_SKIP
    assert decision.coord_space == "absolute"


@pytest.mark.asyncio
async def test_ephemeral_gate_overseas_gpt_cu_uses_openai_responses(monkeypatch):
    """主 vlm = gpt_cu 时，ephemeral gate 复用主 VLM 的 OpenAI Responses 端点。

    这里不走 chat completions，也不挂 computer tool，只做单次图像 verdict。
    """
    seen = {}

    async def fake_call(*, backend, api_url, api_key, model, timeout_sec,
                        system, prompt, images):
        seen["backend"] = backend
        seen["api_url"] = api_url
        seen["model"] = model
        seen["api_key"] = api_key
        return (
            "{\"verdict\": \"EXECUTE_ORIGINAL\","
            " \"reason\": \"popup still present, replay original close\"}"
        )

    monkeypatch.setattr(ephemeral_module, "_call_vlm_with_images", fake_call)

    settings = Settings(
        _env_file=None,
        vlm_backend="gpt_cu",
        vlm_api_url="https://api.openai.com/v1/responses",
        vlm_api_key="main-key",
        vlm_model="computer-use-preview",
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_gate_enabled=True,
        trajectory_cache_ephemeral_gate_timeout_sec=30,
        trajectory_cache_ephemeral_gate_use_recovery_vlm_config=True,
        trajectory_cache_recovery_vlm_api_url="",
        trajectory_cache_recovery_vlm_api_key="",
        trajectory_cache_recovery_vlm_model="",
    )
    gate = ephemeral_module.CacheEphemeralGateVerifier(
        settings=settings,
        main_vlm_backend="gpt_cu",
    )

    decision = await gate.decide(
        goal="g",
        action={"action_id": "a001", "type": "click"},
        current_bytes=_test_jpeg(80, 120, (20, 20, 20)),
        cached_popup_before_bytes=_test_jpeg(80, 120, (220, 220, 220)),
        cached_after_bytes=_test_jpeg(80, 120, (120, 120, 120)),
    )

    assert seen["backend"] == "openai_responses"
    assert seen["api_url"] == "https://api.openai.com/v1/responses"
    assert seen["model"] == "computer-use-preview"
    assert seen["api_key"] == "main-key"
    assert decision.verdict == GATE_EXECUTE_ORIGINAL


@pytest.mark.asyncio
async def test_ephemeral_gate_call_failure_falls_back_to_execute_original(monkeypatch):
    """调用异常 / 解析失败时，gate 默认 EXECUTE_ORIGINAL（保底执行原 action），
    不再 ESCALATE。原因：ESCALATE → recovery 也可能失败 → ASSERT_FAIL → 整个回放卡死。
    optional_ephemeral 本来就是低风险，最坏空点一下。
    """
    async def boom(**_kwargs):
        raise RuntimeError("upstream 5xx")

    monkeypatch.setattr(ephemeral_module, "_call_vlm_with_images", boom)

    settings = Settings(
        _env_file=None,
        vlm_backend="doubao_responses",
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_gate_enabled=True,
        trajectory_cache_ephemeral_gate_timeout_sec=30,
        trajectory_cache_ephemeral_gate_backend="openai_compatible",
        trajectory_cache_ephemeral_gate_api_url="https://example.com/chat",
        trajectory_cache_ephemeral_gate_api_key="key",
        trajectory_cache_ephemeral_gate_model="m",
        trajectory_cache_ephemeral_gate_use_recovery_vlm_config=False,
    )
    gate = ephemeral_module.CacheEphemeralGateVerifier(
        settings=settings,
        main_vlm_backend="doubao_responses",
    )

    decision = await gate.decide(
        goal="g",
        action={"action_id": "a001", "type": "click"},
        current_bytes=_test_jpeg(),
        cached_popup_before_bytes=_test_jpeg(),
        cached_after_bytes=_test_jpeg(),
    )
    assert decision.verdict == GATE_EXECUTE_ORIGINAL
    assert decision.error == "RuntimeError"
    assert "保底执行" in decision.reason or "EXECUTE_ORIGINAL" in decision.reason


@pytest.mark.asyncio
async def test_ephemeral_gate_unparseable_response_falls_back_to_execute_original(monkeypatch):
    async def fake_call(**_kwargs):
        return "I think you should probably skip this popup."  # 自然语言，非 JSON 也无 verdict 关键字

    monkeypatch.setattr(ephemeral_module, "_call_vlm_with_images", fake_call)

    settings = Settings(
        _env_file=None,
        vlm_backend="doubao_responses",
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_gate_enabled=True,
        trajectory_cache_ephemeral_gate_backend="openai_compatible",
        trajectory_cache_ephemeral_gate_api_url="https://example.com/chat",
        trajectory_cache_ephemeral_gate_api_key="key",
        trajectory_cache_ephemeral_gate_model="m",
        trajectory_cache_ephemeral_gate_use_recovery_vlm_config=False,
    )
    gate = ephemeral_module.CacheEphemeralGateVerifier(
        settings=settings,
        main_vlm_backend="doubao_responses",
    )

    decision = await gate.decide(
        goal="g",
        action={"action_id": "a001", "type": "click"},
        current_bytes=_test_jpeg(),
        cached_popup_before_bytes=_test_jpeg(),
        cached_after_bytes=_test_jpeg(),
    )
    assert decision.verdict == GATE_EXECUTE_ORIGINAL
    assert decision.error == "parse_error"


def test_overseas_cu_to_chat_config_translates_claude_and_gpt():
    assert ephemeral_module._overseas_cu_to_chat_config(
        main_backend="claude_cu",
        main_api_url="https://api.anthropic.com/v1/messages",
        main_api_key="k",
        main_model="claude-sonnet",
    ) == ("claude_messages", "https://api.anthropic.com/v1/messages", "k", "claude-sonnet")

    assert ephemeral_module._overseas_cu_to_chat_config(
        main_backend="gpt_cu",
        main_api_url="https://api.openai.com/v1/responses",
        main_api_key="k",
        main_model="cu-preview",
    ) == ("openai_responses", "https://api.openai.com/v1/responses", "k", "cu-preview")

    # 自部署代理保留 host 前缀
    assert ephemeral_module._overseas_cu_to_chat_config(
        main_backend="gpt_cu",
        main_api_url="https://my-proxy.internal/openai/v1/responses",
        main_api_key="k",
        main_model="m",
    ) == ("openai_responses", "https://my-proxy.internal/openai/v1/responses", "k", "m")

    assert ephemeral_module._overseas_cu_to_chat_config(
        main_backend="gpt_cu",
        main_api_url="https://api.openai.com/v1/chat/completions",
        main_api_key="k",
        main_model="m",
    ) == ("openai_responses", "https://api.openai.com/v1/responses", "k", "m")


@pytest.mark.asyncio
async def test_ephemeral_openai_responses_payload_shape(monkeypatch):
    captured: dict = {}

    async def _fake_post_json(api_url, api_key, payload, timeout_sec):
        captured["api_url"] = api_url
        captured["api_key"] = api_key
        captured["payload"] = payload
        captured["timeout_sec"] = timeout_sec
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "{\"verdict\":\"SKIP\"}"}
                    ],
                }
            ]
        }

    monkeypatch.setattr(ephemeral_module, "_post_json", _fake_post_json)

    text = await ephemeral_module._call_vlm_with_images(
        backend="openai_responses",
        api_url="https://api.openai.com/v1/responses",
        api_key="sk-openai",
        model="computer-use-preview",
        timeout_sec=12,
        system="system",
        prompt="prompt",
        images=[("current", _test_jpeg(10, 20, (10, 10, 10)))],
    )

    assert text == "{\"verdict\":\"SKIP\"}"
    assert captured["api_url"] == "https://api.openai.com/v1/responses"
    assert captured["api_key"] == "sk-openai"
    assert captured["timeout_sec"] == 12
    payload = captured["payload"]
    assert payload["model"] == "computer-use-preview"
    assert "messages" not in payload
    assert "tools" not in payload
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["input"][0] == {"role": "system", "content": "system"}
    assert payload["input"][1]["role"] == "user"
    assert payload["input"][1]["content"][0] == {"type": "input_text", "text": "prompt"}
    assert payload["input"][1]["content"][1]["type"] == "input_image"


@pytest.mark.asyncio
async def test_v3_locator_openai_responses_payload_shape(monkeypatch):
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "<point>10 20</point>"}],
                    }
                ]
            }

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

    monkeypatch.setattr(v3_replay_module.httpx, "AsyncClient", FakeAsyncClient)
    locator = V3PlanLocator(
        settings=Settings(
            _env_file=None,
            trajectory_cache_v3_coord_gpt_reasoning_effort="high",
        ),
        main_vlm_backend="gpt_cu",
    )

    text = await locator._openai_responses_single_image(
        prompt="locate",
        image_bytes=_test_jpeg(20, 30, (10, 10, 10)),
        api_url="https://api.openai.com/v1/responses",
        api_key="sk-openai",
        model="computer-use-preview",
        timeout_sec=11,
    )

    assert text == "<point>10 20</point>"
    assert captured["api_url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer sk-openai"
    payload = captured["payload"]
    assert payload["model"] == "computer-use-preview"
    assert payload["reasoning"] == {"effort": "high"}
    assert "tools" not in payload
    assert "messages" not in payload
    assert payload["input"][1]["content"][0]["type"] == "input_text"
    assert payload["input"][1]["content"][1]["type"] == "input_image"


@pytest.mark.asyncio
async def test_v3_locator_gpt_cu_uses_openai_responses(monkeypatch):
    """gpt_cu 主 vlm → v3 locator 走 openai_responses 单次视觉定位。

    见 docs/executable-logic-contract.md §14：定位 vlm 和主 vlm 用同一把 key、
    同一个模型，但不走 CU agent loop。坐标空间仍然 absolute。
    """
    settings = Settings(
        _env_file=None,
        vlm_backend="gpt_cu",
        vlm_api_url="https://api.openai.com/v1/responses",
        vlm_api_key="main-openai-key",
        vlm_model="gpt-4o",
        trajectory_cache_v3_coord_enabled=True,
    )
    locator = V3PlanLocator(settings=settings, main_vlm_backend="gpt_cu")
    backend, api_url, api_key, model, _timeout = locator._config()
    assert backend == "openai_responses"
    assert api_url == "https://api.openai.com/v1/responses"
    assert api_key == "main-openai-key"
    assert model == "gpt-4o"
    assert locator.coord_space == "absolute"

    captured: dict = {}

    async def _fake_chat(**kwargs):
        captured.update(kwargs)
        return "<point>50 100</point>"

    monkeypatch.setattr(locator, "_openai_responses_single_image", _fake_chat)

    result = await locator.locate_action(
        goal="g",
        trajectory={"actions": []},
        action={"index": 1, "type": "click", "plan_intent": "点击目标"},
        screenshot_bytes=_test_jpeg(100, 200, (20, 20, 20)),
        image_size=(100, 200),
        window_size=(1000, 2000),
    )

    assert captured["api_url"] == "https://api.openai.com/v1/responses"
    assert captured["api_key"] == "main-openai-key"
    assert captured["model"] == "gpt-4o"
    # absolute 坐标按 image_size→window_size 等比放大
    assert result.action["point"] == {"x": 500, "y": 1000}


@pytest.mark.asyncio
async def test_v3_locator_claude_cu_falls_back_to_chat_messages(monkeypatch):
    """claude_cu 主 vlm → v3 locator 走 anthropic /v1/messages chat 协议。

    URL 复用主 vlm（anthropic /v1/messages 既能跑 CU 也能跑普通 chat），不打
    CU beta header / 不挂 computer 工具——这就把 Claude 从 agent 模式切回
    普通看图回坐标的模式。
    """
    settings = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        vlm_api_url="https://api.anthropic.com/v1/messages",
        vlm_api_key="main-anthropic-key",
        vlm_model="claude-sonnet-4-5",
        trajectory_cache_v3_coord_enabled=True,
    )
    locator = V3PlanLocator(settings=settings, main_vlm_backend="claude_cu")
    backend, api_url, api_key, model, _timeout = locator._config()
    assert backend == "claude_messages"
    assert api_url == "https://api.anthropic.com/v1/messages"
    assert api_key == "main-anthropic-key"
    assert model == "claude-sonnet-4-5"
    assert locator.coord_space == "absolute"

    captured: dict = {}

    async def _fake_messages(**kwargs):
        captured.update(kwargs)
        return "<point>40 80</point>"

    async def _no_completions(**_kwargs):  # pragma: no cover
        raise AssertionError("claude_cu 不应路由到 chat completions")

    async def _no_responses(**_kwargs):  # pragma: no cover
        raise AssertionError("claude_cu 不应路由到 responses")

    monkeypatch.setattr(locator, "_messages_single_image", _fake_messages)
    monkeypatch.setattr(locator, "_chat_completions_single_image", _no_completions)
    monkeypatch.setattr(locator, "_responses_single_image", _no_responses)

    result = await locator.locate_action(
        goal="g",
        trajectory={"actions": []},
        action={"index": 1, "type": "click", "plan_intent": "点击目标"},
        screenshot_bytes=_test_jpeg(100, 200, (20, 20, 20)),
        image_size=(100, 200),
        window_size=(1000, 2000),
    )

    assert captured["api_key"] == "main-anthropic-key"
    assert captured["model"] == "claude-sonnet-4-5"
    assert captured["api_url"] == "https://api.anthropic.com/v1/messages"
    assert result.action["point"] == {"x": 400, "y": 800}


@pytest.mark.asyncio
async def test_v3_rescue_overseas_falls_back_to_chat_messages(monkeypatch):
    """claude_cu 主 vlm → v3 rescue 走 chat 协议（_call_vlm_with_images）。"""
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

    captured: dict = {}

    async def _fake_call(**kwargs):
        captured.update(kwargs)
        return (
            '{"verdict":"REPAIR_ACTION","reason":"关闭弹窗",'
            '"repair_action":{"type":"click","point":{"x":25,"y":40}}}'
        )

    monkeypatch.setattr(v3_replay_module, "_call_vlm_with_images", _fake_call)
    settings = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        vlm_api_url="https://api.anthropic.com/v1/messages",
        vlm_api_key="main-anthropic-key",
        vlm_model="claude-sonnet-4-5",
        trajectory_cache_v3_rescue_enabled=True,
    )
    rescue = V3RescueVerifier(settings=settings, main_vlm_backend="claude_cu")
    backend, api_url, api_key, model, _timeout = rescue._config()
    assert backend == "claude_messages"
    assert api_url == "https://api.anthropic.com/v1/messages"
    assert api_key == "main-anthropic-key"
    assert model == "claude-sonnet-4-5"
    assert rescue.coord_space == "absolute"

    decision = await rescue.decide(
        goal="g",
        trajectory={"actions": []},
        action={"index": 1, "type": "click", "plan_intent": "点击目标"},
        current_bytes=_test_jpeg(100, 200, (20, 20, 20)),
        miss_reason="target missing",
    )

    assert captured["backend"] == "claude_messages"
    assert captured["api_key"] == "main-anthropic-key"
    assert captured["model"] == "claude-sonnet-4-5"
    assert decision.verdict == "REPAIR_ACTION"
    assert decision.coord_space == "absolute"
    assert decision.repair_action["point"] == {"x": 25, "y": 40}


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


@pytest.mark.parametrize(
    (
        "provider",
        "base_url",
        "api_key",
        "model",
        "expected_backend",
        "expected_url",
    ),
    [
        (
            "claude",
            "https://api.anthropic.com",
            "sk-ant-phone",
            "claude-sonnet-4-5",
            "claude_messages",
            "https://api.anthropic.com/v1/messages",
        ),
        (
            "openai",
            "https://api.openai.com/v1",
            "sk-openai-phone",
            "computer-use-preview",
            "openai_responses",
            "https://api.openai.com/v1/responses",
        ),
    ],
)
def test_new_overseas_config_drives_all_phone_layer_helpers(
    provider,
    base_url,
    api_key,
    model,
    expected_backend,
    expected_url,
):
    import ai_phone.config as cfg

    settings = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider=provider,
        phone_vlm_base_url=base_url,
        phone_vlm_api_key=api_key,
        phone_vlm_model=model,
        aux_provider=provider,
        aux_base_url=base_url,
        aux_api_key=f"{api_key}-aux",
        aux_model="aux-model",
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_gate_enabled=True,
        trajectory_cache_v3_coord_enabled=True,
        trajectory_cache_v3_rescue_enabled=True,
    ))
    expected = (expected_backend, expected_url, api_key, model)

    gate = ephemeral_module.CacheEphemeralGateVerifier(
        settings=settings,
        main_vlm_backend=settings.vlm_backend,
    )
    assert gate._config()[:4] == expected

    recovery = CacheReplayRecoveryVerifier(
        settings=settings,
        main_vlm_backend=settings.vlm_backend,
    )
    assert recovery._resolve_chat_config()[:4] == expected

    locator = V3PlanLocator(settings=settings, main_vlm_backend=settings.vlm_backend)
    assert locator._config()[:4] == expected

    rescue = V3RescueVerifier(settings=settings, main_vlm_backend=settings.vlm_backend)
    assert rescue._config()[:4] == expected


@pytest.mark.asyncio
async def test_ephemeral_classifier_new_claude_config_uses_aux_claude_messages(monkeypatch):
    import ai_phone.config as cfg

    settings = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="claude",
        phone_vlm_base_url="https://api.anthropic.com/v1/messages",
        phone_vlm_api_key="sk-ant-phone",
        phone_vlm_model="claude-sonnet-4-5",
        aux_provider="claude",
        aux_base_url="https://api.anthropic.com/v1/messages",
        aux_api_key="sk-ant-aux",
        aux_model="claude-haiku",
    )).model_copy(update={
        "trajectory_cache_ephemeral_action_enabled": True,
        "trajectory_cache_ephemeral_classify_enabled": True,
    })
    captured: dict = {}

    async def _fake_call(**kwargs):
        captured.update(kwargs)
        return (
            '{"role":"optional_ephemeral","category":"marketing_popup",'
            '"confidence":0.99,"skip_if_absent":true,"reason":"popup close"}'
        )

    monkeypatch.setattr(ephemeral_module, "_call_vlm_with_images", _fake_call)
    classifier = ephemeral_module.CacheEphemeralActionClassifier(settings=settings)
    result = await classifier.classify_action(
        goal="关闭广告后继续播放",
        action={"action_id": "a001", "type": "click"},
        before_bytes=_test_jpeg(80, 120, (20, 20, 20)),
        after_bytes=_test_jpeg(80, 120, (30, 30, 30)),
    )

    assert captured["backend"] == "claude_messages"
    assert captured["api_url"] == "https://api.anthropic.com/v1/messages"
    assert captured["api_key"] == "sk-ant-aux"
    assert captured["model"] == "claude-haiku"
    assert [label for label, _ in captured["images"]] == ["action_before", "action_after"]
    assert result.role == ephemeral_module.ROLE_OPTIONAL_EPHEMERAL
    assert result.skip_if_absent is True


@pytest.mark.asyncio
async def test_ephemeral_classifier_new_gpt_config_uses_aux_chat_not_phone_responses(monkeypatch):
    import ai_phone.config as cfg

    settings = cfg._derive_new_model_config(Settings(
        _env_file=None,
        phone_vlm_provider="openai",
        phone_vlm_base_url="https://api.openai.com/v1",
        phone_vlm_api_key="sk-openai-phone",
        phone_vlm_model="computer-use-preview",
        aux_provider="openai",
        aux_base_url="https://api.openai.com/v1",
        aux_api_key="sk-openai-aux",
        aux_model="gpt-4o-mini",
    )).model_copy(update={
        "trajectory_cache_ephemeral_action_enabled": True,
        "trajectory_cache_ephemeral_classify_enabled": True,
    })
    captured: dict = {}

    async def _fake_call(**kwargs):
        captured.update(kwargs)
        return (
            '{"role":"business_required","category":"case_goal_related",'
            '"confidence":0.98,"skip_if_absent":false,"reason":"business click"}'
        )

    monkeypatch.setattr(ephemeral_module, "_call_vlm_with_images", _fake_call)
    classifier = ephemeral_module.CacheEphemeralActionClassifier(settings=settings)
    result = await classifier.classify_action(
        goal="打开设置页",
        action={"action_id": "a001", "type": "click"},
        before_bytes=_test_jpeg(80, 120, (20, 20, 20)),
        after_bytes=_test_jpeg(80, 120, (30, 30, 30)),
    )

    assert captured["backend"] == "openai_compatible"
    assert captured["api_url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["api_key"] == "sk-openai-aux"
    assert captured["model"] == "gpt-4o-mini"
    assert result.role == "business_required"
    assert result.skip_if_absent is False


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
    assert "anthropic-beta" not in captured["headers"]
    assert "tools" not in captured["payload"]
    assert captured["payload"]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 1024,
    }
    assert captured["payload"]["max_tokens"] == 8192
    user_content = captured["payload"]["messages"][0]["content"]
    assert user_content[0] == {"type": "text", "text": "prompt"}
    assert user_content[1]["type"] == "image"
    assert user_content[1]["source"]["type"] == "base64"


@pytest.mark.asyncio
async def test_recovery_claude_messages_payload_has_no_cu_beta_or_tools(monkeypatch):
    from ai_phone.agent.trajectory_cache import recovery as recovery_module

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"content": [{"type": "text", "text": "Thought: ok\nAction: wait(1000)"}]}

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

    monkeypatch.setattr(recovery_module.httpx, "AsyncClient", FakeAsyncClient)
    verifier = CacheReplayRecoveryVerifier(
        settings=Settings(_env_file=None),
        main_vlm_backend="claude_cu",
    )

    text = await verifier._messages_double_image(
        prompt="recover",
        landmark_bytes=_test_jpeg(20, 30, (10, 10, 10)),
        current_bytes=_test_jpeg(20, 30, (20, 20, 20)),
        api_url="https://api.anthropic.com/v1/messages",
        api_key="sk-ant",
        model="claude-sonnet-4-5",
        timeout_sec=13,
    )

    assert text == "Thought: ok\nAction: wait(1000)"
    assert captured["api_url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant"
    assert "anthropic-beta" not in captured["headers"]
    payload = captured["payload"]
    assert payload["model"] == "claude-sonnet-4-5"
    assert payload["max_tokens"] == 8192
    assert "tools" not in payload
    user_content = payload["messages"][0]["content"]
    assert [item["type"] for item in user_content] == ["text", "image", "image"]
    assert user_content[1]["source"]["type"] == "base64"
    assert user_content[2]["source"]["type"] == "base64"


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
    monkeypatch.setenv("AI_PHONE_PHONE_VLM_PROVIDER", "claude")
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


class CacheStableDriver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.stable_bytes = _test_jpeg(color=(8, 120, 200))
        self.stable_calls = []

    def wait_stable_screenshot_jpeg(self, quality=25, max_side=None, **kwargs):
        self.stable_calls.append(
            {"quality": quality, "max_side": max_side, **kwargs}
        )
        return StabilityResult(
            self.stable_bytes,
            True,
            1234,
            2,
            logs=[{"level": 1, "title": "截图已稳定", "content": "ok"}],
        )


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
        # 分别记录"调用源"，让单测能钉死"哪些日志走 emit_serial、哪些走 emit"。
        # 比如缓存断言 PASS 这条必须走 emit_serial，否则 force_finish 的
        # _drain_serial_queue 排不到它，会出现"Run finished 但断言日志缺失"
        # 的窗口；区分两个 list 后，回归测试可以一眼断言归属。
        self.emit_calls = []
        self.emit_serial_calls = []

    def emit(self, evt):
        self.events.append(evt)
        self.emit_calls.append(evt)

    async def aemit(self, evt):
        # 顺序保序版的 emit；保留是为了覆盖 emitter.aemit 自身的单测
        # （test_emitter_aemit_serializes_log_writes_in_call_order 等），
        # 缓存 / 首跑主路径已切到 emit_serial。
        self.events.append(evt)

    def emit_serial(self, evt):
        # 模拟 ServerRunEmitter.emit_serial：调用方同步入队，FakeEmitter
        # 用 list.append 直接追加；调用顺序 = events 顺序，与生产代码"后台
        # worker FIFO 处理"得到的最终顺序等价，足够单测验证保序。
        self.events.append(evt)
        self.emit_serial_calls.append(evt)

    async def force_finish(self, **kwargs):
        self.finishes.append(kwargs)


class FakeReplayRunner:
    last_init_kwargs = None

    def __init__(self, *, driver, trajectory, log, **kwargs):
        self.driver = driver
        self.trajectory = trajectory
        self.log = log
        self.kwargs = kwargs
        FakeReplayRunner.last_init_kwargs = kwargs

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
        from ai_phone.agent.trajectory_cache.v3_replay import V3LocatorMiss

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
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
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
    # 步骤化日志改造（2026-05-16）：V3 执行后不再做版本3稳定检测（对齐首跑节奏，
    # 只 500ms 观察 + 截图证明）。单步只触发 1 次执行前版本3稳定。详见
    # 内部缓存回放步骤化日志约定。
    assert len(stable_calls) == 1
    assert locator.calls[0]["action"]["plan_intent"] == "点击教材同步"
    assert any(title == "V3寻找目标" and "点击教材同步" in content for _level, title, content in logs)


@pytest.mark.asyncio
async def test_v3_replay_runner_skips_optional_ephemeral_when_gate_says_skip(
    monkeypatch,
    tmp_path,
):
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
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
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
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
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
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
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
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
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
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
async def test_delete_trajectory_cache_for_run_is_idempotent(_test_engine, session):
    session.add(Device(serial="D1", platform="android"))
    run = Run(id="run-cache-fail", device_serial="D1", goal="same goal", status="failed")
    session.add(run)
    cache_key, normalized, semantic_hash = build_cache_key(
        device_code="D1",
        run_semantic_text="same goal",
    )
    session.add(
        VlmTrajectoryCacheV2(
            cache_key=cache_key,
            device_code="D1",
            run_semantic_hash=semantic_hash,
            run_semantic_text=normalized,
            status="active",
            trajectory_json={"actions": []},
        )
    )
    await session.commit()

    deleted = await delete_trajectory_cache_v2_for_run(db_module.get_session_factory(), run.id)
    deleted_again = await delete_trajectory_cache_v2_for_run(db_module.get_session_factory(), run.id)

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
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    # 步骤化日志改造（2026-05-16）：observe_delay 由独立 `缓存稳定` 标题实时
    # 流出来，不再压进 `缓存步骤完成` 汇总块（见 内部缓存回放步骤化日志约定）。
    assert any(
        title == "缓存稳定" and "动作执行后观察 500ms" in content
        for _level, title, content in logs
    )


@pytest.mark.asyncio
async def test_v1_replay_runner_emits_streaming_step_log_skeleton():
    """步骤化日志方案（2026-05-16）核心回归 —— V1 节奏：单步必须按
    "缓存步骤 → 缓存稳定(执行前) → 缓存动作 → 缓存执行 → 缓存完成" 顺序产出
    流式日志，且 `缓存完成` 必须**单行**，避免回退到 9 行汇总块。

    同时锁定"V1 执行后**不**再做版本1稳定检测"：本测试用 monkey 替换的
    `_wait_stable` 计数器在 V1 单步里**只能被调一次**（执行前那次）。
    详见 内部缓存回放步骤化日志约定。
    """
    logs = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content, step=None):
        logs.append({"level": level, "title": title, "content": content, "step": step})

    runner = ReplayRunner(
        driver=driver,
        trajectory={"actions": [{"index": 1, "type": "click", "point": {"x": 1, "y": 2}}]},
        log=log,
        observe_delay_ms=0,
        replay_mode="v1",
    )

    stable_calls = 0

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return SimpleNamespace(bytes_=b"jpeg")

    runner._wait_stable = stable  # type: ignore[method-assign]
    result = await runner.run()
    assert result.success is True

    # V1 改造后：执行前 1 次稳定，执行后改成 `_screenshot_jpeg()` 抓一张 —— 0 次。
    assert stable_calls == 1, (
        f"V1 执行后不应再做版本1稳定检测，期望 1 次（仅执行前），实际={stable_calls}"
    )

    titles = [entry["title"] for entry in logs]
    assert "缓存步骤" in titles
    assert "缓存目标" in titles
    assert "缓存完成" in titles
    # 端点 + 拆分目标行后的标准七拍序列（2026-05-16 二轮改造）：
    # 缓存步骤 → 缓存目标 → 缓存稳定 → 缓存动作 → 缓存执行 → 缓存完成。
    expected_sequence = [
        "缓存步骤", "缓存目标", "缓存稳定", "缓存动作", "缓存执行", "缓存完成",
    ]
    positions = []
    cursor = 0
    for expected in expected_sequence:
        try:
            idx = titles.index(expected, cursor)
        except ValueError as exc:  # pragma: no cover - 失败诊断
            raise AssertionError(
                f"七拍缺失或顺序错误，title 序列={titles}，缺={expected}"
            ) from exc
        positions.append(idx)
        cursor = idx + 1
    assert positions == sorted(positions)
    # `缓存稳定` 只能出现一次（执行前），不能再有第二条"执行后稳定"。
    assert titles.count("缓存稳定") == 1, (
        f"V1 单步只能有 1 条 `缓存稳定`（执行前），实际={titles.count('缓存稳定')}"
    )

    # —— #N 前缀约定：端点行（缓存步骤 / 缓存完成）必须带 step=index，让前端
    # 渲染 `#N` 前缀；过程行（缓存目标 / 缓存稳定 / 缓存动作 / 缓存执行）一律
    # 不带，避免每行都挂前缀视觉噪点。详见 内部缓存回放步骤化日志约定。
    step_entries = [e for e in logs if e["title"] == "缓存步骤"]
    done_entries = [e for e in logs if e["title"] == "缓存完成"]
    target_entries = [e for e in logs if e["title"] == "缓存目标"]
    assert step_entries and all(e["step"] == 1 for e in step_entries), (
        f"`缓存步骤` 必须带 step=index，实际={[e['step'] for e in step_entries]}"
    )
    assert done_entries and all(e["step"] == 1 for e in done_entries), (
        f"`缓存完成` 必须带 step=index，实际={[e['step'] for e in done_entries]}"
    )
    assert target_entries and all(e["step"] is None for e in target_entries), (
        f"`缓存目标` 是过程行，不能带 step，实际={[e['step'] for e in target_entries]}"
    )
    for e in logs:
        if e["title"] in {"缓存稳定", "缓存动作", "缓存执行"}:
            assert e["step"] is None, (
                f"过程行 `{e['title']}` 不能带 step，实际 step={e['step']}"
            )

    # `缓存步骤` content 必须瘦身：只剩 `━━ 第 N 步 / 共 M 步 ━━`，
    # 不能再回退到旧版"━━ 开始第 N 步 / 共 M 步 ━━ 目标=... action_id=..."
    # 那种多塞元信息的格式。
    step_content = step_entries[0]["content"]
    assert step_content == "━━ 第 1 步 / 共 1 步 ━━", (
        f"`缓存步骤` content 应已瘦身，实际={step_content!r}"
    )
    # 目标 + action_id + type 必须落到独立的 `缓存目标` 行。
    target_content = target_entries[0]["content"]
    assert "action_id=" in target_content and "type=" in target_content, (
        f"`缓存目标` 必须包含 action_id 和 type，实际={target_content!r}"
    )

    for entry in done_entries:
        assert "\n" not in entry["content"], (
            f"`缓存完成` 必须单行，实际={entry['content']!r}"
        )
        assert "elapsed=" in entry["content"]
        assert "status=" in entry["content"]


@pytest.mark.asyncio
async def test_v3_replay_runner_step_endpoints_carry_step_index(monkeypatch):
    """V3 步骤端点 ``#N`` 前缀回归（2026-05-16 二轮改造）：

    V3 单步必须把 ``缓存步骤`` / ``缓存完成`` 这两个端点行的 ``step``
    字段设成 ``index``——前端拿到 step 才会渲染 ``#N`` 前缀，让 V3 缓存
    的步骤端点视觉对齐首跑的 ``#N ━━ 第 N 步 ━━`` / ``#N 第 N 步完成``。

    同时锁定 ``缓存目标`` 作为过程行不带 step（避免每行都挂 #N 视觉噪点），
    以及 ``缓存步骤`` content 已瘦身为单纯 ``━━ 第 N 步 / 共 M 步 ━━``。
    """
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

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

    monkeypatch.setattr(v3_replay_module, "wait_page_stable_v2_compare", fake_wait_stable)
    driver = FakeDriver()
    locator = FakeV3Locator()
    logs = []

    async def log(level, title, content, step=None):
        logs.append({"level": level, "title": title, "content": content, "step": step})

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

    step_entries = [e for e in logs if e["title"] == "缓存步骤"]
    done_entries = [e for e in logs if e["title"] == "缓存完成"]
    target_entries = [e for e in logs if e["title"] == "缓存目标"]

    assert step_entries and all(e["step"] == 1 for e in step_entries), (
        f"V3 `缓存步骤` 必须带 step=index，实际={[e['step'] for e in step_entries]}"
    )
    assert done_entries and all(e["step"] == 1 for e in done_entries), (
        f"V3 `缓存完成` 必须带 step=index，实际={[e['step'] for e in done_entries]}"
    )
    assert target_entries and all(e["step"] is None for e in target_entries), (
        f"V3 `缓存目标` 是过程行，不能带 step，实际={[e['step'] for e in target_entries]}"
    )

    step_content = step_entries[0]["content"]
    assert step_content == "━━ 第 1 步 / 共 1 步 ━━", (
        f"V3 `缓存步骤` content 应已瘦身，实际={step_content!r}"
    )
    target_content = target_entries[0]["content"]
    assert "action_id=a001" in target_content and "type=click" in target_content, (
        f"V3 `缓存目标` 必须包含 action_id 和 type，实际={target_content!r}"
    )


@pytest.mark.asyncio
async def test_replay_runner_step_end_arrives_before_next_step_start_log():
    """缓存路径事件顺序铁律（2026-05-16 三轮修复）：

    用户反馈："`#1 第 1 步完成 · click`" 出现在 "`#2 缓存步骤`" 之后。根
    因是 ``_emit_step_end`` 走的是同步 ``emitter.emit`` → 后台
    ``ensure_future``，而下一步的 ``await _log(缓存步骤)`` 走的是同步
    ``await _forward_log`` 立刻入库——两条路径竞争，``EVT_STEP_END``
    被甩到下一步开头之后。

    修复后：``_emit_step_end`` 是 ``async`` + 内部 ``await
    _emit_maybe_await``；service 那层缓存通道传 ``emit=emitter.aemit``
    （顺序保序版）。所以 ``EVT_STEP_END(step=N)`` 必须严格在
    ``EVT_LOG title=缓存步骤 step=N+1`` **之前**进入事件流。

    本测试用一个慢 emit（asyncio.sleep 0.01 模拟后台延迟）+ 真实
    ReplayRunner（2 步动作），验证：``EVT_STEP_END(step=1)`` 一定在
    所有 step=2 的 EVT_LOG 之前。
    """
    from ai_phone.agent.runner.events import EVT_LOG, EVT_STEP_END
    from ai_phone.agent.trajectory_cache import ReplayRunner

    events: list[dict] = []
    log_lock = asyncio.Lock()

    async def emit(evt):
        # 模拟后台 ensure_future 的"延迟"：让没拿到锁的同类事件被排到后面。
        # 真实 emitter.aemit 内部也是用 _serial_lock 串行，这里语义对齐。
        async with log_lock:
            await asyncio.sleep(0.01)
            events.append(evt)

    async def log(level, title, content, step=None):
        await emit({
            "type": EVT_LOG, "level": level, "title": title,
            "content": content, "step": step,
        })

    driver = FakeDriver()
    runner = ReplayRunner(
        driver=driver,
        trajectory={
            "actions": [
                {"index": 1, "type": "click", "point": {"x": 1, "y": 2}},
                {"index": 2, "type": "click", "point": {"x": 3, "y": 4}},
            ]
        },
        run_id="seq-run",
        log=log,
        emit=emit,
        observe_delay_ms=0,
        replay_mode="v1",
    )

    async def stable():
        return SimpleNamespace(bytes_=b"jpeg")

    runner._wait_stable = stable  # type: ignore[method-assign]
    result = await runner.run()
    assert result.success is True

    step_end_1_idx = next(
        i for i, e in enumerate(events)
        if e.get("type") == EVT_STEP_END and e.get("step") == 1
    )
    next_step_log_indices = [
        i for i, e in enumerate(events)
        if e.get("type") == EVT_LOG
        and e.get("step") == 2
        and e.get("title") == "缓存步骤"
    ]
    assert next_step_log_indices, "缺少 `#2 缓存步骤` 端点日志"
    next_step_log_idx = next_step_log_indices[0]
    assert step_end_1_idx < next_step_log_idx, (
        f"EVT_STEP_END(step=1) 必须出现在 `#2 缓存步骤` 之前，实际 "
        f"step_end_1_idx={step_end_1_idx} >= next_step_log_idx={next_step_log_idx}\n"
        f"events 序列={[(i, e.get('type'), e.get('step'), e.get('title')) for i, e in enumerate(events)]}"
    )


@pytest.mark.asyncio
async def test_replay_runner_capture_final_frame_always_waits_for_stability():
    """断言入口不变量（2026-05-16）：``capture_final_frame()`` 必须先 await
    一次 ``_wait_stable()`` 再返回，**即便 ``_final_after_bytes`` 已经被主
    循环填好**。

    背景：V1/V2 主循环里 ``_final_after_bytes`` 是"500ms 观察后随手拍的"
    （V1）或"路标对比命中帧"（V2 主路径）。中间步骤这种动画态没事——下一
    步执行前还会再做稳定/路标对比；但**最后一步**没有下一步，after 帧直
    接喂给断言系统。如果短路返回，最后一击触发跳转时断言会拿到空白图导
    致误判 FAIL。

    本测试钉住"capture_final_frame 必须触发稳定"——回归一旦把短路改回来
    立刻报错。
    """
    from ai_phone.agent.trajectory_cache import ReplayRunner

    driver = FakeDriver()
    runner = ReplayRunner(
        driver=driver,
        trajectory={"actions": [{"index": 1, "type": "click", "point": {"x": 1, "y": 2}}]},
        log=None,
        observe_delay_ms=0,
    )
    stable_calls = 0

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return SimpleNamespace(bytes_=b"stable-final")

    runner._wait_stable = stable  # type: ignore[method-assign]
    runner._final_after_bytes = b"maybe-mid-transition-after-frame"

    frame = await runner.capture_final_frame()

    assert stable_calls == 1, "capture_final_frame() 必须触发一次稳定检测"
    assert frame == b"stable-final", (
        "capture_final_frame() 应当返回稳定后的最新帧，不能短路返回 _final_after_bytes"
    )


@pytest.mark.asyncio
async def test_v3_replay_runner_capture_final_frame_always_waits_for_stability(monkeypatch):
    """断言入口不变量（2026-05-16）：V3 ``capture_final_frame()`` 同 V1/V2，
    必须先 await 一次版本3稳定检测（``_wait_stable``）再返回，即便
    ``_final_after_bytes`` 已经填好。

    V3 单步循环里 V3 执行后改成"500ms 观察 + 截图证明"，没有版本3稳定——
    最后一步的 after 帧可能正好是跳转动画态。如果 capture_final_frame 短
    路返回它，断言会拿到空白图。详见 内部缓存回放步骤化日志约定。
    """
    from ai_phone.agent.trajectory_cache import V3ReplayRunner

    driver = FakeDriver()
    runner = V3ReplayRunner(
        driver=driver,
        trajectory={"actions": []},
    )
    stable_calls = 0

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return b"stable-final-v3"

    runner._wait_stable = stable  # type: ignore[method-assign]
    runner._final_after_bytes = b"maybe-mid-transition-after-frame"

    frame = await runner.capture_final_frame()

    assert stable_calls == 1, "V3 capture_final_frame() 必须触发一次版本3稳定检测"
    assert frame == b"stable-final-v3", (
        "V3 capture_final_frame() 应当返回稳定后的最新帧，不能短路返回 _final_after_bytes"
    )


@pytest.mark.asyncio
async def test_v2_replay_runner_does_not_run_stability_before_action():
    """步骤化日志方案（2026-05-16）—— V2 节奏锁定：

    V2 设计是"先动作后对比"——执行前**不做任何稳定检测**，执行后用
    版本2路标对比作为稳定/正确性的双判定。本测试通过 monkey 替换的
    `_wait_stable` 计数器锁死："V2 整段一次稳定都不该被触发"。
    """
    logs = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import ReplayRunner
    from ai_phone.agent.trajectory_cache import replay as replay_module

    async def log(level, title, content):
        logs.append((level, title, content))

    # 强制版本2路标对比直接命中（避免 fallback 走到 `_wait_stable`）。
    pytest_monkey = pytest.MonkeyPatch()
    try:
        pytest_monkey.setattr(
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
            log=log,
            capture_after_each_action=True,
            observe_delay_ms=0,
            replay_mode="v2",
        )
        runner.alignment_enabled = True
        runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

        stable_calls = 0

        async def stable():
            nonlocal stable_calls
            stable_calls += 1
            return SimpleNamespace(bytes_=b"jpeg")

        runner._wait_stable = stable  # type: ignore[method-assign]
        result = await runner.run()
        assert result.success is True
        assert stable_calls == 0, (
            f"V2 执行前/执行后都不该走版本1稳定，实际触发={stable_calls} 次"
        )
    finally:
        pytest_monkey.undo()


@pytest.mark.asyncio
async def test_replay_runner_ephemeral_gate_skip_does_not_click(tmp_path):
    logs = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    # 步骤化日志改造（2026-05-16）：V2 设计原则是"先动作后对比"，执行前
    # 不再做版本1稳定检测。两步都通过版本2路标对比 + carry 路径，整段回放
    # 一次稳定检测都不应触发。详见 内部缓存回放步骤化日志约定。
    assert stable_calls == 0
    assert sleeps == [0.5, 0.5]
    assert any("对齐成功 action_id=a001" in content for _level, title, content in logs if title == "轨迹缓存状态路标")
    assert any("复用上一 action 路标帧作为 #2 before" in content for _level, title, content in logs if title == "轨迹缓存状态路标")


@pytest.mark.asyncio
async def test_replay_runner_unavailable_landmark_uses_historical_action_gap(monkeypatch):
    logs = []
    sleeps = []
    stable_calls = 0
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
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
                    "status": "unavailable",
                    "missing_reason": "image_url_empty",
                    "timing": {"gap_to_next_action_ms": 1800},
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
    )
    runner.alignment_enabled = True

    async def stable():
        nonlocal stable_calls
        stable_calls += 1
        return SimpleNamespace(bytes_=b"stable")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    # 步骤化日志改造（2026-05-16）：V2 执行前不再做版本1稳定。本例：
    #   - 第 1 步 a001 landmark unavailable → historical_gap 兜底（不调稳定）
    #     + carry 给 #2 当 before
    #   - 第 2 步 a002 没有 landmark → V2 fallback _wait_stable_for_step("执行后")
    #     调一次稳定（这是用户拍板**保留**的 fallback，几乎不触发）
    # 所以期望 stable_calls == 1（fallback 那次）。
    assert stable_calls == 1
    assert sleeps == [0.5, 1.3, 0.5]
    assert any(
        "目标图不可用" in content and "按首次真实间隔兜底等待" in content
        for _level, title, content in logs
        if title == "轨迹缓存状态路标"
    )
    assert any(
        "按首次成功交接间隔等待 1300ms" in content
        for _level, title, content in logs
        if title == "轨迹缓存状态路标"
    )
    assert any("复用上一 action 路标帧作为 #2 before" in content for _level, title, content in logs if title == "轨迹缓存状态路标")


@pytest.mark.asyncio
async def test_replay_runner_alignment_also_handles_wait_action(monkeypatch):
    sleeps = []
    stable_calls = 0
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    # 步骤化日志改造（2026-05-16）：V2 执行前不再做稳定检测；版本2路标对比
    # 成功后 carry 给（虚拟的）下一步。本测试只有 1 步，整段一次稳定都不触发。
    assert stable_calls == 0
    assert sleeps == [3, 0.5]


@pytest.mark.asyncio
async def test_replay_runner_alignment_miss_uses_historical_gap_then_stops_replay(monkeypatch):
    logs = []
    sleeps = []
    stable_calls = 0
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    # 步骤化日志改造（2026-05-16）：V2 执行前不再做版本1稳定检测；本例
    # 路标对比失败后停在 alignment_miss，整段不应触发任何稳定检测。
    assert stable_calls == 0
    assert sleeps == [0.5, 0.1]
    assert any("执行后截图比对 action_id=a001" in content and "首次真实间隔=600ms" in content for _level, title, content in logs if title == "轨迹缓存状态路标")
    assert any("截图不一致 action_id=a001" in content and "按首次真实间隔再等待 100ms" in content for _level, title, content in logs if title == "轨迹缓存状态路标")
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
    assert "handoff 路标图）本身就是加载中" in prompt
    assert "让缓存里的 wait 自己执行" in prompt
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
async def test_recovery_verifier_chat_failure_falls_back_to_wait_more(monkeypatch):
    """recovery 调用异常时默认 WAIT_MORE 一次（让上层再试），而不是直接 ASSERT_FAIL。

    见 _recovery_call_failure_fallback 注释：第二次仍失败时会被
    ReplayRunner 的 max_wait_more 上限自然降级到 ASSERT_FAIL，不会无限循环；
    给瞬态网络抖动 / 模型偶发空响应一个自愈窗口。
    """
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

    assert decision.verdict == VERDICT_WAIT_MORE
    assert decision.error == "RuntimeError"
    assert "network unreachable" in decision.reason
    assert decision.wait_ms > 0


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

    async def _fake_messages(**kwargs):
        called["prompt_head"] = kwargs["prompt"][:60]
        called["landmark_bytes"] = kwargs["landmark_bytes"]
        called["current_bytes"] = kwargs["current_bytes"]
        called["api_url"] = kwargs["api_url"]
        called["api_key"] = kwargs["api_key"]
        called["model"] = kwargs["model"]
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
    # 显式配置 backend=claude_messages → 用 trajectory_cache_recovery_vlm_* 自身的 url/key/model
    assert called.get("api_url") == "https://api.anthropic.com/v1/messages"
    assert called.get("api_key") == "sk-ant-fake"
    assert called.get("model") == "claude-sonnet-4-5"
    # Claude 主 VLM → coord_space 应被推断成 absolute（C-3 派发逻辑）
    assert decision.verdict == VERDICT_REPAIR_ACTION
    assert decision.parsed_actions[0].coord_space == "absolute"
    assert decision.parsed_actions[0].point == [540, 1024]


@pytest.mark.asyncio
async def test_recovery_overseas_claude_cu_falls_back_to_chat_messages(monkeypatch):
    """海外 claude_cu 主 vlm + recovery 未独立配置 → 走 claude_messages chat 协议。

    见 docs/executable-logic-contract.md §14：辅助 vlm（recovery）不再走 CU
    agent loop，而是用主 vlm 的 model + key + url，按 chat 单次协议调
    （anthropic /v1/messages 同 endpoint，但不打 CU beta header / 不挂 computer
    工具）。坐标空间仍按主 vlm 习惯走 absolute。
    """
    settings = Settings(
        _env_file=None,
        vlm_backend="claude_cu",
        vlm_api_url="https://api.anthropic.com/v1/messages",
        vlm_api_key="main-anthropic-key",
        vlm_model="claude-sonnet-4-5",
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_api_url="",
        trajectory_cache_recovery_vlm_api_key="",
        trajectory_cache_recovery_vlm_model="",
        trajectory_cache_recovery_vlm_timeout_sec=30,
    )
    verifier = CacheReplayRecoveryVerifier(
        settings=settings, main_vlm_backend="claude_cu"
    )
    assert verifier._main_vlm_is_overseas_cu() is True
    backend, api_url, api_key, model, _timeout = verifier._resolve_chat_config()
    assert backend == "claude_messages"
    assert api_url == "https://api.anthropic.com/v1/messages"
    assert api_key == "main-anthropic-key"
    assert model == "claude-sonnet-4-5"

    captured: dict = {}

    async def _fake_messages(**kwargs):
        captured.update(kwargs)
        return "Thought: 点击当前页按钮。\nAction: click(point='<point>130 74</point>')"

    async def _no_chat_completions(**_kwargs):  # pragma: no cover - 防御
        raise AssertionError("claude_cu → 不应路由到 chat completions")

    async def _no_responses(**_kwargs):  # pragma: no cover - 防御
        raise AssertionError("claude_cu → 不应路由到 responses")

    monkeypatch.setattr(verifier, "_messages_double_image", _fake_messages)
    monkeypatch.setattr(
        verifier, "_chat_completions_double_image", _no_chat_completions
    )
    monkeypatch.setattr(verifier, "_responses_double_image", _no_responses)

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

    assert captured["api_key"] == "main-anthropic-key"
    assert captured["model"] == "claude-sonnet-4-5"
    assert captured["api_url"] == "https://api.anthropic.com/v1/messages"
    assert decision.verdict == VERDICT_REPAIR_ACTION
    assert decision.parsed_actions[0].coord_space == "absolute"
    assert decision.parsed_actions[0].point == [130, 74]


@pytest.mark.asyncio
async def test_recovery_overseas_gpt_cu_uses_openai_responses(monkeypatch):
    """海外 gpt_cu 主 vlm → recovery 用 openai_responses 单次视觉判断。"""
    settings = Settings(
        _env_file=None,
        vlm_backend="gpt_cu",
        vlm_api_url="https://api.openai.com/v1/responses",
        vlm_api_key="main-openai-key",
        vlm_model="gpt-4o",
        trajectory_cache_recovery_vlm_enabled=True,
        trajectory_cache_recovery_vlm_api_url="",
        trajectory_cache_recovery_vlm_api_key="",
        trajectory_cache_recovery_vlm_model="",
    )
    verifier = CacheReplayRecoveryVerifier(
        settings=settings, main_vlm_backend="gpt_cu"
    )
    backend, api_url, api_key, model, _timeout = verifier._resolve_chat_config()
    assert backend == "openai_responses"
    assert api_url == "https://api.openai.com/v1/responses"
    assert api_key == "main-openai-key"
    assert model == "gpt-4o"

    captured: dict = {}

    async def _fake_chat(**kwargs):
        captured.update(kwargs)
        return "Thought: 点击当前页按钮。\nAction: click(point='<point>130 74</point>')"

    monkeypatch.setattr(verifier, "_openai_responses_double_image", _fake_chat)

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

    assert captured["api_url"] == "https://api.openai.com/v1/responses"
    assert captured["api_key"] == "main-openai-key"
    assert decision.verdict == VERDICT_REPAIR_ACTION
    # gpt 也按 absolute 像素坐标走
    assert decision.parsed_actions[0].coord_space == "absolute"


@pytest.mark.asyncio
async def test_recovery_openai_responses_payload_shape(monkeypatch):
    from ai_phone.agent.trajectory_cache import recovery as recovery_module

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Thought: ok\nAction: click(point='<point>1 2</point>')",
                            }
                        ],
                    }
                ]
            }

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

    monkeypatch.setattr(recovery_module.httpx, "AsyncClient", FakeAsyncClient)
    verifier = CacheReplayRecoveryVerifier(
        settings=Settings(_env_file=None, vlm_main_reasoning_effort="high"),
        main_vlm_backend="gpt_cu",
    )

    text = await verifier._openai_responses_double_image(
        prompt="recover",
        landmark_bytes=_test_jpeg(20, 30, (10, 10, 10)),
        current_bytes=_test_jpeg(20, 30, (20, 20, 20)),
        api_url="https://api.openai.com/v1/responses",
        api_key="sk-openai",
        model="computer-use-preview",
        timeout_sec=14,
    )

    assert text == "Thought: ok\nAction: click(point='<point>1 2</point>')"
    assert captured["api_url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer sk-openai"
    payload = captured["payload"]
    assert payload["model"] == "computer-use-preview"
    assert payload["reasoning"] == {"effort": "high"}
    assert "tools" not in payload
    assert "messages" not in payload
    user_content = payload["input"][1]["content"]
    assert [item["type"] for item in user_content] == [
        "input_text",
        "input_image",
        "input_image",
    ]


@pytest.mark.asyncio
async def test_recovery_verifier_unknown_backend_falls_back_to_wait_more():
    """未知 backend 时 _chat_double_image 抛 RuntimeError → 走 fallback WAIT_MORE。

    错误信息仍要列出三家可选 backend，方便用户自查 .env。
    """
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

    assert decision.verdict == VERDICT_WAIT_MORE
    assert "doubao_responses" in decision.reason
    assert "openai_compatible" in decision.reason
    assert "openai_responses" in decision.reason
    assert "claude_messages" in decision.reason


@pytest.mark.asyncio
async def test_replay_runner_recovery_continue_accepts_current_frame(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
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
    from ai_phone.agent.trajectory_cache.replay import _decode_image_size

    buf = io.BytesIO()
    Image.new("RGB", (640, 360), color=(255, 0, 0)).save(buf, format="JPEG", quality=70)
    assert _decode_image_size(buf.getvalue()) == (640, 360)
    assert _decode_image_size(b"") is None
    assert _decode_image_size(None) is None
    assert _decode_image_size(b"\x00\x01\x02not a real image") is None


@pytest.mark.asyncio
async def test_replay_runner_recovery_repair_action_absolute_rescales_to_device(monkeypatch):
    """Bug 修复回归：claude_cu / gpt_cu 路径下，模型看到的"附图 2"是按
    backend 协议参数压缩后的 JPEG（_screenshot_jpeg 内决定具体 quality /
    max_long_edge），模型按这个尺寸输出 absolute 坐标，**必须**按
    (model_image_size → device_window_size) 等比缩回设备坐标，不能直接 clamp。

    本测试不验证压缩参数本身，只验证"模型坐标 → 设备坐标"的等比缩放语义，
    所以为构造方便用 720x360 当作模型送图尺寸（与生产 1568 上限解耦）：
    device 1000x2000，模型回 (320, 100)：
      - 旧实现：直接 clamp → (320, 100) ← 错位（落在屏幕中上区）
      - 新实现：等比 → (320*1000/720=444, 100*2000/360=555) ← 正确落点
    """
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    # V2 主线：执行后截图 MISS → 按首次真实间隔补等后仍 MISS →
    # 进入 recovery_vlm；WAIT_MORE 等待结束后的 recheck 才命中 MATCH。
    compare_results = [
        {  # 执行后截图：MISS
            "match": False, "global_diff": 0.04, "center_mae": 0.30,
            "black_ratio_diff": 0.0, "reason": "global>0.0300",
        },
        {  # 按首次真实间隔补等后：MISS
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
async def test_replay_runner_final_action_uses_handoff_wait_ms(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

    async def log(level, title, content):
        logs.append((level, title, content))

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    compare_results = [
        {
            "match": False,
            "global_diff": 0.25,
            "center_mae": 0.02,
            "black_ratio_diff": 0.0,
            "reason": "global>0.0300",
        },
        {
            "match": True,
            "global_diff": 0.01,
            "center_mae": 0.01,
            "black_ratio_diff": 0.0,
            "reason": "match",
        },
    ]
    compare_calls = {"i": 0}

    def fake_compare(**_kwargs):
        i = compare_calls["i"]
        compare_calls["i"] += 1
        return compare_results[min(i, len(compare_results) - 1)]

    monkeypatch.setattr(replay_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(replay_module, "_compare_alignment", fake_compare)

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
                    "timing": {
                        "gap_to_next_action_ms": None,
                        "handoff_wait_ms": 1500,
                    },
                },
            ],
        },
        log=log,
        capture_after_each_action=True,
        observe_delay_ms=500,
        goal="g",
    )
    runner.alignment_enabled = True
    runner._landmark_image_bytes = lambda _landmark: b"ref"  # type: ignore[method-assign]

    async def stable():
        return SimpleNamespace(bytes_=b"before")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert sleeps == [0.5, 1.0]
    assert any(
        "执行后截图比对 action_id=a001" in content and "首次真实间隔=1500ms" in content
        for _level, title, content in logs
        if title == "轨迹缓存状态路标"
    )
    assert any(
        "按首次真实间隔再等待 1000ms" in content
        for _level, title, content in logs
        if title == "轨迹缓存状态路标"
    )


@pytest.mark.asyncio
async def test_replay_runner_recovery_wait_more_exhausts_quota(monkeypatch):
    logs: list = []
    sleeps: list = []
    driver = FakeDriver()
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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
    from ai_phone.agent.trajectory_cache import replay as replay_module
    from ai_phone.agent.trajectory_cache import ReplayRunner

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


# ---------------------------------------------------------------------------
# 辅助 VLM 截图压缩参数：必须与首跑主 VLM 同 backend 同口径，
# 否则 claude_cu / gpt_cu 这类对画质敏感的 Computer Use 模型会因低画质识别失准
# 给出看似合理但落空的坐标。三处入口（agent/runner/vlm_loop.py、replay.py、
# v3_replay.py）按"高冗余低耦合"独立内联，下面四个测试每条线路分别钉死参数。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v1v2_replay_screenshot_uses_doubao_full_resolution_params(monkeypatch):
    from ai_phone.agent.trajectory_cache import replay as replay_module

    settings = Settings(_env_file=None, vlm_backend="doubao_responses")
    monkeypatch.setattr(replay_module, "get_settings", lambda: settings)

    driver = FakeDriver()
    runner = replay_module.V2ReplayRunner(
        driver=driver,
        trajectory={"actions": []},
    )
    await runner._screenshot_jpeg()

    screenshot_calls = [c for c in driver.calls if c[0] == "screenshot_jpeg"]
    assert screenshot_calls == [("screenshot_jpeg", 95, None)]


@pytest.mark.asyncio
async def test_v1v2_replay_screenshot_uses_claude_cu_high_quality_params(monkeypatch):
    from ai_phone.agent.trajectory_cache import replay as replay_module

    settings = Settings(_env_file=None, vlm_backend="claude_cu")
    monkeypatch.setattr(replay_module, "get_settings", lambda: settings)

    driver = FakeDriver()
    runner = replay_module.V2ReplayRunner(
        driver=driver,
        trajectory={"actions": []},
    )
    await runner._screenshot_jpeg()

    screenshot_calls = [c for c in driver.calls if c[0] == "screenshot_jpeg"]
    assert screenshot_calls == [("screenshot_jpeg", 90, 1568)]


@pytest.mark.asyncio
async def test_v2_replay_wait_stable_keeps_server_side_path(monkeypatch):
    from ai_phone.agent.trajectory_cache import replay as replay_module

    settings = Settings(
        _env_file=None,
        vlm_backend="doubao_responses",
        trajectory_cache_page_stable_enabled=False,
    )
    monkeypatch.setattr(replay_module, "get_settings", lambda: settings)

    async def fake_wait_stable(screenshot, frame_a_bytes=None, **kwargs):
        assert kwargs["use_cache_settings"] is True
        data = await screenshot()
        return StabilityResult(data, True, 111, 1)

    monkeypatch.setattr(replay_module, "wait_page_stable_pixel", fake_wait_stable)

    driver = CacheStableDriver()
    runner = replay_module.V2ReplayRunner(
        driver=driver,
        trajectory={"actions": []},
    )
    result = await runner._wait_stable()

    assert result.bytes_ == b"jpeg"
    assert driver.stable_calls == []
    assert [c for c in driver.calls if c[0] == "screenshot_jpeg"] == [
        ("screenshot_jpeg", 95, None)
    ]


@pytest.mark.asyncio
async def test_v3_replay_screenshot_uses_doubao_full_resolution_params(monkeypatch):
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

    settings = Settings(_env_file=None, vlm_backend="doubao_responses")
    monkeypatch.setattr(v3_replay_module, "get_settings", lambda: settings)

    driver = FakeDriver()
    runner = V3ReplayRunner(driver=driver, trajectory={"actions": []})
    await runner._screenshot_jpeg()

    screenshot_calls = [c for c in driver.calls if c[0] == "screenshot_jpeg"]
    assert screenshot_calls == [("screenshot_jpeg", 95, None)]


@pytest.mark.asyncio
async def test_v3_replay_screenshot_uses_claude_cu_high_quality_params(monkeypatch):
    from ai_phone.agent.trajectory_cache import v3_replay as v3_replay_module

    settings = Settings(_env_file=None, vlm_backend="claude_cu")
    monkeypatch.setattr(v3_replay_module, "get_settings", lambda: settings)

    driver = FakeDriver()
    runner = V3ReplayRunner(driver=driver, trajectory={"actions": []})
    await runner._screenshot_jpeg()

    screenshot_calls = [c for c in driver.calls if c[0] == "screenshot_jpeg"]
    assert screenshot_calls == [("screenshot_jpeg", 90, 1568)]
