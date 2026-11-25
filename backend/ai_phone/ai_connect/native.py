"""Bridge the ADB wire protocol to streams on one authorized local transport."""
from __future__ import annotations

import asyncio
import itertools
import struct
from contextlib import suppress
from dataclasses import dataclass, field

from websockets.exceptions import ConnectionClosed

from .android import adb_connection, device_connection, local_inventory, read_string, request


HEADER = struct.Struct("<4s5I")
MAX_PACKET = 1024 * 1024
CHUNK = 64 * 1024


async def packets(ws):
    """WebSocket messages carry arbitrary pieces of the original ADB byte stream."""
    buf = bytearray()
    async for chunk in ws:
        if not isinstance(chunk, bytes):
            raise ValueError("原生通道需要二进制数据")
        buf.extend(chunk)
        while len(buf) >= HEADER.size:
            name, arg0, arg1, size, _, magic = HEADER.unpack_from(buf)
            if size > MAX_PACKET or magic != int.from_bytes(name, "little") ^ 0xffffffff:
                raise ValueError("无效的 ADB 数据帧")
            end = HEADER.size + size
            if len(buf) < end:
                break
            data = bytes(buf[HEADER.size:end])
            del buf[:end]
            yield name, arg0, arg1, data


@dataclass
class Stream:
    local: int
    remote: int
    ack: asyncio.Event = field(default_factory=asyncio.Event)
    writer: asyncio.StreamWriter | None = None
    task: asyncio.Task | None = None


class DeviceTransport:
    def __init__(self, ws, serial, transport):
        self.ws = ws
        self.serial = serial
        self.transport = transport
        self.ids = itertools.count(1)
        self.streams = {}
        self.max_payload = CHUNK
        self.version = 0

    async def send(self, name, arg0, arg1, data=b""):
        magic = int.from_bytes(name, "little") ^ 0xffffffff
        checksum = 0 if self.version >= 0x01000001 else sum(data) & 0xffffffff
        await self.ws.send(HEADER.pack(name, arg0, arg1, len(data), checksum, magic) + data)

    async def handshake(self, version, limit):
        if self.version or version < 0x01000000 or limit < 4096:
            raise ValueError("无效的 ADB 握手")
        self.version = min(version, 0x01000001)
        self.max_payload = min(limit, CHUNK)
        props = (await local_inventory({self.serial})).get(self.serial, {})
        if props.get("transport_id") != str(self.transport):
            raise ValueError("本次占用的 ADB 连接已变化")
        banner = "".join(f"ro.product.{prop}={props[key]};"
                         for prop, key in (("name", "product"), ("model", "model"), ("device", "device"))
                         if key in props)
        async with asyncio.timeout(5), adb_connection() as (reader, writer):
            await request(reader, writer, f"host-serial:{self.serial}:features")
            # Device services pass through. The bridge uses one acknowledged packet per stream.
            features = [f for f in (await read_string(reader)).split(b",") if f != b"delayed_ack"]
        await self.send(b"CNXN", self.version, self.max_payload,
                        b"device::" + banner.encode() + b"features=" + b",".join(features) + b";\0")

    async def service(self, stream, name):
        opened = False
        try:
            # The host smart socket also understands host administration and device
            # selection. Reverse bindings target that host and would outlive the stream.
            if not name or name.startswith((b"host", b"reverse:")):
                return
            async with device_connection(self.serial, self.transport) as (reader, writer):
                await request(reader, writer, name)
                stream.writer = writer
                await self.ws.send("request")
                await self.send(b"OKAY", stream.local, stream.remote)
                opened = True
                while data := await reader.read(self.max_payload):
                    stream.ack.clear()
                    await self.send(b"WRTE", stream.local, stream.remote, data)
                    await stream.ack.wait()
        except (OSError, ValueError, asyncio.IncompleteReadError, ConnectionClosed):
            pass
        finally:
            self.streams.pop(stream.local, None)
            with suppress(OSError, ConnectionClosed):
                await self.send(b"CLSE", stream.local if opened else 0, stream.remote)

    async def run(self):
        try:
            async for name, remote, local, data in packets(self.ws):
                if name == b"CNXN":
                    await self.handshake(remote, local)
                elif not self.version:
                    raise ValueError("需要先建立 ADB 连接")
                elif name == b"OPEN":
                    stream = Stream(next(self.ids), remote)
                    self.streams[stream.local] = stream
                    stream.task = asyncio.create_task(self.service(stream, data.removesuffix(b"\0")))
                else:
                    stream = self.streams.get(local)
                    if stream is None or stream.remote != remote:
                        continue
                    if name == b"CLSE":
                        stream.task.cancel()
                    elif name == b"OKAY":
                        stream.ack.set()
                    elif name == b"WRTE" and stream.writer:
                        stream.writer.write(data)
                        await stream.writer.drain()
                        if data:
                            await self.ws.send("request")
                        await self.send(b"OKAY", local, remote)
        finally:
            tasks = [stream.task for stream in self.streams.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
