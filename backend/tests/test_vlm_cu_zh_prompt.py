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


@pytest.mark.parametrize("backend", ["doubao_responses", "claude_cu", "gpt_cu"])
def test_substep_boundaries_follow_injected_checklist_not_fixed_punctuation(
    backend: str,
) -> None:
    prompt = build_system_prompt_for_backend(
        "操作步骤：点击共学，点击我的",
        backend=backend,
        substeps_text=_SUBSTEPS,
    )

    assert "only substep boundary" in prompt or "唯一子步骤边界" in prompt
    assert "do not re-split" in prompt or "禁止再按某一种标点自行重拆" in prompt


def test_doubao_system_prompt_keeps_map_policy_but_not_map_body() -> None:
    map_body = "MAP_SENTINEL：首页底部有『我的』"
    prompt = build_system_prompt_for_backend(
        "进入我的页",
        backend="doubao_responses",
        function_map_context=map_body,
    )

    assert "## Function Map 使用契约" in prompt
    assert "优先于模型自身常识和无依据猜测" in prompt
    assert "不得新增、删除、合并、重排或跨越子步骤" in prompt
    assert map_body not in prompt


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
def test_cu_system_prompt_keeps_map_policy_but_not_map_body(backend: str) -> None:
    map_body = "MAP_SENTINEL: Home has a Profile tab"
    prompt = build_system_prompt_for_backend(
        "进入我的页",
        backend=backend,
        function_map_context=map_body,
    )

    assert "## Function Map Usage Contract" in prompt
    assert "Prefer it over generic model knowledge and unsupported guesses" in prompt
    assert "must not add, remove, merge, reorder, or skip substeps" in prompt
    assert map_body not in prompt


@pytest.mark.parametrize("backend", ["doubao_responses", "claude_cu", "gpt_cu"])
def test_system_prompt_without_map_does_not_add_map_policy(backend: str) -> None:
    prompt = build_system_prompt_for_backend("进入我的页", backend=backend)

    assert "Function Map 使用契约" not in prompt
    assert "Function Map Usage Contract" not in prompt


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
