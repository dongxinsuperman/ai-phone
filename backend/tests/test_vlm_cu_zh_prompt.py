import pytest

from ai_phone.config import Settings
from ai_phone.shared.llm.prompts import build_system_prompt_for_backend


_SUBSTEPS = "1. 点击共学\n2. 点击我的"

# 真实回归样本：公司批次 7faf78b5a958 / login-loop10 / run 1d1ad7544ca7。
# 报告中第 30 轮 Thought 重复携带了“子步骤1到子步骤11已满足”，随后
# Action wait(seconds=5) 服务子步骤 12；该数据只用于 Prompt 回归，不触发设备执行。
_REAL_REPORT_7FAF78B5A958_SUBSTEPS = """1. 关闭当前App，回到手机主屏幕
2. 在主屏幕上找到App「洋葱学园」，它的图标是一个蓝色葱头，长按住不放
3. 等菜单弹出来，在弹出的菜单里点叹号图标（也可能显示为「应用信息」或「详情」）进入应用详情页
4. 找到「存储占用」（也可能是「存储」或「存储空间」）点进去
5. 点「清除数据」，注意不是清空缓存而是清空数据
6. 弹出确认框就确认清除
7. 回到手机主屏幕，重新打开App「洋葱学园」（蓝色葱头图标）
8. 出现「用户隐私协议」就点「同意并继续」
9. 在登录页的手机号输入框里输入13001212755
10. 选验证码登录，验证码填 1111（不要点获取真实短信）
11. 有用户协议勾选框就先勾上，点「登录」
12. 登录后固定等待 5 秒，让新手引导充分曝光出来
13. 此时是全新安装态，会出现首次使用的新手引导，按它的步骤一步步完成，它没有跳过或关闭的入口，中途不要放弃、不要去找别的路
14. 完成之后进入首页"""


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
    assert "进度归属遵循上面的「执行关系与权重」" in prompt_default
    assert "引导、弹窗、异常状态等场景规则优先于普通页面导航规则" in prompt_default
    assert "禁止仅按背景页面命中普通导航规则" in prompt_default
    assert "未提供 Function Map 时按原流程执行" in prompt_default


def test_doubao_substeps_start_at_one_and_advance_only_by_contiguous_verdicts() -> None:
    prompt = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend="doubao_responses",
        substeps_text=_SUBSTEPS,
    )

    assert "首轮必须从子步骤 1 开始" in prompt
    assert "当前截图只能用于判断当前子步骤的完整原文" in prompt
    assert "Thought 必须继续按同一模板判读 N+1" in prompt
    assert "Action 只能服务于本轮最后一条[未满足]的子步骤" in prompt
    assert "截图符合后续子步骤不能作为跳过当前项的依据" in prompt
    assert "只属于排除当前子步骤的阻碍" in prompt
    assert "Action 直接给下一条子步骤的动作" not in prompt
    assert "wait(1)" not in prompt


@pytest.mark.parametrize("backend", ["doubao_responses", "claude_cu", "gpt_cu"])
@pytest.mark.parametrize("zh_readable", [False, True])
def test_substep_rules_start_only_after_preconditions(
    backend: str,
    zh_readable: bool,
) -> None:
    prompt = build_system_prompt_for_backend(
        "测试标题：切换账号后进入我的\n"
        "前置条件：先登录测试账号\n"
        "操作步骤：点击共学，点击我的\n"
        "预期结果：进入我的页",
        backend=backend,
        substeps_text=_SUBSTEPS,
        zh_readable=zh_readable,
    )

    if backend == "doubao_responses" or zh_readable:
        assert "子步骤规则仅在前置条件完成" in prompt
        assert "进入「操作步骤」阶段" in prompt
        assert "首轮必须从子步骤 1 开始" in prompt
    else:
        assert "Substep rules apply only after all Preconditions are complete" in prompt
        assert "entered Operation Steps" in prompt
        assert "stage must start at substep 1" in prompt


def test_real_report_substeps_archive_completed_prefix_across_turns() -> None:
    prompt = build_system_prompt_for_backend(
        "清除App「洋葱学园」的数据并登录13001212755",
        backend="doubao_responses",
        substeps_text=_REAL_REPORT_7FAF78B5A958_SUBSTEPS,
    )

    assert _REAL_REPORT_7FAF78B5A958_SUBSTEPS in prompt
    assert "上一轮 Action 服务于子步骤 N" in prompt
    assert "本轮仍必须从同一子步骤 N 开始" in prompt
    assert "早于本轮起点 N 的子步骤均已归档" in prompt
    assert "禁止再次输出、概括或重新判定" in prompt
    assert "子步骤 1 到 N-1 已完成" in prompt


