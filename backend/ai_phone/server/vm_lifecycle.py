"""Small shared guards for the existing three VM lifecycle implementations.

The VM row remains the authority. Each native operation has one bounded attempt
in its existing runtime JSON; there is no retry or resume workflow.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
import weakref
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlalchemy import event, text

_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_operation_tasks: dict[tuple[str, str], asyncio.Task] = {}
CLEANUP_SEND_TIMEOUT_SEC = 2.0
OPERATION_SEND_TIMEOUT_SEC = 5.0


async def transaction_lock(session, key: str) -> None:
    """Serialize decisions through commit, including across production workers."""
    held = session.info.setdefault("vm_lifecycle_locks", set())
    if key in held:
        return
    if session.bind.dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") & ((1 << 63) - 1)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})
        lock = None
    else:
        # SQLite has no advisory locks. Used by isolated tests/local deployments.
        lock_key = (id(session.bind), id(asyncio.get_running_loop()), key)
        lock = _locks.setdefault(lock_key, asyncio.Lock())
        await lock.acquire()
        try:
            await session.connection()
        except BaseException:
            lock.release()
            raise
    held.add(key)

    released = False

    def release(sync_session, transaction):
        nonlocal released
        if transaction.parent is None and not released:
            released = True
            held.discard(key)
            if lock is not None and lock.locked():
                lock.release()

    event.listen(session.sync_session, "after_transaction_end", release)


def runtime(vm) -> dict:
    return (
        dict(getattr(vm, "runtime_state", None) or {})
        if hasattr(vm, "runtime_state")
        else dict(vm.runtime or {})
    )


def update_runtime(vm, **values) -> None:
    setattr(vm, "runtime_state" if hasattr(vm, "runtime_state") else "runtime", {**runtime(vm), **values})


def require_idle(vm, *, allow_running=False) -> None:
    blocked = {
        "starting",
        "stopping",
        "probing",
        "dispatching",
        "creating",
        "connecting_hdc",
        "checking_driver",
    }
    if not allow_running:
        blocked.add("running")
    if vm.state in blocked:
        raise HTTPException(409, "实例正在操作中，请等待本次结果")


def require_startable(vm) -> None:
    require_idle(vm)
    data = runtime(vm)
    last = data.get("last_status") or {}
    details = last.get("details") or data.get("details") or {}
    op = data.get("operation") or {}
    if (
        op.get("first_bind")
        and op.get("outcome") == "failed"
        and op.get("created_in_attempt") is False
        and details.get("instance_created")
    ):
        raise HTTPException(409, "本次未创建该宿主实例，不能自动接管已有数据；请使用新配置")
    if vm.state not in {"draft", "stopped"} and not (
        vm.state in {"error", "unavailable"} and details.get("cleanup_confirmed")
    ):
        raise HTTPException(409, "实例尚未确认停止，请先重试停止或等待对账")


def host_id(hub, agent_id: str) -> str:
    fn = getattr(hub, "agent_host_id", None)
    return str(fn(agent_id) or "") if fn else ""


def bind_owner(vm, agent_id: str, hub) -> None:
    vm.assigned_agent_id = agent_id
    value = host_id(hub, agent_id)
    if value:
        update_runtime(vm, owner_host_id=value)


def accept_owner(vm, agent_id: str, hub, *, legacy_evidence=False) -> bool:
    owner = str(vm.assigned_agent_id or "")
    reporter_host = host_id(hub, agent_id)
    expected_host = str(runtime(vm).get("owner_host_id") or "")
    op = runtime(vm).get("operation") or {}
    if not owner and op.get("first_bind"):
        target_host = str(op.get("target_host_id") or "")
        if op.get("target_agent_id") == agent_id:
            return not (target_host and reporter_host and target_host != reporter_host)
        if not op.get("pending") and target_host and target_host == reporter_host:
            return True
    if owner == agent_id:
        if expected_host and reporter_host and expected_host != reporter_host:
            return False
        bind_owner(vm, agent_id, hub)
        return True
    if not owner or hub is None or getattr(hub, "has_agent", lambda _: True)(owner):
        return False
    same_host = bool(expected_host and reporter_host == expected_host)
    if same_host or (not expected_host and legacy_evidence):
        bind_owner(vm, agent_id, hub)
        return True
    return False


def begin_operation(
    vm,
    action: str,
    request_id: str = "",
    *,
    target_agent_id: str = "",
    target_host_id: str = "",
    first_bind: bool = False,
    timeout_sec: float | None = None,
) -> str:
    cancel_operation_timeout(vm)
    request_id = request_id or uuid.uuid4().hex[:16]
    seconds = float(timeout_sec if timeout_sec is not None else operation_timeout_seconds(vm, action))
    started = datetime.now(timezone.utc)
    update_runtime(
        vm,
        operation={
            "action": action,
            "request_id": request_id,
            "pending": True,
            "outcome": "pending",
            "target_agent_id": target_agent_id or str(vm.assigned_agent_id or ""),
            "target_host_id": target_host_id,
            "first_bind": first_bind,
            "started_at": started.isoformat(),
            "deadline_at": (started + timedelta(seconds=seconds)).isoformat(),
            "timeout_sec": seconds,
        },
    )
    return request_id


def accept_status(vm, msg: dict) -> bool:
    op = runtime(vm).get("operation") or {}
    if operation_deadline_expired(vm):
        return False
    request_id = str(msg.get("request_id") or "")
    observed = str(msg.get("observed_request_id") or "")
    if observed and op.get("request_id") and observed != op["request_id"]:
        return False
    if op.get("outcome") == "failed" and not op.get("pending"):
        # A late rollback result may confirm cleanup, never bind/start again.
        details = msg.get("details") or {}
        matching = request_id == op.get("request_id") and bool(details.get("cleanup_confirmed"))
        late_stop_fact = (
            op.get("action") == "stop" and msg.get("state") == "stopped" and msg.get("ok") is True
        )
        return bool(matching and (msg.get("ok") is False or late_stop_fact))
    if request_id:
        return not op or (request_id == op.get("request_id") and bool(op.get("pending")))
    if op.get("pending"):
        # First binding is confirmed only by its exact operation ACK.
        if op.get("first_bind"):
            return False
        expected = {"start": "running", "stop": "stopped"}.get(op.get("action"))
        return msg.get("reason") == "reclaimed" and msg.get("state") == expected
    return True


def finish_operation(vm, msg: dict) -> None:
    op = runtime(vm).get("operation") or {}
    if not op.get("pending"):
        return  # a health observation must not rewrite a completed attempt
    if msg.get("state") in {"running", "stopped", "error", "unavailable"}:
        failed = msg.get("state") in {"error", "unavailable"} or msg.get("ok") is False
        update_runtime(
            vm,
            operation={
                **op,
                "pending": False,
                "outcome": "failed" if failed else "succeeded",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        cancel_operation_timeout(vm)


def maintenance(hub, vm, enabled: bool) -> None:
    fn = getattr(hub, "set_vm_maintenance", None)
    if not fn:
        return
    if hasattr(vm, "hdc_serial"):
        serial = f"harmony-vm:{vm.id}"
    else:
        serial = str(getattr(vm, "udid", None) or getattr(vm, "adb_serial", None) or "")
    if serial:
        fn(serial, vm.id if enabled else None)


def require_device_idle(hub, vm) -> None:
    from .lockstore import get_default_lock_store

    store = getattr(hub, "vm_lock_store", None) or get_default_lock_store()
    serial = (
        f"harmony-vm:{vm.id}"
        if hasattr(vm, "hdc_serial")
        else str(getattr(vm, "udid", None) or getattr(vm, "adb_serial", None) or "")
    )
    subscribers = getattr(hub, "subscriber_count", None)
    if serial and (store.peek(serial) is not None or (subscribers and subscribers(serial))):
        raise HTTPException(409, "device_busy: 设备正在执行任务、安装或观看，请结束后再操作")


def device_serial(vm) -> str:
    return (
        f"harmony-vm:{vm.id}"
        if hasattr(vm, "hdc_serial")
        else str(getattr(vm, "udid", None) or getattr(vm, "adb_serial", None) or "")
    )


async def delete_device_record(session, vm) -> None:
    from sqlalchemy import delete
    from .models import Device

    serial = device_serial(vm)
    if serial:
        await session.execute(
            delete(Device).where(Device.serial == serial, Device.agent_id == vm.assigned_agent_id)
        )


async def forget_device_route(hub, vm) -> None:
    """After ledger deletion, remove only the retired VM's live route."""
    serial = device_serial(vm)
    if not hub or not serial:
        return
    extra_fn = getattr(hub, "get_device_extra", None)
    extra = extra_fn(serial) if extra_fn else {}
    reported_id = str(extra.get("vm_instance_id") or "")
    if reported_id and reported_id != vm.id:
        return  # the port already belongs to a different, newer VM
    owner_fn = getattr(hub, "agent_id_for_serial", None)
    owner = owner_fn(serial) if owner_fn else None
    if owner and owner != vm.assigned_agent_id:
        return
    if owner and getattr(hub, "detach_device", None):
        await hub.detach_device(owner, serial)
    clear = getattr(hub, "clear_device_extra", None)
    if clear:
        clear({serial})
    maintenance(hub, vm, False)


