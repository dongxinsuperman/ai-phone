"""对外 ``/api/submissions`` 匿名路由（第 3 梯队）。

与内部 ``/api/internal/submissions`` 的区别：

============  ===========================  ==============================
方面          内部（第 2 梯队）            外部（本文件）
============  ===========================  ==============================
鉴权          Authorization: Bearer        **匿名**，靠网络隔离 / 防火墙
origin        ``internal``                 ``external``
15 天过期     不关心                       终态后 ``submission_external_retention_days``
                                           天外查询统一 ``404 expired``
数据模型      共用 Submission/SubmissionItem
scheduler     共用一个 SubmissionScheduler 实例
============  ===========================  ==============================

对外契约冻结清单（v1）：

- 请求体必须是 JSON 数组 ``[{}, {}]``；不允许 ``{"items":[...]}`` 套壳。
- ``submissionId`` 由执行器生成并在响应里返回；外部不传入。
- 查询 / 取消都用 ``submissionId + caseId + platform`` 作为外部主键。
- 广播层（Kafka / stdout）只发终态，中间态不发——想拿中间态 → 轮询 GET。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from typing import AsyncGenerator

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings

from ..db import get_session_factory
from ..models import Run, RunLog, RunStep, Submission, SubmissionItem
from ..scheduler import AdmissionError, SubmissionScheduler, get_scheduler
from .reports import report_url_for_item


# 就地定义 DB session 依赖。原本想复用 ``api/_deps.py`` 的 ``DBSession``，但
# ``api/__init__.py`` 会把 public_routes 的 router 拉回来注册，直接 import
# ``..api._deps`` 会触发循环。本质上 DB session dep 非常薄，复制一份更干净。
async def _db_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


DBSession = Depends(_db_session)

router = APIRouter(prefix="/api/submissions", tags=["submissions"])


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _scheduler(request: Request) -> SubmissionScheduler:
    sched = getattr(request.app.state, "scheduler", None) or get_scheduler()
    if sched is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scheduler 未初始化",
        )
    return sched


def _external_retention_days() -> int:
    return int(get_settings().submission_external_retention_days)


def _external_expired(sub: Submission) -> bool:
    """对外 API 视角的"过期"：submission 已终态 + 终态时间超过 retention。

    ``accepted`` 状态的 submission 哪怕受理时间很早也不算过期（它还在跑，
    外部查询能看到 queued/running 状态）。有些终态路径可能没设 finished_at
    （老数据），这里兜底用 ``expire_at``（accepted_at + 3h）+ retention。
    """
    now = datetime.now(timezone.utc)
    retention = timedelta(days=_external_retention_days())
    if sub.state == "accepted":
        return False
    base = sub.finished_at or sub.expire_at or sub.accepted_at
    if base is None:
        return False
    # 兜底：finished_at 可能是 naive，统一对齐 UTC 再比较
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (now - base) > retention


def _raise_if_expired(sub: Submission) -> None:
    if _external_expired(sub):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "expired",
                "message": (
                    f"submission 已超过对外可查窗口（{_external_retention_days()} 天），"
                    "元数据与报告仍在服务端保留，但对外不再开放查询"
                ),
            },
        )


def _state_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for it in items:
        s = it.get("state") or "?"
        counts[s] = counts.get(s, 0) + 1
    return counts


def _serialize_submission(sub: Submission, items: List[SubmissionItem]) -> Dict[str, Any]:
    out = sub.to_dict()
    item_dicts: List[Dict[str, Any]] = []
    for it in items:
        row = it.to_dict()
        # 对外响应里直接把 reportUrl 附上，方便调用方不订阅 Kafka 也能拉到
        if it.state in ("success", "failed") and it.run_id:
            row["report_url"] = report_url_for_item(it)
        else:
            row["report_url"] = None
        item_dicts.append(row)
    out["items"] = item_dicts
    out["counts"] = _state_counts(item_dicts)
    out["external_retention_days"] = _external_retention_days()
    return out


# ---------------------------------------------------------------------------
# POST /api/submissions  —— 匿名投递
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_submission(
    request: Request,
    body: Any = Body(
        ...,
        description=(
            "支持两种格式："
            "1) 老：JSON 数组 [{caseId, platform, runContent, deviceAlias?, caseName?}, ...]；"
            "2) 新：wrapper 对象 {submissionName?, items: [...]}（推荐，submissionName 用于报告/大盘展示，缺省回落 submissionId）"
        ),
    ),
) -> Dict[str, Any]:
    """匿名受理一批 item。"""
    sched = _scheduler(request)
    try:
        payload = await sched.submit(body, origin="external")
    except AdmissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "rejectReason": exc.reason,
                "rejectDetail": exc.detail,
                "index": exc.index,
            },
        )
    return payload


# ---------------------------------------------------------------------------
# GET /api/submissions/{id}
# ---------------------------------------------------------------------------


async def _load_submission_or_404(session: AsyncSession, sub_id: str) -> Submission:
    sub = await session.get(Submission, sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="submission not found")
    _raise_if_expired(sub)
    return sub


@router.get("/{sub_id}")
async def get_submission(
    sub_id: str,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    sub = await _load_submission_or_404(session, sub_id)
    res = await session.execute(
        select(SubmissionItem)
        .where(SubmissionItem.submission_id == sub_id)
        .order_by(SubmissionItem.enqueued_at.asc())
    )
    items = list(res.scalars().all())
    return _serialize_submission(sub, items)


# ---------------------------------------------------------------------------
# GET /api/submissions/{id}/items/{case_id}/{platform}
# ---------------------------------------------------------------------------


@router.get("/{sub_id}/items/{case_id}/{platform}")
async def get_submission_item(
    sub_id: str,
    case_id: str,
    platform: str,
    session: AsyncSession = DBSession,
    include_run: bool = Query(True, description="是否返回 Run/Step/Log 详情"),
) -> Dict[str, Any]:
    """单条 item 详情——包含 Run 摘要、步骤、日志、报告 URL。"""
    platform_norm = (platform or "").strip().lower()

    sub = await _load_submission_or_404(session, sub_id)
    res = await session.execute(
        select(SubmissionItem).where(
            SubmissionItem.submission_id == sub_id,
            SubmissionItem.case_id == case_id,
            SubmissionItem.platform == platform_norm,
        )
    )
    item = res.scalars().first()
    if item is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="submission item not found",
        )

    row = item.to_dict()
    row["report_url"] = (
        report_url_for_item(item)
        if item.run_id and item.state in ("success", "failed")
        else None
    )

    run_detail: Optional[Dict[str, Any]] = None
    steps: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    if include_run and item.run_id:
        run = await session.get(Run, item.run_id)
        if run is not None:
            run_detail = run.to_dict()
            steps_res = await session.execute(
                select(RunStep)
                .where(RunStep.run_id == item.run_id)
                .order_by(RunStep.step.asc(), RunStep.id.asc())
            )
            steps = [s.to_dict() for s in steps_res.scalars().all()]
            logs_res = await session.execute(
                select(RunLog)
                .where(RunLog.run_id == item.run_id)
                .order_by(RunLog.ts.asc(), RunLog.id.asc())
            )
            logs = [l.to_dict() for l in logs_res.scalars().all()]

    return {
        "submission_id": sub_id,
        "submission_name": sub.submission_name or sub_id,
        "submission_state": sub.state,
        "item": row,
        "run": run_detail,
        "steps": steps,
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# POST /api/submissions/{id}/cancel  —— 匿名整批取消
# ---------------------------------------------------------------------------


@router.post("/{sub_id}/cancel")
async def cancel_submission_route(sub_id: str, request: Request) -> Dict[str, Any]:
    sched = _scheduler(request)
    try:
        return await sched.cancel_submission(sub_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /api/submissions/{id}/cases/{case_id}/cancel?platform=...  —— 匿名单条
# ---------------------------------------------------------------------------


@router.post("/{sub_id}/cases/{case_id}/cancel")
async def cancel_submission_item_route(
    sub_id: str,
    case_id: str,
    request: Request,
    platform: str = Query(..., description="android / ios / harmony"),
) -> Dict[str, Any]:
    sched = _scheduler(request)
    try:
        return await sched.cancel_item(sub_id, case_id, platform)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


__all__ = ["router"]
