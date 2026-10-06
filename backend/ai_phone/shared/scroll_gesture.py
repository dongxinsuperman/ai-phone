"""Seed touch geometry and provider-aware trajectory replay isolation.

Distances use 0-1000 units of the scrolling axis, not screenshot pixels, so a
recorded gesture retains its intent on another device resolution. Legacy Seed
scrolls are incompatible; native Computer Use scrolls retain their old contract.
"""
from dataclasses import dataclass
import hashlib
from typing import Optional, Tuple

SCROLL_GESTURE_VERSION = 1
SCROLL_TYPES = ("singleAction", "toEdge")
NORMAL_DURATION_MS = 1000
FAST_DURATION_MS = 100
TO_EDGE_PASSES = 10
DEFAULT_DISTANCE = 600
CU_BACKENDS = ("claude_cu", "gpt_cu")


def is_cu_backend(backend: str) -> bool:
    return str(backend or "").strip().lower() in CU_BACKENDS


def seed_scroll_cache_key(key: str) -> str:
    return hashlib.sha256(f"seed-scroll-v{SCROLL_GESTURE_VERSION}:{key}".encode()).hexdigest()


def scroll_cache_candidates(key: str, backend: str):
    return [key] if is_cu_backend(backend) else [seed_scroll_cache_key(key), key]


def contains_seed_scroll(actions) -> bool:
    return any(isinstance(a, dict) and a.get("type") == "scroll" and a.get("scroll_gesture_version") == SCROLL_GESTURE_VERSION for a in actions or [])


def has_incompatible_scroll(actions, backend: str = "doubao_responses") -> bool:
    expected = 0 if is_cu_backend(backend) else SCROLL_GESTURE_VERSION
    return any(
        isinstance(action, dict) and action.get("type", action.get("action")) == "scroll"
        and action.get("scroll_gesture_version", 0) != expected
        for action in actions or []
    )


def has_obsolete_scroll(actions) -> bool:
    """Seed-only retirement predicate. Never use globally for CU archives."""
    return has_incompatible_scroll(actions, "doubao_responses")


def validate_scroll_options(scroll_type: str, distance: Optional[int]) -> None:
    if scroll_type not in SCROLL_TYPES:
        raise ValueError("scroll_type must be singleAction or toEdge")
    if distance is not None:
        if isinstance(distance, bool) or not isinstance(distance, int) or not 1 <= distance <= 1000:
            raise ValueError("scroll distance must be an integer in [1, 1000]")
        if scroll_type != "singleAction":
            raise ValueError("distance is only supported for singleAction")


@dataclass(frozen=True)
class ScrollGesture:
    start: Tuple[int, int]
    end: Tuple[int, int]
    duration_ms: int
    repeat: int


def build_scroll_gesture(
    size: Tuple[int, int], point: Optional[Tuple[int, int]], direction: str,
    *, scroll_type: str = "singleAction", distance: Optional[int] = None,
    amount: int = 1,
) -> ScrollGesture:
    validate_scroll_options(scroll_type, distance)
    if direction not in ("down", "up", "left", "right"):
        raise ValueError("invalid scroll direction")
    width, height = map(int, size)
    if width <= 0 or height <= 0:
        raise ValueError("invalid scroll window size")
    # A missing point is only an internal fallback, not the Seed XML contract.
    # Place it near the appropriate edge so the default gesture has room to move.
    if point is None:
        point = (width // 2, int(height * (.8 if direction == "down" else .2))) if direction in ("down", "up") else (int(width * (.8 if direction == "right" else .2)), height // 2)
    sx = max(0, min(int(point[0]), width - 1))
    sy = max(0, min(int(point[1]), height - 1))
    ex, ey = sx, sy
    axis = height if direction in ("down", "up") else width
    travel = axis if scroll_type == "toEdge" else max(1, int(axis * (distance or DEFAULT_DISTANCE) / 1000))
    # Keep the endpoint off system-gesture edges, without relocating the model's
    # start or accidentally reversing direction when it starts near an edge.
    x_lo, x_hi = int(width * .03), min(width - 1, int(width * .97))
    y_lo, y_hi = int(height * .03), min(height - 1, int(height * .97))
    if direction == "down":
        ey = min(sy, max(y_lo, sy - travel))
    elif direction == "up":
        ey = max(sy, min(y_hi, sy + travel))
    elif direction == "right":
        ex = min(sx, max(x_lo, sx - travel))
    else:
        ex = max(sx, min(x_hi, sx + travel))
    return ScrollGesture(
        start=(sx, sy), end=(ex, ey),
        duration_ms=FAST_DURATION_MS if scroll_type == "toEdge" else NORMAL_DURATION_MS,
        repeat=TO_EDGE_PASSES if scroll_type == "toEdge" else max(1, min(10, int(amount))),
    )
