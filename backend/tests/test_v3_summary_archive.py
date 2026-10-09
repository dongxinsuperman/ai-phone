"""A Case uses one archive mode; copied summaries cannot be rewritten by the model."""
from copy import deepcopy
import json

import pytest

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache import archive
from ai_phone.agent.trajectory_cache.batch_plan_cleaner import BatchPlanValidationError
from ai_phone.agent.trajectory_cache.v3_replay import build_v3_locator_prompt


def source_actions():
    return [
        {"action_id": "a1", "source_step": 1, "type": "close_app", "app_name": "com.demo",
         "plan_intent": "关闭应用", "thought": ""},
        {"action_id": "a2", "source_step": 2, "type": "open_app", "app_name": "com.demo",
         "plan_intent": "打开应用", "thought": ""},
        {"action_id": "a3", "source_step": 3, "type": "click", "point": {"x": 80, "y": 20},
         "action_summary": "点击阻挡首页的升级提示右上角关闭按钮。",
         "plan_intent": "旧候选", "thought": "OLD_THOUGHT_MUST_NOT_LEAK"},
        {"action_id": "a4", "source_step": 4, "type": "click", "point": {"x": 20, "y": 80},
         "action_summary": "点击底部导航栏的「我的」按钮。",
         "plan_intent": "旧候选", "thought": "OLD_THOUGHT_MUST_NOT_LEAK"},
    ]


def classified_output(actions):
    return {"actions": [
        {"action_id": a["action_id"], "ephemeral": {
            "role": "optional_ephemeral" if a["action_id"] == "a3" else "business_required",
            "category": "upgrade_popup" if a["action_id"] == "a3" else "case_goal_related",
            "confidence": .95, "skip_if_absent": a["action_id"] == "a3",
            "business_risk": "low", "reason": "升级提示清障" if a["action_id"] == "a3" else "Case 必需动作",
        }} for a in actions
    ]}


@pytest.fixture
def settings(monkeypatch):
    value = Settings(
        _env_file=None, assistant_api_url="https://unit.invalid/chat/completions",
        assistant_api_key="unit", assistant_model="unit", aux_reasoning_effort="high",
        trajectory_cache_ephemeral_classifier_api_url="",
        trajectory_cache_ephemeral_classifier_api_key="",
        trajectory_cache_ephemeral_classifier_model="",
        trajectory_cache_ephemeral_action_enabled=True,
        trajectory_cache_ephemeral_classify_enabled=True,
        trajectory_cache_ephemeral_classifier_timeout_sec=300,
    )
    monkeypatch.setattr(archive, "get_settings", lambda: value)
    return value


@pytest.mark.asyncio
async def test_complete_case_copies_full_summaries_and_only_classifies_popups(monkeypatch, settings):
    source = source_actions()
    # Longer than the old 120-character cleaner limit, including trailing punctuation.
    source[-1]["action_summary"] = "点击页面底部「我的」按钮（" + "补充可见区域信息" * 18 + "）。"
    assert 120 < len(source[-1]["action_summary"]) <= 300
    before = deepcopy(source)
    requests = []

    async def call(**kwargs):
        requests.append(kwargs)
        output = classified_output(source)
        output["actions"].reverse()  # Must still merge by original ID/order.
        return json.dumps(output, ensure_ascii=False)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    payload = {"actions": source, "meta": {}}
    await archive._clean_v3_plan_intents(payload=payload, goal="关闭并打开应用，然后进入我的页面")

    assert len(requests) == 1
    request = requests[0]
    assert "唯一任务" in request["prompt"]
    assert archive._v3_plan_cleaner_rules() not in request["prompt"]
    assert "plan_intent" not in request["prompt"]
    assert "OLD_THOUGHT_MUST_NOT_LEAK" not in request["prompt"]
    assert request["images"] == [] and request["aux_reasoning_effort"] == "high"
    for old, new in zip(before, source):
        assert new["plan_intent"] == old.get("action_summary", old["plan_intent"])
        for key in old.keys() - {"plan_intent"}:
            assert new[key] == old[key]
    assert source[2]["role"] == "optional_ephemeral"
    assert source[2]["ephemeral_meta"]["skip_if_absent"] is True
    assert source[3]["role"] == "business_required"
    assert source[3]["plan_intent_meta"] == {"source": "action_summary"}
    assert payload["meta"]["plan_intent_cleaner"] == "action_summary"
    assert payload["meta"]["plan_intent_reused_actions"] == 2
    prompt = build_v3_locator_prompt(goal="goal", trajectory=payload, action=source[-1], coord_space="absolute")
    assert source[-1]["action_summary"] in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "", 123, "x" * 301])
