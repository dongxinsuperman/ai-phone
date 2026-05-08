from __future__ import annotations

from ai_phone.server.analytics.aggregator import _token_summary
from ai_phone.server.models import Run, SubmissionItem


def test_token_summary_recomputes_claude_read_write_totals_for_top_items():
    item = SubmissionItem(
        id="item-1",
        submission_id="sub-1",
        case_id="case-1",
        case_name="Claude case",
        platform="Android",
        run_content="run",
        run_id="run-1",
    )
    run = Run(
        id="run-1",
        device_serial="device-1",
        goal="run",
        token_summary={
            "call_count": 1,
            "prompt_tokens": 8109,
            "completion_tokens": 1868,
            # Historical Claude summaries stored the provider raw total here.
            "total_tokens": 9977,
            "cached_tokens": 11145,
            "cache_read_tokens": 11145,
            "cache_write_tokens": 0,
            "by_scene": [
                {
                    "model": "claude",
                    "calls": 1,
                    "prompt_tokens": 8109,
                    "completion_tokens": 1868,
                    "total_tokens": 9977,
                    "cached_tokens": 11145,
                    "cache_read_tokens": 11145,
                    "cache_write_tokens": 0,
                }
            ],
        },
    )

    summary = _token_summary([item], {"run-1": run})

    assert summary["cacheAccounting"] == "read_write"
    assert summary["totalTokens"] == 21122
    assert summary["byPlatform"]["Android"]["totalTokens"] == 21122
    assert summary["byModel"][0]["totalTokens"] == 21122
    assert summary["topItems"][0]["totalTokens"] == 21122


def test_token_summary_keeps_cached_prompt_subset_totals_unchanged():
    item = SubmissionItem(
        id="item-1",
        submission_id="sub-1",
        case_id="case-1",
        case_name="Doubao case",
        platform="Android",
        run_content="run",
        run_id="run-1",
    )
    run = Run(
        id="run-1",
        device_serial="device-1",
        goal="run",
        token_summary={
            "call_count": 1,
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "cached_tokens": 400,
            "cache_read_tokens": 400,
            "cache_write_tokens": 0,
        },
    )

    summary = _token_summary([item], {"run-1": run})

    assert summary["cacheAccounting"] == ""
    assert summary["totalTokens"] == 1100
    assert summary["topItems"][0]["totalTokens"] == 1100
