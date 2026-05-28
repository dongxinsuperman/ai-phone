"""Phase 1-C：RemoteDriver + DriverRpcWaiter mock 闭环测试。

不接真设备、不起 WS：

- Server 主 event loop 跑在独立线程
- 测试线程模拟 VLMRunner worker 同步调 driver.xxx()
- Fake Agent 用内存 channel 回复 driver_result，覆盖成功 / 各类失败 / 超时 / 取消

跑法：``cd backend && .venv/bin/python -m pytest tests/test_remote_driver_mock.py -q``
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import pytest

from ai_phone.server.runner.remote_driver import RemoteDriver
from ai_phone.server.runner.rpc import (
    DriverRpcWaiter,
    RemoteDriverAgentOfflineError,
    RemoteDriverDeviceError,
    RemoteDriverModelError,
    RemoteDriverNetworkError,
)
from ai_phone.shared.protocol import MSG_DRIVER_COMMAND, MSG_DRIVER_RESULT


# =============================================================================
# 公共 fixture
# =============================================================================
@pytest.fixture
def server_loop():
    """模拟 Server 主 event loop：在独立线程跑 run_forever。"""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=_run, name="server-main-loop", daemon=True)
    t.start()
    ready.wait(timeout=2)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)
    loop.close()


@pytest.fixture
def waiter():
    return DriverRpcWaiter()


class FakeAgent:
    """内存版 Agent：收 driver_command → 调 replier → 在 server_loop 上回复 driver_result。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        waiter: DriverRpcWaiter,
        replier: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
        *,
        send_fails: bool = False,
        reply_delay_s: float = 0.0,
    ) -> None:
        self.loop = loop
        self.waiter = waiter
        self.replier = replier
        self.received: list[Dict[str, Any]] = []
        self.send_fails = send_fails
        self.reply_delay_s = reply_delay_s

    async def send(self, payload: Dict[str, Any]) -> bool:
        if self.send_fails:
            return False
        self.received.append(payload)
        if self.replier is None:
            return True  # 不回复，让 caller 超时

        reply = self.replier(payload)
        if reply is not None:
            # 在 server_loop 上调度回复，模拟真实 Agent 异步回复
            if self.reply_delay_s <= 0:
                self.loop.call_soon(self.waiter.resolve, reply)
            else:
                self.loop.call_later(
                    self.reply_delay_s, self.waiter.resolve, reply
                )
        return True


def _build_driver(
    server_loop: asyncio.AbstractEventLoop,
    waiter: DriverRpcWaiter,
    agent: FakeAgent,
    *,
    run_id: str = "run-mock-001",
    serial: str = "fake-serial",
    agent_id: str = "fake-agent",
) -> RemoteDriver:
    return RemoteDriver(
        serial=serial,
        agent_id=agent_id,
        waiter=waiter,
        send_fn=agent.send,
        loop=server_loop,
        run_id=run_id,
    )


def _count_send_and_wait_tasks(loop: asyncio.AbstractEventLoop) -> int:
    async def _count() -> int:
        # 让 call_soon_threadsafe(cancel) / discard 引发的取消传播完。
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        n = 0
        for task in asyncio.all_tasks():
            if task is asyncio.current_task():
                continue
            coro = task.get_coro()
            qualname = getattr(coro, "__qualname__", "")
            if "RemoteDriver._send_and_wait" in qualname:
                n += 1
        return n

    return asyncio.run_coroutine_threadsafe(_count(), loop).result(timeout=2)


def _ok_reply(cmd: Dict[str, Any], result: Any) -> Dict[str, Any]:
    return {
        "type": MSG_DRIVER_RESULT,
        "message_id": cmd["message_id"],
        "run_id": cmd["run_id"],
        "serial": cmd["serial"],
        "ok": True,
        "result": result,
        "elapsed_ms": 0,
    }


