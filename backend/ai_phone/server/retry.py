"""Run retry helpers.

Retry is intentionally a scheduler/runtime concern, not a VLM/cache mode.  The
small helpers here keep payload normalization, env caps, and attempt context in
one place so async emit workers can stamp rows with the attempt that created
the event.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional


_CURRENT_ATTEMPT: ContextVar[int] = ContextVar("ai_phone_run_attempt", default=1)


def current_attempt() -> int:
    """Return the active run attempt number, always at least 1."""

    try:
        value = int(_CURRENT_ATTEMPT.get())
    except Exception:  # noqa: BLE001
        return 1
    return max(1, value)


@contextmanager
def attempt_context(attempt: int) -> Iterator[None]:
    """Temporarily bind the current async context to ``attempt``."""

    token = _CURRENT_ATTEMPT.set(max(1, int(attempt or 1)))
    try:
        yield
    finally:
        _CURRENT_ATTEMPT.reset(token)


def normalize_requested_retry_max(value: Any) -> Optional[int]:
    """Normalize user supplied retryMax.

    Missing value stays ``None`` so API responses can distinguish "not
    provided" from "provided but invalid/zero".  Invalid, bool, negative, and
    non-integer values are silently treated as 0 per the design doc.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0
        try:
            parsed = int(stripped, 10)
        except ValueError:
            return 0
        return max(0, parsed)
    return 0


def resolve_effective_retry_max(
    *,
    env_retry_enabled: bool,
    env_retry_max: int,
    payload_retry_max: Optional[int],
) -> int:
    """Resolve effective retry cap from env and payload."""

    if not env_retry_enabled:
        return 0
    try:
        env_cap = int(env_retry_max)
    except Exception:  # noqa: BLE001
        return 0
    if env_cap <= 0:
        return 0
    requested = int(payload_retry_max or 0)
    if requested <= 0:
        return 0
    return min(requested, env_cap)


def total_attempts_for_retry_max(retry_max: int) -> int:
    """Convert retry count to total attempts for user-facing logs."""

    return max(1, int(retry_max or 0) + 1)


__all__ = [
    "attempt_context",
    "current_attempt",
    "normalize_requested_retry_max",
    "resolve_effective_retry_max",
    "total_attempts_for_retry_max",
]
