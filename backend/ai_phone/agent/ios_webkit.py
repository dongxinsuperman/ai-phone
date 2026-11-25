"""Relay the leased iOS device's Web Inspector without exposing usbmux or the host."""
import asyncio
import json
import plistlib
import struct
import subprocess
from contextlib import suppress

from ai_phone.ai_connect import ios_wda


_LENGTH = struct.Struct("!I")
_CHUNK = 65536
_SOCKET = "/com.apple.webinspectord_sim.socket"


def simulator_socket(serial):
    def output(*args):
        return subprocess.check_output(args, text=True, timeout=3, stderr=subprocess.DEVNULL)

    devices = json.loads(output("xcrun", "simctl", "list", "devices", "booted", "--json"))["devices"]
    matches = [device for group in devices.values() for device in group if device["udid"] == serial]
    if len(matches) != 1 or matches[0]["state"] != "Booted":
        raise ValueError("本次领取的 iOS 模拟器已停止")
    bootstrap = matches[0]["dataPath"].rstrip("/") + "/var/run/launchd_bootstrap.plist"
    processes = [line.strip().split(maxsplit=2) for line in output("ps", "-axo", "pid=,command=").splitlines()]
    owners = [p[0] for p in processes if len(p) == 3 and p[1] == "launchd_sim" and p[2] == bootstrap]
    if len(owners) != 1:
        raise ValueError("无法确认所领模拟器的 Web Inspector 归属")
    sockets = {line[1:] for line in output("lsof", "-a", "-p", owners[0], "-U", "-Fn").splitlines()
               if line.startswith("n/") and line.endswith(_SOCKET)}
    if len(sockets) != 1:
        raise ValueError("本次领取的模拟器没有可用的 Web Inspector")
    return sockets.pop()


async def forward(peer, serial, transport, *, simulator_port=None, deadline=None):
    if simulator_port is not None:
        from .ios_sim_observation import boot_identity

        address = await asyncio.to_thread(simulator_socket, serial)
        reader, writer = await asyncio.open_unix_connection(address)
        try:
            if await asyncio.to_thread(boot_identity, serial, simulator_port, deadline) != transport:
                raise ValueError("本次领取的 iOS 模拟器已重启")

            async def upload():
                async for data in peer:
                    if not isinstance(data, bytes):
                        raise ValueError("Web Inspector 需要二进制数据")
                    await peer.send("request")
                    writer.write(data)
                    await writer.drain()

            async def download():
                while data := await reader.read(_CHUNK):
                    await peer.send(data)

            await exchange(upload, download)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        return

    from pymobiledevice3.lockdown import create_using_usbmux

    async with asyncio.timeout(5):
        lockdown = await create_using_usbmux(serial=serial, connection_type="USB", autopair=False)
    service = None
    try:
        async with asyncio.timeout(5):
            service = await lockdown.start_lockdown_service("com.apple.webinspector")
            device = (await ios_wda.usb_devices({serial})).get(serial)
            if not device or device.devid != transport:
                raise ValueError("本次领取的 iOS USB 连接已变化")

        async def upload():
            pending = bytearray()
            async for data in peer:
                if not isinstance(data, bytes):
                    raise ValueError("Web Inspector 需要二进制数据")
                pending.extend(data)
                while len(pending) >= _LENGTH.size:
                    size = _LENGTH.unpack_from(pending)[0]
                    if not 0 < size <= 32 * 1024 * 1024:
                        raise ValueError("无效的 Web Inspector 数据帧")
                    end = _LENGTH.size + size
                    if len(pending) < end:
                        break
                    message = plistlib.loads(pending[_LENGTH.size:end])
                    del pending[:end]
                    await peer.send("request")
                    await service.send_plist(message)

        async def download():
            while True:
                data = plistlib.dumps(await service.recv_plist(), fmt=plistlib.FMT_BINARY)
                await peer.send(_LENGTH.pack(len(data)))
                for offset in range(0, len(data), _CHUNK):
                    await peer.send(data[offset:offset + _CHUNK])

        await exchange(upload, download)
    finally:
        try:
            if service is not None:
                await service.close()
        finally:
            await lockdown.close()


async def exchange(upload, download):
    tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