def owner_host_matches(vm, hub) -> bool:
    expected = str(runtime(vm).get("owner_host_id") or "")
    actual = host_id(hub, str(vm.assigned_agent_id or ""))
    return not (expected and actual and expected != actual)


def require_owner_host(vm, hub) -> None:
    if not owner_host_matches(vm, hub):
        raise HTTPException(409, "Agent 标识对应的宿主已变化，原实例归属尚未确认，禁止在其他宿主执行")


async def cleanup_foreign_copy(hub, vm, reporter: str, proof: dict, report: dict | None = None) -> bool:
    """Delete a proved platform copy on a different known host, never the owner.

    VM ID alone or legacy unknown host identity is not sufficient evidence.
    Transport fields come only from the reporter, not the authoritative VM row.
    """
    from ai_phone.shared import protocol as P
    import re

    expected, actual = str(runtime(vm).get("owner_host_id") or ""), host_id(hub, reporter)
    if not expected or not actual or expected == actual:
        return False
    report = report or {}
    payload = {"request_id": uuid.uuid4().hex[:16], "vm_id": vm.id}
    if hasattr(vm, "udid"):
        udid = str(proof.get("udid") or report.get("udid") or "")
        if (proof.get("name") or proof.get("sim_name")) != f"aiphone_sim_{vm.id}" or not udid:
            return False
        payload.update(type=P.MSG_IOS_SIM_VM_DELETE, udid=udid)
    elif hasattr(vm, "hdc_serial"):
        name = "aiphone_harmony_" + re.sub(r"[^A-Za-z0-9_]+", "_", vm.id)[:32]
        if proof.get("instance_name") != name or not (
            proof.get("managed_instance_present") or (report.get("lease_token") and report.get("hdc_serial"))
        ):
            return False
        payload.update(
            type=P.MSG_HARMONY_VM_DELETE,
            hdc_serial=str(report.get("hdc_serial") or ""),
            lease_token=str(report.get("lease_token") or ""),
        )
    else:
        if proof.get("avd_name") != f"aiphone_vm_{vm.id}":
            return False
        payload.update(type=P.MSG_VM_DELETE, adb_serial=str(report.get("adb_serial") or ""))
    return await send_operation_once(hub, reporter, payload, timeout_sec=CLEANUP_SEND_TIMEOUT_SEC)


