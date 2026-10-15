#!/usr/bin/env python3
"""Personal native regression: legacy stopped registry + lost stop ACK.

Default is a read-only plan. Root must authorize a hardware slot before
--execute. Reuses the a7 harness's environment, source and metadata guards.
No browser, IME, VLM, company endpoint, default orphan scan or global HDC action.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import threading
import time
import traceback
import uuid


HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent.parent
SPEC = importlib.util.spec_from_file_location("personal_harmony_common", AUDIT / "execution/harmony_personal_e2e.py")
common = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(common)
API = "/api/internal/harmony-vm/instances"
PREFIX = "aiphone-personal-harmony-legacy-"
PORT_HELPER = common.REPO / "backend/ai_phone/agent/vm_ports.py"


def plan():
    return {
        "scope": "ONE personal local Harmony Phone; new isolated SQLite/WS/storage/registry/instance root",
        "default_action": "plan only; explicit --execute after root hardware approval",
        "reuse": str(AUDIT / "execution/harmony_personal_e2e.py"),
        "native_steps": ["create unbound VM via API", "real capability/bind/start", "write random guest marker",
                         "real stop while dropping exactly its terminal stop ACK", "disconnect old Agent session",
                         "inject legacy format in only owned registry and SQLite row", "new Agent + fresh Manager, same real host identity file and real pre-hello reclaim",
                         "actual private inventory -> owner handoff only, no state or lease release",
                         "ordinary stop freshly checks native absence; direct old port/token row query proves lease deleted", "restart original VM/UUID and read guest marker",
                         "disconnect only the second WS session; fresh third Manager/session reclaims the still-running private instance with process=None",
                         "ordinary stop, immediate ledger delete, independent native cleanup"],
        "injections": [
            "Drop exactly one own terminal stopped ACK with cleanup_confirmed=true; preserve the real receipt as evidence.",
            "After actual stop and old-session disconnect, only own registry row: ready=false; remove cleanup_confirmed and exit_evidence; keep instance/UUID/port/lease.",
            "Only the freshly-created isolated SQLite VM row: remove runtime.owner_host_id; preserve state/error/last_status/lease and operation facts.",
        ],
        "guards": ["fresh ASCII /tmp root, no .env inheritance", "Python network loopback only", "one exact VM ID and canonical private instance path",
                   "refuse existing emulator processes or connected unowned HDC targets", "private inventory only; never default instance roots",
                   "no global HDC kill/restart or image changes", "default DevEco/config metadata hashes before and after", "product/source hashes before and after",
                   "one scoped emergency delete on failure; preserve failed outcome"],
        "running_reclaim_boundary": "Fresh Manager and Agent session in this harness process; this does not simulate killing the Agent OS process or host.",
        "shared_port_helper": str(PORT_HELPER),
    }


def sources():
    result = common.source_fingerprint()
    for path in (Path(__file__), PORT_HELPER, common.REPO / "backend/ai_phone/agent/identity.py",
                 common.REPO / "backend/ai_phone/server/harmony_vm/models.py"):
        result[str(path.relative_to(common.REPO))] = common.digest(path)
    return result


async def execute(root: Path):
    assert PORT_HELPER.is_file(), "wait for frozen product port helper before native execution"
    protected = common.protected_fingerprint()
    source_hashes = sources()
    token = secrets.token_urlsafe(32)
    common.isolate_environment(root, token)
    # This enables only manager.report_orphan_reconcile for the explicitly
    # supplied private runtime_dir below; it cannot enumerate default roots.
    os.environ["AI_PHONE_HARMONY_VM_ORPHAN_CLEANUP"] = "true"
    import psutil
    import uvicorn
    from fastapi import FastAPI
    from httpx import AsyncClient
    from loguru import logger
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import close_all_sessions
    from ai_phone.agent.drivers.hdc import hdc_run, hdc_list_targets
    from ai_phone.agent.harmony_vm.manager import HarmonyVmManager
    from ai_phone.agent.harmony_vm.registry import managed_vm_owner
    from ai_phone.agent.identity import load_host_identity
    from ai_phone.agent.ws_client import AgentWSClient
    from ai_phone.config import get_settings
    from ai_phone.server import db as db
    from ai_phone.server import models as _models
    from ai_phone.server.api import include_routers
    from ai_phone.server.db import Base
    from ai_phone.server.harmony_vm.models import HarmonyVmCatalogSnapshot, HarmonyVmInstance, HarmonyVmPortLease
    from ai_phone.server.hub import Hub
    from ai_phone.server.lockstore import DeviceLockStore
    from ai_phone.server.ws import include_ws
    from ai_phone.shared import protocol as P

    logger.add(root / "runtime.log", encoding="utf-8")
    report = {"status": "failed", "root": str(root), "started_at": common.utc(), "plan": plan(),
              "protected_before": protected, "source_hashes_at_start": source_hashes}
    common.write_json(root / "preflight.json", report)
    journal = root / "events.jsonl"

    def record(kind, value):
        with journal.open("a") as stream:
            stream.write(json.dumps({"time": common.utc(), "kind": kind, "value": value}, ensure_ascii=False, default=str) + "\n")
        print(common.utc(), kind, flush=True)

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = str(proc.info.get("name") or "").lower()
        if (name == "emulator" or "qemu" in name) and ("-start" in (proc.info.get("cmdline") or []) or "qemu" in name):
            raise RuntimeError(f"unowned emulator running pid={proc.pid}")
    if psutil.virtual_memory().available < 4 * 1024**3:
        raise RuntimeError("wait for root: less than 4 GiB available")
    initial = await asyncio.to_thread(hdc_list_targets)
    assert not any(t.status.lower() == "connected" for t in initial), "unowned connected HDC target"
    report["initial_hdc_targets"] = [asdict(t) for t in initial]
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.harmony_vm_orphan_cleanup is True
    assert Path(settings.storage_dir).resolve() == (root / "storage").resolve()
    host_id = load_host_identity(settings.storage_dir)
    host_file = Path(settings.storage_dir) / "agent-host-id"
    host_hash = common.digest(host_file)
    report["host_identity"] = {"id": host_id, "path": str(host_file), "sha256": host_hash}
    await db.dispose_engine()
    db.init_engine(db_url=str(settings.db_url))
    async with db.get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    image_id = "Phone|HarmonyOS 6.0.31(23)|arm64"
    async with db.get_session_factory()() as session:
        session.add(HarmonyVmCatalogSnapshot(id="official", source_type="deveco_emulator_official",
            device_types_json=["Phone"], images_json=[{"id": image_id, "device_type": "Phone", "os_version": "HarmonyOS 6.0.31(23)", "api_version": "23", "abi": "arm64"}],
            screen_profiles_json=[{"id": "Phone|Default", "device_type": "Phone", "name": "Default Phone", "width": 1080, "height": 2340, "density": 420, "supported_image_ids": [image_id], "create_methods": {image_id: "default"}}]))
        await session.commit()
    app = FastAPI()
    app.state.hub = Hub()
    app.state.lock_store = DeviceLockStore()
    include_routers(app)
    include_ws(app)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen(128); listener.setblocking(False)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    await common.eventually(lambda: asyncio.sleep(0, result=server.started), bool)
    owned = set()
    native_ports = set()
    probe_ports = set()
    probe_observations = []
    observation_futures = []
    observation_lock = threading.Lock()
    observation_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="own-port-observer")
    manager = HarmonyVmManager(runtime_dir=root / "agent-runtime")
    agents = []
    tasks = []
    receive_log = []
    inventories = []
    prehello_checks = []
    prehello_errors = []
    terminal_stops = []
    dropped = asyncio.Event()
    drop_next_stop_ack = False
    delete_commands = 0
    delete_requested = False
    vm_id = ""
    config_path = None

    def bind_comparison(number, reuse):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if reuse:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", number))
                return {"bindable": True}
            except OSError as error:
                return {"bindable": False, "errno": error.errno, "error": str(error)}

    def side_channel_snapshot(sample):
        number = sample["port"]
        value = {**sample, "side_channel_started_unix": time.time()}
        with socket.socket() as sock:
            sock.settimeout(.15)
            value["connect_ex"] = sock.connect_ex(("127.0.0.1", number))
        value["plain_bind_at_snapshot"] = bind_comparison(number, False)
        value["reuse_bind_at_snapshot"] = bind_comparison(number, True)
        for name, args in (("netstat", ["/usr/sbin/netstat", "-anv", "-p", "tcp"]),
                           ("lsof", ["/usr/sbin/lsof", "-nP", f"-iTCP:{number}"])):
            started = time.monotonic()
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=1)
                lines = result.stdout.splitlines()
                if name == "netstat":
                    lines = [line for line in lines if re.search(r"[.:]" + str(number) + r"(?:\s|$)", line)]
                value[name] = {"args": args, "returncode": result.returncode, "lines": lines,
                               "stderr": result.stderr, "elapsed_sec": round(time.monotonic() - started, 6)}
            except Exception as error:
                value[name] = {"args": args, "error": repr(error), "elapsed_sec": round(time.monotonic() - started, 6)}
        # Only exact own instance process commands are retained. Other process
        # command lines are neither saved nor acted on.
        value["owned_processes"] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            args = proc.info.get("cmdline") or []
            if any(str(root) in arg or ("aiphone_harmony_" + vm_id) in arg for arg in args) and any(
                    key in str(proc.info.get("name") or "").lower() for key in ("emulator", "qemu", "crash")):
                value["owned_processes"].append(proc.info)
        value["side_channel_finished_unix"] = time.time()
        common.write_json(root / f"port-window-{sample['sample_index']}.json", value)
        return value

    def observe_manager(mgr):
        original = mgr._port_is_free
        def observed(number):
            # Preserve the exact real return value. Only two quick bind/close
            # comparisons are synchronous; netstat/lsof run on a bounded
            # side channel and do not extend or alter the product deadline.
            returned = original(number)
            if number not in probe_ports:
                return returned
            with observation_lock:
                if len(probe_observations) >= 3:
                    return returned
            started = time.monotonic()
            plain = bind_comparison(number, False)
            if not returned or not plain["bindable"]:
                reuse = bind_comparison(number, True)
                with observation_lock:
                    if len(probe_observations) < 3:
                        sample = {"sample_index": len(probe_observations) + 1, "vm_id": vm_id, "port": number,
                                  "probe_unix": time.time(), "real_product_return": returned,
                                  "plain_bind": plain, "reuse_bind": reuse,
                                  "synchronous_observer_sec": round(time.monotonic() - started, 6)}
                        probe_observations.append(sample)
                        observation_futures.append(observation_pool.submit(side_channel_snapshot, sample))
            return returned
        mgr._port_is_free = observed

    def assert_private(mgr):
        assert mgr.instance_path.resolve() == (root / "agent-runtime/instances").resolve()
        assert set(mgr._known).issubset(owned) and set(mgr._runtimes).issubset(owned)
        for key, row in mgr._known.items():
            assert row["instance_name"] == "aiphone_harmony_" + key
            assert Path(row["instance_path"]).resolve().is_relative_to(root.resolve())

    class RecordedAgent(AgentWSClient):
        async def send(self, payload):
            nonlocal drop_next_stop_ack
            if payload.get("type") == P.MSG_HARMONY_VM_STATUS and payload.get("state") == "stopped":
                terminal_stops.append(copy.deepcopy(payload))
                if drop_next_stop_ack:
                    assert payload.get("vm_id") in owned and payload.get("details", {}).get("cleanup_confirmed") is True
                    drop_next_stop_ack = False
                    record("injected_lost_terminal_stop_ack", payload)
                    dropped.set()
                    return True
            if payload.get("type") == P.MSG_HARMONY_VM_RECONCILE:
                inventories.append(copy.deepcopy(payload))
            if str(payload.get("type", "")).startswith("harmony_vm"):
                record("agent_send", payload)
            return await super().send(payload)

    def new_agent(mgr, name, inventory=False, running_reclaim=False):
        assert_private(mgr)
        observe_manager(mgr)
        assert load_host_identity(settings.storage_dir) == host_id and common.digest(host_file) == host_hash
        def devices():
            assert_private(mgr)
            infos = [rt.driver.device_info() for key, rt in list(mgr._runtimes.items()) if key in owned and rt.ready and rt.driver]
            return [info.to_dict() for info in mgr.decorate_devices(infos)]
        client = RecordedAgent(ws_url=f"ws://127.0.0.1:{port}/ws/agent", server_http_base=base, token=token,
            agent_id=name, agent_name=name, host_id=host_id, device_provider=devices, ping_interval=5, rescan_interval=15)
        for message_type, handler in ((P.MSG_HARMONY_VM_CAPABILITY_PROBE, mgr.handle_capability_probe),
                                     (P.MSG_HARMONY_VM_START, mgr.handle_start), (P.MSG_HARMONY_VM_STOP, mgr.handle_stop),
                                     (P.MSG_HARMONY_VM_DELETE, mgr.handle_delete)):
            async def checked(c, message, message_type=message_type, handler=handler):
                nonlocal delete_commands
                assert_private(mgr)
                if message_type != P.MSG_HARMONY_VM_CAPABILITY_PROBE:
                    assert message.get("vm_id") in owned
                if message_type == P.MSG_HARMONY_VM_START:
                    assigned = message.get("assigned_port")
                    assert isinstance(assigned, int) and 10000 <= assigned <= 16555
                    probe_ports.add(assigned)
                if message_type in {P.MSG_HARMONY_VM_CAPABILITY_PROBE, P.MSG_HARMONY_VM_START}:
                    allowed = {rt.hdc_serial for key, rt in mgr._runtimes.items() if key in owned}
                    targets = await asyncio.to_thread(hdc_list_targets)
                    assert all(t.status.lower() != "connected" or t.serial in allowed for t in targets), "unowned connected HDC target appeared"
                if message_type == P.MSG_HARMONY_VM_DELETE:
                    delete_commands += 1
                    assert delete_commands == 1, "duplicate cleanup instruction"
                receive_log.append({"agent_id": name, "type": message_type, "message": copy.deepcopy(message)})
                record("agent_receive", receive_log[-1])
                await handler(c, message)
            client.on(message_type, checked)
        if inventory or running_reclaim:
            async def prehello_reclaim(c):
                assert_private(mgr)
                before = copy.deepcopy(mgr._known)
                try:
                    reclaimed = await asyncio.to_thread(mgr.reconcile_running_vms_sync)
                    after = copy.deepcopy(mgr._known)
                    check = {"agent_id": name, "registry_before": before, "registry_after": after,
                             "reclaimed_ids": [rt.vm_id for rt in reclaimed],
                             "runtime_ids": sorted(mgr._runtimes), "phase": "pre_hello",
                             "scenario": "running_process_handle_absent" if running_reclaim else "legacy_stopped",
                             "process_handles": {rt.vm_id: rt.process is not None for rt in reclaimed}}
                    prehello_checks.append(check)
                    record("real_prehello_reclaim", check)
                    assert_private(mgr)
                    if running_reclaim:
                        assert [rt.vm_id for rt in reclaimed] == [vm_id] and set(mgr._runtimes) == {vm_id}
                        rt = mgr._runtimes[vm_id]
                        assert rt.process is None and rt.ready is True, "fresh manager must actually reclaim without a Popen handle"
                        for field in ("vm_id", "instance_name", "instance_path", "hdc_serial", "hdc_port", "lease_token"):
                            assert after[vm_id][field] == before[vm_id][field], field
                    else:
                        assert before == after and not reclaimed and not mgr._runtimes, "real upgrade hook changed the injected stopped registry"
                        assert "cleanup_confirmed" not in mgr._known[vm_id] and mgr._known[vm_id]["ready"] is False
                except Exception as error:
                    prehello_errors.append(repr(error))
                    record("real_prehello_failure", {"error": repr(error), "traceback": traceback.format_exc()})
                    await c.stop()
                    raise
            async def report_inventory(c):
                assert_private(mgr)
                assert not prehello_errors and prehello_checks, "pre-hello upgrade must complete before inventory"
                if running_reclaim:
                    await mgr.report_reclaimed_vms(c, rescan=False)
                await mgr.report_orphan_reconcile(c)
            client.on_pre_hello(prehello_reclaim)
            client.on_connect(report_inventory)
        agents.append(client)
        tasks.append(asyncio.create_task(client.run_forever()))
        return client, tasks[-1]

    async def snapshot():
        if prehello_errors:
            raise AssertionError({"prehello_errors": prehello_errors})
        async with db.get_session_factory()() as session:
            vm = await session.get(HarmonyVmInstance, vm_id, populate_existing=True)
            if vm is None:
                return {"absent": True}
            lease = await session.get(HarmonyVmPortLease, vm.hdc_port) if vm.hdc_port else None
            return {"state": vm.state, "assigned_agent_id": vm.assigned_agent_id,
                    "error_code": vm.error_code, "error_message": vm.error_message,
                    "runtime": copy.deepcopy(vm.runtime), "lease_token": vm.lease_token,
                    "hdc_serial": vm.hdc_serial, "hdc_port": vm.hdc_port,
                    "lease_row": ({"vm_id": lease.vm_id, "agent_id": lease.agent_id, "lease_token": lease.lease_token, "state": lease.state} if lease else None)}

    async def direct_lease_row(number):
        """Query the saved lease key independently of the VM's mutable fields."""
        async with db.get_session_factory()() as session:
            row = await session.get(HarmonyVmPortLease, number, populate_existing=True)
            return ({"port": row.port, "vm_id": row.vm_id, "agent_id": row.agent_id,
                     "lease_token": row.lease_token, "state": row.state} if row else None)

    def native_state(mgr):
        assert_private(mgr)
        processes = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            args = proc.info.get("cmdline") or []
            name = str(proc.info.get("name") or "").lower()
            if any(str(root) in a or ("aiphone_harmony_" + vm_id) in a for a in args) and any(s in name for s in ("emulator", "qemu", "crash")):
                processes.append(proc.pid)
        ports = {}
        for number in native_ports:
            with socket.socket() as sock:
                sock.settimeout(.3); listening = sock.connect_ex(("127.0.0.1", number)) == 0
            with socket.socket() as sock:
                try: sock.bind(("127.0.0.1", number)); free = True
                except OSError: free = False
            ports[str(number)] = {"listening": listening, "bindable": free}
        return {"instance_directory_absent": config_path is not None and not config_path.parent.exists(),
                "registry_absent": vm_id not in mgr._known, "runtime_absent": vm_id not in mgr._runtimes,
                "owned_processes": processes, "ports": ports}

    def clean(value):
        return value["instance_directory_absent"] and value["registry_absent"] and value["runtime_absent"] and not value["owned_processes"] and all(not p["listening"] and p["bindable"] for p in value["ports"].values())

    async def marker_io(rt, command):
        assert rt.vm_id in owned and Path(rt.instance_path).resolve().is_relative_to(root.resolve())
        value = await asyncio.to_thread(hdc_run, "shell", command, serial=rt.hdc_serial, timeout=10)
        record("own_guest_shell", {"serial": rt.hdc_serial, "command": command, "result": value})
        return value

    auth = {"Authorization": "Bearer " + token}
    first_agent = "personal-legacy-old-" + uuid.uuid4().hex[:12]
    second_agent = "personal-legacy-new-" + uuid.uuid4().hex[:12]
    third_agent = "personal-running-reclaim-" + uuid.uuid4().hex[:12]
    client_a, task_a = new_agent(manager, first_agent)
    try:
        await common.eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(first_agent)), bool)
        async with AsyncClient(base_url=base, timeout=70, trust_env=False) as http:
            async def request(method, path, **kwargs):
                response = await http.request(method, path, headers=auth, **kwargs)
                record("http", {"method": method, "path": path, "status": response.status_code, "body": response.json()})
                assert response.is_success, response.text
                return response.json()
            async def wait_state(expected, timeout=300):
                async def read():
                    value = await snapshot()
                    if value.get("state") == "error":
                        raise AssertionError(value)
                    return value
                return await common.eventually(read, lambda v: v.get("state") == expected, timeout=timeout, interval=.5)
            created = await request("POST", API, json={"alias": "PERSONAL-legacy-" + root.name[-10:], "os_version": "6.0.31", "api_version": "23", "abi": "arm64", "memory_gb": 2, "storage_gb": 4})
            vm_id = created["id"]; owned.add(vm_id); report["vm_id"] = vm_id
            assert created["state"] == "draft" and not created["assigned_agent_id"]
            await request("POST", f"{API}/{vm_id}/dispatch", json={"agent_id": first_agent})
            first = await wait_state("running")
            saved_lease_port, saved_lease_token = first["hdc_port"], first["lease_token"]
            saved_lease = await direct_lease_row(saved_lease_port)
            assert saved_lease and saved_lease["vm_id"] == vm_id and saved_lease["lease_token"] == saved_lease_token
            rt = manager._runtimes[vm_id]
            config_path = Path(rt.instance_path) / rt.instance_name / "config.ini"
            native_ports.update(p for p in (rt.hdc_port, rt.driver_fport_port) if p)
            original_uuid = re.search(r"(?m)^uuid\s*=\s*(.+)$", config_path.read_text()).group(1).strip()
            guest_path = "/data/local/tmp/aiphone-personal-legacy-" + vm_id + ".txt"
            marker = secrets.token_hex(20)
            await marker_io(rt, "printf %s " + shlex.quote(marker) + " > " + shlex.quote(guest_path))
            assert (await marker_io(rt, "cat " + shlex.quote(guest_path))).strip() == marker
            report["initial"] = {"uuid": original_uuid, "server": first, "marker_path": guest_path, "marker_sha256": hashlib.sha256(marker.encode()).hexdigest()}
            drop_next_stop_ack = True
            await request("POST", f"{API}/{vm_id}/stop")
            await asyncio.wait_for(dropped.wait(), 100)
            assert len(terminal_stops) == 1 and manager._known[vm_id]["cleanup_confirmed"] is True
            unacked = await snapshot()
            assert unacked["state"] == "stopping" and unacked["lease_token"] == first["lease_token"]
            assert config_path.is_file() and vm_id not in manager._runtimes
            record("native_stopped_server_ack_missing", {"server": unacked, "native": native_state(manager)})
            await client_a.stop(); await asyncio.wait_for(task_a, 15)
            await common.eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(first_agent)), lambda value: not value)
            await common.eventually(snapshot, lambda row: not row.get("runtime", {}).get("operation", {}).get("pending"))
            assert managed_vm_owner(first["hdc_serial"]) is None
            assert_private(manager)
            before_registry = json.loads(manager.registry_path.read_text())
            assert set(before_registry) == {vm_id}
            legacy = copy.deepcopy(before_registry)
            legacy[vm_id]["ready"] = False
            legacy[vm_id].pop("cleanup_confirmed", None)
            legacy[vm_id].pop("exit_evidence", None)
            manager.registry_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2))
            async with db.get_session_factory()() as session:
                vm = await session.get(HarmonyVmInstance, vm_id)
                assert vm.assigned_agent_id == first_agent and vm.lease_token == first["lease_token"]
                vm.runtime = {key: value for key, value in vm.runtime.items() if key != "owner_host_id"}
                await session.commit()
            before_handoff = await snapshot()
            record("injected_legacy_owned_data_only", {"registry_before": before_registry, "registry_after": legacy, "server": before_handoff})
            manager = HarmonyVmManager(runtime_dir=root / "agent-runtime")
            assert_private(manager)
            assert "cleanup_confirmed" not in manager._known[vm_id] and manager._known[vm_id]["ready"] is False
            before_commands = len(receive_log)
            client_b, task_b = new_agent(manager, second_agent, inventory=True)
            adopted = await common.eventually(snapshot, lambda row: row.get("assigned_agent_id") == second_agent, timeout=30)
            assert prehello_checks and not prehello_errors
            assert inventories and inventories[-1]["inventory_complete"] is True
            assert len(inventories[-1]["instances"]) == 1
            item = inventories[-1]["instances"][0]
            assert item["vm_id"] == vm_id and item["details"]["cleanup_confirmed"] is False
            assert item["details"]["managed_instance_present"] is True
            assert Path(item["details"]["instance_path"]).resolve().is_relative_to(root.resolve())
            for key in ("state", "error_code", "error_message", "lease_token", "hdc_serial", "hdc_port", "lease_row"):
                assert adopted[key] == before_handoff[key], (key, adopted, before_handoff)
            assert adopted["runtime"]["last_status"] == before_handoff["runtime"]["last_status"]
            assert adopted["runtime"]["owner_host_id"] == host_id
            assert not receive_log[before_commands:], "inventory must not cause native lifecycle commands"
            lease_after_handoff = await direct_lease_row(saved_lease_port)
            assert lease_after_handoff == saved_lease
            report["ownership_only_handoff"] = {"before": before_handoff, "after": adopted, "inventory": inventories[-1], "prehello_checks": copy.deepcopy(prehello_checks),
                "direct_saved_lease_query": {"port": saved_lease_port, "token": saved_lease_token, "row": lease_after_handoff}, "same_host_file_sha256": common.digest(host_file)}
            record("ownership_only_handoff_confirmed", report["ownership_only_handoff"])
            await request("POST", f"{API}/{vm_id}/stop")
            stopped = await wait_state("stopped", timeout=60)
            assert len(terminal_stops) == 2
            receipt = terminal_stops[-1]
            exit_proof = receipt["details"]["exit_evidence"]
            assert receipt["details"]["cleanup_confirmed"] is True and exit_proof["source"] == "registry_instance_absence"
            assert exit_proof["instance_process_status"] == "absent" and exit_proof["last_sample"]["port_bindable"] is True
            assert stopped["lease_token"] is None and stopped["hdc_port"] is None and stopped["lease_row"] is None
            removed_lease = await direct_lease_row(saved_lease_port)
            assert removed_lease is None, {"saved_port": saved_lease_port, "saved_token": saved_lease_token, "remaining_row": removed_lease}
            assert stopped["assigned_agent_id"] == second_agent and config_path.is_file()
            report["fresh_stop_released_legacy_lease"] = {"server": stopped, "receipt": receipt,
                "independent_saved_lease_query": {"port": saved_lease_port, "token": saved_lease_token, "row": removed_lease}}
            await request("POST", f"{API}/{vm_id}/start")
            restarted = await wait_state("running")
            rt = manager._runtimes[vm_id]
            native_ports.update(p for p in (rt.hdc_port, rt.driver_fport_port) if p)
            assert restarted["assigned_agent_id"] == second_agent
            assert re.search(r"(?m)^uuid\s*=\s*(.+)$", config_path.read_text()).group(1).strip() == original_uuid
            assert (await marker_io(rt, "cat " + shlex.quote(guest_path))).strip() == marker
            report["restart_data_preserved"] = {"vm_id": vm_id, "uuid": original_uuid, "marker_sha256": hashlib.sha256(marker.encode()).hexdigest(), "matched": True}
            record("restart_marker_and_uuid_preserved", report["restart_data_preserved"])
            # Change only the Agent session and Manager object. Keep the real
            # guest running. The former Popen is retained solely as independent
            # evidence, never assigned to the new Manager or used to stop it.
            old_process = rt.process
            assert old_process is not None and old_process.poll() is None
            live_lease = (restarted["hdc_port"], restarted["hdc_serial"], restarted["lease_token"])
            await client_b.stop(); await asyncio.wait_for(task_b, 15)
            await common.eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(second_agent)), lambda value: not value)
            await common.eventually(snapshot, lambda row: row.get("state") == "agent_offline")
            assert old_process.poll() is None, "the guest must stay running while its session changes"
            manager = HarmonyVmManager(runtime_dir=root / "agent-runtime")
            assert_private(manager)
            assert not manager._runtimes and manager._known[vm_id]["ready"] is True
            running_command_boundary = len(receive_log)
            client_c, task_c = new_agent(manager, third_agent, running_reclaim=True)
            reclaimed_server = await common.eventually(snapshot,
                lambda row: row.get("assigned_agent_id") == third_agent and row.get("state") == "running", timeout=60)
            claimed = manager._runtimes[vm_id]
            assert claimed.process is None and claimed.ready is True
            assert (reclaimed_server["hdc_port"], reclaimed_server["hdc_serial"], reclaimed_server["lease_token"]) == live_lease
            assert old_process.poll() is None
            assert not receive_log[running_command_boundary:], "reclaim must not restart or stop the guest"
            assert (await marker_io(claimed, "cat " + shlex.quote(guest_path))).strip() == marker
            report["running_reclaim_without_process_handle"] = {
                "previous_agent": second_agent, "new_agent": third_agent, "host_id": host_id,
                "host_file_sha256": common.digest(host_file), "old_process_pid": old_process.pid,
                "old_process_still_running": True, "new_runtime_process_is_none": True,
                "prehello_check": copy.deepcopy(prehello_checks[-1]), "server": reclaimed_server,
                "marker_still_matches": True, "scope": "fresh Manager/session, not OS process kill"}
            record("running_reclaim_without_process_handle_confirmed", report["running_reclaim_without_process_handle"])
            await request("POST", f"{API}/{vm_id}/stop")
            final_stopped = await wait_state("stopped", timeout=90)
            stop_without_handle = terminal_stops[-1]
            assert stop_without_handle["details"]["cleanup_confirmed"] is True
            no_handle_exit = stop_without_handle["details"]["exit_evidence"]
            assert no_handle_exit["process_handle_present"] is False
            assert no_handle_exit["instance_process_status"] == "absent"
            assert no_handle_exit["instance_process_sampled_unix"] >= no_handle_exit["started_unix"]
            own_native_stopped = native_state(manager)
            assert not own_native_stopped["owned_processes"] and old_process.poll() is not None
            assert manager._active_instance_serial(vm_id, manager._known[vm_id]) == ""
            assert await direct_lease_row(live_lease[0]) is None
            report["stop_after_running_reclaim"] = {"server": final_stopped, "receipt": stop_without_handle,
                "independent_native_state": own_native_stopped, "original_process_returncode": old_process.returncode,
                "independent_saved_lease_query": {"port": live_lease[0], "token": live_lease[2], "row": None}}
            delete_requested = True
            deletion = await request("DELETE", f"{API}/{vm_id}")
            assert deletion.get("deleted") and deletion.get("cleanup_sent")
            assert (await http.get(f"{API}/{vm_id}", headers=auth)).status_code == 404
            final_native = await common.eventually(lambda: asyncio.to_thread(native_state, manager), clean, timeout=90)
            assert delete_commands == 1 and common.digest(host_file) == host_hash
            report["cleanup"] = {"server_404": True, "delete_commands": delete_commands, **final_native}
            report["status"] = "passed"
    except BaseException as error:
        report["error"] = repr(error); report["traceback"] = traceback.format_exc()
        record("failure", report["error"])
        raise
    finally:
        for key in list(owned):
            assert_private(manager)
            if key in manager._known or key in manager._runtimes:
                try:
                    if delete_requested:
                        report["existing_delete_completion"] = await common.eventually(lambda: asyncio.to_thread(native_state, manager), clean, timeout=90)
                    else:
                        report["safety_cleanup"] = await asyncio.to_thread(manager.delete_sync, key)
                except Exception as error: report["safety_cleanup_error"] = repr(error)
        for client in agents: await client.stop()
        for task in tasks:
            with suppress(Exception, asyncio.CancelledError): await asyncio.wait_for(task, 10)
        server.should_exit = True
        with suppress(Exception, asyncio.CancelledError): await asyncio.wait_for(server_task, 10)
        listener.close(); await close_all_sessions(); await db.dispose_engine()
        observation_pool.shutdown(wait=True)
        report["port_probe_observations"] = []
        for future in observation_futures:
            try: report["port_probe_observations"].append(future.result())
            except Exception as error: report["port_probe_observations"].append({"observer_error": repr(error)})
        report["protected_after"] = common.protected_fingerprint()
        report["protected_unchanged"] = report["protected_after"] == protected
        report["source_hashes_at_end"] = sources()
        report["sources_unchanged"] = report["source_hashes_at_end"] == source_hashes
        if vm_id:
            report["final_native_state"] = native_state(manager)
            if not clean(report["final_native_state"]): report["status"] = "failed"
        if not report["protected_unchanged"] or not report["sources_unchanged"]: report["status"] = "failed"
        report["finished_at"] = common.utc()
        common.write_json(root / "result.json", report)
        print(json.dumps({"status": report["status"], "result": str(root / "result.json")}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(plan(), ensure_ascii=False, indent=2)); return
    root = args.run_root or Path("/tmp") / (PREFIX + uuid.uuid4().hex)
    if root.parent != Path("/tmp") or not root.name.startswith(PREFIX) or not str(root).isascii():
        parser.error("new ASCII /tmp/" + PREFIX + "* root required")
    root.mkdir(mode=0o700, exist_ok=False)
    common.write_json(root / "plan.json", plan())
    result = asyncio.run(execute(root))
    if result["status"] != "passed": raise SystemExit(1)


if __name__ == "__main__": main()
