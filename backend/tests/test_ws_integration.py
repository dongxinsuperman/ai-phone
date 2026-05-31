"""/ws/agent + /ws/browser/{serial} + /api/files/upload 端到端测试。

用 FastAPI 的同步 TestClient（starlette 提供），直接在同一个 app 实例上：
1. 起 agent WS，发 hello 上线一台设备
2. 起 browser WS 订阅该设备
3. agent 再发 log，应出现在 browser 的接收队列里
4. 断开 agent，浏览器侧也收到 device_update offline（由 Server 主动广播）

文件上传：走 /api/files/upload，验证返回 url，再 GET /files/<rel> 拉回字节。
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_phone.config import get_settings
from ai_phone.server import db as db_module
from ai_phone.server.api import include_routers
from ai_phone.server.db import Base
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.storage import mount_static
from ai_phone.server.ws import include_ws


@pytest.fixture
def storage_tmp(monkeypatch):
    """把 storage_dir 指向独立临时目录。"""
    td = tempfile.mkdtemp(prefix="ai-phone-test-")
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", td)
    get_settings.cache_clear()  # pydantic-settings lru_cache
    yield Path(td)
    get_settings.cache_clear()


@pytest.fixture
def ws_app(storage_tmp, tmp_path):
    """为 WS / 文件上传专门起一个 app（同步 TestClient 要求）。

    SQLite 走文件库（而不是内存库），避免"连接绑定在初始化那个 loop"的坑。
    """
    db_file = tmp_path / f"test-{uuid.uuid4().hex}.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    app = FastAPI()
    app.state.lock_store = DeviceLockStore()
    app.state.hub = Hub()
    include_routers(app)
    include_ws(app)
    mount_static(app)

    # 在 TestClient 自己的 loop 里建表；用 startup 事件确保时序
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

    # 测试拆完后释放线程池 + 取消在飞 RPC，避免线程泄露
    try:
        app.state.driver_rpc_waiter.cancel_all(reason="test teardown")
    except Exception:
        pass
    try:
        app.state.driver_pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


# --------------------------------------------------------------------- upload


def test_file_upload_and_fetch(ws_app, storage_tmp):
    with TestClient(ws_app) as client:
        resp = client.post(
            "/api/files/upload",
            files={"file": ("shot.jpg", b"\xff\xd8\xff\xe0hello", "image/jpeg")},
            data={"content_type": "image/jpeg"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["size"] == len(b"\xff\xd8\xff\xe0hello")
        assert body["url"].startswith("/files/")

        # 直接 GET 该 URL 应拿回字节
        got = client.get(body["url"])
        assert got.status_code == 200
        assert got.content == b"\xff\xd8\xff\xe0hello"


def test_file_upload_rejects_empty(ws_app):
    with TestClient(ws_app) as client:
        resp = client.post(
            "/api/files/upload",
            files={"file": ("x.jpg", b"", "image/jpeg")},
        )
        assert resp.status_code == 400


# --------------------------------------------------------------------- WS


def test_agent_ws_bad_token_rejected(ws_app):
    with TestClient(ws_app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/agent?token=WRONG") as _:
                pass


def _wait_device_online(client, serial: str, timeout: float = 2.0):
    """等 WS 异步 upsert 完成。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/devices/{serial}")
        if resp.status_code == 200 and resp.json().get("status") == "online":
            return resp.json()
        time.sleep(0.05)
    raise AssertionError(f"device {serial} did not come online within {timeout}s")


def _recv_until(agent, msg_type: str, max_msgs: int = 5) -> dict:
    """收到指定 type 的消息为止，跳过中间的其它消息（如连接时下发的 agent_config）。"""
    for _ in range(max_msgs):
        msg = agent.receive_json()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"未在前 {max_msgs} 条消息内收到 type={msg_type}")


