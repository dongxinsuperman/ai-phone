"""Recovery boundaries for an offline Harmony VM that still owns an HDC port."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ai_phone.agent.harmony_vm.capability import HarmonyVmTools
from ai_phone.agent.harmony_vm.manager import HarmonyVmManager, HarmonyVmRuntime
from ai_phone.agent.harmony_vm.registry import (
    register_managed_serial,
    register_managed_vm,
    unregister_managed_serial,
    unregister_managed_vm,
)
from ai_phone.server.harmony_vm import api as harmony_api
from ai_phone.server.harmony_vm.models import HarmonyVmInstance, HarmonyVmPortLease
from ai_phone.server.harmony_vm.service import (
    allocate_port_lease,
    handle_vm_reconcile,
    handle_vm_status,
)
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.shared import protocol as P


AUTH = {"Authorization": "Bearer dev"}
REASON = "Operator verified that the old host instance is stopped"


class _RecordingHub(Hub):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.online = {"new-agent"}

    def has_agent(self, agent_id: str) -> bool:
        return agent_id in self.online

    async def send_to_agent(self, agent_id: str, payload: dict[str, Any]) -> bool:
        self.messages.append((agent_id, payload))
        return self.has_agent(agent_id)


class _ReadyProbe:
    async def probe_agent(self, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    def excluded_ports_for(self, _agent_id: str) -> set[int]:
        return set()


class _FakeCrashProcess:
    def __init__(self, pid: int, args: list[str]) -> None:
        self.pid = pid
        self.info = {"cmdline": args}
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: int) -> int:
        return 0


async def _vm_with_lease(
    session: Any, *, vm_id: str, state: str, agent_id: str
) -> tuple[int, str]:
    vm = HarmonyVmInstance(
        id=vm_id,
        name=vm_id,
        alias=vm_id,
        os_version="6.0.0",
        api_version="20",
        state=state,
        assigned_agent_id=agent_id,
    )
    session.add(vm)
    await session.flush()
    lease = await allocate_port_lease(session, vm, agent_id=agent_id)
    await session.commit()
    return lease.port, lease.lease_token


@pytest.mark.parametrize("start_ok", [True, False])
async def test_offline_vm_switch_creates_new_id_before_old_orphan_cleanup(
    client, app, session, monkeypatch, start_ok
):
    """Switch is delete plus one fresh start; old reports never revive the ledger."""
    old_port, old_token = await _vm_with_lease(
        session, vm_id="offline-vm", state="agent_offline", agent_id="old-agent",
    )
    other_port, other_token = await _vm_with_lease(
        session, vm_id="running-vm", state="running", agent_id="other-agent",
    )
    hub = _RecordingHub()
    app.state.hub = hub
    monkeypatch.setattr(harmony_api, "get_capability_waiter", lambda: _ReadyProbe())
    old_identity = "harmony-vm:offline-vm"
    other_identity = "harmony-vm:running-vm"
    await hub.register_agent("old-agent", "Old", "macos", object())
    await hub.register_agent("other-agent", "Other", "macos", object())
    await hub.attach_device("old-agent", old_identity)
    await hub.attach_device("other-agent", other_identity)
    hub.set_device_extra(old_identity, {"owner": "old"})
    hub.set_device_readiness(old_identity, {"ready": True})
    hub.set_device_extra(other_identity, {"owner": "other"})
    hub.set_device_readiness(other_identity, {"ready": True})
    session.add_all([
        Device(serial=old_identity, agent_id="old-agent", platform="harmony"),
        Device(serial=other_identity, agent_id="other-agent", platform="harmony"),
    ])
    await session.commit()

    # Manual confirmation is still not evidence that a bound VM stopped.
    for body in (
        {"confirmed": False, "reason": REASON},
        {"confirmed": True, "reason": "        "},
        {"confirmed": True, "reason": REASON},
    ):
        rejected = await client.post(
            "/api/internal/harmony-vm/instances/offline-vm/force-release",
            headers=AUTH, json=body,
        )
        assert rejected.status_code == 409, rejected.text
    session.expire_all()
    old = await session.get(HarmonyVmInstance, "offline-vm")
    assert (old.state, old.assigned_agent_id, old.lease_token) == (
        "agent_offline", "old-agent", old_token,
    )
    assert "lease_token" not in old.to_dict()
    assert (await session.get(HarmonyVmPortLease, old_port)).lease_token == old_token
    assert hub.agent_id_for_serial(old_identity) == "old-agent"
    assert hub.messages == []
    active = await client.post(
        "/api/internal/harmony-vm/instances/running-vm/force-release",
        headers=AUTH, json={"confirmed": True, "reason": REASON},
    )
    assert active.status_code == 409

    switched = await client.post(
        "/api/internal/harmony-vm/instances/offline-vm/dispatch",
        headers=AUTH, json={"agent_id": "new-agent"},
    )
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert body["switched"] is True and body["sent"] is True
    assert body["old_vm_id"] == "offline-vm"
    replacement_id = body["instance"]["id"]
    assert replacement_id != "offline-vm"
    assert body["instance"]["state"] == "starting"
    assert body["instance"]["assigned_agent_id"] is None
    session.expire_all()
    assert await session.get(HarmonyVmInstance, "offline-vm") is None
    assert await session.get(Device, old_identity) is None
    assert hub.agent_id_for_serial(old_identity) is None
    assert hub.get_device_extra(old_identity) == {}
    assert await session.get(Device, other_identity) is not None
    assert hub.agent_id_for_serial(other_identity) == "other-agent"
    assert hub.get_device_extra(other_identity) == {
        "owner": "other", "readiness": {"ready": True},
    }
    recovered = await session.get(HarmonyVmInstance, replacement_id)
    assert recovered.assigned_agent_id is None
    assert recovered.hdc_port == old_port and recovered.lease_token != old_token
    assert recovered.runtime["operation"]["target_agent_id"] == "new-agent"
    assert recovered.runtime["operation"]["first_bind"] is True
    assert recovered.runtime["operation"]["pending"] is True
    assert (await session.get(HarmonyVmPortLease, old_port)).vm_id == replacement_id
    starts = [item for item in hub.messages if item[1]["type"] == P.MSG_HARMONY_VM_START]
    assert len(starts) == 1
    agent_id, payload = starts[0]
    assert agent_id == "new-agent" and payload["vm_id"] == replacement_id
    assert payload["assigned_port"] == old_port
    assert payload["lease_token"] == recovered.lease_token
    assert payload["require_existing_instance"] is False

    # The former offline host later reports its deleted VM. It receives only
    # cleanup for that old ID; its reply cannot continue or repeat the new start.
    hub.online.add("old-agent")
    await handle_vm_reconcile(
        "old-agent",
        {"instances": [{"vm_id": "offline-vm", "state": "stopped",
                        "lease_token": old_token, "hdc_serial": f"127.0.0.1:{old_port}"}],
         "inventory_complete": True},
        hub,
    )
    target, cleanup = hub.messages[-1]
    assert target == "old-agent" and cleanup["type"] == P.MSG_HARMONY_VM_DELETE
    assert cleanup["vm_id"] == "offline-vm" and cleanup["lease_token"] == old_token
    await handle_vm_status(
        "old-agent",
        {"vm_id": "offline-vm", "request_id": cleanup["request_id"],
         "lease_token": old_token, "state": "stopped", "ok": True,
         "reason": "deleted", "details": {"cleanup_confirmed": True}},
        hub,
    )
    session.expire_all()
    recovered = await session.get(HarmonyVmInstance, replacement_id)
    assert recovered.state == "starting" and recovered.assigned_agent_id is None
    assert (await session.get(HarmonyVmPortLease, old_port)).vm_id == replacement_id
    assert len([item for item in hub.messages if item[1]["type"] == P.MSG_HARMONY_VM_START]) == 1

    await handle_vm_status(
        "new-agent",
        {"vm_id": replacement_id, "request_id": payload["request_id"],
         "lease_token": payload["lease_token"], "state": "running" if start_ok else "error",
         "ok": start_ok, "reason": "started" if start_ok else "boot_failed",
         "hdc_serial": f"127.0.0.1:{old_port}",
         "details": {} if start_ok else {"cleanup_confirmed": True}},
        hub,
    )
    session.expire_all()
    recovered = await session.get(HarmonyVmInstance, replacement_id)
    running = await session.get(HarmonyVmInstance, "running-vm")
    assert recovered.state == ("running" if start_ok else "error")
    assert recovered.assigned_agent_id == ("new-agent" if start_ok else None)
    assert recovered.runtime["operation"]["pending"] is False
    assert len([item for item in hub.messages if item[1]["type"] == P.MSG_HARMONY_VM_START]) == 1
    assert await session.get(HarmonyVmInstance, "offline-vm") is None
    assert (running.state, running.hdc_port, running.lease_token) == (
        "running", other_port, other_token,
    )
    assert (await session.get(HarmonyVmPortLease, other_port)).lease_token == other_token


async def test_failed_first_binding_cannot_be_revived_by_late_status_or_inventory(
    client, app, session, monkeypatch
):
    hub = _RecordingHub()
    app.state.hub = hub
    monkeypatch.setattr(harmony_api, "get_capability_waiter", lambda: _ReadyProbe())
    vm = HarmonyVmInstance(name="single-attempt", alias="single-attempt")
    session.add(vm)
    await session.commit()
    vm_id = vm.id
    response = await client.post(
        f"/api/internal/harmony-vm/instances/{vm_id}/dispatch",
        headers=AUTH, json={"agent_id": "new-agent"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["instance"]["assigned_agent_id"] is None
    _, start = next(item for item in hub.messages if item[1]["type"] == P.MSG_HARMONY_VM_START)
    report = {
        "vm_id": vm_id, "request_id": start["request_id"],
        "lease_token": start["lease_token"], "hdc_serial": f"127.0.0.1:{start['assigned_port']}",
    }
    await handle_vm_status(
        "new-agent",
        {**report, "request_id": "different-attempt", "state": "running", "ok": True},
        hub,
    )
    session.expire_all()
    pending = await session.get(HarmonyVmInstance, vm_id)
    assert pending.state == "starting" and pending.assigned_agent_id is None

    await handle_vm_status(
        "new-agent",
        {**report, "state": "error", "ok": False, "reason": "boot_failed",
         "details": {
             "created_in_attempt": True, "instance_deleted": True,
             "instance_created": False, "cleanup_confirmed": True, "rollback_new_instance": True,
             "instance_observation": {
                 "vm_id": vm_id, "state": "missing", "resources_released": True,
                 "reason": "new_instance_rollback_confirmed",
                 "identity": {
                     "instance_name": f"aiphone_harmony_{vm_id}",
                     "instance_path": "/fixture/harmony/instances",
                     "lease_token": start["lease_token"],
                     "hdc_serial": report["hdc_serial"],
                 },
             },
         }},
        hub,
    )
    session.expire_all()
    failed = await session.get(HarmonyVmInstance, vm_id)
    assert failed.state == "error" and failed.assigned_agent_id is None
    assert failed.runtime["operation"]["pending"] is False
    assert failed.runtime["operation"]["outcome"] == "failed"
    assert await session.get(HarmonyVmPortLease, start["assigned_port"]) is None

    late = {**report, "state": "running", "ok": True,
            "details": {"instance_name": f"aiphone_harmony_{vm_id}", "managed_instance_present": True}}
    await handle_vm_status("new-agent", late, hub)
    await handle_vm_reconcile(
        "new-agent", {"instances": [late], "inventory_complete": True}, hub
    )
    session.expire_all()
    failed = await session.get(HarmonyVmInstance, vm_id)
    assert failed.state == "error" and failed.assigned_agent_id is None
    assert failed.runtime["operation"]["outcome"] == "failed"
    assert len([item for item in hub.messages if item[1]["type"] == P.MSG_HARMONY_VM_START]) == 1


async def test_start_on_offline_agent_preserves_old_lease(client, session):
    port, token = await _vm_with_lease(
        session,
        vm_id="old-agent-offline",
        state="agent_offline",
        agent_id="old-agent",
    )

    response = await client.post(
        "/api/internal/harmony-vm/instances/old-agent-offline/start",
        headers=AUTH,
    )

    assert response.status_code == 409
    session.expire_all()
    vm = await session.get(HarmonyVmInstance, "old-agent-offline")
    assert vm is not None
    assert (vm.state, vm.assigned_agent_id, vm.hdc_port, vm.lease_token) == (
        "agent_offline",
        "old-agent",
        port,
        token,
    )
    assert (await session.get(HarmonyVmPortLease, port)).lease_token == token


async def test_same_agent_cannot_redispatch_with_unconfirmed_old_lease(
    client, app, session, monkeypatch
):
    port, token = await _vm_with_lease(
        session,
        vm_id="old-lease-same-agent",
        state="error",
        agent_id="new-agent",
    )
    hub = _RecordingHub()
    app.state.hub = hub
    monkeypatch.setattr(harmony_api, "get_capability_waiter", lambda: _ReadyProbe())

    response = await client.post(
        "/api/internal/harmony-vm/instances/old-lease-same-agent/dispatch",
        headers=AUTH,
        json={"agent_id": "new-agent"},
    )

    assert response.status_code == 409
    assert hub.messages == []
    session.expire_all()
    vm = await session.get(HarmonyVmInstance, "old-lease-same-agent")
    assert vm is not None
    assert (vm.state, vm.hdc_port, vm.lease_token) == ("error", port, token)
    assert (await session.get(HarmonyVmPortLease, port)).lease_token == token


async def test_stop_without_assigned_agent_keeps_unconfirmed_lease(client, session):
    port, token = await _vm_with_lease(
        session,
        vm_id="lost-assignment",
        state="error",
        agent_id="old-agent",
    )
    vm = await session.get(HarmonyVmInstance, "lost-assignment")
    assert vm is not None
    vm.assigned_agent_id = None
    await session.commit()

    response = await client.post(
        "/api/internal/harmony-vm/instances/lost-assignment/stop",
        headers=AUTH,
    )

    assert response.status_code == 409
    session.expire_all()
    vm = await session.get(HarmonyVmInstance, "lost-assignment")
    assert vm is not None
    assert (vm.state, vm.hdc_port, vm.lease_token) == ("error", port, token)
    assert (await session.get(HarmonyVmPortLease, port)).lease_token == token


def test_agent_stop_retry_never_confirms_unverified_old_port_or_touches_new_vm(
    monkeypatch, tmp_path
):
    import ai_phone.agent.harmony_vm.manager as manager_module

    serial = "127.0.0.1:10007"
    vm_id = "old-vm"
    old_token = "old-lease"
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id=vm_id,
        name=vm_id,
        instance_name="aiphone_harmony_old_vm",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10007,
        hdc_serial=serial,
        lease_token=old_token,
        ready=True,
    )
    manager._runtimes[vm_id] = runtime  # noqa: SLF001
    manager._known[vm_id] = {**runtime.persistent_dict(), "created": True}  # noqa: SLF001
    register_managed_vm(vm_id, serial, old_token)
    register_managed_serial(serial)

    calls: list[tuple[str, tuple[Any, ...]]] = []
    confirmations = iter([(False, "unconfirmed"), (False, "unconfirmed"), (True, "removed")])
    native = {"stops": 0, "running": True}

    def stop_emulator(args, **_kwargs):
        assert args[1:] == ["-stop", runtime.instance_name]
        calls.append(("emulator", tuple(args)))
        native["stops"] += 1
        # The first two explicit attempts do not exit. Only the last native
        # stop actually removes the instance, allowing a positive exit proof.
        native["running"] = native["stops"] < 3
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def active_instance(*_args):
        return serial if native["running"] else ""

    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (HarmonyVmTools(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        stop_emulator,
    )
    monkeypatch.setattr(
        manager_module,
        "hdc_run",
        lambda *args, **_kwargs: calls.append(("hdc", args)) or "",
    )
    monkeypatch.setattr(manager, "_wait_removed", lambda *_args: next(confirmations))
    monkeypatch.setattr(manager, "_active_instance_serial", active_instance)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))

    try:
        first = manager.stop_sync(vm_id, serial, old_token)
        assert first["ok"] is False
        assert first["details"]["cleanup_confirmed"] is False
        assert manager._known[vm_id]["cleanup_pending"] is True  # noqa: SLF001

        # A process restart must not turn ready=False into a false cleanup ack.
        manager = HarmonyVmManager(runtime_dir=tmp_path)
        monkeypatch.setattr(manager, "_wait_removed", lambda *_args: next(confirmations))
        monkeypatch.setattr(manager, "_active_instance_serial", active_instance)
        monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
        assert manager._known[vm_id]["cleanup_pending"] is True  # noqa: SLF001

        second = manager.stop_sync(vm_id, serial, old_token)
        assert second["ok"] is False
        assert second["details"]["cleanup_confirmed"] is False
        assert manager._known[vm_id]["cleanup_pending"] is True  # noqa: SLF001

        register_managed_vm("new-vm", serial, "new-lease")
        calls_before_old_retry = list(calls)
        blocked = manager.stop_sync(vm_id, serial, old_token)
        assert blocked["ok"] is False
        assert blocked["reason"] == "hdc_owned_by_another_vm"
        assert calls == calls_before_old_retry
        assert manager._known[vm_id]["cleanup_pending"] is True  # noqa: SLF001

        unregister_managed_vm("new-vm", "new-lease")
        confirmed = manager.stop_sync(vm_id, serial, old_token)
        assert confirmed["ok"] is True
        assert confirmed["details"]["cleanup_confirmed"] is True
        assert confirmed["details"]["exit_evidence"]["instance_process_status"] == "absent"
        assert native == {"stops": 3, "running": False}
        assert manager._known[vm_id]["cleanup_pending"] is False  # noqa: SLF001
    finally:
        unregister_managed_vm("new-vm", "new-lease")
        unregister_managed_vm(vm_id, old_token)
        unregister_managed_serial(serial)


def test_stop_only_terminates_crash_service_with_exact_vm_socket_and_data_dir(
    monkeypatch, tmp_path
):
    import psutil
    import ai_phone.agent.harmony_vm.manager as manager_module

    vm_id = "vm-a"
    instance_name = "aiphone_harmony_vm_a"
    instance_root = tmp_path / "instances"
    instance_dir = instance_root / instance_name
    exe = "/Applications/DevEco/Emulator.app/Contents/MacOS/emulator-crash-service"
    own = _FakeCrashProcess(
        101,
        [exe, "-socket", f"{instance_name}_crash", "-data-dir", str(instance_dir)],
    )
    own_equals = _FakeCrashProcess(
        102,
        [exe, f"-socket={instance_name}_crash", f"-data-dir={instance_dir}"],
    )
    wrong_socket = _FakeCrashProcess(
        103,
        [exe, "-socket", "aiphone_harmony_vm_b_crash", "-data-dir", str(instance_dir)],
    )
    wrong_dir = _FakeCrashProcess(
        104,
        [exe, "-socket", f"{instance_name}_crash", "-data-dir", str(instance_root / "aiphone_harmony_vm_b")],
    )
    nested_dir = _FakeCrashProcess(
        105,
        [exe, "-socket", f"{instance_name}_crash", "-data-dir", str(instance_dir / "deployed")],
    )
    wrong_program = _FakeCrashProcess(
        106,
        ["/bin/other-process", "-socket", f"{instance_name}_crash", "-data-dir", str(instance_dir)],
    )
    processes = [own, own_equals, wrong_socket, wrong_dir, nested_dir, wrong_program]
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: processes)

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id=vm_id,
        name=vm_id,
        instance_name=instance_name,
        instance_path=str(instance_root),
        image_root="",
        hdc_port=10007,
        hdc_serial="127.0.0.1:10007",
        lease_token="lease-a",
        ready=True,
    )
    manager._runtimes[vm_id] = runtime  # noqa: SLF001
    manager._known[vm_id] = runtime.persistent_dict()  # noqa: SLF001
    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (HarmonyVmTools(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(manager_module, "hdc_run", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(manager, "_wait_removed", lambda *_args: (True, "removed"))

    result = manager.stop_sync(vm_id, runtime.hdc_serial, runtime.lease_token)

    assert result["ok"] is True
    assert result["details"]["crash_service_cleanup_confirmed"] is True
    assert [proc.pid for proc in processes if proc.terminated] == [101, 102]


def test_delete_rechecks_crash_service_that_outlives_stop(monkeypatch, tmp_path):
    import psutil
    import ai_phone.agent.harmony_vm.manager as manager_module

    vm_id = "vm-b"
    instance_name = "aiphone_harmony_vm_b"
    instance_root = tmp_path / "instances"
    orphan = _FakeCrashProcess(
        201,
        [
            "/Applications/DevEco/Emulator.app/Contents/MacOS/emulator-crash-service",
            "-socket",
            f"{instance_name}_crash",
            "-data-dir",
            str(instance_root / instance_name),
            "-ppid",
            "1",
        ],
    )
    # Explicit stop first proves that the exact Emulator is absent; the two
    # later scans belong to crash-helper cleanup before and after deletion.
    scans = iter([[], [], [orphan]])
    events = []

    def process_scan(_attrs):
        events.append("process_scan")
        return next(scans)

    def delete_emulator(args, **_kwargs):
        assert args[1] == "-delete" and args[2] == instance_name
        events.append("delete")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(psutil, "process_iter", process_scan)
    monkeypatch.setattr(manager_module, "hdc_list_targets", lambda: events.append("hdc_query") or [])
    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (HarmonyVmTools(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        delete_emulator,
    )
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    manager._known[vm_id] = {  # noqa: SLF001
        "vm_id": vm_id,
        "instance_name": instance_name,
        "instance_path": str(instance_root),
        "ready": False,
        "cleanup_confirmed": True,
    }

    result = manager.delete_sync(vm_id)

    assert result["ok"] is True
    assert result["details"]["crash_service_cleanup_confirmed"] is True
    assert orphan.terminated is True
    assert events == ["process_scan", "hdc_query", "process_scan", "delete", "process_scan"]


def test_crash_service_scan_failure_does_not_block_confirmed_hdc_stop(
    monkeypatch, tmp_path
):
    import psutil
    import ai_phone.agent.harmony_vm.manager as manager_module

    def scan_unavailable(_attrs):
        raise PermissionError("process list unavailable")

    monkeypatch.setattr(psutil, "process_iter", scan_unavailable)
    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (HarmonyVmTools(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(manager_module, "hdc_run", lambda *_args, **_kwargs: "")
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = HarmonyVmRuntime(
        vm_id="vm-c",
        name="vm-c",
        instance_name="aiphone_harmony_vm_c",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10007,
        hdc_serial="127.0.0.1:10007",
        lease_token="lease-c",
        # This case is about an optional crash-helper query failing after the
        # VM itself has independently exited, not an unverified reclaimed VM.
        process=SimpleNamespace(poll=lambda: 0),
    )
    manager._runtimes[runtime.vm_id] = runtime  # noqa: SLF001
    monkeypatch.setattr(manager, "_wait_removed", lambda *_args: (True, "removed"))

    result = manager.stop_sync(runtime.vm_id)

    assert result["ok"] is True
    assert result["details"]["cleanup_confirmed"] is True
    assert result["details"]["crash_service_cleanup_confirmed"] is False
    assert result["details"]["exit_evidence"]["process_return_code"] == 0


async def test_stale_port_lease_row_cannot_be_reused_by_new_dispatch(session):
    vm = HarmonyVmInstance(
        id="stale-lease",
        name="stale-lease",
        alias="stale-lease",
        state="stopped",
    )
    session.add(vm)
    await session.flush()
    session.add(HarmonyVmPortLease(
        port=10007,
        vm_id=vm.id,
        agent_id="old-agent",
        lease_token="old-token",
        state="reserved",
    ))
    await session.commit()

    with pytest.raises(RuntimeError, match="previous_port_lease_not_released"):
        await allocate_port_lease(session, vm, agent_id="new-agent")
    assert vm.hdc_port is None
    assert vm.lease_token is None
