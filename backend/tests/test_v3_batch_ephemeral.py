"""V3 专属整批分类与当前帧 gate；不共享 V2 业务函数，不冒充真机验收。"""

import json
from copy import deepcopy

import pytest

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache import archive, ephemeral, v3_ephemeral, v3_replay
from ai_phone.agent.trajectory_cache.batch_plan_cleaner import (
    BatchPlanValidationError,
    validate_batch_plan_output,
)


def popup(**changes):
    return {
        "role": "optional_ephemeral",
        "category": "upgrade_popup",
        "confidence": 0.95,
        "skip_if_absent": True,
        "business_risk": "low",
        "reason": "升级提示清障后继续业务",
        **changes,
    }


def output(actions):
    return {
        "actions": [
            {
                "action_id": a["action_id"],
                "plan_intent": "点击升级提示的取消按钮",
                "confidence": 0.95,
                "reason": "描述真实动作",
                "ephemeral": popup(),
            }
            for a in actions
        ]
    }


def settings():
    return Settings(
        _env_file=None,
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_classify_enabled=True,
        assistant_api_key="unit-key",
        assistant_api_url="https://unit.invalid/chat/completions",
        assistant_model="unit-model",
        aux_reasoning_effort="high",
        trajectory_cache_ephemeral_classifier_api_url="",
        trajectory_cache_ephemeral_classifier_api_key="",
        trajectory_cache_ephemeral_classifier_model="",
    )


@pytest.mark.asyncio
async def test_fifteen_actions_have_one_text_request_and_no_v2_classifier_or_images(monkeypatch):
    source = [
        {
            "action_id": f"a{i}",
            "source_step": i,
            "type": "click",
            "thought": "升级弹窗挡住首页，先取消后继续业务",
            "plan_intent": "点击升级提示的取消按钮",
            "point": {"x": 20, "y": 30},
            "raw": "click",
            "role": "business_required",
        }
        for i in range(15)
    ]
    before = deepcopy(source)
    monkeypatch.setattr(archive, "get_settings", settings)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return json.dumps(output(source), ensure_ascii=False)

    async def forbidden(*args, **kwargs):
        pytest.fail("V3 不应调用逐条分类或 V2 分类")

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    monkeypatch.setattr(archive, "_classify_ephemeral_actions", forbidden)
    monkeypatch.setattr(ephemeral.CacheEphemeralActionClassifier, "classify_action", forbidden)
    payload = {"actions": source, "meta": {}}
    await archive._clean_v3_plan_intents(payload=payload, goal="进入首页")
    assert len(calls) == 1 and calls[0]["images"] == []
    assert "【同批任务：瞬态清障标记】" in calls[0]["prompt"]
    assert payload["meta"]["ephemeral_optional_actions"] == 15
    assert payload["meta"]["plan_intent_batch_model_calls"] == 1
    for old, new in zip(before, source):
        assert new["role"] == "optional_ephemeral"
        assert new["ephemeral_meta"]["classification_source"] == "v3_batch_semantic"
        for key in ("type", "point", "raw", "thought", "source_step", "action_id"):
            assert new[key] == old[key]


@pytest.mark.asyncio
async def test_popup_structure_errors_repair_same_batch_before_any_merge(monkeypatch):
    source = [
        {
            "action_id": "a1",
            "type": "click",
            "thought": "关闭升级提示",
            "role": "business_required",
            "plan_intent": "点击取消",
        }
    ]
    before = deepcopy(source)
    valid = output(source)
    invalid = deepcopy(valid)
    invalid["actions"][0]["ephemeral"]["skip_if_absent"] = "false"
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        assert source == before
        return json.dumps(invalid if len(calls) == 1 else valid, ensure_ascii=False)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    result = await archive.V3PlanIntentCleaner(settings=settings()).clean_actions(actions=source)
    assert result["model_calls"] == 2 and result["repair_rounds"] == 1
    assert "ephemeral.skip_if_absent 必须是布尔值" in calls[1]["prompt"]
    assert source == before


@pytest.mark.asyncio
async def test_invalid_popup_batch_exhausts_bounded_repairs_without_partial_archive(monkeypatch):
    source = [
        {
            "action_id": "a1",
            "type": "click",
            "thought": "关闭升级提示",
            "role": "business_required",
            "plan_intent": "点击取消",
        }
    ]
    payload = {"actions": source, "meta": {}}
    before = deepcopy(payload)
    invalid = output(source)
    invalid["actions"][0]["ephemeral"].pop("category")
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return json.dumps(invalid)

    monkeypatch.setattr(archive, "get_settings", settings)
    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    with pytest.raises(BatchPlanValidationError, match="本次不归档"):
        await archive._clean_v3_plan_intents(payload=payload, goal="进入首页")
    assert len(calls) == 3 and payload == before


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["before_bytes", "after_bytes", "both"])
async def test_v3_semantic_tagging_needs_no_first_run_images(monkeypatch, missing):
    monkeypatch.setattr(archive, "get_settings", settings)
    steps = [
        {
            "step": 1,
            "thought": "升级弹窗挡住首页，点击取消后继续",
            "actions": [{"action": "click", "point": [10, 20]}],
            "before_bytes": b"before",
            "after_bytes": b"after",
        }
    ]
    if missing == "both":
        steps[0].pop("before_bytes")
        steps[0].pop("after_bytes")
    else:
        steps[0].pop(missing)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        inputs = json.loads(kwargs["prompt"].split("全部 action：", 1)[1].split("\n", 1)[0])
        return json.dumps(output(inputs), ensure_ascii=False)

    async def no_upload(data):
        pytest.fail("V3 语义分类不应上传首跑图片")

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    result = await archive.build_v3_archive(
        goal="进入首页",
        device_serial="unit",
        source_run_id="unit-run",
        steps=steps,
        upload_image=no_upload,
    )
    assert len(calls) == 1 and calls[0]["images"] == []
    assert result["actions"][0]["role"] == "optional_ephemeral"


