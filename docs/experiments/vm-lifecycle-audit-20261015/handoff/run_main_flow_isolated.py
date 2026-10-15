"""Run the previously uncovered main-flow tests with hard external-I/O guards.

Use the worktree virtualenv; launch with a clean environment (see the report).
This harness changes no product code and never starts an Agent/native device.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


HANDOFF = Path(__file__).resolve().parent
REPO = Path(os.environ.get("AIPHONE_AUDIT_SOURCE_ROOT", str(HANDOFF.parents[3]))).resolve()
BACKEND = REPO / "backend"
BASELINE_XML = HANDOFF.parent / "execution" / "final-regression.xml"
SOURCE_MANIFEST = Path(os.environ.get(
    "AIPHONE_AUDIT_MANIFEST", str(HANDOFF.parent / "execution" / "final-code-manifest.json")
))
FILES = [
    "test_agent_cancellation.py",
    "test_agent_config_distribution.py",
    "test_agent_ios_disconnect.py",
    "test_agent_supervisor.py",
    "test_android_driver.py",
    "test_app_uninstall_agent.py",
    "test_device_wake_policies.py",
    "test_harmony_driver_concurrency.py",
    "test_hub.py",
    "test_ios_demand_source.py",
    "test_ios_driver.py",
    "test_ios_mjpeg_source.py",
    "test_mirror_latest_sender.py",
    "test_orphan_reap.py",
    "test_reliable_reporting.py",
    "test_runner_bridge_attempt.py",
    "test_scheduler_notifications.py",
    "test_server_api.py",
    "test_ws_integration.py",
    "test_v3_capture_sources.py",
    "test_v3_screenshot_provider.py",
]
EXCLUDED = {
    "tests/test_agent_config_distribution.py::test_env_defaults_contains_only_public_runtime_defaults":
        "Repository default-file content check is outside the runtime-flow scope; repository .env reads are forbidden here.",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*, only_tests: list[str] | None = None, prefix: str = "main-flow") -> int:
    selected_files = [] if only_tests else FILES
    baseline = ET.parse(BASELINE_XML).getroot()
    cases = list(baseline.iter("testcase"))
    modules = {case.attrib["classname"].split(".")[1] for case in cases}
    overlap = sorted(name for name in selected_files if Path(name).stem in modules)
    if overlap:
        raise RuntimeError(f"Selected files overlap the previous 934-test suite: {overlap}")

    runtime = Path(tempfile.mkdtemp(prefix="ai-phone-main-flow-impact-")).resolve()
    os.chdir(runtime)  # Relative .env lookup can only see this empty directory.
    for name in list(os.environ):
        if name.startswith("AI_PHONE_") or name.upper().endswith("_PROXY"):
            os.environ.pop(name)
    explicit_config = {
        "AI_PHONE_ENV": "isolated-main-flow-test",
        "AI_PHONE_DB_URL": f"sqlite+aiosqlite:///{runtime / 'unconfigured-test.db'}",
        "AI_PHONE_STORAGE_DIR": str(runtime / "storage"),
        "AI_PHONE_AGENT_TOKEN": "main-flow-fixture-token",
        "AI_PHONE_SUBMISSION_INTERNAL_TOKEN": "main-flow-fixture-token",
        "AI_PHONE_SERVER_WS_URL": "ws://127.0.0.1:9/blocked",
        "AI_PHONE_SERVER_HTTP_BASE": "http://127.0.0.1:9/blocked",
        "AI_PHONE_PHONE_VLM_PROVIDER": "doubao",
        "AI_PHONE_PHONE_VLM_BASE_URL": "http://127.0.0.1:9/blocked",
        "AI_PHONE_PHONE_VLM_API_KEY": "fixture-only",
        "AI_PHONE_PHONE_VLM_MODEL": "fixture-only",
        "AI_PHONE_AUX_PROVIDER": "doubao",
        "AI_PHONE_AUX_BASE_URL": "http://127.0.0.1:9/blocked",
        "AI_PHONE_AUX_API_KEY": "fixture-only",
        "AI_PHONE_AUX_MODEL": "fixture-only",
    }
    os.environ.update(explicit_config)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["TMPDIR"] = str(runtime)
    tempfile.tempdir = str(runtime)
    sys.path.insert(0, str(BACKEND))

    manifest = json.loads(SOURCE_MANIFEST.read_text())
    hashes_before = {name: sha256(REPO / name) for name in manifest["source_sha256"]}
    drift = [name for name, digest in hashes_before.items() if manifest["source_sha256"][name] != digest]
    if drift:
        raise RuntimeError(f"Product source differs from frozen manifest: {drift}")

    evidence = {
        "base_commit": "74f209dc6556d4678291b0dce63a9817741b6bea",
        "tested_source_label": manifest.get("tested_source_label", "unmodified 74f209d"),
        "source_manifest": str(SOURCE_MANIFEST),
        "source_root": str(REPO),
        "baseline_testcases": len(cases),
        "baseline_skipped": sum(case.find("skipped") is not None for case in cases),
        "baseline_xml_sha256": sha256(BASELINE_XML),
        "selected_files": selected_files,
        "additional_test_files": {
            name: sha256(Path(name.split("::", 1)[0])) for name in (only_tests or [])
        },
        "baseline_file_overlap": overlap,
        "explicit_config": explicit_config,
        "runtime_dir": str(runtime),
        "product_hashes_before": hashes_before,
        "collected_nodeids": [],
        "excluded_nodeids": EXCLUDED,
        "blocked_external_calls": [],
    }
    current_test = ["collection/import"]

    def deny(event: str, description: str) -> None:
        evidence["blocked_external_calls"].append({
            "event": event, "description": description, "test": current_test[0],
        })
        raise PermissionError(f"isolated main-flow test blocked {event}: {description}")

    def audit(event: str, args: tuple) -> None:
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
            deny(event, str(args[0]))
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind"}:
            # In-process ASGI/TestClient and socketpair-based event-loop wakeups
            # need no connects, DNS or listeners; USB/device sockets also deny.
            deny(event, str(args[1] if len(args) > 1 else args[0]))
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if path.name in {".env", ".env.local", ".env.defaults"}:
                resolved = path.resolve()
                if not resolved.is_relative_to(runtime):
                    deny(event, f"non-fixture environment file: {resolved}")

    sys.addaudithook(audit)
    import pytest

    class EvidencePlugin:
        def pytest_collection_modifyitems(self, config, items):
            deselected = [item for item in items if item.nodeid in EXCLUDED]
            items[:] = [item for item in items if item.nodeid not in EXCLUDED]
            if deselected:
                config.hook.pytest_deselected(items=deselected)
            evidence["collected_nodeids"] = [item.nodeid for item in items]

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            current_test[0] = item.nodeid
            yield
            current_test[0] = "between tests"

    args = [
        "-p", "pytest_asyncio.plugin", "-p", "pytest_timeout",
        "-o", "asyncio_default_fixture_loop_scope=function",
        "--rootdir", str(BACKEND), "--basetemp", str(runtime / "pytest"),
        "--timeout=30", "--timeout-method=thread", "--maxfail=1", "--durations=15",
        "--junitxml", str(HANDOFF / f"{prefix}-regression.xml"), "-q",
        *[str(BACKEND / "tests" / name) for name in selected_files],
        *(only_tests or []),
    ]
    evidence["pytest_args"] = args
    started = time.monotonic()
    exit_code = int(pytest.main(args, plugins=[EvidencePlugin()]))
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    evidence["pytest_exit_code"] = exit_code
    evidence["product_hashes_after"] = {name: sha256(REPO / name) for name in hashes_before}
    evidence["product_source_unchanged"] = hashes_before == evidence["product_hashes_after"]
    if exit_code == 0:
        shutil.rmtree(runtime)
        evidence["runtime_removed"] = True
    else:
        evidence["runtime_removed"] = False
    (HANDOFF / f"{prefix}-isolation.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "pytest_exit_code": exit_code,
        "collected": len(evidence["collected_nodeids"]),
        "blocked_external_calls": len(evidence["blocked_external_calls"]),
        "product_source_unchanged": evidence["product_source_unchanged"],
    }, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--only-test", action="append", default=[])
    parser.add_argument("--prefix", default="main-flow")
    args = parser.parse_args()
    raise SystemExit(run(only_tests=args.only_test, prefix=args.prefix))
