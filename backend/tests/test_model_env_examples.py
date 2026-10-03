"""Deployment examples must agree on the current Doubao model defaults."""
from pathlib import Path

import pytest
from dotenv import dotenv_values

from ai_phone.config import Settings, build_downlink_config


BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", [".env.example", ".env.full.example"])
def test_model_examples_match_defaults_and_build_agent_config(filename):
    path = BACKEND / filename
    values = dotenv_values(path)
    assert values["AI_PHONE_PHONE_VLM_MODEL"] == Settings.model_fields["vlm_model"].default
    assert values["AI_PHONE_AUX_MODEL"] == Settings.model_fields["assistant_model"].default
    # No service calls: fill only the required credentials with test placeholders.
    settings = Settings(_env_file=path, phone_vlm_api_key="unit-phone-key", aux_api_key="unit-aux-key")
    snapshot = build_downlink_config(settings=settings)
    assert snapshot["vlm_model"] == values["AI_PHONE_PHONE_VLM_MODEL"]
    assert snapshot["assistant_model"] == values["AI_PHONE_AUX_MODEL"]


def test_full_example_copyable_comments_do_not_reintroduce_retired_seed_16():
    assert "doubao-seed-1-6" not in (BACKEND / ".env.full.example").read_text()
