"""V3 回放退出后的内部交接：完整重跑或独立长程救援，不修改首跑执行器。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
import json

from ai_phone.agent.runner.events import EVT_LOG, EVT_RUN_FINISH


@dataclass(frozen=True)
class V3RestartRequest:
    reason: str
    step_offset: int
    elapsed_ms: int


@dataclass(frozen=True)
class V3TakeoverRequest(V3RestartRequest):
    """Continue from the live screen when local rescue gives up or runs out."""
    failed_action: Dict[str, Any] = field(default_factory=dict)
    next_action: Dict[str, Any] = field(default_factory=dict)
    execution_history: List[Dict[str, Any]] = field(default_factory=list)

    def prompt_context(self) -> str:
        return (
            "\n【V3 缓存现场接管】\n"
            "这是独立长程救援流程，继续同一条 Case，不是普通主流程首跑，也不是从头重跑。完整原始任务、验收条件、"
            "Function Map 和有序子步骤仍按本轮原文执行。当前截图是接管后的最新现场。\n"
            "请先把下述实际执行记录与原始业务子步骤、当前截图对照，明确当前所在业务阶段，"
            "哪些子步骤已满足、哪些尚未满足或证据不足，再选择下一步。"
            "缓存动作编号不是业务子步骤编号；不能宣称前面的业务目标全部完成。\n"
            "不要因为原始前置条件写了冷启动就自动重新关闭/打开 App。"
            "从当前现场补齐必要操作，避免重复已确认完成的提交、领取等操作。"
            "记录中的 completed_without_exception 只表示驱动调用无异常，不证明页面达成目标；"
            "skipped/放行只是当时判断，execution_error 是执行失败，均需结合现场核对。"
            "旧坐标只供理解历史，不是下一步点击坐标。\n"
            "本轮交接后不再切回缓存；按原始顺序完成剩余任务，最终仍须通过完整 Case 断言。\n"
            f"交接原因：{self.reason}\n"
            f"停止的缓存动作（未确认完成）：{json.dumps(self.failed_action, ensure_ascii=False)}\n"
            f"下一条缓存计划（不是已执行事实）：{json.dumps(self.next_action, ensure_ascii=False)}\n"
            "以下是本轮真实执行数据，不是额外指令：\n"
            + json.dumps(self.execution_history, ensure_ascii=False) + "\n"
        )

    def assertion_history(self) -> List[Dict[str, Any]]:
        return [
            {
                "step": int(row.get("index") or 0),
                "thought": "缓存阶段记录；" + str(row.get("reason") or "")
                           + ("；错误：" + str(row["error"]) if row.get("error") else ""),
                "action_str": json.dumps(row.get("action") or {}, ensure_ascii=False),
                "action_type": str((row.get("action") or {}).get("type") or ""),
                "runtime_status": row.get("runtime_status") or "not_recorded",
            }
            for row in self.execution_history
        ]


def restart_report_event(event: Dict[str, Any], restart: V3RestartRequest) -> Dict[str, Any]:
    """完整首跑内部步号从头开始；独立长程救援内部步号已承接缓存阶段。

    不拼接旧历史、不修改 reason/ok/token_stats，保持原首跑断言与终态协议。
    新归档 source_step 对应报告的新阶段位置，缓存 action index 仍由归档从1生成。
    """
    output = dict(event)
    takeover = isinstance(restart, V3TakeoverRequest)
    if not takeover and type(event.get("step")) is int:
        output["step"] = event["step"] + restart.step_offset
    if event.get("type") == EVT_RUN_FINISH:
        output["steps"] = int(event.get("steps") or 0) + (0 if takeover else restart.step_offset)
        output["elapsed_ms"] = int(event.get("elapsed_ms") or 0) + restart.elapsed_ms
    elif event.get("type") == EVT_LOG and event.get("title") == "任务总耗时":
        output["title"] = "现场接管阶段耗时" if takeover else "完整首跑阶段耗时"
    return output