def _err_reply(
    cmd: Dict[str, Any],
    *,
    category: str = "device",
    error_class: str = "AdbError",
    message: str = "device offline",
) -> Dict[str, Any]:
    return {
        "type": MSG_DRIVER_RESULT,
        "message_id": cmd["message_id"],
        "run_id": cmd["run_id"],
        "serial": cmd["serial"],
        "ok": False,
        "error": {
            "category": category,
            "error_class": error_class,
            "message": message,
            "traceback": "",
        },
        "elapsed_ms": 0,
    }


# =============================================================================
# 1. 成功路径：各种返回值类型解码
# =============================================================================
def test_screenshot_jpeg_round_trip(server_loop, waiter):
    """字节型返回：base64 编码 → RemoteDriver 解回 bytes。"""
    raw_bytes = b"\xff\xd8\xff\xe0fake_jpeg_bytes"
    agent = FakeAgent(
        server_loop,
        waiter,
        replier=lambda cmd: _ok_reply(
            cmd,
            {
                "encoding": "base64",
                "mime": "image/jpeg",
                "data": base64.b64encode(raw_bytes).decode(),
            },
        ),
    )
    driver = _build_driver(server_loop, waiter, agent)

    out = driver.screenshot_jpeg(quality=20, max_side=720)

    assert out == raw_bytes
    assert len(agent.received) == 1
    cmd = agent.received[0]
    assert cmd["type"] == MSG_DRIVER_COMMAND
    assert cmd["method"] == "screenshot_jpeg"
    assert cmd["params"] == {"quality": 20, "max_side": 720}
    assert cmd["run_id"] == "run-mock-001"
    assert cmd["serial"] == "fake-serial"
    assert isinstance(cmd["message_id"], str) and len(cmd["message_id"]) >= 8


def test_prepare_for_run_sends_wake_policy(server_loop, waiter):
    agent = FakeAgent(server_loop, waiter, replier=lambda cmd: _ok_reply(cmd, None))
    driver = _build_driver(server_loop, waiter, agent)

    driver.prepare_for_run(wake_policy={"wake_swipe": True})

    assert len(agent.received) == 1
    cmd = agent.received[0]
    assert cmd["type"] == MSG_DRIVER_COMMAND
    assert cmd["method"] == "prepare_for_run"
    assert cmd["params"] == {"wake_policy": {"wake_swipe": True}}


def test_wait_stable_screenshot_jpeg_round_trip(server_loop, waiter):
    raw_bytes = b"\xff\xd8stable_jpeg_bytes"

    def _replier(cmd: Dict[str, Any]) -> Dict[str, Any]:
        return _ok_reply(
            cmd,
            {
                "image": {
                    "encoding": "base64",
                    "mime": "image/jpeg",
                    "data": base64.b64encode(raw_bytes).decode(),
                },
                "stable": True,
                "elapsed_ms": 3210,
                "checks": 3,
                "reused_frame": True,
                "logs": [{"level": 1, "title": "截图已稳定", "content": "ok"}],
            },
        )

    agent = FakeAgent(server_loop, waiter, replier=_replier)
    driver = _build_driver(server_loop, waiter, agent)

    out = driver.wait_stable_screenshot_jpeg(
        quality=90,
        max_side=1568,
        enabled=True,
        total_timeout_s=7.0,
        poll_interval_s=0.6,
        threshold=0.02,
        roi_threshold=0.12,
        black_threshold=0.08,
        strategy="v3_compare",
    )

    assert out.bytes_ == raw_bytes
    assert out.stable is True
    assert out.elapsed_ms == 3210
    assert out.checks == 3
    assert out.reused_frame is True
    assert out.logs == [{"level": 1, "title": "截图已稳定", "content": "ok"}]
    cmd = agent.received[0]
    assert cmd["method"] == "wait_stable_screenshot_jpeg"
    assert cmd["params"] == {
        "quality": 90,
        "max_side": 1568,
        "enabled": True,
        "total_timeout_s": 7.0,
        "poll_interval_s": 0.6,
        "threshold": 0.02,
        "roi_threshold": 0.12,
        "black_threshold": 0.08,
        "strategy": "v3_compare",
    }
    assert cmd["deadline_ms"] == 20_000


