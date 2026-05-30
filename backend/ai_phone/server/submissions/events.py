"""Submission 终态事件 payload 构造。

输出契约（v2 字段集，version 字段保留 1，新增字段属兼容追加）：

item 终态事件（``submission.item.terminal``）字段一览：

- ``event`` / ``version`` / ``ts``: 事件元数据
- ``submissionId`` / ``submissionName``: 批次维度
- ``itemId``: 该执行单元主键，方便消费方一键定位日志
- ``caseId`` / ``caseName`` / ``platform``: case 维度
- ``engine``: 实际跑这条 item 的执行引擎（``vlm`` / ``midscene``）；
  没绑 Run 时为 ``null``
- ``state`` / ``statusReason``: 终态结果（详见 scheduler 11 项 statusReason）
- ``runId`` / ``deviceSerial``: 实际派发结果；queued 阶段被收尾时为 ``null``
- ``deviceAliasPool``: 投递时声明的别名池（v1.7 池语义）
- ``enqueuedAt``: 入队时刻，永远有值
- ``startedAt`` / ``finishedAt`` / ``elapsedMs``: 执行时段；未启动时三者均 None
- ``steps`` / ``tokenStats``: 执行规模摘要（来自 Run）
- ``reportUrl``: HTML 单条报告地址；queued 阶段被收尾时为 ``null``
- ``origin``: ``external`` / ``internal``

字段约定：

- ``reportUrl`` 为 ``null`` 表示没生成报告（典型场景：item 在 queued 状态下被
  取消；或批次 submission_timeout 把未启动的 item 踢出）。
- ``runId`` / ``engine`` 为 ``null`` 同理——没跑过 Run。
- ``elapsedMs`` 未启动或异常未拿到时间戳时为 ``null``。

此函数不落盘报告、不访问 Kafka，只负责装字段。
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

    # Run.engine 为空字符串时按 'vlm' 兜底，跟 Run.to_dict 的行为对齐
    engine: Optional[str] = None
    if run is not None:
        engine = run.engine or "vlm"

    return {
        "event": SCHEMA_EVENT,
        "version": SCHEMA_VERSION,
        "ts": now_ts,
        "submissionId": item.submission_id,
        "submissionName": submission_name or item.submission_id,
        "itemId": item.id,
        "caseId": item.case_id,
        "caseName": (item.case_name or item.case_id),
        "platform": item.platform,
        "engine": engine,
        "state": item.state,
        "statusReason": item.status_reason or None,
        "runId": item.run_id or None,
        "deviceSerial": item.device_serial or None,
        "deviceAliasPool": list(item.device_alias_pool or []) or None,
        "retryMax": item.effective_retry_max or 0,
        "attempts": item.attempts or (run.attempts if run is not None else 0),
        "enqueuedAt": item.enqueued_at.isoformat() if item.enqueued_at else None,
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

    聚合字段：
      - ``counts``: 按 state 聚合，业务方看总盘
      - ``platformCounts``: 按 platform 聚合，业务方看每端跑了几条
      - ``platformStateCounts``: 端 × 状态 二维矩阵（嵌套字典），业务方一眼
        看出"每端各状态分别多少条"，不用回拉每条 item 自己算
    """
    now_ts = (now or datetime.now(timezone.utc)).isoformat()

    counts: Dict[str, int] = {}
    plat_counts: Dict[str, int] = {}
    plat_state_counts: Dict[str, Dict[str, int]] = {}
    total_elapsed_ms = 0
    for it in items:
        counts[it.state] = counts.get(it.state, 0) + 1
        plat_counts[it.platform] = plat_counts.get(it.platform, 0) + 1
        # 二维矩阵：先按 platform 分桶，再按 state 计数
        bucket = plat_state_counts.setdefault(it.platform, {})
        bucket[it.state] = bucket.get(it.state, 0) + 1
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
        "requestedRetryMax": submission.requested_retry_max,
        "effectiveRetryMax": submission.effective_retry_max or 0,
        "acceptedAt": submission.accepted_at.isoformat() if submission.accepted_at else None,
        "finishedAt": submission.finished_at.isoformat() if submission.finished_at else None,
        "totalItems": len(items),
        "counts": counts,
        "platformCounts": plat_counts,
        "platformStateCounts": plat_state_counts,
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
