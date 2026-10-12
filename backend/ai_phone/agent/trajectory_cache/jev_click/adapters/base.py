from __future__ import annotations

from ..client import JevError
from ..types import Frame, image_size, tree_data


class TreeAdapter:
    model_note = ""

    def __init__(self, driver):
        self.driver = driver

    def initialize(self):
        pass

    def close(self):
        # Shared drivers and their WDA/UITest sessions belong to the main task.
        pass

    def tree(self, size):
        raise NotImplementedError

    def capture(self, screenshot_bytes, expected_image_size, window_size):
        """Reuse the current V3 frame and read its hierarchy exactly once.

        No screenshot, viewport probe or reverse hierarchy validation is added.
        Frame dimensions come from the existing V3 locate invocation.
        """
        size = image_size(screenshot_bytes)
        if size != tuple(expected_image_size) or size != tuple(window_size) or min(size) <= 0:
            raise JevError("Viewport changed since V3 step capture")
        nodes, _ = tree_data(self.tree(size), size)
        return Frame(screenshot_bytes, size, nodes)