@pytest.mark.parametrize(
    "field,value",
    [
        ("skip_if_absent", "false"),
        ("confidence", True),
        ("confidence", float("nan")),
        ("confidence", 1.1),
        ("role", []),
        ("category", {}),
        ("business_risk", []),
        ("reason", None),
        ("point", {"x": 10, "y": 20}),
    ],
)
def test_popup_fields_strictly_validate_without_crash_or_extra_execution_fields(field, value):
    data = output([{"action_id": "a1"}])
    data["actions"][0]["ephemeral"][field] = value
    with pytest.raises(BatchPlanValidationError):
        validate_batch_plan_output(json.dumps(data), ["a1"], classify_ephemeral=True)


@pytest.mark.parametrize(
    "change",
    [
        {"role": "business_required"},
        {"category": "login_or_security"},
        {"category": "case_goal_related"},
        {"category": "confirm_modal"},
        {"category": "permission_required"},
        {"category": "uncertain"},
        {"business_risk": "high"},
        {"skip_if_absent": False},
        {"confidence": 0.1},
    ],
)
def test_semantic_classification_conservatively_keeps_required(change):
    assert (
        v3_ephemeral.batch_ephemeral_metadata(
            {"type": "click"},
            popup(**change),
            min_confidence=0.85,
        )
        is None
    )


@pytest.mark.parametrize("action_type", ["type", "scroll", "drag", "open_app", "close_app", "wait"])
def test_non_popup_actions_cannot_be_marked_optional(action_type):
    assert (
        v3_ephemeral.batch_ephemeral_metadata(
            {"type": action_type},
            popup(),
            min_confidence=0.85,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_images", [False, True])
async def test_v3_owned_gate_supports_current_only_and_old_three_image_cache(monkeypatch, legacy_images):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return '{"verdict":"SKIP","reason":"当前无升级弹窗且后续首页按钮可见"}'

    def forbidden(*args, **kwargs):
        pytest.fail("V3 不得复用 V2 gate/prompt/parser")

    monkeypatch.setattr(v3_ephemeral, "_call_vlm_with_images", call)
    monkeypatch.setattr(ephemeral.CacheEphemeralGateVerifier, "decide", forbidden)
    monkeypatch.setattr(ephemeral, "build_ephemeral_gate_prompt", forbidden)
    monkeypatch.setattr(ephemeral, "parse_ephemeral_gate_response", forbidden)
    verifier = v3_ephemeral.V3EphemeralGateVerifier(settings=settings())
    monkeypatch.setattr(
        verifier,
        "_config",
        lambda: (
            "openai_compatible",
            "https://unit.invalid/chat/completions",
            "unit-key",
            "unit-model",
            300.0,
        ),
    )
    decision = await verifier.decide(
        goal="进入首页",
        action={"type": "click", "plan_intent": "点击升级取消"},
        current_bytes=b"current",
        cached_popup_before_bytes=b"before" if legacy_images else None,
        cached_after_bytes=b"after" if legacy_images else None,
    )
    assert decision.verdict == "SKIP"
    assert len(calls[0]["images"]) == (3 if legacy_images else 1)
    assert "点击升级取消" in calls[0]["prompt"]
    assert ("本次只附一张" in calls[0]["prompt"]) is not legacy_images
    assert v3_replay.CacheEphemeralGateVerifier is v3_ephemeral.V3EphemeralGateVerifier
    assert v3_replay.CacheEphemeralGateVerifier is not ephemeral.CacheEphemeralGateVerifier


def test_rescue_prompt_declares_generic_rescue_identity_not_a_business_route():
    prompt = v3_replay.build_v3_rescue_prompt(
        goal="原始业务目标",
        trajectory={},
        action={"type": "click", "plan_intent": "点击目标"},
        previous_action=None,
        next_action=None,
        miss_reason="无",
        coord_space="normalized",
    )
    assert "你的身份是救援模型" in prompt
    assert "恢复到能够继续当前或后续缓存步骤的状态" in prompt
    assert "不是从头执行整个 Case" in prompt
    assert "不要把动作调用完成当成恢复成功" in prompt
    assert "洋葱" not in prompt and "应用抽屉" not in prompt