def test_agent_ws_hello_registers_device(ws_app):
    token = get_settings().agent_token
    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            agent.send_json(
                {
                    "type": "hello",
                    "agent_id": "agent-x",
                    "agent_name": "mac-local",
                    "host_os": "Darwin",
                    "devices": [
                        {
                            "serial": "S1",
                            "platform": "android",
                            "brand": "Test",
                            "model": "M1",
                            "os_version": "14",
                            "screen_width": 1080,
                            "screen_height": 2400,
                            "status": "online",
                        }
                    ],
                }
            )
            _wait_device_online(client, "S1")


def test_browser_receives_agent_log(ws_app):
    token = get_settings().agent_token
    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            agent.send_json(
                {
                    "type": "hello",
                    "agent_id": "agent-x",
                    "agent_name": "mac",
                    "host_os": "Darwin",
                    "devices": [{"serial": "S1", "platform": "android", "status": "online"}],
                }
            )
            _wait_device_online(client, "S1")
            with client.websocket_connect("/ws/browser/S1") as browser:
                agent.send_json(
                    {
                        "type": "log",
                        "serial": "S1",
                        "level": 1,
                        "title": "hello",
                        "content": "world",
                    }
                )
                got = browser.receive_json()
                assert got["type"] == "log"
                assert got["title"] == "hello"


def test_start_run_dispatched_to_agent(ws_app):
    token = get_settings().agent_token
    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            agent.send_json(
                {
                    "type": "hello",
                    "agent_id": "agent-x",
                    "agent_name": "mac",
                    "host_os": "Darwin",
                    "devices": [{"serial": "S1", "platform": "android", "status": "online"}],
                }
            )
            _wait_device_online(client, "S1")
            resp = client.post(
                "/api/runs",
                json={
                    "device_serial": "S1",
                    "goal": "打开设置",
                    "functionMapContext": "设置 App 首页有蓝牙入口",
                },
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["dispatched"] is True
            assert body["agent_id"] == "agent-x"
            assert body["function_map_context_chars"] == len("设置 App 首页有蓝牙入口")

            # Agent 端应收到 start_run 消息（跳过连接时 Server 下发的 agent_config）
            msg = _recv_until(agent, "start_run")
            assert msg["type"] == "start_run"
            assert msg["device_serial"] == "S1"
            assert msg["goal"] == "打开设置"
            assert msg["function_map_context"] == "设置 App 首页有蓝牙入口"
            assert msg["functionMapContext"] == "设置 App 首页有蓝牙入口"


def test_stop_run_forwarded(ws_app):
    token = get_settings().agent_token
    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            agent.send_json(
                {
                    "type": "hello",
                    "agent_id": "agent-x",
                    "agent_name": "mac",
                    "host_os": "Darwin",
                    "devices": [{"serial": "S1", "platform": "android", "status": "online"}],
                }
            )
            _wait_device_online(client, "S1")
            resp = client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
            run_id = resp.json()["id"]
            _recv_until(agent, "start_run")  # 吞掉 start_run（跳过 agent_config）

            stop = client.post(f"/api/runs/{run_id}/stop")
            assert stop.status_code == 200

            msg = _recv_until(agent, "stop_run")
            assert msg == {"type": "stop_run", "run_id": run_id}


def test_device_update_propagates(ws_app):
    """Agent 上行 device_update 能更新 Device.status，并广播给浏览器订阅方。"""
    token = get_settings().agent_token
    with TestClient(ws_app) as client:
        with client.websocket_connect(f"/ws/agent?token={token}") as agent:
            agent.send_json(
                {
                    "type": "hello",
                    "agent_id": "agent-x",
                    "agent_name": "mac",
                    "host_os": "Darwin",
                    "devices": [{"serial": "S1", "platform": "android", "status": "online"}],
                }
            )
            _wait_device_online(client, "S1")

            with client.websocket_connect("/ws/browser/S1") as browser:
                agent.send_json(
                    {"type": "device_update", "serial": "S1", "status": "offline"}
                )
                got = browser.receive_json()
                assert got["type"] == "device_update" and got["status"] == "offline"

            # 同时 REST 也能看到状态变化
            import time

            deadline = time.time() + 2.0
            while time.time() < deadline:
                dev = client.get("/api/devices/S1").json()
                if dev["status"] == "offline":
                    break
                time.sleep(0.05)
            assert dev["status"] == "offline"
