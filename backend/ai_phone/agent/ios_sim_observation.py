"""iOS frame assembly and touch mapping, with a CoreSimulator WDA connection."""
from __future__ import annotations

import base64
import hashlib
import io
import math
import re
import subprocess
import time
from contextlib import contextmanager
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx
import psutil
from PIL import Image

from ai_phone.shared.mobile_frame import build_frame
from .drivers.ios_simulator_driver import get_sim_endpoint
from .ios_sim_snapshot import _INPUT_TYPES, from_xctest, native_keyboard
from .mobile_sim_errors import MobileSimError

_ACTIONS = ("observe", "screenshot", "tap", "scroll", "input", "open_app", "home", "back")
_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"


def _remaining(deadline):
    left = deadline - time.time()
    if left <= 0:
        raise MobileSimError(504, "iOS 设备请求超时")
    return left


class _Wda:
    def __init__(self, client, endpoint, deadline):
        self.client = client
        self.endpoint = endpoint
        self.deadline = deadline
        self.sid = None

    @property
    def duo(self):
        return self.endpoint.duo if self.endpoint is not None else None

    def request(self, method, path, body=None):
        try:
            response = self.client.request(method, path, json=body, timeout=_remaining(self.deadline))
        except httpx.TimeoutException as exc:
            raise MobileSimError(504, "iOS 设备未在期限内返回结果") from exc
        except httpx.HTTPError as exc:
            raise MobileSimError(503, "本次占用绑定的 iOS 控制服务暂不可达") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise MobileSimError(502, "iOS 控制服务响应无效") from exc
        if not isinstance(payload, dict):
            raise MobileSimError(502, "iOS 控制服务响应无效")
        value = payload.get("value")
        error = value.get("error") if isinstance(value, dict) else None
        if error == "invalid session id":
            raise MobileSimError(409, "iOS 控制会话已更换，请重新观察当前页面")
        if error in ("no such element", "stale element reference", "element not interactable"):
            raise MobileSimError(409, "当前没有可用于此操作的 iOS 控件")
        if error == "unknown command" or response.status_code == 405:
            raise MobileSimError(501, "当前 iOS 控制服务未提供这个操作")
        if not response.is_success or error or payload.get("status") not in (None, 0):
            raise MobileSimError(502, "iOS 设备未确认本次请求")
        return payload

    def session(self, method, path, body=None):
        return self.request(method, f"/session/{quote(self.sid, safe='')}{path}", body).get("value")

    def reuse_session(self, status):
        info = status.get("value") or {}
        sid = status.get("sessionId") or info.get("sessionId")
        if not sid:
            # Creating a second session evicts the workbench's established session.
            fresh = self.request("POST", "/session", {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}})
            sid = fresh.get("sessionId") or (fresh.get("value") or {}).get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise MobileSimError(409, "iOS 设备没有就绪的控制会话")
        self.sid = sid


def boot_identity(serial, port, deadline):
    """Tie the actual TCP listener to the requested guest's launchd_sim generation."""
    try:
        listeners = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True, text=True, timeout=min(2.0, _remaining(deadline)),
        )
        pids = {int(row[1:]) for row in listeners.stdout.splitlines()
                if row.startswith("p") and row[1:].isdigit()}
        if len(pids) == 1:
            proc = psutil.Process(pids.pop())
            suffix = f"/CoreSimulator/Devices/{serial}/data/var/run/launchd_bootstrap.plist".lower()
            boot = next((p for p in proc.parents() if p.name() == "launchd_sim"
                         and any(arg.lower().endswith(suffix) for arg in p.cmdline())), None)
            if boot:
                return hashlib.sha256(f"{serial}/{boot.pid}/{boot.create_time():.6f}".encode()).hexdigest()
    except (OSError, psutil.Error, subprocess.TimeoutExpired):
        pass
    raise MobileSimError(409, "当前控制端口未绑定到所领 iOS 模拟器的运行实例")


