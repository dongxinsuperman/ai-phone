#!/usr/bin/env python3
"""Personal Android QuickBoot matrix. Default is a read-only plan; --execute is opt-in.

One fresh AVD, five native generations: save/save/discard/discard/cold. This is
snapshot-policy acceptance, not App persistence, external networking or a VLM
Case. No APK, fixture, global Agent, CLI profile, foreign VM or root command is
added. Product provisioning and stop logic run unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
import re
import runpy
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / "android_app_install_sync_a5.py"
BASE_SHA256 = "43356f481dea68c8adcd7b5fb7344b08912f2b2e4f678075533f2a15856eaf51"
if hashlib.sha256(BASE_PATH.read_bytes()).hexdigest() != BASE_SHA256:
    raise RuntimeError("The reviewed a5 helpers changed; a6 requires a new review")
B = runpy.run_path(str(BASE_PATH))
REPO, SDK, DEFAULT_PARENT = B["REPO"], B["SDK"], B["DEFAULT_PARENT"]
ADB_PORT, EMULATOR_PORT = B["ADB_PORT"], B["EMULATOR_PORT"]
SERIAL = f"emulator-{EMULATOR_PORT}"
POLICIES = ("save", "save", "discard_changes", "discard_changes", "cold_boot")
MARKER_SECONDS = {"A": 1131, "B": 1133}
MAX_RUN_SECONDS = 900
IMAGE_33 = "system-images;android-33;google_apis;arm64-v8a"
IMAGE_34 = "system-images;android-34;google_apis;arm64-v8a"


def private_adb_argv(args, *, run_id, environment):
    expected_environment = {"ADB_SERVER_SOCKET": f"tcp:127.0.0.1:{ADB_PORT}",
                            "ANDROID_ADB_SERVER_HOST": "127.0.0.1",
                            "ANDROID_ADB_SERVER_PORT": str(ADB_PORT), "ADB_SERVER_PORT": str(ADB_PORT)}
    if any(environment.get(key) != value for key, value in expected_environment.items()):
        raise RuntimeError("Private ADB environment is missing or points elsewhere")
    if not re.fullmatch(r"android-snapshot-a6-[A-Za-z0-9_-]{8,70}", run_id):
        raise RuntimeError("Private ADB requires this a6 run identity")
    args = list(args)
    base = [str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT)]
    if args == ["start-server"]:
        return [*base, "--one-device", "personal-a6-no-usb-" + run_id, "start-server"]
    allowed = args in (["devices"], ["kill-server"]) or (
        len(args) >= 3 and args[:2] == ["-s", SERIAL] and
        (args[2:] == ["emu", "avd", "name"] or (len(args) == 4 and args[2] == "shell")))
    if not allowed:
        raise RuntimeError("Unexpected ADB client command or target")
    # --one-device is a server-start option; the installed SDK rejects it on
    # ordinary clients. Match the already-tested a5 private TCP invocation.
    return [*base, *args]


def installed_image(package_id):
    path = SDK.joinpath(*package_id.split(";"))
    metadata = path / "package.xml"
    source = path / "source.properties"
    if not metadata.is_file() or not source.is_file():
        return {"installed": False, "package_id": package_id, "reason": "SDK package metadata absent"}
    tree = ET.parse(metadata).getroot()
    ids = [element.get("path") for element in tree.iter() if element.tag.rsplit("}", 1)[-1] == "localPackage"]
    if ids != [package_id]:
        return {"installed": False, "package_id": package_id, "reason": "SDK metadata package identity differs"}
    return {"installed": True, "package_id": package_id, "package_xml": str(metadata),
            "package_xml_sha256": B["digest"](metadata), "source_properties": source.read_text(),
            "source_properties_sha256": B["digest"](source)}


def checked_avd(root, vm_id):
    root = Path(root)
    if not re.fullmatch(r"[0-9a-f]{12}", vm_id):
        raise RuntimeError("Invalid owned VM ID")
    path = root / "android/avd" / ("aiphone_vm_" + vm_id + ".avd")
    for parent in (root, root / "android", root / "android/avd", path):
        if parent.is_symlink():
            raise RuntimeError("An isolated AVD path is a symlink")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Owned AVD escaped the isolated root")
    return path


def check_policy(instance, vm_id, policy, *, state="stopped"):
    if instance.get("id") != vm_id or instance.get("state") != state:
        raise RuntimeError("Snapshot policy requires the exact VM in the expected state")
    config = instance.get("config_json")
    storage = config.get("storage") if isinstance(config, dict) else None
    if not isinstance(storage, dict) or storage.get("snapshot_policy") != policy or storage.get("wipe_data") is not False:
        raise RuntimeError("Snapshot policy/wipe_data readback differs from the request")
    if policy not in {"save", "discard_changes", "cold_boot"}:
        raise RuntimeError("Policy is outside the a6 matrix")
    return instance


def check_launch(argv, *, vm_id, policy):
    expected = {"-avd": "aiphone_vm_" + vm_id, "-port": str(EMULATOR_PORT)}
    if not argv or argv[0] != str(SDK / "emulator/emulator"):
        raise RuntimeError("Unexpected emulator executable")
    for key, value in expected.items():
        if argv.count(key) != 1 or argv.index(key) + 1 >= len(argv) or argv[argv.index(key) + 1] != value:
            raise RuntimeError("Launch does not identify the exact owned AVD/port")
    expected_flags = {"save": set(), "discard_changes": {"-no-snapshot-save"}, "cold_boot": {"-no-snapshot-load"}}
    observed = set(argv) & {"-no-snapshot", "-no-snapshot-load", "-no-snapshot-save", "-wipe-data", "-read-only"}
    if policy not in expected_flags or observed != expected_flags[policy]:
        raise RuntimeError("Native launch flags differ from the requested snapshot policy")


def marker_launch(label):
    if label not in MARKER_SECONDS:
        raise RuntimeError("Only the two predefined RAM markers may be created")
    # The child inherits ignored HUP and has no inherited ADB pipes. There is no
    # persistent guest file, app, daemon or signal sent to an existing process.
    return ("trap '' HUP; /system/bin/sleep " + str(MARKER_SECONDS[label]) +
            " </dev/null >/dev/null 2>&1 & printf '%s\\n' \"$!\"")


def marker_read_command(pid):
    if type(pid) is not int or not 1 < pid < 4194304:
        raise RuntimeError("Unsafe marker PID")
    return (f"if [ -d /proc/{pid} ]; then printf 'PRESENT\\n'; "
            f"cat /proc/{pid}/stat || exit 41; printf 'CMDLINE\\n'; "
            f"cat /proc/{pid}/cmdline || exit 42; else printf 'ABSENT\\n'; fi")


def parse_marker(raw, pid):
    marker_read_command(pid)
    if raw == b"ABSENT\n":
        return {"pid": pid, "present": False}
    if not isinstance(raw, bytes) or not raw.startswith(b"PRESENT\n") or raw.count(b"\nCMDLINE\n") != 1:
        raise RuntimeError("Incomplete marker process evidence")
    stat, cmdline = raw[len(b"PRESENT\n"):].split(b"\nCMDLINE\n", 1)
    text = stat.decode("ascii")
    if not text.startswith(str(pid) + " (") or ") " not in text:
        raise RuntimeError("Marker stat does not belong to the requested PID")
    fields = text.rsplit(") ", 1)[1].split()
    if len(fields) < 20 or not fields[19].isdigit() or not cmdline or not cmdline.endswith(b"\0"):
        raise RuntimeError("Missing marker starttick/cmdline")
    return {"pid": pid, "present": True, "state": fields[0], "starttick": int(fields[19]),
            "cmdline_hex": cmdline.hex(), "cmdline": cmdline.rstrip(b"\0").replace(b"\0", b" ").decode("ascii"),
            "raw_stat": text}


def same_marker(actual, expected):
    return (actual.get("present") is True and actual.get("state") != "Z" and
            all(actual.get(key) == expected.get(key) for key in ("pid", "starttick", "cmdline_hex")))


def check_ram(boot_id, initial_boot_id, observed, expected, *, cold=False, require_b_absent=False):
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id):
        raise RuntimeError("Invalid kernel boot_id evidence")
    if cold:
        if boot_id == initial_boot_id or any(same_marker(observed[label], signature) for label, signature in expected.items()):
            raise RuntimeError("Cold boot restored an old kernel identity or RAM marker")
    else:
        if boot_id != initial_boot_id or not same_marker(observed["A"], expected["A"]):
            raise RuntimeError("QuickBoot did not restore the exact original kernel and RAM marker A")
        if require_b_absent and same_marker(observed["B"], expected["B"]):
            raise RuntimeError("discard_changes saved the later RAM marker B")


def snapshot_manifest(avd):
    avd = Path(avd)
    target = avd / "snapshots/default_boot"
    result = {"path": str(target), "exists": target.is_dir(), "files": []}
    for path in (avd / "snapshots", target):
        if path.is_symlink():
            raise RuntimeError("Snapshot path is a symlink")
    if not result["exists"]:
        return result
    paths = sorted(target.rglob("*"))
    if len(paths) > 2000:
        raise RuntimeError("Snapshot inventory exceeds its bounded scope")
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(avd.resolve()):
            raise RuntimeError("Snapshot entry escaped the owned AVD")
        if not path.is_file():
            continue
        stat = path.stat()
        row = {"name": str(path.relative_to(target)), "size": stat.st_size,
               "inode": stat.st_ino, "mtime_ns": stat.st_mtime_ns}
        # RAM can be GiB-sized; keep metadata plus real restored-process proof.
        if stat.st_size <= 8 * 1024 * 1024:
            row["sha256"] = B["digest"](path)
        result["files"].append(row)
    return result


def require_real_snapshot(value):
    files = {row["name"]: row for row in value["files"]}
    if not value["exists"] or not files.get("snapshot.pb", {}).get("size", 0) or not files.get("ram.bin", {}).get("size", 0):
        raise RuntimeError("A real default_boot snapshot with metadata and RAM is required")


def image_failure_ready(instance, *, vm_id, request_id):
    op = B["require_operation"](instance, vm_id=vm_id, action="start", request_id=request_id)
    if op.get("outcome") == "pending" and op.get("pending") is True:
        return False
    if op.get("outcome") != "failed" or op.get("pending") is not False or instance.get("state") != "error":
        raise RuntimeError("Image mismatch did not end this exact operation as failed")
    error = str(instance.get("error_message") or "")
    if not all(text in error for text in ("未修改或启动", "实际镜像", IMAGE_33, IMAGE_34, "不一致")):
        raise RuntimeError("The failed operation is not evidence of the existing-image mismatch guard")
    return True


def avd_content_fingerprint(avd):
    """Before/after the stopped negative phase: all owned regular file bytes.

    No native operation is running during this read. Bound paths/count/total
    bytes, reject symlinks, and retain inode/mtime as well as streaming SHA-256.
    """
    avd = Path(avd)
    files = sorted(avd.rglob("*"))
    if len(files) > 2000:
        raise RuntimeError("Owned AVD content inventory exceeds its bounded scope")
    result, total = {}, 0
    for path in [avd.with_suffix(".ini"), *files]:
        if path.is_symlink() or not path.resolve().is_relative_to(avd.parent.resolve()):
            raise RuntimeError("AVD content fingerprint encountered a foreign path")
        if not path.is_file():
            continue
        stat = path.stat()
        total += stat.st_size
        if total > 32 * 1024 ** 3:
            raise RuntimeError("Owned AVD content fingerprint exceeds 32 GiB")
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
        after = path.stat()
        if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise RuntimeError("Owned AVD changed during a stopped content fingerprint")
        result[str(path.relative_to(avd.parent))] = {"sha256": sha.hexdigest(), "size": stat.st_size,
                                                   "inode": stat.st_ino, "mtime_ns": stat.st_mtime_ns}
    if not any(name.endswith("/config.ini") for name in result) or not any("userdata" in name for name in result):
        raise RuntimeError("AVD configuration and userdata were not captured")
    return result


def plan(run_id):
    return {"mode": "dry-run", "run_id": run_id, "platform": "android", "source_repo": str(REPO),
            "root": str(DEFAULT_PARENT / run_id), "base_helpers": {"path": str(BASE_PATH), "sha256": BASE_SHA256},
            "frozen_products": B["frozen_products"](), "private_adb_port": ADB_PORT, "serial": SERIAL,
            "http": "Fresh loopback-only Server/WS + token + SQLite; no CLI/profile or default Agent",
            "matrix": [
                "1 save: create one fresh AVD; record boot_id; create bounded sleep A; stop; require real default_boot",
                "2 save: start same AVD; same boot_id and exact A; stop to save again",
                "3 stopped PATCH discard_changes; start; require A; create bounded sleep B; stop without saving",
                "4 discard_changes: start; require original boot_id/A and absent exact B; stop",
                "5 stopped PATCH cold_boot; start; new boot_id and neither original marker; stop; exact VM delete"],
            "markers": MARKER_SECONDS, "max_run_seconds": MAX_RUN_SECONDS,
            "existing_image_negative": {"installed_api34": installed_image(IMAGE_34),
                "sequence": "After cold stop: save full Server config and fingerprint all owned AVD bytes; PATCH only system_image=installed34; one start must fail the exact operation on image identity before launch or writes; explicitly PATCH back original33 and issue a separate successful cold start/stop",
                "failure_is_not_retry": True, "extra_native_launches": 1,
                "missing_image": "Mark this phase not covered; missing SDK cannot count as mismatch proof"},
            "proof": ["Each dispatch/start/stop uses its own exact request_id/action/outcome",
                      "Each native launch has the exact expected policy flags and original owned AVD",
                      "PID, /proc starttick and byte-exact cmdline distinguish marker restoration from PID reuse",
                      "Snapshot metadata and full native per-generation logs are kept; logs alone are not success",
                      "Failed/unresolved stop is never retried in finally; failure preserves the current AVD"],
            "excluded": ["a2/a4 sealed disks", "App persistence", "external networking", "VLM/business Case"],
            "execute_authorization": "Prepared only; root must explicitly release a unique hardware slot before --execute"}


async def execute(run_id):
    freeze = B["frozen_products"]()
    if freeze["mismatches"]:
        raise RuntimeError("Frozen product source changed: " + ", ".join(freeze["mismatches"]))
    root = DEFAULT_PARENT / run_id
    if root.exists() or root.is_symlink():
        raise RuntimeError("A new private run root is mandatory")
    if any(not B["port_free"](port) for port in (ADB_PORT, EMULATOR_PORT, EMULATOR_PORT + 1)):
        raise RuntimeError("A private port is occupied; no VM was started")
    image = SDK / "system-images/android-33/google_apis/arm64-v8a/source.properties"
    if not image.is_file():
        raise RuntimeError("The existing API33 image is required; no SDK download allowed")
    root.mkdir(parents=True, mode=0o700)
    protected = B["protected_fingerprint"]()
    token = B["configure"](root, run_id)
    evidence = B["Evidence"](root, run_id)
    evidence.value.update(classification="single-AVD native QuickBoot snapshot-policy matrix; no App/VLM acceptance",
                          not_covered=plan(run_id)["excluded"], plan=plan(run_id), product_freeze_at_start=freeze,
                          protected_before=protected, native_calls=[], lifecycle_attempts=[], stop_attempts=[],
                          guest_observations=[], snapshot_inventory=[], vm_readbacks=[])
    sources = [Path(__file__).resolve(), BASE_PATH, B["FROZEN_MANIFEST"]]
    evidence.value["harness_sources_at_start"] = {str(path): B["digest"](path) for path in sources}
    (root / "sources").mkdir()
    for path in sources:
        (root / "sources" / path.name).write_bytes(path.read_bytes())
    sys.path.insert(0, str(REPO / "backend"))
    import psutil
    import uvicorn
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import close_all_sessions
    from ai_phone.agent.android_vm import manager as module
    from ai_phone.agent.android_vm.manager import AndroidVmManager
    from ai_phone.agent.drivers.base import DeviceInfo
    from ai_phone.agent.identity import load_host_identity
    from ai_phone.agent.ws_client import AgentWSClient
    from ai_phone.config import get_settings
    from ai_phone.server import db
    from ai_phone.server.api import include_routers
    from ai_phone.server.android_vm import service
    from ai_phone.server.db import Base
    from ai_phone.server.hub import Hub
    from ai_phone.server.lockstore import DeviceLockStore
    from ai_phone.server.ws import include_ws
    from ai_phone.server.ws import agent_ws
    from ai_phone.shared import protocol as P

    get_settings.cache_clear()
    settings = get_settings()
    if settings.db_url != f"sqlite+aiosqlite:///{root / 'server.sqlite3'}" or Path(settings.storage_dir).resolve() != root / "storage":
        raise RuntimeError("Server storage isolation was not applied")
    if settings.android_vm_orphan_cleanup or settings.android_vm_kill_foreign:
        raise RuntimeError("Foreign or orphan cleanup must remain disabled")
    service._EMULATOR_PORT_MIN = service._EMULATOR_PORT_MAX = EMULATOR_PORT
    manager = AndroidVmManager(runtime_dir=root / "android/runtime", max_instances=1)
    host_id = load_host_identity(root / "storage")
    agent_id = "personal-snapshot-" + run_id
    prefix, auth = "/api/internal/vm/instances", {"Authorization": "Bearer " + token}
    adb = SDK / "platform-tools/adb"
    owned, handles = set(), {}
    native_calls, attempts, stop_attempts = (evidence.value[key] for key in ("native_calls", "lifecycle_attempts", "stop_attempts"))
    log_offsets = {}
    current_policy = "save"
    image34 = installed_image(IMAGE_34)
    expected_policies = list(POLICIES) + (["cold_boot"] if image34["installed"] else [])
    private_daemon = None
    agent = agent_task = server = server_task = listener = None
    original_subprocess, original_stopper = module.subprocess, module._stop_owned_process
    start_time = time.monotonic()
    snapshot_saved = False
    deleted = False
    failure = None

    def generation(vm_id):
        launches = [row for row in native_calls if row["kind"] == "launch" and row.get("vm_id") == vm_id and row.get("pid")]
        return f"{launches[-1]['pid']}:{launches[-1]['monotonic_ns']}" if launches else None

    def assert_owned(vm_id):
        if vm_id not in owned or len(owned) != 1:
            raise RuntimeError("Action is not for the sole owned VM")
        return checked_avd(root, vm_id)

    def adb_command(*args, timeout=15, binary=False):
        return B["command"](private_adb_argv(args, run_id=run_id, environment=os.environ),
                            timeout=timeout, binary=binary)

    def target(vm_id):
        assert_owned(vm_id)
        runtime = manager._runtimes.get(vm_id)
        if runtime is None or runtime.adb_serial != SERIAL or runtime.port != EMULATOR_PORT:
            raise RuntimeError("Missing exact private runtime")
        if adb_command("-s", SERIAL, "emu", "avd", "name").splitlines()[0].strip() != "aiphone_vm_" + vm_id:
            raise RuntimeError("Actual emulator AVD identity differs from the owned VM")
        return SERIAL

    def alive():
        return [pid for pid, process in handles.items() if process.poll() is None]

    class ObservedSubprocess:
        def __getattr__(self, name):
            return getattr(original_subprocess, name)

        def Popen(self, args, *extra, **kwargs):
            if len(owned) != 1 or len([r for r in native_calls if r["kind"] == "launch"]) >= len(expected_policies):
                raise RuntimeError("Unknown or additional native launch")
            vm_id = next(iter(owned))
            assert_owned(vm_id)
            argv = [str(x) for x in args]
            check_launch(argv, vm_id=vm_id, policy=current_policy)
            row = {"kind": "launch", "vm_id": vm_id, "policy": current_policy, "argv": argv,
                   "at": B["utc_now"](), "monotonic_ns": time.monotonic_ns()}
            native_calls.append(row)
            process = original_subprocess.Popen(args, *extra, **kwargs)
            row["pid"] = process.pid
            handles[process.pid] = process
            return process

        def run(self, args, *extra, **kwargs):
            argv = [str(x) for x in args]
            sync = B["validate_guest_sync_argv"](argv, os.environ if kwargs.get("env") is None else kwargs["env"],
                                                 adb_path=adb, serial=SERIAL)
            if argv[0] == str(adb) and "-s" in argv and argv[argv.index("-s") + 1] != SERIAL:
                raise RuntimeError("Product attempted a different ADB serial")
            mutates_avd = any(argv[index:index + 2] in (["create", "avd"], ["delete", "avd"]) for index in range(len(argv) - 1))
            if mutates_avd and ("-n" not in argv or argv[argv.index("-n") + 1] not in {"aiphone_vm_" + key for key in owned}):
                raise RuntimeError("Product attempted a different AVD mutation")
            if not (sync or mutates_avd or argv[-2:] == ["emu", "kill"]):
                return original_subprocess.run(args, *extra, **kwargs)
            row = {"kind": "command", "argv": argv, "sync": sync, "at": B["utc_now"](), "monotonic_ns": time.monotonic_ns()}
            native_calls.append(row)
            try:
                result = original_subprocess.run(args, *extra, **kwargs)
                row.update(returncode=result.returncode,
                           stdout=result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout,
                           stderr=result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr)
                return result
            except BaseException as exc:
                row["error"] = repr(exc)
                raise
            finally:
                row["finished_monotonic_ns"] = time.monotonic_ns()

    def observed_stopper(process, *, graceful, **kwargs):
        row = {"kind": "process-stop", "pid": process.pid, "graceful": graceful,
               "kwargs": dict(kwargs), "calls": [], "monotonic_ns": time.monotonic_ns()}
        native_calls.append(row)

        class Proxy:
            def __getattr__(self, name):
                actual = getattr(process, name)
                if name not in {"wait", "poll", "is_running", "terminate", "kill"}:
                    return actual
                def invoke(*args, **inner):
                    entry = {"name": name, "args": list(args), "kwargs": dict(inner), "monotonic_ns": time.monotonic_ns()}
                    row["calls"].append(entry)
                    try:
                        value = actual(*args, **inner)
                        entry["result"] = value
                        return value
                    except BaseException as exc:
                        entry["error"] = repr(exc)
                        raise
                return invoke
        return original_stopper(Proxy(), graceful=graceful, **kwargs)

    module.subprocess, module._stop_owned_process = ObservedSubprocess(), observed_stopper
    B["observe_manager_stops"](manager, generation, stop_attempts)

    def owned_devices():
        lines = adb_command("devices").splitlines()[1:]
        connected = {line.split()[0] for line in lines if len(line.split()) >= 2}
        if connected - ({SERIAL} if owned else set()):
            raise RuntimeError("Unknown device in private ADB; no scan/reclaim permitted")
        infos = [DeviceInfo(serial=SERIAL, platform="android", brand="Personal", model="A6 API33",
                            screen_width=540, screen_height=960) for key, rt in list(manager._runtimes.items())
                 if key in owned and rt.ready and rt.adb_serial in connected]
        return [device.to_dict() for device in manager.decorate_devices(infos)]

    def inventory(vm_id, phase):
        avd = assert_owned(vm_id)
        value = snapshot_manifest(avd)
        row = {"phase": phase, "at": B["utc_now"](), "snapshot": value, "avd_path": str(avd), "avd_files": []}
        for name in ("config.ini", "hardware-qemu.ini", "emulator-user.ini", "userdata-qemu.img", "userdata-qemu.img.qcow2"):
            path = avd / name
            if path.is_symlink():
                raise RuntimeError("Owned AVD metadata contains a symlink")
            if path.is_file():
                stat = path.stat()
                item = {"name": name, "inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "birth_time": getattr(stat, "st_birthtime", None)}
                if name.endswith(".ini"):
                    item.update(sha256=B["digest"](path), text=path.read_text())
                row["avd_files"].append(item)
        log = root / "android/runtime" / vm_id / "emulator.log"
        if log.is_file():
            data = log.read_bytes()
            output = root / (phase + ".emulator.log")
            output.write_bytes(data)
            offset = log_offsets.get(vm_id, 0)
            if len(data) < offset:
                raise RuntimeError("Native emulator log was truncated between observations")
            delta = root / (phase + ".emulator.delta.log")
            delta.write_bytes(data[offset:])
            log_offsets[vm_id] = len(data)
            row.update(emulator_log=str(output), emulator_log_sha256=hashlib.sha256(data).hexdigest(),
                       emulator_log_delta=str(delta), delta_start_offset=offset, delta_end_offset=len(data),
                       snapshot_log_lines=[line for line in data.decode(errors="replace").splitlines()
                                           if any(word in line.lower() for word in ("snapshot", "quickboot", "cold boot", "saving state"))])
        evidence.value["snapshot_inventory"].append(row)
        evidence.flush()
        return value

    async def read_ram(vm_id, phase, markers):
        serial = await asyncio.to_thread(target, vm_id)
        boot = (await asyncio.to_thread(adb_command, "-s", serial, "shell", "cat /proc/sys/kernel/random/boot_id")).strip()
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot):
            raise RuntimeError("No valid boot_id returned")
        observed = {}
        for label, signature in markers.items():
            raw = await asyncio.to_thread(adb_command, "-s", serial, "shell", marker_read_command(signature["pid"]), binary=True)
            observed[label] = parse_marker(raw, signature["pid"])
        evidence.value["guest_observations"].append({"phase": phase, "boot_id": boot, "markers": observed,
                                                     "at": B["utc_now"](), "monotonic_ns": time.monotonic_ns()})
        evidence.flush()
        return boot, observed

    async def create_marker(vm_id, label):
        serial = await asyncio.to_thread(target, vm_id)
        raw_pid = await asyncio.to_thread(adb_command, "-s", serial, "shell", marker_launch(label))
        if not re.fullmatch(r"[0-9]+\n", raw_pid):
            raise RuntimeError("Marker creation did not return one exact PID")
        pid = int(raw_pid)
        raw = await asyncio.to_thread(adb_command, "-s", serial, "shell", marker_read_command(pid), binary=True)
        result = parse_marker(raw, pid)
        expected_cmdline = ("/system/bin/sleep\0" + str(MARKER_SECONDS[label]) + "\0").encode().hex()
        if not result["present"] or result.get("state") == "Z" or result.get("cmdline_hex") != expected_cmdline:
            raise RuntimeError("RAM marker did not retain the exact bounded sleep argv")
        evidence.add("marker-created", label=label, signature=result, lifetime_seconds=MARKER_SECONDS[label])
        return result

    try:
        await asyncio.to_thread(adb_command, "start-server")
        daemons = []
        for process in psutil.process_iter(["name", "cmdline", "cwd"]):
            if process.info.get("name") == "adb" and "personal-a6-no-usb-" + run_id in (process.info.get("cmdline") or []) and process.info.get("cwd") == str(root):
                daemons.append(process)
        if len(daemons) != 1:
            raise RuntimeError("Exactly one private ADB daemon must be identified")
        private_daemon = (daemons[0].pid, daemons[0].create_time())
        if adb_command("devices").strip() != "List of devices attached":
            raise RuntimeError("Private ADB is not initially empty")
        await db.dispose_engine()
        db.init_engine(db_url=settings.db_url)
        from ai_phone.server import models  # noqa: F401
        app = FastAPI()
        app.state.hub, app.state.lock_store = Hub(), DeviceLockStore()
        include_routers(app)
        include_ws(app)
        async with db.get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        os.environ["AI_PHONE_SERVER_HTTP_BASE"], os.environ["AI_PHONE_SERVER_WS_URL"] = base_url, f"ws://127.0.0.1:{port}/ws/agent"
        get_settings.cache_clear()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="error", access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        await B["eventually"](lambda: asyncio.sleep(0, result=server.started), bool)

        def guard(handler):
            async def call(client, msg):
                vm_id = str(msg.get("vm_id") or "")
                assert_owned(vm_id)
                if msg.get("assigned_port") not in (None, EMULATOR_PORT):
                    raise RuntimeError("WS assigned an unexpected emulator port")
                evidence.add("agent-command", command=dict(msg))
                await handler(client, msg)
            return call
        agent = AgentWSClient(ws_url=f"ws://127.0.0.1:{port}/ws/agent", token=token, server_http_base=base_url,
                              agent_id=agent_id, agent_name=agent_id, host_id=host_id, device_provider=owned_devices,
                              ping_interval=5, rescan_interval=5, vm_command_concurrency=1)
        for kind, handler in ((P.MSG_VM_CAPABILITY_PROBE, manager.handle_capability_probe), (P.MSG_VM_START, manager.handle_start),
                              (P.MSG_VM_STOP, manager.handle_stop), (P.MSG_VM_DELETE, manager.handle_delete)):
            agent.on(kind, guard(handler))
        agent_task = asyncio.create_task(agent.run_forever())
        await B["eventually"](lambda: asyncio.sleep(0, result=app.state.hub.has_agent(agent_id)), bool)
        evidence.add("isolated-services", base_url=base_url, agent_id=agent_id, host_id=host_id,
                     private_adb_pid=private_daemon[0], private_adb_port=ADB_PORT)

        async with AsyncClient(base_url=base_url, timeout=30, trust_env=False) as http:
            async def api(method, path, **kwargs):
                response = await http.request(method, path, headers=auth, **kwargs)
                if response.status_code >= 400:
                    raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:2000]}")
                return response.json()

            async def operation(vm_id, endpoint, **kwargs):
                action = "stop" if endpoint == "stop" else "start"
                row = {"vm_id": vm_id, "generation_id": generation(vm_id), "action": action, "endpoint": endpoint,
                       "outcome": "requested", "monotonic_ns": time.monotonic_ns()}
                attempts.append(row)
                try:
                    if time.monotonic() - start_time >= MAX_RUN_SECONDS:
                        raise TimeoutError("The bounded a6 run deadline expired")
                    response = await api("POST", prefix + "/" + vm_id + "/" + endpoint, **kwargs)
                    if response.get("sent") is not True:
                        raise RuntimeError("This HTTP operation was not explicitly sent")
                    op = B["require_operation"](response.get("instance"), vm_id=vm_id, action=action)
                    row.update(request_id=op["request_id"], response_operation=op)
                    B["operation_snapshot_ready"](response["instance"], vm_id=vm_id, action=action,
                                                   request_id=row["request_id"], desired_state="stopped" if action == "stop" else "running")
                    async def read():
                        value = await api("GET", prefix + "/" + vm_id)
                        B["operation_snapshot_ready"](value, vm_id=vm_id, action=action, request_id=row["request_id"],
                                                       desired_state="stopped" if action == "stop" else "running")
                        return value
                    value = await B["eventually"](read, lambda value: B["operation_snapshot_ready"](value, vm_id=vm_id, action=action,
                        request_id=row["request_id"], desired_state="stopped" if action == "stop" else "running"),
                        timeout=min(240, max(0.1, MAX_RUN_SECONDS - (time.monotonic() - start_time))))
                    row.update(outcome="succeeded", terminal_instance=value)
                    evidence.value["vm_readbacks"].append(value)
                    evidence.flush()
                    return value
                except BaseException as exc:
                    row.update(outcome="failed", error=repr(exc))
                    evidence.flush()
                    raise

            async def start(vm_id, phase, *, first=False):
                instance = await operation(vm_id, "dispatch" if first else "start", **({"json": {"agent_id": agent_id}} if first else {}))
                check_policy(instance, vm_id, current_policy, state="running")
                serial = await asyncio.to_thread(target, vm_id)
                boot = (await asyncio.to_thread(adb_command, "-s", serial, "shell", "getprop sys.boot_completed")).strip()
                package = await asyncio.to_thread(adb_command, "-s", serial, "shell", "service check package")
                if boot != "1" or not re.search(r"package:\s*found\b", package, re.I):
                    raise RuntimeError("Real guest boot/package service was not ready")
                inventory(vm_id, phase)
                evidence.add(phase, status="passed", policy=current_policy, vm_id=vm_id,
                             generation=generation(vm_id), native_pid=manager._runtimes[vm_id].process.pid)

            async def stop(vm_id, phase):
                instance = await operation(vm_id, "stop")
                if (alive() or vm_id in manager._runtimes or not assert_owned(vm_id).is_dir() or
                        not module._is_port_free(EMULATOR_PORT) or not module._is_port_free(EMULATOR_PORT + 1)):
                    attempts[-1].update(outcome="failed", error="native exit/preservation check failed")
                    raise RuntimeError("Stopped API result did not match native exit and AVD preservation")
                if instance.get("assigned_agent_id") != agent_id:
                    attempts[-1].update(outcome="failed", error="stop lost existing binding")
                    raise RuntimeError("Stop lost the existing binding")
                value = inventory(vm_id, phase)
                evidence.add(phase, status="passed", policy=current_policy, native_exit_confirmed=True,
                             avd_preserved=True, snapshot=value)
                return value

            async def patch_policy(vm_id, policy):
                nonlocal current_policy
                previous = await api("GET", prefix + "/" + vm_id)
                check_policy(previous, vm_id, current_policy)
                config = copy.deepcopy(previous["config_json"])
                config["storage"]["snapshot_policy"] = policy
                patched = await api("PATCH", prefix + "/" + vm_id, json={"config_json": config})
                check_policy(patched, vm_id, policy)
                current_policy = policy
                evidence.add("stopped-policy-patch", policy=policy, instance=patched)

            config = {"alias": "personal-snapshot-" + run_id, "api_level": 33, "abi": "arm64", "system_type": "google_apis",
                      "screen_width": 540, "screen_height": 960, "density": 240,
                      "config_json": {"performance": {"ram_mb": 2048, "cpu_cores": 2, "gpu_mode": "host"},
                                      "storage": {"internal_storage_mb": 4096, "snapshot_policy": "save", "wipe_data": False},
                                      "startup": {"no_window": True, "no_audio": True, "no_boot_anim": True}}}
            created = await api("POST", prefix, json=config)
            vm_id = str(created.get("id") or "")
            checked_avd(root, vm_id)
            check_policy(created, vm_id, "save", state="draft")
            if created.get("assigned_agent_id"):
                raise RuntimeError("New configuration was already bound")
            owned.add(vm_id)
            evidence.value["owned_vm_ids"] = [vm_id]
            (root / "ownership.json").write_text(json.dumps({"run_id": run_id, "vm_ids": [vm_id], "agent_id": agent_id}, indent=2))
            candidates = await api("POST", prefix + "/" + vm_id + "/dispatch-candidates")
            if not any(row.get("agent_id") == agent_id and row.get("ok") for row in candidates.get("agents", [])):
                raise RuntimeError("Real capability probe did not accept the isolated host")
            await start(vm_id, "01-save-first-start", first=True)
            initial_boot, _ = await read_ram(vm_id, "01-initial-boot", {})
            markers = {"A": await create_marker(vm_id, "A")}
            require_real_snapshot(await stop(vm_id, "01-save-first-stop"))
            snapshot_saved = True

            await start(vm_id, "02-save-restore-start")
            boot, observed = await read_ram(vm_id, "02-save-restored-A", markers)
            check_ram(boot, initial_boot, observed, markers)
            require_real_snapshot(await stop(vm_id, "02-save-restore-stop"))

            await patch_policy(vm_id, "discard_changes")
            await start(vm_id, "03-discard-before-B-start")
            boot, observed = await read_ram(vm_id, "03-discard-restored-A", markers)
            check_ram(boot, initial_boot, observed, markers)
            markers["B"] = await create_marker(vm_id, "B")
            require_real_snapshot(await stop(vm_id, "03-discard-after-B-stop"))

            await start(vm_id, "04-discard-restore-start")
            boot, observed = await read_ram(vm_id, "04-discard-restored-A-not-B", markers)
            check_ram(boot, initial_boot, observed, markers, require_b_absent=True)
            require_real_snapshot(await stop(vm_id, "04-discard-restore-stop"))

            await patch_policy(vm_id, "cold_boot")
            await start(vm_id, "05-cold-start")
            boot, observed = await read_ram(vm_id, "05-cold-new-boot-no-old-markers", markers)
            check_ram(boot, initial_boot, observed, markers, cold=True)
            await stop(vm_id, "05-cold-stop")

            if image34["installed"]:
                previous = await api("GET", prefix + "/" + vm_id)
                check_policy(previous, vm_id, "cold_boot")
                (root / "image-negative-original-server-config.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
                disk_before = await asyncio.to_thread(avd_content_fingerprint, assert_owned(vm_id))
                (root / "image-negative-avd-before.json").write_text(json.dumps(disk_before, indent=2) + "\n")
                launch_before = len([row for row in native_calls if row["kind"] == "launch"])
                changed = await api("PATCH", prefix + "/" + vm_id, json={"system_image": IMAGE_34})
                check_policy(changed, vm_id, "cold_boot")
                if changed.get("system_image") != IMAGE_34 or any(changed.get(key) != previous.get(key) for key in
                        ("config_json", "api_level", "abi", "system_type", "assigned_agent_id")):
                    raise RuntimeError("Negative phase changed more than the requested explicit system_image")
                row = {"vm_id": vm_id, "action": "start", "endpoint": "start", "outcome": "requested",
                       "expected_failure": "existing-image-mismatch", "generation_id": generation(vm_id)}
                attempts.append(row)
                try:
                    response = await api("POST", prefix + "/" + vm_id + "/start")
                    if response.get("sent") is not True:
                        raise RuntimeError("Negative image attempt was not sent to the real Agent")
                    op = B["require_operation"](response.get("instance"), vm_id=vm_id, action="start")
                    row["request_id"] = op["request_id"]
                    image_failure_ready(response["instance"], vm_id=vm_id, request_id=row["request_id"])
                    failed = await B["eventually"](lambda: api("GET", prefix + "/" + vm_id),
                        lambda value: image_failure_ready(value, vm_id=vm_id, request_id=row["request_id"]), timeout=90)
                    disk_after = await asyncio.to_thread(avd_content_fingerprint, assert_owned(vm_id))
                    (root / "image-negative-avd-after.json").write_text(json.dumps(disk_after, indent=2) + "\n")
                    if disk_before != disk_after or alive() or vm_id in manager._runtimes or \
                            len([entry for entry in native_calls if entry["kind"] == "launch"]) != launch_before:
                        raise RuntimeError("Rejected image attempt changed original AVD bytes or launched native code")
                    row.update(outcome="expected-failed", terminal_instance=failed, native_launches_added=0, original_avd_bytes_unchanged=True)
                    evidence.add("06-existing-image-negative", status="passed", operation=row, installed_image=image34)
                except BaseException as exc:
                    row.update(outcome="failed", error=repr(exc))
                    raise
                # This is an explicit restoration and a new attempt, after the
                # previous attempt's terminal failure was proved. No retry loop.
                restored = await api("PATCH", prefix + "/" + vm_id, json={"system_image": previous.get("system_image", "")})
                check_policy(restored, vm_id, "cold_boot", state="error")
                if any(restored.get(key) != previous.get(key) for key in ("config_json", "api_level", "abi", "system_type", "system_image")):
                    raise RuntimeError("Explicit original image restoration did not restore the saved configuration")
                await start(vm_id, "06-original-image-restored-start")
                recovery_boot, recovery_markers = await read_ram(vm_id, "06-original-image-recovered-cold", markers)
                check_ram(recovery_boot, initial_boot, recovery_markers, markers, cold=True)
                sdk = (await asyncio.to_thread(adb_command, "-s", await asyncio.to_thread(target, vm_id),
                                               "shell", "getprop ro.build.version.sdk")).strip()
                if sdk != "33" or recovery_boot == boot:
                    raise RuntimeError("Recovery did not cold boot the original API33 image")
                evidence.add("06-original-image-recovery", status="passed", vm_id=vm_id, actual_sdk=sdk,
                             explicit_new_operation=attempts[-1]["request_id"], avd_path=str(assert_owned(vm_id)))
                await stop(vm_id, "06-original-image-restored-stop")
            else:
                evidence.add("06-existing-image-negative", status="not-covered", reason=image34["reason"])

            response = await api("DELETE", prefix + "/" + vm_id)
            readback = await http.get(prefix + "/" + vm_id, headers=auth)
            if readback.status_code != 404:
                raise RuntimeError("Ledger delete was not immediate")
            avd = assert_owned(vm_id)
            await B["eventually"](lambda: asyncio.sleep(0, result=(not avd.exists() and not avd.with_suffix(".ini").exists()
                and vm_id not in manager._runtimes and not alive())), bool, timeout=45)
            deleted = True
            evidence.add("delete", status="passed", response=response, server_absent=True, native_absent=True)
        if [row["policy"] for row in native_calls if row["kind"] == "launch"] != expected_policies:
            raise RuntimeError("Actual native generations did not match the snapshot and explicit image-restoration matrix")
        creates = [row for row in native_calls if row["kind"] == "command" and "create" in row["argv"] and "avd" in row["argv"]]
        if len(creates) != 1:
            raise RuntimeError("The matrix did not use exactly one native AVD creation")
        evidence.value["status"] = "passed"
    except BaseException as exc:
        failure = exc
        evidence.value.update(status="failed", error=repr(exc))
        evidence.add("failed", error=repr(exc), snapshot_saved=snapshot_saved)
    finally:
        # Never delete failed evidence and never repeat a failed stop of the
        # current native generation. A newly started generation may receive one
        # protective product stop after a later marker/readback failure.
        # Wait for already dispatched owned handlers without cancel/reissue.
        # Their product deadline includes start-failure cleanup. If they still
        # have no terminal result, preserve the entire resource scope.
        pending_handlers = {task for task in getattr(agent, "_vm_tasks", set()) if not task.done()}
        if pending_handlers:
            _done, pending_handlers = await asyncio.wait(pending_handlers, timeout=360)
        evidence.value["native_handlers_active_at_cleanup"] = len(pending_handlers)
        blocked = sorted(owned) if pending_handlers else []
        for vm_id in owned:
            if deleted or vm_id in blocked or (not alive() and vm_id not in manager._runtimes):
                continue
            rows = stop_attempts + [row for row in attempts if row["action"] == "stop"]
            decision = B["cleanup_stop_decision"](vm_id, generation(vm_id), rows)
            if not decision["allowed"]:
                blocked.append(vm_id)
                evidence.add("protective-stop-blocked", vm_id=vm_id, decision=decision)
                continue
            try:
                result = await asyncio.to_thread(manager.stop_sync, vm_id)
                evidence.add("protective-stop-once", vm_id=vm_id, result=result, avd_preserved=True)
                if not result.get("ok"):
                    blocked.append(vm_id)
            except BaseException as exc:
                blocked.append(vm_id)
                evidence.add("protective-stop-failed", vm_id=vm_id, error=repr(exc))
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
        if agent_ws._persist_worker is not None and not agent_ws._persist_worker.done():
            agent_ws._persist_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_ws._persist_worker
        await close_all_sessions()
        await db.dispose_engine()
        if private_daemon is not None and not alive() and not blocked:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(adb_command, "kill-server")
        def daemon_gone():
            if private_daemon is None:
                return True
            try:
                process = psutil.Process(private_daemon[0])
                return process.create_time() != private_daemon[1] or not process.is_running()
            except psutil.NoSuchProcess:
                return True
        if not blocked:
            with contextlib.suppress(TimeoutError):
                await B["eventually"](lambda: asyncio.sleep(0, result=daemon_gone()), bool, timeout=5)
        for process in handles.values():
            if process.poll() is not None:
                with contextlib.suppress(Exception):
                    process.wait(timeout=0)
        for vm_id in owned:
            inventory(vm_id, "final-protected-inventory")
        protected_after = B["protected_fingerprint"]()
        products_after = B["frozen_products"]()
        changed_sources = [str(path) for path in sources if B["digest"](path) != evidence.value["harness_sources_at_start"][str(path)]]
        evidence.value.update(finished_at=B["utc_now"](), duration_sec=time.monotonic() - start_time,
            protected_metadata_changed=[key for key in set(protected) | set(protected_after) if protected.get(key) != protected_after.get(key)],
            product_freeze_at_finish=products_after, harness_source_changes=changed_sources,
            private_adb_stopped=daemon_gone(), live_native_pids=alive(), blocked_cleanup_vm_ids=blocked,
            remaining_owned_runtimes=sorted(manager._runtimes),
            remaining_owned_avds=[str(assert_owned(vm_id)) for vm_id in owned if assert_owned(vm_id).exists()],
            native_ports_free=all(module._is_port_free(port) for port in (EMULATOR_PORT, EMULATOR_PORT + 1)))
        if not pending_handlers:
            module.subprocess, module._stop_owned_process = original_subprocess, original_stopper
        if evidence.value["status"] == "passed" and (evidence.value["protected_metadata_changed"] or products_after["mismatches"] or
                changed_sources or not daemon_gone() or alive() or manager._runtimes or evidence.value["remaining_owned_avds"] or
                not evidence.value["native_ports_free"] or pending_handlers):
            evidence.value["status"] = "failed"
            failure = RuntimeError("Final source/isolation/resource check failed")
        evidence.flush()
        print(json.dumps({"status": evidence.value["status"], "evidence": str(root / "evidence.json"),
                          "live_native_pids": alive(), "preserved_avds": evidence.value["remaining_owned_avds"]}, ensure_ascii=False))
    if failure is not None:
        raise RuntimeError("a6 failed once; inspect the preserved evidence; no automatic rerun") from failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id", default="android-snapshot-a6-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    args = parser.parse_args()
    if not re.fullmatch(r"android-snapshot-a6-[A-Za-z0-9_-]{8,70}", args.run_id):
        parser.error("a6 requires its own ASCII android-snapshot-a6- run-id")
    if args.execute:
        asyncio.run(execute(args.run_id))
    else:
        print(json.dumps(plan(args.run_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
