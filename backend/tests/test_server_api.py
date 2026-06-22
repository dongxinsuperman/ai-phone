"""Server API 集成测试：devices / cases / runs / 占用锁。

走 FastAPI + httpx.AsyncClient + aiosqlite 内存库，不碰真 PG。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_phone.server.db import get_session_factory
from ai_phone.server.models import Device, DeviceAlias, Run, RunCommand, RunLog
from ai_phone.server.hub import Hub
from ai_phone.server.ws.agent_ws import _upsert_devices


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def _seed_device(session, *, serial="S1", status="online") -> Device:
    dev = Device(
        serial=serial,
        agent_id="agent-local",
        platform="android",
        brand="samsung",
        model="SM-G991N",
        os_version="Android 14",
        screen_width=1080,
        screen_height=2400,
        status=status,
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(dev)
    await session.commit()
    return dev


# --------------------------------------------------------------------- devices


@pytest.mark.asyncio
async def test_list_devices_empty(client):
    resp = await client.get("/api/devices")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_and_get_device(client, session):
    await _seed_device(session)
    lst = await client.get("/api/devices")
    assert lst.status_code == 200
    items = lst.json()
    assert len(items) == 1
    assert items[0]["serial"] == "S1"
    assert items[0]["effective_status"] == "online"
    assert items[0]["lock"] is None

    one = await client.get("/api/devices/S1")
    assert one.status_code == 200
    assert one.json()["brand"] == "samsung"

    miss = await client.get("/api/devices/UNKNOWN")
    assert miss.status_code == 404


@pytest.mark.asyncio
async def test_public_device_statuses_include_all_states(client, app, session):
    await _seed_device(session, serial="S_READY")
    await _seed_device(session, serial="S_BUSY")
    await _seed_device(session, serial="S_OFFLINE", status="offline")
    session.add_all(
        [
            DeviceAlias(serial="S_READY", alias="Android-A"),
            DeviceAlias(serial="S_BUSY", alias="Android-B"),
            DeviceAlias(serial="S_OFFLINE", alias="Android-C"),
        ]
    )
    await session.commit()

    hub = Hub()
    app.state.hub = hub
    await hub.register_agent("agent-local", "mac-a", "Darwin", _FakeWs())
    await hub.set_devices("agent-local", {"S_READY", "S_BUSY"})
    hub.set_device_readiness("S_READY", {"ready": True, "hint": "ok"})
    hub.set_device_readiness("S_BUSY", {"ready": True, "hint": "ok"})
    await app.state.lock_store.acquire(
        "S_BUSY",
        holder="sched-item",
        holder_type="auto",
        ttl_seconds=600,
    )

    resp = await client.get("/api/devices/statuses")
    assert resp.status_code == 200
    by_serial = {row["serial"]: row for row in resp.json()}

    assert set(by_serial) == {"S_READY", "S_BUSY", "S_OFFLINE"}
    assert by_serial["S_READY"]["alias"] == "Android-A"
    assert by_serial["S_READY"]["effective_status"] == "online"
    assert by_serial["S_READY"]["platform"] == "android"
    assert by_serial["S_READY"]["brand"] == "samsung"
    assert by_serial["S_READY"]["model"] == "SM-G991N"
    assert by_serial["S_READY"]["os_version"] == "Android 14"
    assert by_serial["S_READY"]["screen_width"] == 1080
    assert by_serial["S_READY"]["screen_height"] == 2400
    assert by_serial["S_READY"]["last_seen_at"]
    assert by_serial["S_READY"]["lock"] is None

    assert by_serial["S_BUSY"]["effective_status"] == "busy"
    assert by_serial["S_BUSY"]["lock"]["holder_type"] == "auto"

    assert by_serial["S_OFFLINE"]["effective_status"] == "offline"

    # 对外新入口保持与当前全量设备接口等价，方便现有消费方无改造切换。
    internal = await client.get("/api/devices")
    assert internal.status_code == 200
    assert by_serial == {row["serial"]: row for row in internal.json()}

    available = await client.get("/api/devices/available")
    assert available.status_code == 200
    assert [row["serial"] for row in available.json()] == ["S_READY"]


@pytest.mark.asyncio
async def test_list_agents(client, app):
    hub = Hub()
    app.state.hub = hub
    await hub.register_agent("agent-1", "mac-a", "Darwin", object())
    await hub.set_devices("agent-1", {"S1", "S2"})
    await hub.bind_run("run-1", "agent-1")

    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["agent_id"] == "agent-1"
    assert body[0]["agent_name"] == "mac-a"
    assert body[0]["device_count"] == 2
    assert body[0]["running_count"] == 1
    assert body[0]["serials"] == ["S1", "S2"]
    assert body[0]["connected_at"]
    assert body[0]["last_seen_at"]


@pytest.mark.asyncio
async def test_upsert_devices_preserves_metadata_when_keepalive_snapshot_is_sparse(
    client, session
):
    assert client is not None
    session.add(
        Device(
            serial="S1",
            agent_id="agent-local",
            platform="android",
            brand="Redmi",
            model="23113RKC6C",
            os_version="15",
            screen_width=1080,
            screen_height=2400,
            status="online",
            last_seen_at=datetime(2026, 5, 26, 8, 6, 45, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    await _upsert_devices(
        "agent-local",
        [
            {
                "serial": "S1",
                "platform": "android",
                "brand": "",
                "model": "",
                "os_version": "",
                "screen_width": 0,
                "screen_height": 0,
                "status": "online",
            }
        ],
        Hub(),
    )

    async with get_session_factory()() as verify:
        dev = await verify.get(Device, "S1")
        assert dev is not None
        assert dev.brand == "Redmi"
        assert dev.model == "23113RKC6C"
        assert dev.os_version == "15"
        assert dev.screen_width == 1080
        assert dev.screen_height == 2400
        assert dev.last_seen_at is not None
        last_seen = dev.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        assert last_seen > datetime(2026, 5, 26, 8, 6, 45, tzinfo=timezone.utc)


# --------------------------------------------------------------------- locking


@pytest.mark.asyncio
async def test_lock_full_flow(client, session):
    await _seed_device(session)

    acq = await client.post(
        "/api/devices/S1/lock",
        json={"holder": "browser-xx", "holder_type": "manual"},
    )
    assert acq.status_code == 201, acq.text
    token = acq.json()["token"]

    # 占用后列表应显示 busy
    lst = (await client.get("/api/devices")).json()
    assert lst[0]["effective_status"] == "busy"
    assert lst[0]["lock"]["holder"] == "browser-xx"

    # 别人再抢应 409
    conflict = await client.post(
        "/api/devices/S1/lock", json={"holder": "other", "holder_type": "manual"}
    )
    assert conflict.status_code == 409

    # 同一持有者再 POST 视为续期，token 不变
    again = await client.post(
        "/api/devices/S1/lock", json={"holder": "browser-xx", "holder_type": "manual"}
    )
    assert again.status_code == 201
    assert again.json()["token"] == token

    # 心跳成功
    hb = await client.post("/api/devices/S1/heartbeat", json={"token": token})
    assert hb.status_code == 200

    # 心跳错误 token → 403
    bad = await client.post("/api/devices/S1/heartbeat", json={"token": "wrong"})
    assert bad.status_code == 403

    # 释放成功
    rel = await client.request(
        "DELETE", "/api/devices/S1/lock", json={"token": token}
    )
    assert rel.status_code == 200 and rel.json() == {"released": True}

    # 释放后再心跳 → 404
    gone = await client.post("/api/devices/S1/heartbeat", json={"token": token})
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_lock_rejects_offline_device(client, session):
    await _seed_device(session, status="offline")
    resp = await client.post(
        "/api/devices/S1/lock", json={"holder": "u", "holder_type": "manual"}
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------- cases


@pytest.mark.asyncio
async def test_case_crud(client):
    # 创建
    c1 = await client.post(
        "/api/cases", json={"title": "登录流", "goal": "打开登录页并输入账号"}
    )
    assert c1.status_code == 201, c1.text
    case_id = c1.json()["id"]

    # 列表
    lst = await client.get("/api/cases")
    assert len(lst.json()) == 1

    # 更新
    upd = await client.put(
        f"/api/cases/{case_id}",
        json={"title": "登录-改", "goal": "点击头像再退出", "prerequisite_case_id": None},
    )
    assert upd.status_code == 200
    assert upd.json()["title"] == "登录-改"

    # 前置引用自己应 400
    self_ref = await client.put(
        f"/api/cases/{case_id}",
        json={
            "title": "x",
            "goal": "y",
            "prerequisite_case_id": case_id,
        },
    )
    assert self_ref.status_code == 400

    # 删除
    rm = await client.delete(f"/api/cases/{case_id}")
    assert rm.status_code == 200
    assert (await client.get(f"/api/cases/{case_id}")).status_code == 404


@pytest.mark.asyncio
async def test_case_effective_goal_concatenation(client):
    pre = await client.post(
        "/api/cases", json={"title": "登录", "goal": "先完成登录步骤"}
    )
    pre_id = pre.json()["id"]
    main = await client.post(
        "/api/cases",
        json={"title": "下单", "goal": "加购并结算", "prerequisite_case_id": pre_id},
    )
    main_id = main.json()["id"]

    eff = await client.get(f"/api/cases/{main_id}/effective-goal")
    data = eff.json()
    assert data["goal"].startswith("先完成登录步骤")
    assert "加购并结算" in data["goal"]


# --------------------------------------------------------------------- runs


@pytest.mark.asyncio
async def test_create_run_with_goal(client, session):
    await _seed_device(session)
    resp = await client.post(
        "/api/runs", json={"device_serial": "S1", "goal": "打开设置"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["goal"] == "打开设置"
    assert "job_lock_token" in body
    run_id = body["id"]

    # 设备被自动占用：holder 就是 run.id，meta 里标记是代抢
    dev = (await client.get("/api/devices/S1")).json()
    assert dev["lock"]["holder_type"] == "job"
    assert dev["lock"]["holder"] == run_id
    assert dev["lock"]["meta"]["auto_acquired"] is True

    # 第二个 run 同设备应 409（自动锁未释放）
    dup = await client.post(
        "/api/runs", json={"device_serial": "S1", "goal": "再来"}
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_create_run_rejects_function_map_context_too_long(client, session):
    await _seed_device(session)
    resp = await client.post(
        "/api/runs",
        json={
            "device_serial": "S1",
            "goal": "打开设置",
            "functionMapContext": "x" * 8001,
        },
    )
    assert resp.status_code == 400
    assert "functionMapContext 超出" in resp.json()["detail"]

    dev = (await client.get("/api/devices/S1")).json()
    assert dev["lock"] is None


@pytest.mark.asyncio
async def test_create_run_with_case_id(client, session):
    await _seed_device(session)
    case = await client.post(
        "/api/cases", json={"title": "打开设置", "goal": "打开 settings app"}
    )
    case_id = case.json()["id"]

    run = await client.post(
        "/api/runs", json={"device_serial": "S1", "case_id": case_id}
    )
    assert run.status_code == 201
    body = run.json()
    assert body["case_id"] == case_id
    assert body["goal"] == "打开 settings app"


@pytest.mark.asyncio
async def test_run_create_needs_goal_or_case(client, session):
    await _seed_device(session)
    bad = await client.post("/api/runs", json={"device_serial": "S1"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_run_list_and_detail(client, session):
    await _seed_device(session)
    r = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
    run_id = r.json()["id"]

    lst = await client.get("/api/runs")
    assert len(lst.json()) == 1

    lst_by_dev = await client.get("/api/runs?device_serial=S1")
    assert len(lst_by_dev.json()) == 1

    detail = await client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200 and detail.json()["id"] == run_id

    steps = await client.get(f"/api/runs/{run_id}/steps")
    assert steps.status_code == 200 and steps.json() == []

    logs = await client.get(f"/api/runs/{run_id}/logs")
    assert logs.status_code == 200
    assert logs.json()["items"] == []
    assert logs.json()["next_since_id"] == 0

    commands = await client.get(f"/api/runs/{run_id}/commands")
    assert commands.status_code == 200
    assert commands.json() == []


@pytest.mark.asyncio
async def test_run_commands_are_read_only_timeline(client, session):
    await _seed_device(session)
    r = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
    run_id = r.json()["id"]
    session.add(
        RunCommand(
            run_id=run_id,
            step=1,
            message_id="cmd-1",
            method="screenshot_jpeg",
            agent_id="agent-local",
            serial="S1",
            ok=True,
            rpc_elapsed_ms=42,
        )
    )
    await session.commit()

    resp = await client.get(f"/api/runs/{run_id}/commands")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["message_id"] == "cmd-1"
    assert body[0]["method"] == "screenshot_jpeg"
    assert body[0]["rpc_elapsed_ms"] == 42


@pytest.mark.asyncio
async def test_run_detail_includes_error_summary_from_log(client, session):
    await _seed_device(session)
    r = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
    run_id = r.json()["id"]
    session.add(
        RunLog(
            run_id=run_id,
            level=3,
            title="Agent 离线",
            content="agent_offline: agent-x",
            error_class="AgentOffline",
            error_category="agent_offline",
            trace_id="trace-1",
        )
    )
    await session.commit()

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    summary = resp.json()["error_summary"]
    assert summary["category"] == "agent_offline"
    assert summary["error_class"] == "AgentOffline"
    assert summary["source"] == "run_log"


@pytest.mark.asyncio
async def test_run_detail_includes_error_summary_from_command(client, session):
    await _seed_device(session)
    r = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
    run_id = r.json()["id"]
    session.add(
        RunCommand(
            run_id=run_id,
            message_id="cmd-err",
            method="click",
            ok=False,
            error_class="AdbError",
            error_category="device",
            error_msg="adb input tap failed",
        )
    )
    await session.commit()

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    summary = resp.json()["error_summary"]
    assert summary["category"] == "device"
    assert summary["error_class"] == "AdbError"
    assert summary["method"] == "click"


@pytest.mark.asyncio
async def test_stop_run_releases_lock(client, session):
    await _seed_device(session)
    r = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g"})
    run_id = r.json()["id"]

    stop = await client.post(f"/api/runs/{run_id}/stop")
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"

    # stop 后 lock 应被清空（直接看设备状态，避免靠下一个 run 间接验证）
    dev = (await client.get("/api/devices/S1")).json()
    assert dev["lock"] is None

    # 锁已释放 → 再下一个 run 应能占（且新 run 的 holder = 新 run_id）
    r2 = await client.post("/api/runs", json={"device_serial": "S1", "goal": "g2"})
    assert r2.status_code == 201
    dev2 = (await client.get("/api/devices/S1")).json()
    assert dev2["lock"]["holder"] == r2.json()["id"]


@pytest.mark.asyncio
async def test_stop_stale_agent_brain_run_resends_to_agent(client, session, app):
    await _seed_device(session)
    hub = Hub()
    ws = _FakeWs()
    app.state.hub = hub
    await hub.register_agent("agent-local", "mac", "Darwin", ws)
    await hub.set_devices("agent-local", {"S1"})

    run = Run(
        id="stale-mid",
        device_serial="S1",
        agent_id="agent-local",
        goal="g",
        status="stopped",
        reason="stopped_by_user",
        engine="midscene",
        execution_mode="agent_brain",
    )
    session.add(run)
    await session.commit()

    resp = await client.post("/api/runs/stale-mid/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    assert ws.sent[-1] == {"type": "stop_run", "run_id": "stale-mid"}
