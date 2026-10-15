#!/usr/bin/env python3
"""Opt-in a5 product guest-sync diagnosis with unchanged data and visual checks.

Default: print the plan only. No imports of ai_phone, network, directory creation,
ADB daemon, emulator, or package build occur without --execute.

This is synthetic native-chain acceptance, not a VLM/business-Case run. The
private WebView APK is built from the source below and renders the shared local
personal-fixture.html. No company service, package, account or model is used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
FIXTURE_HTML = HERE.parents[1] / "execution/personal-fixture.html"
EXPECTED_FIXTURE_SHA256 = "24e20a5cf5ecaf8c3490c0ec11a8065b9974a51328a400e6e36a18ba7162cf87"
JAVA_TEMPLATE = HERE / "a3_MainActivity.java"
STORAGE_PROBE_JS = HERE / "a3_storage_diagnostic.js"
FROZEN_MANIFEST = HERE / "frozen-products.json"
SDK = Path.home() / "Library/Android/sdk"
DEFAULT_PARENT = Path.home() / ".codex/personal-vm-followup"
APP_ID = "org.personal.aiphone.lifecycle"
ADB_PORT = 15037
EMULATOR_PORT = 5666
FIXTURE_GUEST_PORT = 19080
RESTART_VISUAL_TIMEOUT_SEC = 30.0
A4_ADB_READ_PATH_MANIFEST = HERE / "a4-prepare/adbutils-read-path.json"

MANIFEST = '''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
 package="org.personal.aiphone.lifecycle" android:versionCode="1" android:versionName="1.0">
 <uses-sdk android:minSdkVersion="23" android:targetSdkVersion="28" />
 <uses-permission android:name="android.permission.INTERNET" />
 <application android:label="Personal VM E2E" android:usesCleartextTraffic="true"
  android:allowBackup="false" android:theme="@android:style/Theme.Material.Light.NoActionBar">
  <activity android:name=".MainActivity" android:exported="true">
   <intent-filter><action android:name="android.intent.action.MAIN" />
    <category android:name="android.intent.category.LAUNCHER" /></intent-filter>
  </activity>
 </application>
</manifest>'''

JAVA = JAVA_TEMPLATE.read_text().replace(
    '/* A3_READONLY_STORAGE_JS */ ""', json.dumps(STORAGE_PROBE_JS.read_text(), ensure_ascii=True))


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_products():
    manifest = json.loads(FROZEN_MANIFEST.read_text())
    expected = manifest["source_sha256"]
    if len(expected) != 33:
        raise RuntimeError("Expected the reviewed 33-product-source freeze")
    actual = {name: digest(REPO / name) for name in expected}
    return {"manifest": str(FROZEN_MANIFEST), "manifest_sha256": digest(FROZEN_MANIFEST),
            "count": len(expected), "source_sha256": actual,
            "mismatches": [name for name, sha in expected.items() if actual[name] != sha]}


def validate_install_message(msg, *, serial, package_id, filename, server_base):
    """Fail before the real handler can download or touch an unknown target."""
    expected_path = "/api/app-install/packages/" + urllib.parse.quote(package_id, safe="") + "/download"
    expected_url = server_base.rstrip("/") + expected_path
    raw_url = str(msg.get("package_url") or "")
    resolved = urllib.parse.urljoin(server_base.rstrip("/") + "/", raw_url)
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.hostname != "127.0.0.1" or parsed.scheme != "http" or resolved != expected_url:
        raise RuntimeError("Install package URL is not this run's exact localhost package")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Install URL must not contain credentials, query or fragment")
    if msg.get("serial") != serial or msg.get("platform") != "android" or msg.get("filename") != filename:
        raise RuntimeError("Install target/platform/filename does not match this run")
    for key in ("task_id", "item_id"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(msg.get(key) or "")):
            raise RuntimeError("Missing or invalid install task identity")
    if not isinstance(msg.get("timeout_sec"), (int, float)) or not (30 <= msg["timeout_sec"] <= 600):
        raise RuntimeError("Unexpected native install time budget")
    return expected_url


def validate_terminal_install_task(task, *, task_id, item_id, package_id, serial):
    """A completed batch alone does not establish successful installation."""
    items = task.get("items") or []
    expected_summary = {"total": 1, "pending": 0, "running": 0, "success": 1,
                        "failed": 0, "timeout": 0, "unknown": 0}
    if task.get("id") != task_id or task.get("package_id") != package_id or task.get("state") != "done":
        raise RuntimeError("Install task terminal identity/state does not match this run")
    if len(items) != 1 or any(items[0].get(key) != value for key, value in {
            "id": item_id, "task_id": task_id, "serial": serial, "platform": "android", "state": "success"}.items()):
        raise RuntimeError("Install task did not finish its one owned item successfully")
    if task.get("summary") != expected_summary:
        raise RuntimeError("Install task terminal summary is not exactly one success")
    if not task.get("finished_at") or not items[0].get("finished_at"):
        raise RuntimeError("Install task/item terminal timestamps are missing")


def validate_fixture_package_absent(returncode, stdout, stderr, *, package_id=APP_ID):
    """Only a successful, understood package-list result proves absence."""
    if returncode != 0 or stderr.strip():
        raise RuntimeError(f"Package absence probe failed: exit={returncode}, stderr={stderr.strip()[:1000]}")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if any(not re.fullmatch(r"package:[A-Za-z0-9_.]+", line) for line in lines):
        raise RuntimeError("Package absence probe returned an unknown output format")
    if "package:" + package_id in lines:
        raise RuntimeError("The fresh test VM already contains the personal fixture APK")
    return {"returncode": returncode, "package_lines": lines, "exact_package": package_id, "absent": True}


def validate_guest_sync_argv(args, environment, *, adb_path, serial):
    """Permit only the product's exact sync command on the private route."""
    argv = [str(value) for value in args]
    power_words = {"shutdown", "poweroff", "reboot"}
    if any(power_words.intersection(value.split()) for value in argv):
        raise RuntimeError("Guest power commands are not authorized by this sync-only test")
    if not any(re.search(r"(?<![A-Za-z0-9_])(?:/system/bin/)?sync(?![A-Za-z0-9_])", value) for value in argv):
        return False
    expected_prefix = [str(adb_path), "-s", serial, "shell"]
    marker = re.fullmatch(r"/system/bin/sync && printf '%s\\n' '([0-9a-f]{32})'", argv[4]) if len(argv) == 5 else None
    if len(argv) != 5 or argv[:4] != expected_prefix or marker is None or serial != f"emulator-{EMULATOR_PORT}":
        raise RuntimeError("Unapproved native sync command or target")
    expected_environment = {"ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{ADB_PORT}",
                            "ANDROID_ADB_SERVER_HOST": "127.0.0.1", "ANDROID_ADB_SERVER_PORT": str(ADB_PORT),
                            "ADB_SERVER_PORT": str(ADB_PORT)}
    if any(environment.get(key) != value for key, value in expected_environment.items()):
        raise RuntimeError("Guest sync is not routed through this private ADB daemon")
    return {"ack_marker": marker.group(1), "expected_stdout": marker.group(1) + "\n"}


def cleanup_stop_decision(vm_id, generation_id, attempts):
    """A failed/unknown stop cannot turn into an automatic cleanup retry."""
    relevant = [row for row in attempts if row.get("vm_id") == vm_id and row.get("generation_id") == generation_id]
    unresolved = [row for row in relevant if row.get("outcome") != "succeeded"]
    return {"allowed": not unresolved, "generation_id": generation_id,
            "reason": "same-generation stop failed or is unresolved; preserve resources for root" if unresolved else
                      "no unsuccessful stop for this native generation; one protective cleanup is allowed",
            "prior_attempt_count": len(relevant), "unresolved_attempt_count": len(unresolved)}


