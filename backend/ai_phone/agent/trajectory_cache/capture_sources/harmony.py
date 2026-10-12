"""V3-only Harmony alternatives. Borrow the driver's live UITest connection."""
from __future__ import annotations

import io
import json
import time
import types

from PIL import Image


class HarmonyFastChannel:
    name = 'harmony-native-jpeg'

    def __init__(self, driver):
        self.driver = driver

    def initialize(self):
        image = self.capture(95, None)
        with Image.open(io.BytesIO(image)) as frame:
            if frame.format != 'JPEG' or frame.size != tuple(self.driver.window_size()):
                raise ValueError('Harmony screenshot dimensions mismatch')
            frame.verify()
        tree = self.tree()
        if not isinstance(tree, dict) or not isinstance(tree.get('attributes'), dict):
            raise ValueError('Harmony direct hierarchy schema mismatch')

    def capture(self, quality, max_side):
        # Raw native JPEG; generic screenshot normalization handles resizing.
        return self.driver._raw_screenshot_bytes()

    def click(self, x, y):
        # Keep synchronous device completion. Do not retry via a second channel.
        return self.driver._call_with_reconnect(
            lambda: self.driver._raw._invoke('Driver.click', args=[int(x), int(y)])
        )

    def tree(self):
        def read():
            client = self.driver._raw._client
            original = client._recv_msg

            def receive(_client, buff_size=4096, decode=False, print=True):
                payload = bytearray()
                sock = _client.sock
                old_timeout = sock.gettimeout()
                deadline = time.monotonic() + 5.0
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Harmony hierarchy deadline')
                        sock.settimeout(remaining)
                        chunk = sock.recv(65536)
                        if not chunk:
                            raise ConnectionError('Harmony hierarchy connection closed')
                        payload.extend(chunk)
                        if len(payload) > 16 * 1024 * 1024:
                            raise ValueError('Harmony hierarchy exceeds size limit')
                        try:
                            text = payload.decode('utf-8')
                            json.loads(text)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        return text if decode else payload
                finally:
                    sock.settimeout(old_timeout)

            # _call_with_reconnect holds the shared device lock: no other
            # operation can consume this response while its reader is replaced.
            client._recv_msg = types.MethodType(receive, client)
            try:
                return client.invoke_captures('captureLayout').result
            finally:
                client._recv_msg = original
        return self.driver._call_with_reconnect(read)

    def close(self):
        # Driver/UITest lifetime belongs to the Agent, never this borrowed channel.
        pass
