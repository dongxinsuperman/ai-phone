"""Managed Harmony VM identity must survive reuse of an HDC connection address."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.harmony_vm.manager import (
    HarmonyVmManager,
    HarmonyVmRuntime,
    _apply_instance_uuid,
)
from ai_phone.agent.harmony_vm.registry import (
    register_managed_vm,
    resolve_harmony_serial,
    unregister_managed_serial,
    unregister_managed_vm,
)
from ai_phone.server.harmony_vm.models import HarmonyVmCatalogSnapshot, HarmonyVmInstance
from ai_phone.server.harmony_vm.service import (
    allocate_port_lease,
    filter_managed_devices_for_agent,
    handle_vm_status,
    placeholder_serial,
)
from ai_phone.server.hub import Hub
from ai_phone.server.models import Device, DeviceAlias, Run
from ai_phone.server.ws.agent_ws import _dispatch, _upsert_devices
from ai_phone.shared.harmony_identity import harmony_udid_from_uuid
from ai_phone.shared import protocol as P


AUTH = {"Authorization": "Bearer dev"}
HDC_SERIAL = "127.0.0.1:10007"
AGENT_ID = "harmony-agent"


@pytest_asyncio.fixture
async def harmony_catalog(session):
    """The two API-created VMs share one official image and screen profile."""
    image_id = "Phone|HarmonyOS 6.0.0(20)|arm64"
    session.add(
        HarmonyVmCatalogSnapshot(
            id="official",
            source_type="deveco_emulator_official",
            device_types_json=["Phone"],
            images_json=[
                {
                    "id": image_id,
                    "device_type": "Phone",
                    "os_version": "HarmonyOS 6.0.0(20)",
                    "api_version": "20",
                    "abi": "arm64",
                }
            ],
            screen_profiles_json=[
                {
                    "id": "Phone|Default",
                    "device_type": "Phone",
                    "name": "Default Phone",
                    "width": 1080,
                    "height": 2340,
                    "density": 420,
                    "supported_image_ids": [image_id],
                    "create_methods": {image_id: "default"},
                }
            ],
        )
    )
    await session.commit()


async def _create_vm(client, alias: str) -> str:
    response = await client.post(
        "/api/internal/harmony-vm/instances",
        headers=AUTH,
        json={"alias": alias, "os_version": "6.0.0", "api_version": "20"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _start_on_10007(session, vm_id: str) -> str:
    session.expire_all()
    vm = await session.get(HarmonyVmInstance, vm_id)
    lease = await allocate_port_lease(
        session,
        vm,
        agent_id=AGENT_ID,
        excluded_ports=range(10000, 10007),
    )
    assert lease.port == 10007
    token = lease.lease_token
    await session.commit()
    await handle_vm_status(
        AGENT_ID,
        {
            "vm_id": vm_id,
            "lease_token": token,
            "state": "running",
            "ok": True,
            "hdc_serial": HDC_SERIAL,
        },
    )
    return token


def _report(vm_id: str, token: str, *, reason: str) -> dict:
    return {
        "serial": placeholder_serial(vm_id),
        "platform": "harmony",
        "brand": "Huawei",
        "model": "Harmony VM",
        "status": "online",
        "extra": {
            "device_kind": "virtual",
            "is_virtual": True,
            "vm_platform": "harmony",
            "vm_instance_id": vm_id,
            "hdc_serial": HDC_SERIAL,
            "lease_token": token,
            "reason": reason,
        },
    }


async def _agent_reports(hub: Hub, devices: list[dict]) -> list[dict]:
    accepted = await filter_managed_devices_for_agent(AGENT_ID, devices)
    await hub.set_devices(AGENT_ID, {d["serial"] for d in accepted})
    await _upsert_devices(AGENT_ID, accepted, hub)
    return accepted


async def test_deleted_vm_and_new_vm_reusing_10007_keep_all_identities_apart(
    client, session, app, harmony_catalog
):
    """A's alias, state and history must never become B's on port reuse."""
    hub = Hub()
    app.state.hub = hub
    sent = []

    class AgentSocket:
        async def send_json(self, payload):
            sent.append(payload)

    await hub.register_agent(AGENT_ID, AGENT_ID, "macos", AgentSocket())

    vm_a = await _create_vm(client, "别名A")
    identity_a = placeholder_serial(vm_a)
    token_a = await _start_on_10007(session, vm_a)
    assert (await _agent_reports(hub, [_report(vm_a, token_a, reason="A-only")]))[0]["serial"] == identity_a
    hub.set_device_readiness(identity_a, {"ready": True, "platform": "harmony"})
    lock_a = await app.state.lock_store.acquire(identity_a, "A-run", "auto")
    old_run = Run(device_serial=identity_a, goal="A 的历史运行", status="success")
    session.add(old_run)
    await session.commit()
    old_run_id = old_run.id

    session.expire_all()
    assert (await session.get(DeviceAlias, identity_a)).alias == "别名A"
    assert await session.get(Device, HDC_SERIAL) is None
    assert hub.get_device_extra(identity_a)["reason"] == "A-only"
    assert hub.agent_id_for_serial(identity_a) == AGENT_ID

    await handle_vm_status(
        AGENT_ID,
        {
            "vm_id": vm_a,
            "lease_token": token_a,
            "state": "stopped",
            "ok": True,
            "hdc_serial": HDC_SERIAL,
        },
    )
    await _agent_reports(hub, [])
    deleted = await client.delete(f"/api/internal/harmony-vm/instances/{vm_a}", headers=AUTH)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert any(message.get("vm_id") == vm_a for message in sent)

    # Simulate a stale in-memory A lock/readiness surviving deletion.  B must
    # still start with its own empty state rather than inheriting port 10007's.
    hub.set_device_readiness(identity_a, {"ready": True, "platform": "harmony"})
    assert app.state.lock_store.peek(identity_a).token == lock_a.token

    vm_b = await _create_vm(client, "别名B")
    identity_b = placeholder_serial(vm_b)
    assert vm_b != vm_a
    assert identity_b != identity_a
    token_b = await _start_on_10007(session, vm_b)
    assert token_b != token_a
    accepted = await _agent_reports(hub, [_report(vm_b, token_b, reason="B-only")])
    assert [d["serial"] for d in accepted] == [identity_b]
    assert "lease_token" not in accepted[0]["extra"]

    session.expire_all()
    assert await session.get(Device, identity_a) is None
    assert await session.get(Device, identity_b) is not None
    assert await session.get(Device, HDC_SERIAL) is None
    assert await session.get(DeviceAlias, identity_a) is None
    assert (await session.get(DeviceAlias, identity_b)).alias == "别名B"
    assert await session.get(DeviceAlias, HDC_SERIAL) is None
    assert hub.agent_id_for_serial(identity_a) is None
    assert hub.agent_id_for_serial(identity_b) == AGENT_ID
    assert hub.agent_id_for_serial(HDC_SERIAL) is None
    assert hub.get_device_extra(identity_b)["reason"] == "B-only"
    assert hub.get_device_extra(identity_b)["vm_instance_id"] == vm_b
    assert "readiness" not in hub.get_device_extra(identity_b)
    assert app.state.lock_store.peek(identity_b) is None

    listed = await client.get("/api/devices")
    assert listed.status_code == 200
    assert [(row["serial"], row["alias"]) for row in listed.json()] == [(identity_b, "别名B")]
    assert listed.json()[0]["lock"] is None
    assert listed.json()[0]["extra"]["reason"] == "B-only"
    assert (await client.get("/api/devices/available")).json() == []
    await _dispatch(
        hub,
        app.state.lock_store,
        AGENT_ID,
        {
            "type": P.MSG_DEVICE_READINESS,
            "serial": HDC_SERIAL,
            "platform": "harmony",
            "ready": True,
            "ts": 1.0,
        },
    )
    assert "readiness" not in hub.get_device_extra(HDC_SERIAL)
    assert "readiness" not in hub.get_device_extra(identity_b)
    await _dispatch(
        hub,
        app.state.lock_store,
        AGENT_ID,
        {
            "type": P.MSG_DEVICE_READINESS,
            "serial": identity_a,
            "platform": "harmony",
            "ready": False,
            "ts": 2.0,
        },
    )
    assert hub.get_device_extra(identity_a)["readiness"]["ready"] is True
    await _dispatch(
        hub,
        app.state.lock_store,
        AGENT_ID,
        {
            "type": P.MSG_DEVICE_READINESS,
            "serial": identity_b,
            "platform": "harmony",
            "ready": True,
            "ts": 3.0,
        },
    )
    assert [row["serial"] for row in (await client.get("/api/devices/available")).json()] == [identity_b]

    # Exercise the actual Run admission/dispatch boundary without starting an
    # Agent Runner: the fake Agent socket records the downlink message.
    started = await client.post("/api/runs", json={"device_serial": identity_b, "goal": "B 的新运行"})
    assert started.status_code == 201, started.text
    assert started.json()["dispatched"] is True
    assert started.json()["device_serial"] == identity_b
    new_run_id = started.json()["id"]
    session.expire_all()
    assert (await session.get(Run, new_run_id)).device_serial == identity_b
    start_messages = [message for message in sent if message.get("type") == P.MSG_START_RUN]
    assert len(start_messages) == 1
    assert start_messages[0]["run_id"] == new_run_id
    assert start_messages[0]["device_serial"] == identity_b
    history_a = (await client.get("/api/runs", params={"device_serial": identity_a})).json()
    history_b = (await client.get("/api/runs", params={"device_serial": identity_b})).json()
    assert [row["id"] for row in history_a] == [old_run_id]
    assert [row["id"] for row in history_b] == [new_run_id]
    assert (await client.get(f"/api/runs/{old_run_id}")).json()["device_serial"] == identity_a

    # A's delayed report has an obsolete VM id/token.  It cannot take B's route.
    assert await filter_managed_devices_for_agent(AGENT_ID, [_report(vm_a, token_a, reason="stale-A")]) == []
    assert hub.agent_id_for_serial(identity_b) == AGENT_ID


async def test_only_managed_harmony_vm_uses_vm_identity(session, app):
    hub = Hub()
    app.state.hub = hub
    devices = [
        {"serial": "emulator-5554", "platform": "android", "extra": {"is_virtual": True}},
        {"serial": "IOS-SIM-UDID", "platform": "ios_sim", "extra": {"is_virtual": True}},
        {"serial": "HDC-REAL-001", "platform": "harmony", "extra": {}},
    ]
    assert await filter_managed_devices_for_agent("other-agent", devices) == devices
    await _upsert_devices("other-agent", devices, hub)
    for device in devices:
        row = await session.get(Device, device["serial"])
        assert row is not None and row.platform == device["platform"]
    rows = (await session.execute(select(Device))).scalars().all()
    assert {row.serial for row in rows} == {device["serial"] for device in devices}


def test_agent_reports_distinct_vm_identities_with_shared_udid_and_hdc_port(tmp_path):
    shared_uuid = "45d5504d-4143-0415-24d0-0123456789ab"
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    identities = []
    udids = []
    try:
        for vm_id in ("vm-a", "vm-b"):
            config = tmp_path / vm_id / "config.ini"
            config.parent.mkdir()
            config.write_text("uuid=00000000-0000-0000-0000-000000000000\n", encoding="utf-8")
            _apply_instance_uuid(config, shared_uuid)
            udids.append(harmony_udid_from_uuid(config.read_text().strip().split("=", 1)[1]))
            manager._runtimes.clear()  # noqa: SLF001
            manager._runtimes[vm_id] = HarmonyVmRuntime(  # noqa: SLF001
                vm_id=vm_id,
                name=vm_id,
                instance_name=f"aiphone_harmony_{vm_id}",
                instance_path=str(tmp_path),
                image_root="",
                hdc_port=10007,
                hdc_serial=HDC_SERIAL,
                lease_token=f"{vm_id}-lease",
                ready=True,
            )
            register_managed_vm(vm_id, HDC_SERIAL, f"{vm_id}-lease")
            listed = manager.decorate_devices([DeviceInfo(serial=HDC_SERIAL, platform="harmony")])
            assert len(listed) == 1
            assert listed[0].extra["hdc_serial"] == HDC_SERIAL
            assert listed[0].extra["vm_instance_id"] == vm_id
            identities.append(listed[0].serial)
    finally:
        unregister_managed_vm("vm-a", "vm-a-lease")
        unregister_managed_vm("vm-b", "vm-b-lease")
    assert identities == ["harmony-vm:vm-a", "harmony-vm:vm-b"]
    assert udids == [harmony_udid_from_uuid(shared_uuid)] * 2


def test_reused_hdc_port_invalidates_old_vm_logical_connection():
    register_managed_vm("vm-a", HDC_SERIAL, "old-lease")
    try:
        assert resolve_harmony_serial("harmony-vm:vm-a") == HDC_SERIAL
        register_managed_vm("vm-b", HDC_SERIAL, "new-lease")
        assert resolve_harmony_serial("harmony-vm:vm-b") == HDC_SERIAL
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            resolve_harmony_serial("harmony-vm:vm-a")
        unregister_managed_vm("vm-a", "old-lease")
        assert resolve_harmony_serial("harmony-vm:vm-b") == HDC_SERIAL
    finally:
        unregister_managed_vm("vm-a", "old-lease")
        unregister_managed_vm("vm-b", "new-lease")


def test_reconcile_does_not_adopt_stale_ready_a_from_bs_reused_port(monkeypatch, tmp_path):
    """Even ready=true in A's old registry is not proof that 10007 is A."""
    import ai_phone.agent.harmony_vm.manager as manager_module
    import psutil

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    instance_path = tmp_path / "instances"
    instance_path.mkdir()
    for vm_id, token in (("vm-a", "old-lease"), ("vm-b", "new-lease")):
        manager._known[vm_id] = {  # noqa: SLF001
            "vm_id": vm_id,
            "instance_name": f"aiphone_harmony_{vm_id.replace('-', '_')}",
            "instance_path": str(instance_path),
            "hdc_port": 10007,
            "hdc_serial": HDC_SERIAL,
            "lease_token": token,
            "ready": True,
        }

    # HDC shows 10007 as connected, but the only live DevEco process belongs
    # to B's exact instance directory and boot command.
    monkeypatch.setattr(
        manager_module,
        "hdc_list_targets",
        lambda: [SimpleNamespace(serial=HDC_SERIAL, status="Connected")],
    )
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _attrs: [
            SimpleNamespace(
                info={
                    "cmdline": [
                        "/deveco/Emulator",
                        "-start",
                        "aiphone_harmony_vm_b",
                        "-instancePath",
                        str(instance_path),
                        "-hdcPort",
                        "10007",
                    ]
                }
            )
        ],
    )

    class FakeDriver:
        def get_raw_driver(self):
            return SimpleNamespace(_client=SimpleNamespace(local_port=16556))

        def window_size(self):
            return (1080, 2340)

    opened = []

    def fake_open(serial):
        opened.append(serial)
        return FakeDriver()

    monkeypatch.setattr(manager_module, "open_harmony_driver", fake_open)
    try:
        adopted = manager.reconcile_running_vms_sync()
        assert [runtime.vm_id for runtime in adopted] == ["vm-b"]
        assert set(manager._runtimes) == {"vm-b"}  # noqa: SLF001
        assert opened == [HDC_SERIAL]
        assert resolve_harmony_serial("harmony-vm:vm-b") == HDC_SERIAL
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            resolve_harmony_serial("harmony-vm:vm-a")
    finally:
        unregister_managed_vm("vm-a", "old-lease")
        unregister_managed_vm("vm-b", "new-lease")
        unregister_managed_serial(HDC_SERIAL)


