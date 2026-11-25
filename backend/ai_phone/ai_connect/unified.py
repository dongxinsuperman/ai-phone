"""Unified operations over the regular Agent connection."""
from __future__ import annotations

import asyncio
import base64
import secrets
import time
from dataclasses import dataclass, replace
from struct import unpack_from
from uuid import UUID

from fastapi import HTTPException, Response
from pydantic import BaseModel, Field

from ai_phone.server.hub import Hub
from ai_phone.shared import protocol as P

from .android import hardware_serial, is_local_emulator, is_network_adb


COMMAND_TIMEOUT = 10
# On a cold iOS or Harmony Agent, the first structured observation also builds the
# local font tables. Subsequent reads reuse them. Device actions keep their
# own deadline so a slow observation cannot lengthen permission to send input.
MOBILE_OBSERVE_TIMEOUT = 20
MUTATIONS = ("tap", "scroll", "input", "back", "home", "open_app")
READS = ("observe", "screenshot")


@dataclass(frozen=True)
class Target:
    agent_id: str
    platform: str
    action_types: tuple[str, ...]
    transport: int | str | None = None
    binding: str | None = None
    instance_id: str | None = None


@dataclass(frozen=True)
class Screen:
    ident: str
    width: int
    height: int
    display: dict
    observation: dict | None = None


@dataclass
class Pending:
    hub: Hub
    agent_id: str
    serial: str
    future: asyncio.Future
    platform: str
    binding: str | None
    action: str


_pending: dict[str, Pending] = {}


def _transport_error(action: str, *, timeout: bool = False) -> str:
    if action == "observe":
        return "等待设备返回画面超时，请重新观察" if timeout else "设备连接已中断，未取得画面"
    if action == "screenshot":
        return "等待设备返回截图超时" if timeout else "设备连接已中断，未取得截图"
    if action == "bind":
        return "等待设备确认连接超时" if timeout else "设备连接已中断，未完成连接确认"
    return "未收到设备回执，执行结果未知" if timeout else "设备通道中断，执行结果未知"


def agent_disconnected(hub: Hub, agent_id: str) -> None:
    for pending in _pending.values():
        if pending.hub is hub and pending.agent_id == agent_id and not pending.future.done():
            pending.future.set_result({"ok": False, "status": 503, "error": _transport_error(pending.action)})


def simulator_instance(serial: str, platform: str, extra: dict) -> str | None:
    """Verify the managed identity emitted by the platform's own VM manager."""
    instance = extra.get("vm_instance_id")
    if (platform not in {"ios", "harmony"} or extra.get("device_kind") != "virtual"
            or extra.get("is_virtual") is not True or extra.get("vm_platform") != platform
            or not isinstance(instance, str) or not instance.strip()):
        return None
    if platform == "harmony" and serial != f"harmony-vm:{instance}":
        return None
    if platform == "ios":
        try:
            if str(UUID(serial)) != serial.lower():
                return None
        except ValueError:
            return None
    return instance


def public_platform(platform: str) -> str:
    return "ios" if platform == "ios_sim" else platform


def agent_for_device(dev, hub: Hub) -> str | None:
    if dev is None or dev.status != "online" or dev.platform not in {"android", "ios", "ios_sim", "harmony"}:
        return None
    serial = dev.serial
    agent = hub.agent_id_for_serial(serial)
    extra = hub.get_device_extra(serial)
    if not agent or agent != dev.agent_id or (extra.get("readiness") or {}).get("ready") is not True:
        return None
    if dev.platform == "android":
        if (not is_local_emulator(serial) and (serial.startswith("emulator-") or extra.get("is_virtual"))) or (
            is_network_adb(serial) and not hardware_serial(extra.get("adb_hardware_serial"))
        ):
            return None
        capability = P.CAP_UNIFIED_OPERATIONS
    elif dev.platform == "ios":
        capability = P.CAP_IOS_OBSERVATION
    elif dev.platform == "harmony":
        capability = P.CAP_HARMONY_OBSERVATION
    else:
        if not simulator_instance(serial, public_platform(dev.platform), extra):
            return None
        capability = P.CAP_IOS_OBSERVATION
    return agent if hub.agent_supports(agent, capability) else None


