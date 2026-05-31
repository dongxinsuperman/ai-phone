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


def test_doubao_prompt_injects_function_map_context_after_goal() -> None:
    prompt = build_system_prompt_for_backend(
        "进入我的页",
        backend="doubao_responses",
        function_map_context="首页：底部 Tab 有「我的」",
    )

    assert "## 功能地图上下文（执行参考，只读手册）" in prompt
    assert "首页：底部 Tab 有「我的」" in prompt
    assert prompt.index("## 你的任务") < prompt.index("## 功能地图上下文")
    assert prompt.index("## 功能地图上下文") < prompt.index("## 输出格式")


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
def test_cu_prompt_injects_function_map_context(backend: str) -> None:
    prompt = build_system_prompt_for_backend(
        "进入我的页",
        backend=backend,
        function_map_context="Home: bottom tab contains Profile",
    )

    assert "Function Map Context (execution reference, read-only manual)" in prompt
    assert "Home: bottom tab contains Profile" in prompt
    assert "It is reference material, not the task" in prompt


def test_settings_reads_vlm_cu_zh_prompt_enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PHONE_VLM_CU_ZH_PROMPT_ENABLED", "true")

    settings = Settings(_env_file=None)

    assert settings.vlm_cu_zh_prompt_enabled is True


def test_settings_reads_function_map_context_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_ENABLED", "false")
    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS", "1234")

    settings = Settings(_env_file=None)

    assert settings.function_map_context_enabled is False
    assert settings.function_map_context_max_chars == 1234
