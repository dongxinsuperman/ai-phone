import io
from copy import deepcopy

import pytest
from PIL import Image

from ai_phone.agent.trajectory_cache.jev_click import point_overlay
from ai_phone.agent.trajectory_cache.jev_click.types import Frame, annotate_legacy, annotate_with_mode


@pytest.mark.parametrize("size", [(720, 1280), (1125, 2001), (1216, 2688), (2001, 1125)])
def test_shared_overlay_preserves_nested_candidates_in_both_orientations(size):
    width, height = size
    candidates = [
        {"id": "N1", "bounds": [0, 0, width, height]},
        {"id": "N2", "bounds": [0, height - 140, width, height]},
        *[
            {"id": f"N{i + 3}", "bounds": [i * width // 5, height - 140, (i + 1) * width // 5, height]}
            for i in range(5)
        ],
        {"id": "N8", "bounds": [width // 5, height - 130, 2 * width // 5, height - 10]},
    ]
    original = deepcopy(candidates)
    image = io.BytesIO()
    Image.new("RGB", size, "white").save(image, format="PNG")
    frame = Frame(image.getvalue(), size, candidates)
    data, mode = annotate_with_mode(frame)
    assert mode == "point_overlay"
    assert candidates == original
    assert Image.open(io.BytesIO(data)).size == (round(width * 0.8), round(height * 0.8))


def test_layout_failure_uses_exact_legacy_image_without_mutating_candidates(monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(image, format="PNG")
    nodes = [{"id": "N1", "bounds": [30, 40, 120, 160]}]
    frame = Frame(image.getvalue(), (200, 300), nodes)
    original = deepcopy(nodes)
    expected = annotate_legacy(frame)

    def fail(_):
        raise ValueError("No room for labels")

    monkeypatch.setattr(point_overlay, "annotate_points", fail)
    data, mode = annotate_with_mode(frame)
    assert mode == "legacy"
    assert data == expected
    assert nodes == original
