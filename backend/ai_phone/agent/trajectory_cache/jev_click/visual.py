"""Read-only bridge to Jev Phone 0.2.0 drawing enhancement.

V3 retains screenshot, action, click, wait and rescue ownership. Providers never
click or screenshot here. Candidate IDs and click geometry remain host-owned.
"""
from copy import deepcopy
from xml.etree import ElementTree as ET

from .visual_vendor.settings import visual_enhancement_enabled
from .visual_vendor.adapters.base import Observation


class VisualEnhancer:
    def __init__(self, host):
        self.host = host
        self.provider = None
        self.observation = None

    def _provider(self):
        if not visual_enhancement_enabled():
            return None
        if self.provider is not None:
            return self.provider
        host = self.host
        platform = host.driver.platform
        if platform == "android":
            from .visual_vendor.adapters.android import AndroidAdapter
            self.provider = AndroidAdapter(host.driver.serial, device=host.device)
        elif platform in {"ios", "ios_sim"}:
            from .visual_vendor.adapters.ios import IOSAdapter
            wda = host.driver._wda
            self.provider = IOSAdapter(wda.base_url, session_id=wda._ensure_session(), udid=host.driver.serial)
        elif platform == "harmony":
            from .visual_vendor.adapters.harmony import HarmonyAdapter
            # Reconnect may replace the shared raw driver. Resolve it per call.
            host.driver._call_with_reconnect(lambda: None)
            self.provider = HarmonyAdapter(host.driver.serial, driver=host.driver._raw)
        return self.provider

    def capture_tree(self, image, size):
        """Replace the one tree read where native geometry needs one snapshot."""
        self.observation = None
        if not visual_enhancement_enabled():
            return self.host.tree(size)
        provider = self._provider()
        platform = self.host.driver.platform
        if platform == "android":
            tree = self.host.tree(size)
            self.observation = Observation(tree, image, size)
            return tree
        if platform in {"ios", "ios_sim"}:
            source = provider.source()
            app = next(n for n in ET.fromstring(source).iter()
                       if n.get("type") == "XCUIElementTypeApplication")
            viewport = (float(app.get("width", 0)), float(app.get("height", 0)))
            tree, meta = provider.convert(source, image, viewport)
            meta.update(_native_snapshot=provider._native_snapshot, _native_capture=provider._native_capture)
            provider._native_snapshot = None
            self.observation = Observation(tree, image, viewport, meta)
            # Keep AI Phone's converter, candidate order and click coordinates.
            return self.host.convert(source, size)
        if platform == "harmony":
            # Preserve the host reconnect wrapper for original captureLayout fallback.
            provider.driver = self.host.driver._raw
            provider.raw_tree = lambda: self.host.read_raw_tree()
            tree = provider.source()
            raw = provider._visual_raw
            provider.driver = self.host.driver._raw
            # Same handoff as SDK HarmonyAdapter.observe: enhancement consumes
            # the original tree from metadata, not the converted XML alone.
            metadata = {"_visual_raw": raw, "_native_capture": provider._native_capture}
            provider._visual_raw = None
            self.observation = Observation(tree, image, size, metadata)
            return self.host.convert(raw)
        return self.host.tree(size)

    def enhance(self, frame):
        if not visual_enhancement_enabled():
            return {}, {"status": "disabled"}
        if self.observation is None:
            return {}, {"status": "unavailable"}
        provider = self._provider()
        original = deepcopy(frame.candidates)
        # Providers are given copies, never the execution candidates.
        regions = provider.enhance_annotation(self.observation, deepcopy(original))
        from .visual_vendor.visual_channels import safe_regions
        regions = safe_regions(regions, original, frame.size)
        assert frame.candidates == original
        return regions, self.observation.metadata.get("visual_enhancement", {})

    def close(self):
        provider, self.provider = self.provider, None
        self.observation = None
        if provider is None:
            return
        # Never stop shared WDA, UiAutomator or UITest resources.
        channel = getattr(provider, "_visual_channel", None)
        if channel is not None:
            channel.close()
            provider._visual_channel = None
        if getattr(provider, "owns_client", False):
            provider.client.close()
