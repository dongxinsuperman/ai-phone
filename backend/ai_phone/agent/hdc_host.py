"""Proxy one native HDC client channel to the Agent's already-connected device."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from loguru import logger

from ai_phone.ai_connect.activity import RENEW_ACTIVITY, WRITE_ACTIVITY, is_read_only_shell
from ai_phone.ai_connect.hdc import valid_device
from ai_phone.ai_connect.hdc_ports import (
    FIRMWARE_COMMANDS, FIRMWARE_REQUEST, FRAME_LENGTH, MAX_FIRMWARE_BYTES, PORT_ERROR, PORT_READY, PORT_REQUEST,
    PortStreams, arguments, firmware_file_index, frame, port_rule,
)

from .drivers.hdc import hdc_run


_BANNER = b"OHOS HDC"
_KEY_START, _KEY_END = 12, 44
# These HDC operations manage the shared host server, use its network, or open
# files on the Agent host. Target commands go to HDC without a command allowlist.
_HOST_OPERATIONS = (
    b"start", b"kill", b"tconn", b"checkdevice", b"spawn-sub",
)
_FILE_COMMANDS = (b"file send", b"file recv", b"install")
# HDC marks CLI transfers as remote so local paths are handled by the caller's HDC client.
_REMOTE_TRANSFERS = (b"file send remote ", b"file recv remote ", b"install remote ")
# OpenHarmony HDC's host-side file and application transfer command IDs.
_TRANSFER = (*range(3000, 3005), 3006, 3007, *range(3500, 3505))
_FIRMWARE_MIN_FREE = 1024**3
_FIRMWARE_SPACE_CHECK_INTERVAL = 8 * 1024**2
# The free-space reading already counts written bytes. Reserve only the bytes
# still expected by other uploads in this Agent's event loop.
_firmware_bytes_pending = 0


def pinned_handshake(payload, serial, server_banner):
    if (not valid_device(serial) or not payload.startswith(_BANNER)
            or len(payload) not in (_KEY_END, _KEY_END + 64)):
        raise ValueError("HDC 会话握手格式无效")
    requested = payload[_KEY_START:_KEY_END].split(b"\x00", 1)[0]
    key = serial.encode("ascii")
    if requested and requested not in (b"any", key):
        raise ValueError("HDC 请求设备与本次领用不一致")
    # Preserve the server's feature flags and bind this channel to the target.
    return frame(server_banner + key.ljust(_KEY_END - _KEY_START, b"\x00") + payload[_KEY_END:])


async def _port_tunnel(peer, serial, request, *, renew_activity=False):
    direction, _, endpoint = port_rule(arguments(request))
    last_activity = 0.0

    async def activity():
        nonlocal last_activity
        now = time.monotonic()
        if now - last_activity >= 5:
            last_activity = now
            await peer.send(RENEW_ACTIVITY if renew_activity else WRITE_ACTIVITY)

    listener = None
    if direction == "fport":
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        streams = PortStreams(peer, destination=port, activity=activity)
        rule = (f"tcp:{port}", endpoint)
    else:
        streams = PortStreams(peer, activity=activity)
        listener = await asyncio.start_server(streams.accept, "127.0.0.1", 0)
        rule = (endpoint, f"tcp:{listener.sockets[0].getsockname()[1]}")

    setup = asyncio.create_task(asyncio.to_thread(hdc_run, "-p", direction, *rule, serial=serial, timeout=4))
    try:
        try:
            reply = await asyncio.shield(setup)
            if "Forwardport result:OK" not in reply:
                raise ValueError(reply or "HDC did not establish the port")
        except (OSError, ValueError, RuntimeError) as exc:
            await peer.send(PORT_ERROR + str(exc).encode()[:500])
            return
        await activity()
        await peer.send(PORT_READY)
        await streams.run()
    finally:
        if listener is not None:
            listener.close()
            await listener.wait_closed()
        await streams.close()
        # The native command may finish while the caller is disconnecting.
        reply, = await asyncio.gather(setup, return_exceptions=True)
        if isinstance(reply, str) and "Forwardport result:OK" in reply:
            try:
                removed = await asyncio.to_thread(hdc_run, "-p", "fport", "rm", *rule, serial=serial, timeout=3)
                if "success" not in removed.lower():
                    logger.warning("HDC forwarding cleanup did not confirm removal for {}: {}", serial, removed)
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("HDC forwarding cleanup failed for {}: {}", serial, exc)


async def _firmware_tunnel(peer, writer, binding, client_frame, download, manifest):
    global _firmware_bytes_pending
    fields = json.loads(manifest)
    if not isinstance(fields, list) or len(fields) != 3:
        raise ValueError("无效的 HDC 固件文件")
    parts, extension, size = fields
    if (not isinstance(parts, list) or any(not isinstance(p, str) for p in parts)
            or not isinstance(extension, str) or not re.fullmatch(r"(?:\.[A-Za-z0-9]+)?", extension)
            or type(size) is not int or size <= 0):
        raise ValueError("无效的 HDC 固件文件")
    if size > MAX_FIRMWARE_BYTES:
        raise ValueError("HDC 固件文件超过单文件 32 GiB 上限")
    index = firmware_file_index(parts)
    if parts[index]:
        raise ValueError("HDC 固件必须由调用方上传")
    with tempfile.TemporaryDirectory(prefix="ai-phone-hdc-") as directory:
        path = Path(directory) / ("upload" + extension)
        if size + _firmware_bytes_pending > shutil.disk_usage(directory).free - _FIRMWARE_MIN_FREE:
            raise ValueError("Agent 暂存空间不足，无法接收 HDC 固件")
        remaining = size
        last_activity = 0.0
        since_space_check = 0
        _firmware_bytes_pending += size
        try:
            with path.open("wb") as package:
                while remaining:
                    limit = min(65536, remaining)
                    data = await client_frame(limit)
                    count = len(data)
                    if not 0 < count <= limit:
                        raise ValueError("HDC 固件数据超出声明大小或提前结束")
                    if package.write(data) != count:
                        raise OSError("Agent 未能写完 HDC 固件数据")
                    remaining -= count
                    _firmware_bytes_pending -= count
                    since_space_check += count
                    if since_space_check >= _FIRMWARE_SPACE_CHECK_INTERVAL:
                        if _firmware_bytes_pending > shutil.disk_usage(directory).free - _FIRMWARE_MIN_FREE:
                            raise ValueError("Agent 暂存空间不足，HDC 固件上传已中断")
                        since_space_check = 0
                    now = time.monotonic()
                    if now - last_activity >= 5:
                        await peer.send(WRITE_ACTIVITY)
                        last_activity = now
        finally:
            _firmware_bytes_pending -= remaining
        parts[index] = str(path)
        await peer.send(WRITE_ACTIVITY)
        writer.write(binding + frame(" ".join(parts).encode() + b"\0"))
        await writer.drain()
        await download()


async def forward(peer, serial, *, renew_activity=False):
    # Match the existing HDC CLI's local server port. Keep the original USB session attached.
    port = int(os.environ.get("OHOS_HDC_SERVER_PORT", "8710"))
    async with asyncio.timeout(4):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    pending = bytearray()
    tasks = []
    last_use = 0.0

    async def activity(event):
        nonlocal last_use
        now = time.monotonic()
        if now - last_use >= 5:
            last_use = now
            await peer.send(event)

    async def client_frame(limit=65536):
        while len(pending) < FRAME_LENGTH.size:
            pending.extend(await peer.recv())
        size = FRAME_LENGTH.unpack_from(pending)[0]
        if not 0 < size <= limit:
            raise ValueError("无效的 HDC 客户端请求")
        end = size + FRAME_LENGTH.size
        while len(pending) < end:
            pending.extend(await peer.recv())
        data = bytes(pending[FRAME_LENGTH.size:end])
        del pending[:end]
        return data

    async def upload(command, transfer):
        if command != b"shell" and not transfer:
            await peer.wait_closed()
            return
        while True:
            data = await client_frame(8 * 1024 * 1024)
            if transfer and (len(data) < 2 or int.from_bytes(data[:2], "little") not in _TRANSFER):
                raise ValueError("无效的 HDC 文件传输数据")
            await peer.send(WRITE_ACTIVITY)
            writer.write(frame(data))
            await writer.drain()

    async def download(event=WRITE_ACTIVITY):
        while data := await reader.read(65536):
            await activity(event)
            await peer.send(data)

    try:
        async with asyncio.timeout(5):
            hello = await reader.readexactly(FRAME_LENGTH.size)
            length = FRAME_LENGTH.unpack(hello)[0]
            if length not in (_KEY_END, _KEY_END + 64):
                raise ValueError("宿主 HDC 握手格式无效")
            server_hello = await reader.readexactly(length)
            await peer.send(hello + server_hello)
            binding = pinned_handshake(await client_frame(), serial, server_hello[:_KEY_START])
            first = await client_frame()
            command = first.rstrip(b"\x00")
            if command == b"alive":
                first = await client_frame()
                command = first.rstrip(b"\x00")
            if not command or b"\x00" in command:
                raise ValueError("无效的 HDC 客户端命令")
            port_request = command.removeprefix(PORT_REQUEST) if command.startswith(PORT_REQUEST) else None
            firmware_request = command.removeprefix(FIRMWARE_REQUEST) if command.startswith(FIRMWARE_REQUEST) else None
            if command.startswith(b"list targets"):
                if renew_activity:
                    await peer.send(RENEW_ACTIVITY)
                await peer.send(frame(serial.encode("ascii") + b"\n"))
                return
            if command == b"wait":
                if renew_activity:
                    await peer.send(RENEW_ACTIVITY)
                await peer.send(frame(b"Wait for connected target is " + serial.encode("ascii") + b"\n"))
                return
            if command == b"reconnect" or command.startswith(b"reconnect "):
                requested = arguments(command)[1:]
                if requested and requested != [serial]:
                    await peer.send(frame(b"[Fail] The target differs from the leased device\n"))
                    return
                await activity(WRITE_ACTIVITY)
                try:
                    reply = await asyncio.to_thread(hdc_run, "-p", "reconnect", serial, timeout=4)
                except RuntimeError as exc:
                    reply = "[Fail] " + str(exc)
                await peer.send(frame((reply + "\n").encode()))
                return
            if command.startswith((b"fport ", b"rport ")):
                await peer.send(frame(b"[Fail] Use the AI Phone HDC connector for port forwarding\n"))
                return
            if command.startswith((b"sideload", *FIRMWARE_COMMANDS[1:])):
                await peer.send(frame(b"[Fail] Use the AI Phone HDC connector to upload the caller's file\n"))
                return
            transfer = command.startswith(_REMOTE_TRANSFERS)
            if (command in (b"any", b"discover") or command.startswith(_HOST_OPERATIONS)
                    or (command.startswith(_FILE_COMMANDS) and not transfer)):
                await peer.send(frame(b"[Fail] This command requires access to the HDC host\n"))
                return
            if command.startswith(b"bugreport"):
                # HDC's optional output path is on the server. Return stdout to the caller.
                first = b"bugreport\x00"
            shell_command = command.removeprefix(b"shell ")
            event = WRITE_ACTIVITY
            if renew_activity and shell_command != command and is_read_only_shell(shell_command):
                event = RENEW_ACTIVITY
            if port_request is None and firmware_request is None:
                await activity(event)
                writer.write(binding + frame(first))
                await writer.drain()
        if port_request is not None:
            writer.close()
            await _port_tunnel(peer, serial, port_request, renew_activity=renew_activity)
            return
        if firmware_request is not None:
            try:
                await _firmware_tunnel(peer, writer, binding, client_frame, download, firmware_request)
            except (OSError, ValueError) as exc:
                await peer.send(frame(("[Fail] " + str(exc) + "\n").encode()))
            return
        tasks = [asyncio.create_task(upload(command, transfer)), asyncio.create_task(download(event))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
