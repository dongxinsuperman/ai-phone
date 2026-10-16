"""Real VM HTTP routes + fake native observations. --serve requires a source freeze.

No Agent process, SDK tool, device, default service or company endpoint is used.
The fake emits only protocol inputs; Server code owns every lifecycle response.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time

from common import CASES, FAILURES, OFFLINE, OWNER, OUTPUT, PLATFORMS, REPO, SPARE
from common import identities, observation, own_run_dir, plan, sha256, verify_manifest, vm_id


async def serve(run_dir, manifest_path, *, smoke=False):
    run_dir = own_run_dir(run_dir, must_exist=True)
    manifest_path = Path(manifest_path).resolve()
    freeze = verify_manifest(manifest_path)
    scratch = run_dir / "runtime"
    if scratch.exists() or scratch.is_symlink():
        raise RuntimeError("The fixture runtime must be fresh")
    frontend = run_dir / "web-dist"
    if not smoke and (frontend.is_symlink() or not (frontend / "index.html").is_file()):
        raise RuntimeError("A separately built frozen frontend is required")
    scratch.mkdir(mode=0o700)
    os.chdir(scratch)
    sys.dont_write_bytecode = True
    token = secrets.token_urlsafe(32)
    for key in list(os.environ):
        if key.startswith(("AI_PHONE_", "AIPHONE_")) or key.lower().endswith("_proxy"):
            os.environ.pop(key)
    os.environ.update(AI_PHONE_DB_URL=f"sqlite+aiosqlite:///{scratch / 'fixture.db'}",
                      AI_PHONE_STORAGE_DIR=str(scratch / "storage"), AI_PHONE_AGENT_TOKEN=token,
                      AI_PHONE_SUBMISSION_INTERNAL_TOKEN=token,
                      AI_PHONE_SERVER_HTTP_BASE="http://127.0.0.1:9/blocked",
                      AI_PHONE_SERVER_WS_URL="ws://127.0.0.1:9/blocked", NO_PROXY="127.0.0.1,::1,localhost")
    sys.path.insert(0, str(REPO / "backend"))
    port = 0
    blocked, events, tasks, fake_errors = [], [], set(), []
    started = time.monotonic()

    def audit(event, args):
        deny = event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.kill", "os.killpg"}
        if event in {"socket.connect", "socket.connect_ex", "socket.bind"}:
            address = args[1]
            if isinstance(address, tuple):
                try: local = ipaddress.ip_address(address[0]).is_loopback
                except ValueError: local = False
                deny = not local or address[1] in {8000, 5180} or (event != "socket.bind" and address[1] != port)
            else:
                deny = True
        if event == "socket.getaddrinfo":
            deny = args[0] not in {"127.0.0.1", "::1", "localhost"}
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0]))
            if path.name in {".env", ".env.local"}:
                deny = not path.resolve().is_relative_to(scratch)
        if deny:
            blocked.append({"event": event, "at": time.monotonic() - started})
            raise PermissionError("Personal fixture blocked " + event)
    sys.addaudithook(audit)

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    from ai_phone.config import get_settings
    from ai_phone.server import db
    from ai_phone.server.api import include_routers  # noqa: F401 - bootstrap package before domain router imports
    from ai_phone.server.android_vm import api as android_api, service as android_svc
    from ai_phone.server.ios_sim import api as ios_api, service as ios_svc
    from ai_phone.server.harmony_vm import api as harmony_api, service as harmony_svc
    from ai_phone.server.models import AndroidVmInstance
    from ai_phone.server.ios_sim.models import IosSimVmInstance
    from ai_phone.server.harmony_vm.models import HarmonyVmInstance, HarmonyVmPortLease
    from ai_phone.server.hub import Hub
    from ai_phone.server.lockstore import DeviceLockStore
    from ai_phone.server import vm_lifecycle as lifecycle

    globals()["Request"] = Request
    get_settings.cache_clear()
    assert get_settings().db_url == os.environ["AI_PHONE_DB_URL"]
    models = {"android": AndroidVmInstance, "ios": IosSimVmInstance, "harmony": HarmonyVmInstance}
    services = {"android": android_svc, "ios": ios_svc, "harmony": harmony_svc}
    waiters = {key: value.get_capability_waiter() for key, value in services.items()}
    specs = {vm_id(platform, case): {"vm_id": vm_id(platform, case), "platform": platform, "case": case,
             "label": label, "physical_state": state, "calls": {"start": 0, "stop": 0, "delete": 0, "inspect": 0}}
             for platform in PLATFORMS for case, label, state in CASES}

    def record(event, **data):
        events.append({"event": event, "seconds": round(time.monotonic() - started, 6), **data})

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        def completed(value):
            tasks.discard(value)
            if not value.cancelled() and value.exception() is not None:
                error = repr(value.exception())
                fake_errors.append(error)
                record("fake-delivery-error", error=error)
        task.add_done_callback(completed)
        return task

    async def read_vm(spec):
        async with db.get_session_factory()() as session:
            vm = await session.get(models[spec["platform"]], spec["vm_id"])
            if vm is None:
                return None
            if spec["platform"] == "harmony" and vm.lease_token and vm.hdc_serial:
                # A real Harmony _known entry retains the last native lease
                # identity after Server releases its row. A later allocation
                # replaces it only when that new operation is actually sent.
                spec["native_lease_token"], spec["native_serial"] = vm.lease_token, vm.hdc_serial
            # Capture native identity from the real current row/lease, not a
            # browser-supplied or stale seed identifier.
            return {"id": vm.id, "owner": vm.assigned_agent_id,
                    "runtime": lifecycle.runtime(vm), "raw": vm.to_dict(),
                    "lease_token": str(getattr(vm, "lease_token", None) or ""),
                    "serial": str(getattr(vm, "hdc_serial", None) or getattr(vm, "adb_serial", None) or ""),
                    "native_lease_token": spec.get("native_lease_token", ""),
                    "native_serial": spec.get("native_serial", ""),
                    "udid": str(getattr(vm, "udid", None) or "")}

    def observed(spec, state, current, reason=""):
        return observation(spec["platform"], spec["vm_id"], state, scratch=scratch,
                           lease_token=current.get("native_lease_token") or current.get("lease_token", ""),
                           serial=current.get("native_serial") or current.get("serial", ""), reason=reason)

    async def deliver(spec, payload, action, *, success, passive=False, seeded=False):
        current = await read_vm(spec)
        if current is None:
            raise RuntimeError("Fake status target is absent from the owned database")
        physical = ("running" if action == "start" else "stopped") if success and not passive else spec["physical_state"]
        if physical == "unknown":
            physical = "stopped" if action == "start" else "running"
        if not passive:
            spec["physical_state"] = physical
        request_id = str(payload.get("request_id") or "")
        reason = "reclaimed" if passive else ("started" if action == "start" else "stopped") if success else "fixture_" + action + "_failed"
        details = {"instance_created": True, "cleanup_confirmed": physical == "stopped",
                   "instance_observation": observed(spec, physical, current,
                       "被动观察：" + request_id if passive else ""), **identities(spec["platform"], spec["vm_id"],
                       scratch=scratch, lease_token=current.get("native_lease_token") or current["lease_token"],
                       serial=current.get("native_serial") or current["serial"])}
        if spec["case"] == "stale_cleanup" and seeded:
            details["cleanup_confirmed"] = False
        msg = {"vm_id": spec["vm_id"], "request_id": "" if passive else request_id,
               "observed_request_id": request_id if passive else "",
               "state": physical if success else "error", "ok": success, "reason": reason,
               "error": "" if success else FAILURES[action], "details": details}
        if spec["platform"] == "android":
            msg["adb_serial"] = current["serial"]
        elif spec["platform"] == "ios":
            msg["udid"] = current["udid"]
        else:
            msg.update(lease_token=payload.get("lease_token", current.get("native_lease_token") or current["lease_token"]),
                       hdc_serial=payload.get("hdc_serial", current.get("native_serial") or current["serial"]))
        record("status-input", platform=spec["platform"], vm_id=spec["vm_id"], action=action,
               request_id=request_id, success=success, passive=passive, seeded=seeded, message=msg)
        await services[spec["platform"]].handle_vm_status(current["owner"], msg, hub)
        record("status-persisted", vm_id=spec["vm_id"], action=action, request_id=request_id,
               passive=passive, seeded=seeded, database=await read_vm(spec))

    class FakeHub(Hub):
        names = {OWNER: "个人验收主机", SPARE: "个人验收备用主机"}

        def __init__(self):
            super().__init__()
            self.vm_lock_store = DeviceLockStore()

        def snapshot(self):
            return {"agents": [{"agent_id": key, "agent_name": label, "host_os": "Darwin"}
                               for key, label in self.names.items()]}

        def has_agent(self, agent_id):
            return agent_id in self.names

        def agent_host_id(self, agent_id):
            return "personal-simple-host-" + agent_id if agent_id in {OWNER, SPARE, OFFLINE} else ""

        async def send_to_agent(self, agent_id, payload):
            ident, kind = str(payload.get("vm_id") or ""), str(payload.get("type") or "")
            if ident not in specs or agent_id not in {OWNER, SPARE, OFFLINE}:
                raise RuntimeError("FakeHub refused an unknown VM/Agent")
            spec = specs[ident]
            record("agent-send", platform=spec["platform"], agent_id=agent_id, vm_id=ident,
                   kind=kind, request_id=payload.get("request_id"), inspect=payload.get("inspect_instance") is True)
            if agent_id == OFFLINE:
                return False
            if "capability_probe" in kind:
                inspect = payload.get("inspect_instance") is True
                if inspect:
                    spec["calls"]["inspect"] += 1
                current = await read_vm(spec)
                details = {"host_abi": "arm64", "used_ports": [], "memory_available_gb": 32}
                if inspect:
                    details["instance_observation"] = observed(spec, spec["physical_state"], current)
                reply = {"vm_id": ident, "request_id": payload["request_id"], "ok": True,
                         "reason": "个人夹具的协议观察", "warning": "", "details": details}
                async def answer():
                    await asyncio.sleep(0.03)
                    accepted = waiters[spec["platform"]].resolve(agent_id, reply)
                    record("probe-resolved", vm_id=ident, platform=spec["platform"], agent_id=agent_id, request_id=payload["request_id"],
                           accepted=accepted, inspect=inspect, observation=details.get("instance_observation"))
                spawn(answer())
                return True
            action = next((value for value in ("start", "stop", "delete") if kind.endswith("_" + value)), None)
            if action is None:
                raise RuntimeError("FakeHub received an unexpected command")
            spec["calls"][action] += 1
            record("lifecycle-send", vm_id=ident, platform=spec["platform"], agent_id=agent_id,
                   action=action, request_id=payload.get("request_id"), command=dict(payload))
            if action == "delete":
                spec["physical_state"] = "missing"
                return True
            fail = spec["case"] == action + "_failure" and spec["calls"][action] == 1
            async def terminal():
                await asyncio.sleep(0.04)
                await deliver(spec, payload, action, success=not fail)
            spawn(terminal())
            return True

    hub = FakeHub()
    app = FastAPI(docs_url=None, redoc_url=None)
    app.state.hub, app.state.lock_store = hub, hub.vm_lock_store
    server = None

    def fixture_auth(request):
        if request.headers.get("x-personal-fixture-token") != token:
            raise HTTPException(403, "Wrong private fixture token")

    @app.middleware("http")
    async def trace(request: Request, call_next):
        record("http", method=request.method, path=request.url.path)
        response = await call_next(request)
        record("http-response", method=request.method, path=request.url.path, status_code=response.status_code)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:"
        return response

    @app.get("/api/config")
    async def config(): return {}

    @app.get("/api/healthz")
    async def health(): return {"ok": True, "scope": "personal-simple-fake-native", "run_id": run_dir.name}

    @app.get("/api/internal/vm/device-profiles")
    async def profiles(): return {"items": [], "page": {"total": 0, "offset": 0, "limit": 60, "has_more": False}, "stats": {}}

    @app.get("/api/internal/vm/device-facets")
    async def facets(): return {"form_factor": [], "brand": [], "sdk": [], "resolution": [], "ram": [], "matched_total": 0}

    @app.get("/api/internal/ios-sim/catalog")
    async def ios_catalog(): return {"device_types": [], "official_runtimes": [], "source": "personal fixture"}

    @app.get("/api/internal/harmony-vm/catalog")
    async def harmony_catalog(): return {"ok": True, "device_types": [], "images": [], "screen_profiles": [], "source": "personal fixture", "stats": {}}

    @app.get("/fixture/state")
    async def state(request: Request):
        fixture_auth(request)
        return {"scope": "personal-simple-fake-native", "run_id": run_dir.name, "events": events,
                "scenarios": specs, "pending": {key: len(value._pending) for key, value in waiters.items()},
                "blocked": blocked, "fake_errors": fake_errors}

    @app.post("/fixture/passive-observation/{ident}")
    async def passive_observation(ident: str, request: Request):
        fixture_auth(request)
        spec = specs.get(ident)
        if spec is None or spec["case"] not in {"start_failure", "stop_failure"}:
            raise HTTPException(400, "Only the owned unresolved-failure observations are enabled")
        current = await read_vm(spec)
        op = current["runtime"].get("operation") or {}
        if op.get("pending") or op.get("outcome") != "failed":
            raise HTTPException(409, "This fixture expects an exact failed terminal operation")
        await deliver(spec, {"request_id": op["request_id"]}, op["action"], success=True, passive=True)
        return {"observed": True, "request_id": op["request_id"]}

    @app.post("/fixture/stop")
    async def shutdown(request: Request):
        fixture_auth(request)
        server.should_exit = True
        return {"stopping": True}

    for module in (android_api, ios_api, harmony_api):
        app.include_router(module.router)
    if not smoke:
        app.mount("/assets", StaticFiles(directory=str(frontend / "assets")), name="assets")

    @app.get("/{path:path}")
    async def frontend_page(path: str):
        return FileResponse(frontend / "index.html")

    db.init_engine(db_url=os.environ["AI_PHONE_DB_URL"])
    sock = None
    try:
        async with db.get_engine().begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all)
        async with db.get_session_factory()() as session:
            for index, spec in enumerate(specs.values()):
                platform, case, ident = spec["platform"], spec["case"], spec["vm_id"]
                owner = OFFLINE if case == "offline" else OWNER
                action = "start" if case in {"start_failure", "busy"} else "stop"
                fields = {"id": ident, "name": platform + " " + spec["label"], "alias": platform + " " + spec["label"],
                          "state": "starting" if action == "start" else "stopping", "assigned_agent_id": owner}
                data = {"owner_host_id": hub.agent_host_id(owner), "instance_created": True}
                if platform == "android":
                    fields.update(api_level=33, abi="arm64", system_type="google_apis", adb_serial=f"emulator-{5554 + index * 2}", runtime=data,
                                  config_json={"storage": {"snapshot_policy": "no_snapshot", "wipe_data": False}, "performance": {"ram_mb": 2048}})
                elif platform == "ios":
                    fields.update(device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-16",
                                  device_type_name="个人夹具 iPhone", runtime="com.apple.CoreSimulator.SimRuntime.iOS-18-5",
                                  runtime_name="iOS 18.5", os_version="18.5", runtime_state=data,
                                  udid=identities(platform, ident, scratch=scratch)["udid"])
                else:
                    lease = "personal-fixture-lease-" + ident
                    hdc_port = 10001 + index
                    fields.update(device_type="Phone", os_version="5.0.0", api_version="12", abi="arm64",
                                  image_id="personal-fixture-harmony-image", hdc_port=hdc_port,
                                  hdc_serial=harmony_svc.serial_for_port(hdc_port), lease_token=lease, runtime=data)
                    session.add(HarmonyVmPortLease(port=hdc_port, vm_id=ident, agent_id=owner, lease_token=lease, state="active"))
                vm = models[platform](**fields)
                session.add(vm)
                lifecycle.begin_operation(vm, action, "seed-" + ident, target_agent_id=owner,
                                          target_host_id=hub.agent_host_id(owner), timeout_sec=600)
                if case in {"unknown_stopped", "unknown_running", "offline"}:
                    lifecycle.update_runtime(vm, operation={"request_id": "seed-" + ident, "action": action,
                        "pending": False, "outcome": "failed", "first_bind": False},
                        instance_observation=observed(spec, "unknown", {"lease_token": fields.get("lease_token", ""),
                            "serial": fields.get("hdc_serial", fields.get("adb_serial", ""))}, "当前实例尚未核实"))
                    vm.state = "agent_offline" if case == "offline" else "error"
            await session.commit()
        # Seed historical terminal facts through the actual status handlers.
        for spec in specs.values():
            if spec["case"] not in {"stale_cleanup", "start_failure", "stop_failure"}:
                continue
            action = "start" if spec["case"] == "start_failure" else "stop"
            await deliver(spec, {"request_id": "seed-" + spec["vm_id"]}, action,
                          success=spec["case"] == "stale_cleanup", seeded=True)
        for spec in specs.values():
            record("seed-database", vm_id=spec["vm_id"], database=await read_vm(spec))

        if smoke:
            # In-process ASGI only: no socket listener, Vue build or browser.
            # Exercise actual payload construction, FakeHub record/probe/send,
            # service ACK persistence and operation terminal state per platform.
            import httpx
            checks = []
            smoke_result = {"status": "running", "scope": "isolated real ASGI routes plus FakeHub; no network listener/native/browser",
                            "checks": checks, "native_or_agent_started": False}
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://127.0.0.1:9", trust_env=False) as client:
                    for platform, domain in PLATFORMS.items():
                        ident = vm_id(platform, "unknown_stopped")
                        base = f"/api/internal/{domain}/instances/{ident}"
                        request_ids = []
                        for action, desired in (("start", "running"), ("stop", "stopped")):
                            response = await client.post(base + "/" + action, headers={"Authorization": "Bearer " + token})
                            body = response.json()
                            if response.status_code != 200 or body.get("sent") is not True:
                                raise RuntimeError(f"ASGI smoke {platform}/{action}: {response.status_code} {body}")
                            instance = body.get("instance") or {}
                            operation = (instance.get("runtime_state") or instance.get("runtime") or {}).get("operation") or {}
                            request_id = operation.get("request_id")
                            if not request_id or request_id in request_ids or operation.get("action") != action:
                                raise RuntimeError("ASGI smoke did not receive a new exact operation")
                            request_ids.append(request_id)
                            deadline = time.monotonic() + 10
                            while True:
                                read = await client.get(base, headers={"Authorization": "Bearer " + token})
                                terminal = read.json()
                                current = (terminal.get("runtime_state") or terminal.get("runtime") or {}).get("operation") or {}
                                if read.status_code != 200 or current.get("request_id") != request_id:
                                    raise RuntimeError("ASGI smoke readback changed operation identity")
                                if current.get("outcome") == "failed":
                                    raise RuntimeError(f"ASGI smoke operation failed: {terminal}")
                                if current.get("outcome") == "succeeded" and current.get("pending") is False:
                                    if terminal.get("lifecycle", {}).get("state") != desired:
                                        raise RuntimeError("ASGI smoke terminal physical state does not match the action")
                                    break
                                if time.monotonic() >= deadline:
                                    raise TimeoutError(f"ASGI smoke {platform}/{action} terminal outcome timed out")
                                await asyncio.sleep(0.01)
                            checks.append({"platform": platform, "action": action, "request_id": request_id,
                                           "outcome": current["outcome"], "server_lifecycle": terminal["lifecycle"], "passed": True})
                        calls = specs[ident]["calls"]
                        if calls["start"] != 1 or calls["stop"] != 1 or calls["inspect"] != 1:
                            raise RuntimeError(f"ASGI smoke unexpected native/probe count: {platform} {calls}")
                if tasks:
                    await asyncio.gather(*tuple(tasks))
                if blocked or fake_errors or any(waiter._pending for waiter in waiters.values()):
                    raise RuntimeError("ASGI smoke left blocked/error/pending work")
                smoke_result.update(status="passed", action_count=len(checks),
                                    message_counts={platform: specs[vm_id(platform, "unknown_stopped")]["calls"] for platform in PLATFORMS})
            except BaseException as exc:
                smoke_result.update(status="failed", error=repr(exc))
                raise
            finally:
                (run_dir / "asgi-smoke-report.json").write_text(json.dumps(smoke_result, ensure_ascii=False, indent=2) + "\n")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0)); sock.listen(128)
        port = sock.getsockname()[1]
        if port in {8000, 5180}:
            raise RuntimeError("Forbidden main-service port selected")
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=3))
        runtime_info = {"scope": "personal-simple-fake-native", "run_id": run_dir.name,
                        "url": f"http://127.0.0.1:{port}", "pid": os.getpid(), "runtime": str(scratch),
                        "output": str(run_dir), "auth_token": token, "source_freeze": freeze["manifest_sha256"]}
        runtime_file = run_dir / "fixture-runtime.json"
        runtime_file.write_text(json.dumps(runtime_info, indent=2) + "\n")
        runtime_file.chmod(0o600)
        print("PERSONAL_FIXTURE " + runtime_info["url"], flush=True)
        async def deadline():
            await asyncio.sleep(300)
            record("fixture-deadline")
            server.should_exit = True
        watchdog = spawn(deadline())
        await server.serve(sockets=[sock])
    finally:
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
        final_rows = {}
        async with db.get_session_factory()() as session:
            for spec in specs.values():
                vm = await session.get(models[spec["platform"]], spec["vm_id"])
                if vm is not None:
                    lifecycle.cancel_operation_timeout(vm)
                    final_rows[vm.id] = vm.to_dict()
        await db.dispose_engine()
        if sock is not None:
            sock.close()
        try:
            after = verify_manifest(manifest_path)
        except Exception as exc:
            after = {"mismatches": [str(exc)]}
        shutil.rmtree(scratch)
        (run_dir / "http-evidence.json").write_text(json.dumps({"scope": "real Server projection; fake Agent protocol inputs only",
            "events": events, "blocked": blocked, "fake_errors": fake_errors, "final_rows": final_rows,
            "pending": {key: len(value._pending) for key, value in waiters.items()},
            "runtime_removed": not scratch.exists(), "port": port, "source_freeze_at_start": freeze,
            "source_freeze_at_finish": after}, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--smoke", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if not args.serve and not args.smoke:
        print(json.dumps(plan(), ensure_ascii=False, indent=2))
        return
    if args.run_dir is None or args.manifest is None:
        parser.error("--serve requires the exact owned run directory and frozen source manifest")
    if args.smoke:
        target = own_run_dir(args.run_dir)
        if target.exists():
            parser.error("Smoke requires a separate fresh owned run directory")
        target.mkdir(parents=True, mode=0o700)
    asyncio.run(serve(args.run_dir, args.manifest, smoke=args.smoke))


if __name__ == "__main__":
    main()