@contextmanager
def _connection(serial, deadline, binding=None):
    endpoint = get_sim_endpoint(serial)
    if not endpoint:
        raise MobileSimError(409, "指定 iOS 模拟器没有已就绪的控制连接")
    generation = boot_identity(serial, endpoint.wda_port, deadline)
    if binding is not None and generation != binding:
        raise MobileSimError(409, "iOS 模拟器已重启，请重新领取")
    with httpx.Client(base_url=f"http://127.0.0.1:{endpoint.wda_port}", trust_env=False) as client:
        wda = _Wda(client, endpoint, deadline)
        wda.serial = serial
        status = wda.request("GET", "/status")
        info = status.get("value") or {}
        if not (isinstance(info, dict) and (info.get("ios") or {}).get("simulatorVersion")):
            raise MobileSimError(409, "控制服务没有报告 iOS 模拟器身份")
        wda.reuse_session(status)
        if binding is not None and endpoint.duo is not None:
            with _duo_foreground(wda):
                yield wda, generation
        else:
            yield wda, generation


def bind(serial: str, deadline: float) -> dict:
    with _connection(serial, deadline) as (_, identity):
        return {"binding": identity, "action_types": list(_ACTIONS)}


def _panel_agrees(window, pixels, scale):
    try:
        return (abs(float(window["x"])) < 1 and abs(float(window["y"])) < 1
                and all(abs(float(window[key]) * scale - extent) <= 2
                        for key, extent in zip(("width", "height"), pixels)))
    except (KeyError, TypeError, ValueError):
        return False


@contextmanager
def _duo_foreground(wda):
    """Scope WDA to one active app only if its viewport belongs to the lit panel."""
    setting = "defaultActiveApplication"
    wda.panel_app = None
    scale = (wda.session("GET", "/wda/screen") or {}).get("scale")
    wda.panel_scale = scale if type(scale) in (int, float) and 0 < scale <= 8 else None
    panel = wda.endpoint.duo.display(time.monotonic() + _remaining(wda.deadline))
    if panel.id != wda.endpoint.duo.capture_id:
        raise MobileSimError(409, "iPhone Duo 活动屏正在切换，请重新观察")
    app = wda.session("GET", "/wda/activeAppInfo") or {}
    window = wda.session("GET", "/window/rect")
    if _panel_agrees(window, panel.pixel_size, wda.panel_scale):
        wda.panel_app = app.get("bundleId")
        yield
        return
    # XCTest may report the cover display's SpringBoard for an app on the inner panel.
    # The list is read from WDA, never from the test scenario or a package-name preference.
    active = wda.session("GET", "/wda/apps/list")
    candidates = {item["bundleId"] for item in (active or []) if isinstance(item, dict)
                  and item.get("bundleId") and item["bundleId"] != "com.apple.springboard"}
    if app.get("bundleId") != "com.apple.springboard" or len(candidates) != 1:
        yield
        return
    candidate = candidates.pop()
    settings = wda.session("GET", "/appium/settings") or {}
    original = settings.get(setting)
    if not isinstance(original, str) or original == candidate:
        yield
        return
    try:
        wda.session("POST", "/appium/settings", {"settings": {setting: candidate}})
        selected = wda.session("GET", "/wda/activeAppInfo") or {}
        if selected.get("bundleId") == candidate and _panel_agrees(
                wda.session("GET", "/window/rect"), panel.pixel_size, wda.panel_scale):
            wda.panel_app = candidate
        yield
    finally:
        # Restore even if a read or action consumed the request deadline. Restoration is
        # scoped to the same live WDA session; it neither operates the device nor changes a successor.
        restore = _Wda(wda.client, wda.endpoint, time.time() + 2.0)
        restore.sid = wda.sid
        if restore.request("GET", "/status").get("sessionId") == wda.sid:
            current = restore.session("GET", "/appium/settings") or {}
            if current.get(setting) == candidate:
                restore.session("POST", "/appium/settings", {"settings": {setting: original}})


def _foreground_identity(wda):
    rotation = wda.session("GET", "/rotation")
    app = wda.session("GET", "/wda/activeAppInfo")
    try:
        turn = int(rotation["z"]) % 360 // 90
    except (KeyError, TypeError, ValueError) as exc:
        raise MobileSimError(409, "iOS 还没有稳定的前台显示区域") from exc
    return turn, app.get("bundleId") if isinstance(app, dict) else None


