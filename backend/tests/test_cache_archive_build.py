"""M4 片3b：首跑旁路收集 + V3 成品整理（recorder + archive）单测。

不依赖真机 / 真 VLM：用模拟事件流喂 recorder，再用 archive 整理成 V3 成品。覆盖
**三协议（doubao normalized / claude·gpt absolute）** 与回放执行器字段口径：

- recorder 按 step 聚合 thought / 结构化动作，并据 EVT_RUN_FINISH 判断成功；
- archive 用 next 的 ``_action_from_parsed_raw`` 规范化字段（drag ``start``/``end``、
  scroll ``amount``、open_app ``app_name``、click ``point{x,y}``）——这些正是
  ReplayActionDispatcher 读的字段，避免回放执行对不上；
- 坐标按 coord_space：doubao normalized 按屏幕换算成 abs；claude/gpt absolute 存原值；
- 动作链拆成多条（不合并）；plan_intent 规则生成、可被模型 cleaner 覆盖；空动作不回传。

注：``.env`` 实配了 plan cleaner（classifier=doubao），故默认 autouse fixture 把
``is_configured`` 短路成 False（走规则兜底、不真打外网）；单独的 cleaner 用例显式
打开并 mock ``clean_action``，验证模型清洗覆盖规则的路径。
"""
from __future__ import annotations

import pytest

from ai_phone.agent.runner.events import (
    EVT_ACTION,
    EVT_LOG,
    EVT_RUN_FINISH,
    EVT_SCREENSHOT,
    EVT_THOUGHT,
    make_event,
)
from ai_phone.agent.trajectory_cache import archive as archive_mod
from ai_phone.agent.trajectory_cache.archive import (
    build_v1_archive,
    build_v2_archive,
    build_v3_archive,
)
from ai_phone.agent.trajectory_cache.recorder import TrajectoryRecorder


@pytest.fixture(autouse=True)
def _rule_only_cleaner(monkeypatch):
    """默认禁用模型 cleaner（.env 配了 classifier，单测不真调 VLM），走规则兜底。"""
    monkeypatch.setattr(
        archive_mod.V3PlanIntentCleaner, "is_configured", lambda self: False
    )


def _feed_step(rec, run_id, step, *, thought, action_type, actions, display="x"):
    rec.feed(make_event(EVT_THOUGHT, run_id, step=step, text=thought))
    rec.feed(
        make_event(
            EVT_ACTION, run_id, step=step, text=display, elapsed_ms=100,
            action_type=action_type, actions=actions,
        )
    )


def test_recorder_aggregates_steps_and_success():
    rec = TrajectoryRecorder("r1")
    _feed_step(
        rec, "r1", 1, thought="点击搜索框", action_type="click",
        actions=[{"action": "click", "point": [100, 200]}],
    )
    _feed_step(
        rec, "r1", 2, thought="输入关键词", action_type="type",
        actions=[{"action": "type", "content": "微信"}],
    )
    rec.feed(make_event(EVT_RUN_FINISH, "r1", ok=True, reason="finished: 完成"))
    steps = rec.steps()
    assert len(steps) == 2
    assert steps[0]["thought"] == "点击搜索框"
    assert steps[0]["actions"][0]["point"] == [100, 200]
    assert rec.success is True


def test_recorder_failure_not_success():
    rec = TrajectoryRecorder("r2")
    rec.feed(make_event(EVT_RUN_FINISH, "r2", ok=False, reason="assert_fail: x"))
    assert rec.success is False


