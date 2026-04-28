"""scrcpy 控制信道：把 touch / key / text 等事件序列化后发回手机端 server。

Vendored from leng-yue/py-scrcpy-client（MIT），改成 package 内相对 import。
"""
from __future__ import annotations

import functools
import socket
import struct
from time import sleep
from typing import TYPE_CHECKING

from . import const

if TYPE_CHECKING:  # 仅类型注解用，运行时避免循环 import
    from .core import Client


def inject(control_type: int):
    """装饰器：把 inner() 返回的 payload 前面 prepend 一个 type byte，再写入控制 socket。"""

    def wrapper(f):
        @functools.wraps(f)
        def inner(self: "ControlSender", *args, **kwargs):
            package = struct.pack(">B", control_type) + f(self, *args, **kwargs)
            sock = self.parent.control_socket
            if sock is not None:
                with self.parent.control_socket_lock:
                    sock.send(package)
            return package

        return inner

    return wrapper


class ControlSender:
    def __init__(self, parent: "Client") -> None:
        self.parent = parent

    @inject(const.TYPE_INJECT_KEYCODE)
    def keycode(self, keycode: int, action: int = const.ACTION_DOWN, repeat: int = 0) -> bytes:
        return struct.pack(">Biii", action, keycode, repeat, 0)

    @inject(const.TYPE_INJECT_TEXT)
    def text(self, text: str) -> bytes:
        buf = text.encode("utf-8")
        return struct.pack(">i", len(buf)) + buf

    @inject(const.TYPE_INJECT_TOUCH_EVENT)
    def touch(
        self,
        x: int,
        y: int,
        action: int = const.ACTION_DOWN,
        touch_id: int = 0x1234567887654321,
    ) -> bytes:
        x, y = max(int(x), 0), max(int(y), 0)
        # 注意：resolution 必须是 (w, h)；启动时由 server 给出
        w, h = self.parent.resolution or (0, 0)
        return struct.pack(">BqiiHHHii", action, touch_id, x, y, int(w), int(h), 0xFFFF, 1, 1)

    @inject(const.TYPE_INJECT_SCROLL_EVENT)
    def scroll(self, x: int, y: int, h: int, v: int) -> bytes:
        x, y = max(int(x), 0), max(int(y), 0)
        rw, rh = self.parent.resolution or (0, 0)
        return struct.pack(">iiHHii", x, y, int(rw), int(rh), int(h), int(v))

    @inject(const.TYPE_BACK_OR_SCREEN_ON)
    def back_or_turn_screen_on(self, action: int = const.ACTION_DOWN) -> bytes:
        return struct.pack(">B", action)

    @inject(const.TYPE_EXPAND_NOTIFICATION_PANEL)
    def expand_notification_panel(self) -> bytes:
        return b""

    @inject(const.TYPE_COLLAPSE_PANELS)
    def collapse_panels(self) -> bytes:
        return b""

    @inject(const.TYPE_SET_SCREEN_POWER_MODE)
    def set_screen_power_mode(self, mode: int = const.POWER_MODE_NORMAL) -> bytes:
        return struct.pack(">b", mode)

    @inject(const.TYPE_ROTATE_DEVICE)
    def rotate_device(self) -> bytes:
        return b""

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        move_step_length: int = 5,
        move_steps_delay: float = 0.005,
    ) -> None:
        """把一段直线滑动拆成多帧 ACTION_MOVE，跟原版行为一致。"""
        self.touch(start_x, start_y, const.ACTION_DOWN)
        next_x, next_y = start_x, start_y

        rw, rh = self.parent.resolution or (0, 0)
        if rw and end_x > rw:
            end_x = rw
        if rh and end_y > rh:
            end_y = rh

        decrease_x = start_x > end_x
        decrease_y = start_y > end_y
        while True:
            if decrease_x:
                next_x -= move_step_length
                if next_x < end_x:
                    next_x = end_x
            else:
                next_x += move_step_length
                if next_x > end_x:
                    next_x = end_x

            if decrease_y:
                next_y -= move_step_length
                if next_y < end_y:
                    next_y = end_y
            else:
                next_y += move_step_length
                if next_y > end_y:
                    next_y = end_y

            self.touch(next_x, next_y, const.ACTION_MOVE)
            if next_x == end_x and next_y == end_y:
                self.touch(next_x, next_y, const.ACTION_UP)
                break
            sleep(move_steps_delay)

    # 也暴露一个底层 sendbytes，以便上层手动组包（极少用到）
    def send_raw(self, data: bytes) -> None:
        sock = self.parent.control_socket
        if sock is None:
            return
        with self.parent.control_socket_lock:
            sock.send(data)

    # ---- 显式屏幕尺寸的 touch / swipe ----
    # 上面 ``touch`` / ``swipe`` 用的是 ``self.parent.resolution``（= scrcpy 编
    # 码后的视频帧尺寸，例如 max_width=720 时是 328×720），上层若直接以"设备物理
    # 像素"传 (x, y) 会被 scrcpy server 错误缩放。这里给一组接受 (screen_w, screen_h)
    # 的低阶方法，方便把"屏幕逻辑像素 (1080×2400)"原样下发。
    def touch_at(
        self,
        x: int,
        y: int,
        screen_w: int,
        screen_h: int,
        action: int = const.ACTION_DOWN,
        touch_id: int = 0x1234567887654321,
    ) -> None:
        x, y = max(int(x), 0), max(int(y), 0)
        payload = struct.pack(
            ">BqiiHHHii",
            action,
            touch_id,
            x,
            y,
            int(screen_w),
            int(screen_h),
            0xFFFF,
            1,
            1,
        )
        self.send_raw(struct.pack(">B", const.TYPE_INJECT_TOUCH_EVENT) + payload)

    def swipe_at(
        self,
        sx: int,
        sy: int,
        ex: int,
        ey: int,
        screen_w: int,
        screen_h: int,
        duration_ms: int = 300,
        steps: int = 20,
    ) -> None:
        """按时长 + 步数把直线滑动拆成 N 帧 ACTION_MOVE，整体延迟 ≈ duration_ms。"""
        steps = max(2, int(steps))
        per_sleep = max(0.0, (duration_ms / 1000.0) / steps)
        self.touch_at(sx, sy, screen_w, screen_h, action=const.ACTION_DOWN)
        for i in range(1, steps + 1):
            t = i / steps
            x = int(sx + (ex - sx) * t)
            y = int(sy + (ey - sy) * t)
            self.touch_at(x, y, screen_w, screen_h, action=const.ACTION_MOVE)
            if per_sleep:
                sleep(per_sleep)
        self.touch_at(ex, ey, screen_w, screen_h, action=const.ACTION_UP)
