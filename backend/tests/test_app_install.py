from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_phone.server.app_install.service import handle_result, mark_startup_unknown
from ai_phone.server.hub import Hub
from ai_phone.server.models import AppInstallTask, AppInstallTaskItem, AppPackage, Device
from ai_phone.shared import protocol as P


class _FakeWs:
    def __init__(self) -> None:
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def _seed_ready_device(session, hub: Hub, serial: str = "A1") -> _FakeWs:
    ws = _FakeWs()
    await hub.register_agent("agent-a", "agent-a", "Darwin", ws)
    await hub.set_devices("agent-a", {serial})
    hub.set_device_readiness(serial, {"ready": True, "hint": "", "ts": 1.0})
    session.add(
        Device(
            serial=serial,
            agent_id="agent-a",
            platform="android",
            brand="Pixel",
            model="8",
            os_version="14",
            status="online",
            last_seen_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return ws


async def _seed_package(session, tmp_path) -> AppPackage:
    pkg_path = tmp_path / "demo.apk"
    pkg_path.write_bytes(b"fake apk")
    pkg = AppPackage(
        filename="demo.apk",
        platform="android",
        storage_path=str(pkg_path),
    )
    session.add(pkg)
    await session.commit()
    await session.refresh(pkg)
    return pkg


@pytest.mark.asyncio
async def test_create_task_dispatches_to_device_agent(client, app, session, tmp_path):
    hub = Hub()
    app.state.hub = hub
    ws = await _seed_ready_device(session, hub)
    pkg = await _seed_package(session, tmp_path)

    resp = await client.post(
        "/api/app-install/tasks",
        json={"package_id": pkg.id, "serials": ["A1"]},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["summary"]["running"] == 1
    assert len(ws.sent) == 1
    payload = ws.sent[0]
    assert payload["type"] == P.MSG_APP_INSTALL_START
    assert payload["serial"] == "A1"
    assert payload["package_url"] == f"/api/app-install/packages/{pkg.id}/download"


@pytest.mark.asyncio
async def test_handle_result_updates_item_and_task(app, session, tmp_path):
    hub = Hub()
    app.state.hub = hub
    pkg = await _seed_package(session, tmp_path)
    task = AppInstallTask(package_id=pkg.id, state="running")
    session.add(task)
    await session.flush()
    item = AppInstallTaskItem(
        task_id=task.id,
        serial="A1",
        platform="android",
        state="running",
        started_at=datetime.now(timezone.utc),
    )
    session.add(item)
    await session.commit()

    await handle_result(
        session,
        {
            "type": P.MSG_APP_INSTALL_RESULT,
            "task_id": task.id,
            "item_id": item.id,
            "serial": "A1",
            "success": True,
            "message": "ok",
        },
    )

    refreshed_item = await session.get(AppInstallTaskItem, item.id)
    refreshed_task = await session.get(AppInstallTask, task.id)
    assert refreshed_item.state == "success"
    assert refreshed_item.reason == ""
    assert refreshed_task.state == "done"
    assert refreshed_task.finished_at is not None


@pytest.mark.asyncio
async def test_retry_unsuccessful_only_dispatches_terminal_failures(client, app, session, tmp_path):
    hub = Hub()
    app.state.hub = hub
    ws = await _seed_ready_device(session, hub)
    pkg = await _seed_package(session, tmp_path)
    task = AppInstallTask(package_id=pkg.id, state="done", finished_at=datetime.now(timezone.utc))
    session.add(task)
    await session.flush()
    ok_item = AppInstallTaskItem(
        task_id=task.id,
        serial="A1",
        platform="android",
        state="success",
        message="安装成功",
        finished_at=datetime.now(timezone.utc),
    )
    bad_item = AppInstallTaskItem(
        task_id=task.id,
        serial="A1",
        platform="android",
        state="failed",
        reason="install_failed",
        message="old error",
        finished_at=datetime.now(timezone.utc),
    )
    session.add_all([ok_item, bad_item])
    await session.commit()

    resp = await client.post(f"/api/app-install/tasks/{task.id}/retry-unsuccessful")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = {it["id"]: it for it in body["items"]}
    assert items[ok_item.id]["state"] == "success"
    assert items[bad_item.id]["state"] == "running"
    assert items[bad_item.id]["reason"] is None
    assert len(ws.sent) == 1
    assert ws.sent[0]["item_id"] == bad_item.id


@pytest.mark.asyncio
async def test_handle_result_timeout_reason_sets_timeout_state(app, session, tmp_path):
    app.state.hub = Hub()
    pkg = await _seed_package(session, tmp_path)
    task = AppInstallTask(package_id=pkg.id, state="running")
    session.add(task)
    await session.flush()
    item = AppInstallTaskItem(
        task_id=task.id,
        serial="A1",
        platform="android",
        state="running",
        started_at=datetime.now(timezone.utc),
    )
    session.add(item)
    await session.commit()

    await handle_result(
        session,
        {
            "type": P.MSG_APP_INSTALL_RESULT,
            "task_id": task.id,
            "item_id": item.id,
            "serial": "A1",
            "success": False,
            "reason": "timeout",
            "message": "安装超过 600s 未完成",
        },
    )

    refreshed_item = await session.get(AppInstallTaskItem, item.id)
    refreshed_task = await session.get(AppInstallTask, task.id)
    assert refreshed_item.state == "timeout"
    assert refreshed_item.reason == "timeout"
    assert refreshed_task.state == "done"


@pytest.mark.asyncio
async def test_startup_marks_active_items_unknown(_test_engine, session, tmp_path):
    pkg = await _seed_package(session, tmp_path)
    task = AppInstallTask(package_id=pkg.id, state="running")
    session.add(task)
    await session.flush()
    item = AppInstallTaskItem(
        task_id=task.id,
        serial="A1",
        platform="android",
        state="running",
        started_at=datetime.now(timezone.utc),
    )
    session.add(item)
    await session.commit()

    changed = await mark_startup_unknown(session)

    refreshed_item = await session.get(AppInstallTaskItem, item.id)
    refreshed_task = await session.get(AppInstallTask, task.id)
    assert changed == 1
    assert refreshed_item.state == "unknown"
    assert refreshed_item.reason == "unknown_server_restarted"
    assert refreshed_task.state == "done"
