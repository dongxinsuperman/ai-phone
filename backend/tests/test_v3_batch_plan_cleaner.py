"""Batch generation preserves single-action semantics and repairs invalid structures atomically."""
import json
import asyncio
from copy import deepcopy

import pytest

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache import archive
from ai_phone.agent.trajectory_cache.batch_plan_cleaner import (
    BatchPlanValidationError, batch_action_ids, build_batch_plan_prompt, validate_batch_plan_output,
)


def actions(count=3):
    return [{"action_id": f"a{i}", "index": i, "type": "click",
             "point": {"x": i, "y": i * 2}, "thought": f"点击目标{i}",
             "plan_intent": f"点击目标{i}", "raw": f"click({i})", "role": "business_required"}
            for i in range(1, count + 1)]


def response(source):
    return {"actions": [{"action_id": a["action_id"], "plan_intent": a["thought"],
                         "confidence": 0.9, "reason": "当前动作目标"} for a in source]}


def cleaner():
    return archive.V3PlanIntentCleaner(settings=Settings(
        _env_file=None, assistant_api_key="unit-key", assistant_api_url="https://example.test/chat/completions",
        assistant_model="unit-model", aux_reasoning_effort="high",
        trajectory_cache_ephemeral_classifier_api_key="",
        trajectory_cache_ephemeral_classifier_api_url="",
        trajectory_cache_ephemeral_classifier_model="",
        trajectory_cache_ephemeral_classifier_timeout_sec=300,
        trajectory_cache_ephemeral_action_enabled=False,
    ))


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(archive, "get_settings", lambda: cleaner().settings)


def test_batch_keeps_original_rules_and_independent_action_facts():
    source = actions()
    rules = archive._v3_plan_cleaner_rules()
    inputs = [{"action_id": a["action_id"], **archive._v3_action_brief(a)} for a in source]
    prompt = build_batch_plan_prompt(goal="目标", action_inputs=inputs, rules=rules)
    assert rules in prompt
    for a in source:
        assert archive._v3_action_brief(a) == {k: v for k, v in inputs[a["index"] - 1].items() if k != "action_id"}
    assert "不合并、不删条、不新增动作" in prompt
    assert "不写下一步" in prompt
    assert "此限制只针对结果字段" in prompt
    assert "保留每条动作的完整描述粒度" in prompt
    assert "输入框名称、位置/区域提示、输入内容或等待秒数" in prompt
    assert "不等于唯一控件文案" in prompt
    assert "不能据此覆盖源语义" in prompt
    assert "拖拽" in rules and "截图" in rules and "按键" in rules


def test_batch_validation_reorders_one_hundred_rows_by_original_ids():
    source = actions(100)
    data = response(source)
    data["actions"].reverse()
    rows = validate_batch_plan_output(json.dumps(data), batch_action_ids(source))
    assert [r["action_id"] for r in rows] == batch_action_ids(source)
    assert len(rows) == 100


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "extra_parameter", "wrong_type",
                                    "missing_field", "bad_confidence", "long_intent", "broken_json"])
def test_invalid_batch_output_has_specific_validation_errors(fault):
    source = actions()
    data = response(source)
    rows = data["actions"]
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif fault == "unknown":
        rows[0]["action_id"] = "unknown-id"
    elif fault == "extra_parameter":
        rows[0]["point"] = {"x": 123, "y": 456}
    elif fault == "wrong_type":
        rows[0]["plan_intent"] = 123
    elif fault == "missing_field":
        rows[0].pop("reason")
    elif fault == "bad_confidence":
        rows[0]["confidence"] = float("nan")
    elif fault == "long_intent":
        rows[0]["plan_intent"] = "点击" * 61
    text = "{broken" if fault == "broken_json" else json.dumps(data)
    with pytest.raises(BatchPlanValidationError) as exc:
        validate_batch_plan_output(text, batch_action_ids(source))
    assert exc.value.errors


def test_empty_description_is_allowed_as_in_original_rules():
    source = actions(1)
    data = response(source)
    data["actions"][0]["plan_intent"] = ""
    assert validate_batch_plan_output(json.dumps(data), batch_action_ids(source))[0]["plan_intent"] == ""


def test_duplicate_json_keys_are_not_silently_overwritten():
    text = '{"actions":[],"actions":[]}'
    with pytest.raises(BatchPlanValidationError, match="字段重复"):
        validate_batch_plan_output(text, [])


@pytest.mark.asyncio
async def test_one_hundred_actions_use_one_model_call(monkeypatch):
    source = actions(100)
    before = deepcopy(source)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return json.dumps(response(source), ensure_ascii=False)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    result = await cleaner().clean_actions(actions=source, goal="目标")
    assert result["model_calls"] == 1
    assert len(calls) == 1
    assert calls[0]["aux_reasoning_effort"] == "high"
    assert source == before


