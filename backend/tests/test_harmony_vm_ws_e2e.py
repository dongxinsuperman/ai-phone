"""Local Harmony VM E2E through real HTTP, WebSocket, and Agent VM handlers.

DevEco, HDC discovery/commands, and hmdriver2 are simulated. Run admission and
its downlink are checked; this does not execute VLM/Case steps on an Emulator.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import close_all_sessions

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.harmony_vm import manager as manager_module
from ai_phone.agent.harmony_vm.manager import HarmonyVmManager
from ai_phone.agent.harmony_vm.registry import managed_vm_owner, unregister_managed_vm
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.config import get_settings
from ai_phone.server import db as db_module
from ai_phone.server.api import include_routers
from ai_phone.server.db import Base
from ai_phone.server.harmony_vm import api as harmony_api
from ai_phone.server.harmony_vm.models import HarmonyVmCatalogSnapshot
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.ws import include_ws
from ai_phone.server.ws import agent_ws as server_agent_ws
from ai_phone.shared import protocol as P


HDC = "127.0.0.1:10007"
AGENT_ID = "local-harmony-agent"
AUTH = {"Authorization": "Bearer harmony-e2e"}


class _ReadyProbe:
    async def probe_agent(self, **_kwargs):
        return {"ok": True}

    def excluded_ports_for(self, _agent_id):
        return set(range(10000, 10007))


class _FakeProcess:
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):  # noqa: ARG002
        return self.returncode

    def kill(self):
        self.returncode = -9


class _FakeDriver:
    def get_raw_driver(self):
        return SimpleNamespace(_client=SimpleNamespace(local_port=16557))

    def close(self):
        return None


@pytest_asyncio.fixture
async def protocol_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("AI_PHONE_AGENT_TOKEN", "harmony-e2e")
    monkeypatch.setenv("AI_PHONE_SUBMISSION_INTERNAL_TOKEN", "harmony-e2e")
    get_settings.cache_clear()
    monkeypatch.setattr(harmony_api, "get_capability_waiter", lambda: _ReadyProbe())

    await db_module.dispose_engine()
    db_module.init_engine(db_url=f"sqlite+aiosqlite:///{tmp_path / 'harmony-e2e.db'}")
    from ai_phone.server import models  # noqa: F401

    async with db_module.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_module.get_session_factory()() as session:
        image_id = "Phone|HarmonyOS 6.0.0(20)|arm64"
        session.add(HarmonyVmCatalogSnapshot(
            id="official",
            source_type="deveco_emulator_official",
            device_types_json=["Phone"],
            images_json=[{
                "id": image_id,
                "device_type": "Phone",
                "os_version": "HarmonyOS 6.0.0(20)",
                "api_version": "20",
                "abi": "arm64",
            }],
            screen_profiles_json=[{
                "id": "Phone|Default",
                "device_type": "Phone",
                "name": "Default Phone",
                "width": 1080,
                "height": 2340,
                "density": 420,
                "supported_image_ids": [image_id],
                "create_methods": {image_id: "default"},
            }],
        ))
        await session.commit()

    app = FastAPI()
    app.state.hub = Hub()
    app.state.lock_store = DeviceLockStore()
    include_routers(app)
    include_ws(app)
    previous_queue = server_agent_ws._persist_queue  # noqa: SLF001
    previous_worker = server_agent_ws._persist_worker  # noqa: SLF001
    server_agent_ws._persist_queue = None  # noqa: SLF001
    server_agent_ws._persist_worker = None  # noqa: SLF001
    try:
        yield app
    finally:
        from ai_phone.server.trajectory_cache.finalize import _BACKGROUND_TASKS

        if _BACKGROUND_TASKS:
            await asyncio.gather(*tuple(_BACKGROUND_TASKS), return_exceptions=True)
        worker = server_agent_ws._persist_worker  # noqa: SLF001
        if worker is not None and not worker.done():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        server_agent_ws._persist_queue = previous_queue  # noqa: SLF001
        server_agent_ws._persist_worker = previous_worker  # noqa: SLF001
        await close_all_sessions()
        await db_module.dispose_engine()
        get_settings.cache_clear()


@pytest.fixture
def native_simulator(tmp_path, monkeypatch):
    """Replace only calls that would touch this Mac's Emulator/HDC/driver."""
    manager = HarmonyVmManager(runtime_dir=tmp_path / "agent-runtime")
    monkeypatch.setattr(manager, "probe", lambda _msg: {"ok": True})
    monkeypatch.setattr(
        manager_module,
        "find_harmony_tools",
        lambda: (SimpleNamespace(emulator="/fake/Emulator"), []),
    )
    monkeypatch.setattr(manager, "_resolve_image_root", lambda _msg: "")
    monkeypatch.setattr(manager, "_port_conflict", lambda _port, _serial: "")

    def create_instance(_emulator, runtime, _msg):
        config = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("uuid=00000000-0000-0000-0000-000000000000\n", encoding="utf-8")

    monkeypatch.setattr(manager, "_create_instance", create_instance)
    monkeypatch.setattr(manager_module, "_spawn_harmony_emulator", lambda *_a, **_kw: _FakeProcess())
    monkeypatch.setattr(manager, "_wait_hdc", lambda _runtime, **_kw: None)
    monkeypatch.setattr(manager, "_wait_driver", lambda _runtime, **_kw: _FakeDriver())
    monkeypatch.setattr(manager, "_wait_removed", lambda _serial, _port: (True, "removed"))
    monkeypatch.setattr(manager_module, "hdc_run", lambda *_a, **_kw: "")

    real_run = subprocess.run

    def emulator_run(args, **kwargs):
        if args and args[0] == "/fake/Emulator":
            if "-delete" in args:
                instance_name = args[args.index("-delete") + 1]
                instance_path = Path(args[args.index("-instancePath") + 1])
                shutil.rmtree(instance_path / instance_name, ignore_errors=True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(manager_module.subprocess, "run", emulator_run)
    yield manager
    for vm_id, runtime in list(manager._runtimes.items()):  # noqa: SLF001
        unregister_managed_vm(vm_id, runtime.lease_token)


async def _eventually(call, predicate, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await call()
        if predicate(last):
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not met; last result={last!r}")


async def _serve_loopback(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off",
        log_level="error", access_log=False,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]), name="harmony-e2e-server")
    await _eventually(
        lambda: asyncio.sleep(0, result=server.started or task.done()),
        lambda started: started,
    )
    assert not task.done(), task.exception()
    return server, task, listener, port


