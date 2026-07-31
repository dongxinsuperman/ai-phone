from __future__ import annotations

import re


HARMONY_INSTANCE_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
HARMONY_UDID_PREFIX = "454D5504D4143041524D0"
HARMONY_UDID_LENGTH = 64


def normalize_harmony_instance_uuid(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not HARMONY_INSTANCE_UUID_PATTERN.fullmatch(text):
        raise ValueError("instance_uuid must be empty or canonical 8-4-4-4-12 UUID")
    return text


def harmony_udid_from_uuid(instance_uuid: object) -> str:
    try:
        normalized = normalize_harmony_instance_uuid(instance_uuid)
    except ValueError:
        return ""
    if not normalized:
        return ""
    compact = normalized.replace("-", "").upper()
    return (HARMONY_UDID_PREFIX + compact).ljust(HARMONY_UDID_LENGTH, "0")
