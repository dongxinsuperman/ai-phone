from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ai_phone.config import Settings
from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Device, Run, RunCommand, RunLog, RunStep, VlmTrajectoryCache
from ai_phone.server.runner.rpc import DriverRpcWaiter
from ai_phone.server.runner.service import ServerRunnerService
from ai_phone.server.trajectory_cache import (
    CacheReplayAssertionVerifier,
    CacheReplayRecoveryVerifier,
    RecoveryDecision,
    ReplayActionDispatcher,
    VERDICT_ASSERT_FAIL,
    VERDICT_CONTINUE,
    VERDICT_REPAIR_ACTION,
    VERDICT_WAIT_MORE,
    build_cache_assertion_prompt,
    build_cache_key,
    build_recovery_prompt,
    delete_trajectory_cache_for_run,
    get_active_trajectory_cache,
    normalize_run_semantic,
    parse_cache_assertion_response,
    parse_recovery_response,
    save_trajectory_cache_after_success,
)
from ai_phone.server.trajectory_cache.recovery import _extract_responses_text


def test_normalize_run_semantic_is_strict_and_deterministic():
    assert normalize_run_semantic("  打开　微信\n\n发送  hello  ") == "打开 微信 发送 hello"


def test_parse_cache_assertion_response():
    assert parse_cache_assertion_response("PASS: ok").verdict == "PASS"
    assert parse_cache_assertion_response("FAIL: bad").reason == "bad"
    assert parse_cache_assertion_response("MAYBE").verdict == "SKIP"


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
            to_dict=lambda: {},
        )

    async def capture_final_frame(self):
        return b"jpeg"


class FakeAlignmentMissReplayRunner(FakeReplayRunner):
    async def run(self):
        await self.log(3, "轨迹缓存状态路标", "轨迹偏航，终止缓存回放")
        return SimpleNamespace(
            success=False,
            error="index=1 type=click error=alignment_miss action_id=a001 elapsed=1000/1000ms",
            final_before_bytes=None,
            to_dict=lambda: {},
        )


class FakeCacheVerifier:
    def __init__(self, *, settings):
        self.settings = settings

    async def verify(self, *, goal, final_bytes, trajectory, prev_before_bytes=None):
        return SimpleNamespace(verdict="PASS", reason="fake assertion", passed=True)


@pytest.mark.asyncio
async def test_save_trajectory_cache_from_run_steps(_test_engine, session):
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
async def test_save_trajectory_cache_from_unlinked_run_commands(_test_engine, session):
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

    run = Run(id="run-replay-ok", device_serial="D1", goal="cached goal", status="running")
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
    assert emitter.finishes == [{"result": "pass", "message": "trajectory_cache_pass: fake assertion"}]
    assert [event.get("title") for event in emitter.events] == [
        "轨迹缓存",
        "fake replay",
        "轨迹缓存断言",
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

    run = Run(id="run-replay-align-miss", device_serial="D1", goal="cached goal", status="running")
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
    assert emitter.finishes == [
        {
            "result": "assert_fail",
            "message": "trajectory_cache_alignment_fail: index=1 type=click error=alignment_miss action_id=a001 elapsed=1000/1000ms",
            "error_class": "TrajectoryCacheAlignmentError",
            "error_category": "model",
        }
    ]


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