@pytest.mark.asyncio
async def test_http_ws_agent_a_to_b_reused_hdc_keeps_device_and_run_identity(
    protocol_app, native_simulator
):
    manager = native_simulator
    server, server_task, listener, port = await _serve_loopback(protocol_app)
    base_url = f"http://127.0.0.1:{port}"
    start_messages: asyncio.Queue[dict] = asyncio.Queue()
    stop_messages: asyncio.Queue[dict] = asyncio.Queue()
    delete_messages: asyncio.Queue[dict] = asyncio.Queue()
    run_messages: asyncio.Queue[dict] = asyncio.Queue()

    def devices():
        # Simulated native HDC discovery reports the target only while a VM runs.
        physical = (
            [DeviceInfo(serial=HDC, platform="harmony")]
            if manager._runtimes else []  # noqa: SLF001
        )
        return [info.to_dict() for info in manager.decorate_devices(physical)]

    agent = AgentWSClient(
        ws_url=f"ws://127.0.0.1:{port}/ws/agent",
        token=get_settings().agent_token,
        agent_id=AGENT_ID,
        agent_name=AGENT_ID,
        device_provider=devices,
        ping_interval=3600,
        rescan_interval=3600,
    )

    async def on_start(client, msg):
        await start_messages.put(msg)
        await manager.handle_start(client, msg)

    async def on_stop(client, msg):
        await stop_messages.put(msg)
        await manager.handle_stop(client, msg)

    async def on_delete(client, msg):
        await delete_messages.put(msg)
        await manager.handle_delete(client, msg)

    async def on_run(_client, msg):
        # Runner/VLM execution is outside this VM identity protocol test.
        await run_messages.put(msg)

    agent.on(P.MSG_HARMONY_VM_START, on_start)
    agent.on(P.MSG_HARMONY_VM_STOP, on_stop)
    agent.on(P.MSG_HARMONY_VM_DELETE, on_delete)
    agent.on(P.MSG_START_RUN, on_run)
    agent_task = asyncio.create_task(agent.run_forever(), name="harmony-e2e-agent")

    try:
        await _eventually(
            lambda: asyncio.sleep(0, result=protocol_app.state.hub.has_agent(AGENT_ID)),
            lambda online: online,
        )
        async with AsyncClient(base_url=base_url, timeout=5.0) as http:
            async def create(alias):
                response = await http.post(
                    "/api/internal/harmony-vm/instances",
                    headers=AUTH,
                    json={"alias": alias, "os_version": "6.0.0", "api_version": "20"},
                )
                assert response.status_code == 201, response.text
                return response.json()["id"]

            async def dispatch(vm_id):
                response = await http.post(
                    f"/api/internal/harmony-vm/instances/{vm_id}/dispatch",
                    headers=AUTH,
                    json={"agent_id": AGENT_ID},
                )
                assert response.status_code == 200, response.text
                assert response.json()["sent"] is True
                start = await asyncio.wait_for(start_messages.get(), timeout=5)
                assert start["vm_id"] == vm_id
                assert start["assigned_port"] == 10007
                identity = f"harmony-vm:{vm_id}"
                await _eventually(
                    lambda: http.get(f"/api/internal/harmony-vm/instances/{vm_id}", headers=AUTH),
                    lambda result: result.json()["state"] == "running",
                )
                device = await _eventually(
                    lambda: http.get(f"/api/devices/{identity}"),
                    lambda result: result.status_code == 200 and result.json()["status"] == "online",
                )
                return start, device.json()

            vm_a = await create("别名A")
            identity_a = f"harmony-vm:{vm_a}"
            start_a, device_a = await dispatch(vm_a)
            assert (await http.get("/api/devices")).json()[0]["alias"] == "别名A"
            assert device_a["extra"]["hdc_serial"] == HDC
            assert device_a["extra"]["vm_instance_id"] == vm_a
            assert managed_vm_owner(HDC) == (vm_a, start_a["lease_token"])

            await agent.send({
                "type": P.MSG_DEVICE_READINESS,
                "serial": identity_a,
                "platform": "harmony",
                "ready": True,
                "ts": 1.0,
            })
            await _eventually(
                lambda: http.get(f"/api/devices/{identity_a}"),
                lambda result: result.json()["extra"].get("readiness", {}).get("ready") is True,
            )
            run_a = await http.post("/api/runs", json={"device_serial": identity_a, "goal": "A 的运行"})
            assert run_a.status_code == 201, run_a.text
            assert run_a.json()["dispatched"] is True
            run_a_id = run_a.json()["id"]
            assert (await asyncio.wait_for(run_messages.get(), 5))["device_serial"] == identity_a
            # The Agent's terminal message checks the Run readback path without
            # starting a VLM Runner or modifying the Case execution chain.
            await agent.send({
                "type": P.MSG_RUN_DONE,
                "run_id": run_a_id,
                "serial": identity_a,
                "result": "finished",
                "steps": 0,
            })
            completed_a = await _eventually(
                lambda: http.get(f"/api/runs/{run_a_id}"),
                lambda result: result.json()["status"] == "success",
            )
            assert completed_a.json()["device_serial"] == identity_a

            stopped = await http.post(
                f"/api/internal/harmony-vm/instances/{vm_a}/stop", headers=AUTH
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["sent"] is True
            assert (await asyncio.wait_for(stop_messages.get(), 5))["vm_id"] == vm_a
            await _eventually(
                lambda: http.get(f"/api/internal/harmony-vm/instances/{vm_a}", headers=AUTH),
                lambda result: result.json()["state"] == "stopped"
                and result.json()["hdc_port"] is None,
            )
            deleted = await http.delete(
                f"/api/internal/harmony-vm/instances/{vm_a}", headers=AUTH
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["deleted"] is True
            assert deleted.json()["cleanup_sent"] is True
            delete_a = await asyncio.wait_for(delete_messages.get(), 5)
            await _eventually(
                lambda: asyncio.sleep(0, result=manager._known.get(vm_a)),  # noqa: SLF001
                lambda known: known is None,
            )

            vm_b = await create("别名B")
            identity_b = f"harmony-vm:{vm_b}"
            start_b, device_b = await dispatch(vm_b)
            assert vm_b != vm_a
            assert start_b["lease_token"] != start_a["lease_token"]
            assert managed_vm_owner(HDC) == (vm_b, start_b["lease_token"])
            assert (await http.get("/api/devices")).json()[0]["alias"] == "别名B"
            assert device_b["extra"]["hdc_serial"] == HDC
            assert device_b["extra"]["vm_instance_id"] == vm_b
            assert device_b["extra"].get("readiness") is None
            assert device_b["lock"] is None
            assert (await http.get(f"/api/devices/{HDC}")).status_code == 404
            assert (await http.get(f"/api/devices/{identity_a}")).status_code == 404
            assert [row["serial"] for row in (await http.get("/api/devices")).json()] == [identity_b]

            await agent.send({
                "type": P.MSG_DEVICE_READINESS,
                "serial": identity_b,
                "platform": "harmony",
                "ready": True,
                "ts": 2.0,
            })
            await _eventually(
                lambda: http.get(f"/api/devices/{identity_b}"),
                lambda result: result.json()["extra"].get("readiness", {}).get("ready") is True,
            )
            run_b = await http.post("/api/runs", json={"device_serial": identity_b, "goal": "B 的运行"})
            assert run_b.status_code == 201, run_b.text
            run_b_id = run_b.json()["id"]
            assert (await asyncio.wait_for(run_messages.get(), 5))["device_serial"] == identity_b
            await agent.send({
                "type": P.MSG_RUN_DONE,
                "run_id": run_b_id,
                "serial": identity_b,
                "result": "finished",
                "steps": 0,
            })
            completed_b = await _eventually(
                lambda: http.get(f"/api/runs/{run_b_id}"),
                lambda result: result.json()["status"] == "success",
            )
            assert completed_b.json()["device_serial"] == identity_b
            assert [row["id"] for row in (await http.get("/api/runs", params={"device_serial": identity_a})).json()] == [run_a_id]
            assert [row["id"] for row in (await http.get("/api/runs", params={"device_serial": identity_b})).json()] == [run_b_id]
            assert (await http.get(f"/api/runs/{run_a_id}")).json()["device_serial"] == identity_a

            # Delayed A reports are mixed with B's current snapshot. B must
            # remain the only routable and visible owner of port 10007.
            stale_a = {
                "serial": identity_a,
                "platform": "harmony",
                "status": "online",
                "extra": {
                    "device_kind": "virtual",
                    "vm_platform": "harmony",
                    "vm_instance_id": vm_a,
                    "hdc_serial": HDC,
                    "lease_token": start_a["lease_token"],
                },
            }
            await agent.send({
                "type": P.MSG_HARMONY_VM_STATUS,
                "vm_id": vm_a,
                "lease_token": start_a["lease_token"],
                "state": "running",
                "ok": True,
                "hdc_serial": HDC,
            })
            await agent.send({"type": P.MSG_HELLO, "devices": [stale_a, *devices()]})
            await agent.send({
                "type": P.MSG_DEVICE_READINESS,
                "serial": identity_a,
                "platform": "harmony",
                "ready": False,
                "ts": 3.0,
            })
            await manager.handle_delete(agent, delete_a)
            # The B readiness update is a FIFO barrier on the Agent WS: once
            # read back, every earlier stale A message has been processed.
            await agent.send({
                "type": P.MSG_DEVICE_READINESS,
                "serial": identity_b,
                "platform": "harmony",
                "ready": True,
                "ts": 4.0,
            })
            await _eventually(
                lambda: http.get(f"/api/devices/{identity_b}"),
                lambda result: result.json()["extra"].get("readiness", {}).get("ts") == 4.0,
            )
            assert managed_vm_owner(HDC) == (vm_b, start_b["lease_token"])
            assert protocol_app.state.hub.agent_id_for_serial(identity_b) == AGENT_ID
            assert protocol_app.state.hub.agent_id_for_serial(identity_a) is None
            rows = (await http.get("/api/devices")).json()
            assert [(row["serial"], row["alias"]) for row in rows] == [(identity_b, "别名B")]
            assert rows[0]["extra"]["readiness"]["ready"] is True
            assert manager._known.get(vm_a) is None  # noqa: SLF001
            assert (manager.instance_path / f"aiphone_harmony_{vm_a}").exists() is False
    finally:
        await agent.stop()
        await asyncio.wait_for(agent_task, timeout=5)
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        listener.close()
