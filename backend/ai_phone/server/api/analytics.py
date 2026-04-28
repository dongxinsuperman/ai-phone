"""大盘 API：``/api/internal/analytics/*``。

走内部 Bearer 鉴权（和 ``/api/internal/submissions`` 共用一套 token）。对外不暴露，
调用方只有前端 web 和开发排错工具。

路由清单：

- ``GET  /api/internal/analytics/summary?date=YYYY-MM-DD`` → 当日大盘切片
- ``POST /api/internal/analytics/ai-analyze`` body: ``{"date": "YYYY-MM-DD"}`` → 调豆包同步分析
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings

from ..analytics import (
    AnalyticsAIClient,
    AnalyticsAIError,
    aggregate_day,
    parse_date,
)
from ..analytics.aggregator import _tz  # noqa: WPS450 —— 内部复用，不公开
from ._deps import DBSession
from .submissions import RequireBearer

router = APIRouter(prefix="/api/internal/analytics", tags=["internal-analytics"])


def _ensure_ai_allowed(d: date) -> None:
    """AI 分析允许的日期范围校验：只能查今天往前若干天，不能查未来。"""
    settings = get_settings()
    today_local = datetime.now(_tz()).date()
    max_age = int(settings.analytics_ai_max_age_days or 3)
    earliest = today_local - timedelta(days=max_age - 1)
    if d > today_local:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不允许分析未来日期 {d.isoformat()}",
        )
    if d < earliest:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"AI 分析只允许在最近 {max_age} 天内（{earliest.isoformat()} 起），"
                f"收到 {d.isoformat()}。历史数据请直接查大盘 summary。"
            ),
        )


@router.get("/summary", dependencies=[RequireBearer])
async def get_summary(
    session: AsyncSession = DBSession,
    date_str: Optional[str] = Query(None, alias="date", description="本地日期 YYYY-MM-DD，缺省 = 今天"),
) -> Dict[str, Any]:
    """拉单日大盘切片；无条件允许任意历史日期。

    响应里附带 ``display`` 开关（纯前端渲染用，后端所有数据照常返回）：
    ``{"token": bool, "stability": bool}``；由 ``AI_PHONE_ANALYTICS_SHOW_TOKEN`` /
    ``AI_PHONE_ANALYTICS_SHOW_STABILITY`` 控制。关掉后前端会整块隐藏这两张卡。
    """
    try:
        d = parse_date(date_str)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    snapshot = await aggregate_day(session, d)
    settings = get_settings()
    snapshot["display"] = {
        "token": bool(settings.analytics_show_token),
        "stability": bool(settings.analytics_show_stability),
    }
    return snapshot


@router.post("/ai-analyze", dependencies=[RequireBearer])
async def ai_analyze(
    body: Dict[str, Any] = Body(default_factory=dict),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    """手动触发：同步调豆包生成中文分析文本。

    请求体：``{"date": "YYYY-MM-DD"}``。缺省 = 今天。
    """
    try:
        d = parse_date(str(body.get("date") or "").strip() or None)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    _ensure_ai_allowed(d)

    snapshot = await aggregate_day(session, d)
    total_items = int(snapshot.get("totalItems") or 0)
    # 样本不足硬拦一下：避免"今天还没开始跑"就去撩模型花钱
    if total_items == 0:
        return {
            "date": d.isoformat(),
            "model": None,
            "analyzedAt": datetime.now(_tz()).isoformat(),
            "text": f"{d.isoformat()} 无任何执行记录，样本不足，无需 AI 分析。",
            "elapsedMs": 0,
            "tokenUsage": None,
            "skipped": True,
        }

    client = AnalyticsAIClient()
    try:
        result = await client.analyze(snapshot)
    except AnalyticsAIError as exc:
        logger.warning("[analytics] AI 分析失败 date={} err={}", d.isoformat(), exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return {
        "date": d.isoformat(),
        "model": result.model,
        "analyzedAt": datetime.now(_tz()).isoformat(),
        "text": result.text,
        "elapsedMs": result.elapsed_ms,
        "tokenUsage": {
            "promptTokens": result.prompt_tokens,
            "completionTokens": result.completion_tokens,
            "totalTokens": result.total_tokens,
        },
        "skipped": False,
    }
