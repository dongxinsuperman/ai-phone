"""Independent reproductions of the two submitted findings; fake native I/O only."""
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.agent import ws_client as ws_mod
from ai_phone.agent.ios_sim import manager as ios_mod
from ai_phone.agent.harmony_vm import manager as harmony_mod
from ai_phone.server.vm_lifecycle import update_runtime
from ai_phone.shared import protocol as P
from tests.test_vm_lifecycle_view_api import view_platform, view_context, create_vm, AUTH

OUT = Path(os.environ["REVIEW_CASE_OUT"])

def evidence(name, value):
    (OUT / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True], ids=["online-control", "offline-then-manual-dispatch"])
async def test_replacement_can_be_manually_dispatched_after_target_returns(view_context, monkeypatch, disconnect):
    context = view_context
    platform, hub = context.platform, context.hub
    old_id = (await create_vm(context))["id"]
    row = await context.session.get(platform.model, old_id)
    row.assigned_agent_id = "agent-1"
    row.state = "stopped"
    update_runtime(row, owner_host_id="fixture-host", instance_created=True,
        last_status={"state":"stopped", "ok":True, "details":{"cleanup_confirmed":True}})
    await context.session.commit()
    hub.online.add("agent-2")
    original_send = hub.send_to_agent
    async def send(agent_id, payload):
        if disconnect and payload.get("vm_id") == old_id and payload["type"].endswith("_delete"):
            hub.online.discard("agent-2")
        return await original_send(agent_id, payload)
    monkeypatch.setattr(hub, "send_to_agent", send)
    first = await context.client.post(platform.base + "/" + old_id + "/dispatch",
        json={"agent_id":"agent-2"}, headers=AUTH)
    if not disconnect:
        assert first.status_code == 200, first.text
        assert first.json()["switched"] is True
        assert any(payload["type"] == platform.start for _, payload in hub.sent)
        return
    assert first.status_code == 409, first.text
    detail = first.json()["detail"]
    assert detail["switched"] is True and detail["vm_id"] != old_id
    new_id = detail["vm_id"]
    hub.online.add("agent-2")
    view = (await context.client.get(platform.base + "/" + new_id, headers=AUTH)).json()
    second = await context.client.post(platform.base + "/" + new_id + "/dispatch",
        json={"agent_id":"agent-2"}, headers=AUTH)
    old_get = await context.client.get(platform.base + "/" + old_id, headers=AUTH)
    evidence("redispatch-" + platform.name, {
        "first_http":first.status_code, "replacement_id":new_id,
        "old_http":old_get.status_code, "replacement_state":view["state"],
        "replacement_owner":view["assigned_agent_id"], "lifecycle":view["lifecycle"],
        "target_online":hub.has_agent("agent-2"), "second_http":second.status_code,
        "second_body":second.json(), "native_start_commands":sum(p["type"] == platform.start for _,p in hub.sent)})
    assert second.status_code == 200, f"User-requested new dispatch is blocked: {second.text}"

