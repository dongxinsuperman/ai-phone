"""V3 screenshot boundary: sources vary, consumers always receive JPEG bytes."""

from __future__ import annotations

import base64
import io
from typing import Callable

from PIL import Image


ScreenshotProvider = Callable[[int, int | None], bytes | str | Image.Image]


def as_jpeg(payload: bytes | str | Image.Image, quality: int, max_side: int | None) -> bytes:
    """Normalize source output without capturing again or altering orientation.

    A native JPEG at the requested size is passed through without recompression.
    Providers receive the existing V3 quality/size parameters and own their device
    connection; this helper does not create or close WDA/UiAutomator sessions.
    """
    if isinstance(payload, str):
        if payload.startswith("data:"):
            prefix, payload = payload.split(",", 1)
            if prefix not in {"data:image/jpeg;base64", "data:image/png;base64"}:
                raise ValueError("Unsupported screenshot data URL")
        payload = base64.b64decode(payload, validate=True)
    if isinstance(payload, bytes):
        image = Image.open(io.BytesIO(payload))
        if image.format not in {"JPEG", "PNG"}:
            raise ValueError("Screenshot must be JPEG or PNG")
        if image.format == "JPEG" and (not max_side or max(image.size) <= max_side):
            return payload
    elif isinstance(payload, Image.Image):
        image = payload
    else:
        raise TypeError("Unsupported screenshot payload")
    image = image.convert("RGB")
    if max_side and max(image.size) > max_side:
        ratio = max_side / max(image.size)
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