@pytest.mark.asyncio
async def test_build_v3_archive_from_first_hand_steps():
    rec = TrajectoryRecorder("r3")
    _feed_step(
        rec, "r3", 1, thought="需要点击搜索按钮", action_type="click",
        actions=[{"action": "click", "point": [50, 60]}],
    )
    _feed_step(
        rec, "r3", 2, thought="输入查询词", action_type="type",
        actions=[{"action": "type", "content": "abc"}],
    )
    _feed_step(
        rec, "r3", 3, thought="任务完成", action_type="finished",
        actions=[{"action": "finished", "content": "done"}],
    )
    rec.feed(make_event(EVT_RUN_FINISH, "r3", ok=True, reason="finished: done"))

    archive = await build_v3_archive(
        goal="打开微信搜索", device_serial="S1", source_run_id="r3",
        source_vlm_backend="doubao_responses", platform="android",
        screen_size=(1000, 1000), steps=rec.steps(),
    )
    assert archive["cache_mode"] == "v3"
    actions = archive["actions"]
    assert len(actions) == 2  # finished 被过滤
    assert actions[0]["type"] == "click"
    assert "搜索" in actions[0]["plan_intent"]  # 规则生成（cleaner 已禁用）
    assert actions[0]["action_id"] == "a1_1"
    assert actions[1]["type"] == "type" and actions[1]["content"] == "abc"
    assert archive["meta"]["plan_intent_cleaner"] == "rule"


@pytest.mark.asyncio
async def test_v3_archive_normalizes_fields_for_replay_dispatcher():
    """drag/scroll/open_app 必须产出回放执行器认的字段名（曾因简化用 to_dict 原始名而崩）。"""
    rec = TrajectoryRecorder("rf")
    _feed_step(
        rec, "rf", 1, thought="拖动滑块", action_type="drag",
        actions=[{"action": "drag", "start_point": [10, 20], "end_point": [30, 40]}],
    )
    _feed_step(
        rec, "rf", 2, thought="向下滑动列表", action_type="scroll",
        actions=[{"action": "scroll", "direction": "down", "scroll_amount": 3}],
    )
    _feed_step(
        rec, "rf", 3, thought="打开微信", action_type="open_app",
        actions=[{"action": "open_app", "name": "微信"}],
    )
    rec.feed(make_event(EVT_RUN_FINISH, "rf", ok=True, reason="finished"))
    actions = (await build_v3_archive(
        goal="g", device_serial="S", source_run_id="rf",
        screen_size=(1000, 1000), steps=rec.steps(),
    ))["actions"]
    assert len(actions) == 3
    drag = actions[0]
    assert drag["type"] == "drag" and "start" in drag and "end" in drag
    assert "start_point" not in drag
    scroll = actions[1]
    assert scroll["type"] == "scroll" and scroll["amount"] == 3
    openapp = actions[2]
    assert openapp["type"] == "open_app" and openapp["app_name"] == "微信"


@pytest.mark.asyncio
async def test_v3_archive_coord_space_doubao_vs_overseas():
    """三协议坐标：doubao normalized 按屏幕换算成 abs；claude/gpt absolute 存原值。"""
    rec_d = TrajectoryRecorder("rd")
    _feed_step(
        rec_d, "rd", 1, thought="点击", action_type="click",
        actions=[{"action": "click", "point": [500, 500]}],
    )
    rec_d.feed(make_event(EVT_RUN_FINISH, "rd", ok=True, reason="finished"))
    a_d = await build_v3_archive(
        goal="g", device_serial="S", source_run_id="rd",
        screen_size=(1000, 1000), steps=rec_d.steps(),
    )
    assert a_d["actions"][0]["point"] == {"x": 500, "y": 500}

    rec_c = TrajectoryRecorder("rc")
    _feed_step(
        rec_c, "rc", 1, thought="点击", action_type="click",
        actions=[{"action": "click", "point": [640, 360], "coord_space": "absolute"}],
    )
    rec_c.feed(make_event(EVT_RUN_FINISH, "rc", ok=True, reason="finished"))
    a_c = await build_v3_archive(
        goal="g", device_serial="S", source_run_id="rc",
        screen_size=(1080, 1920), steps=rec_c.steps(),
    )
    assert a_c["actions"][0]["point"] == {"x": 640, "y": 360}  # absolute 原值


