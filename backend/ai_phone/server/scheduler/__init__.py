"""v1 第 2 梯队：内部排队 + 调度器。

对外只暴露 :class:`SubmissionScheduler`。它负责：

- 接收 submission（按平台分池排队）
- 轮询可调度 item，挑一台 ready 设备派发
- 监听 run 终态，落位 item 状态，驱动下一轮 drain
- 批次级 / item 级超时守护

模块只读 `Hub` / `DeviceLockStore`，不修改它们的结构，也不碰既有的
`/api/runs` 派发路径——调度器复用 WS `start_run` 协议（保持
`run_id + goal` 不变），这条承诺写在 `codex后续计划表.md` 的
v1 冻结清单里。
"""

from .service import (
    AdmissionError,
    ItemDraft,
    SubmissionScheduler,
    get_scheduler,
    set_scheduler,
)

__all__ = [
    "AdmissionError",
    "ItemDraft",
    "SubmissionScheduler",
    "get_scheduler",
    "set_scheduler",
]
