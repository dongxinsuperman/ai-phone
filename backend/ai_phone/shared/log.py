"""Sonic 风格日志事件构造器。

对齐 Sonic `logHandler.sendStepLog(level, title, detail)`：
- level: 1=info（蓝）/ 2=warn（黄）/ 3=error（红），前端按级别着色
- title: 短标题（如「决策耗时」「Token消耗」）
- detail: 详细内容

Agent 侧通过 emit() 把 dict 推给 Server；Server 再广播给订阅该设备的浏览器 WS。
"""
from __future__ import annotations

import time
from typing import Optional

from ai_phone.shared.protocol import MSG_LOG, LogLevel, LogMsg

LEVEL_INFO: LogLevel = 1
LEVEL_WARN: LogLevel = 2
LEVEL_ERROR: LogLevel = 3


def make_log(
    level: LogLevel,
    title: str,
    detail: str = "",
    *,
    run_id: Optional[int] = None,
    step_index: Optional[int] = None,
) -> LogMsg:
    """构造一条日志 WS 消息（dict）。序列化即 JSON 推到 WS 上。"""
    msg: LogMsg = {
        "type": MSG_LOG,
        "level": level,
        "title": title,
        "detail": detail,
        "timestamp": time.time(),
    }
    if run_id is not None:
        msg["run_id"] = run_id
    if step_index is not None:
        msg["step_index"] = step_index
    return msg


def info(title: str, detail: str = "", **kw) -> LogMsg:
    return make_log(LEVEL_INFO, title, detail, **kw)


def warn(title: str, detail: str = "", **kw) -> LogMsg:
    return make_log(LEVEL_WARN, title, detail, **kw)


def error(title: str, detail: str = "", **kw) -> LogMsg:
    return make_log(LEVEL_ERROR, title, detail, **kw)
