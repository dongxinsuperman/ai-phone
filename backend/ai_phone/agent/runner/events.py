"""Runner 对外 emit 的事件常量与构造函数。

设计原则：
- 事件是纯 dict（JSON-safe），runner 本身不依赖 WS / HTTP，任何消费者都可以
  接入（命令行脚本 / Agent→Server WS / 单测断言）。
- 字段命名与 shared/protocol.py 中 WS 上行消息保持对齐，方便 Agent 直接转发
  给 Server 而不需要字段翻译。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

# ---- 事件类型常量 ----
EVT_RUN_START = "run_start"
EVT_RUN_FINISH = "run_finish"  # 成功（finished）或失败（assert_fail / 异常）
EVT_STEP_START = "step_start"
EVT_STEP_END = "step_end"
EVT_LOG = "log"                 # Sonic 风格日志条目
EVT_SCREENSHOT = "screenshot"   # 操作前/后截图 bytes 让上层决定是否上传
EVT_THOUGHT = "thought"
EVT_ACTION = "action"
EVT_EXEC_RESULT = "exec_result"
EVT_TOKEN_SUMMARY = "token_summary"


def _now_ms() -> int:
    return int(time.time() * 1000)


def make_event(type_: str, run_id: str, step: Optional[int] = None, **payload: Any) -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "type": type_,
        "run_id": run_id,
        "ts": _now_ms(),
    }
    if step is not None:
        evt["step"] = step
    evt.update(payload)
    return evt


def log_event(
    run_id: str,
    level: int,
    title: str,
    content: str,
    step: Optional[int] = None,
) -> Dict[str, Any]:
    """Sonic-style 日志事件。level: 1=info, 2=warn, 3=error。"""
    return make_event(
        EVT_LOG, run_id, step=step, level=level, title=title, content=content
    )
