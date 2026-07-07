"""v1 第 2 梯队内部 HTTP 入口：/api/internal/submissions。

只对"内部"开放——用 ``Authorization: Bearer <token>`` 头校验，token 默认
复用 ``settings.agent_token``（也可以用 ``AI_PHONE_SUBMISSION_INTERNAL_TOKEN``
单独指定）。对外 API（第 3 梯队）走独立路径和独立鉴权，不共享本文件。

路由清单：

- ``POST /api/internal/submissions``               —— 投递一批 item（body: ``[{}, {}]``）
- ``GET  /api/internal/submissions``               —— 列表（给 Web 队列总览页用）
- ``GET  /api/internal/submissions/{id}``          —— 详情
- ``POST /api/internal/submissions/{id}/cancel``   —— 取消整批（queued → cancelled; running → stop_run）
- ``POST /api/internal/submissions/{id}/cases/{case_id}/cancel?platform=<p>``
                                                   —— 取消单条 item
- ``GET  /api/internal/scheduler/snapshot``        —— 调度器内存快照（排错用）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings

from ..models import Submission, SubmissionItem
from ..scheduler import AdmissionError, SubmissionScheduler, get_scheduler
from ._deps import DBSession

router = APIRouter(prefix="/api/internal", tags=["internal-submissions"])


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def _expected_token() -> str:
    s = get_settings()
    return s.submission_internal_token or s.agent_token


async def _require_bearer(authorization: Optional[str] = Header(default=None)) -> None:
    """Bearer 校验。空 token 配置下也要求 ``Bearer dev``，防止裸机部署忘配置裸
    暴露；真要关，设置 ``AI_PHONE_SUBMISSION_INTERNAL_TOKEN`` 为特定值即可。
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization: Bearer header",
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization 格式必须是 'Bearer <token>'",
        )
    if parts[1] != _expected_token():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token 不匹配",
        )


RequireBearer = Depends(_require_bearer)


def _scheduler(request: Request) -> SubmissionScheduler:
    sched = getattr(request.app.state, "scheduler", None) or get_scheduler()
    if sched is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scheduler 未初始化（server lifespan 没启起来？）",
        )
    return sched


SchedulerDep = Depends(_scheduler)


# ---------------------------------------------------------------------------
# POST /api/internal/submissions
# ---------------------------------------------------------------------------


@router.post("/submissions", status_code=status.HTTP_201_CREATED, dependencies=[RequireBearer])
async def create_submission(
    body: Any = Body(...),
    sched: SubmissionScheduler = SchedulerDep,
) -> Dict[str, Any]:
    """准入一批 item。请求体格式（v1.7 唯一形态）：

    .. code-block:: json

        {
          "submissionName": "...",
          "functionMapContext": "...", // 可选，批次级执行参考
          "items": [
            {
              "caseId": "...",
              "caseName": "...",
              "runContent": "...",
              "platforms": ["android", ...],
              "functionMapContext": "...", // 可选，当前 item 追加执行参考
              "deviceAliasPools": {"android": ["A1","B1"], ...}
            }
          ]
        }

    详见 :func:`ai_phone.server.scheduler.service.parse_and_validate`。
    """
    try:
        payload = await sched.submit(body, origin="internal")
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
# GET /api/internal/submissions
# ---------------------------------------------------------------------------


@router.get("/submissions", dependencies=[RequireBearer])
async def list_submissions(
    session: AsyncSession = DBSession,
    state: Optional[str] = Query(None, description="accepted / cancelled / expired"),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    stmt = select(Submission).order_by(Submission.accepted_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(Submission.state == state)
    res = await session.execute(stmt)
    subs = list(res.scalars().all())
    sub_ids = [sub.id for sub in subs]

    items_by_sub: Dict[str, List[Dict[str, Any]]] = {sub_id: [] for sub_id in sub_ids}
    if sub_ids:
        items_res = await session.execute(
            select(SubmissionItem)
            .where(SubmissionItem.submission_id.in_(sub_ids))
            .order_by(SubmissionItem.submission_id.asc(), SubmissionItem.enqueued_at.asc())
        )
        for it in items_res.scalars().all():
            items_by_sub.setdefault(it.submission_id, []).append(it.to_dict())

    out: List[Dict[str, Any]] = []
    for sub in subs:
        items = items_by_sub.get(sub.id, [])
        row = sub.to_dict()
        row["items"] = items
        row["counts"] = _state_counts(items)
        out.append(row)
    return out


def _state_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for it in items:
        s = it.get("state") or "?"
        counts[s] = counts.get(s, 0) + 1
    return counts


@router.get("/submissions/{sub_id}", dependencies=[RequireBearer])
async def get_submission(sub_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    sub = await session.get(Submission, sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="submission not found")
    res = await session.execute(
        select(SubmissionItem)
        .where(SubmissionItem.submission_id == sub_id)
        .order_by(SubmissionItem.enqueued_at.asc())
    )
    items = [it.to_dict() for it in res.scalars().all()]
    row = sub.to_dict()
    row["items"] = items
    row["counts"] = _state_counts(items)
    return row


# ---------------------------------------------------------------------------
# POST /api/internal/submissions/{id}/cancel
# ---------------------------------------------------------------------------


@router.post("/submissions/{sub_id}/cancel", dependencies=[RequireBearer])
async def cancel_submission(
    sub_id: str,
    sched: SubmissionScheduler = SchedulerDep,
) -> Dict[str, Any]:
    try:
        return await sched.cancel_submission(sub_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post(
    "/submissions/{sub_id}/cases/{case_id}/cancel",
    dependencies=[RequireBearer],
)
async def cancel_submission_item(
    sub_id: str,
    case_id: str,
    platform: str = Query(..., description="android / ios / harmony"),
    sched: SubmissionScheduler = SchedulerDep,
) -> Dict[str, Any]:
    try:
        return await sched.cancel_item(sub_id, case_id, platform)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /api/internal/scheduler/snapshot
# ---------------------------------------------------------------------------


@router.get("/scheduler/snapshot", dependencies=[RequireBearer])
async def scheduler_snapshot(sched: SubmissionScheduler = SchedulerDep) -> Dict[str, Any]:
    return sched.snapshot()
