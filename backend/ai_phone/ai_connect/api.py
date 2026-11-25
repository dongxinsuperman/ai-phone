"""A direct session binds the existing device lock to its advertised device channels."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ai_phone.server.api._deps import DBSession, HubDep, LockStoreDep, hub as get_hub, lock_store
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import BadToken, DeviceLockStore, LockConflict, LockNotFound
from ai_phone.server.models import Device
from ai_phone.shared import protocol as P
from sqlalchemy.ext.asyncio import AsyncSession

from . import android_actions, hdc, ios_wda, unified
from .android_changes import changes
from .android import COMMAND_SECONDS, allowed_service, hardware_serial, is_local_emulator, is_network_adb
from .client_bundle import client_bundle
from .relay import connect_native

router = APIRouter(tags=["ai-connect"])
BASE = "/api/ai-connect"
NO_CACHE = {"Cache-Control": "no-store"}
LEASE_SECONDS = 30 * 60


def bearer(headers):
    scheme, _, value = headers.get("authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" else ""


@dataclass
class Lease:
    serial: str
    host_id: str
    transport: int | str
    lock_token: str
    expires: float
    unified_target: unified.Target
    connection_info: dict
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    running: asyncio.Task | None = None
    tunnels: set = field(default_factory=set)
    screen: unified.Screen | None = None
    native_revision: int = 0
    wda_session: ios_wda.Session | None = None


class DirectAccess:
    def __init__(self):
        self.leases = {}
        self.tickets = {}

    def lookup(self, ident, request):
        lease = self.leases.get(ident)
        if lease is None:
            raise HTTPException(410, "本次设备占用已结束")
        if not secrets.compare_digest(lease.secret.encode(), bearer(request.headers).encode()):
            raise HTTPException(403, "需要本次领取返回的凭证")
        return lease

    def check_lease(self, lease, store):
        lock = store.peek(lease.serial)
        if lease.closing or not lock or lock.token != lease.lock_token or time.monotonic() >= lease.expires:
            raise HTTPException(410, "本次设备占用已结束")

    def check_device(self, lease, store, hub):
        self.check_lease(lease, store)
        if lease.unified_target.platform != "android":
            raise HTTPException(409, "该设备未提供 Android ADB 原生通道")
        agent = hub.agent_id_for_serial(lease.serial)
        current = hub.agent_native_channel(agent)
        if (not agent or hub.agent_host_id(agent) != lease.host_id or current is None
                or current.devices.get(lease.serial) != lease.transport):
            raise HTTPException(503, "原设备的 ADB 通道暂不可用，占用有效期间可以重连")
        return current

    def check_wda_device(self, lease, store, hub):
        self.check_lease(lease, store)
        if lease.connection_info["native"]["protocol"] != "wda-http":
            raise HTTPException(409, "该设备未提供 iOS WDA 原生通道")
        agent = hub.agent_id_for_serial(lease.serial)
        current = hub.agent_native_channel(agent)
        instance = lease.unified_target.instance_id
        if instance and unified.simulator_instance(lease.serial, "ios", hub.get_device_extra(lease.serial)) != instance:
            raise HTTPException(503, "本次占用的 iOS 模拟器实例暂不可用")
        if (not agent or hub.agent_host_id(agent) != lease.host_id or current is None
                or current.wda_transport(lease.serial, simulator=bool(instance)) != lease.transport):
            raise HTTPException(503, "原设备的 WDA 通道暂不可用")
        return current

    def check_hdc_device(self, lease, store, hub):
        self.check_lease(lease, store)
        target = lease.unified_target
        if target.platform != "harmony":
            raise HTTPException(409, "该设备未提供鸿蒙 HDC 原生通道")
        agent = hub.agent_id_for_serial(lease.serial)
        current = hub.agent_native_channel(agent)
        instance = unified.simulator_instance(lease.serial, "harmony", hub.get_device_extra(lease.serial))
        if (not agent or hub.agent_host_id(agent) != lease.host_id or current is None
                or instance != target.instance_id or current.hdc_devices.get(lease.serial) != lease.transport):
            raise HTTPException(503, "原设备的 HDC 通道暂不可用")
        return current

    async def activity(self, lease, store, hub):
        if lease.unified_target.platform == "harmony":
            self.check_hdc_device(lease, store, hub)
        elif lease.unified_target.platform == "ios":
            self.check_wda_device(lease, store, hub)
        else:
            self.check_device(lease, store, hub)
        await self.renew(lease, store)
        # A native service or interactive write can change what a numbered target means.
        lease.native_revision += 1
        lease.screen = None

    async def renew(self, lease, store):
        self.check_lease(lease, store)
        try:
            lock = await store.heartbeat(lease.serial, lease.lock_token)
        except (BadToken, LockNotFound):
            raise HTTPException(410, "本次设备占用已结束") from None
        lease.expires = lock.last_heartbeat_at + lock.ttl_seconds


def state(app):
    if not hasattr(app.state, "ai_direct"):
        app.state.ai_direct = DirectAccess()
    return app.state.ai_direct


@router.websocket("/ws/ai-connect/pipe")
async def agent_pipe(ws: WebSocket):
    tunnel = state(ws.app).tickets.pop(bearer(ws.headers), None)
    if tunnel is None or tunnel.stop.is_set():
        await ws.close(code=1008)
        return
    await ws.accept()
    if tunnel.stop.is_set() or tunnel.closed.is_set():
        await ws.close()
        return
    tunnel.ready.set_result(ws)
    await tunnel.closed.wait()


@router.websocket("/ws/ai-connect/sessions/{ident}/adb")
async def native_socket(ident: str, ws: WebSocket):
    direct = state(ws.app)
    store, hub = lock_store(ws), get_hub(ws)
    try:
        lease = direct.lookup(ident, ws)
        host = direct.check_device(lease, store, hub)
    except HTTPException as exc:
        if exc.status_code == 503:
            # The client can reconnect with the same lease when its ADB host returns.
            await ws.accept()
            await ws.close(code=1013, reason="ADB host temporarily unavailable")
        else:
            await ws.close(code=1008)
        return

    def check():
        if direct.check_device(lease, store, hub) is not host:
            raise HTTPException(503, "ADB 宿主通道已重新连接")

    await connect_native(ws, direct, lease, host, check, lambda: direct.activity(lease, store, hub))


@router.websocket("/ws/ai-connect/sessions/{ident}/hdc")
async def native_hdc_socket(ident: str, ws: WebSocket):
    direct = state(ws.app)
    store, hub = lock_store(ws), get_hub(ws)
    try:
        lease = direct.lookup(ident, ws)
        host = direct.check_hdc_device(lease, store, hub)
    except HTTPException as exc:
        if exc.status_code == 503:
            await ws.accept()
            await ws.close(code=1013, reason="HDC host temporarily unavailable")
        else:
            await ws.close(code=1008)
        return

    def check():
        if direct.check_hdc_device(lease, store, hub) is not host:
            raise HTTPException(503, "HDC 宿主通道已重新连接")

    await connect_native(ws, direct, lease, host, check, lambda: direct.activity(lease, store, hub),
                         hdc_binding=lease.unified_target.binding, host_hdc=lease.unified_target.instance_id is None)


@router.websocket("/ws/ai-connect/sessions/{ident}/webkit")
async def native_webkit_socket(ident: str, ws: WebSocket):
    direct = state(ws.app)
    store, hub = lock_store(ws), get_hub(ws)
    try:
        lease = direct.lookup(ident, ws)
        host = direct.check_wda_device(lease, store, hub)
    except HTTPException as exc:
        if exc.status_code == 503:
            await ws.accept()
            await ws.close(code=1013, reason="Web Inspector device temporarily unavailable")
        else:
            await ws.close(code=1008)
        return

    def check():
        if direct.check_wda_device(lease, store, hub) is not host:
            raise HTTPException(503, "Web Inspector 宿主通道已重新连接")

    await connect_native(ws, direct, lease, host, check, lambda: direct.activity(lease, store, hub), webkit=True)


@router.post(BASE + "/pair", include_in_schema=False)
@router.post(BASE + "/access", include_in_schema=False)
@router.post(BASE + "/access/instance-key", include_in_schema=False)
async def retired_access():
    raise HTTPException(410, "请刷新「AI 直连」页面，可直接复制接入说明。")


def device_host(dev, hub):
    if dev is None or dev.platform not in {"android", "ios", "ios_sim", "harmony"} or dev.status != "online":
        return None
    agent = hub.agent_id_for_serial(dev.serial)
    if agent != dev.agent_id or not agent:
        return None
    host_id = hub.agent_host_id(agent)
    host = hub.agent_native_channel(agent) if host_id else None
    extra = hub.get_device_extra(dev.serial)
    ready = extra.get("readiness") or {}
    if not host or ready.get("ready") is not True:
        return None
    if dev.platform == "android":
        if is_network_adb(dev.serial) and not hardware_serial(extra.get("adb_hardware_serial")):
            return None
        transport = host.devices.get(dev.serial)
    elif dev.platform == "harmony":
        transport = host.hdc_devices.get(dev.serial)
    else:
        transport = host.wda_transport(dev.serial, simulator=dev.platform == "ios_sim")
    if transport is None:
        return None
    return host_id, host


def device_agent(dev, hub):
    if dev is None:
        return None
    if dev.platform == "ios_sim" and not unified.simulator_instance(dev.serial, "ios", hub.get_device_extra(dev.serial)):
        return None
    return hub.agent_id_for_serial(dev.serial)


@router.get(BASE + "/devices")
async def devices(response: Response, session: AsyncSession = DBSession,
                  store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep):
    response.headers.update(NO_CACHE)
    rows = await session.scalars(select(Device).where(Device.platform.in_(("android", "ios", "ios_sim", "harmony")), Device.status == "online"))
    entries = []
    for dev in rows:
        if not device_host(dev, hub) or not device_agent(dev, hub):
            continue
        row = {"serial": dev.serial, "model": dev.model, "platform": unified.public_platform(dev.platform),
               "status": "busy" if store.peek(dev.serial) else "available"}
        if dev.platform == "android":
            extra = hub.get_device_extra(dev.serial)
            virtual = is_local_emulator(dev.serial) or extra.get("device_kind") == "virtual"
            row["device_kind"] = "virtual" if virtual else "physical"
        elif dev.platform in {"ios", "ios_sim"}:
            row["device_kind"] = "virtual" if dev.platform == "ios_sim" else "physical"
        elif dev.platform == "harmony":
            row["device_kind"] = ("virtual" if unified.simulator_instance(
                dev.serial, "harmony", hub.get_device_extra(dev.serial)) else "physical")
        entries.append(row)
    return entries


class Claim(BaseModel):
    serial: str = Field(min_length=1, max_length=256)


async def bind_channels(dev, owner, agent, hub):
    _, host = owner
    if dev.platform == "harmony":
        transport = host.hdc_devices.get(dev.serial)
        if transport is None or device_host(dev, hub) != owner:
            raise HTTPException(409, "鸿蒙设备的 HDC 连接已变化，请重新查询")
        instance = unified.simulator_instance(dev.serial, "harmony", hub.get_device_extra(dev.serial))
        reply = await host.exchange(dev.serial, transport, 5, hdc_bind=True)
        binding = reply.get("binding")
        if not hdc.valid_identity(dev.serial, binding):
            raise HTTPException(502, "鸿蒙设备未返回有效的 HDC 连接标识")
        if (device_host(dev, hub) != owner or device_agent(dev, hub) != agent
                or host.hdc_devices.get(dev.serial) != transport
                or unified.simulator_instance(dev.serial, "harmony", hub.get_device_extra(dev.serial)) != instance):
            raise HTTPException(409, "鸿蒙设备的 HDC 连接已变化，请重新查询")
        actions = ()
        available = reply.get("action_types")
        if (isinstance(available, list) and "observe" in available and "tap" in available
                and unified.agent_for_device(dev, hub) == agent):
            actions = tuple(action for action in (*unified.READS, *unified.MUTATIONS) if action in available)
        return unified.Target(agent, "harmony", actions, transport=transport, binding=binding, instance_id=instance)

    if dev.platform in {"ios", "ios_sim"}:
        simulator = dev.platform == "ios_sim"
        instance = unified.simulator_instance(dev.serial, "ios", hub.get_device_extra(dev.serial)) if simulator else None
        transport = host.wda_transport(dev.serial, simulator=simulator)
        if transport is None:
            raise HTTPException(409, "iOS 原生连接已变化，请重新查询设备")
        status, raw = await host.call_wda(dev.serial, transport, "GET", "/status", b"", budget=5)
        try:
            details = json.loads(raw)["value"]
            ready = details["ready"] is True and (not simulator or bool(details["ios"]["simulatorVersion"]))
        except (KeyError, TypeError, ValueError):
            ready = False
        if status != 200 or not ready:
            raise HTTPException(503, "本次设备的 WDA 尚未就绪")
        if (device_host(dev, hub) != owner or device_agent(dev, hub) != agent
                or host.wda_transport(dev.serial, simulator=simulator) != transport
                or (simulator and unified.simulator_instance(dev.serial, "ios", hub.get_device_extra(dev.serial)) != instance)):
            raise HTTPException(409, "iOS 原生连接已变化，请重新查询设备")
        actions = ()
        binding = None
        if unified.agent_for_device(dev, hub) == agent:
            actions = (*unified.READS, *unified.MUTATIONS)
            if simulator:
                binding = transport
        return unified.Target(agent, "ios", actions, transport=transport, binding=binding, instance_id=instance)

    transport = host.devices.get(dev.serial)
    if transport is None or device_host(dev, hub) != owner or device_agent(dev, hub) != agent:
        raise HTTPException(409, "原生设备连接已变化，请重新查询设备")
    actions = ()
    if unified.agent_for_device(dev, hub) == agent:
        actions = ((*unified.READS, *unified.MUTATIONS) if hub.agent_supports(agent, P.CAP_ANDROID_OBSERVATION)
                   else ("screenshot", "tap"))
    return unified.Target(agent, "android", actions, transport=transport)


def session_channels(ident: str, target: unified.Target) -> dict:
    path = f"{BASE}/sessions/{ident}"
    if target.platform == "ios":
        native = {"protocol": "wda-http", "url": path + "/wda", "web_inspector": {
            "url": f"/ws/ai-connect/sessions/{ident}/webkit", "client": BASE + "/webkit-connect.pyz",
        }}
    elif target.platform == "harmony":
        protocol = "hdc-connect" if target.instance_id else "hdc-server"
        native = {"protocol": protocol, "url": f"/ws/ai-connect/sessions/{ident}/hdc",
                  "client": BASE + "/hdc-connect.pyz"}
    else:
        native = {"protocol": "adb-connect", "url": f"/ws/ai-connect/sessions/{ident}/adb",
                  "client": BASE + "/adb-connect.pyz"}
    endpoints = {}
    if "observe" in target.action_types:
        endpoints.update(observe=path + "/observe", act=path + "/act")
    if "screenshot" in target.action_types:
        endpoints["screenshot"] = path + "/screen"
    if "tap" in target.action_types:
        endpoints["tap"] = path + "/tap"
    return {
        "session_id": ident,
        "native": native,
        "api": endpoints,
        "action_types": [action for action in target.action_types if action in unified.MUTATIONS],
    }


@router.post(BASE + "/sessions", status_code=201)
async def claim(body: Claim, request: Request, response: Response, session: AsyncSession = DBSession,
                store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep):
    direct = state(request.app)
    dev = await session.get(Device, body.serial)
    owner = device_host(dev, hub)
    agent = device_agent(dev, hub)
    if not owner or not agent:
        raise HTTPException(409, "此设备的原生直连尚未就绪")
    now = time.monotonic()
    direct.leases = {k: v for k, v in direct.leases.items() if v.expires > now or v.running or v.tunnels}
    ident = secrets.token_urlsafe(18)
    try:
        lock = await store.acquire(dev.serial, "ai-direct-" + ident, "session", ttl_seconds=LEASE_SECONDS)
    except LockConflict as exc:
        raise HTTPException(409, "设备正在使用中") from exc
    try:
        target = await bind_channels(dev, owner, agent, hub)
        if store.peek(dev.serial) is not lock:
            raise HTTPException(409, "设备占用已失效，请重新领取")
    except BaseException:
        if store.peek(dev.serial) is lock:
            await store.release(dev.serial, lock.token)
        raise
    lease = Lease(dev.serial, owner[0], target.transport, lock.token, lock.last_heartbeat_at + LEASE_SECONDS,
                  unified_target=target, connection_info=session_channels(ident, target))
    direct.leases[ident] = lease
    path = f"{BASE}/sessions/{ident}"
    response.headers.update(NO_CACHE)
    return {"serial": dev.serial, "platform": target.platform, "token": lease.secret,
            "expires_in": LEASE_SECONDS, "session": path, "release": path, **lease.connection_info}


@router.get(BASE + "/sessions/{ident}")
async def session_status(ident: str, request: Request, response: Response,
                         store: DeviceLockStore = LockStoreDep):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    direct.check_lease(lease, store)
    response.headers.update(NO_CACHE)
    return {"serial": lease.serial, "state": "active", "idle_timeout_seconds": LEASE_SECONDS,
            "remaining_seconds": max(0, int(lease.expires - time.monotonic())), **lease.connection_info}


async def forward_wda(host, lease, method, path, target, body):
    if method != "POST" or path != "/session":
        status, raw = await host.call_wda(lease.serial, lease.transport, method, target, body)
        return status, raw, lease.wda_session

    # Reuse the existing session for a default attach. An explicit capabilities
    # request must be sent to WDA so the caller's settings actually take effect.
    status, raw = await host.call_wda(lease.serial, lease.transport, "GET", "/status", b"")
    if status >= 300:
        return status, raw, lease.wda_session
    try:
        receipt, value = ios_wda.session_info(raw)
        active_id = receipt.get("sessionId") or value.get("sessionId")
        if value.get("ready") is not True or (active_id and not ios_wda.is_session_id(active_id)):
            raise ValueError("WDA is not ready")
        default = ios_wda.default_session_request(body)
        reused = active_id if default else None
        if reused:
            status, raw = await host.call_wda(lease.serial, lease.transport, "GET", "/session/" + reused, b"")
        else:
            status, raw = await host.call_wda(lease.serial, lease.transport, "POST", "/session",
                                             ios_wda.DEFAULT_SESSION if default else body)
        if status >= 300:
            return status, raw, lease.wda_session
        receipt, value = ios_wda.session_info(raw)
        device_id = value.get("sessionId") or receipt.get("sessionId")
        if not ios_wda.is_session_id(device_id) or (reused and reused != device_id):
            raise ValueError("unexpected WDA session ID")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise HTTPException(502, "WDA 返回了无效的会话信息") from None
    return status, raw, ios_wda.Session(secrets.token_urlsafe(24), device_id)


async def native_wda(ident: str, command: str, request: Request, store: DeviceLockStore = LockStoreDep,
                     hub: Hub = HubDep):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    direct.check_wda_device(lease, store, hub)
    path = "/" + command
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > ios_wda.MAX_REQUEST:
            raise HTTPException(413, "WDA 请求数据过大")
        body.extend(chunk)
    async with lease.gate:
        host = direct.check_wda_device(lease, store, hub)
        session = lease.wda_session
        public_id = session.public_id if session else None
        if not ios_wda.allowed_request(request.method, path, public_id):
            raise HTTPException(409, "该 WDA 操作或会话不属于本次设备占用")
        query = quote(request.url.query, safe="!$&'()*+,;=:@/?%")
        target = (session.target(path) if session else path) + ("?" + query if query else "")
        if len(target) > 2048:
            raise HTTPException(414, "WDA 请求地址过长")
        await direct.renew(lease, store)
        host = direct.check_wda_device(lease, store, hub)
        if session and request.method == "DELETE" and path == f"/session/{public_id}":
            lease.wda_session = None
            return JSONResponse({"value": None}, headers=NO_CACHE)
        if request.method != "GET":
            lease.screen = None
        lease.running = asyncio.create_task(forward_wda(host, lease, request.method, path, target, body))
        try:
            status, raw, next_session = await lease.running
        except asyncio.CancelledError:
            if lease.closing:
                raise HTTPException(410, "本次设备占用已归还") from None
            raise
        finally:
            lease.running = None
        if direct.check_wda_device(lease, store, hub) is not host:
            raise HTTPException(503, "WDA 宿主通道已重新连接")
        lease.wda_session = next_session
        return Response(ios_wda.public_reply(raw, next_session), status_code=status,
                        media_type="application/json", headers=NO_CACHE)


# Register each method separately so generated clients have distinct operation IDs.
for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
    router.add_api_route(BASE + "/sessions/{ident}/wda/{command:path}", native_wda, methods=[method])


async def operate(ident, request, store, hub, service):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    direct.check_device(lease, store, hub)
    async with lease.gate:
        host = direct.check_device(lease, store, hub)
        await direct.activity(lease, store, hub)
        lease.running = asyncio.create_task(host.call(lease, service, COMMAND_SECONDS))
        try:
            while not lease.running.done():
                finished, _ = await asyncio.wait([lease.running], timeout=0.25)
                if not finished and not lease.closing:
                    direct.check_device(lease, store, hub)
            raw = await lease.running
        except asyncio.CancelledError:
            if lease.closing:
                raise HTTPException(410, "本次设备占用已归还")
            raise
        finally:
            if not lease.running.done():
                lease.running.cancel()
                await asyncio.gather(lease.running, return_exceptions=True)
            lease.running = None
        direct.check_device(lease, store, hub)
        return raw


class AdbService(BaseModel):
    service: str = Field(min_length=1, max_length=16384)


@router.post(BASE + "/sessions/{ident}/adb", include_in_schema=False)
async def adb(ident: str, body: AdbService, request: Request,
              store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep):
    if not allowed_service(body.service):
        raise HTTPException(400, "本入口支持非交互 adb shell 和 exec-out")
    raw = await operate(ident, request, store, hub, body.service)
    return Response(raw, media_type="application/octet-stream", headers=NO_CACHE)


@router.get(BASE + "/sessions/{ident}/screenshot", include_in_schema=False)
async def screenshot(ident: str, request: Request, store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep):
    raw = await operate(ident, request, store, hub, "exec:screencap -p")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or not raw.endswith(b"IEND\xaeB\x60\x82"):
        raise HTTPException(502, "设备没有返回完整的 PNG 截图")
    return Response(raw, media_type="image/png", headers=NO_CACHE)


@router.get(BASE + "/sessions/{ident}/screen")
async def unified_screen(ident: str, request: Request, store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    async with lease.gate:
        direct.check_lease(lease, store)
        if not lease.unified_target or "screenshot" not in lease.unified_target.action_types:
            raise HTTPException(409, "本次占用未提供统一操作")
        await direct.renew(lease, store)
        if "observe" in lease.unified_target.action_types:
            lease.screen = None
        result = await unified.screenshot(hub, lease)
        direct.check_lease(lease, store)
        return result


@router.post(BASE + "/sessions/{ident}/tap")
async def unified_tap(ident: str, body: unified.Tap, request: Request, store: DeviceLockStore = LockStoreDep,
                      hub: Hub = HubDep):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    async with lease.gate:
        direct.check_lease(lease, store)
        if not lease.unified_target or "tap" not in lease.unified_target.action_types:
            raise HTTPException(409, "本次占用未提供统一操作")
        screen = unified.tap_screen(lease, body)
        await direct.renew(lease, store)
        try:
            result = await unified.tap(hub, lease, body, screen)
            direct.check_lease(lease, store)
            return result
        finally:
            if "observe" in lease.unified_target.action_types:
                lease.screen = None


def check_observation(direct, lease, store, hub):
    direct.check_lease(lease, store)
    if not lease.unified_target or "observe" not in lease.unified_target.action_types:
        raise HTTPException(409, "本次领取未提供画面观察与操作")
    if lease.unified_target.platform == "ios":
        direct.check_wda_device(lease, store, hub)
    elif lease.unified_target.platform == "harmony":
        direct.check_hdc_device(lease, store, hub)


async def session_frame(direct, lease, store, hub, *, expected_text: str | None = None):
    revision = lease.native_revision
    frame = await android_actions.observe(hub, lease, expected_text=expected_text)
    direct.check_lease(lease, store)
    if lease.native_revision != revision:
        lease.screen = None
        raise HTTPException(409, "取帧期间收到了原生请求，请重新观察当前页")
    return {**frame, **lease.connection_info}


def frame_error(direct, lease, store, error, *, action_status=None):
    # Keep the command receipt when acquisition of the next frame fails, even if the lease just ended.
    body = {"observation": None, "detail": error.detail}
    if action_status:
        body.update(action_status=action_status, changes=None)
        if action_status == "executed":
            body["observation_error"] = error.detail
    try:
        direct.check_lease(lease, store)
    except HTTPException:
        if not action_status:
            raise
    else:
        body.update(lease.connection_info)
    return JSONResponse(body, status_code=200 if action_status == "executed" else error.status_code, headers=NO_CACHE)


@router.get(BASE + "/sessions/{ident}/observe", name="observe_android")
async def observe(ident: str, request: Request, response: Response,
                  store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep,
                  expected_text: str | None = Query(default=None, min_length=1, max_length=160)):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    async with lease.gate:
        check_observation(direct, lease, store, hub)
        await direct.renew(lease, store)
        response.headers.update(NO_CACHE)
        try:
            return await session_frame(direct, lease, store, hub, expected_text=expected_text)
        except HTTPException as exc:
            return frame_error(direct, lease, store, exc)


@router.post(BASE + "/sessions/{ident}/act", name="act_android")
async def act(ident: str, body: android_actions.Action, request: Request, response: Response,
              store: DeviceLockStore = LockStoreDep, hub: Hub = HubDep):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    async with lease.gate:
        check_observation(direct, lease, store, hub)
        if body.action not in lease.unified_target.action_types:
            raise HTTPException(422, "本次设备绑定未提供这一操作")
        if body.expected_text and lease.unified_target.platform != "android":
            raise HTTPException(422, "预期文字目前仅支持 Android")
        previous = lease.screen
        params = android_actions.parameters(body, previous, lease.unified_target.platform)
        await direct.renew(lease, store)
        # A concurrent caller cannot reuse the old target after an input is sent.
        lease.screen = None
        try:
            await unified.operate(hub, lease, body.action, **params)
        except HTTPException as exc:
            if exc.status_code < 500:
                raise
            return frame_error(direct, lease, store, exc, action_status="unknown")

        response.headers.update(NO_CACHE)
        try:
            await asyncio.sleep(0.5)
            direct.check_lease(lease, store)
            current = await session_frame(direct, lease, store, hub, expected_text=body.expected_text)
        except HTTPException as exc:
            # The input already ran. A failed read must never prompt a replay of that input.
            lease.screen = None
            return frame_error(direct, lease, store, exc, action_status="executed")
        delta = changes(previous.observation, lease.screen.observation) if previous and previous.observation is not None else None
        return {"action_status": "executed", "observation": current, "changes": delta}


@router.delete(BASE + "/sessions/{ident}", status_code=204)
async def release(ident: str, request: Request, store: DeviceLockStore = LockStoreDep):
    direct = state(request.app)
    lease = direct.lookup(ident, request)
    if not lease.closing:
        lease.closing = True
        if lease.running:
            lease.running.cancel()
    tunnels = list(lease.tunnels)
    for tunnel in tunnels:
        tunnel.stop.set()
    await asyncio.gather(*(t.closed.wait() for t in tunnels))
    async with lease.gate:
        lease.wda_session = None
        try:
            await store.release(lease.serial, lease.lock_token)
        except BadToken:
            pass  # A different owner already holds the canonical lock.
        direct.leases.pop(ident, None)
    return Response(status_code=204, headers=NO_CACHE)


@router.get(BASE + "/adb-connect.pyz")
async def download_native_client():
    return Response(client_bundle(), media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="adb-connect.pyz"'})


@router.get(BASE + "/hdc-connect.pyz")
async def download_hdc_client():
    return Response(client_bundle(), media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="hdc-connect.pyz"'})


@router.get(BASE + "/webkit-connect.pyz")
async def download_webkit_client():
    return Response(client_bundle(), media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="webkit-connect.pyz"'})


@router.get(BASE + "/adb-client.py", include_in_schema=False)
async def download_client():
    return Response(Path(__file__).with_name("adb_client.py").read_bytes(), media_type="text/x-python",
                    headers={"Content-Disposition": 'attachment; filename="ai-phone-adb.py"'})


@router.get(BASE + "/guide", response_class=Response)
async def guide():
    return Response(
        """AI Phone 移动设备接入指南

