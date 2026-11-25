"""Serve leased native device requests from the existing Agent connection."""
from __future__ import annotations

import asyncio
import base64
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.legacy.client import connect

from ai_phone.ai_connect import ios_wda
from ai_phone.ai_connect.android import COMMAND_SECONDS, execute, local_devices
from ai_phone.ai_connect.native import DeviceTransport
from ai_phone.shared import protocol as P


class NativeDeviceAccess:
    def __init__(self, client):
        self.operations = {}
        self.cancel_replies = set()
        self.inventory_task = None
        self.last_inventory = None
        self.simulators = {}
        client.on(P.MSG_AI_DIRECT_REQUEST, self.handle)
        client.on_connect(self.refresh_inventory)
        client.on_rescan(self.refresh_inventory)
        client.on_disconnect(self.disconnected)

    async def refresh_inventory(self, client, _serials=None):
        # Schedule beside the existing scan so a slow native probe cannot hold up its callbacks.
        if self.inventory_task is None or self.inventory_task.done():
            self.inventory_task = asyncio.create_task(self.publish(client), name="ai-direct-inventory")

    async def disconnected(self, _client):
        tasks = [*self.operations.values(), *self.cancel_replies]
        if self.inventory_task:
            tasks.append(self.inventory_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inventory_task = None
        self.last_inventory = None
        self.simulators = {}

    async def android_inventory(self, serials):
        if not serials:
            return {}
        try:
            return await local_devices(serials)
        except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
            return {}

    async def usb_inventory(self, serials):
        if not serials:
            return {}
        try:
            async with asyncio.timeout(2):
                return await ios_wda.usb_devices(serials)
        except Exception:
            return {}

    async def simulator_inventory(self, serials):
        if not serials:
            return {}
        try:
            async with asyncio.timeout(3):
                return await asyncio.to_thread(ios_wda.simulator_endpoints, serials, time.time() + 2.5)
        except Exception:
            return {}

    async def publish(self, client):
        devices, usb, self.simulators = await asyncio.gather(
            self.android_inventory(client.online_device_serials("android")),
            self.usb_inventory(client.online_device_serials("ios")),
            self.simulator_inventory(client.online_device_serials("ios_sim")),
        )
        wda_devices = {serial: device.devid for serial, device in usb.items()}
        simulators = {serial: generation for serial, (_, generation) in self.simulators.items()}
        snapshot = devices, wda_devices, simulators
        if snapshot != self.last_inventory:
            if await client.send({"type": P.MSG_AI_DIRECT_INVENTORY, "devices": devices,
                                  "wda_devices": wda_devices, "wda_simulators": simulators}):
                self.last_inventory = snapshot

    async def handle(self, client, message):
        ident = message.get("id")
        if not isinstance(ident, str):
            return
        task = self.operations.get(ident)
        if message.get("cancel"):
            if task is not None and task.cancelling():
                return
            if task is not None:
                task.cancel()
            reply = asyncio.create_task(self.reply_after_cancel(client, ident, task))
            self.cancel_replies.add(reply)
            reply.add_done_callback(self.cancel_replies.discard)
        elif task is None:
            task = asyncio.create_task(self.run(client, ident, message), name="ai-direct-request")
            self.operations[ident] = task
            task.add_done_callback(lambda _: self.operations.pop(ident, None))

    async def reply_after_cancel(self, client, ident, task):
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await client.send({"type": P.MSG_AI_DIRECT_RESULT, "id": ident, "error": "操作已终止"})

    async def run(self, client, ident, message):
        try:
            result = await self.execute(client, message)
        except TimeoutError:
            result = {"error": "设备操作超时，动作结果未知"}
        except Exception as exc:
            result = {"error": str(exc)[:300] or "本次设备的原生通道不可用"}
        await client.send({"type": P.MSG_AI_DIRECT_RESULT, "id": ident, **result})

    async def execute(self, client, message):
        serial, transport = message["serial"], message["transport"]
        if "wda" in message:
            budget = min(COMMAND_SECONDS, message["budget"])
            async with asyncio.timeout(budget):
                if serial in client.online_device_serials("ios_sim"):
                    port, generation = self.simulators.get(serial, (None, None))
                    if not port or transport != generation:
                        raise ValueError("本次领取的 iOS 模拟器已停止或重启")
                    deadline = time.time() + budget
                    return await ios_wda.request(
                        lambda: ios_wda.simulator_stream(serial, port, generation, deadline), **message["wda"],
                    )
                if serial not in client.online_device_serials("ios"):
                    raise ValueError("本次领取的 iOS 设备已离线")
                device = (await self.usb_inventory({serial})).get(serial)
                if not device or device.devid != transport:
                    raise ValueError("本次领取的 iOS USB 连接已变化")
                return await ios_wda.request(lambda: ios_wda.usb_stream(device), **message["wda"])

        if (serial not in client.online_device_serials("android")
                or (await self.android_inventory({serial})).get(serial) != transport):
            raise ValueError("本次领取的 ADB 连接已变化")
        if "native_ticket" in message:
            endpoint = urlsplit(client.ws_url)
            path = endpoint.path.removesuffix("/ws/agent") + "/ws/ai-connect/pipe"
            url = urlunsplit((endpoint.scheme, endpoint.netloc, path, "", ""))
            headers = {"Authorization": "Bearer " + message["native_ticket"]}
            async with connect(url, extra_headers=headers, max_size=128 * 1024, open_timeout=5,
                               close_timeout=1, compression=None) as peer:
                await DeviceTransport(peer, serial, transport).run()
            return {"closed": True}

        async with asyncio.timeout(min(COMMAND_SECONDS, message["budget"])):
            result = await execute(serial, transport, message["service"])
        return {"data": base64.b64encode(result).decode("ascii")}