def test_window_size_list_to_tuple(server_loop, waiter):
    agent = FakeAgent(
        server_loop, waiter, replier=lambda cmd: _ok_reply(cmd, [1080, 2400])
    )
    driver = _build_driver(server_loop, waiter, agent)

    w, h = driver.window_size()
    assert (w, h) == (1080, 2400)


def test_click_no_return(server_loop, waiter):
    agent = FakeAgent(server_loop, waiter, replier=lambda cmd: _ok_reply(cmd, None))
    driver = _build_driver(server_loop, waiter, agent)

    driver.click(540, 1200)  # 不该抛错
    assert agent.received[0]["params"] == {"x": 540, "y": 1200}


def test_device_info_dict_to_dataclass(server_loop, waiter):
    payload = {
        "serial": "fake-serial",
        "platform": "android",
        "brand": "Pixel",
        "model": "Pixel 7",
        "os_version": "14",
        "screen_width": 1080,
        "screen_height": 2400,
        "status": "online",
        "extra": {"note": "mock"},
    }
    agent = FakeAgent(
        server_loop, waiter, replier=lambda cmd: _ok_reply(cmd, payload)
    )
    driver = _build_driver(server_loop, waiter, agent)

    info = driver.device_info()
    assert info.serial == "fake-serial"
    assert info.platform == "android"
    assert info.brand == "Pixel"
    assert info.model == "Pixel 7"
    assert info.screen_width == 1080
    assert info.screen_height == 2400
    assert info.status == "online"
    assert info.extra == {"note": "mock"}


def test_scroll_center_tuple_serialised_as_list(server_loop, waiter):
    """JSON 不传 tuple，center 会被转 list。"""
    received_params = {}

    def _replier(cmd):
        received_params.update(cmd["params"])
        return _ok_reply(cmd, None)

    agent = FakeAgent(server_loop, waiter, replier=_replier)
    driver = _build_driver(server_loop, waiter, agent)

    driver.scroll("down", center=(540, 1200), amount=3)

    assert received_params["direction"] == "down"
    assert received_params["center"] == [540, 1200]  # 不能是 tuple
    assert received_params["amount"] == 3


def test_scroll_center_none_passes_through(server_loop, waiter):
    received_params = {}

    def _replier(cmd):
        received_params.update(cmd["params"])
        return _ok_reply(cmd, None)

    agent = FakeAgent(server_loop, waiter, replier=_replier)
    driver = _build_driver(server_loop, waiter, agent)

    driver.scroll("up")  # center 默认 None
    assert received_params["center"] is None
    assert received_params["amount"] == 1


def test_list_packages_pass_through(server_loop, waiter):
    pkgs = ["com.example.a", "com.example.b"]
    agent = FakeAgent(server_loop, waiter, replier=lambda cmd: _ok_reply(cmd, pkgs))
    driver = _build_driver(server_loop, waiter, agent)
    assert driver.list_third_party_packages() == pkgs


# =============================================================================
# 2. 错误路径：四类 category 各自抛对应异常
# =============================================================================
def test_device_error_raises_device_exception(server_loop, waiter):
    agent = FakeAgent(
        server_loop,
        waiter,
        replier=lambda cmd: _err_reply(
            cmd,
            category="device",
            error_class="AdbError",
            message="device offline",
        ),
    )
    driver = _build_driver(server_loop, waiter, agent)

    with pytest.raises(RemoteDriverDeviceError) as ei:
        driver.click(100, 200)
    assert ei.value.category == "device"
    assert ei.value.error_class == "AdbError"
    assert "device offline" in ei.value.message
    assert ei.value.message_id  # 透传了 trace_id