接入说明可反复使用。AI 只需访问同一个实例，就能使用接在不同电脑上且原生通道已就绪的设备。以下路径都相对于该实例地址。

首次接入
  即使用户只粘贴接入说明或发来本指南链接，也请先实际查询 GET /api/ai-connect/devices。这一步无需凭证，不占用设备。
  确认收到 HTTP 200 和设备列表后，告诉用户已连接哪个 AI Phone 实例，用编号列出当前可领设备的型号、系统和已知的设备类型。已占用的另报数量。空列表表示暂时没有可直连设备。查询失败时，报告实际连接问题。
  用户只发说明时，选一台 available 的设备做一次看图体验。领取成功后优先 GET api.screenshot 保存设备当前原图，也可从 api.observe 解码 screenshot.data，或用本次领用返回的 native 取图。体验只截图，保持设备当前画面。
  保存图片后立即向 release 发 DELETE，确认 204 后再告诉用户已归还。取图失败也要归还。没有空闲设备时如实说明，等用户决定下一步。
  在回复里直接展示取回的图片，或附上用户能打开的图片文件，并说明设备型号、归还结果和可领设备。简短介绍用机和新建模拟器能力，再问用户想用哪台设备、哪种系统，或想完成什么任务。用户已明确选择设备，或任务中只有一台符合条件时，直接领用并继续该任务。
  需要新模拟器时，可以用 aiphone vm 管理 Android、iOS 和鸿蒙。在具备本实例已有管理权限时，通过 AIPHONE_BASE_URL 临时指定实例，用对应平台的 catalog 和 systems 查询机型与兼容系统。用户明确要创建时，用 create --start --wait 创建并启动，等设备就绪后从同一清单领用。已有设备的查询和领用不依赖 CLI。

