"""Bounded, transport-ID-scoped Android reads and focused text input."""
from __future__ import annotations

from contextlib import closing, contextmanager
from functools import lru_cache
from http.client import HTTPConnection
from importlib.resources import files
import json
import math
import re
import secrets
import struct
import time
from xml.etree import ElementTree as ET


MAX_TREE = 8 * 1024 * 1024
_TREE_ERRORS = {
    b"AIPHONE_TREE_NO_ROOT": "无障碍服务未返回活动窗口根节点",
    b"AIPHONE_TREE_NO_FILE": "系统取到了根节点，但未写出层级文件",
    b"AIPHONE_TREE_EMPTY_FILE": "系统写出的层级文件为空",
    b"AIPHONE_STOCK_BUSY": "系统 uiautomator 未能在时限内读到界面树",
    b"AIPHONE_STOCK_EMPTY": "系统 uiautomator 未写出可读层级文件",
}
WINDOW = ("dumpsys window | grep -Ei 'mCurrentFocus=|mFocusedApp=|mObscuringWindow=|"
          "InsetsSource.*(type=ime|mType=ime)'; dumpsys input_method | grep -m 1 'mInputShown='")
READINESS_WINDOW = ("dumpsys window | grep -E 'mCurrentFocus=|mDisplayFrozen=true|"
                    "mAppTransitionState=APP_STATE_RUNNING'")
# Only borrow the shell UID's known UiAutomator2 process if it holds the actual listening socket.
U2_OWNER = r"""
ports=$(awk '$2 ~ /:2330$/ && $4 == "0A" && $8 == 2000 {print $10}' /proc/net/tcp /proc/net/tcp6 2>/dev/null)
[ -n "$ports" ] || exit 0
for pid in $(ps -A -o PID,ARGS | awk '/[c]om[.]wetest[.]uia2[.]Main/ {print $1}'); do
  [ "$(awk '/^Uid:/ {print $2}' /proc/$pid/status 2>/dev/null)" = 2000 ] || continue
  tr '\000' ' ' </proc/$pid/cmdline 2>/dev/null |
    grep -Eq '^(/system/bin/)?app_process(32|64)? / com\.wetest\.uia2\.Main( (-p|--port) 9008)? *$' || continue
  fds=$(ls -l /proc/$pid/fd 2>/dev/null)
  for inode in $ports; do
    if echo "$fds" | grep -Fq "socket:[$inode]"; then echo owned; exit 0; fi
  done
done
"""


class UiError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def remaining(deadline):
    seconds = deadline - time.time()
    if seconds <= 0:
        raise TimeoutError("设备操作超时")
    return seconds


@contextmanager
def channel(device, service, deadline):
    with device.open_transport(timeout=remaining(deadline)) as connection:
        connection.conn.settimeout(remaining(deadline))
        connection.send_command(service)
        connection.check_okay()
        yield connection.conn


def read_exec(device, command, deadline, limit=MAX_TREE):
    with channel(device, "exec:" + command, deadline) as sock:
        data = bytearray()
        while True:
            sock.settimeout(remaining(deadline))
            chunk = sock.recv(65536)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("设备返回数据过大")


def display_state(device, deadline):
    command = ("dumpsys display | grep -E 'orientation=|^[[:space:]]*m(DisplayId|PrimaryDisplayDevice)='; "
               "wm size")
    output = read_exec(device, command, deadline, 524288).decode(errors="replace")
    logical = None
    physical = None
    for line in output.splitlines():
        field = line.strip()
        if field.startswith("mDisplayId="):
            logical = field.partition("=")[2].strip()
        elif logical == "0" and field.startswith("mPrimaryDisplayDevice="):
            match = re.search(r"\(\s*local:([0-9]{1,20})\s*\)", field)
            if match:
                physical = match[1]
                break
    default = ""
    conflicting_viewport = False
    for viewport in re.findall(r"DisplayViewport\{([^}]*)\}", output):
        if not re.search(r"\bdisplayId=0\b", viewport) or "valid=false" in viewport or "isActive=false" in viewport:
            continue
        local = re.search(r"""\buniqueId=['"]?local:([0-9]{1,20})\b""", viewport)
        if local and physical and local[1] != physical:
            conflicting_viewport = True
            continue
        if local and not physical:
            physical = local[1]
        default = viewport
        break
    if conflicting_viewport and not default:
        raise UiError(409, "默认显示屏正在切换，请重新观察")
    rotation = re.search(r"\borientation=([0-3])\b", default or output)
    dimensions = re.search(r"Override size:\s*(\d+)x(\d+)", output) or re.search(
        r"Physical size:\s*(\d+)x(\d+)", output)
    if not rotation or not dimensions:
        raise UiError(502, "读取当前屏幕方向或尺寸失败")
    w, h = map(int, dimensions.groups())
    value = int(rotation[1])
    if value % 2:
        w, h = h, w
    if min(w, h) < 1:
        raise UiError(502, "设备返回的屏幕尺寸无效")
    state = {"width": w, "height": h, "rotation": value}
    if physical is not None:
        state["physical_id"] = physical  # SurfaceFlinger uses an unsigned 64-bit ID.
    return state


