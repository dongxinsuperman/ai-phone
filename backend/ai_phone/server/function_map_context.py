"""Function map context validation helpers.

This field is a run-level read-only reference blob. Validation stays simple and
content-agnostic: accept optional text, optionally enforce a length cap, and
never try to classify what the text contains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class FunctionMapContextValidationError(ValueError):
    reason: str
    detail: str

    def __str__(self) -> str:
        return self.detail


def normalize_function_map_context_text(
    value: Any,
    *,
    max_chars: Optional[int],
) -> str:
    """Return normalized text or ``""`` when absent.

    ``max_chars`` > 0 is a hard cap. ``max_chars`` <= 0 means ai-phone does not
    apply a product-layer character cap. We reject instead of truncating because
    missing the tail of a reference manual is more misleading than making the
    caller shorten it deliberately.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise FunctionMapContextValidationError(
            "invalid_body",
            "functionMapContext 必须是字符串",
        )

    text = value.strip()
    if not text:
        return ""

    limit = int(max_chars or 0)
    if limit <= 0:
        return text
    if len(text) > limit:
        raise FunctionMapContextValidationError(
            "function_map_context_too_long",
            f"functionMapContext 超出 {limit} 字符上限，请精简后重投（当前 {len(text)}）",
        )
    return text