def _display(wda):
    origin_known = True
    try:
        window = wda.session("GET", "/window/rect")
    except MobileSimError as exc:
        if exc.status != 501:
            raise
        origin_known = False
        window = {"x": 0, "y": 0, **wda.session("GET", "/window/size")}
    duo = wda.duo
    # WDA's window describes the active app. A windowed iPad app occupies only
    # part of the system screenshot, while XCTest's element frames remain global.
    screen = {}
    if duo is None:
        try:
            screen = wda.session("GET", "/wda/screen") or {}
        except MobileSimError as exc:
            if exc.status not in (501, 502):
                raise
        if not isinstance(screen, dict):
            screen = {}
        if wda.endpoint is not None and not (screen.get("screenSize") or screen.get("scale")):
            # Older or partially bundled WDA products may omit the system geometry.
            # CoreSimulator's live LCD "Preferred UI Scale" is independent of the app.
            from .drivers.simctl import simulator_screen

            hardware = simulator_screen(wda.serial)
            if hardware:
                screen = {"scale": hardware.scale}
    turn, package = _foreground_identity(wda)
    try:
        size = screen.get("screenSize") or window
        pixel_scale = float(screen["scale"]) if duo is None and not screen.get("screenSize") else None
        width, height = float(size["width"]), float(size["height"])
        window_box = [float(window[k]) for k in ("x", "y", "width", "height")]
        if (not all(math.isfinite(n) for n in (width, height, *window_box))
                or min(width, height, *window_box[2:]) <= 0
                or (pixel_scale is not None and not (math.isfinite(pixel_scale) and pixel_scale > 0))):
            raise ValueError("invalid WDA logical display")
    except (KeyError, TypeError, ValueError) as exc:
        raise MobileSimError(409, "iOS 还没有稳定的前台显示区域") from exc
    display = {"wda_session": wda.sid, "width": width, "height": height, "rotation": turn,
               "window": window_box, "package": package}
    if not origin_known:
        display["unknown_window_origin"] = True
    if pixel_scale:
        display["pixel_scale"] = pixel_scale
    if duo is not None:
        try:
            panel = duo.display(time.monotonic() + _remaining(wda.deadline))
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            raise MobileSimError(504, "iPhone Duo 的活动屏未在期限内就绪") from exc
        except (RuntimeError, OSError, ValueError) as exc:
            raise MobileSimError(503, "暂时无法读取 iPhone Duo 的活动屏") from exc
        if panel.id != duo.capture_id:
            raise MobileSimError(409, "iPhone Duo 活动屏正在切换，请重新观察")
        display.update(panel_id=panel.id, panel_uuid=panel.uuid, panel_rotation=panel.rotation,
                       panel_width=panel.pixel_size[0], panel_height=panel.pixel_size[1])
        display["panel_tree"] = bool(wda.panel_app and wda.panel_app == display["package"]
                                     and _panel_agrees(window, panel.pixel_size, wda.panel_scale))
        if not display["panel_tree"]:
            display["package"] = None
    return display


def _logical_size(display, size):
    # On WDA builds that supply only scale, the observed full-screen PNG supplies the two axes.
    if scale := display.get("pixel_scale"):
        return size[0] / scale, size[1] / scale
    return display["width"], display["height"]