def observe_manager_stops(manager, generation_for, attempts):
    """Record real stop results without changing arguments, deadlines or return values."""
    original = manager.stop_sync

    def observed(vm_id, *args, **kwargs):
        record = {"vm_id": vm_id, "generation_id": generation_for(vm_id), "outcome": "running",
                  "at": utc_now(), "monotonic_ns": time.monotonic_ns(), "args": list(args), "forwarded_kwargs": dict(kwargs)}
        attempts.append(record)
        try:
            result = original(vm_id, *args, **kwargs)
            record.update(outcome="succeeded" if isinstance(result, dict) and result.get("ok") is True else "failed", result=result)
            return result
        except BaseException as exc:
            record.update(outcome="failed", error=type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            record.update(finished_at=utc_now(), finished_monotonic_ns=time.monotonic_ns())

    manager.stop_sync = observed


def require_operation(instance, *, vm_id, action, request_id=None):
    if not isinstance(instance, dict) or instance.get("id") != vm_id:
        raise RuntimeError("Lifecycle response is missing the exact requested VM identity")
    runtime = instance.get("runtime")
    operation = runtime.get("operation") if isinstance(runtime, dict) else None
    if not isinstance(operation, dict) or operation.get("action") != action:
        raise RuntimeError("Lifecycle response is missing the requested operation")
    observed_id = operation.get("request_id")
    if not isinstance(observed_id, str) or not observed_id or observed_id.strip() != observed_id:
        raise RuntimeError("Lifecycle operation request_id is missing or invalid")
    if request_id is not None and observed_id != request_id:
        raise RuntimeError("Lifecycle readback belongs to another operation request_id")
    return operation


def operation_snapshot_ready(instance, *, vm_id, action, request_id, desired_state):
    operation = require_operation(instance, vm_id=vm_id, action=action, request_id=request_id)
    outcome = operation.get("outcome")
    if outcome in {"failed", "timeout", "timed_out", "cancelled"}:
        raise RuntimeError(f"This {action} operation {request_id} ended as {outcome}; physical {instance.get('state')} is not success")
    if outcome == "pending" and operation.get("pending") is True:
        return False
    if outcome != "succeeded" or operation.get("pending") is not False:
        raise RuntimeError("Lifecycle operation has no explicit successful terminal outcome")
    return instance.get("state") == desired_state


def diagnostic_summary(records, *, witness_name, witness_sha256, witness_bytes, fixture_url, storage_key, token):
    """Report each independent track; missing/error evidence never means empty storage."""
    phases = {}
    for phase in ("first-write", "restart-read"):
        rows = [row for row in records if row.get("phase") == phase]
        witnesses = [row["data"] for row in rows if row.get("kind") == "witness"]
        providers = [row["data"] for row in rows if row.get("kind") == "webview-provider"]
        storage = [row["data"] for row in rows if row.get("kind") == "storage"]
        witness_errors = []
        witness = witnesses[0] if len(witnesses) == 1 else None
        if witness is None:
            witness_errors.append("exactly one witness observation required")
        else:
            if not witness.get("ok") or witness.get("mode") != phase:
                witness_errors.append("witness operation failed or mode mismatch")
            if witness.get("sha256") != witness_sha256 or witness.get("bytes") != witness_bytes:
                witness_errors.append("witness content changed or unavailable")
            if Path(str(witness.get("path") or "")).name != witness_name or \
                    witness.get("parent") not in {f"/data/user/0/{APP_ID}", f"/data/data/{APP_ID}"}:
                witness_errors.append("witness path is not the exact app-private file")
            if phase == "first-write":
                if witness.get("existedBefore") is not False or not all(witness.get(key) is True for key in
                        ("created", "openedForWrite", "fileFsync", "parentFsync")):
                    witness_errors.append("durable first-write evidence incomplete")
            elif witness.get("existedBefore") is not True or any(witness.get(key) is not False for key in ("created", "openedForWrite", "fileFsync", "parentFsync")):
                witness_errors.append("restart was not strictly read-only")
        observations = []
        for data in storage:
            identity_ok = data.get("href") == fixture_url and data.get("storageKey") == storage_key \
                and data.get("origin") == f"http://127.0.0.1:{FIXTURE_GUEST_PORT}"
            parsed = data.get("parsedValue")
            try:
                independently_parsed = json.loads(data["rawValue"]) if isinstance(data.get("rawValue"), str) else None
            except (TypeError, ValueError):
                independently_parsed = None
            confirmed_value = data.get("probeOk") is True and identity_ok and data.get("readError") is None \
                and data.get("parseError") is None and data.get("missing") is False \
                and isinstance(data.get("rawValue"), str) and isinstance(parsed, dict) \
                and independently_parsed == parsed and parsed.get("token") == token and parsed.get("count") == 1
            observations.append({"identity_ok": identity_ok, "confirmed_value": confirmed_value,
                "probe_ok": data.get("probeOk"), "missing": data.get("missing"),
                "read_error": data.get("readError"), "parse_error": data.get("parseError"),
                "raw_value": data.get("rawValue"), "parsed_value": parsed,
                "trigger": data.get("trigger"), "evaluation_finished_ns": data.get("evaluationFinishedElapsedNs")})
        phases[phase] = {"witness_ok": not witness_errors, "witness_errors": witness_errors,
                         "witness": witness, "providers": providers, "storage_observations": observations,
                         "witness_observation_count": len(witnesses),
                         "confirmed_storage_observed": any(x["confirmed_value"] for x in observations)}
    complete = all(x["witness_observation_count"] == 1 and x["providers"] and
                   any(s["probe_ok"] is True and s["identity_ok"] for s in x["storage_observations"]) for x in phases.values())
    provider_ids = {phase: sorted({(str(p.get("packageName")), str(p.get("versionName")), str(p.get("longVersionCode")))
                    for p in value["providers"] if p.get("ok") is True}) for phase, value in phases.items()}
    return {"phases": phases, "evidence_complete": bool(complete),
            "missing_evidence_means": "diagnostic evidence incomplete; never inferred as missing file/storage",
            "raw_storage_false_means": "not proven preserved; inspect missing/read_error/parse_error/identity before assigning cause",
            "webview_provider_identities": provider_ids,
            "webview_provider_same": (provider_ids["first-write"] == provider_ids["restart-read"])
                                      if all(provider_ids.values()) else None,
            "durable_witness_preserved": all(x["witness_ok"] for x in phases.values()),
            "raw_storage_preserved": all(x["confirmed_storage_observed"] for x in phases.values()),
            "actual_webview_versions_observed": all(any(p.get("ok") is True and p.get("packageName")
                and p.get("versionName") and p.get("longVersionCode") is not None for p in x["providers"]) for x in phases.values())}


def planned_paths(run_id: str):
    root = DEFAULT_PARENT / run_id
    return {
        "root": str(root), "db": str(root / "server.sqlite3"),
        "storage": str(root / "storage"),
        "avd_home": str(root / "android/avd"),
        "android_user_home": str(root / "android/user"),
        "android_runtime": str(root / "android/runtime"),
        "private_adb_port": ADB_PORT,
        "allowed_emulator_ports": [EMULATOR_PORT],
        "fixture_apk": str(root / "fixture/personal-lifecycle.apk"),
        "fixture_html": str(FIXTURE_HTML),
        "diagnostic_java_template": str(JAVA_TEMPLATE), "diagnostic_readonly_js": str(STORAGE_PROBE_JS),
        "agent_download_tmp": str(root / "tmp"),
        "server_upload_storage": str(root / "storage/app-install"),
        "ios_device_set_plan": str(root / "ios/devices"),
        "harmony_instance_path_plan": str(root / "harmony/runtime/instances"),
    }


def plan(run_id: str):
    return {
        "mode": "dry-run", "platform": "android", "run_id": run_id,
        "source_repo": str(REPO), "paths": planned_paths(run_id),
        "frozen_products": frozen_products(),
        "diagnostic": {"fixture_html_sha256": digest(FIXTURE_HTML), "expected_html_sha256": EXPECTED_FIXTURE_SHA256,
            "witness": "Before first WebView: exclusive private file write + FD.sync + parent Os.fsync. Restart: read only, never repair.",
            "raw_storage": "Asynchronous read-only evaluateJavascript on page load and original receipt POST; no HTML modification.",
            "confirmation_stop": "No new wait, guest command, sync, force-stop or reinstall after confirmation; monotonic timing only.",
            "a2_avd": "Never inspected or changed by this new harness; a new run root is mandatory."},
        "restart_visual_gate": {"position": "Only after original restart load receipt/count1, before saving final screenshot",
            "budget_sec": RESTART_VISUAL_TIMEOUT_SEC, "xml_read_budget_sec": 8, "screenshot_budget_sec": 5,
            "requires": "Exact native result label/token/count1 excluding input nodes, plus substantial solid blue button and green result border in real PNG",
            "read_connection": "Exact private ADB/serial; no CLI, no implicit start-server, deadline closes only observation sockets",
            "selected_frame": "Save the exact passing PNG/XML pair; do not take a replacement screenshot",
            "failure": "Read/identity errors fail immediately; not-yet-drawn valid samples poll until bounded timeout; no UI/device recovery",
            "root_visual_review": "Still required; pixel text is not automatically verified by the gate"},
        "a5_stop_scope": {"policy": "Observe only the product's exact private-ADB /system/bin/sync with per-command acknowledgment marker, then its original emu kill/exit verification",
            "fixture_sync_added": False, "before_stop_wait_added": False, "guest_power_shutdown_authorized": False,
            "acknowledgment_limit": "Marker proves this sync command completed; it does not prove App userspace commit, every storage I/O result, or VM exit",
            "failed_stop_cleanup": "Do not repeat failed/unresolved stop for the same native generation; keep resources for root",
            "new_generation_cleanup": "After successful old-generation stop and restart, one protective stop remains allowed for a later data/visual failure",
            "snapshot_coverage": "This case uses no_snapshot only; save/discard_changes/cold_boot reuse require a separate authorized matrix"},
        "ownership": "Only VM IDs returned by this fresh local Server and recorded before dispatch.",
        "server": "Fresh SQLite and loopback-only HTTP/WS, ephemeral port and token. No project .env or CLI defaults.",
        "agent": "Restricted AgentWSClient + AndroidVmManager. No agent.main, global provider, foreign reclaim or orphan cleanup.",
        "native_isolation": [
            "Private ANDROID_AVD_HOME and ANDROID_USER_HOME; SDK images read-only.",
            "Private adb port with --one-device nonexistent USB ID; mDNS and emulator auto-scan disabled.",
            "Native operations check both allowlisted VM ID and the actual emulator AVD name.",
            "Existing AVD/CoreSimulator/Harmony directories are fingerprinted and preserved.",
            "Exact-serial adb reverse maps guest127.0.0.1:19080 to this run's loopback fixture; no guest gateway/external network assumption.",
        ],
        "stages": [
            "preflight", "build personal WebView APK", "start isolated Server/ADB/Agent",
            "HTTP create draft", "WS capability probe", "HTTP bind/start", "real native ready",
            "POST multipart personal APK /api/app-install/packages",
            "GET /api/app-install/packages/{id}/eligible-devices",
            "POST /api/app-install/tasks with only this package and this serial",
            "real handle_app_install_start -> NativeOperation -> install_apk -> app_install_result",
            "GET /api/app-install/tasks/{id}: exactly one item success + native pm path + released leases",
            "shared HTML input/click/screenshot/receipt",
            "WS reconnect without stopping VM", "stop and verify native exit",
            "start same AVD and verify token/count persistence", "stop, delete, verify native and Server absence",
        ],
        "not_covered": ["VLM execution", "company business Case", "physical device control", "guest external networking", "iOS/Harmony (separate harnesses)",
                        "agent.main._get_driver live cache eviction and serial/port reuse (fresh drivers are used here)"],
        "execute_command": f"<personal backend venv>/bin/python {Path(__file__).resolve()} --execute --run-id {run_id}",
    }


class Evidence:
    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.value = {
            "run_id": run_id, "platform": "android", "started_at": utc_now(),
            "classification": "synthetic real Server package/task/WS/NativeOperation installation + native lifecycle E2E; not VLM/business acceptance",
            "not_covered": ["external guest networking", "VLM/company business Case", "physical device control",
                            "agent.main._get_driver live cache eviction and serial/port reuse"],
            "status": "running", "stages": [], "owned_vm_ids": [], "receipts": [],
        }

    def flush(self):
        target = self.root / "evidence.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.value, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(target)

    def add(self, stage, **details):
        self.value["stages"].append({"stage": stage, "at": utc_now(), "monotonic_ns": time.monotonic_ns(), **details})
        self.flush()


def command(args, *, timeout=30, env=None, cwd=None, binary=False):
    result = subprocess.run(
        [str(x) for x in args], capture_output=True, text=not binary,
        timeout=timeout, env=env, cwd=cwd,
    )
    if result.returncode:
        stderr = result.stderr.decode(errors="replace") if binary else result.stderr
        raise RuntimeError(f"{Path(str(args[0])).name} exited {result.returncode}: {stderr[-3000:]}")
    return result.stdout


def port_free(port):
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((address, port))
            except OSError:
                return False
    return True


def fixture_color_bounds(png: bytes, color: tuple[int, int, int]):
    """Locate our fixed fixture controls from real pixels, never from injected DOM.

    This deliberately belongs only to the deterministic test page. It is not a
    production locator, fallback policy, or general app automation mechanism.
    """
    from io import BytesIO
    from PIL import Image
    image = Image.open(BytesIO(png)).convert("RGB")
    points = [(x, y) for y in range(image.height) for x in range(image.width)
              if image.getpixel((x, y)) == color]
    if len(points) < 300:
        raise RuntimeError("Fixture control color not present in the actual screenshot")
    xs, ys = zip(*points)
    box = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
    if box[2] - box[0] < image.width // 2 or box[3] - box[1] < 20:
        raise RuntimeError("Fixture control pixels did not form the expected large control")
    return {"bounds": box, "center": ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2),
            "pixel_count": len(points), "rgb": color}


def inspect_visual_sample(xml_text: str, png: bytes, expected_token: str):
    """Require native saved text plus both large markers in the same raw frame."""
    result = {"ready": False, "reasons": [], "native_saved_token": False, "native_count_one": False,
              "png_sha256": hashlib.sha256(png).hexdigest(),
              "xml_sha256": hashlib.sha256(xml_text.encode()).hexdigest()}
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(png)) as native_frame:
            if native_frame.size != (540, 960):
                result["reasons"].append("unexpected dimensions for this exact 540x960 test VM")
                return result
        button = fixture_color_bounds(png, (21, 90, 182))
        saved = fixture_color_bounds(png, (10, 121, 91))
        result.update(button=button, result_region=saved)
        for marker in (button, saved):
            box = marker["bounds"]
            marker["fill_ratio"] = marker["pixel_count"] / ((box[2] - box[0]) * (box[3] - box[1]))
        if button["fill_ratio"] < 0.6 or saved["fill_ratio"] > 0.25:
            result["reasons"].append("marker pixels do not form the fixed solid button and result border")
            return result
        if button["bounds"][3] >= saved["bounds"][1]:
            result["reasons"].append("result region is not below the fixture button")
            return result
    except Exception as exc:
        result["reasons"].append("fixture pixels absent or invalid: " + str(exc))
        return result
    try:
        tree = ET.fromstring(xml_text[xml_text.index("<?xml"):])
    except Exception as exc:
        result["reasons"].append("native XML unavailable or invalid: " + str(exc))
        return result
    region = saved["bounds"]
    fragments = []
    for node in tree.iter("node"):
        if "EditText" in node.attrib.get("class", "") or node.attrib.get("editable") == "true" or node.attrib.get("visible-to-user") == "false":
            continue
        bounds = [int(value) for value in re.findall(r"-?\d+", node.attrib.get("bounds", ""))]
        if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1] or \
                bounds[2] <= region[0] or bounds[0] >= region[2] or bounds[3] <= region[1] or bounds[1] >= region[3]:
            continue
        for attribute in ("text", "content-desc"):
            value = re.sub(r"\s+", "", node.attrib.get(attribute, ""))
            if value:
                fragments.append(value)
    joined = "".join(fragments)
    result["native_result_fragments"] = fragments
    result["native_saved_token"] = bool(re.search(r"已保存标记[:：]" + re.escape(expected_token) + r"(?=$|[^A-Za-z0-9])", joined))
    result["native_count_one"] = bool(re.search(r"确认次数[:：]1(?=$|[^0-9A-Za-z])", joined))
    if not result["native_saved_token"]:
        result["reasons"].append("exact saved token is not exposed by native result nodes")
    if not result["native_count_one"]:
        result["reasons"].append("native result count is not exactly one")
    result["ready"] = result["native_saved_token"] and result["native_count_one"]
    return result


