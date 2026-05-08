from __future__ import annotations

from ai_phone.shared.vlm import TokenCounter


def test_token_counter_keeps_doubao_cached_tokens_as_prompt_subset():
    counter = TokenCounter()

    counter.record(
        "VLM决策",
        "doubao",
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_tokens_details": {"cached_tokens": 400},
        },
    )

    summary = counter.summary()
    assert summary["prompt_tokens"] == 1000
    assert summary["completion_tokens"] == 100
    assert summary["total_tokens"] == 1100
    assert summary["cached_tokens"] == 400
    assert summary["cache_read_tokens"] == 400
    assert summary["cache_write_tokens"] == 0
    assert summary["cache_accounting"] == ""


def test_token_counter_tracks_claude_cache_read_as_separate_accounting():
    counter = TokenCounter()

    counter.record(
        "VLM决策",
        "claude",
        {
            "cache_accounting": "read_write",
            "input_tokens": 8109,
            "output_tokens": 1868,
            "total_tokens": 9977,
            "cache_read_tokens": 11145,
            "cache_write_tokens": 512,
        },
    )

    summary = counter.summary()
    assert summary["prompt_tokens"] == 8109
    assert summary["completion_tokens"] == 1868
    assert summary["total_tokens"] == 21634
    assert summary["cached_tokens"] == 11145
    assert summary["cache_read_tokens"] == 11145
    assert summary["cache_write_tokens"] == 512
    assert summary["cache_accounting"] == "read_write"
    assert summary["by_scene"][0]["cache_accounting"] == "read_write"
