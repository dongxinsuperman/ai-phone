"""Native direction control for the three emulator backends."""
from __future__ import annotations

import base64
import io
import re
import time

from PIL import Image

DIRECTIONS = ("portrait", "landscape_left", "portrait_upside_down", "landscape_right")
_WDA_DIRECTIONS = ("PORTRAIT", "LANDSCAPE", "", "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT")


def observe_orientation(driver, platform, requested, *, harmony_rotate, expires_at):
    """Request an absolute direction and verify the foreground render before replying."""
    target = DIRECTIONS.index(requested) if requested is not None else None
    natural_offset = 0
    if platform == "android":
        sizes = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", driver._device.shell("wm size"))
        if not sizes:
            raise RuntimeError("无法确认 Android 屏幕的自然方向")
        natural_offset = int(int(sizes[-1][0]) > int(sizes[-1][1]))
        def read_rotation():
            angle = int(driver._device.rotation())
            if angle not in range(4):
                raise RuntimeError("Android 未返回有效屏幕方向")
            return (angle - natural_offset) % 4
    elif platform == "ios_sim":
        read_rotation = driver._wda.rotation_quadrant
    elif platform == "harmony" and harmony_rotate is not None:
        read_rotation = driver.rotation
    else:
        raise RuntimeError("当前设备未启用工作台转屏")

    if time.time() >= expires_at:
        raise RuntimeError("转屏请求已过期")
    if target is not None and read_rotation() != target:
        if platform == "android":
            driver._device.shell("settings put system accelerometer_rotation 0", timeout=5)
            driver._device.shell(f"settings put system user_rotation {(target + natural_offset) % 4}", timeout=5)
        elif platform == "ios_sim":
            driver._wda.set_orientation(_WDA_DIRECTIONS[target])
        elif not harmony_rotate(driver, target, expires_at):
            raise RuntimeError("鸿蒙虚拟机归属已变更")

    deadline = min(expires_at, time.time() + 8)
    last = None
    while time.time() < deadline:
        before = read_rotation()
        with Image.open(io.BytesIO(driver.screenshot_png())) as frame:
            width, height = frame.size
            actual = read_rotation()
            native = tuple(driver.window_size())
            # Some Simulator screenshots are rounded by a single pixel.
            tolerance = 1 if platform == "ios_sim" else 0
            stable = (width != height and actual in range(4) and before == actual
                      and (width > height) == bool(actual % 2)
                      and (target is None or actual == target)
                      and all(abs(a - b) <= tolerance for a, b in zip(native, frame.size)))
            sample = (actual, native)
            if stable and sample == last:
                payload = {"orientation": DIRECTIONS[actual], "width": native[0], "height": native[1]}
                if platform != "android":
                    # A stationary MJPEG stream may suppress duplicate frames.
                    # Send a bounded current frame, with the same native geometry.
                    captured_at = time.time()
                    frame.thumbnail((1280, 1280))
                    output = io.BytesIO()
                    frame.convert("RGB").save(output, format="JPEG", quality=65)
                    payload.update(captured_at=captured_at, jpeg=base64.b64encode(output.getvalue()).decode("ascii"))
                return payload
            last = sample if stable else None
        time.sleep(0.2)
    raise RuntimeError("应用或设备未确认目标方向。请检查应用的方向锁定或重新同步。")
