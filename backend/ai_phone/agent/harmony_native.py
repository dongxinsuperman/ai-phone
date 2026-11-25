"""Identify connected Harmony devices and relay managed simulator HDC links."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import suppress

from ai_phone.ai_connect.hdc import valid_device, valid_identity

from .harmony_vm.registry import managed_vm_owner, resolve_harmony_serial


_LOOPBACK = re.compile(r"127\.0\.0\.1:([0-9]{1,5})")


def inventory(serials):
    result = {}
    for serial in serials:
        if not serial.startswith("harmony-vm:"):
            if valid_device(serial) and managed_vm_owner(serial) is None:
                result[serial] = hashlib.sha256(f"hdc-device\0{serial}".encode()).hexdigest()
            continue
        try:
            address = resolve_harmony_serial(serial)
        except RuntimeError:
            continue
        owner = managed_vm_owner(address)
        if owner and serial == f"harmony-vm:{owner[0]}" and _LOOPBACK.fullmatch(address):
            generation = hashlib.sha256(f"{serial}\0{address}\0{owner[1]}".encode()).hexdigest()
            if valid_identity(serial, generation):
                result[serial] = generation
    return result


def guest(serial, transport, deadline):
    from .harmony_sim_observation import _guest

    if inventory({serial}).get(serial) != transport:
        raise ValueError("本次领取的鸿蒙设备连接已变化")
    current = _guest(serial, deadline)
    if not serial.startswith("harmony-vm:"):
        from .drivers.hdc import hdc_list_targets

        remaining = max(0, deadline - time.time())
        targets = hdc_list_targets(deadline=time.monotonic() + remaining, strict=True)
        if not any(target.serial == serial and target.status.lower() == "connected" for target in targets):
            raise ValueError("本次领取的鸿蒙设备已离线")
    if inventory({serial}).get(serial) != transport:
        raise ValueError("本次领取的鸿蒙设备连接已变化")
    return current


async def forward(peer, target):
    port = int(_LOOPBACK.fullmatch(target.address)[1])
    async with asyncio.timeout(4):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    tasks = []

    async def download():
        while data := await reader.read(65536):
            target.check()
            await peer.send(data)

    async def upload():
        async for data in peer:
            target.check()
            if not isinstance(data, bytes):
                raise ValueError("无效的 HDC 数据")
            writer.write(data)
            await writer.drain()

    try:
        target.check()
        tasks = [asyncio.create_task(download()), asyncio.create_task(upload())]
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
