"""Deterministic accuracy suite for AI Phone's main VLM.

The suite deliberately uses generated phone screens instead of a live app.  Every
click target therefore has an exact ground-truth rectangle, which makes model
comparisons repeatable even when the app under test changes.  It measures what a
real task consumes: emitted action, normalized coordinate, state judgement, and
multi-turn continuation.  It never operates a device.
"""
from __future__ import annotations

import io
from dataclasses import asdict, dataclass, field
from math import hypot
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

from ai_phone.shared.actions import ParsedAction, parse_action
from ai_phone.shared.llm.prompts import build_system_prompt_for_backend
from ai_phone.shared.vlm import Decision, VLMClient


SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 1920
DEFAULT_TOLERANCE = 42


@dataclass(frozen=True)
class Rect:
    """Rectangle in screenshot pixels; model points are normalized to 0--1000."""

    x: int
    y: int
    width: int
    height: int

    def contains_normalized(self, point: Sequence[int], tolerance: int = 0) -> bool:
        x, y = point
        left = max(0, int(self.x * 1000 / SCREEN_WIDTH) - tolerance)
        right = min(1000, int((self.x + self.width) * 1000 / SCREEN_WIDTH) + tolerance)
        top = max(0, int(self.y * 1000 / SCREEN_HEIGHT) - tolerance)
        bottom = min(1000, int((self.y + self.height) * 1000 / SCREEN_HEIGHT) + tolerance)
        return left <= x <= right and top <= y <= bottom

    def distance_to_normalized(self, point: Sequence[int]) -> float:
        """Distance from a point to this target, in normalized-coordinate units."""
        x, y = point
        left = self.x * 1000 / SCREEN_WIDTH
        right = (self.x + self.width) * 1000 / SCREEN_WIDTH
        top = self.y * 1000 / SCREEN_HEIGHT
        bottom = (self.y + self.height) * 1000 / SCREEN_HEIGHT
        nearest_x = min(max(x, left), right)
        nearest_y = min(max(y, top), bottom)
        return round(hypot(x - nearest_x, y - nearest_y), 2)


@dataclass(frozen=True)
class Button:
    label: str
    rect: Rect
    color: str = "#2563EB"
    text_color: str = "#FFFFFF"


@dataclass(frozen=True)
class Screen:
    title: str
    subtitle: str = ""
    buttons: tuple[Button, ...] = ()
    notice: str = ""
    input_label: str = ""
    input_value: str = ""
    input_focused: bool = False
    list_rows: tuple[str, ...] = ()


@dataclass(frozen=True)
class Expectation:
    action: str
    target: Rect | None = None
    direction: str | None = None
    content: str | None = None
    tolerance: int = DEFAULT_TOLERANCE


@dataclass(frozen=True)
class EvalStep:
    id: str
    screen: Screen
    expectation: Expectation
    hint: str = ""


@dataclass(frozen=True)
class AccuracyCase:
    id: str
    label: str
    goal: str
    steps: tuple[EvalStep, ...]
    category: str


@dataclass
class StepResult:
    case_id: str
    step_id: str
    category: str
    expected_action: str
    actual_action: str
    passed: bool
    reason: str
    coordinate_distance: float | None = None
    parsed_action: dict[str, Any] = field(default_factory=dict)
    raw_output: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    model: str
    results: list[StepResult]
    token_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        total = len(self.results)
        passed = sum(item.passed for item in self.results)
        coordinate = [item for item in self.results if item.coordinate_distance is not None]
        coordinate_hits = sum(item.passed for item in coordinate)
        return {
            "model": self.model,
            "summary": {
                "steps": total,
                "passed": passed,
                "pass_rate": round(passed / total, 4) if total else 0.0,
                "coordinate_steps": len(coordinate),
                "coordinate_hits": coordinate_hits,
                "coordinate_hit_rate": round(coordinate_hits / len(coordinate), 4)
                if coordinate
                else None,
            },
            "results": [item.to_dict() for item in self.results],
            "token_summary": self.token_summary,
        }