async def request(hub: Hub, agent_id: str, serial: str, action: str, *,
                  platform: str = "android", binding: str | None = None, instance_id: str | None = None, **params) -> dict:
    physical_ios = platform == "ios" and "transport" in params
    simulator = platform != "android" and not physical_ios and not (platform == "harmony" and instance_id is None)
    if physical_ios:
        capability, message = P.CAP_IOS_OBSERVATION, P.MSG_IOS_USB_OPERATION
    elif simulator or platform == "harmony":
        capability, message = P.CAP_MOBILE_SIMULATOR_OPERATIONS, P.MSG_MOBILE_SIMULATOR_OPERATION
    else:
        capability, message = P.CAP_UNIFIED_OPERATIONS, P.MSG_UNIFIED_OPERATION
    if hub.agent_id_for_serial(serial) != agent_id or not hub.agent_supports(agent_id, capability):
        raise HTTPException(503, "领到的设备暂不可用")
    if simulator:
        extra = hub.get_device_extra(serial)
        if not instance_id or simulator_instance(serial, platform, extra) != instance_id:
            raise HTTPException(409, "原模拟器实例已变更，请重新领取")
        if (extra.get("readiness") or {}).get("ready") is not True:
            raise HTTPException(503, "领到的模拟器尚未就绪")
    if platform != "android":
        params["platform"] = platform
        if binding is not None:
            params["binding"] = binding
    ident = secrets.token_hex(16)
    future = asyncio.get_running_loop().create_future()
    _pending[ident] = Pending(hub, agent_id, serial, future, platform, binding, action)
    timeout = MOBILE_OBSERVE_TIMEOUT if platform != "android" and action == "observe" else COMMAND_TIMEOUT
    try:
        async with asyncio.timeout(timeout + 2):
            sent = await hub.send_to_agent(agent_id, {
                "type": message,
                "request_id": ident,
                "serial": serial,
                "action": action,
                "deadline": time.time() + timeout,
                **params,
            })
            if not sent:
                raise HTTPException(503, _transport_error(action))
            result = await future
    except TimeoutError as exc:
        raise HTTPException(504, _transport_error(action, timeout=True)) from exc
    finally:
        _pending.pop(ident, None)
    if not result.get("ok"):
        raise HTTPException(result.get("status", 502), result.get("error") or "设备操作失败")
    if simulator:
        latest = hub.get_device_extra(serial)
        if hub.agent_id_for_serial(serial) != agent_id or simulator_instance(serial, platform, latest) != instance_id:
            raise HTTPException(409, "操作期间原模拟器实例已变更，请重新领取")
        if action == "bind" and (not hub.agent_supports(agent_id, capability)
                                 or (latest.get("readiness") or {}).get("ready") is not True):
            raise HTTPException(503, "领取期间模拟器操作通道已不可用")
    return result


def resolve(hub: Hub, agent_id: str, message: dict) -> None:
    pending = _pending.get(str(message.get("request_id") or ""))
    if (not pending or pending.hub is not hub or pending.agent_id != agent_id or pending.future.done()
            or pending.serial != message.get("serial")):
        return
    if pending.platform != "android" and (message.get("platform") != pending.platform
            or (pending.binding is not None and message.get("binding") != pending.binding)):
        return
    pending.future.set_result(message)


async def bind(hub: Hub, agent_id: str, serial: str, platform: str = "android") -> Target:
    if platform != "android":
        instance = simulator_instance(serial, platform, hub.get_device_extra(serial))
        result = await request(hub, agent_id, serial, "bind", platform=platform, instance_id=instance)
        binding, actions = result.get("binding"), result.get("action_types")
        if (not isinstance(binding, str) or not binding.strip() or len(binding) > 512
                or not isinstance(actions, list) or not all(isinstance(a, str) for a in actions)
                or "observe" not in actions):
            raise HTTPException(502, "模拟器未返回有效的绑定身份和观察能力")
        supported = tuple(action for action in (*READS, *MUTATIONS) if action in actions)
        return Target(agent_id, platform, supported, binding=binding, instance_id=instance)
    result = await request(hub, agent_id, serial, "bind")
    transport = result.get("transport")
    if type(transport) is not int or transport <= 0:
        raise HTTPException(502, "设备连接标识无效")
    supported = (*READS, *MUTATIONS) if result.get("android_observation") == 1 else ("screenshot", "tap")
    return Target(agent_id, "android", supported, transport=transport)


