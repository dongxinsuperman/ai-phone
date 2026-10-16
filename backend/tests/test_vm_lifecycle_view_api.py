"""Lifecycle DTO contracts over isolated ASGI routes and fake Agent messages.

Use the guard runner and an empty temporary working directory. These tests do
not start Server lifespan, contact a native device, or open a network socket.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from ai_phone.server import db
from ai_phone.server.android_vm import api as android_api, service as android_service
from ai_phone.server.harmony_vm import api as harmony_api, service as harmony_service
from ai_phone.server.harmony_vm.models import HarmonyVmCatalogSnapshot, HarmonyVmInstance
from ai_phone.server.hub import Hub
from ai_phone.server.ios_sim import api as ios_api, service as ios_service
from ai_phone.server.ios_sim.models import IosSimCatalogSnapshot, IosSimVmInstance
from ai_phone.server.models import AndroidVmInstance
from ai_phone.server.vm_lifecycle import cancel_operation_timeout, runtime
from ai_phone.shared import protocol as P


AUTH = {"Authorization": "Bearer dev"}
IOS_DEVICE = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
IOS_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
HARMONY_IMAGE = "Phone|HarmonyOS 6.0.0(20)|arm64"
LIFECYCLE_STATES = {None, "draft", "stopped", "starting", "running", "stopping"}
ACTIONS = {"start", "stop", "dispatch", "delete", "edit", "copy"}


class ViewHub(Hub):
    def __init__(self):
        super().__init__()
        self.online = {"agent-1"}
        self.sent = []
        self.failed_types = set()

    def has_agent(self, agent_id):
        return agent_id in self.online

    def agent_host_id(self, agent_id):
        return "fixture-host" if agent_id == "agent-1" else ""

    def snapshot(self):
        return {"agents": [{"agent_id": agent, "agent_name": agent, "host_os": "Darwin"}
                           for agent in sorted(self.online)]}

    async def send_to_agent(self, agent_id, payload):
        self.sent.append((agent_id, dict(payload)))
        return self.has_agent(agent_id) and payload.get("type") not in self.failed_types


@pytest.fixture(params=("android", "ios", "harmony"))
def view_platform(request):
    return {
        "android": SimpleNamespace(
            name="android", api=android_api, svc=android_service, model=AndroidVmInstance,
            base="/api/internal/vm/instances", start=P.MSG_VM_START, stop=P.MSG_VM_STOP,
            body={"alias": "view-vm", "api_level": 33, "abi": "arm64"}, failure_status=409,
        ),
        "ios": SimpleNamespace(
            name="ios", api=ios_api, svc=ios_service, model=IosSimVmInstance,
            base="/api/internal/ios-sim/instances", start=P.MSG_IOS_SIM_VM_START,
            stop=P.MSG_IOS_SIM_VM_STOP,
            body={"alias": "view-vm", "device_type": IOS_DEVICE, "runtime": IOS_RUNTIME},
            failure_status=502,
        ),
        "harmony": SimpleNamespace(
            name="harmony", api=harmony_api, svc=harmony_service, model=HarmonyVmInstance,
            base="/api/internal/harmony-vm/instances", start=P.MSG_HARMONY_VM_START,
            stop=P.MSG_HARMONY_VM_STOP,
            body={"alias": "view-vm", "device_type": "Phone", "image_id": HARMONY_IMAGE,
                  "os_version": "HarmonyOS 6.0.0(20)", "api_version": "20",
                  "abi": "arm64", "screen_profile": "Fixture Phone"}, failure_status=409,
        ),
    }[request.param]


@pytest_asyncio.fixture
async def view_context(app, client, session, view_platform, monkeypatch):
    """Seed minimal catalog rows in the existing per-test temporary DB."""
    if view_platform.name == "ios":
        session.add(IosSimCatalogSnapshot(
            id="official", device_type_count=1,
            payload={"device_types": [{"identifier": IOS_DEVICE, "name": "iPhone 17 Pro",
                "min_runtime_version": 0, "max_runtime_version": 4294967295,
                "min_runtime_version_string": "1.0", "max_runtime_version_string": "65535.255.255"}]},
        ))
    elif view_platform.name == "harmony":
        session.add(HarmonyVmCatalogSnapshot(
            id="official", device_types_json=["Phone"],
            images_json=[{"id": HARMONY_IMAGE, "device_type": "Phone",
                          "os_version": "HarmonyOS 6.0.0(20)", "api_version": "20", "abi": "arm64"}],
            screen_profiles_json=[{"id": "Phone|Fixture Phone", "name": "Fixture Phone",
                "device_type": "Phone", "width": 1080, "height": 2340, "density": 420,
                "size_in": "6.5", "supported_image_ids": [HARMONY_IMAGE],
                "create_methods": {HARMONY_IMAGE: "screen_profile"}}],
        ))
    await session.commit()
    hub = ViewHub()
    app.state.hub = hub

    async def available(**_kwargs):
        return {"ok": True, "reason": "fixture available", "details": {}}

    if view_platform.name != "android":
        monkeypatch.setattr(
            view_platform.api, "get_capability_waiter",
            lambda: SimpleNamespace(probe_agent=available, excluded_ports_for=lambda _agent: []),
        )
    context = SimpleNamespace(client=client, session=session, platform=view_platform, hub=hub)
    try:
        yield context
    finally:
        from sqlalchemy import select

        async with db.get_session_factory()() as cleanup:
            rows = (await cleanup.execute(select(view_platform.model))).scalars().all()
            for vm in rows:
                cancel_operation_timeout(vm)


def assert_lifecycle(instance, *, state=...):
    assert isinstance(instance, dict) and instance.get("id")
    value = instance.get("lifecycle")
    assert isinstance(value, dict), "每个 VM DTO 都必须携带统一 lifecycle"
    assert set(value) == {"state", "agent_online", "actions", "notice", "error"}
    assert value["state"] in LIFECYCLE_STATES
    assert type(value["agent_online"]) is bool
    assert set(value["actions"]) == ACTIONS
    assert all(type(enabled) is bool for enabled in value["actions"].values())
    assert isinstance(value["notice"], str) and isinstance(value["error"], str)
    if state is not ...:
        assert value["state"] == state
    return value


async def create_vm(context):
    response = await context.client.post(context.platform.base, json=context.platform.body, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


async def read_vm(context, vm_id):
    response = await context.client.get(f"{context.platform.base}/{vm_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


async def report_state(
    context, vm_id, state, *, request_id="", observed_request_id="", reason="fixture_observation"
):
    """Send platform identity and the agreed observation shape, never native work."""
    platform = context.platform
    async with db.get_session_factory()() as session:
        row = await session.get(platform.model, vm_id)
        extra = {}
        if platform.name == "android":
            extra["adb_serial"] = row.adb_serial or "emulator-5554"
            identity = {"avd_name": f"aiphone_vm_{vm_id}"}
        elif platform.name == "ios":
            extra["udid"] = row.udid or f"fixture-udid-{vm_id}"
            identity = {"sim_name": f"aiphone_sim_{vm_id}", "udid": extra["udid"]}
        else:
            extra.update(lease_token=row.lease_token or "", hdc_serial=row.hdc_serial or "")
            identity = {"instance_name": f"aiphone_harmony_{vm_id}",
                        "instance_path": f"/fixture-only/aiphone_harmony_{vm_id}", **extra}
    observation = {"vm_id": vm_id, "state": state, "identity": identity,
                   "resources_released": state == "stopped", "reason": reason}
    details = {**identity, "instance_observation": observation,
               "instance_created": True, "cleanup_confirmed": state == "stopped"}
    message = {"vm_id": vm_id, "state": state, "ok": True, "reason": reason,
               "details": details, **extra}
    if request_id:
        message["request_id"] = request_id
    if observed_request_id:
        message["observed_request_id"] = observed_request_id
    await platform.svc.handle_vm_status("agent-1", message, context.hub)


def last_command(context, message_type):
    return next(payload for _, payload in reversed(context.hub.sent) if payload["type"] == message_type)


@pytest.mark.asyncio
async def test_create_list_get_patch_and_ios_copy_include_complete_lifecycle(view_context):
    context = view_context
    created = await create_vm(context)
    value = assert_lifecycle(created, state="draft")
    assert value["agent_online"] is False and value["error"] == ""
    vm_id = created["id"]
    assert_lifecycle(await read_vm(context, vm_id), state="draft")

    listed = await context.client.get(context.platform.base, headers=AUTH)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [vm_id]
    assert_lifecycle(listed.json()[0], state="draft")
    patched = await context.client.patch(
        f"{context.platform.base}/{vm_id}", json={"alias": "view-renamed"}, headers=AUTH
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["alias"] == "view-renamed"
    assert_lifecycle(patched.json(), state="draft")

    if context.platform.name == "ios":
        copied = await context.client.post(
            f"{context.platform.base}/{vm_id}/copy",
            json={**context.platform.body, "alias": "view-copy"}, headers=AUTH,
        )
        assert copied.status_code == 201, copied.text
        assert copied.json()["id"] != vm_id
        assert_lifecycle(copied.json(), state="draft")


@pytest.mark.asyncio
async def test_dispatch_stop_and_restart_responses_include_complete_lifecycle(view_context):
    context = view_context
    vm_id = (await create_vm(context))["id"]
    dispatched = await context.client.post(
        f"{context.platform.base}/{vm_id}/dispatch", json={"agent_id": "agent-1"}, headers=AUTH
    )
    assert dispatched.status_code == 200, dispatched.text
    assert_lifecycle(dispatched.json()["instance"], state="starting")
    await report_state(context, vm_id, "running", request_id=last_command(context, context.platform.start)["request_id"])
    running = assert_lifecycle(await read_vm(context, vm_id), state="running")
    assert running["agent_online"] is True

    stopped = await context.client.post(f"{context.platform.base}/{vm_id}/stop", headers=AUTH)
    assert stopped.status_code == 200, stopped.text
    assert_lifecycle(stopped.json()["instance"], state="stopping")
    await report_state(context, vm_id, "stopped", request_id=last_command(context, context.platform.stop)["request_id"])
    assert_lifecycle(await read_vm(context, vm_id), state="stopped")
    restarted = await context.client.post(f"{context.platform.base}/{vm_id}/start", headers=AUTH)
    assert restarted.status_code == 200, restarted.text
    assert_lifecycle(restarted.json()["instance"], state="starting")


@pytest.mark.asyncio
async def test_send_failure_detail_and_readback_include_same_lifecycle_error(view_context):
    context = view_context
    vm_id = (await create_vm(context))["id"]
    context.hub.failed_types.add(context.platform.start)
    failed = await context.client.post(
        f"{context.platform.base}/{vm_id}/dispatch", json={"agent_id": "agent-1"}, headers=AUTH
    )
    assert failed.status_code == context.platform.failure_status, failed.text
    detail = failed.json()["detail"]
    assert detail["sent"] is False and detail["vm_id"] == vm_id
    failure = assert_lifecycle(detail["instance"])
    assert failure["error"], "失败提示不能只存在本次 HTTP response 内"
    readback = assert_lifecycle(await read_vm(context, vm_id))
    assert readback["error"] == failure["error"]
    listed = await context.client.get(context.platform.base, headers=AUTH)
    assert listed.status_code == 200
    assert assert_lifecycle(listed.json()[0])["error"] == failure["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_action", ["start", "stop"])
async def test_reconnect_updates_native_fact_but_keeps_failure_until_new_accepted_operation(
    view_context, failed_action
):
    """新鲜事实可以改变展示状态，但不能抹掉失败或把旧操作改为成功。"""
    context = view_context
    platform = context.platform
    vm_id = (await create_vm(context))["id"]
    initial = await context.client.post(
        f"{platform.base}/{vm_id}/dispatch", json={"agent_id": "agent-1"}, headers=AUTH
    )
    assert initial.status_code == 200, initial.text
    await report_state(context, vm_id, "running", request_id=last_command(context, platform.start)["request_id"])
    assert_lifecycle(await read_vm(context, vm_id), state="running")

    if failed_action == "start":
        stopped = await context.client.post(f"{platform.base}/{vm_id}/stop", headers=AUTH)
        assert stopped.status_code == 200, stopped.text
        await report_state(context, vm_id, "stopped", request_id=last_command(context, platform.stop)["request_id"])
        assert_lifecycle(await read_vm(context, vm_id), state="stopped")

    context.hub.failed_types.add(platform.start if failed_action == "start" else platform.stop)
    failed = await context.client.post(f"{platform.base}/{vm_id}/{failed_action}", headers=AUTH)
    assert failed.status_code == platform.failure_status, failed.text
    detail = failed.json()["detail"]
    failure_text = assert_lifecycle(detail["instance"])["error"]
    assert failure_text
    failed_request = detail["request_id"]
    assert failed_request
    assert assert_lifecycle(await read_vm(context, vm_id))["error"] == failure_text

    # Exercise actual disconnect persistence, then a same-host reclaimed report.
    context.hub.online.clear()
    if platform.name == "android":
        await platform.svc.mark_agent_vms_unavailable("agent-1", hub=context.hub)
    else:
        await platform.svc.mark_agent_vms_offline("agent-1", hub=context.hub)
    offline = assert_lifecycle(await read_vm(context, vm_id))
    assert offline["agent_online"] is False and offline["error"] == failure_text
    context.hub.online.add("agent-1")

    observed_state = "running" if failed_action == "start" else "stopped"
    await report_state(
        context, vm_id, observed_state, reason="reclaimed", observed_request_id=failed_request
    )
    recovered = assert_lifecycle(await read_vm(context, vm_id), state=observed_state)
    assert recovered["agent_online"] is True
    assert recovered["error"] == failure_text
    async with db.get_session_factory()() as session:
        current = await session.get(platform.model, vm_id)
        operation = runtime(current)["operation"]
        assert operation["request_id"] == failed_request
        assert operation["outcome"] == "failed" and operation["pending"] is False
        assert current.assigned_agent_id == "agent-1"

    context.hub.failed_types.clear()
    next_action = "stop" if observed_state == "running" else "start"
    accepted = await context.client.post(f"{platform.base}/{vm_id}/{next_action}", headers=AUTH)
    assert accepted.status_code == 200, accepted.text
    next_state = "stopping" if next_action == "stop" else "starting"
    assert assert_lifecycle(accepted.json()["instance"], state=next_state)["error"] == ""
    assert assert_lifecycle(await read_vm(context, vm_id), state=next_state)["error"] == ""
    async with db.get_session_factory()() as session:
        current = await session.get(platform.model, vm_id)
        assert runtime(current)["operation"]["request_id"] != failed_request
        assert runtime(current)["operation"]["pending"] is True
