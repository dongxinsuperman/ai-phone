import pytest

from ai_phone.config import Settings
from ai_phone.shared.llm.prompts import build_system_prompt_for_backend


_SUBSTEPS = "1. 点击共学\n2. 点击我的"


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
def test_cu_prompt_default_keeps_english_language_policy(backend: str) -> None:
    prompt = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend=backend,
        substeps_text=_SUBSTEPS,
    )

    assert "Human-readable Language Policy" not in prompt
    assert "Use Simplified Chinese" not in prompt
    assert "Current screenshot: [SATISFIED / NOT SATISFIED]" in prompt
    assert "当前截图：[已满足 / 未满足]" not in prompt


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
def test_cu_prompt_zh_readable_injects_chinese_readability_policy(
    backend: str,
) -> None:
    prompt = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend=backend,
        substeps_text=_SUBSTEPS,
        zh_readable=True,
    )

    assert "Human-readable Language Policy" in prompt
    assert "Use Simplified Chinese" in prompt
    assert "当前截图：[已满足 / 未满足]" in prompt
    assert "Current screenshot: [SATISFIED / NOT SATISFIED]" not in prompt
    assert "FINISHED" in prompt
    assert "ASSERT_FAIL" in prompt
    assert "PLATFORM_ACTION" in prompt


def test_zh_readable_flag_does_not_change_doubao_prompt() -> None:
    prompt_default = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend="doubao_responses",
        substeps_text=_SUBSTEPS,
    )
    prompt_zh_flag = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend="doubao_responses",
        substeps_text=_SUBSTEPS,
        zh_readable=True,
    )

    assert prompt_zh_flag == prompt_default


def test_settings_reads_vlm_cu_zh_prompt_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PHONE_VLM_CU_ZH_PROMPT_ENABLED", "true")

    settings = Settings(_env_file=None)

    assert settings.vlm_cu_zh_prompt_enabled is True
