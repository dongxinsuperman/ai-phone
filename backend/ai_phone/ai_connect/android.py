"""Device-side ADB services for a selected local Android transport."""
from __future__ import annotations

import asyncio
import re
import struct
from contextlib import asynccontextmanager

from .activity import is_read_only_shell

MAX_OUTPUT = 8 * 1024 * 1024
COMMAND_SECONDS = 20
_DEVTOOLS_SOCKET = re.compile(rb"localabstract:(?:webview|chrome)_devtools_remote(?:_[A-Za-z0-9_.-]+)?")


def is_local_emulator(serial: str) -> bool:
    return re.fullmatch(r"emulator-[0-9]+", serial) is not None


def is_network_adb(serial: str) -> bool:
    return ":" in serial or "._adb-" in serial


def hardware_serial(value) -> str | None:
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value)
            and value.lower() not in {"unknown", "none", "null", "0", "0123456789abcdef", "1234567890"}):
        return value
    return None


def local_transport_id(serial: str, properties: dict[str, str]) -> int | None:
    if properties.get("usb") or is_local_emulator(serial) or is_network_adb(serial):
        value = properties.get("transport_id", "")
        if value.isascii() and value.isdecimal() and int(value) > 0:
            return int(value)
    return None


def allowed_service(service: str) -> bool:
    name, separator, command = service.partition(":")
    if not separator or not command.strip() or len(service.encode()) > 16384 or "\0" in service:
        return False
    return name in {"shell", "exec"} or bool(
        re.fullmatch(r"shell,v2,raw(?:,TERM=[A-Za-z0-9_.+-]+)?", name)
        or re.fullmatch(r"shell,v2,TERM=[A-Za-z0-9_.+-]+,raw", name)
    )


def is_read_only_service(name: bytes) -> bool:
    if name in (b"reverse:list-forward", b"exec:screencap -p") or _DEVTOOLS_SOCKET.fullmatch(name):
        return True
    service, separator, command = name.partition(b":")
    return bool(separator and service.split(b",", 1)[0] in (b"shell", b"exec") and is_read_only_shell(command))


@asynccontextmanager
async def adb_connection():
    reader, writer = await asyncio.open_connection("127.0.0.1", 5037)
    try:
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()


async def request(reader, writer, name: str):
    payload = name.encode() if isinstance(name, str) else name
    if not 0 < len(payload) <= 0xffff:
        raise ValueError("无效的 ADB 服务名")
    writer.write(f"{len(payload):04x}".encode() + payload)
    await writer.drain()
    status = await reader.readexactly(4)
    if status == b"FAIL":
        detail = await read_string(reader)
        raise ValueError(detail.decode(errors="replace"))
    if status != b"OKAY":
        raise ValueError("ADB 服务返回了未知状态")


async def read_string(reader):
    size = int(await reader.readexactly(4), 16)
    return await reader.readexactly(size)


@asynccontextmanager
async def device_connection(serial, transport):
    async with adb_connection() as (reader, writer):
        await request(reader, writer, f"host:tport:serial:{serial}")
        actual = struct.unpack("=Q", await reader.readexactly(8))[0]
        if actual != transport:
            raise ValueError("设备的 ADB 连接已变化")
        yield reader, writer


async def local_inventory(allowed: set[str]) -> dict[str, dict[str, str]]:
    async with asyncio.timeout(3), adb_connection() as (reader, writer):
        await request(reader, writer, "host:devices-l")
        result = {}
        for line in (await read_string(reader)).decode().splitlines():
            fields = line.split()
            if len(fields) < 3 or fields[0] not in allowed or fields[1] != "device":
                continue
            props = dict(f.split(":", 1) for f in fields[2:] if ":" in f)
            if local_transport_id(fields[0], props) is not None:
                result[fields[0]] = props
        return result


async def local_devices(allowed: set[str]) -> dict[str, int]:
    return {serial: int(props["transport_id"]) for serial, props in (await local_inventory(allowed)).items()}


async def execute(serial: str, transport: int, service: str) -> bytes:
    if not allowed_service(service):
        raise ValueError("此入口支持非交互 adb shell 和 exec-out")
    async with asyncio.timeout(COMMAND_SECONDS), device_connection(serial, transport) as (reader, writer):
        await request(reader, writer, service)
        if service.startswith("shell,v2,"):
            # ADB shell v2 CLOSE_STDIN. Output, stderr and exit status stay in native framing.
            writer.write(b"\x04\0\0\0\0")
            await writer.drain()
        output = bytearray()
        while chunk := await reader.read(65536):
            output.extend(chunk)
            if len(output) > MAX_OUTPUT:
                raise ValueError("本次 ADB 输出超过 8 MiB")
        return bytes(output)
