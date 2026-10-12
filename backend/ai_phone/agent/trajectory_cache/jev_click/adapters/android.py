"""One UiAutomator2 connection per V3 replay, on the driver's exact device."""

from .base import TreeAdapter


class AndroidAdapter(TreeAdapter):
    def __init__(self, driver):
        super().__init__(driver)
        self.device = None

    def initialize(self):
        import uiautomator2 as u2  # Optional dependency; never loaded when disabled.

        # Retain a partially initialized object too: start_uiautomator can fail
        # after launching its process, which still needs owned-resource cleanup.
        device = u2.Device.__new__(u2.Device)
        try:
            u2.Device.__init__(device, self.driver.serial)
        except BaseException:
            if getattr(device, "_process", None) is not None:
                device.stop_uiautomator(wait=False)
            raise
        self.device = device

    def tree(self, size):
        return self.device.dump_hierarchy(compressed=False, pretty=False, max_depth=100, root_in_active=True)

    def click(self, x, y):
        """Exact device-side coordinate click used by the foundation benchmark."""
        if self.device is None:
            raise RuntimeError("UiAutomator2 connection is unavailable")
        result = self.device.click(int(x), int(y))
        if result is False:
            raise RuntimeError("UiAutomator2 rejected coordinate click")

    def close(self):
        if self.device is not None:
            import atexit

            atexit.unregister(self.device.stop_uiautomator)
            # Pinned u2 3.7 owns only its _process. A reused server has _process=None.
            # wait=False avoids waiting for someone else's shared server to stop.
            self.device.stop_uiautomator(wait=False)
            self.device = None
