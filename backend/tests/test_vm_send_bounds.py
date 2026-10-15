"""Bounded lifecycle sends against fake connections and an isolated database."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from ai_phone.server import db, vm_lifecycle
from ai_phone.server.vm_lifecycle import runtime
from tests.test_vm_lifecycle_contract import ContractHub, make_vm, platform, start_first


class SlowHub(ContractHub):
    """A selected send never completes unless the caller cancels it."""

    def __init__(self, *, blocked_types, before_wait=None):
        super().__init__()
        self.blocked_types = set(blocked_types)
        self.before_wait = before_wait
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def send_to_agent(self, agent_id, payload):
        self.sent.append((agent_id, dict(payload)))
        if payload.get("type") in self.blocked_types:
            if self.before_wait is not None:
                await self.before_wait(agent_id, payload)
            self.waiting.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return self.has_agent(agent_id)


@pytest.fixture(autouse=True)
def short_send_limits(monkeypatch):
    monkeypatch.setattr(vm_lifecycle, "CLEANUP_SEND_TIMEOUT_SEC", 0.02)
    monkeypatch.setattr(vm_lifecycle, "OPERATION_SEND_TIMEOUT_SEC", 0.02)


def _allow_capability(platform, monkeypatch):
    async def available(**_kwargs):
        return {"ok": True}

    if platform.name != "android":
        monkeypatch.setattr(
            platform.api,
            "get_capability_waiter",
            lambda: SimpleNamespace(probe_agent=available, excluded_ports_for=lambda _agent: []),
        )


def _messages(hub, action):
    return [(agent, payload) for agent, payload in hub.sent if payload["type"].endswith(f"vm_{action}")]


@pytest.mark.asyncio
async def test_switch_keeps_new_draft_in_ledger_while_old_cleanup_send_stalls(
    session, platform, monkeypatch
):
    """旧清理发送卡住时，新草稿已经落库；超时后只向新宿主启动一次。"""
    old = await make_vm(session, platform, state="stopped")
    old_id, old_alias = old.id, old.alias
    _allow_capability(platform, monkeypatch)
    observed = {}

    async def inspect_ledger(agent_id, payload):
        async with db.get_session_factory()() as check:
            rows = (await check.execute(select(platform.model))).scalars().all()
            observed["rows"] = [(row.id, row.state, row.alias, row.assigned_agent_id) for row in rows]
        observed["target"] = agent_id
        observed["old_id"] = payload["vm_id"]

    hub = SlowHub(blocked_types={platform.delete}, before_wait=inspect_ledger)
    result = await asyncio.wait_for(
        platform.api._switch_agent(vm=old, new_agent_id="new", session=session, hub=hub),
        timeout=1.0,
    )
    new_id = result["instance"]["id"]
    assert new_id != old_id
    assert observed["rows"] == [(new_id, "draft", old_alias, None)], (
        "在等待旧宿主清理之前必须已原子替换总账，不能留下空账窗口"
    )
    assert observed["target"] == "old" and observed["old_id"] == old_id
    assert hub.waiting.is_set() and hub.cancelled.is_set()
    assert result["switched"] is True and result["sent"] is True
    assert result["old_cleanup_sent"] is False
    assert result["instance"]["state"] == "starting"
    assert result["instance"]["assigned_agent_id"] is None
    async with db.get_session_factory()() as check:
        rows = (await check.execute(select(platform.model))).scalars().all()
        assert [row.id for row in rows] == [new_id]
        assert runtime(rows[0])["operation"]["target_agent_id"] == "new"
    await asyncio.sleep(0.05)
    assert [(agent, payload["vm_id"]) for agent, payload in _messages(hub, "delete")] == [("old", old_id)]
    assert [(agent, payload["vm_id"]) for agent, payload in _messages(hub, "start")] == [("new", new_id)]
    vm_lifecycle.cancel_operation_timeout(rows[0])


@pytest.mark.asyncio
async def test_blocked_first_start_send_ends_once_without_binding_or_retry(
    session, platform, monkeypatch
):
    """首次启动发送超时必须结束本次尝试，不能绑定或在后台重复下发。"""
    start_type = platform.delete.removesuffix("delete") + "start"
    hub = SlowHub(blocked_types={start_type})
    with pytest.raises(HTTPException) as failure:
        await asyncio.wait_for(start_first(session, platform, hub, monkeypatch), timeout=1.0)
    assert failure.value.status_code == (502 if platform.name == "ios" else 409)
    detail = failure.value.detail
    assert detail["sent"] is False and detail["message"]
    rows = (await session.execute(select(platform.model))).scalars().all()
    assert len(rows) == 1
    vm = rows[0]
    await session.refresh(vm)
    assert detail["vm_id"] == vm.id and detail["instance"]["id"] == vm.id
    assert hub.waiting.is_set() and hub.cancelled.is_set()
    assert vm.state == "error" and vm.assigned_agent_id is None
    operation = runtime(vm)["operation"]
    assert operation["action"] == "start" and operation["first_bind"] is True
    assert operation["outcome"] == "failed" and operation["pending"] is False
    assert operation["failure_reason"] == "send_failed"
    assert detail["request_id"] == operation["request_id"]
    assert detail["instance"]["state"] == "error"
    await asyncio.sleep(0.05)
    assert len(_messages(hub, "start")) == 1
    assert len(hub.sent) == 1, "发送超时不得隐式重试、切换宿主或继续操作"


@pytest.mark.asyncio
async def test_blocked_stop_send_fails_once_and_native_stop_can_be_retried(
    session, platform
):
    """停止发送超时保留已确认归属；只有用户再次调用原停止接口才创建新尝试。"""
    vm = await make_vm(session, platform, state="running")
    stop_type = platform.delete.removesuffix("delete") + "stop"
    hub = SlowHub(blocked_types={stop_type})
    stop = platform.api.stop if platform.name == "ios" else platform.api.stop_instance
    with pytest.raises(HTTPException) as failure:
        await asyncio.wait_for(stop(vm_id=vm.id, session=session, hub=hub), timeout=1.0)
    assert failure.value.status_code == (502 if platform.name == "ios" else 409)
    detail = failure.value.detail
    await session.refresh(vm)
    assert detail["sent"] is False and detail["message"]
    assert detail["vm_id"] == vm.id and detail["instance"]["id"] == vm.id
    assert hub.waiting.is_set() and hub.cancelled.is_set()
    assert vm.state == "error" and vm.assigned_agent_id == "old"
    operation = runtime(vm)["operation"]
    assert operation["action"] == "stop" and operation["outcome"] == "failed"
    assert operation["pending"] is False and operation["failure_reason"] == "send_failed"
    first_request = operation["request_id"]
    assert detail["request_id"] == first_request
    assert detail["instance"]["state"] == "error"
    await asyncio.sleep(0.05)
    assert len(_messages(hub, "stop")) == 1

    hub.blocked_types.clear()
    retry = await asyncio.wait_for(stop(vm_id=vm.id, session=session, hub=hub), timeout=1.0)
    await session.refresh(vm)
    assert retry["sent"] is True
    assert vm.state == "stopping" and vm.assigned_agent_id == "old"
    assert runtime(vm)["operation"]["request_id"] != first_request
    assert runtime(vm)["operation"]["pending"] is True
    assert len(_messages(hub, "stop")) == 2
    assert len(hub.sent) == 2
    vm_lifecycle.cancel_operation_timeout(vm)
