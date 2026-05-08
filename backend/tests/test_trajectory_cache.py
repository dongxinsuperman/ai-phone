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
    ReplayActionDispatcher,
    build_cache_assertion_prompt,
    build_cache_key,
    delete_trajectory_cache_for_run,
    get_active_trajectory_cache,
    normalize_run_semantic,
    parse_cache_assertion_response,
    save_trajectory_cache_after_success,
)


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
            ]
        },
        has_prev=False,
    )

    assert "缓存轨迹回放后的最终页面" in prompt
    assert "step 1: open_app app=com.demo" in prompt
    assert "step 2: click point={'x': 1, 'y': 2}" in prompt


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
        },
        {
            "index": 2,
            "source": "run_step",
            "raw": "type(content='hello')",
            "type": "type",
            "content": "hello",
            "source_step": 2,
        },
    ]

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
        },
        {
            "index": 2,
            "source": "run_command",
            "driver_method": "click",
            "message_id": "cmd-b",
            "type": "click",
            "point": {"x": 33, "y": 44},
            "coord_mode": "absolute",
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
    )
    async def stable():
        return SimpleNamespace(bytes_=b"jpeg")

    runner._wait_stable = stable  # type: ignore[method-assign]

    result = await runner.run()

    assert result.success is True
    assert any("intent=点击学习" in content for _level, _title, content in logs)


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
