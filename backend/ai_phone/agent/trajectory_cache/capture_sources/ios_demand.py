"""V3 iOS demand screenshots; no persistent video stream."""
import io

from PIL import Image

from .ios import IosMjpegSource


class IosDemandSource(IosMjpegSource):
    name = 'ios-wda-demand'

    def initialize(self):
        sid = self.driver._wda._ensure_session()
        self.settings_path = f'/session/{sid}/appium/settings'
        with Image.open(io.BytesIO(self.capture(95, None))) as image:
            if image.size != tuple(self.driver.window_size()):
                raise ValueError("WDA screenshot dimensions mismatch")
            image.verify()

    def capture(self, quality, max_side):
        return self.driver.screenshot_jpeg(quality=quality, max_side=max_side)