@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["ios", "harmony"])
@pytest.mark.parametrize("phase", ["none", "progress-send", "executor-queue", "instance-lock"])
async def test_stop_does_not_begin_native_mutation_after_original_deadline(monkeypatch, tmp_path, platform, phase):
    clock = SimpleNamespace(now=100.0)
    fake_time = SimpleNamespace(monotonic=lambda:clock.now, time=time.time,
        sleep=lambda seconds:setattr(clock, "now", clock.now+seconds))
    mod = ios_mod if platform == "ios" else harmony_mod
    monkeypatch.setattr(mod, "time", fake_time)
    monkeypatch.setattr(ws_mod, "time", fake_time)
    vm_id, udid = "abcd12345678", "fixture-ios-udid"
    native = []
    observation={"vm_id":vm_id, "state":"stopped", "identity":{},
        "resources_released":True, "reason":"fixture exit"}
    if platform == "ios":
        manager = mod.IosSimVmManager()
        state = {"value":"Booted"}
        monkeypatch.setattr(manager, "_list_all_instances_strict", lambda **kw:{
            udid:{"name":mod.managed_sim_name(vm_id),"state":state["value"],
                  "device_type":"fixture-phone","runtime":"fixture-runtime"}})
        def simctl(*args, **kwargs):
            assert args == ("shutdown", udid)
            native.append({"command":"simctl shutdown", "at":clock.now, "timeout":kwargs.get("timeout")})
            state["value"]="Shutdown"
            return ""
        monkeypatch.setattr(mod, "simctl_run", simctl)
        monkeypatch.setattr(manager, "_forget", lambda *a:None)
        monkeypatch.setattr(manager, "observe_instance", lambda *a, **kw:observation)
        kind = P.MSG_IOS_SIM_VM_STOP
        extra = {"udid":udid}
    else:
        manager = mod.HarmonyVmManager(runtime_dir=tmp_path / "owned-harmony")
        rt = mod.HarmonyVmRuntime(vm_id=vm_id, name=vm_id, instance_name="aiphone_harmony_"+vm_id,
            instance_path=str(tmp_path), image_root=str(tmp_path), hdc_port=12345,
            hdc_serial="127.0.0.1:12345", lease_token="fixture-lease", ready=True)
        manager._runtimes[vm_id]=rt
        monkeypatch.setattr(mod, "managed_vm_owner", lambda *a:None)
        monkeypatch.setattr(mod, "unregister_managed_vm", lambda *a:None)
        monkeypatch.setattr(manager, "_remove_fport", lambda *a:None)
        monkeypatch.setattr(mod, "find_harmony_tools", lambda:(SimpleNamespace(emulator="/fixture/Emulator"),[]))
        def native_run(args, **kwargs):
            assert args == ["/fixture/Emulator","-stop","aiphone_harmony_"+vm_id]
            native.append({"command":"Emulator -stop","at":clock.now,"timeout":kwargs.get("timeout")})
            return SimpleNamespace(returncode=0,stdout="",stderr="")
        monkeypatch.setattr(mod, "subprocess", SimpleNamespace(run=native_run))
        monkeypatch.setattr(mod, "hdc_run", lambda *a,**kw:"")
        monkeypatch.setattr(manager, "_wait_removed", lambda *a:(True,"fixture exit"))
        monkeypatch.setattr(manager, "_active_instance_serial", lambda *a:"")
        monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *a:(True,[]))
        monkeypatch.setattr(manager, "_save_registry", lambda:None)
        monkeypatch.setattr(manager, "_observation_from_exit", lambda *a,**kw:observation)
        kind = P.MSG_HARMONY_VM_STOP
        extra = {"hdc_serial":rt.hdc_serial,"lease_token":rt.lease_token}
    monkeypatch.setattr(manager, "_refresh_devices_safe", AsyncMock())
    if phase == "instance-lock":
        class DelayedLock:
            def __enter__(self): clock.now += 2
            def __exit__(self,*args): pass
        monkeypatch.setattr(manager, "_vm_lock", lambda *a:DelayedLock())
    async def to_thread(fn,*args,**kwargs):
        if phase == "executor-queue": clock.now += 2
        return fn(*args,**kwargs)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    client = AgentWSClient(ws_url="ws://fixture.invalid",token="fixture",agent_id="fixture",agent_name="fixture")
    sent=[]
    async def send(payload):
        sent.append(payload)
        if phase == "progress-send" and payload.get("state") == "stopping": clock.now += 2
        return True
    monkeypatch.setattr(client, "send", send)
    received=[]
    async def handler(c,msg):
        received.append(msg["operation_timeout_sec"])
        await manager.handle_stop(c,msg)
    message={"type":kind,"vm_id":vm_id,"request_id":"fixture-stop","operation_timeout_sec":5.0,**extra}
    await client._dispatch_vm_handler(handler,message)
    tasks=list(client._vm_tasks)
    clock.now += 4.0
    await asyncio.gather(*tasks)
    assert received == [1.0], received
    evidence("stop-"+platform+"-"+phase, {"original_deadline":105.0,
        "handler_remaining_seconds":received,"native_events":native,"reports":sent})
    if phase == "none":
        assert len(native)==1 and native[0]["at"]==104.0
        assert sent[-1]["ok"] is True, sent
    else:
        assert native == [], f"Native stop began after expiry: {native}"