@pytest.mark.asyncio
async def test_v3_archive_splits_action_chain():
    """一步多击（链）拆成多条 action，与 next 一致，不合并。"""
    rec = TrajectoryRecorder("rch")
    _feed_step(
        rec, "rch", 1, thought="连续点击两个位置", action_type="click",
        actions=[
            {"action": "click", "point": [10, 10]},
            {"action": "click", "point": [20, 20]},
        ],
    )
    rec.feed(make_event(EVT_RUN_FINISH, "rch", ok=True, reason="finished"))
    actions = (await build_v3_archive(
        goal="g", device_serial="S", source_run_id="rch",
        screen_size=(1000, 1000), steps=rec.steps(),
    ))["actions"]
    assert len(actions) == 2


@pytest.mark.asyncio
async def test_v3_archive_model_cleaner_overrides_rule(monkeypatch):
    """cleaner 启用时模型 plan_intent 覆盖规则候选，并标 meta=model（还原 next 行为）。"""
    monkeypatch.setattr(
        archive_mod.V3PlanIntentCleaner, "is_configured", lambda self: True
    )

    async def _fake_clean(self, *, action, goal=""):
        return {"plan_intent": "点击顶部搜索框", "confidence": 0.95, "reason": "test"}

    monkeypatch.setattr(archive_mod.V3PlanIntentCleaner, "clean_action", _fake_clean)

    rec = TrajectoryRecorder("rcl")
    _feed_step(
        rec, "rcl", 1, thought="点击搜索", action_type="click",
        actions=[{"action": "click", "point": [10, 10]}],
    )
    rec.feed(make_event(EVT_RUN_FINISH, "rcl", ok=True, reason="finished"))
    out = await build_v3_archive(
        goal="g", device_serial="S", source_run_id="rcl",
        screen_size=(1000, 1000), steps=rec.steps(),
    )
    act = out["actions"][0]
    assert act["plan_intent"] == "点击顶部搜索框"  # 模型覆盖规则
    assert act["plan_intent_meta"]["source"] == "v3_plan_cleaner"
    assert out["meta"]["plan_intent_cleaner"] == "model"


@pytest.mark.asyncio
async def test_v3_archive_source_completion_aligns_next():
    """source_completion 对齐 next 四字段：run_reason/task_done/final_thought/assertion_pass。"""
    rec = TrajectoryRecorder("rsc")
    _feed_step(
        rec, "rsc", 1, thought="点击搜索", action_type="click",
        actions=[{"action": "click", "point": [10, 10]}],
    )
    _feed_step(
        rec, "rsc", 2, thought="已经看到结果页", action_type="finished",
        actions=[{"action": "finished"}],
    )
    rec.feed(make_event(EVT_LOG, "rsc", step=2, level=1, title="任务完成", content="搜索完成"))
    rec.feed(make_event(EVT_LOG, "rsc", step=2, level=1, title="断言系统 · 通过", content="截图支持完成"))
    rec.feed(make_event(EVT_RUN_FINISH, "rsc", ok=True, reason="finished: done"))
    out = await build_v3_archive(
        goal="搜索", device_serial="S", source_run_id="rsc",
        run_reason=rec.finish_reason, completion_logs=rec.completion_logs,
        screen_size=(1000, 1000), steps=rec.steps(),
    )
    sc = out["source_completion"]
    assert sc["run_reason"] == "finished: done"
    assert sc["task_done"] == "搜索完成"
    assert sc["final_thought"] == "已经看到结果页"  # 最后一步 thought（finished 步）
    assert sc["assertion_pass"] == "截图支持完成"


@pytest.mark.asyncio
async def test_build_v3_archive_empty_when_no_actionable_steps():
    rec = TrajectoryRecorder("r4")
    _feed_step(
        rec, "r4", 1, thought="任务完成", action_type="finished",
        actions=[{"action": "finished"}],
    )
    archive = await build_v3_archive(
        goal="g", device_serial="S1", source_run_id="r4", steps=rec.steps(),
    )
    assert archive["actions"] == []


