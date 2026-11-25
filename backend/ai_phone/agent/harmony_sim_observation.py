"""Unified observations and actions on a leased, managed HarmonyOS simulator."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import shlex
import tempfile
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .drivers.hdc import HdcError, hdc_file_recv, hdc_shell
from .harmony_sim_tree import native_page, rect
from .harmony_vm.registry import managed_vm_owner, resolve_harmony_serial
from .mobile_sim_errors import MobileSimError


ACTION_TYPES = ("observe", "screenshot", "tap", "scroll", "back", "home", "open_app")
_BOOT_FILE = "/proc/sys/kernel/random/boot_id"
_BOOT = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_BOOT_EVENTS = re.compile(r"events:[1-9][0-9]{0,15}:[1-9][0-9]{0,15}:[1-9][0-9]{0,15}")
_READ_BOOT_EVENTS = (
    '_ai_event() { _ai_value=$(param get "$1") || return 1; set -f; set -- $_ai_value; '
    '[ "$#" -eq 1 ] || return 1; case "$1" in *[!0-9]*) return 1;; esac; printf "%s" "$1"; }; '
    "printf 'events:%s:%s:%s' "
    '"$(_ai_event ohos.boot.time.kernel)" "$(_ai_event ohos.boot.time.init)" '
    '"$(_ai_event ohos.boot.time.boot.completed)"'
)
_BUNDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,254}")
_ABILITY = re.compile(r"[A-Za-z0-9_$][A-Za-z0-9_.$]{0,254}")
_MAX_FILE = 48 * 1024 * 1024


def _remaining(deadline):
    left = deadline - time.time()
    if left <= 0:
        raise MobileSimError(504, "鸿蒙模拟器操作超时")
    return left


@dataclass(frozen=True)
class _Guest:
    serial: str
    address: str
    lease: str
    boot: str

    @property
    def binding(self):
        data = "\0".join((self.serial, self.address, self.lease, self.boot)).encode()
        return hashlib.sha256(data).hexdigest()

    def check(self):
        if managed_vm_owner(self.address) != (
            self.serial.removeprefix("harmony-vm:"),
            self.lease,
        ):
            raise MobileSimError(409, "鸿蒙模拟器的运行实例已更换，请重新领取")

    def shell(self, command, deadline):
        self.check()
        # Check the actual boot in the same guest shell as the action. A reused
        # HDC port must not send a late tap or cleanup to a different machine.
        read_boot = _READ_BOOT_EVENTS if self.boot.startswith("events:") else f"cat {_BOOT_FILE}"
        guard = (
            f'if [ "$({read_boot})" = "{self.boot}" ]; then '
            f"{command}; _ai_guest_status=$?; "
            'printf "\\n__AI_PHONE_STATUS=%s\\n" "$_ai_guest_status"; '
            "else echo __AI_PHONE_BOOT_CHANGED; fi"
        )
        try:
            output = hdc_shell(self.address, guard, timeout=_remaining(deadline))
        except HdcError as exc:
            raise MobileSimError(504 if exc.returncode == -1 else 502, "鸿蒙设备通道未返回执行回执") from None
        self.check()
        if output.strip().endswith("__AI_PHONE_BOOT_CHANGED"):
            raise MobileSimError(409, "鸿蒙模拟器已经重启，请重新领取")
        body, sep, status = output.rpartition("__AI_PHONE_STATUS=")
        if not sep or status.strip() != "0":
            raise MobileSimError(502, "鸿蒙设备命令没有成功回执")
        return body.rstrip()


def _guest(serial, deadline):
    if not isinstance(serial, str) or not serial.startswith("harmony-vm:") or serial == "harmony-vm:":
        raise MobileSimError(409, "仅接受 AI Phone 已登记的鸿蒙模拟器实例")
    try:
        address = resolve_harmony_serial(serial)
    except RuntimeError:
        raise MobileSimError(409, "鸿蒙模拟器当前没有运行实例") from None
    owner = managed_vm_owner(address)
    if not owner or owner[0] != serial.removeprefix("harmony-vm:"):
        raise MobileSimError(409, "鸿蒙模拟器的连接归属已经变化")
    try:
        boot = hdc_shell(address, f"cat {_BOOT_FILE}", timeout=_remaining(deadline)).strip()
    except HdcError:
        boot = ""
    if not _BOOT.fullmatch(boot):
        # On newer Harmony images hdc shell cannot read Linux's kernel UUID.
        # These boot-event microsecond timestamps are published by the guest at
        # startup. Use all three in addition to the Agent's managed launch ID.
        try:
            boot = hdc_shell(address, _READ_BOOT_EVENTS, timeout=_remaining(deadline)).strip()
        except HdcError:
            raise MobileSimError(503, "无法读取鸿蒙模拟器当前启动标识") from None
        if not _BOOT_EVENTS.fullmatch(boot):
            raise MobileSimError(503, "无法确认鸿蒙模拟器当前启动标识")
    guest = _Guest(serial, address, owner[1], boot)
    guest.check()
    return guest


def bind(serial: str, deadline: float) -> dict:
    guest = _guest(serial, deadline)
    return {"binding": guest.binding, "action_types": list(ACTION_TYPES)}


def _fetch(guest, remote, local, deadline):
    guest.check()
    try:
        hdc_file_recv(guest.address, remote, str(local), timeout=_remaining(deadline))
        if local.is_file() and 0 < local.stat().st_size <= _MAX_FILE:
            return local.read_bytes()
    except (OSError, HdcError):
        pass
    return None


def _png(raw):
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            size = image.size
            if image.format not in ("PNG", "JPEG") or not 1 <= size[0] * size[1] <= 20_000_000:
                return None
            if image.format == "PNG":
                image.verify()
                return raw, size
            result = io.BytesIO()
            image.convert("RGB").save(result, format="PNG")
            return result.getvalue(), size
    except (OSError, ValueError):
        return None


def _capture(guest, deadline, *, tree):
    # Older platform versions also have snapshot_display. Its JPEG is losslessly
    # converted to PNG after decoding, using its own actual display dimensions.
    prefix = "/data/local/tmp/aiphone-sim-" + secrets.token_hex(10)
    names = ("first", "last") if tree else ("last",)
    commands, paths = [], []
    for name in names:
        png, jpg = f"{prefix}-{name}.png", f"{prefix}-{name}.jpeg"
        paths.extend((png, jpg))
        if name == "last" and tree:
            paths.append(f"{prefix}.json")
            commands.append(f"uitest dumpLayout -p {prefix}.json >/dev/null 2>&1")
        commands.append(f"uitest screenCap -p {png} >/dev/null 2>&1")
        commands.append(f"test -s {png} || snapshot_display -f {jpg} >/dev/null 2>&1")
    # Return only the names known to exist. A missing tree must not discard a
    # good screenshot, and missing screenshots can still permit a native frame.
    commands.append("for f in " + " ".join(paths) + '; do if [ -s "$f" ]; then echo "$f"; fi; done')
    try:
        available = set(guest.shell("; ".join(commands), deadline).splitlines())
        with tempfile.TemporaryDirectory(prefix="aiphone-hm-frame-") as tmp:

            def read(path):
                if path in available:
                    return _fetch(guest, path, Path(tmp) / Path(path).name, deadline)
                return None

            def image(name):
                return _png(read(f"{prefix}-{name}.png") or read(f"{prefix}-{name}.jpeg"))

            first = image("first") if tree else None
            raw = read(f"{prefix}.json") if tree else None
            last = image("last")
            try:
                parsed = json.loads(raw) if raw else None
            except (ValueError, UnicodeError):
                parsed = None
            return first, parsed, last
    finally:
        try:
            guest.shell("rm -f " + " ".join(paths), time.time() + 0.8)
        except MobileSimError:
            pass


def _viewport(raw):
    if isinstance(raw, dict) and isinstance(raw.get("attributes"), dict):
        a = raw["attributes"]
        box = rect(a.get("bounds"))
        if box and box[:2] == (0, 0) and not a.get("type"):
            return box[2:]
    return None


def _transition(first, last):
    if first[1] != last[1]:
        return True
    if first[0] == last[0]:
        return False

    def sample(png):
        with Image.open(io.BytesIO(png)) as image:
            return np.asarray(
                image.convert("RGB").resize((64, 64), Image.Resampling.BOX),
                dtype=np.int16,
            )

    changed = np.abs(sample(first[0]) - sample(last[0])).max(axis=2) >= 36
    # Require significant changes across independent screen areas. Clocks,
    # cursors, and isolated playing thumbnails do not hold the entire frame.
    tiles = changed.reshape(4, 16, 4, 16).mean(axis=(1, 3))
    return int((tiles >= 0.25).sum()) >= 4


def _observe(guest, message):
    from ai_phone.shared.mobile_frame import build_frame

    deadline = message["deadline"]
    warnings = []
    for attempt in range(2):
        first, raw, last = _capture(guest, deadline, tree=True)
        viewport = _viewport(raw)
        if last and raw and ((viewport and last[1] != viewport) or (first and _transition(first, last))):
            if not attempt and deadline - time.time() > 2:
                continue
            raw = None
            warnings.append("鸿蒙系统树与转场画面无法对齐，本帧只采用画面证据")
        break
    guest.check()
    if last:
        png, (width, height) = last
    elif viewport and message.get("include_screenshot", True) is False:
        png, (width, height) = None, viewport
        warnings.append("鸿蒙截图暂不可用，本帧只采用系统树")
    else:
        raise MobileSimError(502, "鸿蒙模拟器未返回有效画面尺寸")
    nodes, package, activity, valid = native_page(raw, width, height, warnings=warnings, screenshot=png)
    if not valid and not warnings:
        warnings.append("鸿蒙 UiTest 未提供当前页面的系统树")
    display = {"width": width, "height": height}
    return build_frame(
        nodes,
        platform="harmony",
        width=width,
        height=height,
        png=png,
        include_image=message.get("include_screenshot", True),
        package=package,
        activity=activity,
        tree_available=valid,
        warnings=warnings,
        display=display,
    )


def _screenshot(guest, deadline):
    _first, _tree, image = _capture(guest, deadline, tree=False)
    if not image:
        raise MobileSimError(502, "鸿蒙模拟器未返回有效截图")
    raw, (w, h) = image
    return {
        "png": base64.b64encode(raw).decode("ascii"),
        "width": w,
        "height": h,
        "display": {"width": w, "height": h},
    }


def _checked_size(guest, message):
    w, h, display = message.get("width"), message.get("height"), message.get("display")
    if type(w) is not int or type(h) is not int or display != {"width": w, "height": h}:
        raise MobileSimError(409, "请使用当前鸿蒙画面的坐标")
    _first, _tree, image = _capture(guest, message["deadline"], tree=False)
    if not image or image[1] != (w, h):
        raise MobileSimError(409, "鸿蒙显示区域已变化，请重新观察")
    return w, h


def _entry_ability(output):
    try:
        start, end = output.index("{"), output.rindex("}") + 1
        info = json.loads(output[start:end])
    except (ValueError, AttributeError):
        return None
    if not isinstance(info, dict):
        return None
    entries = []
    for module in info.get("hapModuleInfos") or []:
        if not isinstance(module, dict):
            continue
        for ability in module.get("abilityInfos") or []:
            if not isinstance(ability, dict):
                continue
            name = ability.get("name")
            launcher = ability.get("isLauncherAbility") is True or any(
                "action.system.home" in (skill.get("actions") or [])
                for skill in (ability.get("skills") or [])
                if isinstance(skill, dict)
            )
            if (
                isinstance(name, str)
                and _ABILITY.fullmatch(name)
                and (launcher or name == module.get("mainAbility"))
            ):
                entries.append((launcher, module.get("moduleName") == info.get("mainEntry"), name))
    return max(entries)[2] if entries else None


def _act(guest, message):
    action, deadline = message["action"], message["deadline"]
    if action in ("tap", "scroll"):
        w, h = _checked_size(guest, message)
        if action == "tap":
            x, y = message.get("x"), message.get("y")
            if type(x) is not int or type(y) is not int or not (0 <= x < w and 0 <= y < h):
                raise MobileSimError(422, "坐标超出了鸿蒙画面")
            command = f"uitest uiInput click {x} {y}"
        else:
            box = rect(message.get("bounds")) if message.get("bounds") is not None else (0, 0, w, h)
            if not box or not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h):
                raise MobileSimError(422, "滚动区域超出了鸿蒙画面")
            left, top, right, bottom = box
            x, y = (left + right - 1) // 2, (top + bottom - 1) // 2
            dx, dy = (right - left) // 4, (bottom - top) // 4
            swipes = {
                "down": (x, y + dy, x, y - dy),
                "up": (x, y - dy, x, y + dy),
                "left": (x - dx, y, x + dx, y),
                "right": (x + dx, y, x - dx, y),
            }
            swipe = swipes.get(message.get("direction"))
            if not swipe or swipe[:2] == swipe[2:]:
                raise MobileSimError(422, "鸿蒙滚动方向或区域无效")
            command = "uitest uiInput swipe " + " ".join(map(str, swipe)) + " 700"
    elif action in ("home", "back"):
        command = "uitest uiInput keyEvent " + ("Home" if action == "home" else "Back")
    elif action == "open_app":
        package = message.get("package")
        if not isinstance(package, str) or not _BUNDLE.fullmatch(package):
            raise MobileSimError(422, "请提供有效的鸿蒙应用 bundleName")
        ability = _entry_ability(guest.shell(f"bm dump -n {package}", deadline))
        command = f"aa start -b {package}" + (f" -a {shlex.quote(ability)}" if ability else "")
    else:
        raise MobileSimError(400, "这台鸿蒙模拟器没有提供该操作")
    answer = guest.shell(command, deadline)
    # The shell exit code alone is insufficient: UiTest also exits zero on some errors.
    # Successful injection emits "No Error"; aa has its own launch acknowledgement.
    lines = [line.strip().lower() for line in answer.splitlines() if line.strip()]
    diagnostics = "\n".join(line for line in lines if line != "no error")
    if re.search(
        r"\b(?:unknown command|invalid argument|invalid parameter|command not found|usage:|specified ability does not exist)",
        diagnostics,
    ):
        raise MobileSimError(422, "鸿蒙系统工具拒绝了这个动作")
    if re.search(r"(?:\[fail\]|\berror\b|\bfailed\b)", diagnostics):
        raise MobileSimError(502, "鸿蒙设备报告动作错误，执行结果未知")
    acknowledged = (
        any(line.rstrip(".") == "start ability successfully" for line in lines)
        if action == "open_app"
        else "no error" in lines
    )
    if not acknowledged:
        raise MobileSimError(502, "鸿蒙模拟器未确认动作执行")
    return {"device_x": x, "device_y": y} if action == "tap" else {}


def execute(message: dict) -> dict:
    serial, deadline = message["serial"], message["deadline"]
    if message["action"] == "bind":
        return bind(serial, deadline)
    guest = _guest(serial, deadline)
    if message.get("binding") != guest.binding:
        raise MobileSimError(409, "鸿蒙模拟器已更换或重启，请重新领取")
    if message["action"] == "observe":
        return _observe(guest, message)
    if message["action"] == "screenshot":
        return _screenshot(guest, deadline)
    return _act(guest, message)
