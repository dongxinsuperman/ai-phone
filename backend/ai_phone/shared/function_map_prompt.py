"""执行权重中心与 Function Map 的消息分层。

第 5 点是 System Prompt 常驻的执行调度规则，不依赖 Map 是否存在。Map 字段
本身非必填；一旦调用方提供，就代表它是为本次 Run 选定的相关业务执行上下文。
System 只声明 Map 的使用范围和权限边界，正文始终放在每个逻辑会话段的首条
user message 中。
"""
from __future__ import annotations


def build_function_map_system_policy(*, present: bool, zh: bool) -> str:
    """Map 存在时构造不含正文的使用契约。"""
    if not present:
        return ""
    if zh:
        return (
            "\n## Function Map 使用契约\n"
            "Function Map 正文位于本会话首条 user 消息。该字段本身非必填。\n\n"
            "本次已提供 Function Map，说明调用方为当前任务选定了相关业务执行上下文。"
            "在页面关系、具体对象识别、入口与路径、测试数据、业务术语、异常与弹窗处理方面，"
            "必须认真参考它，优先于模型自身常识和无依据猜测，不得无理由忽略。\n\n"
            "它可以在保持同一任务意图时纠正旧名称、旧入口和旧路径；但它不是额外任务，"
            "不得新增、删除、合并、重排或跨越子步骤，不得替换任务明确指定的测试对象，"
            "不得改变预期结果或完成条件，也不得作为任务已完成的证据。\n\n"
            "当前截图中的直接可见事实用于判断当前实际状态；若它与 Map 指定的目标数据、"
            "账号、路径或前置状态不同，说明前置尚未满足，必须按 Map 还原，不得用当前错误状态"
            "覆盖调用方指定内容。截图暂未显示某个元素，不等于 Function Map 错误。"
            "不要在 Thought 中复述或总结 Map 正文，只使用当前决策需要的信息。\n"
        )

    return (
        "\n## Function Map Usage Contract\n"
        "The Function Map body is provided in the first user message of this logical "
        "session. The field itself is optional.\n\n"
        "A Function Map is present for this run, so the caller deliberately selected it "
        "as relevant business execution context. Give it substantial weight for page "
        "relationships, concrete object identification, entry points and paths, test data, "
        "business terms, and exception or popup handling. Prefer it over generic model "
        "knowledge and unsupported guesses; do not ignore it without reason.\n\n"
        "It may correct an old name, entry point, or path while preserving the same task "
        "intent. It is not an additional task: it must not add, remove, merge, reorder, or "
        "skip substeps; replace an explicitly requested test object; change expected results "
        "or completion conditions; or serve as evidence that the task is complete.\n\n"
        "Directly visible screenshot facts determine the current actual state. If they differ "
        "from Map-specified target data, account, path, or prerequisite state, the prerequisite "
        "is not yet satisfied and must be restored from the Map; do not let the current wrong "
        "state override caller-specified content. An element not currently visible is not proof "
        "that the Map is wrong. Do not restate the Map body; use only what this decision needs.\n"
    )


