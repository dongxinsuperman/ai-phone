"""Run iOS frame operations over the USB connection of the current lease."""
from __future__ import annotations

import asyncio
import base64
from json import dumps

import httpx

from ai_phone.ai_connect import ios_wda

from .device_activity import device_activity
from .ios_sim_observation import _remaining, _Wda, execute_wda
from .mobile_sim_errors import MobileSimError


class UsbWdaClient:
    def __init__(self, device, loop):
        self.device = device
        self.loop = loop

    def request(self, method, path, *, json: dict | None, timeout: float):
        body = b"" if json is None else dumps(json, ensure_ascii=False).encode()
        task = asyncio.run_coroutine_threadsafe(
            ios_wda.request(lambda: ios_wda.usb_stream(self.device), method, path,
                            base64.b64encode(body).decode("ascii")),
            self.loop,
        )
        try:
            response = task.result(timeout)
        except TimeoutError as exc:
            task.cancel()
            raise MobileSimError(504, "iOS USB 设备未在期限内返回结果") from exc
        except (OSError, ValueError) as exc:
            raise MobileSimError(503, "本次占用绑定的 iOS USB 控制服务暂不可达") from exc
        return httpx.Response(response["status"], content=base64.b64decode(response["data"]))


def run(message, client):
    with device_activity.hold(message["serial"]) as acquired:
        if not acquired:
            raise MobileSimError(409, "该 iOS 设备正在维护")
        wda = _Wda(client, None, message["deadline"])
        status = wda.request("GET", "/status")
        info = status.get("value")
        if not isinstance(info, dict) or info.get("ready") is not True:
            raise MobileSimError(503, "本次占用绑定的 iOS USB 控制服务尚未就绪")
        wda.reuse_session(status)
        return execute_wda(wda, message)


async def execute(message):
    serial, transport, deadline = (message.get(key) for key in ("serial", "transport", "deadline"))
    if not ios_wda.is_udid(serial) or type(transport) is not int or not isinstance(deadline, (int, float)):
        raise MobileSimError(400, "请求缺少 iOS USB 设备标识或截止时间")
    async with asyncio.timeout(min(2, _remaining(deadline))):
        device = (await ios_wda.usb_devices({serial})).get(serial)
    if device is None or device.devid != transport:
        raise MobileSimError(409, "本次领取绑定的 iOS USB 连接已变化，请重新领取")
    return await asyncio.to_thread(run, message, UsbWdaClient(device, asyncio.get_running_loop()))
