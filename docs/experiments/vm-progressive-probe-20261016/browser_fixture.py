"""Personal loopback acceptance: real VM HTTP routes and built UI, fake Agents.

No native commands, default profiles, business database or external networking.
The 3 second probe deadline is a fixture override, not a product setting.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time

OUTPUT = Path(__file__).resolve().parent
REPO = OUTPUT.parents[2]
SCRATCH = Path(tempfile.mkdtemp(prefix="aiphone-progressive-browser-")).resolve()
os.chdir(SCRATCH)
for key in list(os.environ):
    if key.startswith(("AI_PHONE_", "AIPHONE_")) or key.lower().endswith("_proxy"):
        os.environ.pop(key)
os.environ.update(
    AI_PHONE_DB_URL=f"sqlite+aiosqlite:///{SCRATCH / 'fixture.db'}",
    AI_PHONE_STORAGE_DIR=str(SCRATCH / "storage"),
    AI_PHONE_SUBMISSION_INTERNAL_TOKEN="dev",
    AI_PHONE_AGENT_TOKEN="dev",
    AI_PHONE_SERVER_HTTP_BASE="http://127.0.0.1:9/blocked",
    AI_PHONE_SERVER_WS_URL="ws://127.0.0.1:9/blocked",
)
sys.path.insert(0, str(REPO / "backend"))
PORT = 0
blocked = []


def audit(event, args):
    deny = event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.kill", "os.killpg"}
    if event in {"socket.connect", "socket.connect_ex", "socket.bind"}:
        address = args[1]
        if isinstance(address, tuple):
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = False
            deny = not local or (event != "socket.bind" and address[1] != PORT)
        else:
            deny = True
    if event == "socket.getaddrinfo":
        deny = args[0] not in {"127.0.0.1", "::1", "localhost"}
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0]))
        if path.name in {".env", ".env.local"}:
            deny = not path.resolve().is_relative_to(SCRATCH)
    if deny:
        blocked.append(event)
        raise PermissionError(f"Personal fixture blocked {event}")


sys.addaudithook(audit)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
import uvicorn  # noqa: E402
from ai_phone.server import db  # noqa: E402
from ai_phone.server.api import include_routers  # noqa: E402,F401
from ai_phone.server.android_vm import api as android_api, service as android_svc  # noqa: E402
from ai_phone.server.ios_sim import api as ios_api, service as ios_svc  # noqa: E402
from ai_phone.server.harmony_vm import api as harmony_api, service as harmony_svc  # noqa: E402
from ai_phone.server.models import AndroidVmInstance  # noqa: E402
from ai_phone.server.ios_sim.models import IosSimVmInstance  # noqa: E402
from ai_phone.server.harmony_vm.models import HarmonyVmInstance  # noqa: E402
from ai_phone.server.hub import Hub  # noqa: E402
from ai_phone.server.lockstore import DeviceLockStore  # noqa: E402
from ai_phone.server.vm_lifecycle import cancel_operation_timeout  # noqa: E402

started = time.monotonic()
events = []
tasks = set()
waiters = {
    "android": android_svc.get_capability_waiter(),
    "ios": ios_svc.get_capability_waiter(),
    "harmony": harmony_svc.get_capability_waiter(),
}
for waiter in waiters.values():
    original = waiter.probe_stream

    def shortened(*, hub, vm, timeout_sec=3.0, _original=original):
        return _original(hub=hub, vm=vm, timeout_sec=timeout_sec)

    waiter.probe_stream = shortened


def record(event, **data):
    events.append({"event": event, "seconds": round(time.monotonic() - started, 5), **data})


class FakeHub(Hub):
    names = {
        "a-slow-send": "发送较慢的测试电脑",
        "b-slow-reply": "响应较慢的测试电脑",
        "c-fast": "已就绪的测试电脑",
        "d-send-failed": "通道异常的测试电脑",
    }

    def __init__(self):
        super().__init__()
        self.vm_lock_store = DeviceLockStore()

    def snapshot(self):
        return {"agents": [{"agent_id": key, "agent_name": name, "host_os": "Darwin"}
                           for key, name in self.names.items()]}

    def has_agent(self, agent_id):
        return agent_id in self.names

    def agent_host_id(self, agent_id):
        return f"fixture-host-{agent_id}"

    async def send_to_agent(self, agent_id, payload):
        kind = payload.get("type", "")
        record("agent_send", agent_id=agent_id, kind=kind, vm_id=payload.get("vm_id"), request_id=payload.get("request_id"))
        if "capability_probe" not in kind:
            # Only acknowledge transport. No simulated native success is emitted.
            record("lifecycle_transport_only", agent_id=agent_id, kind=kind, vm_id=payload.get("vm_id"))
            return True
        platform = "harmony" if "harmony" in kind else "ios" if "ios" in kind else "android"
        if agent_id == "a-slow-send":
            try:
                await asyncio.Event().wait()
            finally:
                record("slow_send_cancelled", vm_id=payload.get("vm_id"), request_id=payload.get("request_id"))
        if agent_id == "d-send-failed":
            return False
        if agent_id == "b-slow-reply":
            if str(payload.get("vm_id", "")).endswith("close"):
                async def late_reply():
                    await asyncio.sleep(0.9)
                    accepted = waiters[platform].resolve(agent_id, {
                        "request_id": payload["request_id"], "ok": True,
                        "reason": "旧轮次迟到结果", "details": {},
                    })
                    record("late_reply", request_id=payload["request_id"], accepted=accepted)
                task = asyncio.create_task(late_reply())
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            return True
        await asyncio.sleep(0.25)
        accepted = waiters[platform].resolve(agent_id, {
            "request_id": payload["request_id"], "ok": True,
            "reason": "测试环境可承接", "warning": "", "details": {},
        })
        record("fast_reply", request_id=payload["request_id"], accepted=accepted, vm_id=payload.get("vm_id"))
        return True


app = FastAPI(docs_url=None, redoc_url=None)
hub = FakeHub()
app.state.hub = hub
app.state.lock_store = hub.vm_lock_store
server = None


@app.middleware("http")
async def trace(request: Request, call_next):
    record("http", method=request.method, path=request.url.path, query=request.url.query)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:"
    return response


@app.get("/api/config")
async def config():
    return {}


@app.get("/api/healthz")
async def health():
    return {"ok": True, "version": "personal-progressive-fixture"}


@app.get("/api/internal/vm/device-profiles")
async def profiles():
    return {"items": [], "page": {"total": 0, "offset": 0, "limit": 60, "has_more": False}, "stats": {}}


@app.get("/api/internal/vm/device-facets")
async def facets():
    return {"form_factor": [], "brand": [], "sdk": [], "resolution": [], "ram": [], "matched_total": 0}


@app.get("/api/internal/ios-sim/catalog")
async def ios_catalog():
    return {"device_types": [], "official_runtimes": [], "source": "personal fixture"}


@app.get("/api/internal/harmony-vm/catalog")
async def harmony_catalog():
    return {"ok": True, "device_types": [], "images": [], "screen_profiles": [], "source": "personal fixture", "stats": {}}


@app.get("/fixture/state")
async def state():
    return {"events": events, "pending": {key: len(value._pending) for key, value in waiters.items()}, "blocked": blocked}


@app.post("/fixture/stop")
async def stop():
    server.should_exit = True
    return {"stopping": True}


for module in (android_api, ios_api, harmony_api):
    app.include_router(module.router)
app.mount("/assets", StaticFiles(directory=str(REPO / "web/dist/assets")), name="assets")


@app.get("/{path:path}")
async def frontend(path: str):
    return FileResponse(REPO / "web/dist/index.html")


async def main():
    global PORT, server
    db.init_engine(db_url=os.environ["AI_PHONE_DB_URL"])
    async with db.get_engine().begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    records = []
    async with db.get_session_factory()() as session:
        for platform, model in [("android", AndroidVmInstance), ("ios", IosSimVmInstance), ("harmony", HarmonyVmInstance)]:
            for case in ("pick", "timeout", "close"):
                fields = {"id": f"stream-{platform}-{case}", "name": f"探查验证 {case}", "alias": f"{platform} 探查验证 {case}", "state": "draft"}
                if platform == "android":
                    fields.update(api_level=33, abi="arm64")
                vm = model(**fields)
                session.add(vm)
                records.append(vm)
        await session.commit()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    PORT = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=3))
    (OUTPUT / "fixture-runtime.json").write_text(json.dumps({"url": f"http://127.0.0.1:{PORT}", "pid": os.getpid(), "runtime": str(SCRATCH), "scope": "real HTTP routers and built UI with fake Agents; no native execution"}, indent=2) + "\n")
    print(f"PERSONAL_FIXTURE http://127.0.0.1:{PORT}", flush=True)
    try:
        await server.serve(sockets=[sock])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with db.get_session_factory()() as session:
            for vm in records:
                current = await session.get(type(vm), vm.id)
                if current is not None:
                    cancel_operation_timeout(current)
        await asyncio.sleep(0)
        await db.dispose_engine()
        sock.close()
        shutil.rmtree(SCRATCH)
        (OUTPUT / "http-evidence.json").write_text(json.dumps({"events": events, "blocked": blocked, "pending": {key: len(value._pending) for key, value in waiters.items()}, "runtime_removed": not SCRATCH.exists(), "port": PORT}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if SCRATCH.exists():
            shutil.rmtree(SCRATCH)