def bounded_private_observation(driver, action, timeout):
    """Keep the real native read; bound only its private ADB connections.

    Installed adbutils.shell otherwise replaces the client timeout with 600s.
    A deadline closes only connections opened by this one private driver call.
    The connection factory also omits adbutils' implicit start-server recovery.
    The real screenshot code/command and captured pixels remain unchanged.
    """
    if timeout <= 0:
        raise TimeoutError("Positive observation budget required")
    deadline = time.monotonic() + timeout
    client = driver._device._client
    if client.host != "127.0.0.1" or client.port != ADB_PORT or driver._device.serial != f"emulator-{EMULATOR_PORT}":
        raise RuntimeError("Visual sampler requires the exact private ADB client")
    original_make_connection = client.make_connection
    connections = []
    lock = threading.Lock()

    def close_connections():
        with lock:
            current = list(connections)
        for connection in current:
            with contextlib.suppress(Exception):
                connection.conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                connection.close()

    from adbutils import AdbConnection

    class ObservationConnection(AdbConnection):
        def _safe_connect(self):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Private observation connection deadline expired")
            connection = socket.create_connection(("127.0.0.1", ADB_PORT), timeout=remaining)
            connection.settimeout(remaining)
            return connection

    def make_connection(timeout=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Private observation connection deadline expired")
        connection = ObservationConnection("127.0.0.1", ADB_PORT)
        connection.conn.settimeout(min(float(timeout or remaining), remaining))
        with lock:
            connections.append(connection)
        if time.monotonic() >= deadline:
            close_connections()
            raise TimeoutError("Private observation connection exceeded its deadline")
        return connection

    client.make_connection = make_connection
    timer = threading.Timer(max(0.001, timeout), close_connections)
    timer.daemon = True
    timer.start()
    try:
        value = action()
        if time.monotonic() >= deadline:
            raise TimeoutError("Native read exceeded its observation budget")
        return value
    finally:
        timer.cancel()
        client.make_connection = original_make_connection
        close_connections()


def protected_fingerprint():
    """Read small metadata only; never traverse other apps' private data."""
    paths = []
    avd = Path.home() / ".android/avd"
    paths += list(avd.glob("*.ini")) + list(avd.glob("*.avd/config.ini"))
    paths += list((Path.home() / "Library/Developer/CoreSimulator/Devices").glob("*/device.plist"))
    harmony = Path.home() / ".Huawei/Emulator/deployed"
    paths += list(harmony.glob("*.ini")) + list(harmony.glob("*/config.ini"))
    if (harmony / "lists.json").exists():
        paths.append(harmony / "lists.json")
    return {str(path): digest(path) for path in sorted(paths) if path.is_file()}


def configure(root: Path, run_id: str):
    """Discard inherited deployment settings; never rewrite HOME or CLI config."""
    for key in list(os.environ):
        if key.lower().endswith("_proxy") or key.startswith("AI_PHONE_") or key in (
            "AIPHONE_BASE_URL", "AIPHONE_INTERNAL_TOKEN", "ANDROID_SERIAL", "ADB_VENDOR_KEYS",
            "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_HOST", "ANDROID_ADB_SERVER_PORT",
        ):
            os.environ.pop(key, None)
    token = secrets.token_urlsafe(32)
    os.environ.update({
        "AI_PHONE_DB_URL": f"sqlite+aiosqlite:///{root / 'server.sqlite3'}",
        "AI_PHONE_STORAGE_DIR": str(root / "storage"),
        "AI_PHONE_AGENT_TOKEN": token,
        "AI_PHONE_SUBMISSION_INTERNAL_TOKEN": token,
        "AI_PHONE_AGENT_NAME": "personal-e2e-" + run_id,
        "AI_PHONE_ANDROID_VM_ORPHAN_CLEANUP": "false",
        "AI_PHONE_ANDROID_VM_KILL_FOREIGN": "false",
        "AI_PHONE_HARMONY_VM_ORPHAN_CLEANUP": "false",
        "AI_PHONE_ANDROID_VM_LOCALE": "",
        "AI_PHONE_ANDROID_VM_BOOT_TIMEOUT_SEC": "180",
        "AI_PHONE_KAFKA_BROKERS": "",
        "ANDROID_HOME": str(SDK), "ANDROID_SDK_ROOT": str(SDK),
        "ANDROID_AVD_HOME": str(root / "android/avd"),
        "ANDROID_USER_HOME": str(root / "android/user"),
        "ANDROID_EMULATOR_HOME": str(root / "android/user"),
        "ANDROID_ADB_SERVER_HOST": "127.0.0.1",
        "ANDROID_ADB_SERVER_PORT": str(ADB_PORT),
        "ADB_SERVER_PORT": str(ADB_PORT),
        "ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{ADB_PORT}",
        "ADB_MDNS_AUTO_CONNECT": "0",
        "ADB_LOCAL_TRANSPORT_MAX_PORT": "1",
        "NO_PROXY": "127.0.0.1,localhost,::1,10.0.2.2",
        "no_proxy": "127.0.0.1,localhost,::1,10.0.2.2",
    })
    jdk17 = Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home")
    if (jdk17 / "bin/java").is_file():
        os.environ["JAVA_HOME"] = str(jdk17)
    for name in ("android/avd", "android/user", "android/runtime", "storage", "fixture"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir()
    os.environ["TMPDIR"] = str(root / "tmp")
    tempfile.tempdir = str(root / "tmp")
    os.chdir(root)  # Settings only reads relative .env files; this new directory has none.
    return token


def build_fixture(root: Path):
    folder = root / "fixture"
    manifest = folder / "AndroidManifest.xml"
    java = folder / "MainActivity.java"
    manifest.write_text(MANIFEST)
    java.write_text(JAVA)
    android_jar = SDK / "platforms/android-36/android.jar"
    if not android_jar.is_file():
        raise RuntimeError("Installed API 36 compile android.jar is required; runtime stays API 33, no SDK download attempted")
    build = SDK / "build-tools/36.0.0"
    for name in ("aapt2", "d8", "zipalign", "apksigner"):
        if not (build / name).is_file():
            raise RuntimeError(f"Missing installed build tool: {build / name}")
    java_candidates = [
        os.environ.get("JAVA_HOME", ""),
        "/Applications/Android Studio.app/Contents/jbr/Contents/Home",
        "/Applications/DevEco-Studio.app/Contents/jbr/Contents/Home",
        "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
        "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
    ]
    java_home = next((p for p in java_candidates if p and (Path(p) / "bin/javac").is_file()), "")
    if not java_home:
        raise RuntimeError("Installed JDK required; no Java install is attempted")
    env = dict(os.environ, JAVA_HOME=java_home)
    classes = folder / "classes"
    dex = folder / "dex"
    classes.mkdir(); dex.mkdir()
    command([Path(java_home) / "bin/javac", "-source", "8", "-target", "8", "-bootclasspath", android_jar,
             "-d", classes, java], env=env, timeout=60)
    class_files = sorted(classes.rglob("*.class"))
    command([build / "d8", "--lib", android_jar, "--output", dex, *class_files], env=env, timeout=60)
    unsigned = folder / "unsigned.apk"
    aligned = folder / "aligned.apk"
    final = folder / "personal-lifecycle.apk"
    command([build / "aapt2", "link", "-I", android_jar, "--manifest", manifest, "-o", unsigned], env=env, timeout=60)
    with zipfile.ZipFile(unsigned, "a") as archive:
        archive.write(dex / "classes.dex", "classes.dex")
    command([build / "zipalign", "-f", "4", unsigned, aligned], env=env)
    env["PERSONAL_E2E_KEYSTORE_PASS"] = secrets.token_urlsafe(24)
    keystore = folder / "personal-test.p12"
    command([Path(java_home) / "bin/keytool", "-genkeypair", "-noprompt", "-keystore", keystore,
             "-storetype", "PKCS12", "-storepass:env", "PERSONAL_E2E_KEYSTORE_PASS",
             "-alias", "personal-test", "-keyalg", "RSA", "-keysize", "2048", "-validity", "2",
             "-dname", "CN=Personal Local Test"], env=env, timeout=60)
    command([build / "apksigner", "sign", "--ks", keystore,
             "--ks-pass", "env:PERSONAL_E2E_KEYSTORE_PASS", "--out", final, aligned], env=env, timeout=60)
    command([build / "apksigner", "verify", final], env=env)
    return final


async def eventually(call, predicate, timeout=30, interval=0.25):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = await call()
        if predicate(last):
            return last
        await asyncio.sleep(interval)
    raise TimeoutError(f"Condition timed out after {timeout}s; last={str(last)[:1000]}")


async def execute(run_id: str):
    if digest(FIXTURE_HTML) != EXPECTED_FIXTURE_SHA256:
        raise RuntimeError("Original shared HTML changed; refusing diagnostic execution")
    adb_read_path = json.loads(A4_ADB_READ_PATH_MANIFEST.read_text())
    if any(digest(Path(record["path"])) != record["sha256"] for record in adb_read_path["files"].values()):
        raise RuntimeError("Reviewed private screenshot library changed; refusing native execution")
    product_freeze = frozen_products()
    if product_freeze["mismatches"]:
        raise RuntimeError("Frozen product source changed; refusing native execution: " + ", ".join(product_freeze["mismatches"]))
    root = DEFAULT_PARENT / run_id
    if root.exists() or root.is_symlink():
        raise RuntimeError(f"Fresh run directory required; refusing to reuse {root}")
    for port in (ADB_PORT, EMULATOR_PORT, EMULATOR_PORT + 1):
        if not port_free(port):
            raise RuntimeError(f"Required private port {port} is occupied; nothing was started")
    if not (SDK / "system-images/android-33/google_apis/arm64-v8a/source.properties").is_file():
        raise RuntimeError("Installed API 33 ARM64 Google APIs image required; no download attempted")
    root.mkdir(parents=True, mode=0o700)
    before = protected_fingerprint()
    token = configure(root, run_id)
    evidence = Evidence(root, run_id)
    source_snapshot = root / "diagnostic-sources"
    source_snapshot.mkdir()
    for original in (Path(__file__).resolve(), FIXTURE_HTML, JAVA_TEMPLATE, STORAGE_PROBE_JS, A4_ADB_READ_PATH_MANIFEST):
        (source_snapshot / original.name).write_bytes(original.read_bytes())
    (source_snapshot / "MainActivity.generated.java").write_text(JAVA)
    for name, record in adb_read_path["files"].items():
        (source_snapshot / ("adbutils-" + name)).write_bytes(Path(record["path"]).read_bytes())
    evidence.value["a4_adb_read_path"] = adb_read_path
    evidence.value["paths"] = planned_paths(run_id)
    evidence.value["protected_before"] = before
    evidence.value["frozen_products_at_start"] = product_freeze
    source_files = [
        Path(__file__).resolve(), FIXTURE_HTML, JAVA_TEMPLATE, STORAGE_PROBE_JS, A4_ADB_READ_PATH_MANIFEST,
        REPO / "backend/ai_phone/agent/android_vm/manager.py",
        REPO / "backend/ai_phone/agent/ws_client.py",
        REPO / "backend/ai_phone/agent/identity.py",
        REPO / "backend/ai_phone/agent/drivers/android.py",
        REPO / "backend/ai_phone/server/android_vm/api.py",
        REPO / "backend/ai_phone/server/android_vm/service.py",
        REPO / "backend/ai_phone/server/ws/agent_ws.py",
        *list((REPO / "backend/ai_phone/server").glob("*lifecycle*.py")),
        *[REPO / path for path in product_freeze["source_sha256"]],
        *list((REPO / "backend/ai_phone/server/app_install").glob("*.py")),
        *list((REPO / "backend/ai_phone/agent/app_install").glob("*.py")),
    ]
    evidence.value["source_hashes_at_start"] = {str(path.relative_to(REPO)): digest(path) for path in source_files if path.is_file()}
    evidence.add("preflight", status="passed", api_scope="loopback only", default_adb_untouched=True)
    sys.path.insert(0, str(REPO / "backend"))

    import adbutils
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import close_all_sessions
    from ai_phone.agent.android_vm.manager import AndroidVmManager
    from ai_phone.agent.android_vm import manager as android_manager_module
    from ai_phone.agent.identity import load_host_identity
    from ai_phone.agent.app_install import handler as install_handler
    from ai_phone.agent.app_install import android as install_android
    from ai_phone.agent.device_activity import device_activity
    from ai_phone.agent.native_operation import NativeOperation
    from ai_phone.agent.drivers.android import AndroidDriver
    from ai_phone.agent.drivers.base import DeviceInfo
    from ai_phone.agent.ws_client import AgentWSClient
    from ai_phone.config import get_settings
    from ai_phone.server import db as db_module
    from ai_phone.server.api import include_routers
    from ai_phone.server.android_vm import service as vm_service
    from ai_phone.server.db import Base
    from ai_phone.server.hub import Hub
    from ai_phone.server.models import AppPackage
    from ai_phone.server.app_install.service import package_file_path
    from ai_phone.server.lockstore import DeviceLockStore
    from ai_phone.server.ws import include_ws
    from ai_phone.server.ws import agent_ws as server_agent_ws
    from ai_phone.shared import protocol as P

    # FastAPI resolves postponed endpoint annotations against module globals.
    # Keep the import lazy so a dry-run remains strictly stdlib/read-only.
    globals()["Request"] = Request
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.db_url == f"sqlite+aiosqlite:///{root / 'server.sqlite3'}"
    assert Path(settings.storage_dir).resolve() == root / "storage"
    assert not settings.android_vm_orphan_cleanup and not settings.android_vm_kill_foreign
    # Restrict the unchanged allocation function to a dedicated local slot.
    vm_service._EMULATOR_PORT_MIN = EMULATOR_PORT
    vm_service._EMULATOR_PORT_MAX = EMULATOR_PORT
    manager = AndroidVmManager(runtime_dir=root / "android/runtime", max_instances=1)
    host_id = load_host_identity(root / "storage")
    owned: set[str] = set()
    adb = SDK / "platform-tools/adb"
    agent = None
    agent_task = None
    server = None
    server_task = None
    listener = None
    private_adb_started = False
    private_adb_pid = None
    private_adb_create_time = None
    auth = {"Authorization": "Bearer " + token}
    prefix = "/api/internal/vm/instances"
    agent_id = "personal-e2e-" + run_id
    expected_token = "personal" + uuid.uuid4().hex[:12]
    witness_name = "personal-witness-" + run_id + ".txt"
    witness_payload = (json.dumps({"run_id": run_id, "witness_token": expected_token}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    witness_sha256 = hashlib.sha256(witness_payload).hexdigest()
    fixture_url = f"http://127.0.0.1:{FIXTURE_GUEST_PORT}/fixture/?" + urllib.parse.urlencode({"run": run_id, "platform": "android"})
    diagnostic_state = {"records": [], "first_write_error": None, "host_timing": [], "receipt_timing": [],
                        "native_calls": [], "avd_snapshots": [], "stop_sync_attempts": [], "api_stop_attempts": [], "api_lifecycle_attempts": [],
                        "witness_name": witness_name, "expected_witness_sha256": witness_sha256,
                        "expected_witness_bytes": len(witness_payload), "fixture_url": fixture_url,
                        "storage_key": "personal-vm-e2e:" + run_id,
                        "source_snapshot": str(source_snapshot), "original_html_sha256": digest(FIXTURE_HTML)}
    evidence.value["a3_diagnostic"] = diagnostic_state
    receipts = evidence.value["receipts"]
    native_pids = set()
    native_process_handles = {}
    first_binding_confirmed = False
    existing_instance_phase = False
    exit_samples = []
    evidence.value["native_exit_samples"] = exit_samples
    installation = {
        "package_id": None, "task_id": None, "item_id": None, "serial": None,
        "filename": None, "fixture_sha256": None, "vm_id": None, "armed": False,
        "handler_calls": 0, "native_calls": 0, "native_finished": False,
        "temporary_paths": [], "trace": [], "result_messages": [], "http_events": [],
        "guard_error": None,
    }
    evidence.value["server_app_install"] = installation
    original_install_apk = install_handler.install_apk
    original_manager_subprocess = android_manager_module.subprocess
    original_stop_owned_process = android_manager_module._stop_owned_process

    def timing(label, **details):
        # In-memory observations only: no added device command, I/O or delay.
        diagnostic_state["host_timing"].append({"label": label, "at": utc_now(),
                                               "monotonic_ns": time.monotonic_ns(), **details})

    def summarize_diagnostic():
        return diagnostic_summary(diagnostic_state["records"], witness_name=witness_name,
                                  witness_sha256=witness_sha256, witness_bytes=len(witness_payload),
                                  fixture_url=fixture_url, storage_key=diagnostic_state["storage_key"], token=expected_token)

    def native_generation(vm_id):
        avd_name = "aiphone_vm_" + vm_id
        launches = [row for row in diagnostic_state["native_calls"] if row.get("kind") == "native-launch"
                    and row.get("pid") is not None and avd_name in row.get("argv", [])]
        if not launches:
            return None
        latest = launches[-1]
        return str(latest["pid"]) + ":" + str(latest["monotonic_ns"])

    def cleanup_decision(vm_id):
        return cleanup_stop_decision(vm_id, native_generation(vm_id),
            diagnostic_state["stop_sync_attempts"] + diagnostic_state["api_stop_attempts"])

    def latest_api_operation(vm_id, action):
        matches = [row for row in diagnostic_state["api_lifecycle_attempts"] if row["vm_id"] == vm_id and row["action"] == action]
        if not matches:
            raise RuntimeError("No current HTTP lifecycle operation was recorded for this VM")
        return matches[-1]

    def finish_api_operation(vm_id, action, outcome, **details):
        record = latest_api_operation(vm_id, action)
        if outcome == "succeeded" and record["outcome"] in {"failed", "timeout", "timed_out", "cancelled"}:
            raise RuntimeError("A failed lifecycle attempt cannot be promoted by later physical facts")
        record.update(outcome=outcome, finished_at=utc_now(), finished_monotonic_ns=time.monotonic_ns(), **details)

    def finish_api_stop(vm_id, outcome, **details):
        finish_api_operation(vm_id, "stop", outcome, **details)

    class ManagerSubprocessObserver:
        """Delegate unchanged calls; observe existing argv/results, not new probes."""
        def __getattr__(self, name):
            return getattr(original_manager_subprocess, name)

        def Popen(self, args, *extra, **kwargs):
            argv = [str(x) for x in args]
            if "-avd" not in argv or argv[argv.index("-avd") + 1] not in {"aiphone_vm_" + key for key in owned}:
                raise RuntimeError("Diagnostic observer refused an unowned native launch")
            record = {"kind": "native-launch", "argv": argv, "at": utc_now(), "monotonic_ns": time.monotonic_ns()}
            diagnostic_state["native_calls"].append(record)
            try:
                process = original_manager_subprocess.Popen(args, *extra, **kwargs)
                record.update(pid=process.pid, returned_at=utc_now(), returned_monotonic_ns=time.monotonic_ns())
                native_pids.add(process.pid)
                native_process_handles[process.pid] = process
                return process
            except BaseException as exc:
                record.update(error=type(exc).__name__ + ": " + str(exc))
                raise

        def run(self, args, *extra, **kwargs):
            argv = [str(x) for x in args]
            command_environment = os.environ if kwargs.get("env") is None else kwargs["env"]
            guest_sync = validate_guest_sync_argv(argv, command_environment,
                adb_path=adb, serial=f"emulator-{EMULATOR_PORT}")
            if guest_sync:
                runtimes = [runtime for key, runtime in manager._runtimes.items()
                            if key in owned and runtime.adb_serial == f"emulator-{EMULATOR_PORT}"]
                if len(runtimes) != 1:
                    raise RuntimeError("Guest sync does not identify exactly one owned runtime")
            relevant = guest_sync or argv[-2:] == ["emu", "kill"] or any(
                argv[index:index + 2] in (["create", "avd"], ["delete", "avd"]) for index in range(len(argv) - 1))
            if not relevant:
                return original_manager_subprocess.run(args, *extra, **kwargs)
            record = {"kind": "native-command", "argv": argv, "at": utc_now(), "monotonic_ns": time.monotonic_ns()}
            if guest_sync:
                record.update(command_category="product-guest-filesystem-sync", vm_id=runtimes[0].vm_id,
                              generation_id=native_generation(runtimes[0].vm_id),
                              ack_marker=guest_sync["ack_marker"], expected_stdout=guest_sync["expected_stdout"],
                              private_adb_verified=True, returncode_is_not_completion_evidence=True)
            diagnostic_state["native_calls"].append(record)
            try:
                result = original_manager_subprocess.run(args, *extra, **kwargs)
                record.update(returncode=result.returncode, returned_at=utc_now(), returned_monotonic_ns=time.monotonic_ns(),
                              stdout=result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout,
                              stderr=result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr)
                return result
            except BaseException as exc:
                record.update(error=type(exc).__name__ + ": " + str(exc), returned_at=utc_now(), returned_monotonic_ns=time.monotonic_ns())
                raise

    def observed_stop_owned_process(process, *, graceful, **kwargs):
        record = {"kind": "owned-process-stop", "pid": process.pid, "emu_kill_succeeded": graceful,
                  "at": utc_now(), "monotonic_ns": time.monotonic_ns(), "calls": [], "forwarded_kwargs": dict(kwargs)}
        diagnostic_state["native_calls"].append(record)

        class ProcessObserver:
            def __getattr__(self, name):
                original = getattr(process, name)
                if name not in {"wait", "poll", "is_running", "terminate", "kill"}:
                    return original
                def invoke(*args, **kwargs):
                    call = {"method": name, "at": utc_now(), "monotonic_ns": time.monotonic_ns(), "kwargs": kwargs}
                    record["calls"].append(call)
                    try:
                        value = original(*args, **kwargs)
                        call.update(result=value, returned_monotonic_ns=time.monotonic_ns())
                        return value
                    except BaseException as exc:
                        call.update(error=type(exc).__name__ + ": " + str(exc), returned_monotonic_ns=time.monotonic_ns())
                        raise
                return invoke
        try:
            return original_stop_owned_process(ProcessObserver(), graceful=graceful, **kwargs)
        finally:
            record.update(returned_at=utc_now(), returned_monotonic_ns=time.monotonic_ns(),
                          fallback_signals=[call["method"] for call in record["calls"] if call["method"] in {"terminate", "kill"}])

    def avd_snapshot(vm_id, phase):
        assert_owned(vm_id)
        avd = root / "android/avd" / ("aiphone_vm_" + vm_id + ".avd")
        snapshot = {"phase": phase, "at": utc_now(), "monotonic_ns": time.monotonic_ns(),
                    "vm_id": vm_id, "avd_name": avd.stem, "path": str(avd), "files": []}
        for name in ("config.ini", "hardware-qemu.ini", "emu-launch-params.txt", "emulator-user.ini", "userdata-qemu.img", "userdata-qemu.img.qcow2"):
            path = avd / name
            if not path.exists():
                snapshot["files"].append({"path": str(path), "exists": False})
                continue
            metadata = path.stat()
            row = {"path": str(path), "exists": True, "inode": metadata.st_ino, "size": metadata.st_size,
                   "mtime_ns": metadata.st_mtime_ns, "birth_time": getattr(metadata, "st_birthtime", None)}
            if name.endswith((".ini", ".txt")) and metadata.st_size < 65536:
                row.update(sha256=digest(path), text=path.read_text())
            snapshot["files"].append(row)
        diagnostic_state["avd_snapshots"].append(snapshot)

    def activity_users(serial):
        with device_activity._lock:
            return device_activity._users.get(serial, 0)

    def observed_install_apk(serial, local_path, timeout_sec):
        # The production NativeOperation and installer execute unchanged. This
        # wrapper guards the test scope and records the real call stack/bytes.
        if serial != installation["serial"] or target(installation["vm_id"]) != serial:
            raise RuntimeError("Native APK installer received an unowned serial")
        local_path = Path(local_path)
        if not local_path.resolve().is_relative_to(root / "tmp") or digest(local_path) != installation["fixture_sha256"]:
            raise RuntimeError("Agent download does not match this run's personal APK")
        frame = inspect.currentframe()
        native_frame_found = False
        try:
            while frame is not None:
                if frame.f_code is NativeOperation._invoke.__code__ and isinstance(frame.f_locals.get("self"), NativeOperation):
                    native_frame_found = True
                    break
                frame = frame.f_back
        finally:
            del frame
        if not native_frame_found or activity_users(serial) < 1:
            raise RuntimeError("Native installer did not enter through a held real NativeOperation")
        installation["native_calls"] += 1
        if installation["native_calls"] != 1:
            raise RuntimeError("More than one native install attempted")
        installation["temporary_paths"].append(str(local_path))
        installation["trace"].append({"event": "native-install-enter", "at": utc_now(),
            "serial": serial, "local_path": str(local_path), "sha256": digest(local_path),
            "real_native_operation_in_stack": True, "activity_users": activity_users(serial)})
        try:
            result = original_install_apk(serial, local_path, timeout_sec)
            installation["trace"].append({"event": "native-install-return", "at": utc_now(),
                "success": bool(result[0]), "reason": str(result[1]),
                "temporary_file_present_until_native_return": local_path.exists()})
            return result
        finally:
            installation["native_finished"] = True

    async def guarded_install_start(client, msg):
        try:
            if not installation["armed"] or installation["handler_calls"]:
                raise RuntimeError("No new install message is authorized for this run")
            validate_install_message(msg, serial=installation["serial"], package_id=installation["package_id"],
                                     filename=installation["filename"], server_base=client.server_http_base)
            if await asyncio.to_thread(target, installation["vm_id"]) != installation["serial"]:
                raise RuntimeError("Install VM ownership is not confirmed")
            installation["handler_calls"] += 1
            # Task rows are committed before the existing Server dispatches WS.
            # This also covers a WS message arriving before POST /tasks returns.
            async with AsyncClient(base_url=client.server_http_base, timeout=10, trust_env=False) as verifier:
                response = await verifier.get("/api/app-install/tasks/" + msg["task_id"])
                response.raise_for_status()
                task = response.json()
            items = task.get("items") or []
            if task.get("package_id") != installation["package_id"] or len(items) != 1 or \
                    items[0].get("id") != msg["item_id"] or items[0].get("serial") != installation["serial"]:
                raise RuntimeError("WS install command does not match the one owned Server task")
            if installation["task_id"] and installation["task_id"] != msg["task_id"]:
                raise RuntimeError("Unexpected install task ID")
            installation.update(task_id=msg["task_id"], item_id=msg["item_id"])
            evidence.add("agent-app-install-handler", status="entered", command=dict(msg))
            await install_handler.handle_app_install_start(client, msg)
        except Exception as exc:
            installation["guard_error"] = str(exc)
            evidence.add("install-guard-rejected", error=str(exc))
            raise

    def observe_install_results(client):
        original_send = client.send

        async def send(payload):
            if payload.get("type") == P.MSG_APP_INSTALL_RESULT:
                if payload.get("task_id") != installation["task_id"] or payload.get("item_id") != installation["item_id"] or payload.get("serial") != installation["serial"]:
                    raise RuntimeError("Unowned install result")
                installation["result_messages"].append({**dict(payload), "observed_at": utc_now(),
                    "native_finished": installation["native_finished"],
                    "temporary_files_removed": all(not Path(p).exists() for p in installation["temporary_paths"])})
                evidence.flush()
            return await original_send(payload)

        client.send = send

    def adb_command(*args, timeout=30, binary=False):
        return command([adb, "-P", str(ADB_PORT), *args], timeout=timeout, binary=binary)

    def assert_owned(vm_id):
        if vm_id not in owned:
            raise RuntimeError("Refusing native action for a non-owned VM ID")
        avd = root / "android/avd" / ("aiphone_vm_" + vm_id + ".avd")
        if avd.is_symlink() or not avd.resolve().is_relative_to(root):
            raise RuntimeError("AVD path escaped the isolated root")

    def target(vm_id):
        assert_owned(vm_id)
        runtime = manager._runtimes.get(vm_id)
        if runtime is None or runtime.port != EMULATOR_PORT:
            raise RuntimeError("No runtime in the dedicated test slot")
        found = adb_command("-s", runtime.adb_serial, "emu", "avd", "name", timeout=10)
        if found.splitlines()[0].strip() != "aiphone_vm_" + vm_id:
            raise RuntimeError("Actual emulator identity does not match the recorded test VM")
        return runtime.adb_serial

    def owned_devices():
        rows = adb_command("devices", timeout=10).splitlines()[1:]
        connected = {row.split()[0] for row in rows if len(row.split()) >= 2 and row.split()[1] == "device"}
        expected = {rt.adb_serial for key, rt in manager._runtimes.items() if key in owned}
        if connected - expected:
            raise RuntimeError("Private ADB observed an unknown device; refusing discovery")
        infos = [DeviceInfo(serial=rt.adb_serial, platform="android", brand="Personal",
                            model="Isolated API33", screen_width=540, screen_height=960)
                 for key, rt in list(manager._runtimes.items())
                 if key in owned and rt.adb_serial in connected and rt.ready]
        return [x.to_dict() for x in manager.decorate_devices(infos)]

    def fresh_driver(vm_id):
        serial = target(vm_id)
        client = adbutils.AdbClient(host="127.0.0.1", port=ADB_PORT, socket_timeout=10)
        return AndroidDriver(client.device(serial), setup_power=False)

    async def collect_guest_network(vm_id):
        """Read only this fresh guest; preserve gateway limitations as evidence."""
        serial = await asyncio.to_thread(target, vm_id)
        diagnostics = {"at": utc_now(), "vm_id": vm_id, "serial": serial,
                       "scope": "read-only local guest diagnostics; not external network acceptance"}
        checks = {
            "interfaces": ["ip", "addr", "show"],
            "policy_rules": ["ip", "rule", "show"],
            "routes_all_tables": ["ip", "route", "show", "table", "all"],
            "connectivity": ["dumpsys", "connectivity"],
            "http_proxy": ["settings", "get", "global", "http_proxy"],
            "proxy_host": ["settings", "get", "global", "global_http_proxy_host"],
            "proxy_port": ["settings", "get", "global", "global_http_proxy_port"],
            "host_gateway_tcp": ["sh", "-c", shlex.quote(
                f"toybox nc -w 2 10.0.2.2 {listener.getsockname()[1]} </dev/null")],
        }
        for label, args in checks.items():
            try:
                result = await asyncio.to_thread(subprocess.run,
                    [str(adb), "-P", str(ADB_PORT), "-s", serial, "shell", *args],
                    capture_output=True, text=True, timeout=8)
                output = result.stdout.strip()
                if label == "connectivity":
                    output = "\n".join(line for line in output.splitlines() if any(word in line for word in (
                        "Active default network", "NetworkAgentInfo", "LinkProperties", "DnsAddresses", "Routes", "HttpProxy", "ProxyInfo")))[:15000]
                if label in {"http_proxy", "proxy_host"} and output not in {"", "null", ":0", "127.0.0.1", "localhost"}:
                    output = "<configured proxy host omitted>"
                output = re.sub(r"https?://\S+", "<URL omitted>", output)
                output = re.sub(r"\S*(?:yc345|yangcong345)\S*", "<company reference omitted>", output, flags=re.I)
                diagnostics[label] = {"returncode": result.returncode, "stdout": output,
                                      "stderr": result.stderr.strip()[:1000]}
            except subprocess.TimeoutExpired:
                diagnostics[label] = {"timeout_seconds": 8}
        path = root / "guest-network.json"
        path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n")
        evidence.add("guest-network-diagnostic", status="collected", vm_id=vm_id, path=str(path),
                     gateway_result=diagnostics.get("host_gateway_tcp"), external_network_validated=False)

    async def ready(vm_id, phase):
        serial = await asyncio.to_thread(target, vm_id)
        boot = await asyncio.to_thread(adb_command, "-s", serial, "shell", "getprop", "sys.boot_completed")
        package = await asyncio.to_thread(adb_command, "-s", serial, "shell", "service", "check", "package")
        if boot.strip() != "1" or not re.search(r"package:\s*found\b", package, re.I):
            raise RuntimeError("Native boot/package service not ready")
        driver = await asyncio.to_thread(fresh_driver, vm_id)
        runtime = manager._runtimes[vm_id]
        if runtime.process is not None:
            native_pids.add(runtime.process.pid)
            native_process_handles[runtime.process.pid] = runtime.process
        image = await asyncio.to_thread(driver.screenshot_png)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Native screenshot was not a PNG")
        path = root / (phase + ".png")
        path.write_bytes(image)
        await agent.refresh_devices()
        await agent.send({"type": P.MSG_DEVICE_READINESS, "serial": serial, "platform": "android",
                          "ready": True, "ts": time.time(), "hint": "real boot + package + screenshot probe"})
        async with AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                               timeout=10, trust_env=False) as readiness_http:
            await eventually(lambda: readiness_http.get("/api/devices/available"),
                             lambda r: r.status_code == 200 and any(
                                 d.get("serial") == serial for d in r.json()), timeout=15)
        evidence.add(phase, status="passed", vm_id=vm_id, serial=serial, screenshot=str(path),
                     sys_boot_completed=boot.strip(), package_service=package.strip(),
                     server_available_readback=True,
                     native_popen_pid=runtime.process.pid if runtime.process is not None else None,
                     native_popen_poll=runtime.process.poll() if runtime.process is not None else None)
        if phase in {"native-ready-first", "native-ready-restarted"}:
            avd_snapshot(vm_id, phase)
        return driver

    def owned_processes_alive():
        import psutil
        alive = []
        for pid in native_pids:
            try:
                args = psutil.Process(pid).cmdline()
            except psutil.NoSuchProcess:
                continue
            # PID reuse by an unrelated process is not a reason to touch it.
            if any("aiphone_vm_" + vm_id in args for vm_id in owned):
                alive.append(pid)
        return alive

    def observe_exit_verification(current_manager):
        """Record the actual stop instant without changing its decision/result."""
        original = current_manager._runtime_exit_confirmed

        def observed(runtime):
            confirmed = original(runtime)
            if runtime.vm_id not in owned:
                return confirmed
            matches, complete = android_manager_module._find_avd_processes(runtime.avd_name)
            bind_errno = {}
            for port in ((runtime.port, runtime.port + 1) if runtime.port else ()):
                values = {}
                for reuse in (False, True):
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        if reuse:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        try:
                            sock.bind(("127.0.0.1", port))
                            values["reuse" if reuse else "plain"] = 0
                        except OSError as exc:
                            values["reuse" if reuse else "plain"] = exc.errno
                bind_errno[str(port)] = values
            sample = {"at": utc_now(), "vm_id": runtime.vm_id, "confirmed": confirmed,
                      "popen_pid": runtime.process.pid if runtime.process is not None else None,
                      "popen_poll": runtime.process.poll() if runtime.process is not None else None,
                      "matching_pids": [proc.pid for proc, _port in matches],
                      "process_inventory_complete": complete, "bind_errno": bind_errno}
            if len(exit_samples) < 100:
                exit_samples.append(sample)
            return confirmed

        current_manager._runtime_exit_confirmed = observed
        observe_manager_stops(current_manager, native_generation, diagnostic_state["stop_sync_attempts"])

    observe_exit_verification(manager)

    def ui_xml(vm_id):
        serial = target(vm_id)
        adb_command("-s", serial, "shell", "uiautomator", "dump", "/sdcard/personal-e2e.xml", timeout=20)
        text = adb_command("-s", serial, "shell", "cat", "/sdcard/personal-e2e.xml", timeout=10)
        return text, ET.fromstring(text[text.index("<?xml"):])

    async def restart_visual_sample(vm_id, driver):
        started = time.monotonic()
        deadline = started + RESTART_VISUAL_TIMEOUT_SEC
        record = {"vm_id": vm_id, "started_at": utc_now(), "timeout_sec": RESTART_VISUAL_TIMEOUT_SEC,
                  "scope": "read-only native XML and real PNG after original restart receipt/count1", "samples": [], "selected_sample": None}
        evidence.value["restart_visual_gate"] = record
        folder = root / "restart-visual-samples"
        folder.mkdir()
        while time.monotonic() < deadline:
            index = len(record["samples"]) + 1
            sample = {"index": index, "started_at": utc_now(), "started_monotonic_ns": time.monotonic_ns()}
            record["samples"].append(sample)
            try:
                guest_xml = f"/sdcard/personal-a4-{run_id}-{index:02d}.xml"
                sample["guest_xml_path"] = guest_xml
                def native_xml_read():
                    # All calls use the existing private device through the
                    # same no-auto-start connection factory and one deadline.
                    assert_owned(vm_id)
                    runtime = manager._runtimes.get(vm_id)
                    if runtime is None or runtime.adb_serial != driver._device.serial:
                        raise RuntimeError("Native XML target no longer matches the verified owned runtime")
                    dump = driver._device.shell(["uiautomator", "dump", guest_xml])
                    if "dumped to: " + guest_xml not in str(dump):
                        raise RuntimeError("Native XML dump did not report a fresh successful capture")
                    text = driver._device.shell(["cat", guest_xml])
                    return text, ET.fromstring(text[text.index("<?xml"):])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Visual observation deadline expired before native XML")
                text, tree = await asyncio.to_thread(bounded_private_observation, driver, native_xml_read, min(8.0, remaining))
                xml_path = folder / f"sample-{index:02d}.xml"
                xml_path.write_text(text)
                sample.update(xml=str(xml_path), xml_read_at=utc_now())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Visual observation deadline expired after native XML")
                png = await asyncio.to_thread(bounded_private_observation, driver, driver.screenshot_png, min(5.0, remaining))
                image_path = folder / f"sample-{index:02d}.png"
                image_path.write_bytes(png)
                assessment = inspect_visual_sample(text, png, expected_token)
                sample.update(assessment=assessment, png=str(image_path), xml=str(xml_path))
                if assessment["ready"] and time.monotonic() <= deadline:
                    record.update(selected_sample=index, finished_at=utc_now(), elapsed_sec=time.monotonic() - started,
                                  outcome="native-tree-and-frame-ready-root-pixel-review-required")
                    sample.update(finished_at=utc_now(), elapsed_sec=(time.monotonic_ns() - sample["started_monotonic_ns"]) / 1e9)
                    evidence.flush()
                    return png, text, tree
            except Exception as exc:
                sample["observation_error"] = type(exc).__name__ + ": " + str(exc)
                sample.update(finished_at=utc_now(), elapsed_sec=(time.monotonic_ns() - sample["started_monotonic_ns"]) / 1e9)
                record.update(finished_at=utc_now(), elapsed_sec=time.monotonic() - started, outcome="failed-read-error")
                evidence.flush()
                raise RuntimeError("Read-only visual observation failed; no connection or UI recovery attempted") from exc
            sample.update(finished_at=utc_now(), elapsed_sec=(time.monotonic_ns() - sample["started_monotonic_ns"]) / 1e9)
            evidence.flush()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(0.25, remaining))
        record.update(finished_at=utc_now(), elapsed_sec=time.monotonic() - started, outcome="failed-timeout")
        evidence.flush()
        raise TimeoutError("Restart visual evidence timeout: exact native saved token/count1 and both real frame markers were not jointly observed; no UI recovery attempted")

    def center(node):
        numbers = [int(x) for x in re.findall(r"\d+", node.attrib.get("bounds", ""))]
        if len(numbers) != 4 or numbers[2] <= numbers[0] or numbers[3] <= numbers[1]:
            raise RuntimeError("Missing native bounds for fixture control")
        return ((numbers[0] + numbers[2]) // 2, (numbers[1] + numbers[3]) // 2)

    async def receipt(event, count, start=0):
        async def current_receipts():
            if diagnostic_state["first_write_error"]:
                raise RuntimeError("Durable witness first-write precondition failed: " + diagnostic_state["first_write_error"])
            return await asyncio.sleep(0, result=list(receipts[start:]))
        return await eventually(current_receipts,
                                lambda values: any(x["event"] == event and x["count"] == count
                                  and (count == 0 or x["token"] == expected_token) for x in values), timeout=35)

    async def visible_case(vm_id, *, expect_existing=False):
        serial = await asyncio.to_thread(target, vm_id)
        start = len(receipts)
        host_port = listener.getsockname()[1]
        await asyncio.to_thread(adb_command, "-s", serial, "reverse", f"tcp:{FIXTURE_GUEST_PORT}", f"tcp:{host_port}")
        reverse_readback = await asyncio.to_thread(adb_command, "-s", serial, "reverse", "--list")
        if f"tcp:{FIXTURE_GUEST_PORT} tcp:{host_port}" not in reverse_readback:
            raise RuntimeError("Private fixture reverse mapping was not confirmed")
        url = fixture_url
        phase = "restart-read" if expect_existing else "first-write"
        remote_command = "am start -n " + shlex.quote(APP_ID + "/.MainActivity") + " --es url " + shlex.quote(url)
        remote_command += " --es witness_mode " + shlex.quote(phase) + " --es witness_name " + shlex.quote(witness_name)
        if not expect_existing:
            remote_command += " --es witness_payload_b64 " + shlex.quote(base64.b64encode(witness_payload).decode())
        timing("activity-launch-requested", phase=phase)
        launch_output = await asyncio.to_thread(adb_command, "-s", serial, "shell", remote_command)
        timing("activity-launch-returned", phase=phase)
        evidence.add("fixture-open", vm_id=vm_id, url=url, output=launch_output.strip(),
                     witness_mode=phase,
                     transport="exact-serial private adb reverse", reverse_mapping=reverse_readback.strip())
        await receipt("load", 1 if expect_existing else 0, start)
        driver = await asyncio.to_thread(fresh_driver, vm_id)
        if not expect_existing:
            initial_png = await asyncio.to_thread(driver.screenshot_png)
            (root / "fixture-before.png").write_bytes(initial_png)
            text, tree = await asyncio.to_thread(ui_xml, vm_id)
            (root / "fixture-before.xml").write_text(text)
            fields = [n for n in tree.iter("node") if n.attrib.get("class") == "android.widget.EditText"]
            if len(fields) == 1:
                field_position = center(fields[0])
                evidence.add("fixture-input-locator", method="native accessibility", center=field_position)
            else:
                png = await asyncio.to_thread(driver.screenshot_png)
                (root / "fixture-before.png").write_bytes(png)
                geometry = await asyncio.to_thread(fixture_color_bounds, png, (82, 101, 122))
                field_position = geometry["center"]
                evidence.add("fixture-input-locator", method="fixed fixture screenshot pixels", **geometry)
            await asyncio.to_thread(driver.click, *field_position)
            await asyncio.sleep(0.3)
            await asyncio.to_thread(driver.type_text, expected_token)
            await asyncio.to_thread(adb_command, "-s", serial, "shell", "input", "keyevent", "4")
            await asyncio.sleep(0.5)
            text, tree = await asyncio.to_thread(ui_xml, vm_id)
            buttons = [n for n in tree.iter("node") if n.attrib.get("text") == "确认" or n.attrib.get("content-desc") == "确认"]
            if buttons:
                button_position = center(buttons[0])
                evidence.add("fixture-button-locator", method="native accessibility", center=button_position)
            else:
                png = await asyncio.to_thread(driver.screenshot_png)
                geometry = await asyncio.to_thread(fixture_color_bounds, png, (21, 90, 182))
                button_position = geometry["center"]
                evidence.add("fixture-button-locator", method="fixed fixture screenshot pixels", **geometry)
            timing("confirm-click-requested")
            await asyncio.to_thread(driver.click, *button_position)
            timing("confirm-click-returned")
            await receipt("confirm", 1, start)
        if expect_existing:
            image, text, _ = await restart_visual_sample(vm_id, driver)
        else:
            image = await asyncio.to_thread(driver.screenshot_png)
        label = "fixture-persisted" if expect_existing else "fixture-confirmed"
        path = root / (label + ".png")
        path.write_bytes(image)
        if not expect_existing:
            text, _ = await asyncio.to_thread(ui_xml, vm_id)
        (root / (label + ".xml")).write_text(text)
        native_ui_token_exposed = expected_token in text
        evidence.add(label, status="native-control-and-receipt-passed", vm_id=vm_id,
                     token=expected_token, count=1, screenshot=str(path),
                     native_accessibility_exposes_token=native_ui_token_exposed,
                     visual_review_required=True)
        evidence.value["visual_review_required"] = True

    if install_android.adb.host != "127.0.0.1" or install_android.adb.port != ADB_PORT:
        raise RuntimeError("Production APK adapter is not bound to the private ADB server")
    install_handler.install_apk = observed_install_apk
    android_manager_module.subprocess = ManagerSubprocessObserver()
    android_manager_module._stop_owned_process = observed_stop_owned_process

    try:
        fixture = await asyncio.to_thread(build_fixture, root)
        evidence.add("fixture-build", status="passed", path=str(fixture), sha256=digest(fixture),
                     source="self-contained personal Java WebView; no company package")
        await asyncio.to_thread(adb_command, "--one-device", "personal-e2e-no-usb-" + run_id, "start-server")
        private_adb_started = True
        import psutil
        private_daemons = [p for p in psutil.process_iter(["name", "cmdline", "cwd"])
                           if p.info.get("name") == "adb"
                           and ("personal-e2e-no-usb-" + run_id) in (p.info.get("cmdline") or [])
                           and p.info.get("cwd") == str(root)]
        if len(private_daemons) != 1:
            raise RuntimeError("Could not identify exactly one run-owned private ADB daemon")
        private_adb_pid = private_daemons[0].pid
        private_adb_create_time = private_daemons[0].create_time()
        if adb_command("devices").strip() != "List of devices attached":
            raise RuntimeError("New private ADB is not empty")
        await db_module.dispose_engine()
        db_module.init_engine(db_url=settings.db_url)
        from ai_phone.server import models  # noqa: F401
        app = FastAPI()
        app.state.hub = Hub()
        app.state.lock_store = DeviceLockStore()

        @app.middleware("http")
        async def observe_package_http(request: Request, call_next):
            response = await call_next(request)
            if request.url.path.startswith("/api/app-install/"):
                installation["http_events"].append({"at": utc_now(), "method": request.method,
                    "path": request.url.path, "status_code": response.status_code})
            return response

        include_routers(app)
        include_ws(app)
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        @app.get("/fixture/", response_class=HTMLResponse)
        async def fixture_page():
            return HTMLResponse((FIXTURE_HTML).read_text(), headers={"Cache-Control": "no-store"})

        @app.post("/fixture/receipt")
        async def fixture_receipt(request: Request):
            value = await request.json()
            if value.get("run") != run_id or value.get("platform") != "android":
                from fastapi import HTTPException
                evidence.add("fixture-receipt-rejected", submitted={k: value.get(k) for k in ("run", "platform", "event", "token", "count")})
                raise HTTPException(400, "wrong personal test scope")
            if value.get("event") not in ("load", "confirm") or not isinstance(value.get("count"), int):
                from fastapi import HTTPException
                raise HTTPException(400, "invalid receipt")
            receipts.append({k: value.get(k) for k in ("run", "platform", "event", "token", "count")})
            diagnostic_state["receipt_timing"].append({"at": utc_now(), "monotonic_ns": time.monotonic_ns(),
                **{k: value.get(k) for k in ("run", "platform", "event", "token", "count")}})
            evidence.flush()
            return {"ok": True}

        @app.post("/fixture/diagnostic")
        async def fixture_diagnostic(request: Request):
            value = await request.json()
            if value.get("protocol") != "personal-vm-a3" or value.get("run") != run_id or value.get("platform") != "android" or \
                    value.get("phase") not in {"first-write", "restart-read"} or \
                    value.get("kind") not in {"witness", "storage", "webview-provider"} or not isinstance(value.get("data"), dict):
                from fastapi import HTTPException
                raise HTTPException(400, "unexpected personal diagnostic scope")
            value["host_received_at"] = utc_now()
            value["host_received_monotonic_ns"] = time.monotonic_ns()
            diagnostic_state["records"].append(value)
            if value["phase"] == "first-write" and value["kind"] == "witness" and value["data"].get("ok") is not True:
                diagnostic_state["first_write_error"] = str(value["data"].get("error") or "unknown fsync/write error")
            # Persist with the next existing evidence flush; do not add disk
            # I/O or wait to the original confirmation-to-stop path.
            return {"ok": True}

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0)); listener.listen(128); listener.setblocking(False)
        port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        os.environ["AI_PHONE_SERVER_HTTP_BASE"] = base_url
        os.environ["AI_PHONE_SERVER_WS_URL"] = f"ws://127.0.0.1:{port}/ws/agent"
        get_settings.cache_clear()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                             lifespan="off", log_level="error", access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        await eventually(lambda: asyncio.sleep(0, result=server.started), bool)
        def guarded(handler):
            async def call(client, msg):
                vm_id = str(msg.get("vm_id") or "")
                assert_owned(vm_id)
                if msg.get("assigned_port") not in (None, EMULATOR_PORT):
                    raise RuntimeError("Server attempted to allocate outside the private slot")
                evidence.add("agent-command", message_type=msg["type"], vm_id=vm_id,
                             request_id=msg.get("request_id"))
                await handler(client, msg)
            return call

        async def bounded_pre_hello(_client):
            # These lists can only contain artifacts in this run's private AVD/ADB roots.
            actual_ids = {p.stem.removeprefix("aiphone_vm_") for p in (root / "android/avd").glob("aiphone_vm_*.avd")}
            if actual_ids - owned:
                raise RuntimeError("Unknown AVD in private test root; no reclaim attempted")
            rows = (await asyncio.to_thread(adb_command, "devices")).splitlines()[1:]
            connected = {row.split()[0] for row in rows if len(row.split()) >= 2}
            expected = {f"emulator-{EMULATOR_PORT}"} if owned else set()
            if connected - expected:
                raise RuntimeError("Unknown transport in private ADB; no reclaim attempted")
            for serial in connected:
                found = await asyncio.to_thread(adb_command, "-s", serial, "emu", "avd", "name", timeout=10)
                if found.splitlines()[0].strip() not in {"aiphone_vm_" + key for key in owned}:
                    raise RuntimeError("Private transport belongs to an unknown AVD; no reclaim attempted")
            await asyncio.to_thread(manager.reconcile_running_vms_sync)
            if set(manager._runtimes) - owned:
                raise RuntimeError("Native manager reclaimed an unknown runtime")

        async def bounded_reconnect(client):
            await manager.report_reclaimed_vms(client, rescan=False)

        def make_agent(current_agent_id):
            client = AgentWSClient(ws_url=f"ws://127.0.0.1:{port}/ws/agent", token=token,
                                   server_http_base=base_url, agent_id=current_agent_id,
                                   agent_name=current_agent_id,
                                   host_id=load_host_identity(root / "storage"),
                                   device_provider=owned_devices, ping_interval=5,
                                   rescan_interval=5, vm_command_concurrency=1)
            client.on(P.MSG_VM_CAPABILITY_PROBE, guarded(manager.handle_capability_probe))
            client.on(P.MSG_VM_START, guarded(manager.handle_start))
            client.on(P.MSG_VM_STOP, guarded(manager.handle_stop))
            client.on(P.MSG_VM_DELETE, guarded(manager.handle_delete))
            client.on(P.MSG_APP_INSTALL_START, guarded_install_start)
            observe_install_results(client)
            client.on_pre_hello(bounded_pre_hello)
            client.on_connect(bounded_reconnect)
            return client

        agent = make_agent(agent_id)
        agent_task = asyncio.create_task(agent.run_forever())
        await eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(agent_id)), bool)
        evidence.add("isolated-services", status="passed", base_url=base_url, agent_id=agent_id,
                     host_id=host_id, host_id_path=str(root / "storage/agent-host-id"),
                     proxy_environment_cleared=not any(k.lower().endswith("_proxy") and k.lower() != "no_proxy" for k in os.environ),
                     private_adb_port=ADB_PORT, private_adb_pid=private_adb_pid, shared_main_invoked=False)

        async with AsyncClient(base_url=base_url, timeout=300, trust_env=False) as http:
            async def api(method, path, **kwargs):
                operation_record = None
                if method == "POST" and path.endswith("/stop"):
                    timing("server-stop-requested", path=path)
                endpoint = path.rsplit("/", 1)[-1]
                if method == "POST" and endpoint in {"dispatch", "start", "stop"}:
                    operation_vm_id = path.rsplit("/", 2)[-2]
                    action = "stop" if endpoint == "stop" else "start"
                    operation_record = {"vm_id": operation_vm_id, "generation_id": native_generation(operation_vm_id),
                        "kind": "http-" + action, "action": action, "endpoint": endpoint, "outcome": "requested",
                        "at": utc_now(), "monotonic_ns": time.monotonic_ns()}
                    diagnostic_state["api_lifecycle_attempts"].append(operation_record)
                    if action == "stop":
                        diagnostic_state["api_stop_attempts"].append(operation_record)
                try:
                    response = await http.request(method, path, headers=auth, **kwargs)
                except BaseException as exc:
                    if operation_record is not None:
                        operation_record.update(outcome="failed", error=type(exc).__name__ + ": " + str(exc))
                    raise
                if method == "POST" and path.endswith("/stop"):
                    timing("server-stop-returned", status_code=response.status_code)
                if response.status_code >= 400:
                    if operation_record is not None:
                        operation_record.update(outcome="failed", status_code=response.status_code)
                    raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:2000]}")
                try:
                    body = response.json()
                    if operation_record is not None:
                        if not isinstance(body, dict) or body.get("sent") is not True:
                            raise RuntimeError("Lifecycle HTTP response did not explicitly send this operation")
                        instance = body.get("instance")
                        operation = require_operation(instance, vm_id=operation_record["vm_id"], action=operation_record["action"])
                        operation_record.update(request_id=operation["request_id"], response_operation=dict(operation))
                        operation_snapshot_ready(instance, vm_id=operation_record["vm_id"], action=operation_record["action"],
                            request_id=operation_record["request_id"], desired_state="stopped" if operation_record["action"] == "stop" else "running")
                    return body
                except BaseException as exc:
                    if operation_record is not None:
                        operation_record.update(outcome="failed", error=type(exc).__name__ + ": " + str(exc))
                    raise

            async def wait_vm(vm_id, state, timeout=240):
                action = "stop" if state == "stopped" else "start" if state == "running" else None
                expected_request_id = None
                async def read():
                    value = await api("GET", prefix + "/" + vm_id)
                    if action is not None:
                        operation_snapshot_ready(value, vm_id=vm_id, action=action, request_id=expected_request_id, desired_state=state)
                    if value["state"] == "error":
                        raise RuntimeError(f"VM operation failed: {value.get('error_message')}")
                    return value
                try:
                    if action is not None:
                        attempt = latest_api_operation(vm_id, action)
                        if attempt["outcome"] in {"failed", "timeout", "timed_out", "cancelled"}:
                            raise RuntimeError("This HTTP lifecycle attempt already failed")
                        expected_request_id = attempt.get("request_id")
                        if not expected_request_id:
                            raise RuntimeError("This HTTP lifecycle attempt has no confirmed request_id")
                    value = await eventually(read, lambda v: operation_snapshot_ready(v, vm_id=vm_id, action=action,
                        request_id=expected_request_id, desired_state=state) if action is not None else v["state"] == state, timeout=timeout)
                    if action is not None:
                        finish_api_operation(vm_id, action, "succeeded", server_state_observed=state, verified_request_id=expected_request_id)
                except BaseException as exc:
                    if action is not None:
                        finish_api_operation(vm_id, action, "failed", error=type(exc).__name__ + ": " + str(exc))
                    raise
                return value

            config = {"alias": "personal-e2e-" + run_id, "api_level": 33, "abi": "arm64",
                      "system_type": "google_apis", "screen_width": 540, "screen_height": 960, "density": 240,
                      "config_json": {"performance": {"ram_mb": 2048, "cpu_cores": 2, "gpu_mode": "host"},
                                      "storage": {"internal_storage_mb": 4096, "snapshot_policy": "no_snapshot", "wipe_data": False},
                                      "startup": {"no_window": True, "no_audio": True, "no_boot_anim": True}}}
            created = await api("POST", prefix, json=config)
            vm_id = created["id"]
            if not re.fullmatch(r"[0-9a-f]{12}", vm_id) or created.get("assigned_agent_id"):
                raise RuntimeError("Create did not return a new unbound VM configuration")
            owned.add(vm_id)
            evidence.value["owned_vm_ids"] = sorted(owned)
            (root / "ownership.json").write_text(json.dumps({"run_id": run_id, "vm_ids": sorted(owned), "agent_id": agent_id}, indent=2))
            evidence.add("create", status="passed", vm_id=vm_id, state=created["state"], bound=False)
            candidates = await api("POST", prefix + "/" + vm_id + "/dispatch-candidates")
            if not any(row.get("agent_id") == agent_id and row.get("ok") for row in candidates.get("agents", [])):
                raise RuntimeError("Real Agent capability probe did not accept this isolated host")
            evidence.add("capability", status="passed", result=candidates)
            await api("POST", prefix + "/" + vm_id + "/dispatch", json={"agent_id": agent_id})
            started = await wait_vm(vm_id, "running")
            await ready(vm_id, "native-ready-first")
            await collect_guest_network(vm_id)
            first_binding_confirmed = True
            evidence.add("bind-start", status="passed", vm_id=vm_id, assigned_agent_id=started["assigned_agent_id"],
                         adb_serial=started["adb_serial"], native_ready_and_server_available_confirmed=True)
            serial = await asyncio.to_thread(target, vm_id)
            before_package = await asyncio.to_thread(subprocess.run,
                [str(adb), "-P", str(ADB_PORT), "-s", serial, "shell", "pm", "list", "packages", APP_ID],
                capture_output=True, text=True, timeout=30)
            absence = validate_fixture_package_absent(before_package.returncode, before_package.stdout, before_package.stderr)
            evidence.add("fixture-package-absent", status="passed", serial=serial, probe=absence)
            temporary_before = await asyncio.to_thread(adb_command, "-s", serial, "shell", "ls", "-1", "/data/local/tmp")
            temporary_before = sorted(name for name in temporary_before.splitlines() if name.startswith("ai-phone-install-"))
            with fixture.open("rb") as stream:
                package = await api("POST", "/api/app-install/packages",
                                    files={"file": (fixture.name, stream, "application/vnd.android.package-archive")})
            package_id = str(package.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", package_id) or package.get("platform") != "android" or package.get("filename") != fixture.name:
                raise RuntimeError("Server upload response does not identify this personal Android APK")
            async with db_module.get_session_factory()() as package_session:
                saved = await package_session.get(AppPackage, package_id)
                if saved is None:
                    raise RuntimeError("Uploaded package row is missing from the isolated Server")
                saved_path = package_file_path(saved)
                if not saved_path.resolve().is_relative_to(root / "storage/app-install") or digest(saved_path) != digest(fixture):
                    raise RuntimeError("Server uploaded file does not match the personal build")
            installation.update(package_id=package_id, serial=serial, vm_id=vm_id,
                                filename=fixture.name, fixture_sha256=digest(fixture), server_file=str(saved_path))
            evidence.add("server-package-upload", status="passed", package=package,
                         server_file=str(saved_path), sha256=digest(fixture))
            eligible = await api("GET", f"/api/app-install/packages/{package_id}/eligible-devices")
            if [item.get("serial") for item in eligible] != [serial]:
                raise RuntimeError("The isolated package eligibility list is not exactly the owned VM")
            installation["armed"] = True
            task = await api("POST", "/api/app-install/tasks", json={"package_id": package_id, "serials": [serial]})
            task_id = str(task.get("id") or "")
            if not task_id or (installation["task_id"] and installation["task_id"] != task_id):
                raise RuntimeError("Task response conflicts with the Agent install command")
            installation["task_id"] = task_id
            evidence.add("server-install-task-created", task=task)

            async def install_task_state():
                if installation["guard_error"]:
                    raise RuntimeError("Agent installation guard rejected the command: " + installation["guard_error"])
                value = await api("GET", "/api/app-install/tasks/" + task_id)
                items = value.get("items") or []
                if len(items) != 1 or items[0].get("serial") != serial:
                    raise RuntimeError("Install task changed its target set")
                if items[0].get("state") in {"failed", "timeout", "unknown"}:
                    evidence.add("server-install-task-failed", task=value)
                    raise RuntimeError("Native install task did not succeed: " + str(items[0].get("reason")))
                return value

            terminal = await eventually(install_task_state,
                lambda value: value.get("state") == "done" and value["items"][0].get("state") == "success",
                timeout=630, interval=0.5)
            validate_terminal_install_task(terminal, task_id=task_id, item_id=installation["item_id"],
                                           package_id=package_id, serial=serial)
            await eventually(lambda: asyncio.sleep(0, result=(activity_users(serial) == 0 and
                not any(not job.done() for job in install_handler._TASKS))), bool, timeout=5)
            installation["armed"] = False
            if installation["handler_calls"] != 1 or installation["native_calls"] != 1 or \
                    len(installation["result_messages"]) != 1 or not installation["result_messages"][0].get("success") or \
                    not installation["result_messages"][0].get("native_finished") or \
                    len(installation["temporary_paths"]) != 1 or not installation["result_messages"][0].get("temporary_files_removed"):
                raise RuntimeError("Install execution/result/temporary cleanup proof is incomplete")
            download_path = f"/api/app-install/packages/{package_id}/download"
            if sum(item["method"] == "GET" and item["path"] == download_path and item["status_code"] == 200
                   for item in installation["http_events"]) != 1:
                raise RuntimeError("Did not observe exactly one successful Agent package download")
            package_path = await asyncio.to_thread(adb_command, "-s", serial, "shell", "pm", "path", APP_ID)
            if not package_path.startswith("package:"):
                raise RuntimeError("Personal APK install did not finish successfully")
            temporary_after = await asyncio.to_thread(adb_command, "-s", serial, "shell", "ls", "-1", "/data/local/tmp")
            temporary_after = sorted(name for name in temporary_after.splitlines() if name.startswith("ai-phone-install-"))
            if temporary_before != temporary_after:
                raise RuntimeError("Native installer left an additional temporary APK on the owned guest")
            evidence.add("server-app-install-terminal", status="passed", task=terminal, package=APP_ID,
                         native_path=package_path.strip(), activity_users_after=activity_users(serial),
                         guest_temp_before=temporary_before, guest_temp_after=temporary_after,
                         direct_adb_install_used=False)
            await visible_case(vm_id)

            old_socket = agent._ws
            old_pid = manager._runtimes[vm_id].process.pid
            await old_socket.close()
            await eventually(lambda: asyncio.sleep(0, result=agent._ws is not None and agent._ws is not old_socket
                                                      and app.state.hub.has_agent(agent_id)), bool, timeout=30)
            await wait_vm(vm_id, "running", timeout=30)
            await ready(vm_id, "native-ready-reconnected")
            if manager._runtimes[vm_id].process.pid != old_pid:
                raise RuntimeError("VM process changed during Agent connection-only reconnect")
            evidence.add("websocket-reconnect", status="passed", vm_id=vm_id, unchanged_emulator_pid=old_pid,
                         scope="connection reconnect with bounded real manager reclaim; not Agent process crash")

            previous_agent_id = agent_id
            await agent.stop()
            await asyncio.wait_for(agent_task, 10)
            await eventually(lambda: asyncio.sleep(0, result=not app.state.hub.has_agent(previous_agent_id)), bool)
            # Reconstruct all process-local Manager state, then reclaim only the private AVD/ADB scope.
            # This is a state reconstruction check; the Python OS process is deliberately not killed.
            for runtime in manager._runtimes.values():
                if runtime.log_file is not None:
                    runtime.log_file.close()
            manager = AndroidVmManager(runtime_dir=root / "android/runtime", max_instances=1)
            observe_exit_verification(manager)
            if manager._runtimes:
                raise RuntimeError("Replacement manager unexpectedly started with in-memory runtimes")
            agent_id = "personal-e2e-restart-" + run_id
            if load_host_identity(root / "storage") != host_id:
                raise RuntimeError("Persisted installation identity changed across reconstruction")
            agent = make_agent(agent_id)
            agent_task = asyncio.create_task(agent.run_forever())
            await eventually(lambda: asyncio.sleep(0, result=app.state.hub.has_agent(agent_id)), bool)
            reconstructed = await wait_vm(vm_id, "running", timeout=30)
            if reconstructed.get("assigned_agent_id") != agent_id:
                raise RuntimeError("Server did not preserve VM ownership for the same host with a new Agent ID")
            if old_pid not in owned_processes_alive():
                raise RuntimeError("Native emulator process did not survive Manager/client reconstruction")
            await ready(vm_id, "native-ready-reconstructed")
            evidence.add("agent-manager-reconstruction", status="passed", vm_id=vm_id,
                         previous_agent_id=previous_agent_id, new_agent_id=agent_id,
                         persisted_host_id=host_id, unchanged_emulator_pid=old_pid,
                         os_process_restart_tested=False)

            avd_path = root / "android/avd" / ("aiphone_vm_" + vm_id + ".avd")
            existing_instance_phase = True
            await api("POST", prefix + "/" + vm_id + "/stop")
            stopped = await wait_vm(vm_id, "stopped")
            if not avd_path.is_dir() or not android_manager_module._is_port_free(EMULATOR_PORT) or not android_manager_module._is_port_free(EMULATOR_PORT + 1):
                finish_api_stop(vm_id, "failed", reason="native stop preservation/port verification failed")
                raise RuntimeError("Stop did not preserve AVD and release both emulator ports")
            if owned_processes_alive():
                finish_api_stop(vm_id, "failed", reason="owned emulator remained alive after reported stop")
                raise RuntimeError("Stop left an owned emulator process alive")
            if stopped.get("assigned_agent_id") != agent_id:
                finish_api_stop(vm_id, "failed", reason="stop lost binding")
                raise RuntimeError("Stop lost the existing Agent binding")
            evidence.add("stop", status="passed", avd_preserved=True, binding_preserved=True, ports_released=True)
            await api("POST", prefix + "/" + vm_id + "/start")
            await wait_vm(vm_id, "running")
            await ready(vm_id, "native-ready-restarted")
            await visible_case(vm_id, expect_existing=True)
            observed_diagnostic = summarize_diagnostic()
            evidence.add("a3-dual-track-diagnostic", result=observed_diagnostic)
            if not observed_diagnostic["evidence_complete"]:
                raise RuntimeError("Diagnostic evidence incomplete; no inference of missing file or localStorage")
            if not observed_diagnostic["actual_webview_versions_observed"]:
                raise RuntimeError("Actual WebView provider version evidence incomplete")
            if not observed_diagnostic["durable_witness_preserved"]:
                raise RuntimeError("Durable app-private witness did not pass first-write/restart-read checks")
            if not observed_diagnostic["raw_storage_preserved"]:
                raise RuntimeError("Raw localStorage readback did not confirm the original saved token/count")
            await api("POST", prefix + "/" + vm_id + "/stop")
            await wait_vm(vm_id, "stopped")
            existing_instance_phase = False
            deleted = await api("DELETE", prefix + "/" + vm_id)
            await eventually(lambda: http.get(prefix + "/" + vm_id, headers=auth), lambda r: r.status_code == 404, timeout=45)
            evidence.add("ledger-delete", status="passed", vm_id=vm_id, response=deleted, server_absent=True)
            # The Server deletes its ledger immediately and sends one native
            # delete. Only poll evidence here; do not redispatch or recreate.
            await eventually(lambda: asyncio.sleep(0, result=(
                not avd_path.exists() and not avd_path.with_suffix(".ini").exists()
                and vm_id not in manager._runtimes and not owned_processes_alive())), bool, timeout=45)
            if avd_path.exists() or avd_path.with_suffix(".ini").exists() or vm_id in manager._runtimes:
                raise RuntimeError("Delete left native AVD metadata or manager runtime")
            if not android_manager_module._is_port_free(EMULATOR_PORT) or not android_manager_module._is_port_free(EMULATOR_PORT + 1):
                raise RuntimeError("Delete left an occupied emulator port")
            if owned_processes_alive():
                raise RuntimeError("Delete left an owned emulator process alive")
            evidence.add("delete", status="passed", vm_id=vm_id, server_absent=True, native_avd_absent=True, ports_released=True, native_processes_absent=True)
        if installation["guard_error"]:
            raise RuntimeError("Install scope guard observed an unexpected command: " + installation["guard_error"])
        evidence.value["status"] = "native_checks_passed_visual_review_pending"
    except BaseException as exc:
        evidence.value["status"] = "failed"
        evidence.add("failure", status="failed", error=type(exc).__name__ + ": " + str(exc))
        # Capture the exact owned screen before normal cleanup; diagnostics do
        # not retry the failed lifecycle operation or broaden device selection.
        gate_failed = str(evidence.value.get("restart_visual_gate", {}).get("outcome") or "").startswith("failed")
        failed_stop = any(not cleanup_decision(key)["allowed"] for key in owned)
        if gate_failed or failed_stop:
            evidence.add("failure-screen", skipped=True, reason="Use saved evidence; no extra screenshot or connection recovery after failed visual gate or failed/unresolved stop")
        for key in ([] if gate_failed or failed_stop else sorted(owned & set(manager._runtimes))):
            try:
                driver = await asyncio.to_thread(fresh_driver, key)
                png = await asyncio.to_thread(driver.screenshot_png)
                image_path = root / ("failure-" + key + ".png")
                image_path.write_bytes(png)
                evidence.add("failure-screen", vm_id=key, screenshot=str(image_path))
            except Exception as diagnostic_error:
                evidence.add("failure-screen", vm_id=key, unavailable=str(diagnostic_error))
        raise
    finally:
        installation["armed"] = False
        pending_install_tasks = [job for job in install_handler._TASKS if not job.done()]
        if pending_install_tasks:
            # Wait once for already-owned work; never cancel/reissue a native
            # install just to make VM cleanup look complete.
            await asyncio.wait(pending_install_tasks, timeout=30)
        install_work_active = bool(
            any(not job.done() for job in install_handler._TASKS)
            or (installation["native_calls"] and not installation["native_finished"])
            or (installation["serial"] and activity_users(installation["serial"]))
        )
        evidence.value["native_install_active_at_cleanup"] = install_work_active
        if install_work_active:
            evidence.add("native-install-still-active", status="failed",
                         cleanup_skipped=True, reason="Preserve owned VM/ADB/temp resources until native exit; no retry")
        else:
            install_handler.install_apk = original_install_apk
        # Normal native deletion ONLY for IDs created in this run, even if start
        # failed before registering a runtime. Never rmtree or force-kill.
        preserve_existing_failure = evidence.value["status"] == "failed" and first_binding_confirmed and existing_instance_phase
        blocked_cleanup_vm_ids = []
        evidence.value["cleanup_stop_blocked_for_vm_ids"] = blocked_cleanup_vm_ids
        for vm_id in ([] if install_work_active else sorted(owned)):
            try:
                assert_owned(vm_id)
                decision = cleanup_decision(vm_id)
                if not decision["allowed"]:
                    blocked_cleanup_vm_ids.append(vm_id)
                    evidence.add("cleanup-stop-retry-blocked", vm_id=vm_id, decision=decision,
                                 stop_and_delete_skipped=True,
                                 reason="Keep failed stop facts and current resources; root must authorize any further native action")
                    continue
                stopped = await asyncio.to_thread(manager.stop_sync, vm_id)
                if preserve_existing_failure:
                    avd_path = root / "android/avd" / ("aiphone_vm_" + vm_id + ".avd")
                    evidence.add("existing-instance-failure-preservation", vm_id=vm_id,
                                 avd_preserved=avd_path.is_dir(), stopped=stopped,
                                 cleanup_delete_skipped=True,
                                 reason="Preserve original data evidence after failed stop/restart; no recreate or retry.")
                else:
                    result = await asyncio.to_thread(manager.delete_sync, vm_id) if stopped.get("ok") else stopped
                    evidence.add("owned-cleanup", vm_id=vm_id, stopped=stopped, deletion=result)
            except Exception as exc:
                evidence.add("owned-cleanup", vm_id=vm_id, error=str(exc))
        if agent is not None:
            await agent.stop()
        if agent_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(agent_task, 10)
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server_task, 10)
        if listener is not None:
            listener.close()
        worker = server_agent_ws._persist_worker
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await close_all_sessions()
        await db_module.dispose_engine()
        if private_adb_started and not manager._runtimes and not install_work_active and not blocked_cleanup_vm_ids:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(adb_command, "kill-server", timeout=10)
        for process in native_process_handles.values():
            if process.poll() is not None:
                with contextlib.suppress(Exception):
                    process.wait(timeout=0)
        def private_daemon_gone():
            if private_adb_pid is None:
                return not private_adb_started
            try:
                process = psutil.Process(private_adb_pid)
                return process.create_time() != private_adb_create_time or not process.is_running()
            except psutil.NoSuchProcess:
                return True
        if not blocked_cleanup_vm_ids:
            with contextlib.suppress(TimeoutError):
                await eventually(lambda: asyncio.sleep(0, result=private_daemon_gone()), bool, timeout=5)
        # Process identity, not residual TIME_WAIT, proves our daemon exited.
        private_adb_stopped = private_daemon_gone()
        remaining_avds = sorted(str(p) for p in (root / "android/avd").iterdir()
                                if p.suffix in {".avd", ".ini"})
        after = protected_fingerprint()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        evidence.value["protected_after"] = after
        evidence.value["protected_metadata_changed"] = changed
        evidence.value["remaining_owned_runtimes"] = sorted(manager._runtimes)
        evidence.value["remaining_private_avds"] = remaining_avds
        evidence.value["preserved_after_existing_instance_failure"] = preserve_existing_failure
        evidence.value["private_adb_stopped"] = private_adb_stopped
        evidence.value["finished_at"] = utc_now()
        android_manager_module.subprocess = original_manager_subprocess
        android_manager_module._stop_owned_process = original_stop_owned_process
        diagnostic_state["summary"] = summarize_diagnostic()
        adb_read_path["source_mismatches_at_finish"] = [name for name, record in adb_read_path["files"].items()
            if digest(Path(record["path"])) != record["sha256"]]
        try:
            all_source_hashes = {name: digest(REPO / name) for name in evidence.value["source_hashes_at_start"]}
            evidence.value["source_hashes_at_finish"] = all_source_hashes
            diagnostic_state["source_mismatches"] = [name for name, sha in evidence.value["source_hashes_at_start"].items()
                                                       if all_source_hashes[name] != sha]
        except Exception as exc:
            diagnostic_state["source_mismatches"] = [str(exc)]
        try:
            evidence.value["frozen_products_at_finish"] = frozen_products()
            freeze_changed = bool(evidence.value["frozen_products_at_finish"]["mismatches"])
        except Exception as exc:
            freeze_changed = True
            evidence.value["frozen_product_check_error"] = str(exc)
        if changed or manager._runtimes or remaining_avds or not private_adb_stopped or install_work_active or freeze_changed or diagnostic_state["source_mismatches"] or adb_read_path["source_mismatches_at_finish"]:
            evidence.value["status"] = "failed"
        evidence.flush()
        print(json.dumps({"status": evidence.value["status"], "evidence": str(root / "evidence.json"),
                          "remaining_owned_runtimes": evidence.value["remaining_owned_runtimes"],
                          "protected_metadata_changed": changed}, ensure_ascii=False))
    if evidence.value["status"] not in {"passed", "native_checks_passed_visual_review_pending"}:
        raise RuntimeError("Cleanup or protected-baseline verification failed; inspect evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Run only the isolated personal Android test")
    mode.add_argument("--dry-run", action="store_true", help="Print plan; default")
    parser.add_argument("--run-id", default="android-app-install-a5-" + dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    args = parser.parse_args()
    if not re.fullmatch(r"android-[A-Za-z0-9_-]{8,70}", args.run_id):
        parser.error("run-id must be an ASCII android- test identifier")
    if not args.execute:
        print(json.dumps(plan(args.run_id), ensure_ascii=False, indent=2))
        return
    asyncio.run(execute(args.run_id))


if __name__ == "__main__":
    main()
