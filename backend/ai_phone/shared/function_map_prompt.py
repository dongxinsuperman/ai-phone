"""Function Map 的消息分层与统一语义。

Map 字段本身非必填；但一旦调用方提供，就代表它是为本次 Run 选定的相关
业务执行上下文。System 只声明它的使用范围和权限边界，正文始终放在每个
逻辑会话段的首条 user message 中。
"""
from __future__ import annotations


def build_function_map_system_policy(*, present: bool, zh: bool) -> str:
    """构造不含 Map 正文的 System 使用契约。"""
    if not present:
        return ""
    if zh:
        return (
            "\n## Function Map 使用契约\n"
            "Function Map 正文位于本会话首条 user 消息。该字段本身非必填；"
            "未提供时，你必须仅依靠任务、子步骤和当前截图保持完整执行能力。\n\n"
            "本次已提供 Function Map，说明调用方为当前任务选定了相关业务执行上下文。"
            "在页面关系、具体对象识别、入口与路径、测试数据、业务术语、异常与弹窗处理方面，"
            "必须认真参考它，优先于模型自身常识和无依据猜测，不得无理由忽略。\n\n"
            "它可以在保持同一任务意图时纠正旧名称、旧入口和旧路径；但它不是额外任务，"
            "不得新增、删除、合并、重排或跨越子步骤，不得替换任务明确指定的测试对象，"
            "不得改变预期结果或完成条件，也不得作为任务已完成的证据。\n\n"
            "当前截图中直接可见的页面事实与 Function Map 不一致时，以直接可见事实为准；"
            "截图暂未显示某个元素，不等于 Function Map 错误。"
            "不要在 Thought 中复述或总结 Map 正文，只使用当前决策需要的信息。\n\n"
            "5. 场景权重与调用：\n\n"
            "Function Map 不是每轮都必须机械执行，但在以下场景中必须优先查找并使用与当前任务、当前子步骤和当前页面匹配的内容：\n\n"
            "a) 当前子步骤描述宽泛、抽象、不包含具体入口或操作路径时，必须使用 Function Map 将该子步骤落实为具体可执行动作。Map 提供的具体路径属于当前子步骤的实现方式，不算新增、拆分、重排或跨越子步骤。\n\n"
            "b) 当前子步骤尚未完成，但因引导、弹窗、异常页面、前置状态不满足或其他突发情况而无法直接执行时，必须先声明阻塞原因，再使用 Function Map 中匹配的处理规则恢复到可执行状态。恢复过程中允许插入必要动作；业务子步骤仍停留在当前项，阻塞解除后继续完成原子步骤，再进入下一项。\n\n"
            "c) Function Map 中存在与当前场景明确匹配的业务规则时，该规则在业务路径、具体操作和异常处理方面优先于模型自身常识、无依据猜测以及对宽泛步骤的自行解释。\n\n"
            "上述优先级不得覆盖当前截图中的直接事实，不得改变任务目标、测试对象、子步骤业务顺序、预期结果或完成条件。若 Function Map 没有匹配内容，必须明确按未命中处理，不得伪造 Map 规则。\n"
        )
    return (
        "\n## Function Map Usage Contract\n"
        "The Function Map body is provided in the first user message of this logical "
        "session. The field itself is optional; when absent, you must retain full "
        "execution capability using the task, substeps, and current screenshot alone.\n\n"
        "A Function Map is present for this run, so the caller deliberately selected it "
        "as relevant business execution context. Give it substantial weight for page "
        "relationships, concrete object identification, entry points and paths, test data, "
        "business terms, and exception or popup handling. Prefer it over generic model "
        "knowledge and unsupported guesses; do not ignore it without reason.\n\n"
        "It may correct an old name, entry point, or path while preserving the same task "
        "intent. It is not an additional task: it must not add, remove, merge, reorder, or "
        "skip substeps; replace an explicitly requested test object; change expected results "
        "or completion conditions; or serve as evidence that the task is complete.\n\n"
        "When directly visible facts in the current screenshot conflict with the Function "
        "Map, trust the directly visible facts. An element not currently visible is not by "
        "itself proof that the Function Map is wrong. Do not restate or summarize the Map "
        "body in reasoning; use only what the current decision needs.\n\n"
        "5. Scenario Priority and Invocation:\n\n"
        "The Function Map does not need to be followed mechanically on every turn. However, "
        "in the following scenarios, you must first look for and use content that matches the "
        "current task, current substep, and current page:\n\n"
        "a) When the current substep is broad or abstract and does not specify a concrete entry "
        "point or action path, you must use the Function Map to turn that substep into concrete, "
        "executable actions. A concrete path provided by the Map is an implementation of the "
        "current substep; it does not count as adding, splitting, reordering, or crossing substeps.\n\n"
        "b) When the current substep is not yet complete but cannot be executed directly because "
        "of onboarding, a popup, an unexpected page, an unmet prerequisite state, or another "
        "interruption, you must first state the reason for the blockage, then use the matching "
        "handling rule in the Function Map to restore an executable state. You may insert necessary "
        "recovery actions while remaining on the current business substep. Once the blockage is "
        "resolved, complete the original substep before moving to the next one.\n\n"
        "c) When the Function Map contains a business rule that clearly matches the current scenario, "
        "that rule takes priority for business paths, concrete actions, and exception handling over "
        "the model's generic knowledge, unsupported guesses, or its own interpretation of a broad substep.\n\n"
        "This priority must not override facts directly visible in the current screenshot or change "
        "the task goal, test object, business order of substeps, expected result, or completion conditions. "
        "If no Function Map content matches, explicitly treat it as a miss and never fabricate a Map rule.\n"
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
    "build_function_map_system_policy",
    "build_function_map_user_context",
]
