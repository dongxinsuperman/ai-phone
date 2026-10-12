from __future__ import annotations

from xml.etree import ElementTree as ET

from ..client import JevError
from .base import TreeAdapter


class IosAdapter(TreeAdapter):
    model_note = (
        "本端XCTest名称可能是内部别名。以当前截图控件及编号区域为准，节点名称仅作辅助；"
        "不得因名称不一致排除图中明确存在的目标。候选包含文字定位区域，不保证原生可点击；"
        "目标被弹窗或遮罩覆盖时，即使底层文字仍可见也不能选择，应返回NOT_FOUND交给救援；"
        "只有目标当前明确可操作才选择其编号。"
    )
    TYPES = {"Button", "Cell", "Switch", "TextField", "SecureTextField", "Link", "Icon", "Image"}

    def tree(self, size):
        source = self.driver._wda._request("GET", "/source?format=xml")["value"]
        return self.convert(source, size)

    @classmethod
    def convert(cls, source, size):
        tree = ET.fromstring(source)
        app = next((n for n in tree.iter() if n.get("type") == "XCUIElementTypeApplication"), None)
        if app is None:
            raise JevError("XCTest Application frame missing")
        width, height = size
        aw, ah = float(app.get("width", "0")), float(app.get("height", "0"))
        # Windowed/nonzero-origin scenes have not been validated. Reject instead
        # of stretching them over the screen and returning plausible wrong points.
        if aw <= 0 or ah <= 0 or float(app.get("x", "0")) != 0 or float(app.get("y", "0")) != 0:
            raise JevError("Unsupported XCTest Application viewport")
        sx, sy = width / aw, height / ah
        if abs(sx - sy) > 0.02:
            raise JevError("XCTest/screenshot aspect mismatch")

        seen_regions = set()

        def visit(n):
            x, y, w, h = [float(n.get(k, "0")) for k in ("x", "y", "width", "height")]
            b = [round(x * sx), round(y * sy), round((x + w) * sx), round((y + h) * sy)]
            kind = n.get("type", "").removeprefix("XCUIElementType")
            label = n.get("label") or n.get("value") or n.get("name") or ""
            # Candidates are visual regions, not certified clickable controls.
            # XCTest exposes real tabs as StaticText; keep their text and bounds.
            candidate = kind in cls.TYPES or (kind in {"Other", "StaticText"} and bool(label.strip()))
            active = (
                candidate
                and n.get("visible") == "true"
                and n.get("enabled") == "true"
                and 0 <= b[0] < b[2] <= width
                and 0 <= b[1] < b[3] <= height
            )
            signature = (tuple(b), label)
            if active:
                if signature in seen_regions:
                    active = False
                else:
                    seen_regions.add(signature)
            node = ET.Element(
                "node",
                {
                    "class": kind,
                    "text": label,
                    "resource-id": n.get("name", ""),
                    "enabled": n.get("enabled", "false"),
                    "visible": n.get("visible", "false"),
                    "clickable": str(active).lower(),
                    "bounds": f"[{b[0]},{b[1]}][{b[2]},{b[3]}]",
                },
            )
            for child in n:
                node.append(visit(child))
            return node

        root = ET.Element("hierarchy")
        root.append(visit(app))
        return ET.tostring(root, encoding="unicode")