@pytest.mark.parametrize("backend", ["doubao_responses", "claude_cu", "gpt_cu"])
@pytest.mark.parametrize("zh_readable", [False, True])
def test_process_substep_evidence_is_a_highest_priority_iron_rule(
    backend: str,
    zh_readable: bool,
) -> None:
    prompt = build_system_prompt_for_backend(
        "执行通用任务",
        backend=backend,
        substeps_text="1. 执行操作 A\n2. 验证状态 B",
        zh_readable=zh_readable,
    )

    if backend == "doubao_responses" or zh_readable:
        assert "子步骤满足证据铁律" in prompt
        assert "最高优先级" in prompt
        assert "同一编号在同一" in prompt
        assert "禁止自问自答" in prompt
        assert "当前 N 完整原文" in prompt
        assert "动作、过程或转移" in prompt
        assert "禁止从当前状态反推" in prompt
        assert "禁止继续 N+1" in prompt
    else:
        assert "Substep Completion-Evidence Iron Rule (highest priority; violation = KILL)" in prompt
        assert "Each substep number may appear at most once" in prompt
        assert "No self-questioning, repeated speculation" in prompt
        assert "complete original text of current N" in prompt
        assert "action, process, or transition" in prompt
        assert "Never infer unproved history from current state" in prompt
        assert "never N+1 or `FINISHED`" in prompt

    assert "清除数据" not in prompt
    assert "完成引导" not in prompt
    assert "clear-data" not in prompt
    assert "complete-guide" not in prompt


def test_doubao_structured_and_free_prompts_use_separate_mode_blocks() -> None:
    structured = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend="doubao_responses",
        substeps_text=_SUBSTEPS,
    )
    free = build_system_prompt_for_backend(
        "看一下当前页面",
        backend="doubao_responses",
    )

    assert "## 结构化 Case 执行与终止" in structured
    assert "## 本 Run 操作步骤子步骤清单" in structured
    assert "## 非结构化任务执行与终止" not in structured

    assert "## 非结构化任务执行与终止" in free
    assert "## 结构化 Case 执行与终止" not in free
    assert "## 子步骤执行规则" not in free


def test_doubao_structured_prompt_has_one_explicit_block_order() -> None:
    prompt = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend="doubao_responses",
        substeps_text=_SUBSTEPS,
        function_map_context="MAP_SENTINEL",
    )
    headings = (
        "## 本次任务 Goal / Case 原文",
        "## 输出格式",
        "## 可用动作",
        "## 执行关系与权重（始终生效）",
        "## 结构化 Case 执行与终止",
        "## 本 Run 操作步骤子步骤清单",
        "## 子步骤执行规则",
        "## Function Map 使用契约",
        "## 完成证据",
        "## 瞬态 UI 动作协议",
    )

    positions = [prompt.index(heading) for heading in headings]
    assert positions == sorted(positions)


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
def test_overseas_prompts_archive_completed_prefix_across_turns(backend: str) -> None:
    prompt = build_system_prompt_for_backend(
        "clear app data and log in",
        backend=backend,
        substeps_text=_REAL_REPORT_7FAF78B5A958_SUBSTEPS,
    )

    assert "previous turn's action served substep N" in prompt
    assert "Earlier archived substeps must not appear" in prompt
    assert "substeps 1 through N-1 are complete" in prompt


@pytest.mark.parametrize("backend", ["claude_cu", "gpt_cu"])
@pytest.mark.parametrize("zh_readable", [False, True])
def test_overseas_substeps_match_contiguous_progression_contract(
    backend: str,
    zh_readable: bool,
) -> None:
    prompt = build_system_prompt_for_backend(
        "点击共学，点击我的",
        backend=backend,
        substeps_text=_SUBSTEPS,
        zh_readable=zh_readable,
    )

    if zh_readable:
        assert "首轮必须从子步骤 1 开始" in prompt
        assert "当前截图只能用于判断当前子步骤的完整原文" in prompt
        assert "必须继续按同一模板判读 N+1" in prompt
        assert "Action 只能服务于本轮最后一条[未满足]的子步骤" in prompt
    else:
        assert "stage must start at substep 1" in prompt
        assert "current screenshot may judge only the complete original text" in prompt
        assert "continue with the same verdict template for N+1" in prompt
        assert "action may serve only the final [NOT SATISFIED] substep" in prompt

    assert "A screenshot match for a later substep cannot justify skipping" in prompt
    assert "After a skip, the next verdict may only be N+1" in prompt
    assert "autonomously dismiss a system popup, guide, or overlay" in prompt
    assert "action call should target substep N+1" not in prompt
    assert "wait(1)" not in prompt


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
        ("doubao_responses", "## 执行关系与权重（始终生效）"),
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


def test_session_reset_threshold_defaults_to_240k(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_PHONE_VLM_SESSION_RESET_PROMPT_THRESHOLD", raising=False)

    settings = Settings(_env_file=None)

    assert settings.vlm_session_reset_prompt_threshold == 240000


def test_session_reset_threshold_remains_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_PHONE_VLM_SESSION_RESET_PROMPT_THRESHOLD", "30000")

    settings = Settings(_env_file=None)

    assert settings.vlm_session_reset_prompt_threshold == 30000


def test_settings_reads_function_map_context_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_ENABLED", "false")
    monkeypatch.setenv("AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS", "1234")

    settings = Settings(_env_file=None)

    assert settings.function_map_context_enabled is False
    assert settings.function_map_context_max_chars == 1234
