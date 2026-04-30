"""VLM Runner 包：主循环 + 稳定检测 + pHash + 事件常量。

外接执行器（Midscene 等）通过 :mod:`ai_phone.agent.runner.factory` 的
``build_runner`` 工厂分流；详见仓库根 ``Midscene执行器接入方案.md``。
"""
from .events import (
    EVT_ACTION,
    EVT_EXEC_RESULT,
    EVT_LOG,
    EVT_RUN_FINISH,
    EVT_RUN_START,
    EVT_SCREENSHOT,
    EVT_STEP_END,
    EVT_STEP_START,
    EVT_THOUGHT,
    EVT_TOKEN_SUMMARY,
    log_event,
    make_event,
)
from .factory import build_runner
from .midscene_runner import MidsceneRunner
from .phash import compute_phash, diff_rate, hamming_distance
from .stability import StabilityResult, wait_page_stable_pixel
from .vlm_loop import RunResult, VLMRunner

__all__ = [
    "VLMRunner",
    "MidsceneRunner",
    "build_runner",
    "RunResult",
    "StabilityResult",
    "wait_page_stable_pixel",
    "compute_phash",
    "diff_rate",
    "hamming_distance",
    "log_event",
    "make_event",
    "EVT_RUN_START",
    "EVT_RUN_FINISH",
    "EVT_STEP_START",
    "EVT_STEP_END",
    "EVT_LOG",
    "EVT_SCREENSHOT",
    "EVT_THOUGHT",
    "EVT_ACTION",
    "EVT_EXEC_RESULT",
    "EVT_TOKEN_SUMMARY",
]
