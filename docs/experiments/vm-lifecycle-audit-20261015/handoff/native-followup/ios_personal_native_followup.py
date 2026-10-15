#!/usr/bin/env python3
"""Personal-only iOS followup acceptance; original 74f-era evidence is preserved.

Default is a non-mutating plan. --execute-personal-ios must be explicitly used
after the coordinating agent has approved readiness. All simctl invocations go
through a fail-closed xcrun shim to an isolated device set. No normal Agent main,
company configuration, VLM, business package, physical device, or default
Simulator set is used. The tiny deterministic run handler is deliberate: this
checks real run routing and native input, not the production VLM/Runner.
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
import xml.etree.ElementTree as ET


AUDIT = Path(__file__).resolve().parents[2]
EXECUTION = AUDIT / "execution"
REPO = AUDIT.parents[2]
PERSONAL = Path(__file__).resolve().parent / "ios-runs"
DEVICE_SET = Path("/tmp/aiphone-personal-ios-plan").resolve()
FIXTURE = EXECUTION / "personal-fixture.html"
DEVICE_TYPE = "com.apple.CoreSimulator.SimDeviceType.iPhone-16e"
RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"


# This shim is not a simulator fake. It invokes Apple's real simctl, adding the
# explicit device-set path and rejecting every lifecycle target except exact
# names/UDIDs created by this run. Direct WDA launch helper calls are covered too.
XCRUN_GUARD = r'''#!/usr/bin/python3
import fcntl, json, os, pathlib, subprocess, sys, time
manifest = pathlib.Path(os.environ["PERSONAL_IOS_MANIFEST"])
log = pathlib.Path(os.environ["PERSONAL_IOS_NATIVE_LOG"])
args = sys.argv[1:]
if "simctl" in args and (not args or args[0] != "simctl"):
    raise SystemExit("personal guard: prefixed simctl invocation forbidden")
if not args or args[0] != "simctl":
    os.execv("/usr/bin/xcrun", ["/usr/bin/xcrun", *args])
args = args[1:]
if not args or args[0].startswith("-"):
    raise SystemExit("personal guard: simctl set overrides/options forbidden")
with open(str(manifest) + ".lock", "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    data = json.loads(manifest.read_text())
    device_set = pathlib.Path(data["device_set"]).resolve()
    if device_set != pathlib.Path(os.environ["PERSONAL_IOS_DEVICE_SET"]).resolve():
        raise SystemExit("personal guard: device set mismatch")
    command = args[0]
    readonly = command in {"list", "help"}
    creating = command == "create"
    allowed = data["created"]
    if creating:
        if len(args) != 4 or args[1] not in ["aiphone_sim_" + vm for vm in data["vm_ids"]]:
            raise SystemExit("personal guard: create name not explicitly owned")
    elif not readonly:
        allowed_commands = {"boot", "bootstatus", "shutdown", "delete", "install", "terminate", "get_app_container", "listapps", "openurl", "io", "launch", "appinfo", "status_bar", "ui"}
        if command not in allowed_commands:
            raise SystemExit("personal guard: unsupported simctl operation: " + command)
        target_index = 2 if command == "launch" and len(args) > 1 and args[1] == "--terminate-running-process" else 1
        if len(args) <= target_index or args[target_index] not in allowed:
            raise SystemExit("personal guard: operation target not owned: " + repr(args))
        target = args[target_index]
        query = subprocess.run(["/usr/bin/xcrun", "simctl", "--set", str(device_set), "list", "devices", "-j"], capture_output=True, text=True, timeout=30)
        if query.returncode:
            raise SystemExit("personal guard: cannot establish actual ownership")
        inventory = json.loads(query.stdout)["devices"]
        hit = next((d for rows in inventory.values() for d in rows if d["udid"] == target), None)
        if hit is not None and hit["name"] != allowed[target]:
            raise SystemExit("personal guard: native name/UDID identity mismatch")
        if command == "delete" and len(args) != 2:
            raise SystemExit("personal guard: only one exact delete target allowed")
    real = ["/usr/bin/xcrun", "simctl", "--set", str(device_set), *args]
    if creating:
        result = subprocess.run(real, capture_output=True, text=True)
        if result.returncode == 0:
            udid = result.stdout.strip()
            if len(udid) != 36:
                raise SystemExit("personal guard: invalid created UDID")
            data["created"][udid] = args[1]
            tmp = manifest.with_suffix(".next")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, manifest)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        with log.open("a") as out:
            out.write(json.dumps({"ts":time.time(),"command":real,"returncode":result.returncode}) + "\n")
        raise SystemExit(result.returncode)
with log.open("a") as out:
    out.write(json.dumps({"ts":time.time(),"command":real}) + "\n")
os.execv("/usr/bin/xcrun", real)
'''


def atomic_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".next")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


class Evidence:
    def __init__(self, folder: Path):
        self.folder = folder
        self.started = time.monotonic()
        self.value = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "scope": "personal localhost only; isolated simulator device set; real HTTP/WS/native input; deterministic run handler; no VLM/Onion/physical devices",
            "device_set": str(DEVICE_SET),
            "phases": [],
            "source_sha256": {
                str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [
                    Path(__file__).resolve(), FIXTURE,
                    *[REPO / "backend" / rel for rel in (
                        "ai_phone/agent/ios_sim/manager.py",
                        "ai_phone/agent/ios_sim/capability.py",
                        "ai_phone/agent/drivers/ios_simulator.py",
                        "ai_phone/agent/drivers/ios_simulator_driver.py",
                        "ai_phone/agent/drivers/ios_simulator_wda.py",
                        "ai_phone/agent/drivers/wda_client.py",
                        "ai_phone/agent/drivers/simctl.py",
                        "ai_phone/agent/health/probe.py",
                        "ai_phone/agent/identity.py",
                        "ai_phone/agent/device_activity.py",
                        "ai_phone/agent/vm_ports.py",
                        "ai_phone/agent/ws_client.py",
                        "ai_phone/config.py",
                        "ai_phone/server/ios_sim/api.py",
                        "ai_phone/server/ios_sim/service.py",
                        "ai_phone/server/ios_sim/models.py",
                        "ai_phone/server/db.py",
                        "ai_phone/server/models.py",
                        "ai_phone/server/app.py",
                        "ai_phone/server/api/_deps.py",
                        "ai_phone/server/ws/_deps.py",
                        "ai_phone/server/vm_lifecycle.py",
                        "ai_phone/server/hub.py",
                        "ai_phone/server/ws/agent_ws.py",
                    )],
                ]
            },
        }

    def record(self, phase: str, **details: object) -> None:
        entry = {"phase": phase, "elapsed_seconds": round(time.monotonic() - self.started, 3), **details}
        self.value["phases"].append(entry)
        atomic_json(self.folder / "result.json", self.value)
        print(json.dumps(entry, ensure_ascii=False, default=str), flush=True)


def prepare_isolation(folder: Path) -> Path:
    global DEVICE_SET
    # No inherited project configuration is loaded. Imports occur after chdir.
    for key in list(os.environ):
        if key.startswith("AI_PHONE_"):
            os.environ.pop(key)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    folder.mkdir(parents=True, exist_ok=False)
    DEVICE_SET = Path("/tmp/aiphone-personal-ios-" + folder.name.removeprefix("ios-")).resolve()
    DEVICE_SET.mkdir(parents=True, exist_ok=False)
    bin_dir = folder / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "xcrun"
    wrapper.write_text(XCRUN_GUARD, encoding="utf-8")
    wrapper.chmod(0o755)
    manifest = folder / "owned.json"
    atomic_json(manifest, {"device_set": str(DEVICE_SET), "vm_ids": [], "created": {}})
    os.environ.update({
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PERSONAL_IOS_MANIFEST": str(manifest),
        "PERSONAL_IOS_NATIVE_LOG": str(folder / "native-commands.ndjson"),
        "PERSONAL_IOS_DEVICE_SET": str(DEVICE_SET),
        "AI_PHONE_STORAGE_DIR": str(folder / "storage"),
        "AI_PHONE_DB_URL": "sqlite+aiosqlite:///" + str(folder / "personal.db"),
        "AI_PHONE_AGENT_TOKEN": secrets.token_urlsafe(24),
        "AI_PHONE_SUBMISSION_INTERNAL_TOKEN": secrets.token_urlsafe(24),
        "AI_PHONE_WDA_PROJECT_DIR": str(REPO / "third_party" / "WebDriverAgent"),
        "AI_PHONE_WDA_STARTUP_TIMEOUT_S": "180",
        "AI_PHONE_IOS_SIM_BOOT_TIMEOUT_SEC": "180",
    })
    os.chdir(folder)
    sys.path.insert(0, str(REPO / "backend"))
    return manifest


def allow_vm(manifest: Path, vm_id: str) -> None:
    with open(str(manifest) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(manifest.read_text())
        current["vm_ids"].append(vm_id)
        atomic_json(manifest, current)


async def eventually(call, predicate, *, timeout=30.0, delay=0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await call()
        if predicate(last):
            return last
        await asyncio.sleep(delay)
    if hasattr(last, "text"):
        last = (last.status_code, last.text)
    raise AssertionError(f"condition not met after {timeout}s; last={last!r}")


async def run(folder: Path, device_type: str, runtime_id: str) -> dict:
    manifest = prepare_isolation(folder)
    evidence = Evidence(folder)
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    from httpx import AsyncClient
    from loguru import logger
    from sqlalchemy.ext.asyncio import close_all_sessions
    import uvicorn
    from ai_phone.config import get_settings
    from ai_phone.server import db as db_module
    from ai_phone.server.api import include_routers
    from ai_phone.server.db import Base
    from ai_phone.server.hub import Hub
    from ai_phone.server.lockstore import DeviceLockStore
    from ai_phone.server.ws import include_ws
    from ai_phone.server.ws import agent_ws as server_agent_ws
    from ai_phone.agent.drivers import ios_simulator_wda as wda_module
    from ai_phone.agent.drivers.ios_simulator import list_ios_simulators
    from ai_phone.agent.drivers.ios_simulator_driver import IosSimulatorDriver, get_sim_endpoint
    from ai_phone.agent.drivers.simctl import simctl_run
    from ai_phone.agent.health.probe import IosSimProbe
    from ai_phone.agent.identity import load_host_identity
    from ai_phone.agent.ios_sim.manager import IosSimVmManager
    from ai_phone.agent.ws_client import AgentWSClient
    from ai_phone.shared import protocol as P

    get_settings.cache_clear()
    logger.add(str(folder / "runtime.log"), level="DEBUG")
    # Only the port domain changes for isolation; real reservation/allocation
    # and identity checks remain in use. Never probe a company WDA endpoint.
    wda_module._SIM_WDA_PORT_BASE, wda_module._SIM_WDA_PORT_LIMIT = 18300, 18309
    wda_module._SIM_MJPEG_PORT_BASE, wda_module._SIM_MJPEG_PORT_LIMIT = 19300, 19309
    for port in [*range(18300, 18310), *range(19300, 19310)]:
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))

    inventory = json.loads(await asyncio.to_thread(simctl_run, "list", "devices", "-j"))
    assert not any(inventory["devices"].values()), "Personal device set is non-empty; explicit cleanup review required"
    evidence.record("isolation", isolated_set_empty=True, source_root=str(REPO), default_set_access=False, private_wda_ports=[18300, 19300])

    app = FastAPI()
    app.state.hub = Hub()
    app.state.lock_store = DeviceLockStore()
    include_routers(app)
    include_ws(app)
    receipts = []

    @app.get("/fixture/")
    async def fixture():
        return HTMLResponse(FIXTURE.read_text(encoding="utf-8"))

    @app.post("/fixture/receipt")
    async def receipt(request: Request):
        item = await request.json()
        item["received_at"] = datetime.now(timezone.utc).isoformat()
        receipts.append(item)
        atomic_json(folder / "receipts.json", receipts)
        return {"ok": True, "index": len(receipts)}

    await db_module.dispose_engine()
    db_module.init_engine(db_url=get_settings().db_url)
    from ai_phone.server import models  # noqa: F401
    async with db_module.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await db_module.init_ios_sim_db()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="error", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    await eventually(lambda: asyncio.sleep(0, result=server.started), bool)
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": "Bearer " + get_settings().submission_internal_token}
    manager = IosSimVmManager(runtime_dir=folder / "manager", max_instances=1)
    agent_id = "personal-ios-" + folder.name
    host_id = load_host_identity(get_settings().storage_dir)
    host_identity_file = Path(get_settings().storage_dir) / "agent-host-id"
    host_identity_sha256 = hashlib.sha256(host_identity_file.read_bytes()).hexdigest()
    assert host_identity_file.resolve().is_relative_to(folder.resolve())
    assert host_identity_file.stat().st_mode & 0o777 == 0o600
    evidence.record("persistent_host_identity", path=str(host_identity_file), host_id=host_id,
                    sha256=host_identity_sha256, permissions="0600")
    case_id = folder.name
    token = "personal-" + uuid.uuid4().hex[:12]
    page_url = base + "/fixture/?" + urlencode({"run": case_id, "platform": "ios"})
    runs = {}
    drivers = {}
    vm_id = ""
    udid = ""
    agent = None
    agent_task = None

    def ui_event(action, **details):
        with (folder / "native-ui.ndjson").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"elapsed_seconds": round(time.monotonic() - evidence.started, 3), "action": action, **details}, ensure_ascii=False, default=str) + "\n")

    def devices():
        allowed = json.loads(manifest.read_text())["created"]
        infos = [info for info in list_ios_simulators() if info.serial in allowed]
        return [info.to_dict() for info in manager.decorate_devices(infos)]

    def acquire_driver(serial):
        assert serial in json.loads(manifest.read_text())["created"]
        endpoint = get_sim_endpoint(serial)
        assert endpoint is not None, "production manager did not register WDA"
        assert endpoint.wda_port in range(18300, 18310)
        driver = IosSimulatorDriver(serial, endpoint.client)
        original_request = endpoint.client._request
        def traced_request(*args, **kwargs):
            began = time.monotonic()
            response = original_request(*args, **kwargs)
            if any(part in args[1] for part in ("/wda/tap", "/wda/keys", "/wda/screen", "/keyboard/dismiss")):
                ui_event("wda_response", method=args[0], path=args[1], request=args[2] if len(args) > 2 else kwargs.get("json"), response=response, duration_seconds=round(time.monotonic() - began, 3))
            return response
        endpoint.client._request = traced_request
        endpoint.client.create_session(capabilities={"shouldWaitForQuiescence": False})
        endpoint.client.update_appium_settings({"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0})
        drivers[serial] = driver
        return driver

    def source(driver):
        sid = driver._wda._ensure_session()
        began = time.monotonic()
        xml = str(driver._wda._request("GET", f"/session/{sid}/source").get("value") or "")
        ui_event("source", duration_seconds=round(time.monotonic() - began, 3), bytes=len(xml))
        return xml

    def find_and_click(driver, predicate, label):
        deadline = time.monotonic() + 45
        stable_rect, stable_since = None, 0.0
        while time.monotonic() < deadline:
            xml = source(driver)
            (folder / f"source-{label}.xml").write_text(xml, encoding="utf-8")
            for element in ET.fromstring(xml).iter():
                attr = element.attrib
                if predicate(attr) and attr.get("visible") != "false":
                    rect = tuple(attr.get(k) for k in ("x", "y", "width", "height"))
                    if rect != stable_rect:
                        stable_rect, stable_since = rect, time.monotonic()
                        break
                    if time.monotonic() - stable_since < 1.5:
                        break
                    x = float(attr.get("x", 0)) + float(attr.get("width", 0)) / 2
                    y = float(attr.get("y", 0)) + float(attr.get("height", 0)) / 2
                    scale = driver._get_scale()
                    (folder / f"before-{label}-tap.png").write_bytes(driver.screenshot_png())
                    ui_event("tap", label=label, rect=attr, scale=scale, point=[x, y], pixel=[round(x * scale), round(y * scale)])
                    driver.click(round(x * scale), round(y * scale))
                    return attr
            # Handle only known first-launch Safari choices on this owned sim.
            for element in ET.fromstring(xml).iter():
                attr = element.attrib
                text = attr.get("label") or attr.get("name")
                if attr.get("type") == "XCUIElementTypeButton" and text in {"Continue", "继续", "Not Now", "以后", "稍后", "Start Browsing", "开始浏览"} and attr.get("visible") != "false":
                    scale = driver._get_scale()
                    driver.click(round((float(attr["x"]) + float(attr["width"]) / 2) * scale), round((float(attr["y"]) + float(attr["height"]) / 2) * scale))
                    break
            time.sleep(0.5)
        (folder / f"failure-{label}.png").write_bytes(driver.screenshot_png())
        raise AssertionError("Could not find visible UI element: " + label)

    def wait_source(driver, predicate, label, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            xml = source(driver)
            (folder / f"source-{label}.xml").write_text(xml, encoding="utf-8")
            if predicate(ET.fromstring(xml)):
                return xml
            time.sleep(0.5)
        (folder / f"failure-{label}.png").write_bytes(driver.screenshot_png())
        raise AssertionError("Native UI did not reach expected condition: " + label)

    def visible_saved_result(root):
        values = {
            str(e.attrib.get("value") or e.attrib.get("name") or e.attrib.get("label") or "")
            for e in root.iter()
            if e.attrib.get("type") == "XCUIElementTypeStaticText" and e.attrib.get("visible") == "true"
        }
        return {token, "确认次数：", "1", "本地回执已记录"}.issubset(values)

    def execute_case(serial):
        driver = acquire_driver(serial)
        simctl_run("openurl", serial, page_url, timeout=30)
        def input_field(a):
            return a.get("type") == "XCUIElementTypeTextField" and (a.get("label") == "输入本轮标记" or a.get("name") == "输入本轮标记" or a.get("value") == "输入本轮标记")
        find_and_click(driver, input_field, "input")
        # A first-ever Safari keyboard activation is asynchronous. Observe
        # focus/keyboard readiness before typing; a tap ACK is not that proof.
        def keyboard_ready(root):
            return any(e.attrib.get("type") == "XCUIElementTypeKeyboard" and e.attrib.get("visible") == "true" for e in root.iter())
        try:
            wait_source(driver, keyboard_ready, "keyboard-ready", timeout=8)
        except AssertionError:
            # Re-read the native target once after first-launch Safari settles.
            # Re-focusing an empty input is harmless; never repeat confirmation.
            ui_event("focus_retry", reason="keyboard not yet visible after stable target tap")
            find_and_click(driver, input_field, "input-focus-retry")
            wait_source(driver, keyboard_ready, "keyboard-ready-after-refocus", timeout=15)
        driver.type_text(token)
        wait_source(driver, lambda root: any(e.attrib.get("type") == "XCUIElementTypeTextField" and e.attrib.get("value") == token for e in root.iter()), "typed-token")
        (folder / "before-confirm.png").write_bytes(driver.screenshot_png())
        button = find_and_click(driver, lambda a: a.get("type") == "XCUIElementTypeButton" and (a.get("label") == "确认" or a.get("name") == "确认"), "confirm")
        xml = wait_source(driver, visible_saved_result, "confirmed-visible-result")
        (folder / "source-confirmed.xml").write_text(xml, encoding="utf-8")
        (folder / "confirmed.png").write_bytes(driver.screenshot_png())
        assert token in xml, "Native UI did not display entered token"
        return {"token": token, "button": button, "screenshot": str(folder / "confirmed.png"), "native_source_has_token": True}

    async def on_run(client, msg):
        serial = str(msg.get("device_serial") or msg.get("serial") or "")
        run_id = str(msg["run_id"])
        try:
            runs[run_id] = await asyncio.to_thread(execute_case, serial)
            result, error = "finished", ""
        except Exception as exc:
            runs[run_id] = {"error": repr(exc), "traceback": traceback.format_exc()}
            result, error = "failed", str(exc)
        await client.send({"type": P.MSG_RUN_DONE, "run_id": run_id, "serial": serial, "result": result, "steps": 3, "error": error})

    def new_agent():
        assert load_host_identity(get_settings().storage_dir) == host_id
        assert hashlib.sha256(host_identity_file.read_bytes()).hexdigest() == host_identity_sha256
        client = AgentWSClient(ws_url=f"ws://127.0.0.1:{port}/ws/agent", token=get_settings().agent_token,
            agent_id=agent_id, agent_name="Personal iOS isolated acceptance", host_id=host_id,
            device_provider=devices, ping_interval=5, rescan_interval=15)
        client.on(P.MSG_IOS_SIM_VM_CAPABILITY_PROBE, manager.handle_capability_probe)
        client.on(P.MSG_IOS_SIM_VM_START, manager.handle_start)
        client.on(P.MSG_IOS_SIM_VM_STOP, manager.handle_stop)
        client.on(P.MSG_IOS_SIM_VM_DELETE, manager.handle_delete)
        client.on(P.MSG_START_RUN, on_run)
        async def reclaim(_client):
            await asyncio.to_thread(manager.reconcile_running_vms_sync)
        async def report_reclaimed(client):
            await manager.report_reclaimed_vms(client, rescan=False)
        client.on_pre_hello(reclaim)
        client.on_connect(report_reclaimed)
        client.on_connect(manager.report_orphan_reconcile)
        client.on_rescan(manager.sweep_vanished_vms)
        return client

    try:
        assert FIXTURE.is_file(), "Shared personal fixture missing"
        agent = new_agent()
        agent_task = asyncio.create_task(agent.run_forever())
        await eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(agent_id)), bool)
        evidence.record("server_agent_connected", base_url=base, agent_id=agent_id)
        async with AsyncClient(base_url=base, timeout=90, trust_env=False) as http:
            response = await http.post("/api/internal/ios-sim/instances", headers=auth, json={"alias": "Personal iOS " + case_id, "device_type": device_type, "runtime": runtime_id})
            assert response.status_code == 201, response.text
            created = response.json()
            vm_id = created["id"]
            allow_vm(manifest, vm_id)
            assert created["state"] == "draft" and not created["assigned_agent_id"] and not created["udid"]
            path = "/api/internal/ios-sim/instances/" + vm_id
            evidence.record("created_unbound_config", vm_id=vm_id, device_type=device_type, runtime=runtime_id)
            candidates = await http.post(path + "/dispatch-candidates", headers=auth)
            assert candidates.status_code == 200, candidates.text
            atomic_json(folder / "candidates.json", candidates.json())
            dispatched = await http.post(path + "/dispatch", headers=auth, json={"agent_id": agent_id})
            assert dispatched.status_code == 200 and dispatched.json()["sent"], dispatched.text
            # Functional admission: no destructive action while starting.
            denied = await http.delete(path, headers=auth)
            assert denied.status_code == 409, denied.text
            evidence.record("dispatch_started", delete_during_start_status=denied.status_code)
            running = await eventually(lambda: http.get(path, headers=auth), lambda r: r.json().get("state") in {"running", "error"}, timeout=600)
            state = running.json()
            assert state["state"] == "running", state
            udid = state["udid"]
            assert udid in json.loads(manifest.read_text())["created"]
            evidence.record("running", vm_id=vm_id, udid=udid, wda_port=state["wda_port"], mjpeg_port=state["mjpeg_port"])
            device = await eventually(lambda: http.get("/api/devices/" + udid), lambda r: r.status_code == 200 and r.json().get("status") == "online", timeout=30)
            assert device.json()["extra"]["vm_instance_id"] == vm_id
            readiness = await IosSimProbe(udid).probe()
            assert readiness.ready, readiness
            await agent.send({"type": P.MSG_DEVICE_READINESS, "serial": udid, "platform": "ios_sim", "ready": True, "ts": time.time()})
            await eventually(lambda: http.get("/api/devices/" + udid), lambda r: r.json().get("extra", {}).get("readiness", {}).get("ready") is True)
            run_response = await http.post("/api/runs", json={"device_serial": udid, "goal": "Personal deterministic acceptance: input token, confirm, observe persistence"})
            assert run_response.status_code == 201 and run_response.json()["dispatched"], run_response.text
            run_id = run_response.json()["id"]
            done = await eventually(lambda: http.get("/api/runs/" + run_id), lambda r: r.json().get("status") in {"success", "failed", "error"}, timeout=180)
            assert done.json()["status"] == "success", (done.json(), runs)
            confirm_receipt = await eventually(lambda: asyncio.sleep(0, result=list(receipts)), lambda entries: any(x.get("event") == "confirm" and x.get("token") == token and x.get("count") == 1 for x in entries))
            evidence.record("real_input_business_result", run_id=run_id, status=done.json()["status"], execution=runs[run_id], receipt=confirm_receipt[-1])
            await asyncio.to_thread(drivers[udid]._wda.close)
            stop = await http.post(path + "/stop", headers=auth)
            assert stop.status_code == 200 and stop.json()["sent"], stop.text
            stopped = await eventually(lambda: http.get(path, headers=auth), lambda r: r.json().get("state") in {"stopped", "error"}, timeout=120)
            assert stopped.json()["state"] == "stopped", stopped.text
            native = manager._list_all_instances_strict()[udid]
            assert native["state"] == "Shutdown", native
            assert stopped.json()["assigned_agent_id"] == agent_id and stopped.json()["udid"] == udid
            evidence.record("stopped_retained", vm_id=vm_id, udid=udid, native=native)
            restart = await http.post(path + "/start", headers=auth)
            assert restart.status_code == 200 and restart.json()["sent"], restart.text
            restarted = await eventually(lambda: http.get(path, headers=auth), lambda r: r.json().get("state") in {"running", "error"}, timeout=240)
            assert restarted.json()["state"] == "running" and restarted.json()["udid"] == udid, restarted.text
            assert restarted.json()["wda_port"] == state["wda_port"]
            driver = await asyncio.to_thread(acquire_driver, udid)
            receipt_boundary = len(receipts)
            await asyncio.to_thread(simctl_run, "openurl", udid, page_url, timeout=30)
            persisted = await eventually(lambda: asyncio.sleep(0, result=receipts[receipt_boundary:]), lambda rows: any(x.get("event") == "load" and x.get("token") == token and x.get("count") == 1 for x in rows), timeout=45)
            await asyncio.to_thread(wait_source, driver, visible_saved_result, "restart-visible-result")
            screenshot = await asyncio.to_thread(driver.screenshot_png)
            (folder / "after-restart.png").write_bytes(screenshot)
            evidence.record("restart_preserves_identity_and_data", vm_id=vm_id, udid=udid, receipt=persisted[-1], native_source_has_persisted_token_and_count=True, screenshot=str(folder / "after-restart.png"))
            await asyncio.to_thread(driver._wda.close)
            stop2 = await http.post(path + "/stop", headers=auth)
            assert stop2.status_code == 200, stop2.text
            stopped2 = await eventually(lambda: http.get(path, headers=auth), lambda r: r.json().get("state") in {"stopped", "error"}, timeout=120)
            assert stopped2.json()["state"] == "stopped", stopped2.text
            # Final product rule: delete removes the Server ledger immediately.
            # An offline native instance is removed by reconnect reconciliation.
            await agent.stop()
            await asyncio.wait_for(agent_task, 20)
            await eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(agent_id)), lambda online: not online)
            deleted = await http.delete(path, headers=auth)
            assert deleted.status_code == 200 and deleted.json().get("deleted") is True, deleted.text
            removed = await http.get(path, headers=auth)
            assert removed.status_code == 404 and udid in manager._list_all_instances_strict()
            evidence.record("offline_delete_ledger_removed", response=deleted.json(), server_status=removed.status_code, native_present=True)
            previous_agent_id = agent_id
            agent_id = "personal-ios-reconnect-" + uuid.uuid4().hex[:12]
            evidence.record("new_agent_session_same_host", previous_agent_id=previous_agent_id, agent_id=agent_id,
                            host_id=host_id, identity_file_sha256=host_identity_sha256)
            agent = new_agent()
            agent_task = asyncio.create_task(agent.run_forever())
            await eventually(lambda: asyncio.to_thread(manager._list_all_instances_strict), lambda rows: udid not in rows, timeout=150)
            assert (await http.get(path, headers=auth)).status_code == 404
            assert udid not in manager._list_all_instances_strict()
            assert not (DEVICE_SET / udid).exists()
            assert udid not in wda_module._load_reservations()
            evidence.record("reconnect_cleanup_complete", vm_id=vm_id, udid=udid, native_absent=True, data_directory_absent=True, port_reservation_released=True)
            evidence.value["status"] = "passed"
    except Exception as exc:
        evidence.value["status"] = "failed"
        evidence.record("failure", error=repr(exc), traceback=traceback.format_exc())
    finally:
        for driver in drivers.values():
            with suppress(Exception):
                await asyncio.to_thread(driver._wda.close)
        if agent is not None:
            with suppress(Exception):
                await agent.stop()
        if agent_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(agent_task, 30)
        # Production manager cleanup, still behind the exact-name/UDID guard.
        # It cannot see, adopt, stop, or delete default-set or physical devices.
        cleanup = []
        for owned_vm in json.loads(manifest.read_text())["vm_ids"]:
            try:
                cleanup.append({"vm_id": owned_vm, "result": await asyncio.to_thread(manager.delete_sync, owned_vm)})
            except Exception as exc:
                cleanup.append({"vm_id": owned_vm, "error": repr(exc)})
        final_inventory = json.loads(await asyncio.to_thread(simctl_run, "list", "devices", "-j"))
        empty = not any(final_inventory["devices"].values())
        evidence.record("personal_cleanup", results=cleanup, isolated_set_empty=empty)
        if not empty:
            evidence.value["status"] = "failed"
        server.should_exit = True
        with suppress(Exception):
            await asyncio.wait_for(server_task, 20)
        worker = server_agent_ws._persist_worker
        if worker is not None and not worker.done():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await close_all_sessions()
        await db_module.dispose_engine()
        mismatches = [path for path, digest in evidence.value["source_sha256"].items()
                      if hashlib.sha256((REPO / path).read_bytes()).hexdigest() != digest]
        identity_unchanged = (host_identity_file.read_text(encoding="ascii").strip() == host_id
                              and hashlib.sha256(host_identity_file.read_bytes()).hexdigest() == host_identity_sha256)
        evidence.record("source_and_host_identity_verified", source_hash_mismatches=mismatches,
                        persistent_host_identity_unchanged=identity_unchanged)
        if mismatches or not identity_unchanged:
            evidence.value["status"] = "failed"
        evidence.value["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(folder / "result.json", evidence.value)
    return evidence.value


def native_baseline(folder: Path, device_type: str, runtime_id: str) -> dict:
    """One real native create/boot/shutdown/reboot sequence; no product imports.

    Safety review: exact short private set, guard-authorized fresh ID, one owned
    UDID, no WDA install, no Browser, no Server/Agent, bounded native commands,
    and finally exact shutdown/delete. Every stdout/stderr is retained.
    """
    manifest = prepare_isolation(folder)
    evidence = Evidence(folder)
    evidence.value["scope"] = "personal native-only simctl baseline; no Server/Agent/WDA/browser/default device set"
    vm_id = "baseline_" + uuid.uuid4().hex
    allow_vm(manifest, vm_id)
    wrapper = folder / "bin" / "xcrun"
    udid = ""

    def command(phase, *args, timeout=30, check=True):
        began = time.monotonic()
        cmd = [str(wrapper), "simctl", *args]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            payload = {"command": cmd, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "duration_seconds": round(time.monotonic() - began, 3)}
        except subprocess.TimeoutExpired as exc:
            payload = {"command": cmd, "timeout": timeout, "stdout": (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout, "stderr": (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr, "duration_seconds": round(time.monotonic() - began, 3)}
            atomic_json(folder / (phase + ".json"), payload)
            evidence.record(phase, outcome="timeout", timeout=timeout)
            raise
        atomic_json(folder / (phase + ".json"), payload)
        evidence.record(phase, returncode=result.returncode, duration_seconds=payload["duration_seconds"])
        if check and result.returncode:
            raise RuntimeError(f"{phase} failed: {result.stderr}")
        return result.stdout

    try:
        initial = json.loads(command("initial_inventory", "list", "devices", "-j"))
        assert not any(initial["devices"].values())
        udid = command("create", "create", "aiphone_sim_" + vm_id, device_type, runtime_id).strip()
        evidence.record("owned_identity", vm_id=vm_id, udid=udid, device_set=str(DEVICE_SET))
        command("first_boot", "boot", udid, timeout=120)
        command("first_bootstatus", "bootstatus", udid, timeout=180)
        command("first_screenshot", "io", udid, "screenshot", str(folder / "first-boot.png"))
        command("shutdown", "shutdown", udid, timeout=60)
        stopped = json.loads(command("stopped_inventory", "list", "devices", "-j"))
        assert next(d for rows in stopped["devices"].values() for d in rows if d["udid"] == udid)["state"] == "Shutdown"
        command("second_boot", "boot", udid, timeout=120)
        try:
            command("second_bootstatus", "bootstatus", udid, timeout=180)
        finally:
            command("second_screenshot", "io", udid, "screenshot", str(folder / "second-boot.png"), check=False)
        evidence.value["status"] = "passed"
    except Exception as exc:
        evidence.value["status"] = "failed"
        evidence.record("failure", error=repr(exc), traceback=traceback.format_exc())
    finally:
        if udid:
            command("cleanup_shutdown", "shutdown", udid, timeout=60, check=False)
            command("cleanup_delete", "delete", udid, timeout=120, check=False)
        inventory = json.loads(command("final_inventory", "list", "devices", "-j"))
        empty = not any(inventory["devices"].values())
        evidence.record("cleanup_verified", native_set_empty=empty, owned_data_absent=not udid or not (DEVICE_SET / udid).exists())
        if not empty:
            evidence.value["status"] = "failed"
        evidence.value["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(folder / "result.json", evidence.value)
    return evidence.value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-personal-ios", action="store_true")
    parser.add_argument("--native-baseline", action="store_true")
    parser.add_argument("--device-type", default=DEVICE_TYPE)
    parser.add_argument("--runtime", default=RUNTIME)
    args = parser.parse_args()
    plan = {"repository": str(REPO), "artifact_root": str(PERSONAL), "device_set": str(DEVICE_SET), "fixture": str(FIXTURE), "device_type": args.device_type, "runtime": args.runtime,
        "persistent_host_identity": "new-run/storage/agent-host-id, mode 0600; same file on reconnect, fresh agent_id",
        "execution_gate": "root source freeze and exclusive native hardware slot; default mode is plan only",
        "actions": ["isolated HTTP/WS server", "create unbound config", "real capability probe", "bind/start WDA", "real input and screenshot", "backend proof receipt", "stop", "restart same UDID and data", "offline delete removes Server ledger", "reconnect exact orphan cleanup"],
        "forbidden": ["default simulator set", "physical devices", "company endpoints/packages/configs", "global shutdown/delete", "production VLM credentials"]}
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not args.execute_personal_ios and not args.native_baseline:
        print("Plan only. No simulator lifecycle operation executed.")
        return 0
    if "iPad" in args.device_type:
        raise SystemExit("iPad custom device-set xcodebuild discovery must be verified separately before enabling")
    PERSONAL.mkdir(parents=True, exist_ok=True)
    prefix = "ios-native-" if args.native_baseline else "ios-"
    folder = PERSONAL / (prefix + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    result = native_baseline(folder, args.device_type, args.runtime) if args.native_baseline else asyncio.run(run(folder, args.device_type, args.runtime))
    print(json.dumps({"result": result["status"], "evidence": str(folder / "result.json")}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
