from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from .base import TreeAdapter


class HarmonyAdapter(TreeAdapter):
    def tree(self, size):
        # Access through the live driver on each call; reconnect may replace _raw.
        fast_tree = getattr(self, "fast_tree", None)
        raw = (fast_tree() if fast_tree is not None else
               self.driver._call_with_reconnect(lambda: self.driver._raw.dump_hierarchy()))
        return self.convert(raw)

    def read_raw_tree(self):
        fast_tree = getattr(self, "fast_tree", None)
        return (fast_tree() if fast_tree is not None else
                self.driver._call_with_reconnect(lambda: self.driver._raw.dump_hierarchy()))

    @staticmethod
    def convert(raw):
        def visit(item, ancestor_clickable=False, parent_bounds=None):
            a = item.get("attributes", {})
            bounds = list(map(int, re.findall(r"-?\d+", a.get("bounds", ""))))
            clickable = str(a.get("clickable", "false")).lower() == "true"
            attrs = {
                "text": a.get("text", ""),
                "content-desc": a.get("description", ""),
                "resource-id": a.get("id", ""),
                "class": a.get("type", ""),
                "enabled": str(a.get("enabled", "false")).lower(),
                "visible": str(a.get("visible", "true")).lower(),
            }
            if len(bounds) == 4 and parent_bounds:
                bounds = [
                    max(bounds[0], parent_bounds[0]),
                    max(bounds[1], parent_bounds[1]),
                    min(bounds[2], parent_bounds[2]),
                    min(bounds[3], parent_bounds[3]),
                ]
            valid = len(bounds) == 4 and bounds[0] < bounds[2] and bounds[1] < bounds[3]
            candidate = clickable or (ancestor_clickable and a.get("type") == "Image")
            attrs["clickable"] = str(valid and candidate and attrs["visible"] == "true").lower()
            attrs["bounds"] = f"[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]" if valid else ""
            node = ET.Element("node", attrs)
            for child in item.get("children", []):
                node.append(visit(child, ancestor_clickable or clickable, bounds if valid else parent_bounds))
            return node

        root = ET.Element("hierarchy")
        root.append(visit(raw))
        return ET.tostring(root, encoding="unicode")