async def test_one_missing_summary_sends_entire_case_through_existing_cleaner(monkeypatch, settings, missing):
    source = source_actions()
    source[-1]["action_summary"] = missing
    source[-1]["thought"] = "LEGACY_FALLBACK"
    requests = []

    async def call(**kwargs):
        requests.append(kwargs)
        data = classified_output(source)
        for i, row in enumerate(data["actions"]):
            row.update(plan_intent=f"点击旧清洗结果{i}", confidence=.9, reason="旧清洗")
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    payload = {"actions": source, "meta": {}}
    await archive._clean_v3_plan_intents(payload=payload, goal="goal")
    assert len(requests) == 1
    assert archive._v3_plan_cleaner_rules() in requests[0]["prompt"]
    assert "LEGACY_FALLBACK" in requests[0]["prompt"]
    assert "OLD_THOUGHT_MUST_NOT_LEAK" not in requests[0]["prompt"]
    assert payload["meta"]["plan_intent_cleaner"] == "model"
    assert "plan_intent_reused_actions" not in payload["meta"]
    assert all(a["plan_intent_meta"]["source"] == "v3_plan_cleaner" for a in source)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["trajectory_cache_ephemeral_action_enabled", "trajectory_cache_ephemeral_classify_enabled"])
async def test_complete_case_needs_no_model_when_popup_classification_is_disabled(monkeypatch, settings, flag):
    setattr(settings, flag, False)

    async def forbidden(**kwargs):
        pytest.fail("No model work is necessary")

    monkeypatch.setattr(archive, "_call_vlm_with_images", forbidden)
    source = source_actions()
    payload = {"actions": source, "meta": {}}
    await archive._clean_v3_plan_intents(payload=payload, goal="goal")
    assert payload["meta"]["plan_intent_batch_model_calls"] == 0
    assert payload["meta"]["plan_intent_cleaner"] == "action_summary"
    assert source[-1]["plan_intent"] == source[-1]["action_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["description_override", "missing_id", "bad_popup"])
async def test_invalid_classification_cannot_partially_change_or_save_case(monkeypatch, settings, fault):
    source = source_actions()
    payload = {"actions": source, "meta": {}}
    before = deepcopy(payload)
    data = classified_output(source)
    if fault == "description_override":
        data["actions"][0]["plan_intent"] = "模型不能覆盖"
    elif fault == "missing_id":
        data["actions"].pop()
    else:
        data["actions"][0]["ephemeral"]["skip_if_absent"] = "true"
    requests = []

    async def call(**kwargs):
        requests.append(kwargs)
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    with pytest.raises(BatchPlanValidationError, match="本次不归档"):
        await archive._clean_v3_plan_intents(payload=payload, goal="goal")
    assert len(requests) == 3
    assert "具体校验错误" in requests[1]["prompt"]
    assert archive._v3_plan_cleaner_rules() not in requests[1]["prompt"]
    assert payload == before


@pytest.mark.asyncio
async def test_summary_fast_path_still_rejects_duplicate_source_ids(monkeypatch, settings):
    settings.trajectory_cache_ephemeral_action_enabled = False
    source = source_actions()
    source[-1]["action_id"] = source[-2]["action_id"]
    with pytest.raises(BatchPlanValidationError, match="原始动作 action_id 重复"):
        await archive.V3PlanIntentCleaner(settings=settings).clean_actions(actions=source)


def test_deterministic_app_steps_do_not_create_a_mixed_case_mode():
    source = source_actions()
    assert archive._can_reuse_case_summaries(source)
    source[0]["app_name"] = ""
    assert not archive._can_reuse_case_summaries(source)
    assert not archive._can_reuse_case_summaries([])
    assert not archive._can_reuse_case_summaries(source[:2])
