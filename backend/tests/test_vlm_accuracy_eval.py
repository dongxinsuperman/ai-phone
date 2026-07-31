from __future__ import annotations

from ai_phone.evals.vlm_accuracy import (
    AccuracyCase,
    Expectation,
    Rect,
    _score,
    default_suite,
    render_screen,
    suite_manifest,
)
from ai_phone.shared.vlm import Decision


def _decision(action: str) -> Decision:
    return Decision(thought="test", action_str=action, elapsed_ms=1, raw_content=f"Thought: test\nAction: {action}")


def test_default_suite_has_coordinate_semantic_and_multistep_coverage():
    categories = {case.category for case in default_suite()}
    assert categories == {"coordinate", "semantic", "multistep"}
    assert len(default_suite()) == 11
    assert next(case for case in default_suite() if case.id == "flow_next_save_finish").steps[2].expectation.action == "finished"


def test_coordinate_score_accepts_target_hit_and_rejects_clear_miss():
    target = Rect(300, 800, 300, 140)
    expectation = Expectation("click", target=target, tolerance=10)
    hit = _score(_decision("click(point='<point>420 450</point>')"), expectation, case_id="c", step_id="s", category="coordinate")
    miss = _score(_decision("click(point='<point>900 100</point>')"), expectation, case_id="c", step_id="s", category="coordinate")
    assert hit.passed is True and hit.coordinate_distance == 0
    assert miss.passed is False and miss.coordinate_distance and miss.coordinate_distance > 0


def test_semantic_score_checks_scroll_direction_and_exact_type_content():
    scroll = _score(_decision("scroll(point='<point>500 800</point>', direction='down')"), Expectation("scroll", direction="down"), case_id="c", step_id="s", category="semantic")
    typed = _score(_decision("type(content='qa@example.com')"), Expectation("type", content="qa@example.com"), case_id="c", step_id="s", category="semantic")
    wrong = _score(_decision("scroll(point='<point>500 800</point>', direction='up')"), Expectation("scroll", direction="down"), case_id="c", step_id="s", category="semantic")
    assert scroll.passed is True
    assert typed.passed is True
    assert wrong.passed is False


def test_rendered_screen_is_a_full_phone_png_and_manifest_is_safe():
    case: AccuracyCase = default_suite()[0]
    image_bytes = render_screen(case.steps[0].screen)
    assert image_bytes.startswith(b"\x89PNG")
    manifest = suite_manifest()
    assert manifest[0]["id"] == "coord_top_left"
    assert all("goal" not in item for item in manifest)