def screenshot_command(display=None):
    physical = (display or {}).get("physical_id")
    # Only the device's parsed numeric ID is interpolated. Warnings may be written even on success.
    if isinstance(physical, str) and re.fullmatch(r"[0-9]{1,20}", physical):
        return f"screencap -p -d {physical} 2>/dev/null"
    return "screencap -p 2>/dev/null"


def _exact(sock, count, deadline):
    data = bytearray()
    while len(data) < count:
        sock.settimeout(remaining(deadline))
        part = sock.recv(count - len(data))
        if not part:
            raise EOFError("设备通道提前关闭")
        data.extend(part)
    return bytes(data)


@lru_cache(maxsize=1)
def _jar():
    return files("ai_phone.agent").joinpath("android_io_assets", "ui-operation.jar").read_bytes()


def _push_jar(device, path, deadline):
    jar = _jar()
    destination = (path + ",33024").encode()  # S_IFREG | 0400; Android requires read-only dynamic code.
    with channel(device, "sync:", deadline) as sock:
        data = (b"SEND" + struct.pack("<I", len(destination)) + destination +
                b"DATA" + struct.pack("<I", len(jar)) + jar + b"DONE" + struct.pack("<I", int(time.time())))
        sock.sendall(data)
        if _exact(sock, 8, deadline)[:4] != b"OKAY":
            raise UiError(502, "设备拒绝写入本次临时无障碍助手")


def _cleanup(device, paths, deadline):
    try:
        read_exec(device, "rm -f " + paths, deadline, 1024)
    except Exception:
        pass  # The remote shell has its own EXIT trap if the host disconnects.


def _helper(device, request, deadline):
    path = "/data/local/tmp/aiphone-observe-" + secrets.token_hex(12) + ".jar"
    paths = path
    if request["action"] == "tree":
        xml_path = path[:-4] + ".xml"
        request = {**request, "output": xml_path}
        paths += " " + xml_path
    payload = (json.dumps(request, ensure_ascii=True) + "\n").encode()
    # The Android watchdog and shell EXIT trap also run if the host disconnects.
    operation_deadline = deadline - .25
    seconds = max(1, math.ceil(remaining(operation_deadline)))
    command = (f"umask 077; trap 'rm -f {paths}' EXIT; trap 'exit 1' HUP TERM INT; "
               f"CLASSPATH={path}:/system/framework/uiautomator.jar "
               f"timeout -s KILL {seconds} app_process / org.aiphone.observation.UiOperation")
    try:
        _push_jar(device, path, operation_deadline)
        with channel(device, "shell,v2,raw:" + command, operation_deadline) as sock:
            sock.sendall(struct.pack("<BI", 0, len(payload)) + payload + struct.pack("<BI", 4, 0))
            out, err = bytearray(), bytearray()
            while True:
                kind, length = struct.unpack("<BI", _exact(sock, 5, operation_deadline))
                if length > MAX_TREE or len(out) + len(err) + length > MAX_TREE:
                    raise ValueError("设备助手返回数据过大")
                part = _exact(sock, length, operation_deadline)
                if kind == 1:
                    out.extend(part)
                elif kind == 2:
                    err.extend(part)
                elif kind == 3:
                    if part != b"\x00":
                        for line in bytes(err).splitlines():
                            if line in _TREE_ERRORS:
                                raise UiError(503, _TREE_ERRORS[line])
                        reason = re.search(rb"AIPHONE_UI_ERROR ([a-zA-Z]+)", err)
                        detail = reason[1].decode() if reason else "系统工具不兼容或已超时"
                        raise UiError(503, "Android 一次性无障碍助手不可用：" + detail)
                    return out.decode("utf-8")
    finally:
        # Covers failures between sync SEND and starting the self-cleaning shell.
        _cleanup(device, paths, deadline)


def _existing_http(device, path, deadline, body=None):
    # The ADB tcp service connects to localhost on the *bound transport*. No forward or listener is created.
    with channel(device, "tcp:9008", deadline) as sock, closing(HTTPConnection("localhost")) as http:
        http.sock = sock
        http.request("POST" if body else "GET", path, body=body, headers={"Content-Type": "application/json"})
        response = http.getresponse()
        if response.status != 200:
            raise ValueError("无障碍接口未接受请求")
        data = bytearray()
        while not response.isclosed():
            sock.settimeout(remaining(deadline))
            part = response.read1(65536)
            if not part:
                break
            data.extend(part)
            if len(data) > MAX_TREE:
                raise ValueError("无障碍接口返回数据过大")
        return bytes(data)


def _existing(device, deadline):
    try:
        if _existing_http(device, "/ping", min(deadline, time.time() + .45)) != b"pong":
            return False
        return read_exec(device, U2_OWNER, min(deadline, time.time() + .9), 1024).strip() == b"owned"
    except Exception:
        return False


def _rpc(device, method, args, deadline):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": args}).encode()
    reply = json.loads(_existing_http(device, "/jsonrpc/0", deadline, payload))
    if reply.get("id") != 1 or "error" in reply or "result" not in reply:
        raise UiError(502, "设备无障碍接口未确认 " + method)
    return reply["result"]