def _font(size: int):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render_screen(screen: Screen) -> bytes:
    """Render a deterministic phone screenshot for one eval step."""
    image = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), "#F7F9FC")
    draw = ImageDraw.Draw(image)
    title_font, body_font, button_font = _font(52), _font(34), _font(38)

    draw.rectangle((0, 0, SCREEN_WIDTH, 96), fill="#0F172A")
    draw.text((48, 28), "9:41", fill="#FFFFFF", font=body_font)
    draw.text((48, 150), screen.title, fill="#111827", font=title_font)
    if screen.subtitle:
        draw.text((48, 220), screen.subtitle, fill="#64748B", font=body_font)

    if screen.input_label:
        draw.text((48, 320), screen.input_label, fill="#334155", font=body_font)
        outline = "#2563EB" if screen.input_focused else "#CBD5E1"
        draw.rounded_rectangle((48, 372, 1032, 496), radius=20, fill="#FFFFFF", outline=outline, width=5)
        if screen.input_value:
            draw.text((78, 412), screen.input_value, fill="#0F172A", font=body_font)
        if screen.input_focused:
            draw.line((82, 404, 82, 460), fill="#2563EB", width=4)

    row_y = 560
    for row in screen.list_rows:
        draw.rounded_rectangle((48, row_y, 1032, row_y + 104), radius=18, fill="#FFFFFF")
        draw.text((80, row_y + 32), row, fill="#334155", font=body_font)
        row_y += 126

    if screen.notice:
        draw.rounded_rectangle((48, 1220, 1032, 1370), radius=24, fill="#E0F2FE")
        draw.text((80, 1274), screen.notice, fill="#075985", font=body_font)

    for button in screen.buttons:
        r = button.rect
        draw.rounded_rectangle((r.x, r.y, r.x + r.width, r.y + r.height), radius=28, fill=button.color)
        box = draw.textbbox((0, 0), button.label, font=button_font)
        tx = r.x + (r.width - (box[2] - box[0])) / 2
        ty = r.y + (r.height - (box[3] - box[1])) / 2 - 4
        draw.text((tx, ty), button.label, fill=button.text_color, font=button_font)

    draw.rectangle((0, SCREEN_HEIGHT - 86, SCREEN_WIDTH, SCREEN_HEIGHT), fill="#FFFFFF")
    draw.line((0, SCREEN_HEIGHT - 86, SCREEN_WIDTH, SCREEN_HEIGHT - 86), fill="#E2E8F0", width=2)
    draw.text((470, SCREEN_HEIGHT - 58), "HOME", fill="#64748B", font=body_font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def default_suite() -> tuple[AccuracyCase, ...]:
    """Return the baseline suite: six coordinates, four state judgements, one flow."""
    top_left = Button("ALPHA", Rect(72, 340, 300, 132), "#2563EB")
    top_right = Button("BRAVO", Rect(708, 340, 300, 132), "#16A34A")
    center = Button("CONFIRM", Rect(300, 818, 480, 144), "#EA580C")
    bottom_left = Button("CANCEL", Rect(72, 1490, 310, 132), "#DC2626")
    bottom_right = Button("SAVE", Rect(698, 1490, 310, 132), "#7C3AED")
    duplicate_primary = Button("CONTINUE", Rect(72, 930, 440, 136), "#2563EB")
    duplicate_secondary = Button("CONTINUE", Rect(568, 930, 440, 136), "#94A3B8")

    coordinate_screen = Screen(
        title="Checkout", subtitle="Select exactly one action", buttons=(top_left, top_right, center, bottom_left, bottom_right)
    )
    duplicate_screen = Screen(
        title="Confirm plan", subtitle="Choose the blue action", buttons=(duplicate_primary, duplicate_secondary)
    )
    cases: list[AccuracyCase] = []
    for case_id, label, button in (
        ("coord_top_left", "top-left target", top_left),
        ("coord_top_right", "top-right target", top_right),
        ("coord_center", "center target", center),
        ("coord_bottom_left", "bottom-left target", bottom_left),
        ("coord_bottom_right", "bottom-right target", bottom_right),
    ):
        cases.append(
            AccuracyCase(
                id=case_id,
                label=label,
                category="coordinate",
                goal=f"Tap the {button.label} button in the Checkout app.",
                steps=(EvalStep("tap", coordinate_screen, Expectation("click", target=button.rect)),),
            )
        )
    cases.append(
        AccuracyCase(
            id="coord_duplicate_label",
            label="duplicate label, color disambiguation",
            category="coordinate",
            goal="Tap the blue CONTINUE button, not the gray CONTINUE button.",
            steps=(EvalStep("tap_blue", duplicate_screen, Expectation("click", target=duplicate_primary.rect)),),
        )
    )
    cases.extend(
        (
            AccuracyCase(
                id="semantic_scroll_below",
                label="scroll direction",
                category="semantic",
                goal="More products are below the current visible list. Continue browsing downward.",
                steps=(
                    EvalStep(
                        "scroll",
                        Screen(title="Products", subtitle="Showing items 1-3", list_rows=("Item 1", "Item 2", "Item 3")),
                        Expectation("scroll", direction="down"),
                    ),
                ),
            ),
            AccuracyCase(
                id="semantic_type_focused",
                label="focused text input",
                category="semantic",
                goal="Enter qa@example.com into the active Email field.",
                steps=(
                    EvalStep(
                        "type",
                        Screen(title="Sign in", input_label="Email", input_focused=True),
                        Expectation("type", content="qa@example.com"),
                    ),
                ),
            ),
            AccuracyCase(
                id="semantic_finished_visible",
                label="finish only with visible evidence",
                category="semantic",
                goal="Open the Dashboard page.",
                steps=(
                    EvalStep(
                        "already_done",
                        Screen(title="Dashboard", subtitle="Overview", notice="Dashboard is ready"),
                        Expectation("finished"),
                    ),
                ),
            ),
            AccuracyCase(
                id="semantic_assert_failure",
                label="visible assertion failure",
                category="semantic",
                goal="Verify that the receipt shows PAYMENT APPROVED. No further retry is allowed.",
                steps=(
                    EvalStep(
                        "assert",
                        Screen(title="Payment result", notice="PAYMENT DECLINED - no retry"),
                        Expectation("assert_fail"),
                    ),
                ),
            ),
            AccuracyCase(
                id="flow_next_save_finish",
                label="three-step stateful flow",
                category="multistep",
                goal="Create a profile, save it, and finish only after the success confirmation is visible.",
                steps=(
                    EvalStep(
                        "next",
                        Screen(
                            title="Create profile",
                            subtitle="Step 1 of 2",
                            list_rows=("Name: Ada Lovelace", "Email: ada@example.com"),
                            notice="Profile details are complete",
                            buttons=(Button("NEXT", Rect(690, 1500, 320, 132)),),
                        ),
                        Expectation("click", target=Rect(690, 1500, 320, 132)),
                    ),
                    EvalStep(
                        "save",
                        Screen(title="Review profile", subtitle="Step 2 of 2", buttons=(Button("SAVE", Rect(690, 1500, 320, 132), "#7C3AED"),)),
                        Expectation("click", target=Rect(690, 1500, 320, 132)),
                    ),
                    EvalStep(
                        "finish",
                        Screen(title="Profile saved", notice="SUCCESS: Profile created"),
                        Expectation("finished"),
                    ),
                ),
            ),
        )
    )
    return tuple(cases)


def _score(decision: Decision, expectation: Expectation, *, case_id: str, step_id: str, category: str) -> StepResult:
    parsed: ParsedAction = parse_action(decision.action_str)
    action_dict = parsed.to_dict()
    if parsed.action != expectation.action:
        return StepResult(
            case_id, step_id, category, expectation.action, parsed.action, False,
            f"expected action {expectation.action}, got {parsed.action}",
            parsed_action=action_dict, raw_output=decision.raw_content, latency_ms=decision.elapsed_ms,
        )
    if expectation.target is not None:
        if not parsed.point:
            return StepResult(
                case_id, step_id, category, expectation.action, parsed.action, False,
                "click-like action omitted normalized point", parsed_action=action_dict,
                raw_output=decision.raw_content, latency_ms=decision.elapsed_ms,
            )
        distance = expectation.target.distance_to_normalized(parsed.point)
        passed = expectation.target.contains_normalized(parsed.point, expectation.tolerance)
        return StepResult(
            case_id, step_id, category, expectation.action, parsed.action, passed,
            "point hit target" if passed else f"point missed target by {distance} normalized units",
            coordinate_distance=distance, parsed_action=action_dict,
            raw_output=decision.raw_content, latency_ms=decision.elapsed_ms,
        )
    if expectation.direction and parsed.direction != expectation.direction:
        return StepResult(
            case_id, step_id, category, expectation.action, parsed.action, False,
            f"expected direction {expectation.direction}, got {parsed.direction!r}",
            parsed_action=action_dict, raw_output=decision.raw_content, latency_ms=decision.elapsed_ms,
        )
    if expectation.content and parsed.content != expectation.content:
        return StepResult(
            case_id, step_id, category, expectation.action, parsed.action, False,
            f"expected content {expectation.content!r}, got {parsed.content!r}",
            parsed_action=action_dict, raw_output=decision.raw_content, latency_ms=decision.elapsed_ms,
        )
    return StepResult(
        case_id, step_id, category, expectation.action, parsed.action, True, "action matched",
        parsed_action=action_dict, raw_output=decision.raw_content, latency_ms=decision.elapsed_ms,
    )


async def run_accuracy_suite(
    *,
    model: str,
    api_url: str,
    api_key: str,
    cases: Iterable[AccuracyCase] | None = None,
) -> RunResult:
    """Run the suite through the production ``VLMClient`` without operating a device."""
    results: list[StepResult] = []
    aggregate_summary: dict[str, Any] = {"call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    for case in cases or default_suite():
        prompt = build_system_prompt_for_backend(case.goal, backend="doubao_responses")
        client = VLMClient(prompt, api_url=api_url, api_key=api_key, model=model, timeout_seconds=90)
        for step in case.steps:
            if step.hint:
                client.add_hint(step.hint)
            decision = await client.decide(render_screen(step.screen), mime="image/png")
            results.append(_score(decision, step.expectation, case_id=case.id, step_id=step.id, category=case.category))
        summary = client.counter.summary()
        for key in aggregate_summary:
            aggregate_summary[key] += int(summary.get(key) or 0)
    return RunResult(model=model, results=results, token_summary=aggregate_summary)


def suite_manifest(cases: Iterable[AccuracyCase] | None = None) -> list[dict[str, Any]]:
    """Safe dry-run manifest; useful for reviewing what will spend model tokens."""
    return [
        {"id": case.id, "label": case.label, "category": case.category, "steps": [step.id for step in case.steps]}
        for case in (cases or default_suite())
    ]
