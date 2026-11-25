"""Dispatch AI Phone mobile frame operations independently of the Feature drivers."""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from ai_phone.shared import protocol as P

from .device_activity import device_activity
from .mobile_sim_errors import MobileSimError


def _adapter(platform):
    if platform == "ios":
        from . import ios_sim_observation

        return ios_sim_observation
    if platform == "harmony":
        from . import harmony_sim_observation

        return harmony_sim_observation
    raise MobileSimError(400, "未识别的设备平台")


def execute(message: dict) -> dict:
    serial = message.get("serial")
    deadline = message.get("deadline")
    if not isinstance(serial, str) or not serial or not isinstance(deadline, (int, float)):
        raise MobileSimError(400, "请求缺少设备标识或截止时间")
    if deadline <= time.time():
        raise MobileSimError(409, "请求已过期")
    adapter = _adapter(message.get("platform"))
    with device_activity.hold(serial) as acquired:
        if not acquired:
            raise MobileSimError(409, "该设备正在维护")
        if message.get("action") == "bind":
            return adapter.bind(serial, deadline)
        if not message.get("binding"):
            raise MobileSimError(409, "本次设备连接已失效，请重新领取")
        return adapter.execute(message)


_pending: set[asyncio.Task] = set()


async def _reply(client, message: dict) -> None:
    envelope = {
        "type": P.MSG_UNIFIED_OPERATION_RESULT,
        "request_id": message.get("request_id"),
        "serial": message.get("serial"),
        "platform": message.get("platform"),
    }
    if message.get("binding") is not None:
        envelope["binding"] = message["binding"]
    try:
        if message.get("type") == P.MSG_IOS_USB_OPERATION:
            from . import ios_usb_observation

            fields = await ios_usb_observation.execute(message)
        else:
            fields = await asyncio.to_thread(execute, message)
    except MobileSimError as exc:
        fields = {"ok": False, "status": exc.status, "error": str(exc)}
    except TimeoutError:
        fields = {"ok": False, "status": 504, "error": "设备响应超时，请观察当前页面"}
    except Exception as exc:
        logger.warning("Mobile operation failed | platform={} action={} error={}",
                       message.get("platform"), message.get("action"), type(exc).__name__)
        fields = {"ok": False, "status": 502, "error": "设备操作中断，执行结果未知"}
    else:
        fields = {**fields, "ok": True}
    await client.send({**fields, **envelope})


async def handle(client, message: dict) -> None:
    task = asyncio.create_task(_reply(client, message))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