def _capture(wda, display):
    value = wda.session("GET", "/screenshot")
    try:
        raw = base64.b64decode(value, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            if display.get("panel_rotation") == "rot180" and image.width <= image.height:
                image = image.transpose(Image.Transpose.ROTATE_180)
            if image.format != "PNG":
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                raw = buf.getvalue()
            size = image.size
    except (TypeError, OSError, ValueError) as exc:
        raise MobileSimError(502, "iOS 设备没有返回可用截图") from exc
    if "panel_id" in display and any(abs(a-b) > 1 for a,b in zip(
            (display["panel_width"], display["panel_height"]), size)):
        raise MobileSimError(409, "iPhone Duo 的截图与活动屏不一致，请重新观察")
    # WDA may return fractional UIKit points on scaled/zoomed devices.
    width, height = _logical_size(display, size)
    if display.get("panel_tree", True) and abs(size[0] / size[1] - width / height) > .025:
        raise MobileSimError(409, "iOS 截图与控件坐标的方向或比例已变化，请重新观察")
    if display.get("unknown_window_origin") and (
            abs(display["window"][2] - width) > 1 or abs(display["window"][3] - height) > 1):
        raise MobileSimError(409, "当前 iPad 窗口原点不可用，请先将应用切到全屏")
    return raw, size


def _observe(wda, include_image):
    warnings = []
    display = _display(wda)
    xml = None
    if display.get("panel_tree", True):
        try:
            xml = wda.session("GET", "/source?format=xml")
        except MobileSimError as exc:
            if exc.status != 502:
                raise
            warnings.append("ios_tree_unavailable")
    else:
        warnings.append("ios_active_display_tree_unavailable")
    # Read XCTest evidence before taking the single image used by the returned frame.
    png, size = _capture(wda, display)
    logical = _logical_size(display, size)
    if wda.duo is not None or display["window"] != [0, 0, *logical]:
        same_display = _display(wda) == display
    else:
        same_display = _foreground_identity(wda) == (display["rotation"], display["package"])
    if not same_display:
        raise MobileSimError(409, "前台应用、方向或活动屏在取帧期间变化，请重新观察")

    nodes = []
    if xml:
        try:
            parsed, bundle = from_xctest(xml, logical, size)
        except (ET.ParseError, ValueError, TypeError):
            warnings.append("ios_tree_unavailable")
            xml = None
        else:
            if not (bundle and display["package"] and bundle != display["package"]):
                nodes = parsed
            else:
                warnings.append("ios_active_application_changed")
                xml = None
    result = build_frame(nodes, platform="ios", width=size[0], height=size[1],
                         png=png, include_image=include_image, tree_available=bool(xml),
                         display=display, package=display["package"], warnings=warnings,
                         keyboard=native_keyboard(nodes), recognize_screen_text=False)
    result["rotation"] = display["rotation"]
    if not result["tree_available"]:
        result["structure_status"] = "partial" if result["targets"] else "unavailable"
    elif result["targets"] and not any(target.get("name") for target in result["targets"]):
        result["structure_status"] = "partial"
    else:
        result["structure_status"] = "available"
    return result


def _gesture(wda, display, size, start, end=None):
    def logical(point):
        width, height = _logical_size(display, size)
        x = point[0] * width / size[0]
        y = point[1] * height / size[1]
        return x - display["window"][0], y - display["window"][1]

    duo = wda.endpoint.duo if wda.endpoint is not None else None
    if duo is not None:
        # The workbench's touch() starts a new eight-second budget. This request
        # retains its original deadline through panel lookup, helper build and simctl spawn.
        try:
            deadline = time.monotonic() + _remaining(wda.deadline)
            panel = duo.display(deadline)
            if (panel.id != duo.capture_id or panel.id != display["panel_id"]
                    or panel.uuid != display["panel_uuid"] or panel.rotation != display["panel_rotation"]):
                raise MobileSimError(409, "iPhone Duo 活动屏已改变，请重新观察")
            duration = 310 if end else 80
            if deadline - time.monotonic() <= duration / 1000:
                raise MobileSimError(504, "本次请求的剩余时间不足以完成触控")
            def panel_point(point):
                # WDA may round an odd physical dimension down by one pixel when encoding.
                return panel.native_point(*(coord * (extent - 1) / max(n - 1, 1)
                                            for coord, extent, n in zip(point, panel.pixel_size, size)))

            coords = (*panel_point(start), *panel_point(end or start))
            duo._send(deadline, "touch", panel.uuid, *(str(v) for v in coords), str(duration), "1", "100")
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            raise MobileSimError(504, "iPhone Duo 的触控没有在期限内返回") from exc
        except (RuntimeError, OSError, ValueError) as exc:
            if getattr(exc, "returncode", None) == -1:
                raise MobileSimError(504, "iPhone Duo 的触控没有在期限内返回") from exc
            raise MobileSimError(409, "iPhone Duo 未确认本次触控，请重新观察") from exc
    elif end is None:
        x, y = logical(start)
        try:
            wda.session("POST", "/wda/tap", {"x": x, "y": y})
        except MobileSimError as exc:
            if exc.status != 501:
                raise
            wda.session("POST", "/wda/tap/0", {"x": x, "y": y})
    else:
        x, y = logical(start)
        ex, ey = logical(end)
        wda.session("POST", "/actions", {"actions": [{
            "type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "origin": "viewport", "x": x, "y": y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerMove", "duration": 310, "origin": "viewport", "x": ex, "y": ey},
                        {"type": "pointerUp", "button": 0}],
        }]})
    return logical(start)


