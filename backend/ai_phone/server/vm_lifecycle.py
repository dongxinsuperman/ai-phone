"""Small shared guards for the existing three VM lifecycle implementations.

The VM row remains the authority. Each native operation has one bounded attempt
in its existing runtime JSON; there is no retry or resume workflow.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import uuid
import weakref
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

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


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _last_details(vm) -> dict:
    data = runtime(vm)
    last = _mapping(data.get("last_status"))
    return _mapping(last.get("details")) or _mapping(data.get("details"))


def observation_identity_matches(vm, observation: dict) -> bool:
    if observation.get("vm_id") != vm.id:
        return False
    identity = _mapping(observation.get("identity"))
    if hasattr(vm, "udid"):
        return bool(identity.get("sim_name") == f"aiphone_sim_{vm.id}"
                    and identity.get("udid")
                    and (not vm.udid or identity["udid"] == vm.udid))
    if hasattr(vm, "hdc_serial"):
        name = "aiphone_harmony_" + re.sub(r"[^A-Za-z0-9_]+", "_", vm.id)[:32]
        return bool(identity.get("instance_name") == name and identity.get("instance_path")
                    and (not vm.lease_token or identity.get("lease_token") == vm.lease_token)
                    and (not vm.hdc_serial or identity.get("hdc_serial") == vm.hdc_serial))
    name = "aiphone_vm_" + re.sub(r"[^A-Za-z0-9_]+", "_", vm.id)[:32]
    return identity.get("avd_name") == name


def instance_observation(vm) -> dict:
    data = runtime(vm)
    observation = _mapping(data.get("instance_observation")) or _mapping(_last_details(vm).get("instance_observation"))
    if not observation:
        return {}
    if observation.get("vm_id") != vm.id:
        return {"vm_id": vm.id, "state": "unknown", "resources_released": False,
                "reason": "原生观察与当前配置不一致"}
    state = observation.get("state")
    if state not in {"running", "stopped", "missing", "unknown"}:
        return {"vm_id": vm.id, "state": "unknown", "resources_released": False,
                "reason": "原生观察未提供可确认的状态"}
    if state in {"running", "stopped"} and not observation_identity_matches(vm, observation):
        return {"vm_id": vm.id, "state": "unknown", "resources_released": False,
                "reason": "原实例身份与记录不一致，尚未核实当前状态"}
    return observation


def observation_notice(observation: dict) -> str:
    state, reason = observation.get("state"), str(observation.get("reason") or "")
    if state == "running":
        return "原实例仍在运行，未重复启动"
    if state == "missing":
        return "原实例配置缺失，未新建替代实例"
    if any(code in reason for code in ("definition_unverified", "definition_missing", "config_unreadable", "config_missing")):
        return "原实例配置文件缺失或不可读，本次启动未执行"
    if any(code in reason for code in ("lock_busy", "operation_in_progress", "operation_busy", "instance_busy")):
        return "当前本机操作尚未结束，本次启动未执行"
    if any(code in reason for code in ("transport_port_unavailable", "port_in_use", "port_busy")):
        return "实例已停止，但原端口仍被占用，本次启动未执行"
    if state == "stopped" and observation.get("resources_released") is not True:
        return reason if any("\u4e00" <= char <= "\u9fff" for char in reason) else "原实例已停止，资源释放尚未核实，本次启动未执行"
    if reason and any("\u4e00" <= char <= "\u9fff" for char in reason):
        return reason
    return "当前原实例状态尚未核实" + (f"：{reason}" if reason else "")


def record_failure(vm, message: str, *, reason: str = "", action: str = "", request_id: str = "", preflight: bool = False) -> None:
    if not message:
        return
    op = _mapping(runtime(vm).get("operation"))
    update_runtime(vm, last_failure={
        "message": str(message)[:4000], "reason": reason,
        "action": action or str(op.get("action") or ""),
        "request_id": request_id or str(op.get("request_id") or ""),
        "at": datetime.now(timezone.utc).isoformat(),
        **({"native_command_sent": False} if preflight else {}),
    })


def preserve_failure(vm) -> None:
    data = runtime(vm)
    if data.get("last_failure"):
        return
    if getattr(vm, "error_message", "") and (
        vm.state in {"error", "unavailable"} or _mapping(data.get("operation")).get("outcome") == "failed"
    ):
        record_failure(vm, vm.error_message, reason=str(getattr(vm, "error_code", "") or ""))


def record_status_facts(vm, msg: dict) -> None:
    """Accepted current evidence only; an old failure is not a native fact."""
    preserve_failure(vm)
    observation = _mapping(_mapping(msg.get("details")).get("instance_observation"))
    update_runtime(vm, instance_observation=observation or None)
    operation = _mapping(runtime(vm).get("operation"))
    if msg.get("ok") is False and (
        msg.get("request_id") or operation.get("pending")
        or (not runtime(vm).get("last_failure") and msg.get("error"))
    ):
        record_failure(vm, str(msg.get("error") or msg.get("reason") or "操作未成功"),
                       reason=str(msg.get("reason") or ""), request_id=str(msg.get("request_id") or ""))


def invalidate_instance_observation(vm, reason: str = "连接中断，当前原实例状态尚未核实") -> None:
    preserve_failure(vm)
    observation = {"vm_id": vm.id, "state": "unknown", "identity": {},
                   "resources_released": False, "reason": reason}
    details = {"instance_observation": observation, "cleanup_confirmed": False}
    update_runtime(vm, instance_observation=observation, details=details,
                   last_status={"state": "unknown", "ok": False, "reason": "observation_required", "details": details})


def record_stopped_inventory(vm, identity: dict) -> None:
    """A complete, exact native inventory proves power state, not free ports."""
    preserve_failure(vm)
    observation = {"vm_id": vm.id, "state": "stopped", "identity": dict(identity),
                   "resources_released": False, "reason": "本次完整原生库存确认实例已停止"}
    details = {"instance_observation": observation, "inventory_complete": True}
    update_runtime(vm, instance_observation=observation, details=details,
                   last_status={"state": "stopped", "ok": True, "reason": "reclaimed", "details": details})


def _resolve_machine_fact(vm):
    data, raw = runtime(vm), str(vm.state or "draft")
    op, last = _mapping(data.get("operation")), _mapping(data.get("last_status"))
    details = _last_details(vm)
    owner = str(vm.assigned_agent_id or "")
    pending = bool(op.get("pending"))
    transition = raw in {"starting", "stopping", "probing", "dispatching", "creating", "connecting_hdc", "checking_driver"}
    busy = pending or (transition and not op)
    observation = instance_observation(vm)
    native_state = observation.get("state")
    state, notice = None, ""
    if busy:
        state = "stopping" if op.get("action") == "stop" or raw == "stopping" else "starting"
    elif (not owner and not data.get("instance_created") and not details.get("instance_created")
          and (raw == "draft" or (not op and _mapping(data.get("last_failure")).get("native_command_sent") is False)
               or (op.get("first_bind") and op.get("outcome") == "failed"
               and details.get("instance_created") is False
               and (details.get("instance_deleted") is True or details.get("cleanup_confirmed") is True)))):
        state = "draft"
    elif native_state in {"running", "stopped"}:
        state = native_state
    elif native_state in {"missing", "unknown"}:
        notice = observation_notice(observation)
    elif raw == "running" and last.get("ok") is not False:
        state = "running"
    elif (raw == "stopped" and (not op or op.get("action") == "stop")
          and last.get("state") == "stopped" and last.get("ok") is True
          and details.get("cleanup_confirmed") is not False
          and (details.get("cleanup_confirmed") is True or details.get("inventory_complete") is True)):
        state = "stopped"
    return state, notice, busy, observation


def machine_state(vm):
    """Last proven machine phase, before connection loss masks current certainty."""
    return _resolve_machine_fact(vm)[0]


def require_editable(vm, hub, *, alias_only: bool) -> None:
    require_action(vm, "edit", hub)
    if not alias_only and machine_state(vm) == "running":
        raise HTTPException(409, operation_failure_detail(vm, "运行中只能修改别名，请先停止后修改配置", hub=hub))


def lifecycle_view(vm, hub=None) -> dict:
    data, raw = runtime(vm), str(vm.state or "draft")
    op, details = _mapping(data.get("operation")), _last_details(vm)
    owner = str(vm.assigned_agent_id or "")
    online = bool(owner and hub is not None and getattr(hub, "has_agent", lambda _id: False)(owner))
    state, notice, busy, observation = _resolve_machine_fact(vm)
    if owner and not online:
        if not busy:
            state = None
        notice = "所属 Agent 离线，当前机器状态无法确认"
    elif raw == "agent_offline" and not busy:
        state, notice = None, "Agent 已连接，原实例状态尚未核实"
    elif state is None and not notice:
        notice = "当前机器状态尚未核实；普通启动会先只读核验原实例"
    # A stopped inventory need not have inspected ports or this instance's
    # services. It is a machine fact, not a failed user operation. Actual
    # preflight/operation failures remain visible through last_failure.
    host_matches = hub is None or owner_host_matches(vm, hub)
    if owner and online and not host_matches:
        state, notice = None, "所属宿主与记录不一致，不能操作其他宿主的实例"
    protected_first_bind = bool(op.get("first_bind") and op.get("outcome") == "failed"
                                and op.get("created_in_attempt") is False and details.get("instance_created"))
    if protected_first_bind:
        notice = "本次未创建该宿主实例，不能自动接管已有数据"
    occupied = bool(owner and online and hub is not None and device_is_busy(hub, vm))
    mutable = not busy
    retire = mutable and state != "running" and not occupied
    actions = {
        "start": bool(mutable and owner and online and host_matches and not occupied
                      and state in {None, "stopped"} and not protected_first_bind),
        "stop": bool(mutable and owner and online and host_matches and not occupied
                     and state in {None, "running"}),
        "dispatch": bool(retire and not protected_first_bind and (owner or state == "draft")),
        "delete": bool(retire),
        "edit": bool(mutable),
        "copy": bool(mutable),
    }
    if occupied:
        notice = "设备正在执行任务、安装或观看，请结束后再操作"
    failure = data.get("last_failure")
    error = str(failure.get("message") or "") if isinstance(failure, dict) else str(failure or "")
    if not error and (raw in {"error", "unavailable"} or op.get("outcome") == "failed"):
        error = str(getattr(vm, "error_message", "") or "")
    return {"state": state, "agent_online": online, "actions": actions, "notice": notice, "error": error}


def vm_view(vm, hub=None) -> dict:
    return {**vm.to_dict(), "lifecycle": lifecycle_view(vm, hub)}


def require_action(vm, action: str, hub, *, idempotent_stop: bool = False) -> None:
    view = lifecycle_view(vm, hub)
    if view["actions"].get(action):
        return
    # The page need not offer Stop for a stopped guest, but existing API/CLI
    # callers retain one explicit idempotent native Stop and its real receipt.
    if (action == "stop" and idempotent_stop and view["state"] == "stopped"
            and vm.assigned_agent_id and view["agent_online"] and owner_host_matches(vm, hub)):
        require_device_idle(hub, vm)
        return
    message = view["notice"] or ("实例正在操作中，请等待本次结果" if view["state"] in {"starting", "stopping"}
                                 else "当前机器状态不允许该操作")
    if vm.assigned_agent_id and view["agent_online"] and device_is_busy(hub, vm):
        message = "device_busy: " + message
    raise HTTPException(409, operation_failure_detail(vm, message, hub=hub))


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
    if _mapping(runtime(vm).get("operation")).get("pending") or vm.state in blocked:
        raise HTTPException(409, "实例正在操作中，请等待本次结果")


def require_startable(vm, hub=None) -> None:
    view = lifecycle_view(vm, hub)
    if vm.assigned_agent_id:
        require_action(vm, "start", hub)
    elif view["state"] != "draft" or not view["actions"]["dispatch"]:
        raise HTTPException(409, operation_failure_detail(vm, view["notice"] or "原实例状态尚未核实，不能新建替代实例", hub=hub))


def start_needs_inspection(vm, hub) -> bool:
    if not vm.assigned_agent_id:
        return False
    view = lifecycle_view(vm, hub)
    if vm.state in {"error", "unavailable", "agent_offline"} or view["state"] is None:
        return True
    if hasattr(vm, "lease_token") and (vm.lease_token or vm.hdc_port is not None or vm.hdc_serial):
        return True
    observation = instance_observation(vm)
    return view["state"] == "stopped" and bool(observation) and observation.get("resources_released") is not True


def start_preflight_snapshot(vm) -> dict:
    """Only state used by this action; unrelated timestamps do not cause conflicts."""
    data = runtime(vm)
    config = vm.to_dict()
    runtime_field = "runtime_state" if hasattr(vm, "runtime_state") else "runtime"
    for field in (runtime_field, "error_message", "error_code", "created_at", "updated_at", "started_at", "stopped_at"):
        config.pop(field, None)
    return copy.deepcopy({"config": config, "operation": data.get("operation"),
                          "owner_host_id": data.get("owner_host_id"),
                          "lease_token": getattr(vm, "lease_token", None)})


async def reject_start_preflight(session, vm, hub, message: str, *, reason: str = "instance_unverified", request_id: str = "") -> None:
    record_failure(vm, message, reason=reason, action="start", request_id=request_id, preflight=True)
    vm.error_message = message
    if hasattr(vm, "error_code"):
        vm.error_code = reason
    await session.commit()
    raise HTTPException(409, operation_failure_detail(vm, message, request_id=request_id, hub=hub, reason=reason))


async def ensure_start_observation(session, vm, hub, waiter):
    """Inspect unknown bound instances outside the VM lock, then fence its use."""
    if not start_needs_inspection(vm, hub):
        return vm
    await transaction_lock(session, operation_lock_key(vm))
    await session.refresh(vm)
    require_startable(vm, hub)
    require_owner_host(vm, hub)
    if not start_needs_inspection(vm, hub):
        return vm
    expected = start_preflight_snapshot(vm)
    vm_id, model, agent_id = vm.id, type(vm), str(vm.assigned_agent_id)
    probe_vm = SimpleNamespace(**{column.key: copy.deepcopy(getattr(vm, column.key)) for column in vm.__table__.columns})
    await session.commit()  # release both the read connection and per-VM lock
    try:
        response = await waiter.inspect_agent(hub=hub, vm=probe_vm, agent_id=agent_id, timeout_sec=10.0)
    except Exception as exc:
        response = {"ok": False, "reason": f"无法读取原实例状态：{exc}", "details": {}}
    await transaction_lock(session, operation_lock_key(vm))
    current = await session.get(model, vm_id, populate_existing=True)
    if current is None:
        raise HTTPException(404, "实例已删除")
    if start_preflight_snapshot(current) != expected:
        raise HTTPException(409, operation_failure_detail(current, "核验期间实例配置、归属或操作已变化，本次未启动", hub=hub))
    if not hub.has_agent(agent_id) or not owner_host_matches(current, hub):
        invalidate_instance_observation(current)
        await reject_start_preflight(session, current, hub, "所属 Agent 连接已变化，本次启动未执行", reason="agent_unavailable")
    observation = _mapping(_mapping(response.get("details")).get("instance_observation"))
    if not observation or observation.get("vm_id") != current.id:
        observation = {"vm_id": current.id, "state": "unknown", "identity": {}, "resources_released": False,
                       "reason": str(response.get("reason") or "Agent 未返回可核实的原实例观察")}
    preserve_failure(current)
    update_runtime(current, instance_observation=observation,
                   last_status={"state": observation.get("state"), "ok": True, "reason": "inspected",
                                "details": {"instance_observation": observation}})
    observation = instance_observation(current)
    observed_state = observation.get("state")
    if observed_state in {"running", "stopped"}:
        current.state = observed_state
        update_runtime(current, instance_created=True)
    # Stopped is the common power-state prerequisite. Android rechecks its
    # chosen port locally; iOS can clean only this instance's own WDA before
    # booting the same UDID. Only Harmony's retained lease requires positive
    # release proof before Server can allocate another lease.
    if observed_state != "stopped":
        await reject_start_preflight(session, current, hub, observation_notice(observation),
            reason=str(observation.get("reason") or f"instance_{observed_state or 'unknown'}"),
            request_id=str(response.get("request_id") or ""))
    if hasattr(current, "lease_token") and (current.lease_token or current.hdc_port is not None or current.hdc_serial):
        from .harmony_vm.models import HarmonyVmPortLease
        from .harmony_vm.service import release_port_lease
        lease = await session.get(HarmonyVmPortLease, current.hdc_port) if current.hdc_port is not None else None
        identity = _mapping(observation.get("identity"))
        if observation.get("resources_released") is not True:
            await reject_start_preflight(session, current, hub, observation_notice(observation),
                reason=str(observation.get("reason") or "lease_release_unverified"),
                request_id=str(response.get("request_id") or ""))
        if (lease is None or not current.lease_token or lease.vm_id != current.id
                or lease.lease_token != current.lease_token
                or identity.get("lease_token") != current.lease_token
                or identity.get("hdc_serial") != current.hdc_serial
                or not observation_identity_matches(current, observation)):
            await reject_start_preflight(session, current, hub, "原实例的资源归属尚未对齐，本次启动未执行", reason="lease_identity_unverified")
        await release_port_lease(session, current, reason="verified_stopped_observation")
    maintenance(hub, current, True)
    return current  # caller retains the lock through its one start reservation


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
    vm.error_message = ""
    if hasattr(vm, "error_code"):
        vm.error_code = ""
    update_runtime(
        vm,
        last_failure=None,
        instance_observation=None,
        last_status={},
        details={},
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


def accept_observation_fact(vm, msg: dict) -> bool:
    """Fresh owner observations may update facts, never revive a failed attempt."""
    op = _mapping(runtime(vm).get("operation"))
    if not vm.assigned_agent_id or op.get("pending"):
        return False
    request_id, observed = str(msg.get("request_id") or ""), str(msg.get("observed_request_id") or "")
    current = str(op.get("request_id") or "")
    if (request_id and request_id != current) or (observed and current and observed != current):
        return False
    observation = _mapping(_mapping(msg.get("details")).get("instance_observation"))
    if observation.get("vm_id") != vm.id:
        return False
    state = observation.get("state")
    if state in {"running", "stopped"}:
        return observation_identity_matches(vm, observation)
    return state in {"unknown", "missing"}


def finish_operation(vm, msg: dict) -> None:
    op = runtime(vm).get("operation") or {}
    if not op.get("pending"):
        return  # a health observation must not rewrite a completed attempt
    if msg.get("state") in {"running", "stopped", "error", "unavailable"}:
        failed = msg.get("state") in {"error", "unavailable"} or msg.get("ok") is False
        if failed:
            record_failure(vm, str(msg.get("error") or msg.get("reason")
                                   or getattr(vm, "error_message", "") or "本次操作未成功"),
                           reason=str(msg.get("reason") or getattr(vm, "error_code", "") or ""))
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


def device_is_busy(hub, vm) -> bool:
    from .lockstore import get_default_lock_store

    store = getattr(hub, "vm_lock_store", None) or get_default_lock_store()
    serial = (
        f"harmony-vm:{vm.id}"
        if hasattr(vm, "hdc_serial")
        else str(getattr(vm, "udid", None) or getattr(vm, "adb_serial", None) or "")
    )
    subscribers = getattr(hub, "subscriber_count", None)
    return bool(serial and (store.peek(serial) is not None or (subscribers and subscribers(serial))))


def require_device_idle(hub, vm) -> None:
    if device_is_busy(hub, vm):
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


async def report_owner_conflict(hub, vm, reporter: str, proof: dict, report: dict | None = None) -> bool:
    """Preserve both the authoritative ledger and a possibly unique native VM.

    Different host-id files do not prove different hardware or separate native
    data. Only an explicitly retired ledger ID authorizes orphan deletion.
    """
    from loguru import logger
    logger.warning(
        "VM ownership conflict; no adoption or cleanup | vm_id={} owner={} owner_host={} reporter={} reporter_host={}",
        vm.id, vm.assigned_agent_id or "", runtime(vm).get("owner_host_id") or "",
        reporter, host_id(hub, reporter),
    )
    return False


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
    # Failure/timeout supplies no fresh native proof. In particular it must
    # not carry a prior operation's exit_evidence or observation forward.
    details = {"cleanup_confirmed": False}
    update_runtime(
        vm, last_status={"state": "error", "ok": False, "reason": reason, "details": details},
        details=details, instance_observation=None,
    )
    record_failure(vm, message, reason=reason)
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
    require_action(vm, "delete", hub)
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


def operation_failure_detail(vm, message: str, *, request_id: str | None = None, hub=None, **extra) -> dict:
    """Keep actionable identity/evidence when an HTTP operation fails."""
    op = runtime(vm).get("operation") or {}
    return {"message": message, "sent": False, "vm_id": vm.id,
            "request_id": str(op.get("request_id") or "") if request_id is None else request_id,
            "instance": vm_view(vm, hub), **extra}
