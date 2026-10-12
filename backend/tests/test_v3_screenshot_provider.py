import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image, UnidentifiedImageError

from ai_phone.agent.trajectory_cache.screenshots import as_jpeg
from ai_phone.agent.trajectory_cache.v3_replay import V3ReplayRunner


def picture(fmt="JPEG", size=(100, 200)):
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, format=fmt)
    return out.getvalue()


@pytest.mark.parametrize("kind", ["bytes", "base64", "data_url", "pil"])
def test_png_source_is_normalized_to_real_jpeg_without_rotating(kind):
    payload = picture("PNG")
    if kind == "base64":
        payload = base64.b64encode(payload).decode()
    elif kind == "data_url":
        payload = "data:image/png;base64," + base64.b64encode(payload).decode()
    elif kind == "pil":
        payload = Image.open(io.BytesIO(payload))
    image = Image.open(io.BytesIO(as_jpeg(payload, 95, None)))
    assert image.format == "JPEG" and image.size == (100, 200)


def test_native_jpeg_is_not_recompressed():
    payload = picture()
    assert as_jpeg(payload, 95, None) is payload


def test_overseas_size_limit_preserves_landscape_aspect():
    result = as_jpeg(picture(size=(2400, 1080)), 90, 1568)
    image = Image.open(io.BytesIO(result))
    assert image.size == (1568, 705) and image.format == "JPEG"


@pytest.mark.asyncio
async def test_injected_source_bypasses_driver_and_serves_final_capture(monkeypatch):
    calls = []
    payload = picture()

    def source(quality, max_side):
        calls.append((quality, max_side))
        return payload

    def forbidden(*args):
        raise AssertionError("No original screenshot allowed when a source is injected")

    driver = SimpleNamespace(screenshot_jpeg=forbidden)
    runner = V3ReplayRunner(driver=driver, trajectory={"actions": []}, screenshot_provider=source)
    assert await runner._screenshot_jpeg() is payload

    async def final():
        return await runner._screenshot_jpeg()

    monkeypatch.setattr(runner, "_wait_stable", final)
    assert await runner.capture_final_frame() is payload
    assert calls == [(95, None), (95, None)]


@pytest.mark.asyncio
async def test_no_source_preserves_original_screenshot_parameters():
    calls = []
    payload = picture()

    def original(*args):
        calls.append(args)
        return payload

    runner = V3ReplayRunner(driver=SimpleNamespace(screenshot_jpeg=original), trajectory={"actions": []})
    assert await runner._screenshot_jpeg() is payload
    assert calls == [(95, None)]


@pytest.mark.asyncio
async def test_bad_source_does_not_silently_take_second_screenshot():
    def original(*args):
        raise AssertionError("Do not fall back to a hidden extra capture")

    runner = V3ReplayRunner(
        driver=SimpleNamespace(screenshot_jpeg=original),
        trajectory={"actions": []},
        screenshot_provider=lambda *a: b"invalid",
    )
    with pytest.raises(UnidentifiedImageError):
        await runner._screenshot_jpeg()
