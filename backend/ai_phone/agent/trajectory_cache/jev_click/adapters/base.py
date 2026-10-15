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
        tree = None
        if getattr(self, "visual_enabled", False):
            try:
                from ..visual import VisualEnhancer
                if not getattr(self, "_visual", None):
                    self._visual = VisualEnhancer(self)
                tree = self._visual.capture_tree(screenshot_bytes, size)
            except Exception:
                # Optional geometry cannot prevent original localization.
                if getattr(self, "_visual", None):
                    self._visual.observation = None
        if tree is None:
            tree = self.tree(size)
        nodes, _ = tree_data(tree, size)
        return Frame(screenshot_bytes, size, nodes)

    def enhance_frame(self, frame):
        if not getattr(self, "visual_enabled", False):
            return
        visual = getattr(self, "_visual", None)
        if visual is None:
            return
        try:
            regions, status = visual.enhance(frame)
            frame.visual_regions = regions
            frame.visual_metadata = status
        except Exception as error:
            frame.visual_metadata = {"status": "unavailable", "reason": type(error).__name__}

    def close_visual(self):
        visual, self._visual = getattr(self, "_visual", None), None
        if visual is not None:
            visual.close()
