#!/usr/bin/env python3
"""PERSONAL Harmony lifecycle + deterministic browser Case, without company access.

Default invocation only prints the plan. ``--execute`` is deliberately required.
Run with this worktree's backend/.venv/bin/python after root grants a resource slot.
The native Emulator, HDC, hmdriver2, Manager, HTTP and WebSocket paths are real.
The Case is synthetic: this script is not a VLM/Onion acceptance test.

Test instance, registry, database and evidence writes belong to one new ASCII
/tmp/aiphone-personal-harmony-* directory; vendor tools retain normal caches.
Existing SDK images are read-only inputs. No default orphan scan, global HDC
restart, shared VM deletion, model credential or non-loopback Python connection.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any
import uuid


REPO = Path(__file__).resolve().parents[4]
FIXTURE = Path(__file__).with_name("personal-fixture.html")
EMULATOR = Path("/Applications/DevEco-Studio.app/Contents/tools/emulator/Emulator")
HDC = Path("/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc")
IMAGE_INFO = Path.home() / ".Huawei/Emulator/images/system-image/HarmonyOS-6.0.31/phone_all_arm/info.json"
API = "/api/internal/harmony-vm/instances"


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def protected_fingerprint() -> dict:
    """Read only vendor/default metadata; never restore or delete these files."""
    home = Path.home()
    deployed = home / ".Huawei/Emulator/deployed"
    paths = {deployed / "lists.json"}
    paths.update(deployed.glob("*.ini"))
    paths.update(deployed.glob("*/config.ini"))
    paths.update((home / "Library/Caches/Huawei").glob("Emulator*/.emu_config"))
    return {str(path): digest(path) for path in sorted(paths)}


def source_fingerprint() -> dict:
    paths = [Path(__file__), FIXTURE]
    paths += [REPO / "backend" / value for value in (
        "ai_phone/agent/harmony_vm/manager.py", "ai_phone/agent/harmony_vm/capability.py",
        "ai_phone/agent/harmony_vm/registry.py", "ai_phone/agent/ws_client.py",
        "ai_phone/agent/drivers/harmony.py", "ai_phone/agent/drivers/hdc.py",
        "ai_phone/server/harmony_vm/api.py", "ai_phone/server/harmony_vm/service.py",
        "ai_phone/server/ws/agent_ws.py", "ai_phone/server/vm_lifecycle.py",
    )]
    return {str(path.relative_to(REPO)): digest(path) for path in paths}


def plan() -> dict:
    return {
        "scope": "PERSONAL local only; synthetic browser Case, not VLM/Onion",
        "execute_required": True,
        "image_input_read_only": str(IMAGE_INFO.parent),
        "image_identity": {"os_version": "HarmonyOS 6.0.31(23)", "software_version": "6.1.0.115", "abi": "arm64"},
        "instance_root": "/tmp/aiphone-personal-harmony-<uuid>/agent-runtime/instances",
        "server": "127.0.0.1 random port; own SQLite, token, storage and Agent ID",
        "case": "create draft -> real capability/dispatch/start -> browser input/confirm/receipt -> stop -> start -> persisted receipt -> delete",
        "proof": "HTTP/WS journal, guest IDs, screenshots, UI trees, browser receipts, final directory/port/DB absence",
        "prohibited": ["company endpoints/credentials", "existing VM mutation", "global HDC kill/restart", "default orphan inventory", "shared image changes"],
    }


def isolate_environment(root: Path, token: str) -> None:
    # Do this before importing ai_phone: Settings reads .env files from cwd.
    for key in list(os.environ):
        if key.startswith("AI_PHONE_") or key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            os.environ.pop(key)
    os.chdir(root)
    # Qt local sockets append a UUID under TMPDIR; macOS sun_path is limited.
    # A long evidence-root TMPDIR makes Emulator 26 crash-service listen fail.
    temporary = Path("/tmp") / ("aph-" + uuid.uuid4().hex[:10])
    temporary.mkdir()
    tempfile.tempdir = str(temporary)
    os.environ.update({
        "TMPDIR": str(temporary),
        "SWIFT_MODULE_CACHE_PATH": str(root / "swift-module-cache"),
        "CLANG_MODULE_CACHE_PATH": str(root / "clang-module-cache"),
        "AI_PHONE_ENV": "personal-isolated-e2e",
        "AI_PHONE_STORAGE_DIR": str(root / "storage"),
        "AI_PHONE_DB_URL": f"sqlite+aiosqlite:///{root / 'server.sqlite3'}",
        "AI_PHONE_AGENT_TOKEN": token,
        "AI_PHONE_SUBMISSION_INTERNAL_TOKEN": token,
        "AI_PHONE_SERVER_HOST": "127.0.0.1",
        "AI_PHONE_HARMONY_VM_ORPHAN_CLEANUP": "false",
        "AI_PHONE_HARMONY_AUTO_ACCEPT_AGREEMENTS": "false",
        "AI_PHONE_HARMONY_VM_BOOT_TIMEOUT_SEC": "240",
        "AI_PHONE_BROADCAST_BACKEND": "null",
        "NO_PROXY": "127.0.0.1,localhost,::1",
    })
    os.environ["PATH"] = str(HDC.parent) + os.pathsep + os.environ.get("PATH", "")
    sys.path.insert(0, str(REPO / "backend"))

    def only_local_python_network(event: str, args: tuple) -> None:
        if event != "socket.connect":
            return
        address = args[1]
        if isinstance(address, tuple) and str(address[0]) not in {"127.0.0.1", "::1", "localhost"}:
            raise PermissionError(f"personal E2E forbids non-loopback Python connection: {address[0]}")

    sys.addaudithook(only_local_python_network)


async def eventually(call, predicate, *, timeout: float = 30, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await call()
        if predicate(last):
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition timeout ({timeout}s); last={last!r}")


def ui_nodes(value):
    if isinstance(value, dict):
        yield value.get("attributes", value)
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from ui_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from ui_nodes(child)


def node_center(tree, *, label: str, button: bool = False) -> tuple[int, int]:
    candidates = []
    for node in ui_nodes(tree):
        if not isinstance(node, dict):
            continue
        values = [str(node.get(key) or "") for key in ("text", "description", "accessibilityText", "hint", "id")]
        if label not in values:
            continue
        bounds = node.get("bounds") or node.get("origBounds")
        if isinstance(bounds, str):
            nums = [int(n) for n in re.findall(r"-?\d+", bounds)]
        elif isinstance(bounds, dict):
            nums = [bounds.get(k) for k in ("left", "top", "right", "bottom")]
        else:
            continue
        if len(nums) != 4 or any(not isinstance(n, int) for n in nums):
            continue
        left, top, right, bottom = nums
        if right > left and bottom > top:
            priority = int(button and "button" in str(node.get("type", "")).lower())
            candidates.append((priority, (right - left) * (bottom - top), (left + right) // 2, (top + bottom) // 2))
    if not candidates:
        raise AssertionError(f"actual native UI has no bounded element labelled {label!r}")
    _, _, x, y = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
    return x, y


def fixture_pixel_point(png: bytes, *, button: bool) -> tuple[tuple[int, int], dict]:
    """Bounded locator for the shared test fixture only; no DOM injection."""
    from io import BytesIO
    from PIL import Image, ImageChops
    rgb = (21, 90, 182) if button else (82, 101, 122)
    image = Image.open(BytesIO(png)).convert("RGB")
    # Harmony's native snapshot is JPEG before conversion to PNG; even a flat
    # CSS fill can differ by one channel value. Require a small RGB tolerance
    # AND the fixture's actual broad rectangular geometry, not exact colors.
    masks = [band.point(lambda value, channel=channel: 255 if abs(value - channel) <= 8 else 0)
             for band, channel in zip(image.split(), rgb)]
    mask = ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2])
    pixels = mask.tobytes()
    rows = [y for y in range(image.height)
            if pixels[y * image.width:(y + 1) * image.width].count(255) >= image.width // 2]
    groups = []
    for y in rows:
        if not groups or y - groups[-1][-1] > 2:
            groups.append([])
        groups[-1].append(y)
    if button:
        candidates = [group for group in groups if 20 <= group[-1] - group[0] <= image.height // 5]
        assert len(candidates) == 1, "fixture button pixels are absent or ambiguous"
        top, bottom = candidates[0][0], candidates[0][-1]
    else:
        edges = [group for group in groups if group[-1] - group[0] <= max(12, image.height // 125)]
        pairs = [(a, b) for a, b in zip(edges, edges[1:]) if 30 <= b[0] - a[-1] <= image.height // 6]
        assert len(pairs) == 1, "fixture input border pixels are absent or ambiguous"
        top, bottom = pairs[0][0][0], pairs[0][1][-1]
    box = mask.crop((0, top, image.width, bottom + 1)).getbbox()
    assert box is not None
    bounds = (box[0], top, box[2], bottom + 1)
    pixel_count = pixels[top * image.width:(bottom + 1) * image.width].count(255)
    assert pixel_count >= 300 and bounds[2] - bounds[0] >= image.width // 2
    point = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
    return point, {"method": "native_pixels", "bounds": bounds, "rgb": rgb,
                   "rgb_tolerance": 8, "geometry": "filled button" if button else "paired input borders",
                   "pixel_count": pixel_count}


OCR_SWIFT = r'''
import Foundation
import Vision
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["en-US", "zh-Hans"]
request.usesLanguageCorrection = false
try VNImageRequestHandler(url: url, options: [:]).perform([request])
let rows: [[String: Any]] = (request.results ?? []).compactMap { item in
    guard let text = item.topCandidates(1).first else { return nil }
    return ["text": text.string, "confidence": text.confidence,
            "x": item.boundingBox.minX, "y": item.boundingBox.minY,
            "width": item.boundingBox.width, "height": item.boundingBox.height]
}
let data = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
'''


def ocr_visible_result(root: Path, png: bytes, marker: str) -> dict:
    """Local Apple Vision reads actual native pixels; never accesses browser DOM."""
    binary = root / "native-ocr"
    if not binary.is_file():
        swift = root / "native-ocr.swift"
        swift.write_text(OCR_SWIFT)
        subprocess.run(["/usr/bin/swiftc", str(swift), "-o", str(binary)], check=True, capture_output=True, timeout=60)
    path = root / "visible-check.png"
    path.write_bytes(png)
    raw = subprocess.run([str(binary), str(path)], check=True, capture_output=True, text=True, timeout=15)
    rows = json.loads(raw.stdout)
    texts = [re.sub(r"\s+", "", row["text"]) for row in rows]
    marker_visible = any(marker in text for text in texts)
    count_visible = any("确认次数" in text and text.rstrip("。.").endswith("1") for text in texts)
    if not count_visible:
        labels = [row for row in rows if "确认次数" in row["text"]]
        digits = [row for row in rows if row["text"].strip() == "1"]
        count_visible = any(abs(a["y"] - b["y"]) < max(a["height"], b["height"]) and b["x"] > a["x"] for a in labels for b in digits)
    return {"method": "native_pixels_vision", "marker_visible": marker_visible,
            "count_visible": count_visible, "rows": rows,
            "ok": marker_visible and count_visible}


async def execute(root: Path, *, no_window: bool = False, min_free_gib: float = 4.0) -> dict:
    protected_before = protected_fingerprint()
    sources_before = source_fingerprint()
    token = secrets.token_urlsafe(32)
    isolate_environment(root, token)
    import psutil
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse
    from httpx import AsyncClient
    from loguru import logger
    from sqlalchemy.ext.asyncio import close_all_sessions
    from ai_phone.agent.drivers.hdc import hdc_run, hdc_list_targets
    from ai_phone.agent.health.probe import HarmonyProbe
    from ai_phone.agent.harmony_vm.manager import HarmonyVmManager
    from ai_phone.agent.ws_client import AgentWSClient
    from ai_phone.config import get_settings
    from ai_phone.server import db as db_module
    from ai_phone.server import models as _models  # registers all shared ORM tables
    from ai_phone.server.api import include_routers
    from ai_phone.server.db import Base
    from ai_phone.server.harmony_vm.models import HarmonyVmCatalogSnapshot
    from ai_phone.server.hub import Hub
    from ai_phone.server.lockstore import DeviceLockStore
    from ai_phone.server.ws import include_ws
    from ai_phone.shared import protocol as P

    globals()["Request"] = Request
    logger.add(root / "runtime.log", encoding="utf-8", rotation="10 MB")
    run = root.name.removeprefix("aiphone-personal-harmony-")
    marker = "HARMONY" + run[:12].upper()
    agent_id = "personal-harmony-" + run
    owned: set[str] = set()
    receipts: list[dict] = []
    events: list[dict] = []
    proof: dict = {"scope": plan(), "started_at": utc(), "run": run, "root": str(root), "passed": False,
                   "protected_before": protected_before, "source_hashes_at_start": sources_before}
    write_json(root / "preflight.json", proof)
    native_ports: set[int] = set()
    delete_requested = False
    delete_commands = 0
    basic_mode_selected = False
    runs: dict[str, dict] = {}
    journal = root / "events.jsonl"

    def record(kind: str, value: Any) -> None:
        item = {"time": utc(), "kind": kind, "value": value}
        events.append(item)
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        print(f"{utc()} {kind}", flush=True)

    foreign = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = str(proc.info.get("name") or "").lower()
        args = proc.info.get("cmdline") or []
        if (name == "emulator" or "qemu" in name) and ("-start" in args or "qemu" in name):
            foreign.append({"pid": proc.pid, "name": name})
    if foreign:
        raise RuntimeError(f"refusing real E2E while another emulator runs: {foreign}")
    if psutil.virtual_memory().available < min_free_gib * 1024**3:
        raise RuntimeError(f"personal Harmony E2E requires at least {min_free_gib} GiB available")
    assert EMULATOR.is_file() and HDC.is_file() and IMAGE_INFO.is_file() and FIXTURE.is_file()
    assert root.is_dir() and str(root).isascii()
    info = json.loads(IMAGE_INFO.read_text())
    assert info["apiVersion"] == "23" and info["abi"] == "arm", info
    proof["image_info"] = {"path": str(IMAGE_INFO), "sha256": hashlib.sha256(IMAGE_INFO.read_bytes()).hexdigest(), "data": info}
    proof["fixture_sha256"] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    initial_targets = await asyncio.to_thread(hdc_list_targets)
    proof["initial_hdc_targets"] = [asdict(row) for row in initial_targets]
    # Capability code inspects fport state per Connected target. Do not even
    # query an unknown physical/company device while checking this personal VM.
    if any(row.status.strip().lower() == "connected" for row in initial_targets):
        raise RuntimeError("refusing E2E with pre-existing Connected HDC targets; ownership is not ours")
    get_settings.cache_clear()
    await db_module.dispose_engine()
    db_module.init_engine(db_url=str(get_settings().db_url))
    async with db_module.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    image_id = "Phone|HarmonyOS 6.0.31(23)|arm64"
    async with db_module.get_session_factory()() as session:
        session.add(HarmonyVmCatalogSnapshot(
            id="official", source_type="deveco_emulator_official",
            device_types_json=["Phone"],
            images_json=[{"id": image_id, "device_type": "Phone", "os_version": "HarmonyOS 6.0.31(23)", "api_version": "23", "abi": "arm64"}],
            screen_profiles_json=[{"id": "Phone|Default", "device_type": "Phone", "name": "Default Phone", "width": 1080, "height": 2340, "density": 420, "supported_image_ids": [image_id], "create_methods": {image_id: "default"}}],
        ))
        await session.commit()

    app = FastAPI()
    app.state.hub = Hub()
    app.state.lock_store = DeviceLockStore()
    include_routers(app)
    include_ws(app)

    @app.get("/fixture/", response_class=HTMLResponse)
    async def page():
        return FIXTURE.read_text(encoding="utf-8")

    @app.post("/fixture/receipt")
    async def receipt(request: Request):
        value = await request.json()
        if value.get("run") != run or value.get("platform") != "harmony":
            raise HTTPException(400, "unowned receipt")
        value.update({"received_at": utc(), "user_agent": request.headers.get("user-agent", "")})
        receipts.append(value)
        record("browser_receipt", value)
        write_json(root / "receipts.json", receipts)
        return {"ok": True}

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False, log_level="warning"))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    await eventually(lambda: asyncio.sleep(0, result=server.started), bool)
    manager = HarmonyVmManager(runtime_dir=root / "agent-runtime")
    # Failed first-bind rollback removes the owned instance. Preserve vendor
    # boot diagnostics before that cleanup, without changing lifecycle behavior.
    original_cleanup = manager._cleanup_failed_start
    def preserve_boot_logs(runtime, *args, **kwargs):
        import shutil
        source = Path(runtime.instance_path) / runtime.instance_name / "Log"
        if source.is_dir():
            shutil.copytree(source, root / ("native-logs-" + runtime.vm_id), dirs_exist_ok=True)
        return original_cleanup(runtime, *args, **kwargs)
    manager._cleanup_failed_start = preserve_boot_logs
    assert manager.instance_path == root / "agent-runtime" / "instances"
    assert manager._known == {}

    def owned_devices():
        infos = []
        for vm_id, runtime in list(manager._runtimes.items()):
            assert vm_id in owned
            assert Path(runtime.instance_path).resolve().is_relative_to(root.resolve())
            if runtime.ready and runtime.driver is not None:
                infos.append(runtime.driver.device_info())
        return [item.to_dict() for item in manager.decorate_devices(infos)]

    class RecordedAgent(AgentWSClient):
        async def send(self, payload):
            if str(payload.get("type", "")).startswith("harmony_vm"):
                record("agent_send", payload)
            return await super().send(payload)

    agent = RecordedAgent(
        ws_url=f"ws://127.0.0.1:{port}/ws/agent", server_http_base=base,
        token=token, agent_id=agent_id, agent_name=agent_id,
        host_id="personal-test-" + run, device_provider=owned_devices,
        ping_interval=5, rescan_interval=10,
    )

    def register(message_type, handler):
        async def checked(client, msg):
            nonlocal delete_commands
            vm_id = str(msg.get("vm_id") or "")
            if message_type != P.MSG_HARMONY_VM_CAPABILITY_PROBE:
                assert vm_id in owned, f"unowned VM instruction: {vm_id}"
            if message_type == P.MSG_HARMONY_VM_DELETE:
                delete_commands += 1
                assert delete_commands == 1, "duplicate native delete instruction"
            if message_type in {P.MSG_HARMONY_VM_CAPABILITY_PROBE, P.MSG_HARMONY_VM_START}:
                allowed_serials = {item.hdc_serial for key, item in manager._runtimes.items() if key in owned}
                targets = await asyncio.to_thread(hdc_list_targets)
                assert all(item.status.lower() != "connected" or item.serial in allowed_serials for item in targets), "unowned live HDC target appeared"
            record("agent_receive", msg)
            await handler(client, msg)
        agent.on(message_type, checked)

    register(P.MSG_HARMONY_VM_CAPABILITY_PROBE, manager.handle_capability_probe)
    register(P.MSG_HARMONY_VM_START, manager.handle_start)
    register(P.MSG_HARMONY_VM_STOP, manager.handle_stop)
    register(P.MSG_HARMONY_VM_DELETE, manager.handle_delete)
    # Only our private registry is eligible. Never invoke the default orphan inventory.
    async def reconnect(client):
        assert set(manager._known).issubset(owned)
        await manager.report_reclaimed_vms(client)
    agent.on_connect(reconnect)
    agent_task = asyncio.create_task(agent.run_forever())
    auth = {"Authorization": "Bearer " + token}
    vm_id = ""

    async def hdc(runtime, *args):
        assert runtime.vm_id in owned
        assert Path(runtime.instance_path).resolve().is_relative_to(root.resolve())
        result = await asyncio.to_thread(hdc_run, *args, serial=runtime.hdc_serial, timeout=15, check=True)
        record("targeted_hdc", {"serial": runtime.hdc_serial, "args": args, "result": result})
        return result

    async def snapshot(runtime, label):
        raw = runtime.driver.get_raw_driver()
        png = await asyncio.to_thread(runtime.driver.screenshot_png)
        (root / f"{label}.png").write_bytes(png)
        tree = await asyncio.to_thread(raw.dump_hierarchy)
        write_json(root / f"{label}.tree.json", tree)
        record("screenshot", {"label": label, "path": str(root / f"{label}.png"), "sha256": hashlib.sha256(png).hexdigest()})
        return tree

    async def control_point(runtime, label, *, button=False):
        tree = await asyncio.to_thread(runtime.driver.get_raw_driver().dump_hierarchy)
        try:
            point = node_center(tree, label=label, button=button)
            details = {"method": "native_tree", "label": label}
        except AssertionError:
            png = await asyncio.to_thread(runtime.driver.screenshot_png)
            point, details = fixture_pixel_point(png, button=button)
        record("control_point", {**details, "point": point})
        return point

    async def visible_result(runtime, label):
        deadline = time.monotonic() + 30
        last = None
        while time.monotonic() < deadline:
            tree = await asyncio.to_thread(runtime.driver.get_raw_driver().dump_hierarchy)
            values = {str(node.get(key) or "") for node in ui_nodes(tree) if isinstance(node, dict)
                      and str(node.get("visible", "true")).lower() != "false"
                      for key in ("text", "description", "accessibilityText")}
            native_ok = marker in values and ("1" in values or "确认次数：1" in values) and any("确认次数" in value for value in values)
            png = await asyncio.to_thread(runtime.driver.screenshot_png)
            last = {"method": "native_tree", "ok": native_ok}
            if not native_ok:
                last = await asyncio.to_thread(ocr_visible_result, root, png, marker)
            if last["ok"]:
                (root / f"{label}.png").write_bytes(png)
                write_json(root / f"{label}.tree.json", tree)
                write_json(root / f"{label}.visible.json", last)
                record("native_visible_result", {"label": label, "method": last["method"], "marker": marker, "count": 1,
                                                 "screenshot": str(root / f"{label}.png"), "sha256": hashlib.sha256(png).hexdigest()})
                return last
            await asyncio.sleep(1)
        write_json(root / f"{label}.visible-failure.json", last)
        raise AssertionError(f"native UI never visibly showed expected token/count: {last}")

    async def bounded_ui_diagnosis(runtime, label, error, *, event, receipt_boundary=0):
        """Keep one live personal guest for at most three minutes of UI repair.

        The control file only requests verification. It cannot supply a token,
        receipt, or a success result: those still come from browser HTTP and
        actual native pixels/tree. Lifecycle failures do not enter this path.
        """
        flag = root / (label + "-VERIFY")
        await snapshot(runtime, label + "-initial-ui-failure")
        diagnostic = {"vm_id": runtime.vm_id, "serial": runtime.hdc_serial,
                      "error": repr(error), "verification_file": str(flag),
                      "max_wait_seconds": 180, "expected_event": event,
                      "expected_token": marker, "expected_count": 1}
        write_json(root / (label + "-ui-diagnostic.json"), diagnostic)
        record("bounded_ui_diagnosis_waiting", diagnostic)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if flag.is_file():
                assert any(row.get("event") == event and row.get("token") == marker and row.get("count") == 1
                           for row in receipts[receipt_boundary:]), "a real matching browser receipt is required"
                visible = await visible_result(runtime, label + "-visible-after-diagnosis")
                record("bounded_ui_diagnosis_verified", {"label": label, "original_error": repr(error), "method": visible["method"]})
                proof.setdefault("assisted_native_ui", []).append(diagnostic)
                return visible
            await asyncio.sleep(.5)
        raise AssertionError("UI diagnosis time limit expired; original error=" + repr(error))

    def native_cleanup_state():
        instance_dir = manager.instance_path / ("aiphone_harmony_" + vm_id)
        processes = []
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            args = process.info.get("cmdline") or []
            if any(str(root) in str(value) or ("aiphone_harmony_" + vm_id) in str(value) for value in args) and (
                "emulator" in str(process.info.get("name") or "").lower()
                or "qemu" in str(process.info.get("name") or "").lower()
                or "crash" in str(process.info.get("name") or "").lower()
            ):
                processes.append({"pid": process.pid, "name": process.info.get("name")})
        listening = []
        for candidate in sorted(native_ports):
            with socket.socket() as probe_socket:
                probe_socket.settimeout(0.3)
                if probe_socket.connect_ex(("127.0.0.1", candidate)) == 0:
                    listening.append(candidate)
        return {"instance_directory_absent": not instance_dir.exists(),
                "registry_absent": vm_id not in manager._known, "runtime_absent": vm_id not in manager._runtimes,
                "owned_processes": processes, "listening_native_ports": listening}

    def native_clean(value):
        return (value["instance_directory_absent"] and value["registry_absent"] and value["runtime_absent"]
                and not value["owned_processes"] and not value["listening_native_ports"])

    async def input_tree_with_basic_ime(runtime, label, *, require_native_focus):
        """Root-authorized basic typing only in this blank, disposable guest."""
        nonlocal basic_mode_selected
        raw = runtime.driver.get_raw_driver()
        cancelled = False
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            tree = await asyncio.to_thread(raw.dump_hierarchy)
            nodes = [node for node in ui_nodes(tree) if isinstance(node, dict)]
            cancel = next((node for node in nodes if node.get("id") == "Paf.Permission.button_cancel" and node.get("text") == "取消"), None)
            ime_name = any(node.get("id") == "Paf.Permission.appName" and node.get("text") == "小艺输入法" for node in nodes)
            basic = next((node for node in nodes if node.get("id") == "advanced_dialog_button_1" and node.get("text") == "同意基本模式"), None)
            if basic is not None and ime_name:
                assert cancelled and not basic_mode_selected, "basic mode setup must follow one first-layer cancel, once per guest"
                assert any("不收集任何个人数据" in str(node.get("text") or "") for node in nodes)
                await snapshot(runtime, label + "-basic-mode-before")
                point = node_center({"attributes": basic}, label="advanced_dialog_button_1")
                setup = {"label": label, "point": point,
                    "node": {key: basic.get(key) for key in ("id", "text", "bounds", "visible")},
                    "decision": "basic typing mode only; no full mode or additional permissions",
                    "authorization": "root explicitly authorized basic input initialization in disposable personal guest"}
                record("ime_basic_mode_selected", setup)
                await asyncio.to_thread(runtime.driver.click, *point)
                basic_mode_selected = True
                proof["basic_input_setup"] = setup
                continue
            if cancel is not None and ime_name:
                assert not cancelled, "IME consent dialog recurred; refusing an automatic retry loop"
                await snapshot(runtime, label + "-ime-consent-not-accepted")
                point = node_center({"attributes": cancel}, label="Paf.Permission.button_cancel")
                record("ime_consent_cancelled", {"label": label, "point": point,
                    "node": {key: cancel.get(key) for key in ("id", "text", "bounds", "visible")},
                    "decision": "cancel; no agreement or system permission granted"})
                await asyncio.to_thread(runtime.driver.click, *point)
                cancelled = True
                continue
            if not require_native_focus or any(str(node.get("focused", "false")).lower() == "true"
                    and "textinput" in str(node.get("type", "")).lower() for node in nodes):
                write_json(root / (label + "-input-ready.tree.json"), tree)
                return tree
            await asyncio.sleep(.25)
        raise AssertionError("No verified input focus after explicit basic-mode setup: " + label)

    async def open_fixture(runtime, label):
        before_load = len(receipts)
        tree = await asyncio.to_thread(runtime.driver.get_raw_driver().dump_hierarchy)
        if any(node.get("id") == "ScreenLockRootComponent" for node in ui_nodes(tree) if isinstance(node, dict)):
            width, height = await asyncio.to_thread(runtime.driver.window_size)
            await asyncio.to_thread(runtime.driver.swipe, width // 2, round(height * 0.82), width // 2, round(height * 0.35))
            await eventually(lambda: asyncio.to_thread(runtime.driver.get_raw_driver().dump_hierarchy),
                             lambda data: not any(node.get("id") == "ScreenLockRootComponent" for node in ui_nodes(data) if isinstance(node, dict)), timeout=10)
            record("fresh_guest_swipe_unlock", {"observed_id": "ScreenLockRootComponent", "serial": runtime.hdc_serial})
        await hdc(runtime, "rport", f"tcp:{port}", f"tcp:{port}")
        raw = runtime.driver.get_raw_driver()
        packages = await asyncio.to_thread(raw.list_apps, include_system_apps=True)
        browsers = sorted(p for p in packages if "browser" in p.lower())
        assert browsers, f"no built-in browser in fresh personal guest: {packages}"
        browser = next((p for p in browsers if p == "com.huawei.hmos.browser"), browsers[0])
        ability = await asyncio.to_thread(raw.get_app_main_ability, browser)
        ability_name = str(ability.get("name") or "MainAbility")
        url = f"{base}/fixture/?run={run}&platform=harmony"
        # a2 proved `aa -U` acknowledges launch but leaves Huawei Browser at
        # its home page. Launch the app only, then use its real native input.
        args = ["aa", "start", "-b", browser, "-a", ability_name]
        await hdc(runtime, "shell", shlex.join(args))
        address_label = "搜索或输入网址"
        # Both the first boot and restart enter via the observed home control.
        # The private browser can only contain this test's own page history.
        for attempt in range(3):
            tree = await snapshot(runtime, f"{label}-browser-entry-{attempt}")
            nodes = [node for node in ui_nodes(tree) if isinstance(node, dict)
                     and node.get("text") == address_label and str(node.get("visible", "true")).lower() != "false"]
            if nodes:
                break
            foreground = await asyncio.to_thread(raw.current_app)
            assert foreground[0] == browser, f"unexpected foreground app before address entry: {foreground}"
            if attempt == 2:
                raise AssertionError("browser did not show the verified native home address control")
            record("browser_return_home", {"from": foreground, "attempt": attempt + 1})
            await asyncio.to_thread(runtime.driver.press_back)
        point = node_center(tree, label=address_label)
        node_evidence = [{key: node.get(key) for key in ("id", "key", "type", "text", "bounds", "origBounds", "visible", "hierarchy")}
                         for node in nodes]
        write_json(root / f"{label}-address-node.json", node_evidence)
        record("browser_address_click", {"point": point, "nodes": node_evidence})
        await asyncio.to_thread(runtime.driver.click, *point)
        focused = await input_tree_with_basic_ime(runtime, label + "-address", require_native_focus=True)
        write_json(root / f"{label}-address-focused.tree.json", focused)
        # type_text passes URL as a JSON string to Driver.inputText; the URL
        # never enters a shell and its `&` query delimiter cannot be expanded.
        await asyncio.to_thread(runtime.driver.type_text, url)
        typed = await eventually(lambda: asyncio.to_thread(raw.dump_hierarchy),
            lambda data: any(str(node.get("text") or "") == url for node in ui_nodes(data) if isinstance(node, dict)), timeout=12)
        write_json(root / f"{label}-address-entered.tree.json", typed)
        await snapshot(runtime, f"{label}-address-entered")
        from hmdriver2.proto import KeyCode
        assert KeyCode.ENTER.value == 2054
        await asyncio.to_thread(runtime.driver.press_keycode, KeyCode.ENTER.value)
        record("browser_open", {"bundle": browser, "ability": ability_name, "url": url,
                                "method": "native_address_input_and_enter", "keycode": KeyCode.ENTER.value})
        await eventually(lambda: asyncio.sleep(0, result=receipts[before_load:]), lambda rows: any(r["event"] == "load" for r in rows), timeout=40)
        return await snapshot(runtime, label)

    async def execute_case(runtime):
        await open_fixture(runtime, "01_before_input")
        x, y = await control_point(runtime, "输入本轮标记")
        await asyncio.to_thread(runtime.driver.click, x, y)
        await input_tree_with_basic_ime(runtime, "fixture", require_native_focus=False)
        await asyncio.to_thread(runtime.driver.type_text, marker)
        await snapshot(runtime, "02_input")
        await asyncio.to_thread(runtime.driver.press_back)
        await snapshot(runtime, "03_before_confirm")
        x, y = await control_point(runtime, "确认", button=True)
        await asyncio.to_thread(runtime.driver.click, x, y)
        await eventually(lambda: asyncio.sleep(0, result=receipts), lambda rows: any(r.get("event") == "confirm" and r.get("token") == marker and r.get("count") == 1 for r in rows), timeout=20)
        visible = await visible_result(runtime, "04_confirmed")
        return {"token": marker, "count": 1, "visible": visible["method"], "case": "synthetic real driver input; no VLM"}

    async def preparation_gate(runtime):
        """Five-minute local test setup, before the single formal business Run.

        Root explicitly authorizes ordinary basic keyboard onboarding in this
        same disposable guest. The operator uses actual native tree/pixels;
        the gate never writes a business receipt or declares the Case passed.
        """
        deadline = time.monotonic() + 300
        instructions = {
            "vm_id": runtime.vm_id, "hdc_serial": runtime.hdc_serial,
            "instance_path": runtime.instance_path, "instance_name": runtime.instance_name,
            "ready_file": str(root / "PREPARED"), "deadline_seconds": 300,
            "required": "Finish basic IME and 26-key onboarding on this guest; return built-in browser to home. Save native nodes/actions in manual-preparation.json, then create PREPARED.",
            "forbidden": ["full IME mode", "account login", "new contacts/camera/microphone permissions", "other devices", "fabricated browser receipts"],
        }
        write_json(root / "preparation-gate.json", instructions)
        await snapshot(runtime, "preparation-initial")
        record("preparation_gate_waiting", instructions)
        while time.monotonic() < deadline:
            if (root / "PREPARED").is_file():
                setup_path = root / "manual-preparation.json"
                assert setup_path.is_file(), "native setup evidence must precede PREPARED"
                setup = json.loads(setup_path.read_text())
                assert setup.get("vm_id") == runtime.vm_id and setup.get("hdc_serial") == runtime.hdc_serial
                assert setup.get("basic_mode_only") is True and setup.get("account_login") is False
                foreground = await asyncio.to_thread(runtime.driver.get_raw_driver().current_app)
                assert foreground[0] == "com.huawei.hmos.browser", foreground
                tree = await snapshot(runtime, "preparation-complete-browser-home")
                node_center(tree, label="搜索或输入网址")
                proof["manual_preparation"] = setup
                record("preparation_gate_complete", {"foreground": foreground, "setup": str(setup_path)})
                return
            await asyncio.sleep(.5)
        raise AssertionError("Personal native initialization did not complete within the five-minute preparation gate")

    async def on_run(client, msg):
        serial = str(msg.get("device_serial") or msg.get("serial") or "")
        assert serial == f"harmony-vm:{vm_id}" and vm_id in owned
        run_id = str(msg["run_id"])
        try:
            try:
                runs[run_id] = await execute_case(manager._runtimes[vm_id])
            except Exception as initial:
                visible = await bounded_ui_diagnosis(manager._runtimes[vm_id], "case", initial, event="confirm")
                runs[run_id] = {"token": marker, "count": 1, "visible": visible["method"],
                                "case": "synthetic real native input; assisted UI diagnosis", "original_error": repr(initial)}
            result, error = "finished", ""
        except Exception as exc:
            runs[run_id] = {"error": repr(exc), "traceback": traceback.format_exc()}
            result, error = "failed", str(exc)
        await client.send({"type": P.MSG_RUN_DONE, "run_id": run_id, "serial": serial,
                           "result": result, "steps": 3, "error": error})

    agent.on(P.MSG_START_RUN, on_run)

    try:
        await eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(agent_id)), bool)
        async with AsyncClient(base_url=base, timeout=65, trust_env=False) as http:
            async def request(method, path, **kwargs):
                response = await http.request(method, path, headers=auth, **kwargs)
                record("http", {"method": method, "path": path, "status": response.status_code, "body": response.json()})
                assert response.is_success, response.text
                return response.json()

            async def state(expected, timeout=270):
                async def read():
                    response = await http.get(f"{API}/{vm_id}", headers=auth)
                    body = response.json()
                    if response.status_code == 200 and body.get("state") == "error":
                        raise AssertionError(f"VM operation failed: {body}")
                    return body
                value = await eventually(read, lambda row: row.get("state") == expected, timeout=timeout, interval=0.5)
                record("state_" + expected, value)
                return value

            created = await request("POST", API, json={"alias": "PERSONAL-Harmony-" + run[:10], "os_version": "6.0.31", "api_version": "23", "abi": "arm64", "memory_gb": 2, "storage_gb": 4})
            vm_id = created["id"]
            owned.add(vm_id)
            assert created["state"] == "draft" and not created["assigned_agent_id"]
            proof["vm_id"] = vm_id
            probe_started = time.monotonic()
            candidates = await request("POST", f"{API}/{vm_id}/dispatch-candidates")
            proof["candidate_probe"] = {"elapsed_sec": time.monotonic() - probe_started, "result": candidates}
            await request("POST", f"{API}/{vm_id}/dispatch", json={"agent_id": agent_id})
            first = await state("running")
            runtime = manager._runtimes[vm_id]
            proof["first_identity"] = runtime.persistent_dict()
            proof["no_window_requested"] = no_window
            proof["emulator_command"] = psutil.Process(runtime.process.pid).cmdline()
            if no_window:
                assert "-noWindow" in proof["emulator_command"]
            native_ports.update({runtime.hdc_port, runtime.driver_fport_port})
            native_ports.discard(None)
            config_path = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
            assert config_path.is_file()
            original_config = config_path.read_text()
            original_uuid = re.search(r"(?m)^uuid\s*=\s*(.+)$", original_config)
            assert original_uuid, "fresh instance lacks native UUID"
            proof["native_uuid"] = original_uuid.group(1).strip()
            # A lifecycle result arrives before the (real, slower) metadata
            # scan publishes the device. Server correctly rejects readiness
            # without a current route; wait for registration before probing.
            await eventually(lambda: http.get(f"/api/devices/harmony-vm:{vm_id}"),
                             lambda response: response.status_code == 200
                             and response.json().get("status") == "online"
                             and response.json().get("extra", {}).get("vm_instance_id") == vm_id)
            await preparation_gate(runtime)
            readiness = await HarmonyProbe(runtime.hdc_serial, timeout_sec=5).probe()
            record("real_readiness", asdict(readiness))
            assert readiness.ready, readiness
            await agent.send({"type": P.MSG_DEVICE_READINESS, "serial": f"harmony-vm:{vm_id}", "platform": "harmony", "ready": readiness.ready, "ts": time.time()})
            await eventually(lambda: http.get(f"/api/devices/harmony-vm:{vm_id}"),
                             lambda response: response.status_code == 200 and response.json().get("extra", {}).get("readiness", {}).get("ready") is True)
            dispatched = await request("POST", "/api/runs", json={"device_serial": f"harmony-vm:{vm_id}", "goal": "Personal deterministic fixture: input token, confirm, verify visible result"})
            assert dispatched["dispatched"] is True
            run_id = dispatched["id"]
            done = await eventually(lambda: http.get("/api/runs/" + run_id),
                                    lambda response: response.json().get("status") in {"success", "failed", "error"}, timeout=420)
            assert done.json()["status"] == "success", {"run": done.json(), "execution": runs}
            proof["execution"] = {"run_id": run_id, "server_status": "success", **runs[run_id]}
            await request("POST", f"{API}/{vm_id}/stop")
            stopped = await state("stopped", timeout=90)
            assert config_path.is_file() and not stopped.get("hdc_port")
            assert stopped["assigned_agent_id"] == agent_id
            with socket.socket() as probe_socket:
                probe_socket.settimeout(1)
                assert probe_socket.connect_ex(("127.0.0.1", first["hdc_port"])) != 0
            assert vm_id not in manager._runtimes
            load_cutoff = len(receipts)
            await request("POST", f"{API}/{vm_id}/start")
            second = await state("running")
            runtime = manager._runtimes[vm_id]
            proof["second_identity"] = runtime.persistent_dict()
            native_ports.update({runtime.hdc_port, runtime.driver_fport_port})
            native_ports.discard(None)
            second_uuid = re.search(r"(?m)^uuid\s*=\s*(.+)$", config_path.read_text())
            assert second["id"] == first["id"] and second_uuid
            assert second_uuid.group(1).strip() == proof["native_uuid"]
            try:
                await open_fixture(runtime, "05_restarted")
                await eventually(lambda: asyncio.sleep(0, result=receipts[load_cutoff:]), lambda rows: any(r.get("event") == "load" and r.get("token") == marker and r.get("count") == 1 for r in rows), timeout=25)
                await visible_result(runtime, "06_persisted")
            except Exception as error:
                await bounded_ui_diagnosis(runtime, "restart", error, event="load", receipt_boundary=load_cutoff)
            await request("POST", f"{API}/{vm_id}/stop")
            await state("stopped", timeout=90)
            delete_requested = True
            deletion = await request("DELETE", f"{API}/{vm_id}")
            assert deletion.get("deleted") is True and deletion.get("cleanup_sent") is True
            deleted = await http.get(f"{API}/{vm_id}", headers=auth)
            assert deleted.status_code == 404
            record("ledger_deleted_native_pending", {"server_404": True, "native": native_cleanup_state()})
            clean = await eventually(lambda: asyncio.to_thread(native_cleanup_state), native_clean, timeout=90)
            assert delete_commands == 1
            proof["cleanup"] = {"server_404": True, "delete_commands": delete_commands, **clean}
            proof["passed"] = True
            record("terminal_pass", proof["cleanup"])
    except BaseException as exc:
        proof["error"] = f"{type(exc).__name__}: {exc}"
        proof["traceback"] = traceback.format_exc()
        record("failure", proof["error"])
        if vm_id in manager._runtimes:
            with suppress(Exception):
                await snapshot(manager._runtimes[vm_id], "failure")
        raise
    finally:
        # Never turn asynchronous delete into two native delete commands. If
        # HTTP already sent one, only read its completion and report failure.
        for owned_id in tuple(owned):
            known = manager._known.get(owned_id) or {}
            if owned_id in manager._runtimes or known:
                location = Path(str(known.get("instance_path") or manager.instance_path)).resolve()
                assert location.is_relative_to(root.resolve()), location
                try:
                    if delete_requested:
                        cleanup = await eventually(lambda: asyncio.to_thread(native_cleanup_state), native_clean, timeout=90)
                        record("existing_delete_completion", cleanup)
                    else:
                        cleanup = await asyncio.to_thread(manager.delete_sync, owned_id)
                        record("safety_cleanup", {"vm_id": owned_id, "result": cleanup})
                        proof["safety_cleanup"] = cleanup
                except Exception as exc:
                    proof["safety_cleanup_error"] = repr(exc)
        await agent.stop()
        with suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(agent_task, timeout=10)
        server.should_exit = True
        with suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(server_task, timeout=10)
        listener.close()
        await close_all_sessions()
        await db_module.dispose_engine()
        proof["finished_at"] = utc()
        proof["receipts"] = receipts
        proof["owned_ids"] = sorted(owned)
        proof["protected_after"] = protected_fingerprint()
        proof["protected_unchanged"] = proof["protected_after"] == protected_before
        proof["source_hashes_at_end"] = source_fingerprint()
        proof["sources_unchanged"] = proof["source_hashes_at_end"] == sources_before
        if not proof["protected_unchanged"] or not proof["sources_unchanged"]:
            proof["passed"] = False
            proof["environment_verification_error"] = "protected vendor metadata or tested source changed; no restoration attempted"
        if vm_id:
            proof["final_native_state"] = native_cleanup_state()
            if not native_clean(proof["final_native_state"]):
                proof["passed"] = False
        write_json(root / "result.json", proof)
        print(json.dumps({"passed": proof["passed"], "result": str(root / "result.json")}), flush=True)
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="root-authorized real personal VM lifecycle")
    parser.add_argument("--no-window", action="store_true", help="require official headless CLI; never fall back to GUI")
    parser.add_argument("--min-free-gib", type=float, default=4.0, help="minimum available host RAM; test VM uses 2 GiB")
    parser.add_argument("--run-root", type=Path, help="new ASCII /tmp/aiphone-personal-harmony-* directory")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    root = args.run_root or Path("/tmp") / ("aiphone-personal-harmony-" + uuid.uuid4().hex)
    if root.parent != Path("/tmp") or not root.name.startswith("aiphone-personal-harmony-") or not str(root).isascii():
        parser.error("run root must be a new ASCII /tmp/aiphone-personal-harmony-* directory")
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    write_json(root / "plan.json", plan())
    if args.min_free_gib < 2.5:
        parser.error("minimum free memory must be at least 2.5 GiB")
    result = asyncio.run(execute(root, no_window=args.no_window, min_free_gib=args.min_free_gib))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
