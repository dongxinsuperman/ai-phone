"""Carry WDA HTTP to an explicitly exposed iOS device."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

import h11

from .android import MAX_OUTPUT

DEVICE_PORT = 8100
MAX_REQUEST = 64 * 1024
_UDID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{8}-[0-9a-f]{16})", re.I)
_PATH = re.compile(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ROOT_READS = {"/status", "/screenshot", "/source", "/wda/activeAppInfo", "/wda/screen"}


def is_udid(value):
    return isinstance(value, str) and bool(_UDID.fullmatch(value))


def is_simulator_udid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except ValueError:
        return False


def is_boot_identity(value):
    return isinstance(value, str) and bool(re.fullmatch("[0-9a-f]{64}", value))


def is_session_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


@dataclass(frozen=True)
class Session:
    public_id: str
    device_id: str

    def target(self, path):
        prefix = "/session/" + self.public_id
        if path == prefix or path.startswith(prefix + "/"):
            return "/session/" + self.device_id + path[len(prefix):]
        return path


def session_info(raw):
    receipt = json.loads(raw)
    value = receipt["value"]
    if not isinstance(value, dict):
        raise ValueError("invalid WDA session response")
    return receipt, value


def default_session_request(body):
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return False
    if not isinstance(data, dict) or data.keys() - {"capabilities"}:
        return False
    caps = data.get("capabilities", {})
    return (isinstance(caps, dict) and not (caps.keys() - {"alwaysMatch", "firstMatch"})
            and caps.get("alwaysMatch", {}) == {} and caps.get("firstMatch", [{}]) == [{}])


def public_reply(raw, session):
    # WDA includes the device session ID in the envelope of ordinary command replies.
    try:
        receipt = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(receipt, dict):
        return raw
    changed = False
    for record in (receipt, receipt.get("value")):
        if isinstance(record, dict) and "sessionId" in record:
            if session and record["sessionId"] == session.device_id:
                record["sessionId"] = session.public_id
            else:
                del record["sessionId"]
            changed = True
    return json.dumps(receipt, ensure_ascii=False).encode() if changed else raw


def allowed_request(method, path, session_id):
    if not _PATH.fullmatch(path) or len(path) > 512:
        return False
    if method == "GET" and path in _ROOT_READS:
        return True
    if method == "POST" and path == "/session":
        return session_id is None
    if method not in {"GET", "POST", "DELETE"} or not is_session_id(session_id):
        return False
    root = "/session/" + session_id
    return path.startswith(root + "/") or (path == root and method in {"GET", "DELETE"})


async def usb_devices(udids):
    from pymobiledevice3 import usbmux

    return {device.serial: device for device in await usbmux.list_devices()
            if device.is_usb and device.serial in udids and device.devid > 0}


def simulator_endpoints(udids, deadline):
    from ai_phone.agent.drivers.ios_simulator_driver import get_sim_endpoint
    from ai_phone.agent.ios_sim_observation import boot_identity
    from ai_phone.agent.mobile_sim_errors import MobileSimError

    endpoints = {}
    for serial in sorted(udids):
        endpoint = get_sim_endpoint(serial)
        if endpoint is None or time.time() >= deadline:
            continue
        port = endpoint.wda_port
        try:
            generation = boot_identity(serial, port, deadline)
        except MobileSimError:
            continue
        endpoints[serial] = (port, generation)
    return endpoints


async def usb_stream(device):
    socket = await device.connect(DEVICE_PORT)
    try:
        socket.setblocking(False)
        return await asyncio.open_connection(sock=socket)
    except BaseException:
        socket.close()
        raise


async def simulator_stream(serial, port, generation, deadline):
    from ai_phone.agent.ios_sim_observation import boot_identity

    # Verify the actual listener once connected, before sending bytes to the port.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        if await asyncio.to_thread(boot_identity, serial, port, deadline) != generation:
            raise ValueError("iOS 模拟器已重启，请重新领取")
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def request(open_stream, method, target, encoded_body):
    if not isinstance(target, str) or not target.startswith("/") or len(target) > 2048 \
            or any(not 33 <= ord(ch) <= 126 for ch in target):
        raise ValueError("WDA 请求地址无效")
    if method not in {"GET", "POST", "DELETE"}:
        raise ValueError("不支持的 WDA 请求方法")
    body = base64.b64decode(encoded_body, validate=True)
    if len(body) > MAX_REQUEST:
        raise ValueError("WDA 请求数据过大")
    reader, writer = await open_stream()
    try:
        wire = h11.Connection(h11.CLIENT)
        headers = [(b"host", b"127.0.0.1:8100"), (b"connection", b"close"),
                   (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        writer.write(wire.send(h11.Request(method=method, target=target, headers=headers)))
        if body:
            writer.write(wire.send(h11.Data(data=body)))
        writer.write(wire.send(h11.EndOfMessage()))
        await writer.drain()
        status = None
        data = bytearray()
        while True:
            event = wire.next_event()
            if event is h11.NEED_DATA:
                wire.receive_data(await reader.read(65536))
            elif isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                data.extend(event.data)
                if len(data) > MAX_OUTPUT:
                    raise ValueError("WDA 返回数据过大")
            elif isinstance(event, h11.EndOfMessage) and status is not None:
                return {"status": status, "data": base64.b64encode(data).decode("ascii")}
            elif isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
                raise ValueError("WDA 返回不完整")
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
