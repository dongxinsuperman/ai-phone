"""Run-scoped WDA MJPEG sampling; borrow the existing control session."""
from __future__ import annotations

import io
import threading

import httpx
from PIL import Image
from loguru import logger


class IosMjpegSource:
    name = 'ios-wda-mjpeg'

    def __init__(self, driver):
        self.driver = driver
        self.condition = threading.Condition()
        self.frame = None
        self.sequence = 0
        self.stopped = False
        self.error = None
        self.forwarder = None
        self.client = None
        self.response = None
        self.thread = None
        self.saved = None
        self.settings_path = None
        self.click_restore = None

    def initialize(self):
        from ai_phone.agent.drivers.ios import _UsbmuxPortForwarder
        wda = self.driver._wda
        sid = wda._ensure_session()
        self.settings_path = f'/session/{sid}/appium/settings'
        settings = wda._request('GET', self.settings_path)['value']
        keys = ['mjpegScalingFactor', 'mjpegServerFramerate', 'mjpegServerScreenshotQuality']
        self.saved = {key: settings[key] for key in keys}
        wda._request('POST', self.settings_path, {'settings': dict(
            mjpegScalingFactor=100, mjpegServerFramerate=10, mjpegServerScreenshotQuality=80,
        )})
        self.forwarder = _UsbmuxPortForwarder(self.driver.serial, 0, 9100)
        self.forwarder.start()
        port = self.forwarder._listen_sock.getsockname()[1]
        self.client = httpx.Client(trust_env=False, timeout=2)
        self.url = f'http://127.0.0.1:{port}/'
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        frame = self.capture(95, None)
        with Image.open(io.BytesIO(frame)) as image:
            if image.size != tuple(self.driver.window_size()):
                raise ValueError('WDA MJPEG dimensions mismatch')
            image.verify()

    def _pump(self):
        try:
            with self.client.stream('GET', self.url) as response:
                self.response = response
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if self.stopped:
                        break
                    buffer.extend(chunk)
                    if len(buffer) > 16 * 1024 * 1024:
                        raise ValueError('MJPEG frame exceeds limit')
                    while True:
                        start = buffer.find(b'\xff\xd8')
                        end = buffer.find(b'\xff\xd9', start + 2) if start >= 0 else -1
                        if start < 0 or end < 0:
                            break
                        frame = bytes(buffer[start:end + 2])
                        del buffer[:end + 2]
                        with self.condition:
                            self.frame = frame
                            self.sequence += 1
                            self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = type(exc).__name__
                self.condition.notify_all()

    def capture(self, quality, max_side):
        # Require a frame received after this request, not a cached latest frame.
        with self.condition:
            sequence = self.sequence
            ready = self.condition.wait_for(
                lambda: self.sequence > sequence or self.error or self.stopped, timeout=2,
            )
            if not ready or self.error or self.stopped:
                raise RuntimeError('WDA fresh MJPEG frame unavailable')
            return self.frame

    def click(self, x, y):
        wda = self.driver._wda
        current = wda._request('GET', self.settings_path)['value']
        keys = ['waitForIdleTimeout', 'animationCoolOffTimeout']
        original = {key: current[key] for key in keys}
        self.click_restore = original
        try:
            wda._request('POST', self.settings_path, {'settings': dict(
                waitForIdleTimeout=0, animationCoolOffTimeout=0,
            )})
            self.driver.click(x, y)  # Preserve pixel-to-point mapping and completion.
        finally:
            try:
                wda._request('POST', self.settings_path, {'settings': original})
                self.click_restore = None
            except Exception as exc:
                # A completed tap must not become a failed tap and get repeated.
                logger.warning('iOS点击设置恢复失败，收尾重试：{}', type(exc).__name__)

    def close(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        try:
            if self.response is not None:
                self.response.close()
            if self.client is not None:
                self.client.close()
            if self.thread is not None:
                self.thread.join(timeout=2.5)
        finally:
            if self.forwarder is not None:
                self.forwarder.stop()
            restore = {**(self.saved or {}), **(self.click_restore or {})}
            if restore:
                self.driver._wda._request('POST', self.settings_path, {'settings': restore})
                self.saved = None
                self.click_restore = None
