"""Execute AI Connect operations on the Android transport owned by this Agent."""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import re
import time
from xml.etree import ElementTree as ET

from adbutils import adb
from PIL import Image

from ai_phone.ai_connect.android import local_transport_id
from ai_phone.shared import protocol as P

from . import android_observation_io as observation_io
from .device_activity import device_activity


class OperationError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def device_transport(serial: str) -> int:
    for device in adb.list(extended=True):
        if device.serial == serial and device.state == "device":
            transport = local_transport_id(serial, device.tags)
            if transport is not None:
                return transport
    raise OperationError(409, "本地 Android ADB 连接当前不可用")


def time_left(deadline: float) -> float:
    remaining = deadline - time.time()
    if remaining <= 0:
        raise OperationError(409, "请求已过期，动作未执行")
    return remaining


def capture_display(device) -> dict[str, int]:
    rotation = device.rotation()
    size = device.window_size(landscape=rotation % 2 == 1)
    return {"width": size.width, "height": size.height, "rotation": rotation}


def _png(image, display):
    with Image.open(io.BytesIO(image)) as frame:
        width, height = frame.size
        frame.verify()
    return {"png": base64.b64encode(image).decode("ascii"), "width": width, "height": height, "display": display}


def _observation_metadata(device, display, deadline):
    from . import android_webview

    warnings = []
    pages = []
    with ThreadPoolExecutor(max_workers=1) as workers:
        tree = workers.submit(observation_io.hierarchy, device, display, deadline)
        try:
            window_dump = observation_io.read_exec(device, observation_io.WINDOW, deadline, 262144).decode(errors="replace")
        except Exception as exc:
            window_dump = ""
            warnings.append(f"未取得 Android 窗口状态：{exc}")
        else:
            pages, warning = android_webview.observe(device, window_dump, deadline)
            if warning:
                warnings.append(warning)
        try:
            xml = tree.result()
        except Exception as exc:
            xml = ""
            warnings.append(f"未取得 Android 界面树：{exc}")
    return xml, window_dump, pages, warnings


def _focus(window_dump):
    return next((line.strip() for line in window_dump.splitlines()
                 if "mCurrentFocus=Window{" in line), None)


def _tree_readiness(xml, expected_text):
    try:
        nodes = ET.fromstring(xml).iter("node") if xml else ()
    except ET.ParseError:
        return False, False
    loading, expected = False, False
    for node in nodes:
        if node.get("password") == "true" or any(
            node.get(key) == "false" for key in ("visible", "visible-to-user", "displayed")
        ):
            continue
        label = node.get("text") or node.get("content-desc") or ""
        if node.get("class", "").endswith(".ProgressBar"):
            loading |= not label or label.strip().casefold() in {"加载中", "loading", "loading…", "loading..."}
        if expected_text and expected_text in label:
            expected = True
    return loading, expected


def _ready_metadata(device, display, deadline, expected_text):
    from . import android_webview

    cutoff = deadline - 1.5  # Reserve time for the single screenshot and publishing it.
    while time.time() < cutoff:
        probe_deadline = min(cutoff, time.time() + 1)
        try:
            window = observation_io.read_exec(device, observation_io.READINESS_WINDOW, probe_deadline, 65536)
            window = window.decode(errors="replace")
        except TimeoutError:
            window = ""
        focus = _focus(window)
        if focus and not ("mDisplayFrozen=true" in window or "mAppTransitionState=APP_STATE_RUNNING" in window):
            if not android_webview.loading(device, window, min(cutoff, time.time() + .55)):
                data = _observation_metadata(device, display, min(cutoff, time.time() + 5))
                xml, window_dump, pages, _ = data
                loading, expected = _tree_readiness(xml, expected_text)
                if expected_text and not expected:
                    expected = any(expected_text in (target.get("name") or "")
                                   for page in pages for target in page.targets)
                newer_focus = _focus(window_dump)
                if ((xml or pages) and not loading and (newer_focus is None or newer_focus == focus)
                        and (not expected_text or expected)):
                    return data
        time.sleep(min(.2, max(0, cutoff - time.time())))
    raise OperationError(504, "在时限内未确认页面就绪，请重新观察")