def test_model_error_raises_model_exception(server_loop, waiter):
    agent = FakeAgent(
        server_loop,
        waiter,
        replier=lambda cmd: _err_reply(
            cmd, category="model", error_class="VlmRefuse", message="content blocked"
        ),
    )
    driver = _build_driver(server_loop, waiter, agent)

    with pytest.raises(RemoteDriverModelError) as ei:
        driver.click(100, 200)
    assert ei.value.category == "model"
    assert ei.value.error_class == "VlmRefuse"


def test_agent_offline_when_send_fails(server_loop, waiter):
    """send_fn 返回 False（Agent WS 已断开）→ 立刻 RemoteDriverAgentOfflineError。"""
    agent = FakeAgent(server_loop, waiter, send_fails=True)
    driver = _build_driver(server_loop, waiter, agent)

    with pytest.raises(RemoteDriverAgentOfflineError) as ei:
        driver.click(100, 200)
    assert ei.value.category == "agent_offline"
    assert ei.value.error_class == "AgentOffline"


def test_rpc_timeout_when_agent_silent(server_loop, waiter):
    """Agent 收到命令但永远不回复 → RemoteDriverNetworkError(RpcTimeout)。"""
    agent = FakeAgent(server_loop, waiter, replier=None)  # 不回复
    driver = _build_driver(server_loop, waiter, agent)

    # window_size 默认 deadline_ms 较长，加 1s 网络冗余会更慢。我们想测得快点：
    # 直接用一个超短的 method？没有；但 _call 暴露了 deadline_ms 参数。
    # 这里我们调内部接口，把 deadline 拉到 200ms。
    t0 = time.monotonic()
    with pytest.raises(RemoteDriverNetworkError) as ei:
        driver._call("window_size", deadline_ms=200)
    elapsed = time.monotonic() - t0

    assert ei.value.error_class == "RpcTimeout"
    assert ei.value.category == "network"
    assert elapsed < 3.0  # 远低于默认 3s deadline，说明短超时生效
    # 命令仍然发出去了（Agent 收到了，只是没回）
    assert len(agent.received) == 1
    # waiter 应该已经清掉这个 entry
    assert waiter.in_flight == 0
    # 主 loop 上等待 driver_result 的协程也必须被取消，不能只清 pending dict。
    assert _count_send_and_wait_tasks(server_loop) == 0


# =============================================================================
# 3. Waiter 行为
# =============================================================================
def test_resolve_unknown_message_id_returns_false(server_loop, waiter):
    """未知 msg_id（如 Agent 重连补发已超时丢弃的回复）只 warn，不抛错。"""

    async def _try_resolve():
        return waiter.resolve(
            {
                "type": MSG_DRIVER_RESULT,
                "message_id": "ghost-msg-id",
                "run_id": "x",
                "serial": "y",
                "ok": True,
                "result": None,
            }
        )

    fut = asyncio.run_coroutine_threadsafe(_try_resolve(), server_loop)
    assert fut.result(timeout=2) is False


