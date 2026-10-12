"""Android's persistent UiAutomator2 JPEG channel; no ADB screenshot fallback here."""

from __future__ import annotations

import atexit
import base64
import io

from PIL import Image


class AndroidJpegSource:
    name = "uiautomator2-jpeg"

    def __init__(self, driver, device=None):
        self.driver = driver
        self.device = device
        self.owns_device = device is None

    def initialize(self):
        if self.device is None:
            import uiautomator2 as u2
            from adbutils import adb

            if not any(d.serial == self.driver.serial for d in adb.device_list()):
                raise RuntimeError("Android device is not attached at startup")

            device = u2.Device.__new__(u2.Device)
            try:
                u2.Device.__init__(device, self.driver.serial)
            except BaseException:
                if getattr(device, "_process", None) is not None:
                    device.stop_uiautomator(wait=False)
                raise
            self.device = device
        # One startup eligibility check. Do not reuse this probe as a step frame.
        image = self.capture(95, None)
        with Image.open(io.BytesIO(image)) as im:
            if im.size != tuple(self.driver.window_size()):
                raise ValueError("Fast screenshot viewport mismatch at startup")
            im.verify()

    def capture(self, quality, max_side):
        # Scale 1 preserves full display geometry. V3's format boundary handles
        # any existing long-edge constraint; do not crop/rotate at the platform layer.
        value = self.device.jsonrpc.takeScreenshot(1, quality)
        if not isinstance(value, str) or not value:
            raise RuntimeError("UiAutomator2 JPEG screenshot unavailable")
        image = base64.b64decode(value)
        with Image.open(io.BytesIO(image)) as im:
            if im.format != "JPEG":
                raise ValueError("UiAutomator2 did not return JPEG")
        return image

    def close(self):
        if self.device is not None and self.owns_device:
            atexit.unregister(self.device.stop_uiautomator)
            self.device.stop_uiautomator(wait=False)
        self.device = None
