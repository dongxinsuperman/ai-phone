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
from ai_phone.server.harmony_vm.service import allocate_port_lease
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device
from ai_phone.shared import protocol as P


AUTH = {"Authorization": "Bearer dev"}
REASON = "Operator verified that the old host instance is stopped"


class _RecordingHub(Hub):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def has_agent(self, agent_id: str) -> bool:
        return agent_id == "new-agent"

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


async def test_offline_vm_requires_confirmed_release_before_new_dispatch(
    client, app, session, monkeypatch
):
    """A stuck old lease must not silently disappear or block recovery forever."""
    old_port, old_token = await _vm_with_lease(
        session,
        vm_id="offline-vm",
        state="agent_offline",
        agent_id="old-agent",
    )
    other_port, other_token = await _vm_with_lease(
        session,
        vm_id="running-vm",
        state="running",
        agent_id="other-agent",
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
    session.add_all(
        [
            Device(serial=old_identity, agent_id="old-agent", platform="harmony"),
            Device(serial=other_identity, agent_id="other-agent", platform="harmony"),
        ]
    )
    await session.commit()

    # Reproduce the UI's "stop it first" conflict: the old Agent is gone, so
    # dispatch cannot switch owners while its lease is still held.
    blocked = await client.post(
        "/api/internal/harmony-vm/instances/offline-vm/dispatch",
        headers=AUTH,
        json={"agent_id": "new-agent"},
    )
    assert blocked.status_code == 409
    assert hub.messages == []

    unconfirmed = await client.post(
        "/api/internal/harmony-vm/instances/offline-vm/force-release",
        headers=AUTH,
        json={"confirmed": False, "reason": REASON},
    )
    assert unconfirmed.status_code == 400
    blank_reason = await client.post(
        "/api/internal/harmony-vm/instances/offline-vm/force-release",
        headers=AUTH,
        json={"confirmed": True, "reason": "        "},
    )
    assert blank_reason.status_code == 400
    session.expire_all()
    old = await session.get(HarmonyVmInstance, "offline-vm")
    assert old is not None
    assert old.to_dict()["cleanup_pending"] is True
    assert "lease_token" not in old.to_dict()
    assert (old.state, old.assigned_agent_id, old.lease_token) == (
        "agent_offline",
        "old-agent",
        old_token,
    )
    assert await session.get(HarmonyVmPortLease, old_port) is not None

    # Even explicit confirmation must not free a VM that is still active.
    active = await client.post(
        "/api/internal/harmony-vm/instances/running-vm/force-release",
        headers=AUTH,
        json={"confirmed": True, "reason": REASON},
    )
    assert active.status_code == 409
    session.expire_all()
    running = await session.get(HarmonyVmInstance, "running-vm")
    assert running is not None
    assert (running.state, running.lease_token) == ("running", other_token)

    released = await client.post(
        "/api/internal/harmony-vm/instances/offline-vm/force-release",
        headers=AUTH,
        json={"confirmed": True, "reason": REASON},
    )
    assert released.status_code == 200, released.text
    assert released.json()["instance"]["state"] == "stopped"
    assert released.json()["instance"]["cleanup_pending"] is False
    session.expire_all()
    assert await session.get(HarmonyVmPortLease, old_port) is None
    assert (await session.get(HarmonyVmPortLease, other_port)).lease_token == other_token
    assert await session.get(Device, old_identity) is None
    assert await session.get(Device, other_identity) is not None
    assert hub.agent_id_for_serial(old_identity) is None
    assert hub.agent_id_for_serial(other_identity) == "other-agent"
    assert hub.get_device_extra(old_identity) == {}
    assert hub.get_device_extra(other_identity) == {
        "owner": "other",
        "readiness": {"ready": True},
    }

    dispatched = await client.post(
        "/api/internal/harmony-vm/instances/offline-vm/dispatch",
        headers=AUTH,
        json={"agent_id": "new-agent"},
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["sent"] is True
    assert dispatched.json()["instance"]["state"] == "starting"

    session.expire_all()
    recovered = await session.get(HarmonyVmInstance, "offline-vm")
    running = await session.get(HarmonyVmInstance, "running-vm")
    assert recovered is not None and running is not None
    assert recovered.assigned_agent_id == "new-agent"
    assert recovered.hdc_port == old_port
    assert recovered.lease_token != old_token
    assert (running.state, running.hdc_port, running.lease_token) == (
        "running",
        other_port,
        other_token,
    )
    assert len(hub.messages) == 1
    agent_id, payload = hub.messages[0]
    assert agent_id == "new-agent"
    assert payload["type"] == P.MSG_HARMONY_VM_START
    assert payload["vm_id"] == "offline-vm"
    assert payload["assigned_port"] == old_port
    assert payload["lease_token"] == recovered.lease_token


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
    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (HarmonyVmTools(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        lambda args, **_kwargs: calls.append(("emulator", tuple(args)))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        manager_module,
        "hdc_run",
        lambda *args, **_kwargs: calls.append(("hdc", args)) or "",
    )
    monkeypatch.setattr(manager, "_wait_removed", lambda *_args: next(confirmations))
    monkeypatch.setattr(manager, "_active_instance_serial", lambda *_args: serial)

    try:
        first = manager.stop_sync(vm_id, serial, old_token)
        assert first["ok"] is False
        assert first["details"]["cleanup_confirmed"] is False
        assert manager._known[vm_id]["cleanup_pending"] is True  # noqa: SLF001

        # A process restart must not turn ready=False into a false cleanup ack.
        manager = HarmonyVmManager(runtime_dir=tmp_path)
        monkeypatch.setattr(manager, "_wait_removed", lambda *_args: next(confirmations))
        monkeypatch.setattr(manager, "_active_instance_serial", lambda *_args: serial)
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
    scans = iter([[], [orphan]])
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: next(scans))
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
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    manager._known[vm_id] = {  # noqa: SLF001
        "vm_id": vm_id,
        "instance_name": instance_name,
        "instance_path": str(instance_root),
        "ready": False,
    }

    result = manager.delete_sync(vm_id)

    assert result["ok"] is True
    assert result["details"]["crash_service_cleanup_confirmed"] is True
    assert orphan.terminated is True


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
    )
    manager._runtimes[runtime.vm_id] = runtime  # noqa: SLF001
    monkeypatch.setattr(manager, "_wait_removed", lambda *_args: (True, "removed"))

    result = manager.stop_sync(runtime.vm_id)

    assert result["ok"] is True
    assert result["details"]["cleanup_confirmed"] is True
    assert result["details"]["crash_service_cleanup_confirmed"] is False


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
