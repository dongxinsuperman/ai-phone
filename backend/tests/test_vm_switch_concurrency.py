"""Visible replacement configs remain independent of an older switch request."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from ai_phone.server import db
from ai_phone.server.vm_lifecycle import runtime
from tests.test_vm_lifecycle_contract import make_vm, platform as platform_fixture
from tests.test_vm_send_bounds import SlowHub, _allow_capability, _messages

platform = platform_fixture


async def _replacement(platform):
    async with db.get_session_factory()() as check:
        rows = list((await check.execute(select(platform.model))).scalars())
        assert len(rows) == 1 and rows[0].state == "draft"
        return rows[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("resulting_state", ["running", "stopped"])
async def test_original_switch_cannot_overwrite_or_restart_another_callers_machine(
    session, platform, monkeypatch, resulting_state
):
    old = await make_vm(session, platform)
    _allow_capability(platform, monkeypatch)
    hub = SlowHub(blocked_types={platform.delete})
    switching = asyncio.create_task(
        platform.api._switch_agent(vm=old, new_agent_id="new", session=session, hub=hub)
    )
    await asyncio.wait_for(hub.waiting.wait(), timeout=1)
    new_id = await _replacement(platform)
    async with db.get_session_factory()() as other:
        if platform.name == "android":
            started = await platform.api._send_start(vm_id=new_id, agent_id="new", session=other, hub=hub)
        else:
            started = await platform.api._send_start(new_id, "new", other, hub)
        current = await other.get(platform.model, new_id)
        started_request = runtime(current)["operation"]["request_id"]
        running = {
            "vm_id": new_id,
            "request_id": started_request,
            "state": "running",
            "ok": True,
            "reason": "started",
        }
        if platform.name == "android":
            running["adb_serial"] = current.adb_serial
        elif platform.name == "ios":
            running["udid"] = "concurrent-user-owned-udid"
        else:
            running["lease_token"], running["hdc_serial"] = current.lease_token, current.hdc_serial
        await platform.svc.handle_vm_status("new", running, hub)
        await other.refresh(current)
        assert started["sent"] and current.state == "running"
        if resulting_state == "stopped":
            stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
            await stop(vm_id=new_id, session=other, hub=hub)
            stopped = {
                "vm_id": new_id,
                "request_id": runtime(current)["operation"]["request_id"],
                "state": "stopped",
                "ok": True,
                "reason": "stopped",
                "details": {"cleanup_confirmed": True},
            }
            if platform.name == "harmony":
                stopped["lease_token"], stopped["hdc_serial"] = current.lease_token, current.hdc_serial
            await platform.svc.handle_vm_status("new", stopped, hub)
            await other.refresh(current)
        protected_operation = dict(runtime(current)["operation"])
    hub.release.set()
    with pytest.raises(HTTPException) as conflict:
        await asyncio.wait_for(switching, timeout=2)
    assert conflict.value.status_code == 409
    result = conflict.value.detail
    assert result["switched"] and result["vm_id"] == new_id
    assert result["instance"]["state"] == resulting_state
    assert result["instance"]["assigned_agent_id"] == "new"
    async with db.get_session_factory()() as check:
        current = await check.get(platform.model, new_id)
        assert current.state == resulting_state and current.error_message == ""
        assert runtime(current)["operation"] == protected_operation
        assert runtime(current)["operation"]["outcome"] == "succeeded"
    assert len(_messages(hub, "start")) == 1, "the original switch must not repeat an already-used draft"


@pytest.mark.asyncio
async def test_original_switch_cannot_revive_replacement_deleted_by_another_caller(
    session, platform, monkeypatch
):
    old = await make_vm(session, platform)
    _allow_capability(platform, monkeypatch)
    hub = SlowHub(blocked_types={platform.delete})
    switching = asyncio.create_task(
        platform.api._switch_agent(vm=old, new_agent_id="new", session=session, hub=hub)
    )
    await asyncio.wait_for(hub.waiting.wait(), timeout=1)
    new_id = await _replacement(platform)
    async with db.get_session_factory()() as other:
        deleted = await platform.api.delete_instance(vm_id=new_id, session=other, hub=hub)
        assert deleted["deleted"]
    hub.release.set()
    with pytest.raises(HTTPException) as exc:
        await asyncio.wait_for(switching, timeout=2)
    assert exc.value.status_code == 409
    assert not _messages(hub, "start")
    async with db.get_session_factory()() as check:
        assert not list((await check.execute(select(platform.model))).scalars())