def _observe(device, deadline, include_screenshot=True, *, wait_ready=False, expected_text=None):
    from .android_observation import describe
    from ai_phone.shared.android_frame_regions import sample_original

    display = observation_io.display_state(device, deadline)
    if wait_ready:
        xml, window_dump, webview_pages, warnings = _ready_metadata(device, display, deadline, expected_text)
    else:
        metadata_deadline = min(deadline - 1, time.time() + 5)
        xml, window_dump, webview_pages, warnings = _observation_metadata(device, display, metadata_deadline)
    # Capture once, directly after the evidence read, so a slow tree recovery
    # cannot annotate a screen captured before that recovery even started.
    image = observation_io.read_exec(
        device, observation_io.screenshot_command(display), deadline, 64 * 1024 * 1024,
    )
    with Image.open(io.BytesIO(image)) as raw:
        width, height = raw.size
        raw_grid = sample_original(raw)
    if (width - height) * (display["width"] - display["height"]) < 0:
        raise OperationError(409, "截图方向与当前显示屏不一致，无法按图操作，请重新观察")
    evidence = describe(xml, image, width, height, window_dump,
                        display_size=(display["width"], display["height"]), webview_pages=webview_pages)
    if expected_text:
        labels = (*evidence["texts"], *(target.get("name") or "" for target in evidence["targets"]))
        if not any(expected_text in label for label in labels):
            raise OperationError(504, "当前帧未确认出现预期文字，请重新观察")
    result = {**evidence, "width": width, "height": height, "marked_targets": False,
              "_raw_grid_v1": raw_grid}
    if include_screenshot:
        from .android_frame_marks import annotate_frame

        marked = annotate_frame(image, evidence["targets"])
        result.update(_png(marked, display), marked_targets=bool(evidence["targets"]))
    result["display"] = {**display, "android_observation": 1}
    result["rotation"] = display["rotation"]
    if wait_ready:
        result["readiness"] = "expected_text_present" if expected_text else "no_loading_signal"
    result["warnings"] = list(evidence.get("warnings", [])) + warnings
    if not evidence["tree_available"]:
        result["structure_status"] = "partial" if evidence["targets"] else "unavailable"
    elif ({"android_tree_partial", "webview_read_unavailable"}.intersection(result["warnings"])
          or (evidence["targets"] and not any(target.get("name") for target in evidence["targets"]))):
        result["structure_status"] = "partial"
    else:
        result["structure_status"] = "available"
    return result


def _check_display(message, display):
    expected = message.get("display")
    if isinstance(expected, dict) and expected.get("android_observation") == 1:
        expected = {k: v for k, v in expected.items() if k != "android_observation"}
    if expected != display:
        if isinstance(expected, dict) and expected.get("physical_id") != display.get("physical_id"):
            raise OperationError(409, "当前显示屏已切换，请重新截图")
        raise OperationError(409, "屏幕尺寸或方向已变化，请重新截图")


def _map_point(x, y, message, display):
    width, height = message.get("width"), message.get("height")
    if (any(type(n) is not int for n in (x, y, width, height)) or
            not 0 <= x < width or not 0 <= y < height):
        raise OperationError(400, "坐标超出原图或原图尺寸无效")
    return x * display["width"] // width, y * display["height"] // height


def _scroll(device, message, display, deadline):
    _check_display(message, display)
    width, height = message.get("width"), message.get("height")
    bounds = message.get("bounds")
    if bounds is None:
        bounds = (0, 0, width, height)
    if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or
            any(type(n) is not int for n in (*bounds, width, height))):
        raise OperationError(400, "滚动区域须为原图上的矩形")
    left, top, right, bottom = bounds
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise OperationError(400, "滚动区域超出原图")
    x, y = (left + right - 1) // 2, (top + bottom - 1) // 2
    dx, dy = (right - left - 1) // 4, (bottom - top - 1) // 4
    gestures = {"down": (x, y + dy, x, y - dy), "up": (x, y - dy, x, y + dy),
                "right": (x + dx, y, x - dx, y), "left": (x - dx, y, x + dx, y)}
    gesture = gestures.get(message.get("direction"))
    if not gesture:
        raise OperationError(400, "不支持的滚动方向")
    start = _map_point(*gesture[:2], message, display)
    end = _map_point(*gesture[2:], message, display)
    if start == end:
        raise OperationError(400, "滚动区域过小")
    _command(device, ["input", "swipe", *map(str, (*start, *end)), "330"], deadline)
    return {"device_start": list(start), "device_end": list(end)}