查看与领取
  GET /api/ai-connect/devices 查询原生通道已就绪设备的 serial、model、platform、device_kind 和 status。查询无需凭证，也不占用设备。
  选定 available 的设备，以 Content-Type: application/json 向 POST /api/ai-connect/sessions 提交 {"serial":"选中的 serial"}。
  201 表示此次已独占所选设备。保存完整领取结果，session_id 标识这次占用，token 供已交付的入口和归还使用。
  设备清单包含原生通道已就绪的 Android、iOS 和鸿蒙设备。能够确认设备身份的网络 ADB 也可领用。device_kind 为 physical 表示真机，为 virtual 表示模拟器。409 时可重新查看列表。

选择用法
  优先使用原生直连 native。Android 使用 ADB，iOS 使用 WDA，鸿蒙使用 HDC。按文末对应说明操作。
  领取结果提供 api.observe 和 api.act 时，可用它们读取结构和按编号操作，并在同一次占用内切回原生 ADB、WDA 或 HDC。api 为空时直接使用 native。
  每个页面回包都附上同一 session_id、native、api 和 action_types，沿用领取时的 token。已连上的原生通道可继续使用。
  原生操作后要按页面编号操作，先重新 observe，以当前画面为准。

主动式可操作暴露
  带 Authorization: Bearer <token> 请求 GET api.observe。每一帧同时包含 frame_id、structure、targets 和一张 PNG。
  screenshot.data 是 Base64 编码的原尺寸 PNG。有目标时截图按 targets[].id 画框编号，清单为空时返回当前截图。
  可直接读 structure.children 的父子关系、role、文字和 state。节点的 target 引用同帧的 targets[].id。
  targets 列出 id、name、actions、bounds、point 和 state。位置可来自系统控件、图像区域或当前可检查的 WebView。name_source 为 system、screen 或 webview，分别表示名字取自系统、画面或网页结构，读不出时为 null。
  编号只在本帧有效。坐标从截图左上角按原始像素计算。tap 表示系统报告该处可点，try_tap 表示可以按位置尝试。
  system_texts 记录系统文字、位置及关联目标，screen_texts 可为空。keyboard.occlusion_bounds 给出已识别的键盘遮挡范围。
  清单漏掉目标时，可凭本帧截图用坐标操作，也可改走同一次占用的原生通道。
  Android 的 structure_status 为 unavailable 时，本帧未取得可用结构。partial 表示只有部分结构，或已圈出目标但都没有读到名称。available 表示已取得可用结构，页面仍可能有未列出的目标。
  Android 在生成帧前检查可观测的加载信号。readiness 为 no_loading_signal 表示目前未检测到加载信号。若有明确的页面目标，可在 observe 查询参数或 act JSON 中加 expected_text，等当前帧包含该文字。readiness 为 expected_text_present 表示已确认出现该文字。等待超时时重新 observe，已执行的动作无需重发。
  向 POST api.act 发送一个 JSON 动作，带同一会话凭证及 Content-Type: application/json。
  以下动作以本次 action_types 为准，编号、坐标和应用标识换成实际值。
    按目标点     {"action":"tap","frame_id":"当前帧","target":"本帧选中的编号"}
    按原图点     {"action":"tap","frame_id":"当前帧","x":100,"y":200}
    向下翻动     {"action":"scroll","frame_id":"当前帧","direction":"down"}
    填写文字     {"action":"input","text":"要填写的内容"}
    系统返回     {"action":"back"}
    回到桌面     {"action":"home"}
    打开应用     {"action":"open_app","package":"com.example.app"}
  滚动方向可选 up、down、left、right，也可用 target 指定本帧声明支持滚动的区域。
  input 把当前获焦的编辑框内容设为 text，空字符串用于清空。输入和导航也可带 frame_id。

  action_status 为 executed 表示设备已执行动作。observation 是动作后的新帧，格式与 GET api.observe 相同。从这份 observation 取新截图、清单及两个通道入口。
  同级的 changes 给出能确认的文字与状态变化，changes.text_sources 若存在则列出两帧共同用于比较的文字来源。下一步从新 observation 中取目标编号和 frame_id。
  added_target_names 和 removed_target_names 补充系统控件名称的变化。changed_regions 若有值，给出两张原图变化的大致像素范围。
  每张画面的编号和坐标仅用于它所属的会话与画面。409 提示帧过期时，重新观察并选取当前目标。
  若 observation 为 null 且 action_status 为 executed，本次动作已执行。取帧失败时，有效占用的 native 和 api 在回包顶层，可直接改走原生或只重新观察。
  若 action_status 为 unknown，表示未取得可确认的设备回执。请先查看当前画面再决定下一步。

