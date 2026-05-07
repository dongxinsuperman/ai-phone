"""Phase 2-B：``/api/internal/server-brain/driver-probe`` e2e 闭环。

模拟一次完整 PoC 验收路径：

::

    [pytest 主线程]                          [TestClient anyio loop]
        client.websocket_connect(/ws/agent) ─────► Server 注册 Agent
        agent.send_json(hello) ───────────────────► Hub.set_devices

        # 后台线程：发 HTTP probe 请求
        Thread(client.post('/driver-probe')) ────► Server 内部 RemoteDriver
                                                       │
        agent.receive_json() ←──────────────────────── driver_command 出发到 ws
        agent.send_json(driver_result) ──────────────► waiter.resolve
                                                       │
        Thread.join() → resp.json()             ◄──────┘ probe 返回结构

只要这条链路通，PoC 第 1 阶段就算交付：window_size / screenshot_jpeg / click
都跑过一遍 + 错误归因 + Agent 不在线 + 鉴权 + state 诊断。

跑法：``cd backend && .venv/bin/python -m pytest tests/test_server_brain_probe.py -q``
"""
from __future__ import annotations

import base64
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_phone.config import get_settings
from ai_phone.server import db as db_module
from ai_phone.server.api import include_routers
from ai_phone.server.db import Base
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.runner.rpc import DriverRpcWaiter
from ai_phone.server.storage import mount_static
from ai_phone.server.ws import include_ws


# =============================================================================
# fixtures
# =============================================================================
@pytest.fixture
def storage_tmp(monkeypatch):
    td = tempfile.mkdtemp(prefix="ai-phone-probe-test-")
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", td)
    get_settings.cache_clear()
    yield Path(td)
    get_settings.cache_clear()


@pytest.fixture
def probe_app(storage_tmp, tmp_path):
    """一个手工拼装的 FastAPI app，模拟生产 lifespan 的 state 注入。

    跟 ``test_ws_integration.py`` 的 ws_app 等价；这里独立一份以隔离修改面。
    """
    db_file = tmp_path / f"probe-{uuid.uuid4().hex}.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    app = FastAPI()
    app.state.lock_store = DeviceLockStore()
    app.state.hub = Hub()
    app.state.driver_rpc_waiter = DriverRpcWaiter()
    app.state.driver_pool = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="probe-driver-rpc"
    )

    include_routers(app)
    include_ws(app)
    mount_static(app)

    @app.on_event("startup")
    async def _init_db() -> None:
        await db_module.dispose_engine()
        db_module.init_engine(db_url=db_url)
        from ai_phone.server import models  # noqa: F401

        engine = db_module.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @app.on_event("shutdown")
    async def _close_db() -> None:
        await db_module.dispose_engine()

    yield app

    try:
        app.state.driver_rpc_waiter.cancel_all(reason="test teardown")
    except Exception:
        pass
    try:
        app.state.driver_pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


def _bearer() -> Dict[str, str]:
    token = get_settings().agent_token
    return {"Authorization": f"Bearer {token}"}


def _hello_with(agent, *, agent_id: str = "agent-mac-01", serial: str = "S1"):
    agent.send_json(
        {
            "type": "hello",
            "agent_id": agent_id,
            "agent_name": "mac",
            "host_os": "Darwin",
            "devices": [{"serial": serial, "platform": "android", "status": "online"}],
        }
    )


