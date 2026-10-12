"""Bounded screenshot settling for the experimental V3 channel."""
import asyncio
import io
import time
from dataclasses import dataclass

from PIL import Image, ImageChops

from ai_phone.agent.runner.stability import StabilityResult


@dataclass(frozen=True)
class FastStabilityPolicy:
    timeout_s: float = 4.0
    interval_s: float = 0.1
    pixel_delta: int = 12
    change_ratio: float = 0.001
    max_edge: int = 480


def policy_for_platform(platform: str) -> FastStabilityPolicy:
    return FastStabilityPolicy(interval_s={"harmony": 0.0, "ios": 0.2}.get(platform, 0.1))


def thumbnail(data: bytes, max_edge: int = 480) -> Image.Image:
    with Image.open(io.BytesIO(data)) as source:
        result = source.convert("RGB")
    result.thumbnail((max_edge, max_edge))
    return result


def pixel_change_ratio(previous: Image.Image, current: Image.Image, pixel_delta: int = 12) -> float:
    if previous.size != current.size:
        return 1.0
    red, green, blue = ImageChops.difference(previous, current).split()
    strongest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    histogram = strongest.histogram()
    return sum(histogram[pixel_delta + 1:]) / (current.width * current.height)


async def wait_fast_stable(screenshot, *, frame_a_bytes=None, policy=None):
    """Compare fresh consecutive RGB frames; return the exact last JPEG.

    frame_a_bytes is intentionally ignored: an old pre-action frame must not
    substitute the first fresh sample. Visual quiet does not prove loading done.
    """
    policy = policy or FastStabilityPolicy()
    start = time.monotonic()
    previous = None
    last = None
    checks = 0
    while True:
        remaining = policy.timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            return StabilityResult(last, False, int((time.monotonic()-start)*1000), checks)
        try:
            last = await asyncio.wait_for(screenshot(), timeout=remaining)
        except asyncio.TimeoutError:
            return StabilityResult(last, False, int((time.monotonic()-start)*1000), checks)
        current = thumbnail(last, policy.max_edge)
        checks += 1
        if previous is not None:
            ratio = pixel_change_ratio(previous, current, policy.pixel_delta)
            if ratio <= policy.change_ratio:
                return StabilityResult(last, True, int((time.monotonic()-start)*1000), checks)
        previous = current
        elapsed = time.monotonic()-start
        await asyncio.sleep(min(policy.interval_s, max(0, policy.timeout_s-elapsed)))