def _command(device, args, deadline):
    result = device.shell2(args, timeout=time_left(deadline), v2=True)
    if result.returncode != 0:
        raise OperationError(502, "手机端操作命令执行失败")
    return result


def _open_app(device, package, deadline):
    if not isinstance(package, str) or len(package) > 255 or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*", package):
        raise OperationError(400, "Android 包名无效")
    intent = ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER"]
    resolved = _command(device, ["cmd", "package", "resolve-activity", "--brief", *intent, "-p", package], deadline)
    components = [line.strip() for line in resolved.output.splitlines()
                  if re.fullmatch(re.escape(package) + r"/[\w.$]+", line.strip(), re.ASCII)]
    if not components:
        raise OperationError(404, "找不到该应用的启动入口")
    launched = _command(device, ["am", "start", "-W", *intent, "-n", components[-1]], deadline)
    if re.search(r"(^\s*(?:Error|Exception)|Status:\s*(?!ok\b)\S+)", launched.output, re.M | re.I):
        raise OperationError(502, "应用启动入口未能打开")
    return {}


def execute(message: dict) -> dict:
    serial, action = message["serial"], message["action"]
    deadline = message["deadline"]
    time_left(deadline)
    with device_activity.hold(serial) as acquired:
        if not acquired:
            raise OperationError(409, "设备正在维护")
        transport = device_transport(serial)
        if action == "bind":
            return {"transport": transport, "android_observation": 1}
        if message.get("transport") != transport:
            raise OperationError(409, "Android 设备的 ADB 连接已变化，请重新领取")

        device = adb.device(transport_id=transport)
        if action == "observe":
            return _observe(
                device, deadline, include_screenshot=message.get("include_screenshot") is not False,
                wait_ready=message.get("wait_ready") is True, expected_text=message.get("expected_text"),
            )
        if action in ("back", "home"):
            _command(device, ["input", "keyevent", "KEYCODE_BACK" if action == "back" else "KEYCODE_HOME"], deadline)
            return {}
        if action == "open_app":
            return _open_app(device, message.get("package"), deadline)
        if action == "input":
            observation_io.set_focused_text(device, message.get("text"), deadline)
            return {}
        if action == "scroll":
            return _scroll(device, message, observation_io.display_state(device, deadline), deadline)
        if action == "tap" and (message.get("display") or {}).get("android_observation") == 1:
            display = observation_io.display_state(device, deadline)
        else:
            display = capture_display(device)
        if action == "screenshot":
            image = device.shell(observation_io.screenshot_command(), encoding=None, timeout=time_left(deadline))
            result = _png(image, display)
            if capture_display(device) != display:
                raise OperationError(409, "截图时屏幕发生变化，请重新截图")
            return result
        if action == "tap":
            _check_display(message, display)
            x, y = _map_point(message["x"], message["y"], message, display)
            result = device.shell2(["input", "tap", str(x), str(y)], timeout=time_left(deadline), v2=True)
            if result.returncode != 0:
                raise OperationError(502, "手机端点击命令执行失败")
            return {"device_x": x, "device_y": y}
    raise OperationError(400, "不支持的统一操作")


_tasks: set[asyncio.Task] = set()


async def _run(client, message: dict) -> None:
    reply = {
        "type": P.MSG_UNIFIED_OPERATION_RESULT,
        "request_id": message.get("request_id"),
        "serial": message.get("serial"),
    }
    try:
        result = await asyncio.to_thread(execute, message)
    except (OperationError, observation_io.UiError) as exc:
        reply.update(ok=False, status=exc.status, error=str(exc))
    except TimeoutError:
        reply.update(ok=False, status=504, error="设备操作超时，请重新观察当前画面")
    except Exception:
        reply.update(ok=False, status=502, error="设备操作中断，执行结果未知")
    else:
        reply.update(ok=True, **result)
    await client.send(reply)


async def handle(client, message: dict) -> None:
    task = asyncio.create_task(_run(client, message))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