def _wait_device_online(client: TestClient, serial: str, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/devices/{serial}")
        if resp.status_code == 200 and resp.json().get("status") == "online":
            return
        time.sleep(0.02)
    raise AssertionError(f"等待 {serial} 上线超时")


def _start_probe_in_thread(
    client: TestClient, body: Dict[str, Any]
) -> tuple[threading.Thread, Dict[str, Any]]:
    """在 background thread 里发 HTTP probe，返回 (thread, holder)。

    主测试线程腾出来去 ws 上读 driver_command + 回 driver_result，等
    thread.join() 拿到 holder["resp"]。
    """
    holder: Dict[str, Any] = {}

    def _do() -> None:
        try:
            holder["resp"] = client.post(
                "/api/internal/server-brain/driver-probe",
                headers=_bearer(),
                json=body,
            )
        except BaseException as exc:  # noqa: BLE001
            holder["exc"] = exc

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    return t, holder


def _drive_command_loop(
    agent,
    handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    *,
    expected: int = 1,
    timeout: float = 5.0,
) -> list[Dict[str, Any]]:
    """读 expected 条 driver_command，逐条用 handler 生成 driver_result 回去。"""
    received: list[Dict[str, Any]] = []
    deadline = time.time() + timeout
    while len(received) < expected and time.time() < deadline:
        cmd = agent.receive_json()
        assert cmd.get("type") == "driver_command", cmd
        received.append(cmd)
        reply = handler(cmd)
        agent.send_json(reply)
    assert len(received) == expected, f"只读到 {len(received)}/{expected} 条命令"
    return received


# =============================================================================
# 1. 鉴权
# =============================================================================
def test_probe_requires_bearer(probe_app):
    with TestClient(probe_app) as client:
        resp = client.post(
            "/api/internal/server-brain/driver-probe",
            json={"serial": "S1", "method": "window_size"},
        )
        assert resp.status_code == 401


def test_state_endpoint_requires_bearer(probe_app):
    with TestClient(probe_app) as client:
        resp = client.get("/api/internal/server-brain/state")
        assert resp.status_code == 401


# =============================================================================
# 2. 入口校验：serial 不在线 / 缺字段
# =============================================================================
def test_probe_rejects_offline_serial(probe_app):
    with TestClient(probe_app) as client:
        resp = client.post(
            "/api/internal/server-brain/driver-probe",
            headers=_bearer(),
            json={"serial": "ghost", "method": "window_size"},
        )
        assert resp.status_code == 409
        assert "不在线" in resp.json()["detail"]


def test_probe_rejects_missing_fields(probe_app):
    with TestClient(probe_app) as client:
        resp = client.post(
            "/api/internal/server-brain/driver-probe",
            headers=_bearer(),
            json={"serial": "", "method": "window_size"},
        )
        assert resp.status_code == 400


def test_probe_rejects_unknown_method(probe_app):
    """method 不在 DriverMethod 白名单 → 400。"""
    token = get_settings().agent_token
    with TestClient(probe_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            _hello_with(agent)
            _wait_device_online(client, "S1")

            resp = client.post(
                "/api/internal/server-brain/driver-probe",
                headers=_bearer(),
                json={"serial": "S1", "method": "wipe_device"},
            )
            assert resp.status_code == 400
            assert "未知 driver method" in resp.json()["detail"]


# =============================================================================
# 3. 成功路径：window_size / screenshot_jpeg / click（PoC 三件套）
# =============================================================================
def test_probe_window_size_round_trip(probe_app):
    token = get_settings().agent_token
    with TestClient(probe_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            _hello_with(agent)
            _wait_device_online(client, "S1")

            t, holder = _start_probe_in_thread(
                client, {"serial": "S1", "method": "window_size"}
            )

            cmds = _drive_command_loop(
                agent,
                lambda cmd: {
                    "type": "driver_result",
                    "message_id": cmd["message_id"],
                    "run_id": cmd["run_id"],
                    "serial": cmd["serial"],
                    "ok": True,
                    "result": [1080, 2400],
                    "elapsed_ms": 5,
                },
            )
            t.join(timeout=5)

            cmd = cmds[0]
            assert cmd["method"] == "window_size"
            assert cmd["run_id"] == "_probe"
            assert cmd["serial"] == "S1"
            assert isinstance(cmd["message_id"], str) and len(cmd["message_id"]) >= 8
            assert isinstance(cmd["deadline_ms"], int) and cmd["deadline_ms"] > 0

            resp = holder["resp"]
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is True
            assert data["method"] == "window_size"
            assert data["serial"] == "S1"
            assert data["agent_id"] == "agent-mac-01"
            assert data["result"] == [1080, 2400]
            assert isinstance(data["elapsed_ms"], int)


def test_probe_screenshot_base64_round_trip(probe_app):
    raw_bytes = b"\xff\xd8\xff\xe0PoC_jpeg" + b"\x00" * 64
    token = get_settings().agent_token
    with TestClient(probe_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            _hello_with(agent)
            _wait_device_online(client, "S1")

            t, holder = _start_probe_in_thread(
                client,
                {
                    "serial": "S1",
                    "method": "screenshot_jpeg",
                    "params": {"quality": 30, "max_side": 720},
                },
            )

            def _reply(cmd: Dict[str, Any]) -> Dict[str, Any]:
                assert cmd["params"] == {"quality": 30, "max_side": 720}
                return {
                    "type": "driver_result",
                    "message_id": cmd["message_id"],
                    "run_id": cmd["run_id"],
                    "serial": cmd["serial"],
                    "ok": True,
                    "result": {
                        "encoding": "base64",
                        "mime": "image/jpeg",
                        "data": base64.b64encode(raw_bytes).decode(),
                    },
                    "elapsed_ms": 11,
                }

            _drive_command_loop(agent, _reply)
            t.join(timeout=5)

            data = holder["resp"].json()
            assert data["ok"] is True
            result = data["result"]
            # probe 层会重新做 base64 + 加 size 字段
            assert result["encoding"] == "base64"
            assert result["mime"] == "image/jpeg"
            assert result["size"] == len(raw_bytes)
            assert base64.b64decode(result["data"]) == raw_bytes


def test_probe_click_with_no_return(probe_app):
    """click 无返回，但 driver_command 的 params 应包含 x/y。"""
    token = get_settings().agent_token
    with TestClient(probe_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            _hello_with(agent)
            _wait_device_online(client, "S1")

            t, holder = _start_probe_in_thread(
                client,
                {"serial": "S1", "method": "click", "params": {"x": 540, "y": 1200}},
            )
            cmds = _drive_command_loop(
                agent,
                lambda cmd: {
                    "type": "driver_result",
                    "message_id": cmd["message_id"],
                    "run_id": cmd["run_id"],
                    "serial": cmd["serial"],
                    "ok": True,
                    "result": None,
                    "elapsed_ms": 3,
                },
            )
            t.join(timeout=5)

            assert cmds[0]["method"] == "click"
            assert cmds[0]["params"] == {"x": 540, "y": 1200}
            data = holder["resp"].json()
            assert data["ok"] is True
            assert data["result"] is None


# =============================================================================
# 4. 错误路径：Agent 回 device error → probe 200 + ok=false + category=device
# =============================================================================
def test_probe_returns_structured_error(probe_app):
    token = get_settings().agent_token
    with TestClient(probe_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            _hello_with(agent)
            _wait_device_online(client, "S1")

            t, holder = _start_probe_in_thread(
                client,
                {"serial": "S1", "method": "click", "params": {"x": 1, "y": 2}},
            )
            _drive_command_loop(
                agent,
                lambda cmd: {
                    "type": "driver_result",
                    "message_id": cmd["message_id"],
                    "run_id": cmd["run_id"],
                    "serial": cmd["serial"],
                    "ok": False,
                    "error": {
                        "category": "device",
                        "error_class": "AdbError",
                        "message": "device offline",
                        "traceback": "...",
                    },
                    "elapsed_ms": 7,
                },
            )
            t.join(timeout=5)

            resp = holder["resp"]
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["ok"] is False
            err = data["error"]
            assert err["category"] == "device"
            assert err["error_class"] == "AdbError"
            assert "device offline" in err["message"]
            # message_id 应回填，方便手工 grep Agent 日志
            assert isinstance(data["message_id"], str) and data["message_id"]


# =============================================================================
# 5. /state 诊断快照
# =============================================================================
def test_state_endpoint_reports_runtime(probe_app):
    token = get_settings().agent_token
    with TestClient(probe_app) as client:
        # 没 Agent 时基础信息也要可读
        resp = client.get("/api/internal/server-brain/state", headers=_bearer())
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["in_flight_rpc"] == 0
        assert snap["agents"] == []
        assert "driver_pool" in snap
        assert snap["driver_pool"]["max_workers"] >= 1

        # 接一个 Agent 看 agents 数组立刻有
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            _hello_with(agent, agent_id="probe-state-agent", serial="S2")
            _wait_device_online(client, "S2")

            resp = client.get(
                "/api/internal/server-brain/state", headers=_bearer()
            )
            assert resp.status_code == 200
            snap = resp.json()
            agents = snap["agents"]
            assert len(agents) == 1
            assert agents[0]["agent_id"] == "probe-state-agent"
            assert "S2" in agents[0]["serials"]
