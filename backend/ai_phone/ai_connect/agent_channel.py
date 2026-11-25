"""Native requests and inventory on one authenticated Agent WebSocket."""
from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from contextlib import suppress

from fastapi import HTTPException, WebSocketDisconnect

from ai_phone.shared import protocol as P

from . import ios_wda
from .android import COMMAND_SECONDS, MAX_OUTPUT


def inventory(message, field, valid):
    entries = message.get(field)
    return {k: v for k, v in entries.items() if valid(k, v)} if isinstance(entries, dict) else {}


class NativeAgentChannel:
    def __init__(self, socket):
        self.socket = socket
        self.closed = False
        self.devices = {}
        self.wda_devices = {}
        self.wda_simulators = {}
        self.pending = {}

    def update(self, message):
        self.devices = inventory(message, "devices", lambda serial, transport:
                                 isinstance(serial, str) and type(transport) is int and transport > 0)
        self.wda_devices = inventory(message, "wda_devices", lambda serial, transport:
                                     ios_wda.is_udid(serial) and type(transport) is int and transport > 0)
        self.wda_simulators = inventory(message, "wda_simulators", lambda serial, transport:
                                        ios_wda.is_simulator_udid(serial) and ios_wda.is_boot_identity(transport))

    async def send(self, **message):
        if self.closed:
            raise RuntimeError("Agent 通道已断开")
        await self.socket.send_json({"type": P.MSG_AI_DIRECT_REQUEST, **message})

    def resolve(self, message):
        ident = message.get("id")
        wait = self.pending.get(ident) if isinstance(ident, str) else None
        if wait and not wait.done():
            wait.set_result(message)

    def disconnect(self):
        self.closed = True
        for wait in self.pending.values():
            if not wait.done():
                wait.set_result({"error": "设备 Agent 已离线，动作结果未知"})

    async def exchange(self, serial, transport, budget, **fields):
        ident = secrets.token_hex(16)
        done = asyncio.get_running_loop().create_future()
        self.pending[ident] = done
        try:
            try:
                async with asyncio.timeout(budget + 1):
                    await self.send(id=ident, serial=serial, transport=transport, budget=budget, **fields)
                    reply = await asyncio.shield(done)
            except (TimeoutError, asyncio.CancelledError):
                with suppress(TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
                    async with asyncio.timeout(3):
                        await self.send(id=ident, cancel=True)
                        await asyncio.shield(done)
                raise
        except (OSError, RuntimeError, WebSocketDisconnect, TimeoutError) as exc:
            raise HTTPException(503, "设备通道已中断或超时，动作结果未知") from exc
        finally:
            self.pending.pop(ident, None)
        if self.closed:
            raise HTTPException(503, "设备 Agent 已离线，动作结果未知")
        if "error" in reply:
            raise HTTPException(502, str(reply["error"])[:300])
        return reply

    @staticmethod
    def decode(reply):
        try:
            raw = base64.b64decode(reply["data"], validate=True)
        except (KeyError, ValueError, binascii.Error) as exc:
            raise HTTPException(502, "设备返回数据无效") from exc
        if len(raw) > MAX_OUTPUT:
            raise HTTPException(502, "设备返回数据过大")
        return raw

    async def call(self, lease, service, budget):
        return self.decode(await self.exchange(lease.serial, lease.transport, budget, service=service))

    async def call_wda(self, serial, transport, method, target, body, budget=COMMAND_SECONDS):
        reply = await self.exchange(serial, transport, budget, wda={
            "method": method, "target": target, "encoded_body": base64.b64encode(body).decode("ascii"),
        })
        status = reply.get("status")
        if type(status) is not int or not 200 <= status <= 599:
            raise HTTPException(502, "设备返回了无效的 WDA 状态")
        return status, self.decode(reply)

    def wda_transport(self, serial, simulator):
        inventory = self.wda_simulators if simulator else self.wda_devices
        return inventory.get(serial)
