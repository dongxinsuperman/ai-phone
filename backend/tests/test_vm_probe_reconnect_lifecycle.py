"""Old socket DB cleanup must not fail a new same-ID Agent's operation.

Run with the isolated guard runner: real temporary DB sessions, fake Hub,
deterministic Events, no native devices, environment file, or network calls.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from sqlalchemy import select

from ai_phone.server import db
from ai_phone.server.models import DeviceAlias
from ai_phone.server.vm_lifecycle import begin_operation, operation_lock_key, runtime, transaction_lock
from tests.test_vm_lifecycle_contract import ContractHub, make_vm, platform as platform_fixture

platform = platform_fixture


def _runtime_serial(platform, vm):
    if platform.name == "harmony":
        return f"harmony-vm:{vm.id}"
    return getattr(vm, platform.serial)


async def _aliases(session):
    rows = (await session.execute(select(DeviceAlias).order_by(DeviceAlias.serial))).scalars().all()
    return [(row.serial, row.alias, row.note) for row in rows]


async def _start_pending(session, vm, request_id):
    await transaction_lock(session, operation_lock_key(vm))
    begin_operation(
        vm, "start", request_id=request_id, target_agent_id="old",
        target_host_id="host-one", first_bind=False, timeout_sec=120,
    )
    vm.state = "starting"
    vm.error_message = ""
    if hasattr(vm, "error_code"):
        vm.error_code = ""
    await session.commit()


async def _mark_disconnected(platform, hub):
    if platform.name == "android":
        return await platform.svc.mark_agent_vms_unavailable("old", hub=hub)
    return await platform.svc.mark_agent_vms_offline("old", hub=hub)


@pytest.mark.asyncio
async def test_old_disconnect_cannot_fail_new_same_id_pending_start(session, platform, monkeypatch):
    """旧查询完成后同 ID 已重连，锁内刷新不得把新尝试及别名改回离线。"""
    vm = await make_vm(session, platform, state="starting")
    vm_id = vm.id
    session.add(DeviceAlias(serial=_runtime_serial(platform, vm), alias=vm.alias, note="preserve-alias"))
    await _start_pending(session, vm, "old-session-request")
    hub = ContractHub(online=())
    assert hub.agent_host_id("old") == "host-one"

    old_query_returned, resume_old_cleanup = asyncio.Event(), asyncio.Event()
    observed = {}
    original_lock = platform.svc.transaction_lock
    old_task = None

    async def pause_old_cleanup_before_lock(cleanup_session, key):
        if asyncio.current_task() is old_task:
            # The mark function has already selected its VM list. get() reads
            # that session's identity map without replacing the stale snapshot.
            selected = await cleanup_session.get(platform.model, vm_id)
            observed["selected_request"] = runtime(selected)["operation"]["request_id"]
            observed["lock_key"] = key
            old_query_returned.set()
            await resume_old_cleanup.wait()
        await original_lock(cleanup_session, key)

    monkeypatch.setattr(platform.svc, "transaction_lock", pause_old_cleanup_before_lock)
    old_task = asyncio.create_task(_mark_disconnected(platform, hub), name="old-agent-disconnect")
    try:
        await asyncio.wait_for(old_query_returned.wait(), timeout=2)
        assert observed["selected_request"] == "old-session-request"
        assert observed["lock_key"] == operation_lock_key(vm)
        assert not old_task.done()

        # Same agent_id and same host, but a newer live WebSocket session.
        hub.online.add("old")
        async with db.get_session_factory()() as new_session:
            current = await new_session.get(platform.model, vm_id)
            await _start_pending(new_session, current, "new-session-request")
            expected_runtime = copy.deepcopy(runtime(current))
            expected_owner, expected_alias = current.assigned_agent_id, current.alias
            expected_alias_rows = await _aliases(new_session)

        resume_old_cleanup.set()
        await asyncio.wait_for(old_task, timeout=2)
    finally:
        resume_old_cleanup.set()
        if not old_task.done():
            old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)

    async with db.get_session_factory()() as check:
        current = await check.get(platform.model, vm_id)
        assert current.state == "starting"
        assert current.assigned_agent_id == expected_owner == "old"
        assert current.alias == expected_alias
        assert current.error_message == ""
        assert runtime(current) == expected_runtime, "旧离线尾部不得修改新操作、归属或离线标记"
        assert runtime(current)["operation"]["request_id"] == "new-session-request"
        assert runtime(current)["operation"]["pending"] is True
        assert runtime(current)["operation"]["outcome"] == "pending"
        assert runtime(current)["owner_host_id"] == "host-one"
        assert await _aliases(check) == expected_alias_rows
    assert not hub.sent


@pytest.mark.asyncio
async def test_current_offline_agent_still_fails_its_pending_start(session, platform):
    """没有同 ID 新连接时仍必须结束旧 pending，不能把离线处理整体跳过。"""
    vm = await make_vm(session, platform, state="starting")
    vm_id = vm.id
    session.add(DeviceAlias(serial=_runtime_serial(platform, vm), alias=vm.alias, note="existing-alias"))
    await _start_pending(session, vm, "disconnected-session-request")
    hub = ContractHub(online=())

    await asyncio.wait_for(_mark_disconnected(platform, hub), timeout=2)
    async with db.get_session_factory()() as check:
        current = await check.get(platform.model, vm_id)
        operation = runtime(current)["operation"]
        assert current.state == "error"
        assert current.assigned_agent_id == "old"
        assert current.alias == "contract-vm"
        assert current.error_message
        assert operation["request_id"] == "disconnected-session-request"
        assert operation["target_agent_id"] == "old"
        assert operation["pending"] is False
        assert operation["outcome"] == "failed"
        assert operation["failure_reason"] == "agent_disconnected"
        assert runtime(current)["owner_host_id"] == "host-one"
    assert not hub.sent
