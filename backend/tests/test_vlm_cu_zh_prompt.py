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
    assert "matched a method that resolves the current substep's obstacle" in prompt
    assert "Writing only MATCH without the specific method is forbidden" in prompt
    assert "Every Map action belongs to the current substep N" in prompt
    assert "guidance, popup, and abnormal-state rules take priority" in prompt
    assert "matching an ordinary navigation rule from the background" in prompt
    assert "If no Function Map was provided, follow the original flow" in prompt


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
    assert "Thought 第二句**必须**是 Map 判读句" in prompt
    assert "命中可解决当前子步骤无法直接推进之阻碍的处理方式" in prompt
    assert "禁止只写「命中」而不写具体处理方式" in prompt
    assert "Map 产生的所有动作都属于当前子步骤 N" in prompt
    assert "引导、弹窗、异常状态等场景规则优先于普通页面导航规则" in prompt
    assert "禁止仅按背景页面命中普通导航规则" in prompt
    assert "未提供 Function Map 时按原流程执行" in prompt
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
    assert "当前截图：[已满足 / 未满足]" in prompt_default
    assert "Thought 第二句**必须**是 Map 判读句" in prompt_default
    assert "命中可解决当前子步骤无法直接推进之阻碍的处理方式" in prompt_default
    assert "禁止只写「命中」而不写具体处理方式" in prompt_default
    assert "Map 产生的所有动作都属于当前子步骤 N" in prompt_default
    assert "引导、弹窗、异常状态等场景规则优先于普通页面导航规则" in prompt_default
    assert "禁止仅按背景页面命中普通导航规则" in prompt_default
    assert "未提供 Function Map 时按原流程执行" in prompt_default


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
    assert "当前 Case/Item 测试数据、业务路径、账号/对象/状态及执行条件" in prompt
    assert "第一优先级执行依据" in prompt
    assert "Map 或其他恢复方式产生的动作可以连续执行" in prompt
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
    assert "Explicit Case/Item test data, business paths, accounts" in prompt
    assert "first-priority execution source" in prompt
    assert "Map or other recovery actions may run as a sequence" in prompt
    assert map_body not in prompt


@pytest.mark.parametrize(
    ("backend", "execution_heading"),
    [
        ("doubao_responses", "## 5. 场景权重与调用（始终生效）"),
        ("claude_cu", "## 5. Scenario Priority and Invocation (Always Active)"),
        ("gpt_cu", "## 5. Scenario Priority and Invocation (Always Active)"),
    ],
)
def test_system_prompt_without_map_keeps_execution_center_but_not_map_contract(
    backend: str,
    execution_heading: str,
) -> None:
    prompt = build_system_prompt_for_backend("进入我的页", backend=backend)

    assert execution_heading in prompt
    assert "does not depend on whether a Function Map is present" in prompt or (
        "不依赖 Function Map 是否提供" in prompt
    )
    assert "Function Map 使用契约" not in prompt
    assert "Function Map Usage Contract" not in prompt


@pytest.mark.parametrize(
    ("backend", "policy_heading", "completion_marker"),
    [
        ("doubao_responses", "## Function Map 使用契约", "⚠️ 完成铁律"),
        ("claude_cu", "## Function Map Usage Contract", "Completion iron rule"),
        ("gpt_cu", "## Function Map Usage Contract", "Completion iron rule"),
    ],
)
def test_map_policy_follows_task_and_substeps_but_precedes_completion_rule(
    backend: str,
    policy_heading: str,
    completion_marker: str,
) -> None:
    prompt = build_system_prompt_for_backend(
        "进入我的页",
        backend=backend,
        substeps_text=_SUBSTEPS,
        function_map_context="MAP_SENTINEL",
    )

    assert (
        prompt.index("进入我的页")
        < prompt.index(_SUBSTEPS)
        < prompt.index(policy_heading)
        < prompt.index(completion_marker)
    )


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