def build_execution_priority_system_policy(*, zh: bool) -> str:
    """构造与 Map 有无无关、始终进入 System Prompt 的第 5 点。"""
    if zh:
        return (
            "\n## 5. 场景权重与调用（始终生效）\n"
            "本节是每轮执行的常态调度中心，不依赖 Function Map 是否提供。\n\n"
            "a) 主线：始终以当前业务子步骤 N 为执行锚点。可直接执行时完成 N；"
            "尚未完成时不得跳到 N+1。\n\n"
            "b) 承接：当 N 因引导、弹窗、异常页面、前置状态不满足或其他突发情况无法直接推进时，"
            "先识别并声明当前阻碍，再插入解决阻碍所需的动作；处理期间仍属于 N，不得穿插后续业务步骤。\n\n"
            "c) 资料权重：若提供 Function Map，优先查找与当前任务、N 和当前页面匹配的内容。"
            "其中明确提供的当前 Case/Item 测试数据、业务路径、账号/对象/状态及执行条件，是该 Case 的"
            "第一优先级执行依据，必须准确采用；不得以通用规则、模型常识、历史路径或无依据猜测替换。"
            "同一 Map 同时包含通用内容和 Case/Item 专用内容时，以 Case/Item 专用内容为准。"
            "与当前场景明确匹配的业务或异常规则，优先于对宽泛步骤的自行解释。\n\n"
            "d) 连续处理与回流：Map 或其他恢复方式产生的动作可以连续执行，全部属于 N。"
            "达到该处理方式的完成条件、阻碍解除后，立即回到 N 继续；只有 N 的目标状态满足后才进入 N+1。\n\n"
            "e) 边界：上述调度不得改变任务目标、测试对象、子步骤业务顺序、预期结果或完成条件。"
            "未提供 Function Map 时，仍按本节识别和处理突发情况，仅依靠任务、子步骤和当前截图完成执行；"
            "不得伪造 Map。提供了 Map 但没有匹配内容时，必须明确按未命中处理。\n"
        )
    return (
        "\n## 5. Scenario Priority and Invocation (Always Active)\n"
        "This section is the normal execution dispatcher on every turn and does not depend on "
        "whether a Function Map is present.\n\n"
        "a) Mainline: keep the current business substep N as the execution anchor. Complete N "
        "when it is directly executable; while it remains incomplete, do not move to N+1.\n\n"
        "b) Handoff: when onboarding, a popup, an unexpected page, an unmet prerequisite, or another "
        "interruption prevents N from advancing directly, identify and state the obstacle, then insert "
        "the actions needed to resolve it. Those actions still belong to N; do not interleave later "
        "business substeps.\n\n"
        "c) Reference priority: when a Function Map is present, first look for content matching the "
        "current task, N, and current page. Explicit Case/Item test data, business paths, accounts, "
        "objects, states, and execution conditions supplied by the Map are the first-priority execution "
        "source for that Case and must be used accurately; never replace them with generic rules, model "
        "knowledge, historical paths, or unsupported guesses. When one Map contains both generic and "
        "Case/Item-specific content, the Case/Item-specific content wins. A clearly matched business or "
        "exception rule takes priority over the model's own interpretation of a broad substep.\n\n"
        "d) Continuous handling and return: Map or other recovery actions may run as a sequence and all "
        "belong to N. Once that method's completion condition is met and the obstacle is resolved, return "
        "immediately to N; move to N+1 only after N's target state is satisfied.\n\n"
        "e) Boundary: this dispatch must not change the task goal, test object, business order of substeps, "
        "expected result, or completion conditions. Without a Function Map, this section still identifies "
        "and handles interruptions using only the task, substeps, and screenshot; never fabricate a Map. "
        "When a Map is present but nothing matches, explicitly treat it as a miss.\n"
    )


def build_function_map_user_context(
    function_map_context: str | None,
    *,
    zh: bool,
) -> str:
    """把 Map 原文包装成每个逻辑会话段首轮使用的 user context。"""
    text = (function_map_context or "").strip()
    if not text:
        return ""
    if zh:
        return (
            "【Function Map｜本次 Run 的业务执行上下文】\n"
            "以下内容由调用方为本次任务选定。请按照 System 中的 Function Map "
            "使用契约，在相关业务决策中认真参考；它不是新的任务。\n"
            "<function_map_context>\n"
            f"{text}\n"
            "</function_map_context>"
        )
    return (
        "[Function Map | Business Execution Context for This Run]\n"
        "The caller selected the following context for this task. Apply the Function Map "
        "Usage Contract from the system prompt and give it due weight in relevant business "
        "decisions; it is not a new task.\n"
        "<function_map_context>\n"
        f"{text}\n"
        "</function_map_context>"
    )


__all__ = [
    "build_execution_priority_system_policy",
    "build_function_map_system_policy",
    "build_function_map_user_context",
]