def test_cancel_run_aborts_in_flight_calls(server_loop, waiter):
    """Run 被取消时，所有在飞 RPC 抛 RpcCancelled。"""
    agent = FakeAgent(server_loop, waiter, replier=None, reply_delay_s=10.0)

    driver = _build_driver(server_loop, waiter, agent, run_id="run-cancel-001")

    # 在子线程里发起一次 RPC，等到它进入 in_flight 后再 cancel
    result_holder: Dict[str, Any] = {}

    def _worker():
        try:
            driver._call("window_size", deadline_ms=10_000)
        except BaseException as exc:
            result_holder["exc"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    # 等到 waiter 注册成功（发出 driver_command）
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and waiter.in_flight_for_run("run-cancel-001") == 0:
        time.sleep(0.01)
    assert waiter.in_flight_for_run("run-cancel-001") == 1

    # 在 server_loop 上 cancel
    cancelled = asyncio.run_coroutine_threadsafe(
        _async_cancel(waiter, "run-cancel-001"), server_loop
    ).result(timeout=2)
    assert cancelled == 1

    t.join(timeout=2)
    assert "exc" in result_holder
    exc = result_holder["exc"]
    assert isinstance(exc, RemoteDriverNetworkError)
    assert exc.error_class == "RpcCancelled"
    assert waiter.in_flight_for_run("run-cancel-001") == 0


async def _async_cancel(waiter: DriverRpcWaiter, run_id: str) -> int:
    return waiter.cancel_run(run_id, reason="run cancelled by test")


def test_cancel_all_clears_everything(server_loop, waiter):
    agent = FakeAgent(server_loop, waiter, replier=None, reply_delay_s=10.0)
    driver_a = _build_driver(server_loop, waiter, agent, run_id="run-A")
    driver_b = _build_driver(server_loop, waiter, agent, run_id="run-B")

    holders: Dict[str, BaseException] = {}

    def _go(driver, key):
        try:
            driver._call("window_size", deadline_ms=10_000)
        except BaseException as exc:
            holders[key] = exc

    threads = [
        threading.Thread(target=_go, args=(driver_a, "a"), daemon=True),
        threading.Thread(target=_go, args=(driver_b, "b"), daemon=True),
    ]
    for t in threads:
        t.start()

    # 等两条 RPC 都到位
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and waiter.in_flight < 2:
        time.sleep(0.01)
    assert waiter.in_flight == 2

    # 一句 cancel_all 全清
    n = asyncio.run_coroutine_threadsafe(
        _async_cancel_all(waiter), server_loop
    ).result(timeout=2)
    assert n == 2

    for t in threads:
        t.join(timeout=2)
    assert isinstance(holders["a"], RemoteDriverNetworkError)
    assert isinstance(holders["b"], RemoteDriverNetworkError)
    assert waiter.in_flight == 0


async def _async_cancel_all(waiter: DriverRpcWaiter) -> int:
    return waiter.cancel_all(reason="server shutdown by test")


# =============================================================================
# 4. 命令 schema：driver_command 字段齐全
# =============================================================================
def test_driver_command_schema_complete(server_loop, waiter):
    captured = {}

    def _replier(cmd):
        captured.update(cmd)
        return _ok_reply(cmd, None)

    agent = FakeAgent(server_loop, waiter, replier=_replier)
    driver = _build_driver(server_loop, waiter, agent, run_id="run-schema-001")

    driver.swipe(100, 200, 300, 400, duration_ms=600)

    # protocol 约定的全部字段都得有
    assert captured["type"] == MSG_DRIVER_COMMAND
    assert captured["method"] == "swipe"
    assert captured["run_id"] == "run-schema-001"
    assert captured["serial"] == "fake-serial"
    assert isinstance(captured["message_id"], str) and len(captured["message_id"]) >= 8
    assert captured["params"] == {
        "sx": 100,
        "sy": 200,
        "ex": 300,
        "ey": 400,
        "duration_ms": 600,
    }
    assert isinstance(captured["deadline_ms"], int) and captured["deadline_ms"] > 0


def test_concurrent_commands_share_waiter(server_loop, waiter):
    """同一 driver、不同方法并发跑：每条 RPC 的 message_id 应独立。"""
    agent = FakeAgent(
        server_loop,
        waiter,
        replier=lambda cmd: _ok_reply(
            cmd, [1080, 2400] if cmd["method"] == "window_size" else None
        ),
        reply_delay_s=0.05,
    )
    driver = _build_driver(server_loop, waiter, agent)

    results: Dict[int, Any] = {}

    def _go(i):
        if i % 2 == 0:
            results[i] = driver.window_size()
        else:
            driver.click(i, i)
            results[i] = "clicked"

    threads = [threading.Thread(target=_go, args=(i,), daemon=True) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    # 偶数下标都拿到 (1080, 2400)
    for i in range(0, 8, 2):
        assert results[i] == (1080, 2400)
    for i in range(1, 8, 2):
        assert results[i] == "clicked"

    # 8 条命令，message_id 都不同
    msg_ids = {cmd["message_id"] for cmd in agent.received}
    assert len(msg_ids) == 8
    assert waiter.in_flight == 0
