"""V3 救援失败后的完整首跑边界；不续接旧轨迹，不修改首跑执行器。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ai_phone.agent.runner.events import EVT_LOG, EVT_RUN_FINISH


@dataclass(frozen=True)
class V3RestartRequest:
    reason: str
    step_offset: int
    elapsed_ms: int


def restart_report_event(event: Dict[str, Any], restart: V3RestartRequest) -> Dict[str, Any]:
    """首跑内部分步仍从头开始；仅在报告/新归档记录中避开旧缓存阶段的 step。

    不拼接旧历史、不修改 reason/ok/token_stats，保持原首跑断言与终态协议。
    新归档 source_step 对应报告的新阶段位置，缓存 action index 仍由归档从1生成。
    """
    output = dict(event)
    if type(event.get("step")) is int:
        output["step"] = event["step"] + restart.step_offset
    if event.get("type") == EVT_RUN_FINISH:
        output["steps"] = int(event.get("steps") or 0) + restart.step_offset
        output["elapsed_ms"] = int(event.get("elapsed_ms") or 0) + restart.elapsed_ms
    elif event.get("type") == EVT_LOG and event.get("title") == "任务总耗时":
        output["title"] = "完整首跑阶段耗时"
    return output
