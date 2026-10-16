"""Unified lifecycle facts and actions regression; guarded I/O and retained baseline."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BACKEND = REPO / "backend"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    paths = json.loads((REPO / "docs/experiments/vm-progressive-probe-20261016/regression-evidence.json").read_text())["test_files"]
    paths = sorted(set(paths) | {str(path.relative_to(REPO)) for pattern in ("tests/test_*instance_observation.py", "tests/test_vm_lifecycle_view*.py") for path in BACKEND.glob(pattern)})
    runtime = Path(tempfile.mkdtemp(prefix="aiphone-lifecycle-simple-regression-")).resolve()
    os.chdir(runtime)
    for name in list(os.environ):
        if name.startswith("AI_PHONE_") or name.upper().endswith("_PROXY"):
            os.environ.pop(name)
    os.environ.update({
        "AI_PHONE_ENV": "isolated-handoff-regression",
        "AI_PHONE_DB_URL": f"sqlite+aiosqlite:///{runtime / 'test-only.db'}",
        "AI_PHONE_STORAGE_DIR": str(runtime / "storage"),
        "AI_PHONE_AGENT_TOKEN": "dev",
        "AI_PHONE_SUBMISSION_INTERNAL_TOKEN": "dev",
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
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "TMPDIR": str(runtime),
    })
    tempfile.tempdir = str(runtime)
    sys.path.insert(0, str(BACKEND))
    original = json.loads((REPO / "docs/experiments/vm-progressive-probe-20261016/source-manifest.json").read_text())
    sources = set(original["source_sha256"]) | {
        "backend/ai_phone/server/vm_probe.py",
        "web/src/lib/api.js",
        "web/src/lib/candidateStream.js",
        "web/src/lib/vmCandidateProbe.js",
        "web/src/lib/vmLifecycle.js",
    }
    for pattern in ("*probe*.js", "*stream*.js"):
        sources.update(str(path.relative_to(REPO)) for path in (REPO / "web/src/lib").glob(pattern))
    sources = sorted(sources)
    hashes = {name: digest(REPO / name) for name in sources}
    evidence = {
        "tested_source_label": "unified lifecycle facts and actions after 82e48bc",
        "source_root": str(REPO), "runtime": str(runtime),
        "source_sha256": hashes, "test_files": paths,
        "test_sha256": {name: digest(REPO / name) for name in paths},
        "frontend_test_sha256": {
            str(path.relative_to(REPO)): digest(path)
            for path in (REPO / "web/tests").rglob("*.mjs")
        },
        "fixture_sha256": {"backend/tests/fixtures/legacy_ios_reconcile_452e7d7.py.txt": digest(BACKEND / "tests/fixtures/legacy_ios_reconcile_452e7d7.py.txt")},
        "blocked_calls": [], "allowed_subprocesses": [], "collected_nodeids": [],
    }
    current = ["import/collection"]

    def deny(event, description):
        evidence["blocked_calls"].append({"event": event, "description": str(description), "test": current[0]})
        raise PermissionError(f"isolated handoff blocked {event}: {description}")

    def local_address(address):
        if isinstance(address, (str, bytes)):
            value = os.fsdecode(address)
            if value.startswith("/"):
                return Path(value).resolve().is_relative_to(runtime)
            host = value
        elif isinstance(address, tuple) and address:
            host = address[0]
        else:
            return False
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def audit(event, args):
        if event == "subprocess.Popen":
            argv = args[1]
            if isinstance(argv, (tuple, list)):
                argv = [os.fsdecode(value) for value in argv]
                node_test = (
                    len(argv) >= 3 and Path(argv[0]).name == "node" and argv[1] == "--test"
                    and all(Path(value).resolve() in set((REPO / "web/tests").glob("*.test.mjs")) for value in argv[2:])
                )
                if node_test:
                    evidence["allowed_subprocesses"].append({"argv": argv, "test": current[0]})
                    return
            deny(event, args[0])
        if event in {"os.system", "os.posix_spawn", "os.exec", "os.kill", "os.killpg"}:
            deny(event, args[0])
        if event in {"socket.connect", "socket.connect_ex", "socket.bind"}:
            if not local_address(args[1]):
                deny(event, args[1])
        if event == "socket.getaddrinfo" and not local_address(args[0]):
            deny(event, args[0])
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if path.name in {".env", ".env.local", ".env.defaults"}:
                resolved = path.resolve()
                # This versioned example is inspected as text by one existing
                # test; actual repository/runtime credentials remain denied.
                defaults_example = resolved == BACKEND / ".env.defaults"
                if not defaults_example and not resolved.is_relative_to(runtime):
                    deny(event, f"non-fixture environment file: {resolved}")

    sys.addaudithook(audit)
    import pytest

    class EvidencePlugin:
        def pytest_collection_modifyitems(self, config, items):
            evidence["collected_nodeids"] = [item.nodeid for item in items]

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            current[0] = item.nodeid
            yield
            current[0] = "between tests"

    arguments = [
        "-p", "pytest_asyncio.plugin", "-p", "anyio.pytest_plugin", "-p", "pytest_timeout",
        "-c", str(BACKEND / "pyproject.toml"),
        "-o", "asyncio_default_fixture_loop_scope=function",
        "--rootdir", str(BACKEND), "--basetemp", str(runtime / "pytest"),
        "--timeout=45", "--timeout-method=thread", "--maxfail=5", "--durations=12", "-q",
        "--junitxml", str(HERE / "regression.xml"),
        *[str(REPO / path) for path in paths],
    ]
    started = time.monotonic()
    result = int(pytest.main(arguments, plugins=[EvidencePlugin()]))
    evidence["exit_code"] = result
    evidence["duration_seconds"] = round(time.monotonic() - started, 3)
    evidence["source_unchanged"] = all(digest(REPO / name) == value for name, value in hashes.items())
    evidence["test_sources_unchanged"] = all(
        digest(REPO / name) == value
        for group in ("test_sha256", "fixture_sha256", "frontend_test_sha256")
        for name, value in evidence[group].items()
    )
    if not evidence["source_unchanged"] or not evidence["test_sources_unchanged"]:
        result = 1
    evidence["final_exit_code"] = result
    if result == 0:
        shutil.rmtree(runtime)
        evidence["runtime_removed"] = True
    (HERE / "regression-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: evidence[key] for key in ["final_exit_code", "source_unchanged", "duration_seconds"]}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
