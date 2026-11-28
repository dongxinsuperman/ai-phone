"""Bridge the ADB wire protocol to streams on one authorized local transport."""
from __future__ import annotations

import asyncio
import itertools
import struct
from contextlib import suppress
from dataclasses import dataclass, field
from secrets import randbelow

from websockets.exceptions import ConnectionClosed

from .activity import RENEW_ACTIVITY, WRITE_ACTIVITY
from .android import adb_connection, device_connection, is_read_only_service, local_inventory, read_string, request


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
    # The ADB handshake omits delayed_ack, so the peer waits after each write.
    incoming: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    writer: asyncio.StreamWriter | None = None
    task: asyncio.Task | None = None
    activity: str = WRITE_ACTIVITY


@dataclass
class ReverseRule:
    destination: bytes
    listener: asyncio.AbstractServer | None = None


def adb_string(data):
    return f"{len(data):04x}".encode() + data


class DeviceTransport:
    def __init__(self, ws, serial, transport, *, renew_activity=False):
        self.ws = ws
        self.serial = serial
        self.transport = transport
        self.renew_activity = renew_activity
        self.ids = itertools.count(1)
        self.streams = {}
        self.reverse_rules: dict[bytes, ReverseRule] = {}
        self.reverse_gate = asyncio.Lock()
        self.closing = False
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
            if not name or b"\0" in name or name.startswith(b"host"):
                return
            if self.renew_activity and is_read_only_service(name):
                stream.activity = RENEW_ACTIVITY
            if name.startswith(b"reverse:"):
                await self.ws.send(stream.activity)
                await self.send(b"OKAY", stream.local, stream.remote)
                opened = True
                try:
                    async with self.reverse_gate:
                        result = await self.reverse(name.removeprefix(b"reverse:"))
                except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
                    result = b"FAIL" + adb_string(str(exc).encode()[:1024])
                await self.send(b"WRTE", stream.local, stream.remote, result)
                await stream.ack.wait()
                return
            async with device_connection(self.serial, self.transport) as (reader, writer):
                await self.ws.send(stream.activity)
                await request(reader, writer, name)
                stream.writer = writer
                await self.send(b"OKAY", stream.local, stream.remote)
                opened = True
                await self.exchange_stream(stream, reader)
        except (OSError, ValueError, asyncio.IncompleteReadError, ConnectionClosed):
            pass
        finally:
            self.streams.pop(stream.local, None)
            with suppress(OSError, ConnectionClosed):
                await self.send(b"CLSE", stream.local if opened else 0, stream.remote)

    async def download(self, stream, reader):
        while data := await reader.read(self.max_payload):
            stream.ack.clear()
            await self.ws.send(stream.activity)
            await self.send(b"WRTE", stream.local, stream.remote, data)
            await stream.ack.wait()

    async def upload(self, stream):
        while True:
            data = await stream.incoming.get()
            if data:
                await self.ws.send(stream.activity)
            stream.writer.write(data)
            try:
                await stream.writer.drain()
            except asyncio.CancelledError:
                # Closing normally would wait for the blocked socket to flush.
                stream.writer.transport.abort()
                raise
            await self.send(b"OKAY", stream.local, stream.remote)

    async def exchange_stream(self, stream, reader):
        tasks = [asyncio.create_task(self.upload(stream)), asyncio.create_task(self.download(stream, reader))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def accept_reverse(self, rule, reader, writer):
        if self.closing:
            writer.close()
            return
        stream = Stream(next(self.ids), 0, writer=writer, task=asyncio.current_task())
        self.streams[stream.local] = stream
        try:
            # This OPEN is handled by the caller's own ADB server.
            await self.send(b"OPEN", stream.local, 0, rule.destination + b"\0")
            await asyncio.wait_for(stream.ack.wait(), 5)
            await self.exchange_stream(stream, reader)
        except (OSError, ConnectionClosed):
            pass
        finally:
            self.streams.pop(stream.local, None)
            writer.close()
            with suppress(OSError, ConnectionClosed):
                await writer.wait_closed()
                if stream.remote:
                    await self.send(b"CLSE", stream.local, stream.remote)

    async def reverse_rpc(self, command):
        async with asyncio.timeout(4), device_connection(self.serial, self.transport) as (reader, writer):
            await request(reader, writer, b"reverse:" + command)
            status = await reader.readexactly(4)
            if status == b"FAIL":
                raise ValueError((await read_string(reader)).decode(errors="replace"))
            if status != b"OKAY":
                raise ValueError("ADB reverse returned an invalid response")
            try:
                prefix = await reader.readexactly(4)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise
                return b""
            size = int(prefix, 16)
            if not 1 <= size <= 5:
                raise ValueError("ADB reverse returned an invalid port")
            return prefix + await reader.readexactly(size)

    async def remove_reverse(self, source):
        rule = self.reverse_rules[source]
        await self.reverse_rpc(b"killforward:" + source)
        self.reverse_rules.pop(source)
        rule.listener.close()
        await rule.listener.wait_closed()

    async def reverse(self, command):
        if command == b"list-forward":
            return adb_string(b"".join(self.serial.encode() + b" " + source + b" " + rule.destination + b"\n"
                                      for source, rule in self.reverse_rules.items()))
        if command == b"killforward-all":
            for source in list(self.reverse_rules):
                await self.remove_reverse(source)
            return b"OKAY"
        if command.startswith(b"killforward:"):
            source = command.removeprefix(b"killforward:")
            if source not in self.reverse_rules:
                raise ValueError("ADB reverse listener does not belong to this connection")
            await self.remove_reverse(source)
            return b"OKAY"
        if not command.startswith(b"forward:"):
            raise ValueError("Invalid ADB reverse command")
        spec = command.removeprefix(b"forward:")
        unbound = spec.removeprefix(b"norebind:")
        source, delimiter, destination = unbound.partition(b";")
        if not delimiter or not source or not destination or b";" in destination or destination.startswith(b"*"):
            raise ValueError("Invalid ADB reverse endpoints")
        if (rule := self.reverse_rules.get(source)) is not None:
            if spec != unbound:
                raise ValueError("ADB reverse listener already exists")
            rule.destination = destination
            return b"OKAY"

        rule = ReverseRule(destination)
        rule.listener = await asyncio.start_server(lambda r, w: self.accept_reverse(rule, r, w), "127.0.0.1", 0)
        port = rule.listener.sockets[0].getsockname()[1]
        setup = asyncio.create_task(self.install_reverse(source, rule, port))
        try:
            try:
                assigned = await asyncio.shield(setup)
            except asyncio.CancelledError:
                await asyncio.gather(setup, return_exceptions=True)
                raise
            return b"OKAY" + assigned
        finally:
            if rule not in self.reverse_rules.values():
                rule.listener.close()
                await rule.listener.wait_closed()

    async def install_reverse(self, source, rule, port):
        allocate = source == b"tcp:0"
        # Host ADB tracks the requested source. A concrete port gives setup and removal the same key.
        for _ in range(8 if allocate else 1):
            if allocate:
                source = f"tcp:{49152 + randbelow(16384)}".encode()
                if source in self.reverse_rules:
                    continue
            try:
                assigned = await self.reverse_rpc(b"forward:norebind:" + source + f";tcp:{port}".encode())
            except ValueError as exc:
                if allocate and ("address already in use" in str(exc).lower()
                                 or "cannot rebind existing socket" in str(exc)):
                    continue
                raise
            self.reverse_rules[source] = rule
            return adb_string(source[4:]) if allocate else assigned
        raise ValueError("ADB could not allocate an unused device port")

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
                    if stream is None or stream.remote not in (0, remote):
                        continue
                    if name == b"CLSE":
                        stream.task.cancel()
                    elif name == b"OKAY":
                        if not stream.remote:
                            stream.remote = remote
                        stream.ack.set()
                    elif name == b"WRTE" and stream.writer:
                        try:
                            stream.incoming.put_nowait(data)
                        except asyncio.QueueFull:
                            stream.task.cancel()
        finally:
            self.closing = True
            for rule in self.reverse_rules.values():
                rule.listener.close()
            tasks = [stream.task for stream in self.streams.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.streams.clear()
            for rule in self.reverse_rules.values():
                rule.listener.close()
            await asyncio.gather(*(self.remove_reverse(source) for source in list(self.reverse_rules)),
                                 return_exceptions=True)