结构观察超时后的看图操作
  若 observe 超时、act 已执行但没有新帧，或清单不够用，可以在同一次占用中切回 native。领取结果提供 api.screenshot 和 api.tap 时，也可以使用下列入口。
  也可带同一 Bearer 凭证 GET api.screenshot，拿原尺寸 PNG 和响应头 X-AI-Phone-Frame-Id。这个入口不等待结构。
  在图上选点，带同一凭证 POST api.tap，Content-Type 为 application/json，发送 {"frame_id":"X-AI-Phone-Frame-Id 的值","x":100,"y":200}。坐标以截图左上角为原点。
  api.tap 返回执行回执。要确认当前画面，再 GET api.screenshot，或者回到 api.observe。旧截图 ID 不能重复点。

占用与归还
  每次原生设备请求或统一操作请求都将空闲时限顺延 30 分钟。
  向 session 字段给出的地址发送 GET，带会话凭证，可读到剩余秒数 remaining_seconds。查询状态不会续期。
  连接中断时，在同一设备通道恢复且占用仍有效的情况下，可以用原领取结果重连。
  用完向 release 字段给出的地址发送 DELETE，带会话凭证。返回 204 后，本次领取提供的设备入口失效。
  下次继续使用这份接入说明。每次成功领取都会得到新的 session_id 和会话凭证。
  403 表示会话凭证不匹配，410 表示本次占用已结束。

