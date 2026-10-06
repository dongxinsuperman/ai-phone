"""Seed changes must not alter the existing native CU mobile contract."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.runner.vlm_loop import VLMRunner
from ai_phone.agent.trajectory_cache.archive import _actions_from_steps
from ai_phone.agent.trajectory_cache.replay import ReplayActionDispatcher, ReplayActionError
from ai_phone.agent.trajectory_cache.v3_replay import _replay_action_from_parsed, build_v3_locator_prompt
from ai_phone.server import db
from ai_phone.server.models import Run, VlmTrajectoryCache, VlmTrajectoryCacheV2, VlmTrajectoryCacheV3
from ai_phone.server.trajectory_cache import service
from ai_phone.server.trajectory_cache import v3_service
from ai_phone.server.trajectory_cache.repository import store_trajectory_cache_archive
from ai_phone.shared.llm.main.claude_cu import _tool_use_to_parsed_action
from ai_phone.shared.llm.main.gpt_cu import _computer_call_to_parsed_action
from ai_phone.shared.seed_gui_actions import parse_actions


class Driver:
    serial = "scope-unit"
    platform = "android"
    scroll = BaseDriver.scroll
    scroll_seed = BaseDriver.scroll_seed

    def __init__(self):
        self.swipe = Mock()

    def window_size(self):
        return 720, 1280


def native_action(backend, direction="down", amount=3, point=(360, 1024)):
    if backend == "claude_cu":
        return _tool_use_to_parsed_action({"name": "computer", "input": {
            "action": "scroll", "coordinate": list(point),
            "scroll_direction": direction, "scroll_amount": amount,
        }})
    vector = {"down": (0, 100*amount), "up": (0, -100*amount), "right": (100*amount, 0), "left": (-100*amount, 0)}[direction]
    return _computer_call_to_parsed_action({"action": {
        "type": "scroll", "x": point[0], "y": point[1],
        "scroll_x": vector[0], "scroll_y": vector[1],
    }})


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
def test_native_cu_parser_and_serialization_stay_at_prechange_contract(backend):
    action = native_action(backend)
    assert action.to_dict() == {
        "action": "scroll", "point": [360, 1024], "direction": "down",
        "scroll_amount": 3, "coord_space": "absolute",
    }
    assert action.scroll_gesture_version == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
@pytest.mark.parametrize("direction,expected", [
    ("down", (360, 1132, 360, 916)),
    ("up", (360, 916, 360, 1132)),
    ("left", (252, 1024, 468, 1024)),
    ("right", (468, 1024, 252, 1024)),
])
async def test_cu_runner_retains_prechange_400ms_center_gesture(backend, direction, expected, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    driver = Driver()
    driver.scroll_seed = Mock(side_effect=AssertionError("CU must not use Seed geometry"))
    runner = VLMRunner(run_id="scope", driver=driver, goal="查看列表", vlm_client=SimpleNamespace())
    runner._settings = runner._settings.model_copy(update={"vlm_backend": backend})
    runner._last_vlm_screenshot_size = (720, 1280)
    result = await runner._execute_action(native_action(backend, direction), step=1, settle_ms=0)
    assert result["unknown"] is False and driver.swipe.call_count == 3
    driver.swipe.assert_called_with(*expected, duration_ms=400)
    driver.scroll_seed.assert_not_called()


@pytest.mark.asyncio
async def test_seed_runner_never_calls_cu_execution(monkeypatch):
    driver = Driver()
    driver.scroll = Mock(side_effect=AssertionError("Seed must not fall back to CU"))
    runner = VLMRunner(run_id="seed", driver=driver, goal="查看列表", vlm_client=SimpleNamespace())
    action = parse_actions('<seed:tool_call><function name="scroll"><parameter name="point" string="true"><point>500 800</point></parameter><parameter name="direction" string="true">down</parameter></function></seed:tool_call>')[0]
    await runner._execute_action(action, step=1, settle_ms=0)
    driver.swipe.assert_called_once_with(360, 1024, 360, 256, duration_ms=1000)
    driver.scroll.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
async def test_cu_first_run_archive_and_relocation_retain_legacy_fields(backend, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    action = native_action(backend)
    cache = _actions_from_steps([{"step": 1, "actions": [action.to_dict()]}], source_vlm_backend=backend, screen_size=(720, 1280))[0]
    assert cache["center"] == {"x": 360, "y": 1024}
    assert "point" not in cache and "scroll_gesture_version" not in cache
    original = deepcopy(cache)
    relocated = _replay_action_from_parsed(native_action(backend, point=(300, 700)), source_action=cache, image_size=(720, 1280), window_size=(720, 1280))
    assert relocated["center"] == {"x": 300, "y": 700}
    prompt = build_v3_locator_prompt(goal="查看列表", trajectory={}, action=cache, coord_space="absolute")
    assert "操作中心" in prompt and "不是手势中心" not in prompt and "滚动模式" not in prompt
    driver = Driver()
    await ReplayActionDispatcher(driver, main_vlm_backend=backend).execute(relocated)
    assert driver.swipe.call_count == 3
    driver.swipe.assert_called_with(300, 808, 300, 592, duration_ms=400)
    assert cache == original


@pytest.mark.asyncio
async def test_cu_must_not_consume_new_seed_cache():
    driver = Driver()
    with pytest.raises(ReplayActionError):
        await ReplayActionDispatcher(driver, main_vlm_backend="gpt_cu").execute({
            "type": "scroll", "scroll_gesture_version": 1,
            "point": {"x": 360, "y": 1024}, "direction": "down",
        })
    driver.swipe.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [VlmTrajectoryCache, VlmTrajectoryCacheV2, VlmTrajectoryCacheV3])
async def test_cu_cache_hit_is_preserved_and_seed_miss_does_not_retire_it(_test_engine, model, monkeypatch):
    factory = db.get_session_factory()
    async with factory() as session:
        session.add(Run(id="cu-source", device_serial="scope", goal="g", token_summary={"vlm_backend": "claude_cu"}))
        row = model(cache_key="cu-cache", device_code="scope", run_semantic_hash="hash", source_run_id="cu-source", status="active")
        actions = [{"type": "scroll", "direction": "down", "center": {"x": 360, "y": 1024}, "amount": 3}]
        if model is VlmTrajectoryCacheV3:
            row.actions_json = actions
            row.source_vlm_backend = "claude_cu"
        else:
            row.trajectory_json = {"actions": actions}
        session.add(row)
        await session.commit()
        monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(vlm_backend="claude_cu"))
        assert (await service.active_cache_payload(session, row))["status"] == "active"
        monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(vlm_backend="doubao_responses"))
        assert await service.active_cache_payload(session, row) is None
        await session.refresh(row)
        assert row.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["v1", "v2", "v3"])
async def test_cu_archive_upload_is_not_rejected_by_seed_retirement(_test_engine, mode):
    action = {"type": "scroll", "direction": "down", "center": {"x": 360, "y": 1024}, "amount": 3}
    archive = {"cache_mode": mode, "device_code": "scope", "run_semantic_text": "cu-goal", "source_run_id": "cu-source", "source_vlm_backend": "gpt_cu"}
    if mode == "v3": archive["actions"] = [action]
    else: archive["trajectory_json"] = {"actions": [action]}
    key = await store_trajectory_cache_archive(db.get_session_factory(), archive=archive)
    assert key


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
async def test_cu_resized_screenshot_coordinate_conversion_is_unchanged(backend, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    driver = Driver()
    runner = VLMRunner(run_id="resized", driver=driver, goal="查看列表", vlm_client=SimpleNamespace())
    runner._settings = runner._settings.model_copy(update={"vlm_backend": backend})
    runner._last_vlm_screenshot_size = (360, 640)
    await runner._execute_action(native_action(backend, point=(180, 512)), step=1, settle_ms=0)
    driver.swipe.assert_called_with(360, 1132, 360, 916, duration_ms=400)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["v1", "v2", "v3"])
async def test_seed_and_cu_cache_storage_lookup_and_failure_are_isolated(_test_engine, mode, monkeypatch):
    factory = db.get_session_factory()
    cu_action = {"type": "scroll", "direction": "down", "amount": 3, "center": {"x": 360, "y": 1024}}
    seed_action = {"type": "scroll", "direction": "down", "amount": 1, "point": {"x": 360, "y": 1024}, "scroll_gesture_version": 1, "scroll_type": "singleAction"}
    base = {"cache_mode": mode, "device_code": "scope", "run_semantic_text": "same-goal", "source_run_id": "cu-source"}
    cu = {**base, "source_vlm_backend": "gpt_cu"}
    seed = {**base, "source_run_id": "seed-source", "source_vlm_backend": "doubao_responses"}
    if mode == "v3":
        cu["actions"] = [cu_action]
        seed["actions"] = [seed_action]
    else:
        cu["trajectory_json"] = {"actions": [cu_action]}
        seed["trajectory_json"] = {"actions": [seed_action]}
    cu_key = await store_trajectory_cache_archive(factory, archive=cu)
    seed_key = await store_trajectory_cache_archive(factory, archive=seed)
    assert cu_key != seed_key
    expected = service.build_cache_key(device_code="scope", run_semantic_text="same-goal", schema_version=int(mode[-1]))[0]
    assert cu_key == expected
    cfg = SimpleNamespace(vlm_backend="gpt_cu")
    monkeypatch.setattr(service, "get_settings", lambda: cfg)
    monkeypatch.setattr(v3_service, "get_settings", lambda: cfg)
    getter = {"v1": service.get_active_trajectory_cache_v1, "v2": service.get_active_trajectory_cache_v2, "v3": v3_service.get_active_trajectory_cache_v3}[mode]
    kwargs = {"device_code": "scope", "run_semantic_text": "same-goal"}
    assert (await getter(factory, **kwargs))["cache_key"] == cu_key
    cfg.vlm_backend = "doubao_responses"
    hit = await getter(factory, **kwargs)
    assert hit["cache_key"] == seed_key
    async with factory() as session:
        session.add(Run(id="seed-fail", device_serial="scope", goal="same-goal", token_summary={"vlm_backend": "doubao_responses"}))
        await session.commit()
    if mode == "v3":
        await v3_service.record_v3_cache_binding(factory, run_id="seed-fail", hit=hit)
    delete = {"v1": service.delete_trajectory_cache_v1_for_run, "v2": service.delete_trajectory_cache_v2_for_run, "v3": v3_service.delete_trajectory_cache_v3_for_run}[mode]
    assert await delete(factory, "seed-fail") == 1
    cfg.vlm_backend = "gpt_cu"
    preserved = await getter(factory, **kwargs)
    assert preserved["cache_key"] == cu_key
    stored = preserved.get("actions") or preserved["trajectory_json"]["actions"]
    assert stored == [cu_action]