@pytest.mark.asyncio
async def test_missing_duplicate_and_parameter_errors_are_fed_back_for_correction(monkeypatch):
    source = actions()
    invalid = response(source)
    invalid["actions"][-1] = deepcopy(invalid["actions"][0])
    invalid["actions"][0]["point"] = {"x": 999, "y": 999}
    previous = json.dumps(invalid, ensure_ascii=False)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return previous if len(calls) == 1 else json.dumps(response(source), ensure_ascii=False)

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    result = await cleaner().clean_actions(actions=source, goal="目标")
    assert result["model_calls"] == 2
    assert result["repair_rounds"] == 1
    prompt = calls[1]["prompt"]
    assert previous in prompt
    assert "重复 action_id" in prompt and "缺少 action_id" in prompt
    assert "不允许额外字段" in prompt
    assert "返回修正后的完整结果" in prompt
    assert archive._v3_plan_cleaner_rules() in prompt


@pytest.mark.asyncio
async def test_exhausted_repairs_never_apply_partial_results(monkeypatch):
    source = actions()
    payload = {"actions": source, "meta": {"plan_intent_cleaner": "rule"}}
    before = deepcopy(payload)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return json.dumps({"actions": response(source)["actions"][:-1]})

    monkeypatch.setattr(archive.V3PlanIntentCleaner, "_config", lambda self: (
        "openai_compatible", "https://example.test/chat/completions", "unit-key", "unit-model", 300.0,
    ))
    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    with pytest.raises(BatchPlanValidationError, match="本次不归档"):
        await archive._clean_v3_plan_intents(payload=payload, goal="目标")
    assert len(calls) == 3
    assert payload == before


@pytest.mark.asyncio
async def test_structural_validation_and_merge_preserve_all_execution_parameters(monkeypatch):
    source = actions()
    source[0].update(type="type", content="13000000000", thought="输入手机号", plan_intent="输入手机号")
    source[1].update(type="open_app", app_name="com.demo", thought="打开应用", plan_intent="打开应用")
    before = deepcopy(source)

    async def call(**kwargs):
        data = response(source)
        data["actions"].reverse()
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(archive.V3PlanIntentCleaner, "_config", lambda self: (
        "openai_compatible", "https://example.test/chat/completions", "unit-key", "unit-model", 300.0,
    ))
    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    payload = {"actions": source, "meta": {}}
    await archive._clean_v3_plan_intents(payload=payload, goal="目标")
    for old, new in zip(before, payload["actions"]):
        assert {k: v for k, v in new.items() if k not in {"plan_intent", "plan_intent_meta"}} == {
            k: v for k, v in old.items() if k not in {"plan_intent", "plan_intent_meta"}
        }
    assert payload["meta"]["plan_intent_batch_model_calls"] == 1


@pytest.mark.asyncio
async def test_empty_batch_and_bad_source_ids_do_not_call_model(monkeypatch):
    async def call(**kwargs):
        pytest.fail("must not call model")

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    assert (await cleaner().clean_actions(actions=[]))["model_calls"] == 0
    source = actions()
    source[-1]["action_id"] = source[0]["action_id"]
    with pytest.raises(BatchPlanValidationError, match="原始动作 action_id 重复"):
        await cleaner().clean_actions(actions=source)


@pytest.mark.asyncio
async def test_original_empty_and_conflict_acceptance_rules_are_preserved(monkeypatch):
    source = actions()
    source[0].update(thought="点击 Copy", plan_intent="点击 Copy")
    before = deepcopy(source)
    data = response(source)
    data["actions"][0]["plan_intent"] = "点击 Futures"
    data["actions"][1]["plan_intent"] = ""

    async def call(**kwargs):
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(archive.V3PlanIntentCleaner, "_config", lambda self: (
        "openai_compatible", "https://example.test/chat/completions", "unit-key", "unit-model", 300.0,
    ))
    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    payload = {"actions": source, "meta": {}}
    await archive._clean_v3_plan_intents(payload=payload, goal="目标")
    assert payload["actions"][0]["plan_intent"] == before[0]["plan_intent"]
    assert payload["actions"][0]["plan_intent_meta"]["source"] == "v3_plan_cleaner_rejected"
    assert payload["actions"][1] == before[1]
    assert payload["meta"]["plan_intent_cleaned_actions"] == 1
    assert payload["meta"]["plan_intent_cleaner_rejected_actions"] == 1


@pytest.mark.asyncio
async def test_request_failures_are_bounded_and_retried_without_changing_source(monkeypatch):
    source = actions()
    before = deepcopy(source)
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("temporary service error")

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    with pytest.raises(BatchPlanValidationError, match="本次不归档"):
        await cleaner().clean_actions(actions=source)
    assert len(calls) == 3
    assert source == before


@pytest.mark.asyncio
async def test_cancelled_generation_does_not_retry(monkeypatch):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(archive, "_call_vlm_with_images", call)
    with pytest.raises(asyncio.CancelledError):
        await cleaner().clean_actions(actions=actions())
    assert len(calls) == 1