@pytest.mark.parametrize("operation", ["stop", "delete"])
def test_delayed_old_vm_cleanup_cannot_touch_new_owner_of_reused_hdc(monkeypatch, tmp_path, operation):
    """A's late command may affect A's files, never B's current connection."""
    import ai_phone.agent.harmony_vm.manager as manager_module

    manager = HarmonyVmManager(runtime_dir=tmp_path)
    old_path = tmp_path / "instances" / "aiphone_harmony_vm_a"
    old_path.mkdir(parents=True)
    manager._known["vm-a"] = {  # noqa: SLF001
        "vm_id": "vm-a",
        "instance_name": "aiphone_harmony_vm_a",
        "instance_path": str(old_path.parent),
        "hdc_port": 10007,
        "hdc_serial": HDC_SERIAL,
        "lease_token": "old-lease",
        "ready": False,
    }
    current = HarmonyVmRuntime(
        vm_id="vm-b",
        name="vm-b",
        instance_name="aiphone_harmony_vm_b",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10007,
        hdc_serial=HDC_SERIAL,
        lease_token="new-lease",
        ready=True,
    )
    manager._runtimes["vm-b"] = current  # noqa: SLF001
    manager._known["vm-b"] = {  # noqa: SLF001
        "vm_id": "vm-b",
        "instance_name": "aiphone_harmony_vm_b",
        "instance_path": str(tmp_path / "instances"),
        "hdc_port": 10007,
        "hdc_serial": HDC_SERIAL,
        "lease_token": "new-lease",
        "ready": True,
    }

    def forbidden_hdc(*_args, **_kwargs):
        pytest.fail("old VM cleanup touched B's HDC connection")

    def fake_emulator(args, **_kwargs):
        assert args[1] == "-delete"
        assert args[2] == "aiphone_harmony_vm_a"
        old_path.rmdir()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager_module, "hdc_run", forbidden_hdc)
    monkeypatch.setattr(
        manager_module, "find_harmony_tools", lambda: (SimpleNamespace(emulator="Emulator"), [])
    )
    monkeypatch.setattr(manager_module.subprocess, "run", fake_emulator)
    if operation == "stop":
        result = manager.stop_sync("vm-a", HDC_SERIAL, "old-lease")
    else:
        result = manager.delete_sync("vm-a", HDC_SERIAL, "old-lease")
    assert result["ok"] is True
    assert manager._runtimes["vm-b"] is current  # noqa: SLF001
    assert current.ready is True
    assert current.hdc_serial == HDC_SERIAL