原生连接方式 · native.protocol 为 adb-connect
  支持 USB 接入的 Android 真机、本机 Android 模拟器，以及能够确认设备身份的网络 ADB。运行原生工具的电脑需要 Python 3.9+ 和 Android platform-tools。
  将本次领取返回的完整 JSON 保存为仅自己可读的 lease.json。用 GET 从 native.client 给出的地址下载 adb-connect.pyz。
  把 INSTANCE 设为接入说明中的实例地址，运行 python3 adb-connect.pyz "$INSTANCE" lease.json，并保持进程运行。
  连接器在本机执行 adb connect，输出本次连接的 adb_serial。把它赋给 DEVICE，即可运行 adb -s "$DEVICE" shell。
  查看已安装应用的启动入口：adb -s "$DEVICE" shell cmd package query-activities --brief --components -a android.intent.action.MAIN -c android.intent.category.LAUNCHER
  输出中的 package/activity 可以传给 adb -s "$DEVICE" shell am start -n "<package/activity>"。也可以把 package 传给上面的 open_app。
  只有应用显示名时，可用 adb -s "$DEVICE" shell input keyevent KEYCODE_HOME 回桌面，在截图上找应用图标并启动。
  用 observe 的 package 确认前台包名，或用 adb -s "$DEVICE" shell dumpsys activity activities 查 topResumedActivity 或 mResumedActivity。
  原生看图可用 adb -s "$DEVICE" shell -T screencap -p > screen.png，再用 adb -s "$DEVICE" shell input tap <x> <y> 点当前画面。
  文件传输可用 adb push 和 adb pull。adb forward 和 adb reverse 的电脑端连接由客户端 ADB 处理，反向转发随本次连接关闭。

