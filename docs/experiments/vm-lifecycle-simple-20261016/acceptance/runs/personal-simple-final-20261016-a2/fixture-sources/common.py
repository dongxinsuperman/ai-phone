"""Pure preparation/ownership helpers; importing this module starts nothing."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent
REPO = OUTPUT.parents[3]
PLAYWRIGHT = Path("/Users/dongxin/.npm/_npx/705bc6b22212b352/node_modules/playwright")
PLATFORMS = {"android": "vm", "ios": "ios-sim", "harmony": "harmony-vm"}
CASES = (
    ("stale_cleanup", "已停机 · 旧清理标记", "stopped"),
    ("start_failure", "启动失败 · 实际已停止", "stopped"),
    ("stop_failure", "停止失败 · 实际仍运行", "running"),
    ("offline", "所属电脑离线", "unknown"),
    ("unknown_stopped", "未知状态 · 核验后已停止", "stopped"),
    ("unknown_running", "未知状态 · 核验后仍运行", "running"),
    ("busy", "正在操作 · 禁止冲突", "unknown"),
)
OWNER = "fixture-owner"
SPARE = "fixture-spare"
OFFLINE = "fixture-offline"
FAILURES = {"start": "本次启动失败：验收桩拒绝启动，原实例仍已停止",
            "stop": "本次停止失败：验收桩确认原实例仍在运行"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def vm_id(platform, case):
    if platform not in PLATFORMS or case not in {row[0] for row in CASES}:
        raise ValueError("Unknown fixture platform or scenario")
    return hashlib.sha256(("personal-simple:" + platform + ":" + case).encode()).hexdigest()[:12]


def identities(platform, ident, *, scratch, lease_token="", serial=""):
    if platform not in PLATFORMS or not re.fullmatch(r"[0-9a-f]{12}", ident):
        raise ValueError("Only exact fixture VM identities are accepted")
    if platform == "android":
        return {"avd_name": "aiphone_vm_" + ident}
    if platform == "ios":
        return {"sim_name": "aiphone_sim_" + ident,
                "udid": str(uuid.uuid5(uuid.NAMESPACE_URL, "personal-simple-ios:" + ident)).upper()}
    return {"instance_name": "aiphone_harmony_" + ident,
            "instance_path": str(Path(scratch) / "fake-native"),
            "lease_token": lease_token, "hdc_serial": serial}


def observation(platform, ident, state, *, scratch, lease_token="", serial="", reason=""):
    if state not in {"running", "stopped", "missing", "unknown"}:
        raise ValueError("Unknown native observation state")
    return {"vm_id": ident, "state": state,
            "identity": identities(platform, ident, scratch=scratch, lease_token=lease_token, serial=serial),
            "resources_released": state == "stopped",
            "reason": reason or ("已核实当前受管实例" if state in {"running", "stopped"} else "验收桩尚未核实当前实例")}


def own_run_dir(value, *, must_exist=False):
    path = Path(value).absolute()
    runs = OUTPUT / "runs"
    if path.parent != runs or not re.fullmatch(r"personal-simple-[A-Za-z0-9_-]{8,80}", path.name):
        raise ValueError("Only a fresh named run directly inside acceptance/runs is allowed")
    for part in (OUTPUT, runs, path):
        if part.is_symlink():
            raise ValueError("Fixture ownership path is a symlink")
    if must_exist and not path.is_dir():
        raise ValueError("The owned run directory does not exist")
    return path


def source_hashes():
    paths = list((REPO / "backend/ai_phone").rglob("*.py"))
    paths += [path for path in (REPO / "web/src").rglob("*") if path.is_file()]
    paths += [REPO / "web/package.json", REPO / "web/package-lock.json", REPO / "web/vite.config.js", REPO / "web/index.html"]
    paths += [path for path in OUTPUT.iterdir() if path.suffix in {".py", ".js", ".cjs"}]
    result = {}
    for path in sorted(set(paths)):
        if not path.exists():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(REPO):
            raise ValueError("Source snapshot encountered a path outside the worktree")
        result[str(path.relative_to(REPO))] = sha256(path)
    return result


def verify_manifest(path):
    path = Path(path)
    manifest = json.loads(path.read_text())
    if manifest.get("kind") != "personal-lifecycle-simple-frozen-source" or manifest.get("source_repo") != str(REPO):
        raise ValueError("A matching source freeze is required")
    expected = manifest.get("source_sha256")
    if not isinstance(expected, dict) or len(expected) < 20:
        raise ValueError("Incomplete source freeze")
    actual = source_hashes()
    mismatches = sorted(key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key))
    if mismatches:
        raise RuntimeError("Frozen source changed: " + ", ".join(mismatches[:20]))
    return {"manifest": str(path.resolve()), "manifest_sha256": sha256(path),
            "source_count": len(actual), "source_sha256": actual, "mismatches": []}


def plan():
    return {"mode": "prepare-only", "source_repo": str(REPO), "output": str(OUTPUT),
            "platforms": PLATFORMS, "cases": [{"key": key, "label": label, "inspected_state": state} for key, label, state in CASES],
            "ids": {platform: {key: vm_id(platform, key) for key, _label, _state in CASES} for platform in PLATFORMS},
            "server": "Real FastAPI VM routes and real service.handle_vm_status, isolated SQLite, explicit FakeHub only",
            "native": "None. Python audit denies subprocess, signals, external sockets and non-fixture .env files.",
            "browser": "Existing Playwright 1.62.1, newly launched private headless process/context; no shared profile",
            "frontend": "Build frozen Vue source with installed Vite into this run directory; never alter web/dist",
            "freeze": "Root must freeze product and acceptance sources before --execute; this preparation does not execute acceptance",
            "forbidden_ports": [8000, 5180], "playwright_module": str(PLAYWRIGHT)}
