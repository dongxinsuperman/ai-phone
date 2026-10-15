#!/usr/bin/env python3
"""Opt-in personal Android lifecycle E2E, using real HTTP/WS and VM manager.

Default: print the plan only. No imports of ai_phone, network, directory creation,
ADB daemon, emulator, or package build occur without --execute.

This is synthetic native-chain acceptance, not a VLM/business-Case run. The
private WebView APK is built from the source below and renders the shared local
personal-fixture.html. No company service, package, account or model is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
SDK = Path.home() / "Library/Android/sdk"
DEFAULT_PARENT = Path.home() / ".codex/personal-vm-e2e"
APP_ID = "org.personal.aiphone.lifecycle"
ADB_PORT = 15037
EMULATOR_PORT = 5666
FIXTURE_GUEST_PORT = 19080

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

JAVA = '''package org.personal.aiphone.lifecycle;
import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebViewClient;
public final class MainActivity extends Activity {
 @Override public void onCreate(Bundle state) {
  super.onCreate(state);
  WebView view = new WebView(this);
  view.getSettings().setJavaScriptEnabled(true);
  view.getSettings().setDomStorageEnabled(true);
  view.setWebViewClient(new WebViewClient());
  setContentView(view);
  String url = getIntent().getStringExtra("url");
  if (url == null || !url.startsWith("http://127.0.0.1:19080/fixture/")) {
   throw new IllegalArgumentException("personal loopback fixture URL required");
  }
  android.util.Log.i("PersonalVmE2E", "fixture-url=" + url);
  view.loadUrl(url);
 }
}'''


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        "fixture_html": str(HERE / "personal-fixture.html"),
        "ios_device_set_plan": str(root / "ios/devices"),
        "harmony_instance_path_plan": str(root / "harmony/runtime/instances"),
    }


def plan(run_id: str):
    return {
        "mode": "dry-run", "platform": "android", "run_id": run_id,
        "source_repo": str(REPO), "paths": planned_paths(run_id),
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
            "install personal APK", "shared HTML input/click/screenshot/receipt",
            "WS reconnect without stopping VM", "stop and verify native exit",
            "start same AVD and verify token/count persistence", "stop, delete, verify native and Server absence",
        ],
        "not_covered": ["VLM execution", "company business Case", "physical device control", "guest external networking", "iOS/Harmony (separate harnesses)"],
        "execute_command": f"<personal backend venv>/bin/python {Path(__file__).resolve()} --execute --run-id {run_id}",
    }


class Evidence:
    def __init__(self, root: Path, run_id: str):
        self.root = root
        self.value = {
            "run_id": run_id, "platform": "android", "started_at": utc_now(),
            "classification": "synthetic native-chain E2E; not VLM/business acceptance",
            "status": "running", "stages": [], "owned_vm_ids": [], "receipts": [],
        }

    def flush(self):
        target = self.root / "evidence.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.value, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(target)

    def add(self, stage, **details):
        self.value["stages"].append({"stage": stage, "at": utc_now(), **details})
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
    evidence.value["paths"] = planned_paths(run_id)
    evidence.value["protected_before"] = before
    source_files = [
        Path(__file__).resolve(), HERE / "personal-fixture.html",
        REPO / "backend/ai_phone/agent/android_vm/manager.py",
        REPO / "backend/ai_phone/agent/ws_client.py",
        REPO / "backend/ai_phone/agent/identity.py",
        REPO / "backend/ai_phone/agent/drivers/android.py",
        REPO / "backend/ai_phone/server/android_vm/api.py",
        REPO / "backend/ai_phone/server/android_vm/service.py",
        REPO / "backend/ai_phone/server/ws/agent_ws.py",
        *list((REPO / "backend/ai_phone/server").glob("*lifecycle*.py")),
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
    from ai_phone.agent.drivers.android import AndroidDriver
    from ai_phone.agent.drivers.base import DeviceInfo
    from ai_phone.agent.ws_client import AgentWSClient
    from ai_phone.config import get_settings
    from ai_phone.server import db as db_module
    from ai_phone.server.api import include_routers
    from ai_phone.server.android_vm import service as vm_service
    from ai_phone.server.db import Base
    from ai_phone.server.hub import Hub
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
    receipts = evidence.value["receipts"]
    native_pids = set()
    native_process_handles = {}
    first_binding_confirmed = False
    existing_instance_phase = False
    exit_samples = []
    evidence.value["native_exit_samples"] = exit_samples

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

    observe_exit_verification(manager)

    def ui_xml(vm_id):
        serial = target(vm_id)
        adb_command("-s", serial, "shell", "uiautomator", "dump", "/sdcard/personal-e2e.xml", timeout=20)
        text = adb_command("-s", serial, "shell", "cat", "/sdcard/personal-e2e.xml", timeout=10)
        return text, ET.fromstring(text[text.index("<?xml"):])

    def center(node):
        numbers = [int(x) for x in re.findall(r"\d+", node.attrib.get("bounds", ""))]
        if len(numbers) != 4 or numbers[2] <= numbers[0] or numbers[3] <= numbers[1]:
            raise RuntimeError("Missing native bounds for fixture control")
        return ((numbers[0] + numbers[2]) // 2, (numbers[1] + numbers[3]) // 2)

    async def receipt(event, count, start=0):
        return await eventually(lambda: asyncio.sleep(0, result=list(receipts[start:])),
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
        url = f"http://127.0.0.1:{FIXTURE_GUEST_PORT}/fixture/?" + urllib.parse.urlencode({"run": run_id, "platform": "android"})
        remote_command = "am start -n " + shlex.quote(APP_ID + "/.MainActivity") + " --es url " + shlex.quote(url)
        launch_output = await asyncio.to_thread(adb_command, "-s", serial, "shell", remote_command)
        evidence.add("fixture-open", vm_id=vm_id, url=url, output=launch_output.strip(),
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
            await asyncio.to_thread(driver.click, *button_position)
            await receipt("confirm", 1, start)
        image = await asyncio.to_thread(driver.screenshot_png)
        label = "fixture-persisted" if expect_existing else "fixture-confirmed"
        path = root / (label + ".png")
        path.write_bytes(image)
        text, _ = await asyncio.to_thread(ui_xml, vm_id)
        (root / (label + ".xml")).write_text(text)
        native_ui_token_exposed = expected_token in text
        evidence.add(label, status="native-control-and-receipt-passed", vm_id=vm_id,
                     token=expected_token, count=1, screenshot=str(path),
                     native_accessibility_exposes_token=native_ui_token_exposed,
                     visual_review_required=True)
        evidence.value["visual_review_required"] = True

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
        include_routers(app)
        include_ws(app)
        async with db_module.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        @app.get("/fixture/", response_class=HTMLResponse)
        async def fixture_page():
            return HTMLResponse((HERE / "personal-fixture.html").read_text(), headers={"Cache-Control": "no-store"})

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
            evidence.flush()
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
                response = await http.request(method, path, headers=auth, **kwargs)
                if response.status_code >= 400:
                    raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:2000]}")
                return response.json()

            async def wait_vm(vm_id, state, timeout=240):
                async def read():
                    value = await api("GET", prefix + "/" + vm_id)
                    if value["state"] == "error":
                        raise RuntimeError(f"VM operation failed: {value.get('error_message')}")
                    return value
                return await eventually(read, lambda v: v["state"] == state, timeout=timeout)

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
            installed = await asyncio.to_thread(adb_command, "-s", serial, "install", str(fixture), timeout=90)
            package_path = await asyncio.to_thread(adb_command, "-s", serial, "shell", "pm", "path", APP_ID)
            if "Success" not in installed or not package_path.startswith("package:"):
                raise RuntimeError("Personal APK install did not finish successfully")
            evidence.add("fixture-install", status="passed", package=APP_ID, native_path=package_path.strip())
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
                raise RuntimeError("Stop did not preserve AVD and release both emulator ports")
            if owned_processes_alive():
                raise RuntimeError("Stop left an owned emulator process alive")
            if stopped.get("assigned_agent_id") != agent_id:
                raise RuntimeError("Stop lost the existing Agent binding")
            evidence.add("stop", status="passed", avd_preserved=True, binding_preserved=True, ports_released=True)
            await api("POST", prefix + "/" + vm_id + "/start")
            await wait_vm(vm_id, "running")
            await ready(vm_id, "native-ready-restarted")
            await visible_case(vm_id, expect_existing=True)
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
        evidence.value["status"] = "native_checks_passed_visual_review_pending"
    except BaseException as exc:
        evidence.value["status"] = "failed"
        evidence.add("failure", status="failed", error=type(exc).__name__ + ": " + str(exc))
        # Capture the exact owned screen before normal cleanup; diagnostics do
        # not retry the failed lifecycle operation or broaden device selection.
        for key in sorted(owned & set(manager._runtimes)):
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
        # Normal native deletion ONLY for IDs created in this run, even if start
        # failed before registering a runtime. Never rmtree or force-kill.
        preserve_existing_failure = evidence.value["status"] == "failed" and first_binding_confirmed and existing_instance_phase
        for vm_id in sorted(owned):
            try:
                assert_owned(vm_id)
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
        if private_adb_started and not manager._runtimes:
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
        if changed or manager._runtimes or remaining_avds or not private_adb_stopped:
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
    parser.add_argument("--run-id", default="android-" + dt.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    args = parser.parse_args()
    if not re.fullmatch(r"android-[A-Za-z0-9_-]{8,70}", args.run_id):
        parser.error("run-id must be an ASCII android- test identifier")
    if not args.execute:
        print(json.dumps(plan(args.run_id), ensure_ascii=False, indent=2))
        return
    asyncio.run(execute(args.run_id))


if __name__ == "__main__":
    main()