def _xml(raw):
    start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < start:
        raise ValueError("系统未返回活动窗口层级")
    xml = raw[start:end + len("</hierarchy>")]
    if not list(ET.fromstring(xml).iter("node")):
        raise ValueError("活动窗口层级为空")
    return xml


def _stock_tree(device, deadline):
    path = "/data/local/tmp/aiphone-observe-" + secrets.token_hex(12) + ".xml"
    budget = deadline - .2
    seconds = max(1, int(remaining(budget)))
    command = (f"umask 077; trap 'rm -f {path}' EXIT; trap 'exit 1' HUP TERM INT; "
               f'msg=$(timeout -s KILL {seconds} uiautomator dump {path} 2>&1); code=$?; '
               f'if [ -s {path} ]; then cat {path}; else '
               'case "$code:$msg" in *"null root"*) echo AIPHONE_TREE_NO_ROOT;; '
               '124:*|137:*|*"idle state"*) echo AIPHONE_STOCK_BUSY;; '
               '*) echo AIPHONE_STOCK_EMPTY;; esac; fi')
    try:
        raw = read_exec(device, command, budget)
        reason = _TREE_ERRORS.get(raw.strip())
        if reason:
            raise ValueError(reason)
        return _xml(raw.decode(errors="replace"))
    finally:
        _cleanup(device, path, deadline)


def hierarchy(device, display, deadline):
    if _existing(device, deadline):
        try:
            return _xml(_rpc(device, "dumpWindowHierarchy", [False, 100, True], min(deadline, time.time() + 2)))
        except Exception:
            # Only a read was attempted; the phone's own one-shot reader can still provide the tree.
            pass
    try:
        return _xml(_helper(device, {"action": "tree", **display}, min(deadline, time.time() + 2.3)))
    except Exception as first:
        if deadline - time.time() < 1.3:
            raise
        try:
            return _stock_tree(device, deadline)
        except Exception as fallback:
            raise ValueError(f"{first}；{fallback}") from fallback


def _focused_field(device, deadline):
    xml = _xml(_rpc(device, "dumpWindowHierarchy", [False, 100, True], deadline))
    fields = [n for n in ET.fromstring(xml).iter("node") if n.get("focused") == "true"
              and n.get("enabled") != "false" and
              (n.get("editable") == "true" or n.get("class", "").endswith(("EditText", "AutoCompleteTextView")))]
    if len(fields) != 1:
        raise UiError(409, "当前没有唯一已聚焦的可编辑框")
    return fields[0]


def _borrowed_input(device, text, deadline):
    field = _focused_field(device, deadline)
    # UiSelector's wire masks, as used by uiautomator2; the host package is optional.
    selector = {"mask": 0x20000 | 0x8000, "focused": True, "enabled": True,
                "childOrSibling": [], "childOrSiblingSelector": []}
    identity = (("class", "className", 0x10), ("package", "packageName", 0x80000),
                ("resource-id", "resourceId", 0x200000))
    for attr, key, mask in identity:
        if field.get(attr):
            selector[key] = field.get(attr)
            selector["mask"] |= mask
    if text:
        if _rpc(device, "setText", [selector, text], deadline) is not True:
            raise UiError(409, "已聚焦编辑框拒绝设置文本")
        if field.get("password") == "true":
            return
    else:
        # clearTextField is void (JSON null). Only the following readback confirms the content.
        if _rpc(device, "clearTextField", [selector], deadline) is not None:
            raise UiError(502, "设备无障碍接口未返回有效的清空回执")
    actual = _rpc(device, "getText", [selector], deadline)
    if actual == text:
        return
    if not text:
        after = _focused_field(device, deadline)
        same_field = all(field.get(attr) == after.get(attr) for attr, _, _ in identity)
        if not field.get("resource-id"):
            same_field = same_field and field.get("bounds") == after.get("bounds")
        shown_hint = any(after.get(key) == "true" for key in ("showing-hint-text", "showingHintText"))
        hint_transition = (actual and actual == after.get("hint") == after.get("text")
                           and actual == field.get("hint") and actual != field.get("text"))
        if same_field and (after.get("text") == "" or shown_hint or hint_transition):
            return
    raise UiError(502, "编辑框已收到输入，但未回读到请求的文本")


def set_focused_text(device, text, deadline):
    if not isinstance(text, str) or len(text) > 8192:
        raise UiError(400, "输入须为不超过 8192 字符的文本")
    if _existing(device, deadline):
        _borrowed_input(device, text, deadline)
        return
    output = _helper(device, {"action": "input", "text": text}, deadline).splitlines()
    if "AIPHONE_TEXT_SET" in output:
        return
    if "AIPHONE_NO_FOCUSED_EDITOR" in output:
        raise UiError(409, "当前没有已聚焦的可编辑框")
    if "AIPHONE_SET_TEXT_REJECTED" in output:
        raise UiError(409, "已聚焦编辑框拒绝设置文本")
    raise UiError(502, "编辑框已收到输入，但未回读到请求的文本")