def _jpeg_bytes(color=(30, 60, 90)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 60), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_build_v2_archive_state_landmarks_with_upload():
    """V2 归档：每步 after 截图 → state_landmark（phash + 上传 url）；同 next schema。"""
    uploaded: list = []

    async def _upload(data: bytes) -> str:
        uploaded.append(data)
        return f"/files/lm/{len(uploaded)}.jpg"

    rec = TrajectoryRecorder("v2a")
    _feed_step(
        rec, "v2a", 1, thought="点击搜索框", action_type="click",
        actions=[{"action": "click", "point": [10, 20]}],
    )
    rec.feed(make_event(EVT_SCREENSHOT, "v2a", step=1, phase="after", bytes=_jpeg_bytes((10, 20, 30)), ts=1000))
    _feed_step(
        rec, "v2a", 2, thought="输入关键词", action_type="type",
        actions=[{"action": "type", "content": "x"}],
    )
    rec.feed(make_event(EVT_SCREENSHOT, "v2a", step=2, phase="after", bytes=_jpeg_bytes((200, 210, 220)), ts=2000))
    rec.feed(make_event(EVT_RUN_FINISH, "v2a", ok=True, reason="finished"))

    archive = await build_v2_archive(
        goal="搜索", device_serial="S1", source_run_id="v2a",
        screen_size=(1000, 1000), steps=rec.steps(), upload_image=_upload,
    )
    assert archive["cache_mode"] == "v2"
    tj = archive["trajectory_json"]  # V2 用 trajectory_json（repository._upsert_v1_v2 读它）
    assert tj["schema_version"] == 2
    assert len(tj["actions"]) == 2
    assert tj["actions"][0]["type"] == "click" and tj["actions"][0]["role"] == "business_required"
    lms = tj["state_landmarks"]
    assert len(lms) == 2  # 每个 action 一条 landmark（after 截图可用）
    assert lms[0]["status"] == "available"
    assert lms[0]["image_url"].startswith("/files/lm/")
    assert lms[0]["image_phash"] and lms[0]["image_sha256"]
    assert len(uploaded) == 2  # 两张 landmark 图都上传


@pytest.mark.asyncio
async def test_build_v2_archive_chain_landmark_unavailable():
    """链内动作（同 step 多击且非末）无独立 handoff 截图 → landmark unavailable（回放回落）。"""
    rec = TrajectoryRecorder("v2c")
    rec.feed(make_event(EVT_THOUGHT, "v2c", step=1, text="连续点击两处"))
    rec.feed(
        make_event(
            EVT_ACTION, "v2c", step=1, text="click→click", elapsed_ms=100, action_type="click",
            actions=[{"action": "click", "point": [1, 1]}, {"action": "click", "point": [2, 2]}],
        )
    )
    rec.feed(make_event(EVT_SCREENSHOT, "v2c", step=1, phase="after", bytes=_jpeg_bytes(), ts=900))
    rec.feed(make_event(EVT_RUN_FINISH, "v2c", ok=True, reason="finished"))

    async def _upload(data: bytes) -> str:
        return "/files/lm/x.jpg"

    archive = await build_v2_archive(
        goal="g", device_serial="S", source_run_id="v2c",
        screen_size=(1000, 1000), steps=rec.steps(), upload_image=_upload,
    )
    lms = archive["trajectory_json"]["state_landmarks"]
    assert len(lms) == 2
    # 第一击（链内非末）无 handoff → unavailable；第二击（链末）用 step after → available
    assert lms[0]["status"] == "unavailable"
    assert lms[0]["missing_reason"] == "same_step_action_chain_no_handoff"
    assert lms[1]["status"] == "available"