原生连接方式 · native.protocol 为 wda-http
  适用于已开放的 iOS USB 真机和受管 iOS 模拟器。WDA_BASE 是实例地址加 native.url。连接由 AI Phone 路由，无需访问设备宿主机。
  每个请求都带 Authorization: Bearer <本次领取的 token>。先 GET WDA_BASE/status，确认 value.ready。
  POST WDA_BASE/session，Content-Type 为 application/json，请求体为 {"capabilities":{"alwaysMatch":{},"firstMatch":[{}]}}。
  保存返回的 value.sessionId 或 sessionId。这个 ID 由 AI Phone 签发，仅在本次占用有效。需要重建时先 DELETE WDA_BASE/session/<WDA 会话 ID>。
  可以发送 WDA 支持的自定义 capabilities。默认请求会沿用设备已有的 WDA 会话，自定义请求由 WDA 创建设备会话。
  iOS 网页调试使用 native.web_inspector。下载它的 client，运行 python3 webkit-connect.pyz "$INSTANCE" lease.json --web-inspector。输出的本地地址收发 WebKit 原生 plist 数据帧，帧头为 4 字节大端长度。
  iOS 真机需在 Safari 浏览器的高级设置中打开「网页检查器」。模拟器断开后重连时，系统调试服务可能需要约 10 秒重新启动，同一条连接可连续发送调试请求。
  启动应用用 POST WDA_BASE/session/<WDA 会话 ID>/wda/apps/launch，请求体例如 {"bundleId":"com.apple.Preferences"}。
  用 GET WDA_BASE/wda/activeAppInfo 核对前台 App，GET WDA_BASE/screenshot 返回 value 中的 Base64 PNG。
  读取 GET WDA_BASE/session/<WDA 会话 ID>/window/size，再用 POST WDA_BASE/session/<WDA 会话 ID>/wda/tap 发送 {"x":100,"y":200}。WDA 坐标以逻辑点为单位，按截图原尺寸与窗口尺寸换算。用新截图确认实际变化。
  从原生 WDA 操作切回编号清单时，先 GET api.observe 取得新帧。主动观察和编号操作沿用原有 WDA 会话，切回原生时可以继续使用同一 WDA 会话 ID。
  iPhone Duo 展开时，如果默认前台报桌面，先从会话的 /wda/apps/list 核对目标 App。用 GET /appium/settings 读取原 defaultActiveApplication，再向该会话路径 POST {"settings":{"defaultActiveApplication":"<Bundle ID>"}}。用会话的 /source 和 /element 定位，POST /element/<元素 ID>/click。归还前恢复原设置。
  通过领取结果的 release 归还，返回 204 后，原 WDA 入口和原凭证失效。受管模拟器关机或重启后，需要重新领取。

