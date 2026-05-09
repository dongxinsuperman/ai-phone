"""ORM 模型：devices / cases / runs / run_steps / run_logs。

原则：
- 所有字段都用 SQLAlchemy 跨引擎通用类型（String/Integer/Text/JSON/DateTime）
- 时间字段统一带时区
- Run 的 token_summary / case 的 prerequisite_case_id 为软引用，不强 FK，方便
  Case 被删后历史 Run 依然可查
- 非结构化但结构清晰的字段（如 token_summary、extra）直接 JSON 存
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _short_id() -> str:
    """12 位 hex，够用、好读；UUID4 去前 12 位。"""
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Device(Base):
    __tablename__ = "devices"

    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="android")
    brand: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    os_version: Mapped[str] = mapped_column(String(64), default="")
    screen_width: Mapped[int] = mapped_column(Integer, default=0)
    screen_height: Mapped[int] = mapped_column(Integer, default=0)
    # online / offline / unauthorized / busy — 注：busy 是聚合态，由 lock 状态派生
    status: Mapped[str] = mapped_column(String(32), default="offline", index=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "serial": self.serial,
            "agent_id": self.agent_id,
            "platform": self.platform,
            "brand": self.brand,
            "model": self.model,
            "os_version": self.os_version,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "status": self.status,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DeviceAlias(Base):
    """设备别名表：serial ↔ 友好名 一一映射。

    **刻意不 FK 到 devices**：即插即用场景下设备可能反复上下线，甚至这台机的
    serial 第一次出现在别名表时 devices 表里可能还没它（运维先规划别名、再插设备）。
    外键会阻塞这种"先绑后现"，所以留软引用。

    一对一 + alias 全局唯一，数据库层保证；调度在准入阶段强校验 alias 必须命中
    本表，查不到整批 400 拒绝，不允许"字符串撞 model 前缀"的老降级走法。
    """

    __tablename__ = "device_aliases"

    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    alias: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "serial": self.serial,
            "alias": self.alias,
            "note": self.note or "",
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    title: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text, default="")
    # 前置 case：拼接模式，不嵌套递归。软引用避免历史记录被连带删除。
    prerequisite_case_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "prerequisite_case_id": self.prerequisite_case_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    device_serial: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # 软引用：case 可以被删，run 依然保留自己的 goal 副本
    case_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # pending / running / success / failed / stopped
    reason: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    token_summary: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # 执行引擎：'vlm'（默认 / ai-phone 主链路）或 'midscene'（外接寄居）等。
    # 老 run 没有这个字段，server_default 兜底为 'vlm'，归因清晰。
    engine: Mapped[str] = mapped_column(
        String(32), default="vlm", server_default="vlm", index=True
    )
    # 外接引擎产物（如 Midscene HTML 报告）的对外可访问 URL。
    # 仅 engine != 'vlm' 时填充；vlm runner 永远是 None（其报告由 ai-phone 自己组装）。
    external_report_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    step_records: Mapped[list["RunStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )
    log_records: Mapped[list["RunLog"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "device_serial": self.device_serial,
            "agent_id": self.agent_id,
            "case_id": self.case_id,
            "goal": self.goal,
            "status": self.status,
            "reason": self.reason,
            "steps": self.steps,
            "elapsed_ms": self.elapsed_ms,
            "token_summary": self.token_summary or {},
            "engine": self.engine or "vlm",
            "external_report_url": self.external_report_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (Index("ix_run_steps_run_step", "run_id", "step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[int] = mapped_column(Integer)
    thought: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(Text, default="")
    action_type: Mapped[str] = mapped_column(String(32), default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    unknown: Mapped[int] = mapped_column(Integer, default=0)  # 0/1 布尔
    screenshot_before: Mapped[str] = mapped_column(String(512), default="")
    screenshot_after: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="step_records")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step": self.step,
            "thought": self.thought,
            "action": self.action,
            "action_type": self.action_type,
            "elapsed_ms": self.elapsed_ms,
            "unknown": bool(self.unknown),
            "screenshot_before": self.screenshot_before,
            "screenshot_after": self.screenshot_after,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class RunLog(Base):
    __tablename__ = "run_logs"
    __table_args__ = (Index("ix_run_logs_run_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 1=info,2=warn,3=error
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="log_records")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step": self.step,
            "level": self.level,
            "title": self.title,
            "content": self.content,
            "ts": self.ts.isoformat() if self.ts else None,
        }


class VlmTrajectoryCache(Base):
    """VLM 成功轨迹缓存。

    key = device_code + run_semantic_hash + schema_version。命中后由 Agent 侧
    trajectory_cache runner 顺序回放 trajectory_json.actions；主 VLMRunner
    不感知这张表。
    """

    __tablename__ = "vlm_trajectory_cache"
    __table_args__ = (
        Index("ix_vlm_trajectory_cache_key", "cache_key", unique=True),
        Index("ix_vlm_trajectory_device_semantic", "device_code", "run_semantic_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    cache_key: Mapped[str] = mapped_column(String(128), nullable=False)
    device_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_semantic_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_semantic_text: Mapped[str] = mapped_column(Text, default="")
    case_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(16), default="")
    resolution: Mapped[str] = mapped_column(String(32), default="")
    app_package_or_bundle: Mapped[str] = mapped_column(String(255), default="")
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    trajectory_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "cache_key": self.cache_key,
            "device_code": self.device_code,
            "run_semantic_hash": self.run_semantic_hash,
            "run_semantic_text": self.run_semantic_text,
            "case_id": self.case_id,
            "platform": self.platform,
            "resolution": self.resolution,
            "app_package_or_bundle": self.app_package_or_bundle,
            "schema_version": self.schema_version,
            "status": self.status,
            "source_run_id": self.source_run_id,
            "trajectory_json": self.trajectory_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failed_at": self.last_failed_at.isoformat() if self.last_failed_at else None,
        }


class Submission(Base):
    """一次外部请求的批次容器（v1 第 2 梯队）。

    对应『AI 云真机执行器』对外契约里的 submission：
    - 一次请求体（JSON 数组）落一条 Submission + N 条 SubmissionItem
    - ``submissionId`` 由执行器内部生成；外部只拿返回值，不传入
    - 整批超时（默认 3h）到了就把所有仍 queued 的 item 逐条 submission_timeout

    origin 区分来源：``external``（对外 API）/``internal``（内部投递/调试）。
    v1 只用到 internal；external 第 3 梯队才启用。
    """

    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    origin: Mapped[str] = mapped_column(String(16), default="internal", index=True)
    # 批次名称（外部投递时可选传 submissionName，缺省回落 submission_id）。展示位
    # 优先用 submission_name，submission_id 退到 tooltip / 副标题。与 case_name
    # 的设计完全对称，方便外部"集合 / 计划"概念落到我们这边一目了然。
    submission_name: Mapped[str] = mapped_column(String(255), default="")
    # pending / accepted / cancelled / expired / done —— 聚合态；item 级真相在 SubmissionItem 表
    state: Mapped[str] = mapped_column(String(16), default="accepted", index=True)
    # 原始请求体（[{}, {}] 或 {submissionName, items[]}）；落库备查 + 调试
    raw_body: Mapped[Dict[str, Any]] = mapped_column(JSON, default=list)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # 超时硬边界（accepted_at + 3h）；扫描线程据此把仍 queued 的 item 终结为 submission_timeout
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v1.8 webhook：投递时可选传 callbackUrl。整批收口时异步 POST 一份
    # submission.terminal payload；发一次失败就吞，不影响主流程。
    callback_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    items: Mapped[list["SubmissionItem"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", lazy="noload"
    )

    def to_dict(self) -> Dict[str, Any]:
        # summary_report_url 仅在 submission 已经收口（finished_at 非空）时给值；
        # 否则置空。汇总 HTML 由 scheduler `_maybe_finalize_submission` 在最后
        # 一条 item 终态时同步落盘；这里假设 finished_at 非空 ↔ 汇总文件已就位。
        # 与 reports/paths 模块的 URL 规则保持一致。
        summary_url: Optional[str] = None
        if self.finished_at is not None:
            from .submissions.paths import submission_summary_url

            summary_url = submission_summary_url(self.id)
        return {
            "id": self.id,
            "origin": self.origin,
            "submission_name": self.submission_name or self.id,
            "state": self.state,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "expire_at": self.expire_at.isoformat() if self.expire_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "summary_report_url": summary_url,
        }


class SubmissionItem(Base):
    """submission 内部的单条执行单元（= 一个 caseId + platform 组合）。

    状态机（v1）：
        queued → running → success / failed / cancelled

    `run_id` 在 item 进入 running 时回填，指向已有 `runs` 表——v1 为了最小改动，
    **不** 改 WS `start_run` 协议；scheduler 走跟现有 /api/runs 一致的派发路径，
    只是 Run 多了一条 submission_item_id 反链。终态以 Run.status 为准，item 在
    `on_run_done` 时同步落位。
    """

    __tablename__ = "submission_items"
    __table_args__ = (
        Index(
            "ux_submission_case_platform",
            "submission_id", "case_id", "platform",
            unique=True,
        ),
        Index("ix_submission_items_state_platform", "state", "platform"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    submission_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[str] = mapped_column(String(128), index=True)
    # 用例名称（外部投递时可选传 caseName，缺省回落 case_id）。展示位优先用
    # case_name，case_id 退到 tooltip / 副标题。这样兼容老调用方：旧请求里
    # 没传 caseName，前端会拿到 case_name == case_id，行为不变。
    case_name: Mapped[str] = mapped_column(String(255), default="")
    platform: Mapped[str] = mapped_column(String(16), index=True)
    run_content: Mapped[str] = mapped_column(Text)
    # 目标设备别名池（可选）；该端可被消费的别名子集。
    # - None / 空数组 = 该端全池任挑（ready 设备里随便拿一台）
    # - 长度 1（如 ["A1"]）= 锁单台（语义上等价于过去的 deviceAlias）
    # - 长度 N（如 ["A1","B1"]）= 子集池：只在这 N 台里动态消费，
    #   哪台先 ready 哪台拿下一条；自然形成"快机多跑、慢机少跑"的负载分担
    # 写入时以 list[str] 形式（按 sorted+dedup 之后的顺序），DB 用 JSON 列承载
    device_alias_pool: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # queued / running / success / failed / cancelled
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # 11 项 statusReason 枚举之一（定义见 scheduler.service.STATUS_REASONS
    # 和 对外调用清单.md v1.5）；未终态时为 ""
    status_reason: Mapped[str] = mapped_column(String(64), default="")

    # 绑定到具体一条 Run；item 进入 running 时回填
    run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # 实际跑在哪台设备上；running 时回填
    device_serial: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    submission: Mapped[Submission] = relationship(back_populates="items")

    def to_dict(self) -> Dict[str, Any]:
        # report_url 仅在"进入终态 + 挂到了 Run"的 item 上给值；cancelled queued /
        # submission_timeout 的 item 没 Run 跑过，也就没有 HTML 报告，这里置空。
        # URL 拼接规则与 submissions.paths.item_report_url 一致；为避免循环导入
        # 这里在函数内部 import。
        report_url: Optional[str] = None
        if self.run_id and self.state in ("success", "failed"):
            from .submissions.paths import item_report_url

            report_url = item_report_url(self.submission_id, self.case_id, self.platform)
        return {
            "id": self.id,
            "submission_id": self.submission_id,
            "case_id": self.case_id,
            "case_name": self.case_name or self.case_id,
            "platform": self.platform,
            "run_content": self.run_content,
            "device_alias_pool": list(self.device_alias_pool or []) or None,
            "state": self.state,
            "status_reason": self.status_reason or None,
            "run_id": self.run_id,
            "device_serial": self.device_serial,
            "report_url": report_url,
            "enqueued_at": self.enqueued_at.isoformat() if self.enqueued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


__all__ = [
    "Device",
    "Case",
    "Run",
    "RunStep",
    "RunLog",
    "VlmTrajectoryCache",
    "Submission",
    "SubmissionItem",
]