@pytest.mark.asyncio
async def test_build_v1_archive_no_state_landmarks():
    """V1 归档最朴素：固定动作 + 绝对坐标，trajectory_json.state_landmarks 为空。"""
    rec = TrajectoryRecorder("v1a")
    _feed_step(
        rec, "v1a", 1, thought="点击搜索框", action_type="click",
        actions=[{"action": "click", "point": [500, 500]}],
    )
    _feed_step(
        rec, "v1a", 2, thought="任务完成", action_type="finished",
        actions=[{"action": "finished"}],
    )
    rec.feed(make_event(EVT_RUN_FINISH, "v1a", ok=True, reason="finished"))
    archive = await build_v1_archive(
        goal="g", device_serial="S1", source_run_id="v1a",
        screen_size=(1000, 1000), steps=rec.steps(),
    )
    assert archive["cache_mode"] == "v1"
    tj = archive["trajectory_json"]
    assert tj["schema_version"] == 1
    assert len(tj["actions"]) == 1  # finished 过滤
    assert tj["actions"][0]["type"] == "click"
    assert tj["actions"][0]["point"] == {"x": 500, "y": 500}  # 绝对坐标
    assert tj["state_landmarks"] == []  # V1 无 landmark


@pytest.mark.asyncio
async def test_build_v2_archive_ephemeral_classification(monkeypatch):
    """ephemeral classifier 判 optional → action 标 optional_ephemeral + ephemeral_meta + 上传 popup 图。"""
    import types

    from ai_phone.agent.trajectory_cache.ephemeral import EphemeralClassification

    monkeypatch.setattr(
        archive_mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            trajectory_cache_ephemeral_action_enabled=True, vlm_backend="doubao_responses"
        ),
    )
    monkeypatch.setattr(archive_mod.CacheEphemeralActionClassifier, "is_enabled", lambda self: True)
    monkeypatch.setattr(
        archive_mod.CacheEphemeralActionClassifier, "is_configured", lambda self: True
    )

    async def _fake_classify(self, *, goal, action, before_bytes, after_bytes, prev_action=None, next_action=None):
        return EphemeralClassification(
            role="optional_ephemeral", category="popup", confidence=0.95,
            skip_if_absent=True, reason="偶现弹窗关闭", business_risk="low",
        )

    monkeypatch.setattr(
        archive_mod.CacheEphemeralActionClassifier, "classify_action", _fake_classify
    )

    uploaded: list = []

    async def _upload(data: bytes) -> str:
        uploaded.append(data)
        return f"/files/eph/{len(uploaded)}.jpg"

    rec = TrajectoryRecorder("v2e")
    rec.feed(make_event(EVT_THOUGHT, "v2e", step=1, text="关闭偶现弹窗"))
    rec.feed(
        make_event(
            EVT_ACTION, "v2e", step=1, text="click", elapsed_ms=100, action_type="click",
            actions=[{"action": "click", "point": [5, 5]}],
        )
    )
    rec.feed(make_event(EVT_SCREENSHOT, "v2e", step=1, phase="before", bytes=_jpeg_bytes((1, 2, 3))))
    rec.feed(make_event(EVT_SCREENSHOT, "v2e", step=1, phase="after", bytes=_jpeg_bytes((4, 5, 6)), ts=1000))
    rec.feed(make_event(EVT_RUN_FINISH, "v2e", ok=True, reason="finished"))
    archive = await build_v2_archive(
        goal="g", device_serial="S", source_run_id="v2e",
        screen_size=(1000, 1000), steps=rec.steps(), upload_image=_upload,
    )
    act = archive["trajectory_json"]["actions"][0]
    assert act["role"] == "optional_ephemeral"
    meta = act["ephemeral_meta"]
    assert meta["category"] == "popup"
    assert meta["skip_if_absent"] is True
    assert meta["cached_popup_before_snapshot"].startswith("/files/eph/")
