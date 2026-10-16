"""Real native observers feeding the ordinary HTTP Start preflight, with fake I/O."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_phone.agent.android_vm import manager as android_manager
from ai_phone.agent.ios_sim import manager as ios_manager
from ai_phone.server import db
from ai_phone.server.vm_lifecycle import update_runtime
from tests.test_vm_lifecycle_view_api import (
    AUTH,
    IOS_DEVICE,
    IOS_RUNTIME,
    assert_lifecycle,
    create_vm,
    read_vm,
    view_context,
    view_platform,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("view_platform", ["android", "ios"], indirect=True)
async def test_real_stopped_observer_allows_start_of_existing_instance_without_resource_release(
    view_context, monkeypatch, tmp_path
):
    context = view_context
    platform = context.platform
    vm_id = (await create_vm(context))["id"]
    native_reads = []
    observed = []
    expected_udid = f"fixture-owned-{vm_id}"

    async with db.get_session_factory()() as session:
        row = await session.get(platform.model, vm_id)
        row.assigned_agent_id = "agent-1"
        row.state = "error"
        row.error_message = "previous transport state could not be confirmed"
        update_runtime(row, owner_host_id="fixture-host", instance_created=True)
        if platform.name == "android":
            row.adb_serial = "emulator-5554"
        else:
            row.udid = expected_udid
        await session.commit()

    if platform.name == "android":
        # A real temporary AVD directory proves the exact definition exists.
        # There is no process or in-memory runtime from which to infer ports.
        avd_home = tmp_path / "avds"
        avd_home.mkdir()
        avd_name = f"aiphone_vm_{vm_id}"
        (avd_home / f"{avd_name}.avd").mkdir()
        monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))

        def empty_process_inventory(*_args, **_kwargs):
            native_reads.append("process_inventory")
            return iter(())

        def unexpected_port_probe(_port):
            pytest.fail("没有观测到端口的实例不能猜测端口并连接宿主")

        monkeypatch.setattr(android_manager.psutil, "process_iter", empty_process_inventory)
        monkeypatch.setattr(android_manager, "_is_port_free", unexpected_port_probe)
        manager = android_manager.AndroidVmManager(runtime_dir=tmp_path / "agent-runtime")
        assert not manager._runtimes
        expected_identity = {"avd_name": avd_name}
        expected_reason = "ports_not_observed"
    else:
        sim_name = f"aiphone_sim_{vm_id}"

        def fake_simctl(*args, **_kwargs):
            native_reads.append(tuple(args))
            assert args == ("list", "devices", "-j"), "只允许原生只读清单，不得启动、停止或创建"
            return json.dumps({"devices": {IOS_RUNTIME: [{
                "udid": expected_udid, "name": sim_name, "state": "Shutdown",
                "deviceTypeIdentifier": IOS_DEVICE, "isAvailable": True,
            }]}})

        def own_wda_process_is_alive():
            native_reads.append("own_wda_process_poll")
            return None

        monkeypatch.setattr(ios_manager, "simctl_run", fake_simctl)
        manager = ios_manager.IosSimVmManager(runtime_dir=tmp_path / "agent-runtime")
        owned_runtime = ios_manager.SimVmRuntime(
            vm_id=vm_id, name="view-vm", udid=expected_udid, sim_name=sim_name,
            device_type=IOS_DEVICE, runtime_id=IOS_RUNTIME, started_at=0,
            launcher=SimpleNamespace(
                udid=expected_udid,
                _xcodebuild_proc=SimpleNamespace(poll=own_wda_process_is_alive),
            ),
        )
        manager._runtimes[vm_id] = owned_runtime
        expected_identity = {"sim_name": sim_name, "udid": expected_udid}
        expected_reason = "wda_process_running"

    async def inspect_agent(*, hub, vm, agent_id, timeout_sec):
        assert hub is context.hub and agent_id == "agent-1" and vm.id == vm_id
        assert timeout_sec > 0
        observation = manager.observe_instance(vm_id)
        observed.append(observation)
        return {"ok": True, "request_id": "native-observer-preflight",
                "details": {"instance_observation": observation}}

    async def available(**_kwargs):
        return {"ok": True, "reason": "fixture SDK available", "details": {}}

    monkeypatch.setattr(
        platform.api, "get_capability_waiter",
        lambda: SimpleNamespace(inspect_agent=inspect_agent, probe_agent=available,
                                excluded_ports_for=lambda _agent: []),
    )
    before = assert_lifecycle(await read_vm(context, vm_id))
    assert before["state"] is None and before["actions"]["start"] is True
    assert not context.hub.sent

    response = await context.client.post(f"{platform.base}/{vm_id}/start", headers=AUTH)
    assert len(observed) == 1, "普通启动必须调用真实观察函数，不能跳过原实例核验"
    assert observed[0] == {
        "vm_id": vm_id, "state": "stopped", "identity": expected_identity,
        "resources_released": False, "reason": expected_reason,
    }
    assert response.status_code == 200, response.text
    assert response.json()["sent"] is True
    assert_lifecycle(response.json()["instance"], state="starting")
    assert response.json()["instance"]["id"] == vm_id
    assert response.json()["instance"]["assigned_agent_id"] == "agent-1"
    assert len(context.hub.sent) == 1, "只能下发一次 Start，不得自动 Stop/Delete 或创建替代 VM"
    target, payload = context.hub.sent[0]
    assert target == "agent-1" and payload["type"] == platform.start
    assert payload["vm_id"] == vm_id
    assert payload["require_existing_instance"] is True
    assert payload["rollback_new_instance"] is False
    if platform.name == "android":
        assert native_reads == ["process_inventory"]
        assert not manager._runtimes
        assert (avd_home / f"{avd_name}.avd").is_dir()
    else:
        assert payload["udid"] == expected_udid
        assert native_reads == [("list", "devices", "-j"), "own_wda_process_poll"]
        assert manager._runtimes[vm_id] is owned_runtime
        assert owned_runtime.udid == expected_udid
    listed = await context.client.get(platform.base, headers=AUTH)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [vm_id]
