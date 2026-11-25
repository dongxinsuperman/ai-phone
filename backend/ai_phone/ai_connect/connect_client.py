"""Attach one claimed phone to this computer's ADB or HDC tools."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import connect

if __package__:
    from .hdc_ports import (
        FIRMWARE_COMMANDS, FIRMWARE_REQUEST, FRAME_LENGTH, MAX_FIRMWARE_BYTES, PORT_ERROR, PORT_READY, PORT_REQUEST,
        PortStreams, arguments, firmware_file_index, frame, port_rule,
    )
else:
    from hdc_ports import (
        FIRMWARE_COMMANDS, FIRMWARE_REQUEST, FRAME_LENGTH, MAX_FIRMWARE_BYTES, PORT_ERROR, PORT_READY, PORT_REQUEST,
        PortStreams, arguments, firmware_file_index, frame, port_rule,
    )


async def hdc_packet(reader):
    header = await reader.readexactly(FRAME_LENGTH.size)
    size = FRAME_LENGTH.unpack(header)[0]
    if not 0 < size <= 65536:
        raise ValueError("Invalid HDC command frame")
    return await reader.readexactly(size)


async def hdc_reply(writer, message):
    writer.write(frame((message + "\n").encode()))
    await writer.drain()


async def hdc_forward(ws, writer, hello, command, forwarding):
    listener = None
    streams = None
    rule = None
    owner = asyncio.current_task()
    try:
        parts = arguments(command)
        if parts == ["fport", "ls"]:
            listing = [f"{first} {second} [{'Reverse' if kind == 'rport' else 'Forward'}]"
                       for kind, first, second in forwarding]
            await hdc_reply(writer, "\n".join(listing) or "[Empty]")
            return
        if parts[:2] == ["fport", "rm"]:
            matches = [task for key, task in forwarding.items() if list(key[1:]) == parts[2:]]
            if not matches:
                raise ValueError("Forwarding rule not found in this connector")
            for task in matches:
                task.cancel()
            await asyncio.gather(*matches, return_exceptions=True)
            await hdc_reply(writer, "Remove forward ruler success")
            return

        direction, port, _device = port_rule(parts)
        rule = tuple(parts)
        if rule in forwarding:
            raise ValueError("Forwarding rule already exists")
        forwarding[rule] = owner
        streams = PortStreams(ws, destination=port if direction == "rport" else None)
        if direction == "fport":
            listener = await asyncio.start_server(streams.accept, "127.0.0.1", port)
        await ws.send(frame(hello) + frame(PORT_REQUEST + command))
        status = await asyncio.wait_for(ws.recv(), 8)
        if status != PORT_READY:
            reason = (status[len(PORT_ERROR):].decode(errors="replace")
                      if isinstance(status, bytes) and status.startswith(PORT_ERROR) else "HDC forwarding setup failed")
            raise ValueError(reason)
        await hdc_reply(writer, "Forwardport result:OK")
        writer.close()
        await streams.run()
    except (ValueError, OSError) as exc:
        if not writer.is_closing():
            await hdc_reply(writer, "[Fail] " + str(exc))
    finally:
        if listener is not None:
            listener.close()
            await listener.wait_closed()
        if streams is not None:
            await streams.close()
        if forwarding.get(rule) is owner:
            del forwarding[rule]


async def hdc_firmware(ws, reader, writer, hello, command):
    async def receive():
        async for message in ws:
            writer.write(message)
            await writer.drain()

    async def transfer():
        reply = None
        try:
            parts = arguments(command)
            index = firmware_file_index(parts)
            path = Path(parts[index]).expanduser()
            parts[index] = ""
            with path.open("rb") as package:
                remaining = os.fstat(package.fileno()).st_size
                if not remaining:
                    raise ValueError("The firmware file is empty")
                if remaining > MAX_FIRMWARE_BYTES:
                    raise ValueError("HDC firmware file exceeds the 32 GiB upload limit")
                manifest = json.dumps([parts, path.suffix, remaining]).encode()
                await ws.send(frame(hello) + frame(FIRMWARE_REQUEST + manifest))
                reply = asyncio.create_task(receive())
                try:
                    while remaining:
                        data = package.read(min(65536, remaining))
                        if not data:
                            raise ValueError("The firmware file changed while uploading")
                        await ws.send(frame(data))
                        remaining -= len(data)
                except ConnectionClosed:
                    pass  # The Agent can reject the upload before reading the entire file.
            await reply
        except (OSError, ValueError) as exc:
            await hdc_reply(writer, "[Fail] " + str(exc))
        finally:
            if reply is not None:
                reply.cancel()
                await asyncio.gather(reply, return_exceptions=True)

    tasks = [asyncio.create_task(transfer()), asyncio.create_task(reader.read(1))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def attach(instance, lease, *, web_inspector=False):
    url = urlsplit(instance)
    native = lease["native"]
    path = native["web_inspector"]["url"] if web_inspector else native["url"]
    protocol = "webkit" if web_inspector else native.get("protocol", "adb-connect")
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or not path.startswith("/ws/ai-connect/sessions/")
            or protocol not in {"adb-connect", "hdc-connect", "hdc-server", "webkit"}):
        raise ValueError("需要 AI Phone 实例地址以及该实例返回的领取信息")
    target = urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                         url.path.rstrip("/") + path, "", ""))
    headers = {"Authorization": "Bearer " + lease["token"]}
    stopped = asyncio.Event()
    connections = set()
    forwarding = {}

    async def bridge(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            async with connect(target, extra_headers=headers, max_size=128 * 1024, compression=None,
                               open_timeout=6, close_timeout=1) as ws:
                if protocol == "hdc-server":
                    writer.write(await ws.recv())
                    await writer.drain()
                    hello = await asyncio.wait_for(hdc_packet(reader), 5)
                    command = await asyncio.wait_for(hdc_packet(reader), 5)
                    first = frame(command)
                    if command.rstrip(b"\0") == b"alive":
                        command = await asyncio.wait_for(hdc_packet(reader), 5)
                        first += frame(command)
                    if command.rstrip(b"\0").startswith((b"fport ", b"rport ")):
                        await hdc_forward(ws, writer, hello, command, forwarding)
                        return
                    if command.rstrip(b"\0").startswith(FIRMWARE_COMMANDS):
                        await hdc_firmware(ws, reader, writer, hello, command)
                        return
                    await ws.send(frame(hello) + first)

                async def upload():
                    while data := await reader.read(65536):
                        await ws.send(data)

                async def download():
                    async for data in ws:
                        writer.write(data)
                        await writer.drain()

                pumps = [asyncio.create_task(upload()), asyncio.create_task(download())]
                try:
                    done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                    for pump in done:
                        pump.result()
                finally:
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
        except InvalidHandshake as exc:
            print(f"AI Phone 拒绝连接：{exc}", file=sys.stderr, flush=True)
            if getattr(exc, "status_code", None) in (401, 403, 410):
                stopped.set()
        except ConnectionClosed as exc:
            if exc.code == 1008:
                stopped.set()
            elif protocol == "webkit":
                print(f"Web Inspector 连接中断：{exc.reason or exc.code}", file=sys.stderr, flush=True)
        except (EOFError, TimeoutError, OSError, ValueError) as exc:
            print(f"AI Phone 连接中断：{exc}", file=sys.stderr, flush=True)
        finally:
            writer.close()
            connections.discard(task)

    server = await asyncio.start_server(bridge, "127.0.0.1", 0)
    address = f"127.0.0.1:{server.sockets[0].getsockname()[1]}"

    async def invoke(*args):
        cmd = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.STDOUT)
        raw, _ = await cmd.communicate()
        return cmd.returncode, raw.decode(errors="replace").strip()

    async with server:
        try:
            if protocol == "webkit":
                result = {"serial": lease["serial"], "web_inspector": address}
            elif protocol == "hdc-server":
                code, targets = await invoke("hdc", "-p", "-s", address, "list", "targets")
                if code or lease["serial"] not in targets.split():
                    raise ValueError(f"hdc -s {address} 未连上所领设备")
                result = {"serial": lease["serial"], "hdc_server": address, "state": "device"}
            elif protocol == "hdc-connect":
                _, result = await invoke("hdc", "tconn", address)
                code, targets = await invoke("hdc", "list", "targets")
                if code or address not in targets.split():
                    raise ValueError(f"hdc tconn {address} 未连上设备：{result}")
                result = {"serial": lease["serial"], "hdc_serial": address, "state": "device"}
            else:
                _, result = await invoke("adb", "connect", address)
                code, device_state = await invoke("adb", "-s", address, "get-state")
                if code or device_state != "device":
                    raise ValueError(f"adb connect {address} 未连上设备：{result} {device_state}")
                result = {"serial": lease["serial"], "adb_serial": address, "state": "device"}
            print(json.dumps(result), flush=True)
            await stopped.wait()
        finally:
            server.close()
            await server.wait_closed()
            for task in list(connections):
                task.cancel()
            await asyncio.gather(*list(connections), return_exceptions=True)
            if protocol == "hdc-connect":
                await invoke("hdc", "tconn", address, "-remove")
            elif protocol == "adb-connect":
                await invoke("adb", "disconnect", address)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", help="AI Phone 实例的 HTTP(S) 地址")
    parser.add_argument("lease", help="保存领取结果的 JSON 文件")
    parser.add_argument("--web-inspector", action="store_true", help="连接所领 iOS 设备的 Web Inspector")
    args = parser.parse_args()
    try:
        with open(args.lease, encoding="utf-8") as source:
            asyncio.run(attach(args.instance, json.load(source), web_inspector=args.web_inspector))
    except KeyboardInterrupt:
        pass
    except (KeyError, ValueError, OSError) as error:
        sys.exit(str(error))
