"""Freeze explicitly, then run one owned HTTP/browser test. Default prints plan.

Preparation does not execute this program with --freeze or --execute until root
freezes the concurrently edited product. No native VM/Agent/CLI is ever started.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from common import OUTPUT, PLAYWRIGHT, REPO, own_run_dir, plan, sha256, source_hashes, verify_manifest


def freeze_source(path):
    path = Path(path).absolute()
    if path.parent != OUTPUT or path.suffix != ".json" or path.exists() or path.is_symlink():
        raise RuntimeError("Write one new freeze JSON directly in this acceptance directory")
    value = {"kind": "personal-lifecycle-simple-frozen-source", "source_repo": str(REPO),
             "frozen_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source_sha256": source_hashes()}
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest": str(path), "source_count": len(value["source_sha256"])}))


def request_own(runtime, path, *, method="GET", fixture=False):
    parsed = urllib.parse.urlsplit(runtime["url"])
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port in {None, 8000, 5180}:
        raise RuntimeError("Refusing a non-fixture HTTP target")
    if not path.startswith(("/api/healthz", "/fixture/")):
        raise RuntimeError("Runner only observes health or stops its own fixture")
    headers = {"x-personal-fixture-token": runtime["auth_token"]} if fixture else {}
    request = urllib.request.Request(runtime["url"] + path, method=method, headers=headers)
    # Explicit no-proxy opener; never read a CLI/deployment profile.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def run_once(manifest_path, run_id):
    freeze = verify_manifest(manifest_path)
    run_dir = own_run_dir(OUTPUT / "runs" / run_id)
    if run_dir.exists():
        raise RuntimeError("Never reuse or overwrite a prior acceptance run")
    run_dir.mkdir(parents=True, mode=0o700)
    node = shutil.which("node")
    python = REPO / "backend/.venv/bin/python"
    vite = REPO / "web/node_modules/vite/bin/vite.js"
    if not node or not python.is_file() or not vite.is_file() or not (PLAYWRIGHT / "package.json").is_file():
        raise RuntimeError("Use the already installed private runtimes; no install/download is allowed")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AI_PHONE_", "AIPHONE_")) and not key.lower().endswith("_proxy")}
    env.update(NO_PROXY="127.0.0.1,::1,localhost", no_proxy="127.0.0.1,::1,localhost", PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    fixture_process = None
    runtime = None
    error = None
    summary = {"status": "running", "run_id": run_id, "source_freeze_at_start": freeze,
               "scope": "real isolated Server/SQLite/built Vue plus controlled FakeHub; no native VM or real Agent",
               "real_native_started": False, "main_services_touched": False}
    try:
        (run_dir / "empty-env").mkdir()
        build_config = run_dir / "isolated-vite.config.mjs"
        build_config.write_text(
            "import original from " + json.dumps((REPO / "web/vite.config.js").as_uri()) + ";\n" +
            "export default { ...original, server: undefined, envDir: " + json.dumps(str(run_dir / "empty-env")) +
            ", cacheDir: " + json.dumps(str(run_dir / "vite-cache")) + " };\n")
        summary["isolated_build_config_sha256"] = sha256(build_config)
        with (run_dir / "frontend-build.log").open("w") as log:
            built = subprocess.run([node, str(vite), "build", "--config", str(build_config), "--configLoader", "runner",
                                    "--outDir", str(run_dir / "web-dist"), "--emptyOutDir"],
                                   cwd=REPO / "web", env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        if built.returncode:
            raise RuntimeError("Isolated frozen Vue build failed")
        summary["frontend_sha256"] = {str(path.relative_to(run_dir / "web-dist")): sha256(path)
                                      for path in sorted((run_dir / "web-dist").rglob("*")) if path.is_file()}
        verify_manifest(manifest_path)
        log = (run_dir / "fixture.log").open("w")
        fixture_process = subprocess.Popen([str(python), str(OUTPUT / "browser_fixture.py"), "--serve",
            "--run-dir", str(run_dir), "--manifest", str(Path(manifest_path).resolve())],
            cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT)
        summary["fixture_pid"] = fixture_process.pid
        deadline = time.monotonic() + 30
        runtime_file = run_dir / "fixture-runtime.json"
        while not runtime_file.exists():
            if fixture_process.poll() is not None:
                raise RuntimeError("Fixture exited before readiness; see owned fixture.log")
            if time.monotonic() >= deadline:
                raise TimeoutError("Fixture startup deadline expired")
            time.sleep(0.05)
        runtime = json.loads(runtime_file.read_text())
        if runtime.get("pid") != fixture_process.pid or runtime.get("run_id") != run_id or runtime.get("output") != str(run_dir):
            raise RuntimeError("Runtime ownership does not match the exact spawned process")
        while True:
            try:
                health = request_own(runtime, "/api/healthz")
                if health.get("scope") != "personal-simple-fake-native" or health.get("run_id") != run_id:
                    raise RuntimeError("Wrong fixture health identity")
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        summary["fixture_url"] = runtime["url"]
        with (run_dir / "browser-run.log").open("w") as log:
            browser = subprocess.run([node, str(OUTPUT / "run_browser.cjs"), str(runtime_file), str(PLAYWRIGHT)],
                                     cwd=run_dir, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=240)
        summary["browser_exit_code"] = browser.returncode
        report_path = run_dir / "browser-report.json"
        if not report_path.is_file():
            raise RuntimeError("The private browser did not produce terminal evidence")
        summary["browser_report"] = json.loads(report_path.read_text())
        if browser.returncode or summary["browser_report"]["status"] != "passed":
            raise RuntimeError("HTTP/browser acceptance failed; no automatic rerun")
        summary["status"] = "passed"
    except BaseException as exc:
        error = exc
        summary.update(status="failed", error=repr(exc))
    finally:
        if fixture_process is not None and fixture_process.poll() is None:
            if runtime is not None:
                try:
                    request_own(runtime, "/fixture/stop", method="POST", fixture=True)
                    summary["graceful_fixture_stop_requested"] = True
                except Exception as exc:
                    summary["fixture_stop_error"] = repr(exc)
            try:
                fixture_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Only the exact child Popen created here; never a PID search.
                fixture_process.terminate()
                try:
                    fixture_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    fixture_process.kill()
                    fixture_process.wait(timeout=5)
                summary.update(status="failed", forced_owned_fixture_shutdown=True)
        summary["fixture_exited"] = fixture_process is None or fixture_process.poll() is not None
        summary["fixture_exit_code"] = fixture_process.returncode if fixture_process is not None else None
        if fixture_process is not None and fixture_process.returncode != 0:
            summary["status"] = "failed"
        if (run_dir / "http-evidence.json").is_file():
            http = json.loads((run_dir / "http-evidence.json").read_text())
            summary["http_evidence"] = {"path": str(run_dir / "http-evidence.json"), "blocked": http["blocked"],
                "fake_errors": http.get("fake_errors", []), "runtime_removed": http["runtime_removed"], "pending": http["pending"]}
            if http["blocked"] or http.get("fake_errors") or not http["runtime_removed"] or any(http["pending"].values()):
                summary["status"] = "failed"
        else:
            summary["status"] = "failed"
            summary["missing_http_evidence"] = True
        scratch = run_dir / "runtime"
        if scratch.exists():
            if scratch.is_symlink():
                summary.update(status="failed", unsafe_runtime_symlink=True)
            else:
                shutil.rmtree(scratch)
                summary["runner_removed_owned_runtime_after_failure"] = True
        summary["runtime_removed"] = not scratch.exists()
        for name in ("vite-cache", "empty-env"):
            temporary = run_dir / name
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary)
        if runtime is not None:
            port = urllib.parse.urlsplit(runtime["url"]).port
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    check.bind(("127.0.0.1", port))
                    summary["fixture_port_released"] = True
                except OSError as exc:
                    summary.update(status="failed", fixture_port_released=False, port_error=str(exc))
        try:
            summary["source_freeze_at_finish"] = verify_manifest(manifest_path)
        except Exception as exc:
            summary.update(status="failed", source_change_error=str(exc))
        summary["duration_sec"] = round(time.monotonic() - started, 6)
        summary["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (run_dir / "acceptance-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": summary["status"], "summary": str(run_dir / "acceptance-summary.json")}, ensure_ascii=False))
    if error is not None or summary["status"] != "passed":
        raise RuntimeError("One isolated acceptance run failed; preserve evidence, no automatic retry") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--freeze", type=Path, help="Explicitly write a new source freeze after root finishes product work")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--run-id", default="personal-simple-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    args = parser.parse_args()
    if args.freeze:
        freeze_source(args.freeze)
    elif args.execute:
        if args.manifest is None:
            parser.error("--execute requires the root-approved frozen source manifest")
        run_once(args.manifest, args.run_id)
    else:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
