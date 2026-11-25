"""Carry the TCP side of HDC port forwarding across a single leased tunnel."""
import asyncio
import re
import shlex
import struct
from contextlib import suppress


PORT_REQUEST = b"AI-PHONE HDC PORT "
PORT_READY = b"ready"
PORT_ERROR = b"error "
FIRMWARE_REQUEST = b"AI-PHONE HDC FILE "
FIRMWARE_COMMANDS = (b"sideload ", b"update ", b"flash ")
FRAME_LENGTH = struct.Struct("!I")
_STREAM_HEADER = struct.Struct("!cI")


def frame(data):
    return FRAME_LENGTH.pack(len(data)) + data


def arguments(command):
    return shlex.split(command.rstrip(b"\0").decode("utf-8"))


def port_rule(parts):
    if len(parts) != 3 or parts[0] not in ("fport", "rport"):
        raise ValueError("Incorrect forward command")
    direction, first, second = parts
    caller, device = (first, second) if direction == "fport" else (second, first)
    kind, _, port = caller.partition(":")
    if kind != "tcp" or not port.isascii() or not port.isdecimal():
        raise ValueError("The computer endpoint must be tcp:<port>")
    if len(port) > 5 or not 1 <= int(port) <= 65535:
        raise ValueError("Incorrect TCP port")
    kind, separator, endpoint = device.partition(":")
    if not separator or not kind or not endpoint or any(c.isspace() or c in '\0\"\'' for c in device):
        raise ValueError("Incorrect device endpoint")
    return direction, int(port), device


def firmware_file_index(parts):
    if len(parts) == 2 and parts[0] in ("sideload", "update"):
        return 1
    if parts[:1] == ["flash"] and len(parts) in (3, 4):
        offset = int(len(parts) == 4 and parts[1] == "-f")
        if len(parts) == 3 + offset and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1 + offset]):
            return 2 + offset
    raise ValueError("Invalid HDC firmware command")


class PortStreams:
    def __init__(self, peer, destination=None, activity=None):
        self.peer = peer
        self.destination = destination
        self.activity = activity
        self.streams = {}
        self.next_id = 0
        self.ready = asyncio.Event()
        self.closed = False

    async def send(self, action, ident, data=b""):
        if self.activity is not None:
            await self.activity()
        await self.peer.send(_STREAM_HEADER.pack(action, ident) + data)

    async def accept(self, reader, writer):
        try:
            await self.ready.wait()
            if self.closed:
                return
            self.next_id += 1
            self.start(self.next_id, reader, writer, announce=True)
        finally:
            if self.closed:
                writer.close()

    def start(self, ident, reader, writer, *, announce=False):
        stream = PortStream(writer)
        self.streams[ident] = stream
        stream.task = asyncio.create_task(self.read(ident, stream, reader, announce))

    async def read(self, ident, stream, reader, announce):
        try:
            if announce:
                await self.send(b"o", ident)
            while data := await reader.read(65536):
                await self.send(b"d", ident, data)
            await self.send(b"e", ident)
            await stream.incoming_eof.wait()
        except (EOFError, OSError):
            with suppress(Exception):
                await self.send(b"c", ident)
        except Exception:
            if not self.closed:
                await self.peer.close()
        finally:
            self.streams.pop(ident, None)
            stream.writer.close()
            with suppress(OSError):
                await stream.writer.wait_closed()

    async def run(self):
        self.ready.set()
        try:
            async for packet in self.peer:
                if not isinstance(packet, bytes) or len(packet) < _STREAM_HEADER.size:
                    raise ValueError("Invalid HDC forwarding data")
                action, ident = _STREAM_HEADER.unpack_from(packet)
                data = packet[_STREAM_HEADER.size:]
                stream = self.streams.get(ident)
                if self.activity is not None:
                    await self.activity()
                if action == b"o" and self.destination is not None and stream is None and not data:
                    try:
                        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.destination), 4)
                    except (OSError, asyncio.TimeoutError):
                        await self.send(b"c", ident)
                    else:
                        self.start(ident, reader, writer)
                elif stream is not None and action == b"d" and not stream.incoming_eof.is_set():
                    try:
                        stream.writer.write(data)
                        await stream.writer.drain()
                    except OSError:
                        stream.task.cancel()
                        await self.send(b"c", ident)
                elif stream is not None and action == b"e" and not data:
                    with suppress(OSError):
                        stream.writer.write_eof()
                    stream.incoming_eof.set()
                elif stream is not None and action == b"c" and not data:
                    stream.writer.close()
                    stream.task.cancel()
        finally:
            await self.close()

    async def close(self):
        self.closed = True
        self.ready.set()
        streams = list(self.streams.values())
        tasks = [stream.task for stream in streams]
        for stream in streams:
            stream.writer.close()
            stream.task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class PortStream:
    def __init__(self, writer):
        self.writer = writer
        self.incoming_eof = asyncio.Event()
        self.task = None
