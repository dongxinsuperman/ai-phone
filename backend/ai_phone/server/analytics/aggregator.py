"""单日大盘聚合：submissions / items / runs → 扁平可 JSON 序列化的切片。

原则：
- 只 **读**，全程不写库
- 只看本地日历日（用 ``settings.analytics_timezone`` 转 UTC 区间）
- 设备健康刻意分成：
  - ``devices.today``：当日被调度器用过的设备 + 当日成绩
  - ``devices.health``：所有历史 Run 的成功率聚合（和日期无关，用来判断"这台机器靠不靠谱"）
- 失败详情里 ``firstErrorLog`` 只取一条最早的 ``level >= 2`` 日志标题，够定位、不喂 AI 一大坨
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings

from ..models import Device, DeviceAlias, Run, RunLog, Submission, SubmissionItem
from ..submissions.paths import item_report_url, submission_summary_url


# 终态 item state 集合；复用次数太多就拎到模块级常量
_TERMINAL_ITEM_STATES = ("success", "failed", "cancelled")
# 当日每条失败 item 附带的 "错误首日志"：只看 warn/error，避免把 info 的业务提示误当错误
_ERROR_LOG_LEVEL_MIN = 2

# 「平台原因」白名单 —— 只有这些 statusReason 才算稳定性事件。
# 业务断言失败（assert_failed）、用户主动取消（cancelled_by_request）都算
# "业务/人为"，不进稳定性分母。这样吞吐看全量、稳定性看"平台靠不靠谱"，
# 两件事彻底解耦。
#
# ─── 和 scheduler.STATUS_REASONS 的对应关系 ────────────────────────────
# 之前这里写的是一套老 reason（vlm_format_invalid / stuck_no_progress /
# unknown_action / device_offline / internal_error / step_limit / user_abort），
# 那些值在 v1 里**没人 emit**——scheduler 实际发出来的只有 v1 11 项里的 10 项
# 非成功值。老字典会让稳定率永远虚高（大量 fail item 归不到任一桶，走到
# ``other_terminated`` 冷区），口径一错 KPI 就失真。
#
# 这一版直接锚到 scheduler.STATUS_REASONS（11 项：成功 1 + 平台 8 + 业务 2）：
#
#   平台类（8）    ← 进 platformFailureCount / failureByReason
#       run_timeout / queue_timeout / submission_timeout /
#       stuck_detected / vlm_unavailable / device_unavailable /
#       executor_resource_lost / executor_error
#   业务类（2）    ← 只计 businessFailureCount，不进分母
#       assert_failed / cancelled_by_request
#   成功类（1）    ← completed；不进任何分类
#
# 两个 frozenset 的并集必须严格 = STATUS_REASONS \\ {"completed"}；
# 新增 statusReason 时这里要跟着挪，否则会退回到 ``other_terminated`` 里漂走。
PLATFORM_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "run_timeout",
        "queue_timeout",
        "submission_timeout",
        "stuck_detected",
        "vlm_unavailable",
        "device_unavailable",
        "executor_resource_lost",
        "executor_error",
    }
)
BUSINESS_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "assert_failed",
        "cancelled_by_request",
    }
)


# ---------------------------------------------------------------------------
# 时区 / 日期工具
# ---------------------------------------------------------------------------
def _tz():
    """按 settings 拉时区；zoneinfo 缺失或名字写错时回落 UTC。"""
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(get_settings().analytics_timezone or "Asia/Shanghai")
    except Exception:  # noqa: BLE001
        return timezone.utc


def parse_date(s: Optional[str]) -> date:
    """把 ``YYYY-MM-DD`` 字符串解析为 ``date``；缺省或空 → 返回"今天"（本地时区）。"""
    if not s:
        return datetime.now(_tz()).date()
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"date 参数必须是 YYYY-MM-DD 格式，收到 {s!r}") from exc


def local_day_range(d: date) -> Tuple[datetime, datetime]:
    """把本地日历日转成 ``[start_utc, end_utc)`` 半开区间。

    例如 ``2026-04-18 Asia/Shanghai`` → ``[2026-04-17 16:00 UTC, 2026-04-18 16:00 UTC)``。
    DB 里的时间戳都是 UTC，拿这个区间去过滤就能天然对齐"用户眼里的一天"。
    """
    tz = _tz()
    start_local = datetime.combine(d, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 聚合：submissions / items / runs
# ---------------------------------------------------------------------------
@dataclass
class _DayBundle:
    """当日拉出来的所有原始行，算完切片就扔掉，不暴露给调用方。"""
    submissions: List[Submission]
    items: List[SubmissionItem]
    runs_by_id: Dict[str, Run]
    # item_id → 最早一条 level>=2 日志（title, ts）；没有日志的 item 不在表里
    first_error_log: Dict[str, Tuple[str, Optional[datetime]]]


async def _load_day_bundle(session: AsyncSession, d: date) -> _DayBundle:
    start, end = local_day_range(d)

    # 当日受理的 submission（用 accepted_at 切片；item 天然跟着走）
    subs_res = await session.execute(
        select(Submission)
        .where(Submission.accepted_at >= start, Submission.accepted_at < end)
        .order_by(Submission.accepted_at.asc())
    )
    submissions = list(subs_res.scalars().all())
    sub_ids = [s.id for s in submissions]

    if not sub_ids:
        return _DayBundle(submissions=[], items=[], runs_by_id={}, first_error_log={})

    items_res = await session.execute(
        select(SubmissionItem)
        .where(SubmissionItem.submission_id.in_(sub_ids))
        .order_by(SubmissionItem.enqueued_at.asc())
    )
    items = list(items_res.scalars().all())

    run_ids = [it.run_id for it in items if it.run_id]
    runs_by_id: Dict[str, Run] = {}
    if run_ids:
        runs_res = await session.execute(select(Run).where(Run.id.in_(run_ids)))
        for r in runs_res.scalars().all():
            runs_by_id[r.id] = r

    # 每条 run 的"最早一条 warn/error 日志"，给失败列表当错误摘要。
    # 用窗口函数一把聚合；没日志的 run / item 就不出现在 map 里。
    first_error_log: Dict[str, Tuple[str, Optional[datetime]]] = {}
    if run_ids:
        logs_res = await session.execute(
            select(RunLog.run_id, RunLog.title, RunLog.content, RunLog.ts)
            .where(
                RunLog.run_id.in_(run_ids),
                RunLog.level >= _ERROR_LOG_LEVEL_MIN,
            )
            .order_by(RunLog.run_id.asc(), RunLog.ts.asc(), RunLog.id.asc())
        )
        seen_run_ids: set[str] = set()
        for row in logs_res.all():
            rid = str(row.run_id)
            if rid in seen_run_ids:
                continue
            seen_run_ids.add(rid)
            # 标题为空就用 content 前 120 字兜底
            title = row.title or (row.content or "")[:120]
            # 映射回 item 层面，方便下游用：run_id → item.id 一对一反查
            first_error_log[rid] = (title.strip(), row.ts)

    return _DayBundle(
        submissions=submissions,
        items=items,
        runs_by_id=runs_by_id,
        first_error_log=first_error_log,
    )


# ---------------------------------------------------------------------------
# 各维度切片
# ---------------------------------------------------------------------------
def _elapsed_ms(item: SubmissionItem) -> Optional[int]:
    if item.started_at and item.finished_at:
        try:
            return max(0, int((item.finished_at - item.started_at).total_seconds() * 1000))
        except Exception:  # noqa: BLE001
            return None
    return None


def _percentile(values: List[int], p: float) -> Optional[int]:
    """无 numpy 版 p 分位；values 非空时返回 int。"""
    if not values:
        return None
    sv = sorted(values)
    if len(sv) == 1:
        return sv[0]
    k = (len(sv) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sv) - 1)
    frac = k - lo
    return int(sv[lo] * (1 - frac) + sv[hi] * frac)


def _throughput(items: List[SubmissionItem]) -> Dict[str, Any]:
    """吞吐：总量 / 状态分布 / 平台分布 / 平均&p95 耗时 / 成功率。"""
    by_state: Dict[str, int] = {}
    by_platform: Dict[str, Dict[str, int]] = {}
    elapsed_list: List[int] = []

    for it in items:
        by_state[it.state] = by_state.get(it.state, 0) + 1
        plat = by_platform.setdefault(
            it.platform,
            {"total": 0, "queued": 0, "running": 0, "success": 0, "failed": 0, "cancelled": 0},
        )
        plat["total"] += 1
        if it.state in plat:
            plat[it.state] += 1
        em = _elapsed_ms(it)
        if em is not None and it.state in ("success", "failed"):
            elapsed_list.append(em)

    total = len(items)
    done = by_state.get("success", 0) + by_state.get("failed", 0) + by_state.get("cancelled", 0)
    success_rate = (by_state.get("success", 0) / done) if done else None

    return {
        "totalItems": total,
        "byState": by_state,
        "byPlatform": by_platform,
        "avgElapsedMs": int(sum(elapsed_list) / len(elapsed_list)) if elapsed_list else None,
        "p95ElapsedMs": _percentile(elapsed_list, 0.95),
        "successRate": round(success_rate, 4) if success_rate is not None else None,
        "doneCount": done,
    }


def _devices_today(
    items: List[SubmissionItem],
    runs: Dict[str, Run],
    alias_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """当日视角：被调度器派过的设备 + 这台机当天跑了什么样。

    ``alias_map`` 是 serial → alias 的软映射，v1.4 加入。无 alias 的设备 alias
    字段给空串，前端自行降级展示 serial。
    """
    alias_map = alias_map or {}
    by_serial: Dict[str, Dict[str, Any]] = {}
    for it in items:
        serial = it.device_serial or ""
        if not serial:
            continue
        bucket = by_serial.setdefault(
            serial,
            {
                "serial": serial,
                "alias": alias_map.get(serial, ""),
                "platform": it.platform,
                "itemsTotal": 0,
                "success": 0,
                "failed": 0,
                "cancelled": 0,
                "running": 0,
                "busyTimeMs": 0,
            },
        )
        bucket["itemsTotal"] += 1
        if it.state in ("success", "failed", "cancelled", "running"):
            bucket[it.state] += 1
        em = _elapsed_ms(it)
        if em is not None:
            bucket["busyTimeMs"] += em

    # 按"当日跑的条数"倒排，直观看谁最忙
    rows = sorted(by_serial.values(), key=lambda r: r["itemsTotal"], reverse=True)
    return {
        "activeSerials": len(rows),
        "byDevice": rows,
    }


async def _load_alias_map(session: AsyncSession) -> Dict[str, str]:
    """大盘里 serial → alias 的一次性映射。"""
    res = await session.execute(select(DeviceAlias.serial, DeviceAlias.alias))
    return {row.serial: row.alias for row in res.all()}


async def _devices_history(
    session: AsyncSession,
    alias_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """设备健康（历史聚合）：所有 Run 按 device_serial 做成功率。

    不切日期——这就是"这台设备长期稳不稳"。
    """
    stmt = (
        select(
            Run.device_serial.label("serial"),
            func.count(Run.id).label("total"),
            func.sum(case((Run.status == "success", 1), else_=0)).label("success"),
            func.sum(case((Run.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((Run.status == "stopped", 1), else_=0)).label("stopped"),
        )
        .group_by(Run.device_serial)
    )
    res = await session.execute(stmt)
    rows: List[Dict[str, Any]] = []
    for r in res.all():
        serial = r.serial or ""
        if not serial:
            continue
        total = int(r.total or 0)
        succ = int(r.success or 0)
        fail = int(r.failed or 0)
        stopped = int(r.stopped or 0)
        completed = succ + fail
        rows.append(
            {
                "serial": serial,
                "alias": (alias_map or {}).get(serial, ""),
                "totalRuns": total,
                "success": succ,
                "failed": fail,
                "stopped": stopped,
                # successRate 口径：仅在"真正跑完"的 Run 里算；stopped/pending 不计。
                # completed 为 0 的新设备给 None，前端显示"—"
                "successRate": round(succ / completed, 4) if completed else None,
            }
        )

    # 再拉一次 Device 表，把 platform/brand/model 贴上去，方便前端直接展示
    dev_res = await session.execute(select(Device))
    devs_by_serial = {d.serial: d for d in dev_res.scalars().all()}
    for row in rows:
        dev = devs_by_serial.get(row["serial"])
        if dev is not None:
            row["platform"] = dev.platform
            row["brand"] = dev.brand
            row["model"] = dev.model
            row["currentStatus"] = dev.status
        else:
            row["platform"] = ""
            row["brand"] = ""
            row["model"] = ""
            row["currentStatus"] = "unknown"

    # 默认按 successRate 升序（有问题的排前面）；None 归到最后
    rows.sort(
        key=lambda r: (r["successRate"] is None, r["successRate"] or 0, -r["totalRuns"])
    )
    return {"byDevice": rows, "totalDevices": len(rows)}


def _token_summary(
    items: List[SubmissionItem], runs: Dict[str, Run]
) -> Dict[str, Any]:
    """Token：总量 / 平台 / 模型 / TopN 执行单元。

    Run.token_summary 来自 VLMClient 的 TokenCounter.summary()：
    ``{call_count, prompt_tokens, completion_tokens, total_tokens, cached_tokens, by_scene}``
    """
    total = {"callCount": 0, "promptTokens": 0, "completionTokens": 0, "totalTokens": 0, "cachedTokens": 0}
    by_platform: Dict[str, Dict[str, int]] = {}
    by_model: Dict[str, Dict[str, int]] = {}
    top: List[Dict[str, Any]] = []

    for it in items:
        run = runs.get(it.run_id or "")
        ts = (run.token_summary if run else {}) or {}
        call_count = int(ts.get("call_count") or 0)
        pt = int(ts.get("prompt_tokens") or 0)
        ct = int(ts.get("completion_tokens") or 0)
        tt = int(ts.get("total_tokens") or (pt + ct))
        cached = int(ts.get("cached_tokens") or 0)
        if tt <= 0 and call_count <= 0:
            continue

        total["callCount"] += call_count
        total["promptTokens"] += pt
        total["completionTokens"] += ct
        total["totalTokens"] += tt
        total["cachedTokens"] += cached

        plat_bucket = by_platform.setdefault(
            it.platform,
            {"callCount": 0, "promptTokens": 0, "completionTokens": 0, "totalTokens": 0, "cachedTokens": 0},
        )
        plat_bucket["callCount"] += call_count
        plat_bucket["promptTokens"] += pt
        plat_bucket["completionTokens"] += ct
        plat_bucket["totalTokens"] += tt
        plat_bucket["cachedTokens"] += cached

        for scene in ts.get("by_scene") or []:
            model = str(scene.get("model") or "")
            if not model:
                continue
            m_bucket = by_model.setdefault(
                model,
                {"callCount": 0, "promptTokens": 0, "completionTokens": 0, "totalTokens": 0, "cachedTokens": 0},
            )
            m_bucket["callCount"] += int(scene.get("calls") or 0)
            m_bucket["promptTokens"] += int(scene.get("prompt_tokens") or 0)
            m_bucket["completionTokens"] += int(scene.get("completion_tokens") or 0)
            m_bucket["totalTokens"] += int(scene.get("total_tokens") or 0)
            m_bucket["cachedTokens"] += int(scene.get("cached_tokens") or 0)

        top.append(
            {
                "submissionId": it.submission_id,
                "caseId": it.case_id,
                "caseName": it.case_name or it.case_id,
                "platform": it.platform,
                "runId": it.run_id,
                "totalTokens": tt,
                "promptTokens": pt,
                "cachedTokens": cached,
            }
        )

    top.sort(key=lambda r: r["totalTokens"], reverse=True)
    return {
        **total,
        "byPlatform": by_platform,
        "byModel": [{"model": m, **v} for m, v in sorted(by_model.items(), key=lambda kv: kv[1]["totalTokens"], reverse=True)],
        "topItems": top[:10],
    }


def _stability(
    items: List[SubmissionItem],
    runs: Dict[str, Run],
    first_error_log: Dict[str, Tuple[str, Optional[datetime]]],
    device_history: Dict[str, Any],
) -> Dict[str, Any]:
    """稳定性 = "平台靠不靠谱"，业务断言 / 用户取消都不计入分母。

    口径（和"吞吐"刻意分开）：

    - ``platformStabilityRate = 1 - 平台原因失败 / 已完成``
      * 已完成 = ``success + failed + cancelled``（含全部终态）
      * 平台原因失败 = ``state in {failed, cancelled}`` 且 ``statusReason ∈ PLATFORM_FAILURE_REASONS``
    - ``businessFailureCount`` 只做计数，不进 KPI 分母（assert_failed / cancelled_by_request）
    - ``failureByReason`` 只统计平台原因；业务原因单独 ``businessReasons`` 字段给前端做小字
    - ``failedCases`` 只列平台原因导致的失败/异常终止，业务断言失败不进列表
    """
    done_count = 0
    succ_count = 0
    plat_failed = 0
    biz_failed = 0
    other_terminated = 0  # statusReason 缺失 / 不认识的归到这里，避免 KPI 漂移
    failure_by_reason: Dict[str, int] = {}
    business_reasons: Dict[str, int] = {}
    failed_rows: List[Dict[str, Any]] = []

    for it in items:
        if it.state in ("success", "failed", "cancelled"):
            done_count += 1
        if it.state == "success":
            succ_count += 1
            continue
        if it.state not in ("failed", "cancelled"):
            continue

        reason = it.status_reason or ""
        if reason in PLATFORM_FAILURE_REASONS:
            plat_failed += 1
            failure_by_reason[reason] = failure_by_reason.get(reason, 0) + 1
            err_title, err_ts = first_error_log.get(it.run_id or "", ("", None))
            failed_rows.append(
                {
                    "itemId": it.id,
                    "submissionId": it.submission_id,
                    "caseId": it.case_id,
                    "caseName": it.case_name or it.case_id,
                    "platform": it.platform,
                    "deviceSerial": it.device_serial or None,
                    "state": it.state,
                    "statusReason": reason,
                    "elapsedMs": _elapsed_ms(it),
                    "finishedAt": it.finished_at.isoformat() if it.finished_at else None,
                    "reportUrl": (
                        item_report_url(it.submission_id, it.case_id, it.platform)
                        if it.run_id and it.state in ("success", "failed")
                        else None
                    ),
                    "runId": it.run_id or None,
                    "firstErrorLog": err_title or None,
                    "firstErrorAt": err_ts.isoformat() if err_ts else None,
                }
            )
        elif reason in BUSINESS_FAILURE_REASONS:
            biz_failed += 1
            business_reasons[reason] = business_reasons.get(reason, 0) + 1
        else:
            # 未分类：reason 为空（理论不应该），或新增了枚举但忘了归类。
            # 暂归到"其它"，不污染稳定率计算（不算到 platform_failed），但单独提示。
            other_terminated += 1
            if reason:
                business_reasons[reason] = business_reasons.get(reason, 0) + 1

    return {
        "totalItems": len(items),
        "doneCount": done_count,
        "successCount": succ_count,
        "platformFailureCount": plat_failed,
        "businessFailureCount": biz_failed,
        "otherTerminatedCount": other_terminated,
        "platformStabilityRate": (
            round((done_count - plat_failed) / done_count, 4) if done_count else None
        ),
        "failureByReason": failure_by_reason,
        "businessReasons": business_reasons,
        "failedCases": failed_rows,
        "deviceHealth": device_history,
    }


def _submissions_view(
    submissions: List[Submission], items: List[SubmissionItem]
) -> List[Dict[str, Any]]:
    """底部集合块：每个 submission 一张卡，不内嵌 HTML（按用户要求）。"""
    items_by_sub: Dict[str, List[SubmissionItem]] = {}
    for it in items:
        items_by_sub.setdefault(it.submission_id, []).append(it)

    out: List[Dict[str, Any]] = []
    for sub in submissions:
        sub_items = items_by_sub.get(sub.id, [])
        counts: Dict[str, int] = {}
        plat_counts: Dict[str, int] = {}
        total_tokens = 0
        for it in sub_items:
            counts[it.state] = counts.get(it.state, 0) + 1
            plat_counts[it.platform] = plat_counts.get(it.platform, 0) + 1

        elapsed_ms: Optional[int] = None
        if sub.accepted_at and sub.finished_at:
            try:
                elapsed_ms = max(0, int((sub.finished_at - sub.accepted_at).total_seconds() * 1000))
            except Exception:  # noqa: BLE001
                pass

        out.append(
            {
                "submissionId": sub.id,
                "submissionName": sub.submission_name or sub.id,
                "origin": sub.origin,
                "state": sub.state,
                "acceptedAt": sub.accepted_at.isoformat() if sub.accepted_at else None,
                "finishedAt": sub.finished_at.isoformat() if sub.finished_at else None,
                "elapsedMs": elapsed_ms,
                "counts": counts,
                "platformCounts": plat_counts,
                "totalItems": len(sub_items),
                "summaryReportUrl": (
                    submission_summary_url(sub.id) if sub.finished_at is not None else None
                ),
            }
        )
    # 新的在前（accepted_at 降序）
    out.sort(key=lambda r: r["acceptedAt"] or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def aggregate_day(session: AsyncSession, d: date) -> Dict[str, Any]:
    """按本地日历日聚合大盘切片。

    返回扁平 JSON，前端直接 ``v-bind`` 即可；所有计数缺失时给 0 / None，前端做
    ``—`` 兜底。
    """
    bundle = await _load_day_bundle(session, d)
    alias_map = await _load_alias_map(session)
    device_history = await _devices_history(session, alias_map)

    start, end = local_day_range(d)
    now_local = datetime.now(_tz())
    is_today = (d == now_local.date())

    throughput = _throughput(bundle.items)
    devices_today = _devices_today(bundle.items, bundle.runs_by_id, alias_map)
    token_summary = _token_summary(bundle.items, bundle.runs_by_id)
    stability = _stability(
        bundle.items, bundle.runs_by_id, bundle.first_error_log, device_history
    )
    submissions_view = _submissions_view(bundle.submissions, bundle.items)

    return {
        "date": d.isoformat(),
        "timezone": get_settings().analytics_timezone,
        "rangeStartUtc": start.isoformat(),
        "rangeEndUtc": end.isoformat(),
        "isToday": is_today,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalSubmissions": len(bundle.submissions),
        "totalItems": len(bundle.items),
        "throughput": throughput,
        "devices": {
            "today": devices_today,
            "health": device_history,
        },
        "token": token_summary,
        "stability": stability,
        "submissions": submissions_view,
    }
