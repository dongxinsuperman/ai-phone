"""Server API 集成测试：devices / cases / runs / 占用锁。

走 FastAPI + httpx.AsyncClient + aiosqlite 内存库，不碰真 PG。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_phone.server.models import Device, RunCommand


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