async def record_send_failure(session, vm, request_id: str, message: str) -> None:
    """A failed send aborts this attempt without overwriting an already received ACK."""
    await transaction_lock(session, operation_lock_key(vm))
    current = await session.get(type(vm), vm.id, populate_existing=True)
    if current is not None:
        op = runtime(current).get("operation") or {}
        if op.get("request_id") == request_id and op.get("pending"):
            fail_attempt(current, "send_failed", message)
    await session.commit()


def operation_lock_key(vm) -> str:
    prefix = "ios-vm" if hasattr(vm, "udid") else "harmony-vm" if hasattr(vm, "hdc_serial") else "android-vm"
    return f"{prefix}:{vm.id}"


def operation_timeout_seconds(vm, action: str) -> float:
    from ai_phone.config import get_settings

    settings = get_settings()
    if action == "stop":
        return 120.0
    if hasattr(vm, "udid"):
        return float(settings.ios_sim_boot_timeout_sec + settings.wda_startup_timeout_s + 120)
    if hasattr(vm, "hdc_serial"):
        return float(settings.harmony_vm_boot_timeout_sec + 120)
    return float(settings.android_vm_boot_timeout_sec + 120)


def cancel_operation_timeout(vm) -> None:
    op = runtime(vm).get("operation") or {}
    task = _operation_tasks.pop((operation_lock_key(vm), str(op.get("request_id") or "")), None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def fail_attempt(vm, reason: str, message: str) -> None:
    op = runtime(vm).get("operation") or {}
    vm.state, vm.error_message = "error", message
    if hasattr(vm, "error_code"):
        vm.error_code = reason
    if op.get("first_bind"):
        vm.assigned_agent_id = None
    data = runtime(vm)
    details = {
        **((data.get("last_status") or {}).get("details") or data.get("details") or {}),
        "cleanup_confirmed": False,
    }
    update_runtime(
        vm, last_status={"state": "error", "ok": False, "reason": reason, "details": details}, details=details
    )
    finish_operation(vm, {"state": "error", "ok": False})
    update_runtime(vm, operation={**(runtime(vm).get("operation") or {}), "failure_reason": reason})


def arm_operation_timeout(vm, hub) -> None:
    """One deadline for one native operation. Never retries or resumes it."""
    from .db import get_session_factory

    op = runtime(vm).get("operation") or {}
    if not op.get("pending"):
        return
    key, request_id, model, vm_id = operation_lock_key(vm), str(op["request_id"]), type(vm), vm.id
    factory = get_session_factory()

    async def expire():
        # Task scheduling and DB-lock waits consume the original budget. They
        # must never create a fresh timeout window for the same attempt.
        remaining = operation_deadline_remaining(op)
        if remaining is None:
            remaining = float(op["timeout_sec"])
        while True:
            await asyncio.sleep(max(0.0, remaining))
            async with factory() as session:
                await transaction_lock(session, key)
                current = await session.get(model, vm_id)
                if current is None:
                    return
                latest = runtime(current).get("operation") or {}
                if latest.get("request_id") != request_id or not latest.get("pending"):
                    return
                remaining = operation_deadline_remaining(latest)
                if remaining is None or remaining <= 0:
                    fail_attempt(current, "operation_timeout", "本次操作超时，已结束；不会自动重试")
                    maintenance(hub, current, True)
                    await session.commit()
                    return
                # A wall-clock correction may move the UTC deadline forward;
                # release the transaction before waiting for its remainder.

    task = asyncio.create_task(expire(), name=f"vm-deadline:{vm_id}:{request_id}")
    _operation_tasks[(key, request_id)] = task
    task.add_done_callback(lambda _task: _operation_tasks.pop((key, request_id), None))


def cleanup_command(vm, message_type: str) -> tuple[str, dict]:
    op = runtime(vm).get("operation") or {}
    target = str(vm.assigned_agent_id or op.get("target_agent_id") or "")
    payload = {"type": message_type, "request_id": uuid.uuid4().hex[:16], "vm_id": vm.id}
    for key in ("adb_serial", "udid", "hdc_serial", "lease_token"):
        if hasattr(vm, key):
            payload[key] = getattr(vm, key) or ""
    return target, payload


async def send_cleanup_once(hub, vm, target: str, payload: dict) -> bool:
    if not hub or not target:
        return False
    op = runtime(vm).get("operation") or {}
    expected = str(runtime(vm).get("owner_host_id") or op.get("target_host_id") or "")
    actual = host_id(hub, target)
    if expected and actual and expected != actual:
        return False
    try:
        return bool(
            await asyncio.wait_for(hub.send_to_agent(target, payload), timeout=CLEANUP_SEND_TIMEOUT_SEC)
        )
    except Exception:
        return False


async def cleanup_abandoned_first_bind(
    hub, vm, reporter: str, proof: dict, report: dict | None = None
) -> bool:
    """Discard a proved residual from a failed first attempt; never resume it."""
    from ai_phone.shared import protocol as P

    op = runtime(vm).get("operation") or {}
    if vm.assigned_agent_id or not op.get("first_bind") or op.get("pending") or op.get("outcome") != "failed":
        return False
    report = report or {}
    # An exact managed name proves identity, not who created its private data.
    # Explicit False from this attempt is sticky and forbids automatic deletion.
    if op.get("created_in_attempt") is False:
        return False
    created_here = op.get("created_in_attempt") is True or (
        report.get("request_id") == op.get("request_id") and proof.get("created_in_attempt") is True
    )
    if not created_here:
        return False
    if report.get("request_id") and (report["request_id"] != op.get("request_id") or op.get("cleanup_sent")):
        return False
    target_host = str(op.get("target_host_id") or "")
    actual_host = host_id(hub, reporter)
    if target_host and actual_host:
        if target_host != actual_host:
            return False
    elif reporter != op.get("target_agent_id"):
        return False
    if hasattr(vm, "udid"):
        if (proof.get("name") or proof.get("sim_name")) != f"aiphone_sim_{vm.id}":
            return False
        kind = P.MSG_IOS_SIM_VM_DELETE
    elif hasattr(vm, "hdc_serial"):
        if proof.get("instance_name") != f"aiphone_harmony_{vm.id}":
            return False
        kind = P.MSG_HARMONY_VM_DELETE
    else:
        if proof.get("avd_name") != f"aiphone_vm_{vm.id}":
            return False
        kind = P.MSG_VM_DELETE
    payload = {"type": kind, "request_id": uuid.uuid4().hex[:16], "vm_id": vm.id}
    for key in ("adb_serial", "udid", "hdc_serial", "lease_token"):
        payload[key] = str(report.get(key) or proof.get(key) or "")
    sent = await send_operation_once(hub, reporter, payload, timeout_sec=CLEANUP_SEND_TIMEOUT_SEC)
    if sent:
        update_runtime(
            vm, operation={**op, "cleanup_sent": True, "cleanup_request_id": payload["request_id"]}
        )
    return sent


def is_abandoned_cleanup_receipt(vm, msg: dict) -> bool:
    op = runtime(vm).get("operation") or {}
    return bool(
        op.get("first_bind")
        and op.get("outcome") == "failed"
        and not op.get("pending")
        and op.get("cleanup_request_id")
        and msg.get("request_id") == op["cleanup_request_id"]
        and msg.get("ok")
        and msg.get("reason") in {"deleted", "not_found"}
        and (msg.get("details") or {}).get("cleanup_confirmed")
    )


def record_abandoned_cleanup_receipt(vm) -> None:
    data = runtime(vm)
    last = data.get("last_status") or {}
    details = {
        **(last.get("details") or data.get("details") or {}),
        "cleanup_confirmed": True,
        "instance_created": False,
        "instance_deleted": True,
    }
    update_runtime(vm, instance_created=False, last_status={**last, "details": details}, details=details)


def operation_deadline_remaining(operation: dict) -> float | None:
    value = str(operation.get("deadline_at") or "")
    if not value:
        return None  # upgrade compatibility for historical operation records
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0  # an invalid bound cannot leave an operation pending forever
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (deadline - datetime.now(timezone.utc)).total_seconds()


def operation_deadline_expired(vm) -> bool:
    op = runtime(vm).get("operation") or {}
    remaining = operation_deadline_remaining(op)
    return bool(op.get("pending") and remaining is not None and remaining <= 0)


def expire_operation_if_due(vm, hub) -> bool:
    """Call while holding the VM transaction lock before accepting a result."""
    if not operation_deadline_expired(vm):
        return False
    fail_attempt(vm, "operation_timeout", "本次操作超时，已结束；不会自动重试")
    maintenance(hub, vm, True)
    return True


async def send_operation_once(hub, target: str, payload: dict, *, timeout_sec: float | None = None) -> bool:
    """Bound one WebSocket delivery; failure never schedules a resend."""
    try:
        return bool(
            await asyncio.wait_for(
                hub.send_to_agent(target, payload),
                timeout=OPERATION_SEND_TIMEOUT_SEC if timeout_sec is None else timeout_sec,
            )
        )
    except Exception:
        return False


def require_retirement_allowed(hub, vm) -> None:
    require_idle(vm)
    owner = str(vm.assigned_agent_id or "")
    if owner and hub.has_agent(owner) and owner_host_matches(vm, hub):
        require_device_idle(hub, vm)
    # Offline retirement changes the ledger only. Do not wait on, or forcibly
    # release, stale execution locks or viewers belonging to that old host.


def record_creation_provenance(vm, msg: dict) -> None:
    op = runtime(vm).get("operation") or {}
    details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
    if not op.get("first_bind") or msg.get("request_id") != op.get("request_id"):
        return
    created = details.get("created_in_attempt")
    if not isinstance(created, bool):
        return
    if op.get("created_in_attempt") is False and created:
        return  # a late contradictory response cannot erase protected old data
    update_runtime(vm, operation={**op, "created_in_attempt": created})


def proves_live_managed_instance(vm_id: str, msg: dict, platform: str) -> bool:
    """A late completion can itself reconcile a retired ledger ID."""
    if msg.get("state") != "running" or not msg.get("ok", True):
        return False
    details = msg.get("details") if isinstance(msg.get("details"), dict) else {}
    if platform == "android":
        return details.get("avd_name") == f"aiphone_vm_{vm_id}"
    if platform == "ios":
        return details.get("sim_name") == f"aiphone_sim_{vm_id}" and bool(msg.get("udid"))
    return (
        details.get("instance_name") == f"aiphone_harmony_{vm_id}"
        and bool(msg.get("hdc_serial"))
        and bool(msg.get("lease_token"))
    )


def is_untouched_draft(vm) -> bool:
    return vm.state == "draft" and not vm.assigned_agent_id and not runtime(vm).get("operation")