async def operate(hub: Hub, lease, action: str, **params) -> dict:
    target = lease.unified_target
    if action not in target.action_types:
        raise HTTPException(422, "本次设备绑定未提供这一操作")
    if target.transport is not None:
        if not hub.has_agent(target.agent_id):
            raise HTTPException(503, "领到的设备暂不可用，Agent 已离线")
        if hub.agent_host_id(target.agent_id) != lease.host_id:
            raise HTTPException(409, "原设备宿主已变化，请重新领取")
        if target.platform == "ios" and not hub.agent_supports(target.agent_id, P.CAP_IOS_OBSERVATION):
            raise HTTPException(503, "当前 Agent 暂未提供 iOS 主动观察与操作")
        if target.platform == "harmony" and not hub.agent_supports(target.agent_id, P.CAP_HARMONY_OBSERVATION):
            raise HTTPException(503, "当前 Agent 暂未提供鸿蒙主动观察与操作")
        if target.platform == "android" or (target.platform == "ios" and target.instance_id is None):
            params["transport"] = target.transport
    pending = asyncio.create_task(request(
        hub, target.agent_id, lease.serial, action, platform=target.platform,
        binding=target.binding, instance_id=target.instance_id, **params,
    ))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Keep the lease gate held until the dispatched operation settles.
        await asyncio.gather(pending, return_exceptions=True)
        raise


def read_geometry(result: dict) -> Screen:
    try:
        width, height, display = result["width"], result["height"], result["display"]
        # Early Agent responses can encode their positive integer dimensions as decimal strings.
        if isinstance(width, str) and width.isdecimal():
            width = int(width)
        if isinstance(height, str) and height.isdecimal():
            height = int(height)
        if not all(type(n) is int and n > 0 for n in (width, height)) or not isinstance(display, dict):
            raise ValueError("bad frame geometry")
        return Screen(secrets.token_urlsafe(12), width, height, display)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(502, "设备返回的画面尺寸或显示信息无效") from exc


def read_screen(result: dict) -> tuple[bytes, Screen]:
    screen = read_geometry(result)
    try:
        image = base64.b64decode(result["png"], validate=True)
        if (len(image) < 33 or not image.startswith(b"\x89PNG\r\n\x1a\n") or image[12:16] != b"IHDR"
                or not image.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
                or unpack_from(">II", image, 16) != (screen.width, screen.height)):
            raise ValueError("bad screenshot")
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(502, "设备返回的截图无效") from exc
    return image, screen


async def screenshot(hub: Hub, lease) -> Response:
    image, screen = read_screen(await operate(hub, lease, "screenshot"))
    lease.screen = screen
    return Response(image, media_type="image/png", headers={
        "Cache-Control": "no-store",
        "X-AI-Phone-Frame-Id": screen.ident,
        "X-AI-Phone-Width": str(screen.width),
        "X-AI-Phone-Height": str(screen.height),
    })


class Tap(BaseModel):
    frame_id: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)


def tap_screen(lease, body: Tap) -> Screen:
    screen = lease.screen
    if screen is None or body.frame_id != screen.ident:
        raise HTTPException(409, "请使用本次占用的最新截图")
    if body.x >= screen.width or body.y >= screen.height:
        raise HTTPException(422, "点击坐标超出截图范围")
    return screen


async def tap(hub: Hub, lease, body: Tap, screen: Screen) -> dict:
    if screen.observation is not None:
        # Legacy clients can keep clicking their PNG. Re-reading is required to use semantic targets again.
        lease.screen = replace(screen, observation=None)
    result = await operate(
        hub, lease, "tap", x=body.x, y=body.y,
        width=screen.width, height=screen.height, display=screen.display,
    )
    body_echo = {
        "status": "executed",
        "frame_id": body.frame_id,
        "x": body.x,
        "y": body.y,
    }
    if type(result.get("device_x")) is int and type(result.get("device_y")) is int:
        body_echo.update(device_x=result["device_x"], device_y=result["device_y"])
    return body_echo