鸿蒙模拟器原生直连（native.protocol=hdc-connect）
  本机准备 DevEco 的 hdc 并加入 PATH。从 native.client 下载 hdc-connect.pyz。
  运行 python3 hdc-connect.pyz "<实例地址>" lease.json，保持进程运行。使用输出的 hdc_serial。
  例如用 hdc -t "<hdc_serial>" shell bm dump -a 列应用，用 hdc -t "<hdc_serial>" shell aa start -b "<bundle>" -a "<ability>" 启动应用。可用 snapshot_display 截图，再用 hdc file recv 读取。
  领取回包带 api.observe 和 api.act 时，可在同一次领用内切换 HDC 和编号操作。从 HDC 切回编号清单时重新观察。
  归还后 HDC 通道关闭，连接器移除这次建立的本地目标。

鸿蒙真机原生直连（native.protocol=hdc-server）
  同样从 native.client 下载并运行 hdc-connect.pyz。使用输出的 hdc_server。
  运行 hdc -p -s "<hdc_server>" list targets 查看本次领用的设备，或运行 hdc -p -s "<hdc_server>" shell 使用设备命令。
  真机可用 hdc fport、rport、reconnect。端口转发跟随本次连接关闭。
  sideload、update、flash 从运行连接器的电脑读取固件文件。命令中可用本机绝对路径，单个文件上限 32 GiB，需有足够的 Agent 暂存空间。
  领取回包带 api.observe 和 api.act 时，可在同次领用内切换。归还后本次 HDC 地址失效。

""", media_type="text/plain")
