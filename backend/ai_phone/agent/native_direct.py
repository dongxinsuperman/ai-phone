"""Serve leased native device requests from the existing Agent connection."""
from __future__ import annotations

import asyncio
import base64
import time
from contextlib import suppress
from urllib.parse import urlsplit, urlunsplit

from loguru import logger
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect

from ai_phone.ai_connect import ios_wda
from ai_phone.ai_connect.android import COMMAND_SECONDS, execute, local_devices
from ai_phone.ai_connect.native import DeviceTransport
from ai_phone.shared import protocol as P

from . import harmony_native, hdc_host, ios_webkit


def data_peer(client, ticket):
    endpoint = urlsplit(client.ws_url)
    path = endpoint.path.removesuffix("/ws/agent") + "/ws/ai-connect/pipe"
    url = urlunsplit((endpoint.scheme, endpoint.netloc, path, "", ""))
    return connect(url, extra_headers={"Authorization": "Bearer " + ticket},
                   max_size=128 * 1024, open_timeout=5, close_timeout=1, compression=None)


class NativeDeviceAccess:
    def __init__(self, client):
        self.operations = {}
        self.cancel_replies = set()
        self.inventory_task = None
        self.inventory_requested = False
        self.last_inventory = None
        self.simulators = {}
        client.on(P.MSG_AI_DIRECT_REQUEST, self.handle)
        client.on_connect(self.refresh_inventory)
        client.on_rescan(self.refresh_inventory)
        client.on_disconnect(self.disconnected)

    async def refresh_inventory(self, client, _serials=None):
        # Schedule beside the existing scan so a slow native probe cannot hold up its callbacks.
        self.inventory_requested = True
        if self.inventory_task is None or self.inventory_task.done():
            self.inventory_task = asyncio.create_task(self.publish_pending(client), name="ai-direct-inventory")

    async def publish_pending(self, client):
        while self.inventory_requested:
            self.inventory_requested = False
            try:
                await self.publish(client)
            except Exception:
                logger.exception("AI 直连设备清单上报失败")

    async def disconnected(self, _client):
        tasks = [*self.operations.values(), *self.cancel_replies]
        if self.inventory_task:
            tasks.append(self.inventory_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inventory_task = None
        self.inventory_requested = False
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
        try:
            hdc_devices = harmony_native.inventory(client.online_device_serials("harmony"))
        except Exception:
            logger.exception("AI 直连鸿蒙设备清单采集失败")
            hdc_devices = {}
        snapshot = devices, wda_devices, simulators, hdc_devices
        if snapshot != self.last_inventory:
            if await client.send({"type": P.MSG_AI_DIRECT_INVENTORY, "devices": devices,
                                  "wda_devices": wda_devices, "wda_simulators": simulators,
                                  "hdc_devices": hdc_devices}):
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
        if message.get("webkit"):
            if serial in client.online_device_serials("ios_sim"):
                port, generation = self.simulators.get(serial, (None, None))
                if not port or transport != generation:
                    raise ValueError("本次领取的 iOS 模拟器已停止或重启")
            elif serial in client.online_device_serials("ios"):
                device = (await self.usb_inventory({serial})).get(serial)
                if not device or device.devid != transport:
                    raise ValueError("本次领取的 iOS USB 连接已变化")
                port = None
            else:
                raise ValueError("本次领取的 iOS 设备已离线")
            async with data_peer(client, message["native_ticket"]) as peer:
                try:
                    await ios_webkit.forward(peer, serial, transport, simulator_port=port, deadline=time.time() + 10)
                except Exception as exc:
                    with suppress(ConnectionClosed, OSError):
                        await peer.send("error:" + (str(exc)[:300] or "设备 Web Inspector 已关闭连接"))
                    raise
            return {"closed": True}
        if message.get("hdc_bind") or "hdc_binding" in message:
            if serial not in client.online_device_serials("harmony"):
                raise ValueError("本次领取的鸿蒙设备已离线")
            budget = min(COMMAND_SECONDS, message.get("budget", 5))
            async with asyncio.timeout(budget):
                guest = await asyncio.to_thread(harmony_native.guest, serial, transport, time.time() + budget)
            if message.get("hdc_bind"):
                from .harmony_sim_observation import ACTION_TYPES

                return {"binding": guest.binding, "action_types": list(ACTION_TYPES)}
            if guest.binding != message["hdc_binding"]:
                raise ValueError("本次领取的鸿蒙设备连接已变化")
            async with data_peer(client, message["native_ticket"]) as peer:
                if guest.lease:
                    await harmony_native.forward(peer, guest)
                else:
                    await hdc_host.forward(peer, serial)
            return {"closed": True}

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
            async with data_peer(client, message["native_ticket"]) as peer:
                await DeviceTransport(peer, serial, transport).run()
            return {"closed": True}

        async with asyncio.timeout(min(COMMAND_SECONDS, message["budget"])):
            result = await execute(serial, transport, message["service"])
        return {"data": base64.b64encode(result).decode("ascii")}
