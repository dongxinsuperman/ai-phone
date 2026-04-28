"""Submission 终态事件 payload 构造。

输出契约严格对齐 ``codex后续计划表.md`` P1 广播字段：

.. code-block:: json

    {
      "event": "submission.item.terminal",
      "version": 1,
      "ts": "2026-04-22T03:12:05.121+00:00",
      "submissionId": "abc123...",
      "caseId": "login_001",
      "platform": "android",
      "state": "success | failed | cancelled",
      "statusReason": "completed | assert_failed | run_error | ...",
      "runId": "xxxxxxxx" | null,
      "deviceSerial": "R3CR70STPCK" | null,
      "deviceAlias": null,
      "startedAt": iso | null,
      "finishedAt": iso | null,
      "elapsedMs": 12345 | null,
      "steps": 8 | null,
      "tokenStats": { ... } | {},
      "reportUrl": "/files/reports/.../.html" | null,
      "origin": "external | internal"
    }

字段约定：

- ``reportUrl`` 为 ``null`` 表示 **没生成报告**（典型场景：item 在 queued 状态下
  被取消；或批次 submission_timeout 把未启动的 item 踢出）。
- ``runId`` 为 ``null`` 同理——没跑过 Run。
- ``elapsedMs`` 未启动或异常未拿到时间戳时为 ``null``。

此函数**不**落盘报告、**不**访问 Kafka，只负责"装字段"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import Run, Submission, SubmissionItem


SCHEMA_EVENT = "submission.item.terminal"
SCHEMA_VERSION = 1

SUBMISSION_EVENT = "submission.terminal"
SUBMISSION_EVENT_VERSION = 1


def build_terminal_event(
    *,
    item: SubmissionItem,
    submission: Optional[Submission] = None,
    run: Optional[Run] = None,
    report_url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """把 ORM 对象拼成可广播的扁平字典。

    注意：调用方必须保证 ``item`` 已经落到终态；本函数不会校验。
    """
    now_ts = (now or datetime.now(timezone.utc)).isoformat()

    started = item.started_at
    finished = item.finished_at
    elapsed_ms: Optional[int] = None
    if started and finished:
        try:
            elapsed_ms = max(0, int((finished - started).total_seconds() * 1000))
        except Exception:  # noqa: BLE001
            elapsed_ms = None

    steps: Optional[int] = None
    token_stats: Dict[str, Any] = {}
    if run is not None:
        steps = int(run.steps or 0) if run.steps is not None else None
        token_stats = run.token_summary or {}

    submission_name: Optional[str] = None
    if submission is not None:
        submission_name = submission.submission_name or submission.id

    return {
        "event": SCHEMA_EVENT,
        "version": SCHEMA_VERSION,
        "ts": now_ts,
        "submissionId": item.submission_id,
        "submissionName": submission_name or item.submission_id,
        "caseId": item.case_id,
        "caseName": (item.case_name or item.case_id),
        "platform": item.platform,
        "state": item.state,
        "statusReason": item.status_reason or None,
        "runId": item.run_id or None,
        "deviceSerial": item.device_serial or None,
        "deviceAlias": item.device_alias or None,
        "startedAt": started.isoformat() if started else None,
        "finishedAt": finished.isoformat() if finished else None,
        "elapsedMs": elapsed_ms,
        "steps": steps,
        "tokenStats": token_stats,
        "reportUrl": report_url,
        "origin": (submission.origin if submission is not None else None) or "internal",
    }


def build_submission_terminal_event(
    *,
    submission: Submission,
    items: List[SubmissionItem],
    summary_report_url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """submission 整批结束（所有 item 都终态）时广播一次。

    用途：让外部消费方不必"等齐 N 条 item.terminal 事件"也能知道批次收口。
    payload 只放轻量聚合字段，重数据（每条 item 的 reportUrl / steps）请通过
    ``GET /api/submissions/{id}`` 拉。

    summary_report_url 为 ``None`` 表示汇总 HTML 生成失败，调用方可降级用
    JSON 接口拉。
    """
    now_ts = (now or datetime.now(timezone.utc)).isoformat()

    counts: Dict[str, int] = {}
    plat_counts: Dict[str, int] = {}
    total_elapsed_ms = 0
    for it in items:
        counts[it.state] = counts.get(it.state, 0) + 1
        plat_counts[it.platform] = plat_counts.get(it.platform, 0) + 1
        if it.started_at and it.finished_at:
            try:
                total_elapsed_ms += max(
                    0, int((it.finished_at - it.started_at).total_seconds() * 1000)
                )
            except Exception:  # noqa: BLE001
                pass

    return {
        "event": SUBMISSION_EVENT,
        "version": SUBMISSION_EVENT_VERSION,
        "ts": now_ts,
        "submissionId": submission.id,
        "submissionName": submission.submission_name or submission.id,
        "origin": submission.origin,
        "submissionState": submission.state,
        "acceptedAt": submission.accepted_at.isoformat() if submission.accepted_at else None,
        "finishedAt": submission.finished_at.isoformat() if submission.finished_at else None,
        "totalItems": len(items),
        "counts": counts,
        "platformCounts": plat_counts,
        "totalElapsedMs": total_elapsed_ms,
        "summaryReportUrl": summary_report_url,
    }


__all__ = [
    "build_terminal_event",
    "build_submission_terminal_event",
    "SCHEMA_EVENT",
    "SCHEMA_VERSION",
    "SUBMISSION_EVENT",
    "SUBMISSION_EVENT_VERSION",
]
