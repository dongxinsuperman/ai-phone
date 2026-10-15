"""Current-frame candidates. IDs are regenerated for every capture."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFont


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if k.lower() in {"token", "access_token", "api_key", "password", "authorization"}
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):

        def clean(match):
            u = urlsplit(match.group())
            return urlunsplit((u.scheme, u.hostname or "", u.path, "", ""))

        value = re.sub(r"https?://[^\s<>\"']+", clean, value)
        return re.sub(
            r"(?i)(?:Bearer\s+\S+|(?:api[_-]?key|access[_-]?token|password|token)\s*[=:]\s*[^\s&,]+)",
            "[redacted]",
            value,
        )
    return value


@dataclass
class Frame:
    image: bytes
    size: tuple[int, int]
    candidates: list[dict]
    visual_regions: dict = field(default_factory=dict)
    visual_metadata: dict = field(default_factory=dict)


def tree_data(xml: str, size: tuple[int, int]) -> tuple[list[dict], list[str]]:
    root = ET.fromstring(xml)
    width, height = size
    candidates, labels = [], []
    for node in root.iter("node"):
        a = node.attrib
        if a.get("visible", "true") != "true":
            continue
        label = a.get("text") or a.get("content-desc")
        if label:
            labels.append(label)
        bounds = list(map(int, re.findall(r"-?\d+", a.get("bounds", ""))))
        if a.get("clickable") != "true" or a.get("enabled") != "true" or len(bounds) != 4:
            continue
        x0, y0, x1, y1 = bounds
        bounds = [max(0, x0), max(0, y0), min(width, x1), min(height, y1)]
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            continue
        candidates.append(
            {
                "id": f"N{len(candidates) + 1}",
                "bounds": bounds,
                "text": a.get("text", ""),
                "description": a.get("content-desc", ""),
                "resource_id": a.get("resource-id", ""),
                "class": a.get("class", ""),
                "labels": list(
                    dict.fromkeys(
                        n.get("text") or n.get("content-desc")
                        for n in node.iter("node")
                        if n.get("text") or n.get("content-desc")
                    )
                ),
            }
        )

    return candidates, list(dict.fromkeys(labels))


def annotate_legacy(frame: Frame) -> bytes:
    image = Image.open(io.BytesIO(frame.image)).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=20)
    for node in frame.candidates:
        x0, y0, x1, y1 = node["bounds"]
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline="#dc2020", width=2)
    from .label_layout import place_labels

    metrics = {n["id"]: draw.textbbox((0, 0), n["id"], font=font) for n in frame.candidates}
    pad = 2
    sizes = {
        key: (right - left + pad * 2, bottom - top + pad * 2)
        for key, (left, top, right, bottom) in metrics.items()
    }
    placements = place_labels(frame.candidates, sizes, image.size)
    # Leaders are painted before badges so no line obscures an identifier.
    for node in frame.candidates:
        x, y, z, bottom = placements[node["id"]]
        x0, y0, x1, y1 = node["bounds"]
        if (x, y) != (x0, y0):
            anchor = (max(x0, min((x + z) // 2, x1 - 1)), y0)
            draw.line(((x + z) // 2, (y + bottom) // 2, *anchor), fill="#dc2020", width=1)
            draw.ellipse((anchor[0] - 2, anchor[1] - 2, anchor[0] + 2, anchor[1] + 2), fill="#dc2020")
    for node in frame.candidates:
        x, y, z, bottom = placements[node["id"]]
        left, top, _, _ = metrics[node["id"]]
        draw.rectangle((x, y, z - 1, bottom - 1), fill="white")
        draw.text((x + pad - left, y + pad - top), node["id"], fill="#b00000", font=font)
    out = io.BytesIO()
    image.resize(
        (max(1, round(image.width * 0.8)), max(1, round(image.height * 0.8))), Image.Resampling.LANCZOS
    ).save(out, format="JPEG", quality=85)
    return out.getvalue()


def annotate_with_mode(frame: Frame) -> tuple[bytes, str]:
    """Shared three-platform renderer; preserve legacy output on layout failure."""
    from .point_overlay import annotate_points
    from loguru import logger

    if frame.visual_regions:
        try:
            from .visual_vendor.core import annotate as enhanced_annotate
            return enhanced_annotate(frame.image, frame.candidates, frame.visual_regions), "point_overlay"
        except Exception as exc:
            frame.visual_metadata["render_fallback"] = True
            logger.warning("Jev enhanced drawing failed; using original renderer: {}", type(exc).__name__)
    try:
        return annotate_points(frame), "point_overlay"
    except Exception as exc:
        logger.warning("Jev point overlay failed; using legacy renderer: {}", type(exc).__name__)
        return annotate_legacy(frame), "legacy"


def annotate(frame: Frame) -> bytes:
    return annotate_with_mode(frame)[0]


def image_size(image: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(image)) as im:
        return im.size