def _on_frame(wda, msg, action):
    display = _display(wda)
    size = msg.get("width"), msg.get("height")
    if display != msg.get("display"):
        raise MobileSimError(409, "前台应用、方向或活动屏已改变，请重新观察")
    if any(type(n) is not int or n <= 0 for n in size):
        raise MobileSimError(400, "缺少本帧的有效尺寸")
    if action == "tap":
        x, y = msg.get("x"), msg.get("y")
        if any(type(n) is not int for n in (x, y)) or not (0 <= x < size[0] and 0 <= y < size[1]):
            raise MobileSimError(400, "点按坐标超出本帧")
        px, py = _gesture(wda, display, size, (x, y))
        return {"device_x": px, "device_y": py}
    bounds = msg.get("bounds") or [0, 0, *size]
    if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or any(type(n) is not int for n in bounds)):
        raise MobileSimError(400, "滚动范围无效")
    left, top, right, bottom = bounds
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise MobileSimError(400, "滚动范围超出本帧")
    x, y = (left+right-1)//2, (top+bottom-1)//2
    dx, dy = (right-left-1)//4, (bottom-top-1)//4
    options = {"down": ((x, y+dy), (x, y-dy)), "up": ((x, y-dy), (x, y+dy)),
               "right": ((x+dx, y), (x-dx, y)), "left": ((x-dx, y), (x+dx, y))}
    gesture = options.get(msg.get("direction"))
    if not gesture or gesture[0] == gesture[1]:
        raise MobileSimError(400, "滚动方向或范围无效")
    _gesture(wda, display, size, *gesture)
    return {}


def _input(wda, text):
    if not isinstance(text, str) or len(text) > 8192:
        raise MobileSimError(400, "输入内容无效")
    active = wda.session("GET", "/element/active")
    element_id = (active.get(_ELEMENT_KEY) or active.get("ELEMENT")) if isinstance(active, dict) else None
    if not isinstance(element_id, str) or not element_id:
        raise MobileSimError(409, "请先聚焦 iOS 文本输入框")
    path = "/element/" + quote(element_id, safe="")
    kind = wda.session("GET", path + "/attribute/type")
    if kind not in _INPUT_TYPES:
        raise MobileSimError(409, "当前焦点不是文本输入框")
    wda.session("POST", path + "/clear", {})
    if text:
        wda.session("POST", path + "/value", {"text": text})


def execute(message: dict) -> dict:
    binding = message.get("binding")
    if not isinstance(binding, str) or not binding:
        raise MobileSimError(409, "缺少本次 iOS 模拟器绑定")
    with _connection(message["serial"], message["deadline"], binding) as (wda, _):
        return execute_wda(wda, message)


def execute_wda(wda, message):
    action = message.get("action")
    if action == "observe":
        return _observe(wda, message.get("include_screenshot", True))
    if action == "screenshot":
        display = _display(wda)
        png, size = _capture(wda, display)
        if _display(wda) != display:
            raise MobileSimError(409, "iOS 截图期间前台或活动屏发生了变化")
        return {"png": base64.b64encode(png).decode("ascii"), "width": size[0],
                "height": size[1], "display": display}
    if action in ("tap", "scroll"):
        return _on_frame(wda, message, action)
    if action == "open_app":
        bundle = message.get("package")
        if not isinstance(bundle, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+", bundle):
            raise MobileSimError(400, "iOS bundle 标识无效")
        wda.session("POST", "/wda/apps/launch", {"bundleId": bundle})
    elif action == "home":
        wda.request("POST", "/wda/homescreen", {})
    elif action == "input":
        if wda.duo is not None and not _display(wda)["panel_tree"]:
            raise MobileSimError(409, "当前无法确认 iPhone Duo 活动屏上的输入框，请重新观察")
        _input(wda, message.get("text"))
    elif action == "back":
        # iOS exposes a navigation gesture. An app may elect not to handle it.
        display = _display(wda)
        width = display.get("panel_width") or round(display["width"])
        height = display.get("panel_height") or round(display["height"])
        if "panel_id" in display:
            start, end = (2, height//2), (width//2, height//2)
        else:
            left, top, app_width, app_height = display["window"]
            if display.get("unknown_window_origin"):
                # In this legacy case a capture verifies that the active app fills the LCD.
                _capture(wda, display)
            scale = display.get("pixel_scale", 1.0)
            width, height = round(width * scale), round(height * scale)
            mid_y = round((top + app_height/2) * scale)
            start = (round((left + 2) * scale), mid_y)
            end = (round((left + app_width/2) * scale), mid_y)
        _gesture(wda, display, (width, height), start, end)
    else:
        raise MobileSimError(400, "iOS 设备未提供该操作")
    return {}
