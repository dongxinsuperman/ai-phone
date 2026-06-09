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
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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


class DeviceWakePolicy(Base):
    """设备 wake 策略表：仅承载 HarmonyOS Run 前是否兜底上滑。"""

    __tablename__ = "device_wake_policies"

    serial: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    wake_swipe: Mapped[bool] = mapped_column(default=False)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "serial": self.serial,
            "platform": self.platform,
            "wake_swipe": bool(self.wake_swipe),
            "remark": self.remark or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AndroidVmInstance(Base):
    """Android 虚拟手机配置与当前运行态。

    Server 只保存配置、当前托管 Agent 与最近一次 ADB serial；真正的 Emulator
    运行目录在 Agent 本地，由 Agent VM manager 维护。
    """

    __tablename__ = "android_vm_instances"
    __table_args__ = (
        Index("ix_android_vm_instances_state", "state"),
        Index("ix_android_vm_instances_agent", "assigned_agent_id"),
        Index("ix_android_vm_instances_adb_serial", "adb_serial"),
        Index("ix_android_vm_instances_alias", "alias"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str] = mapped_column(String(128), default="")
    profile_ref_type: Mapped[str] = mapped_column(String(32), default="custom")
    profile_ref_id: Mapped[str] = mapped_column(String(64), default="")
    profile_id: Mapped[str] = mapped_column(String(64), default="")
    profile_name: Mapped[str] = mapped_column(String(128), default="")
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    config_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    capability_marks: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    api_level: Mapped[int] = mapped_column(Integer, nullable=False)
    abi: Mapped[str] = mapped_column(String(32), nullable=False)
    system_type: Mapped[str] = mapped_column(String(64), default="google_apis")
    system_image: Mapped[str] = mapped_column(String(255), default="")
    screen_width: Mapped[int] = mapped_column(Integer, default=1080)
    screen_height: Mapped[int] = mapped_column(Integer, default=2400)
    density: Mapped[int] = mapped_column(Integer, default=420)
    orientation: Mapped[str] = mapped_column(String(16), default="portrait")
    state: Mapped[str] = mapped_column(String(32), default="draft")
    assigned_agent_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    adb_serial: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    runtime: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "alias": self.alias,
            "profile_ref_type": self.profile_ref_type or "custom",
            "profile_ref_id": self.profile_ref_id or "",
            "profile_id": self.profile_id or "",
            "profile_name": self.profile_name or "",
            "config_version": self.config_version or 1,
            "config_json": self.config_json or {},
            "capability_marks": self.capability_marks or {},
            "api_level": self.api_level,
            "abi": self.abi,
            "system_type": self.system_type or "google_apis",
            "system_image": self.system_image or "",
            "screen_width": self.screen_width or 1080,
            "screen_height": self.screen_height or 2400,
            "density": self.density or 420,
            "orientation": self.orientation or "portrait",
            "state": self.state,
            "assigned_agent_id": self.assigned_agent_id,
            "adb_serial": self.adb_serial,
            "error_message": self.error_message,
            "runtime": self.runtime or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


class AndroidDeviceProfile(Base):
    """可信设备库条目。

    真实设备 profile 必须带来源和可信等级；无来源的覆盖组合放到
    AndroidVmCoverageProfile，不混进真实设备库。
    """

    __tablename__ = "android_device_profiles"
    __table_args__ = (
        Index("ix_android_device_profiles_brand", "brand"),
        Index("ix_android_device_profiles_device", "device"),
        Index("ix_android_device_profiles_model_code", "model_code"),
        Index("ix_android_device_profiles_marketing_name", "marketing_name"),
        Index("ix_android_device_profiles_source_type", "source_type"),
        Index("ix_android_device_profiles_verification", "verification_status"),
        Index("ix_android_device_profiles_region", "market_region"),
        Index("ix_android_device_profiles_screen_shape", "screen_shape"),
        Index("ix_android_device_profiles_resolution_bucket", "resolution_bucket"),
        Index("ix_android_device_profiles_form_factor", "form_factor"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    source_type: Mapped[str] = mapped_column(String(64), default="google_play_device_catalog")
    source_url: Mapped[str] = mapped_column(String(512), default="")
    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[str] = mapped_column(String(32), default="official")
    verification_status: Mapped[str] = mapped_column(String(32), default="verified")
    popularity_source: Mapped[str] = mapped_column(String(128), default="")
    popularity_score: Mapped[int] = mapped_column(Integer, default=0)
    market_region: Mapped[str] = mapped_column(String(32), default="CN")
    manufacturer: Mapped[str] = mapped_column(String(128), default="")
    brand: Mapped[str] = mapped_column(String(128), default="")
    series: Mapped[str] = mapped_column(String(128), default="")
    device: Mapped[str] = mapped_column(String(128), default="")
    model_code: Mapped[str] = mapped_column(String(128), default="")
    marketing_name: Mapped[str] = mapped_column(String(128), default="")
    variant_key: Mapped[str] = mapped_column(String(128), default="")
    form_factor: Mapped[str] = mapped_column(String(64), default="")
    screen_shape: Mapped[str] = mapped_column(String(64), default="")
    market_tags: Mapped[List[Any]] = mapped_column(JSON, default=list)
    ram_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    soc: Mapped[str] = mapped_column(String(128), default="")
    gpu: Mapped[str] = mapped_column(String(128), default="")
    screen_size_in: Mapped[str] = mapped_column(String(128), default="")
    screen_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    screen_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    densities: Mapped[List[Any]] = mapped_column(JSON, default=list)
    abis: Mapped[List[Any]] = mapped_column(JSON, default=list)
    sdk_versions: Mapped[List[Any]] = mapped_column(JSON, default=list)
    opengl_es: Mapped[str] = mapped_column(String(64), default="")
    # 预清洗派生列：供服务端按维度筛选 + 分页（避免 JSON 跨库查询）。
    resolution_bucket: Mapped[str] = mapped_column(String(16), default="")
    # SDK 版本规范化索引串，形如 ";34;35;36;"，用 LIKE '%;34;%' 精确匹配某版本。
    sdk_index: Mapped[str] = mapped_column(String(128), default="")
    raw: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type or "",
            "source_url": self.source_url or "",
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
            "confidence": self.confidence or "",
            "verification_status": self.verification_status or "verified",
            "popularity_source": self.popularity_source or "",
            "popularity_score": self.popularity_score or 0,
            "market_region": self.market_region or "",
            "manufacturer": self.manufacturer or "",
            "brand": self.brand or "",
            "series": self.series or "",
            "device": self.device or "",
            "model_code": self.model_code or "",
            "marketing_name": self.marketing_name or "",
            "variant_key": self.variant_key or "",
            "form_factor": self.form_factor or "",
            "screen_shape": self.screen_shape or "",
            "market_tags": self.market_tags or [],
            "ram_mb": self.ram_mb,
            "soc": self.soc or "",
            "gpu": self.gpu or "",
            "screen_size_in": self.screen_size_in or "",
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "densities": self.densities or [],
            "abis": self.abis or [],
            "sdk_versions": self.sdk_versions or [],
            "opengl_es": self.opengl_es or "",
            "resolution_bucket": self.resolution_bucket or "",
            "raw": self.raw or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AndroidVmCoverageProfile(Base):
    """内部覆盖策略模板，不伪装成真实设备。"""

    __tablename__ = "android_vm_coverage_profiles"
    __table_args__ = (
        Index("ix_android_vm_coverage_profiles_name", "name"),
        Index("ix_android_vm_coverage_profiles_source_type", "source_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[List[Any]] = mapped_column(JSON, default=list)
    config_template: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    capability_marks: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    source_type: Mapped[str] = mapped_column(String(64), default="internal_strategy")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "tags": self.tags or [],
            "config_template": self.config_template or {},
            "capability_marks": self.capability_marks or {},
            "source_type": self.source_type or "internal_strategy",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
    function_map_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    # —— 以下字段在 next/server-brain 引入；老 main 仅有 schema，不写值 ——
    # 'agent_brain' / 'server_brain'：本条 Run 走的执行链路。
    # server_default='agent_brain' 让历史 / main 路径下的 Run 自动归类为老链路，
    # 排障时 SELECT WHERE execution_mode='server_brain' 一句 SQL 锁定新架构 Run。
    execution_mode: Mapped[str] = mapped_column(
        String(16), default="agent_brain", server_default="agent_brain", index=True
    )
    # 'api' / 'scheduler'：来自 RunDispatchService 的入口标记。
    # NULL 表示老链路（没经过 RunDispatchService）。
    dispatch_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # run 级 trace_id，便于跨进程串联日志 / run_logs / run_commands。
    # 老链路下为 NULL；新链路下生成方式建议 ``uuid.uuid4().hex[:16]``。
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Run 启动时绑定的 Agent ID。区分"现在 Agent 掉线"和"启动时就没 Agent"。
    # 与 agent_id 字段的区别：agent_id 反映当前态（重连后会变），
    # agent_id_at_start 是启动快照，错误归因用。
    agent_id_at_start: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Run 失败原因若为 agent 掉线，记录掉线时刻。Web 错误归因 UI 用。
    agent_offline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_cache_mode: Mapped[str] = mapped_column(
        String(8), default="off", server_default="off"
    )
    effective_cache_mode: Mapped[str] = mapped_column(
        String(8), default="off", server_default="off", index=True
    )
    requested_retry_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_retry_max: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # attempts = 已经启动过的 attempt 数；last_attempt = 最近一次落库/运行 attempt。
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    step_records: Mapped[list["RunStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )
    log_records: Mapped[list["RunLog"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )
    command_records: Mapped[list["RunCommand"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "device_serial": self.device_serial,
            "agent_id": self.agent_id,
            "case_id": self.case_id,
            "goal": self.goal,
            "function_map_context_chars": len(self.function_map_context or ""),
            "status": self.status,
            "reason": self.reason,
            "steps": self.steps,
            "elapsed_ms": self.elapsed_ms,
            "token_summary": self.token_summary or {},
            "engine": self.engine or "vlm",
            "external_report_url": self.external_report_url,
            "execution_mode": self.execution_mode or "agent_brain",
            "dispatch_source": self.dispatch_source,
            "trace_id": self.trace_id,
            "agent_id_at_start": self.agent_id_at_start,
            "agent_offline_at": self.agent_offline_at.isoformat() if self.agent_offline_at else None,
            "requested_cache_mode": self.requested_cache_mode or "off",
            "effective_cache_mode": self.effective_cache_mode or "off",
            "cacheMode": self.effective_cache_mode or "off",
            "requested_retry_max": self.requested_retry_max,
            "effective_retry_max": self.effective_retry_max or 0,
            "retryMax": self.effective_retry_max or 0,
            "attempts": self.attempts or 1,
            "last_attempt": self.last_attempt or self.attempts or 1,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (
        Index("ix_run_steps_run_step", "run_id", "step"),
        # 可靠上报去重：同 (run_id, attempt, event_id) 只落一条；event_id 为 NULL
        # 的老数据/老链路不受唯一约束影响（PG/SQLite 多个 NULL 不冲突）。
        Index("ux_run_steps_event", "run_id", "attempt", "event_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1", index=True)
    step: Mapped[int] = mapped_column(Integer)
    # 可靠上报幂等键（Agent 分配的 uuid hex）；老链路为 NULL
    event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    thought: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(Text, default="")
    action_type: Mapped[str] = mapped_column(String(32), default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    unknown: Mapped[int] = mapped_column(Integer, default=0)  # 0/1 布尔
    screenshot_before: Mapped[str] = mapped_column(String(512), default="")
    screenshot_after: Mapped[str] = mapped_column(String(512), default="")
    # —— 以下字段在 next/server-brain 引入；老 main 不写 ——
    # 本步骤主导调用的 BaseDriver 方法名（screenshot_jpeg / click / type_text 等）。
    # 老链路下为 NULL；新链路下从 driver_command.method 透传。
    driver_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 本步骤"主动作"对应的 driver_command.message_id（不含截图等附属命令）。
    # 用于和 run_commands 表 join 拿命令时间线。
    command_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # RPC 往返耗时（毫秒）；仅含跨进程，不含 VLM。与 elapsed_ms 字段分开统计。
    rpc_elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="step_records")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt": self.attempt or 1,
            "step": self.step,
            "event_id": self.event_id,
            "thought": self.thought,
            "action": self.action,
            "action_type": self.action_type,
            "elapsed_ms": self.elapsed_ms,
            "unknown": bool(self.unknown),
            "screenshot_before": self.screenshot_before,
            "screenshot_after": self.screenshot_after,
            "driver_method": self.driver_method,
            "command_id": self.command_id,
            "rpc_elapsed_ms": self.rpc_elapsed_ms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class RunLog(Base):
    __tablename__ = "run_logs"
    __table_args__ = (
        Index("ix_run_logs_run_ts", "run_id", "ts"),
        # 可靠上报去重：同 (run_id, attempt, event_id) 只落一条；NULL 不受约束。
        Index("ux_run_logs_event", "run_id", "attempt", "event_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1", index=True)
    step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 可靠上报幂等键（Agent 分配的 uuid hex）；老链路为 NULL
    event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 1=info,2=warn,3=error
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    # —— 以下字段在 next/server-brain 引入；老 main 不写 ——
    # 与 runs.trace_id / driver_command.message_id 关联；为 NULL 时退回老行为。
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # 错误类名（AdbError / WDAStaleSession / TimeoutError / RpcTimeout / AgentOffline 等）
    error_class: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 错误归因桶：'model' / 'device' / 'network' / 'agent_offline'。
    # Web 错误归因 UI 直接按这一列分桶展示。
    error_category: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="log_records")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attempt": self.attempt or 1,
            "step": self.step,
            "level": self.level,
            "title": self.title,
            "content": self.content,
            "trace_id": self.trace_id,
            "error_class": self.error_class,
            "error_category": self.error_category,
            "ts": self.ts.isoformat() if self.ts else None,
        }


class RunCommand(Base):
    """跨进程 driver_command 命令的细粒度记录（next/server-brain 引入）。

    设计要点：
    - **仅在 next/server-brain（Server 大脑架构）写入**；老链路（agent_brain）不写
    - 一次 ``driver_command`` ↔ 一条 RunCommand
    - ``message_id`` 与 ``run_logs.trace_id`` / ``run_steps.command_id`` 共享
      同一份 id 空间，方便 SQL join 排障
    - 不与 run_steps 1:1：截图、状态查询等附属命令也都会写一行，但只有"主动作"
      命令的 message_id 会回填到 ``run_steps.command_id``

    典型用法：
    - 排障：``SELECT * FROM run_commands WHERE run_id=? ORDER BY sent_at`` 拿命令时间线
    - 统计：``SELECT method, count(*), avg(rpc_elapsed_ms) FROM run_commands
            WHERE ok=true GROUP BY method`` 看每类方法的 RPC 平均耗时
    - 截图丢失定位：``method='screenshot_jpeg' AND ok=false``
    """

    __tablename__ = "run_commands"
    __table_args__ = (
        Index("ix_run_commands_run_sent", "run_id", "sent_at"),
        Index("ix_run_commands_message_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1", index=True)
    # run_steps.step 的可选反链；附属命令（截图等）可能没有 step
    step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # driver_command.message_id；兼作 trace_id，与 run_logs.trace_id 同空间
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # screenshot_jpeg / click / type_text / window_size 等；DriverMethod 白名单
    method: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Driver 调用参数快照。轨迹缓存清洗优先使用这里的真实绝对坐标；老数据或
    # agent_brain 路径为空时再回退解析 RunStep.action。
    params: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # 派发时绑定的 Agent / 设备；Agent 重连不影响这条历史快照
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    serial: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 命令完成态（True / False / NULL=超时未回）
    ok: Mapped[Optional[bool]] = mapped_column(default=None, nullable=True)
    # 错误结构（仅 ok=False 时填）
    error_class: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_category: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # RPC 往返耗时（毫秒）；含网络
    rpc_elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Server 发出 driver_command 的时刻
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Server 收到 driver_result 的时刻；超时 / 取消时为 NULL
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="command_records")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "attempt": self.attempt or 1,
            "step": self.step,
            "message_id": self.message_id,
            "method": self.method,
            "params": self.params or {},
            "agent_id": self.agent_id,
            "serial": self.serial,
            "ok": self.ok,
            "error_class": self.error_class,
            "error_category": self.error_category,
            "error_msg": self.error_msg,
            "rpc_elapsed_ms": self.rpc_elapsed_ms,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class RunEvent(Base):
    """Agent 上报过程事件的**预留承载表**（Distributed Agent Brain）。

    现状（重要）：本架构按"无感保真"口径推进——执行脑下沉回 Agent 后，
    Web 进度 / 报告 / 审计仍由现有链路承载：Agent 经 ``RunnerBridge`` 上报、
    Server ``agent_ws._persist_log`` / ``_persist_step`` / ``_finalize_run``
    **直写** ``run_logs`` / ``run_steps`` / ``runs``。**不**新增字段、**不**重做
    投影、**不**改报告来源。

    因此本表目前**不写入、不作为主链路事实流**，仅作为"未来若需要更细粒度
    事实流"的预留结构（schema 已就位，按需再启用）。请勿据此引入 model_call /
    结构化 error_category 等新留存——那与无感原则冲突。

    `run_commands`（Server Brain 历史审计表）同样不再作为本分支的动作事实来源。
    """

    __tablename__ = "run_events"
    __table_args__ = (
        Index(
            "ix_run_events_run_attempt_seq",
            "run_id",
            "attempt",
            "seq",
            unique=True,
        ),
        Index("ix_run_events_run_ts", "run_id", "ts"),
        Index("ix_run_events_event_type", "event_type"),
        Index("ix_run_events_event_id", "event_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1", index=True)
    # Agent 侧单调递增序号；(run_id, attempt, seq) 唯一（预留：去重 + 保序）
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 事件全局幂等键（建议 uuid.uuid4().hex）；唯一索引兜底重复上报
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 事件类型（预留）：对齐 agent/runner/events.py 的 EVT_*
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    # 可选关联字段（预留）：步骤号 / 截图引用等
    step: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 结构化失败分类（model/device/network/agent_offline/assertion/cache 等）
    error_category: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    # 事件结构化负载；大产物只放引用，不放 bytes
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # 事件发生时刻（Agent 侧时间戳）
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Server 落库时刻
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "attempt": self.attempt or 1,
            "seq": self.seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "step": self.step,
            "snapshot_id": self.snapshot_id,
            "error_category": self.error_category,
            "payload": self.payload or {},
            "ts": self.ts.isoformat() if self.ts else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class VlmTrajectoryCache(Base):
    """V1 固定动作轨迹缓存。

    V1 是最小可控基线：保存首次成功时真实执行过的 driver 动作，命中后
    只做固定动作顺序回放，增强能力必须放到 V2/V3 的独立表与入口里。
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


class VlmTrajectoryCacheV2(Base):
    """V2 增强轨迹缓存。

    V2 与 V1 分表：V2 可以保存状态路标、局部恢复、瞬态弹窗标记等增强元数据；
    V1 表继续只承担固定动作回放基线。
    """

    __tablename__ = "vlm_trajectory_cache_v2"
    __table_args__ = (
        Index("ix_vlm_trajectory_cache_v2_key", "cache_key", unique=True),
        Index("ix_vlm_trajectory_v2_device_semantic", "device_code", "run_semantic_hash"),
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
    schema_version: Mapped[int] = mapped_column(Integer, default=2)
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
            "mode": "v2",
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


class VlmTrajectoryCacheV3(Base):
    """V3 语义坐标回放缓存。

    V3 与 V2 分表：V2 保留完整 action + handoff landmark 路线；V3 保留增强
    source actions，但正常复跑只信任 ``plan_intent`` 和 action 类型，不信任旧坐标。
    """

    __tablename__ = "vlm_trajectory_cache_v3"
    __table_args__ = (
        Index("ix_vlm_trajectory_cache_v3_key", "cache_key", unique=True),
        Index("ix_vlm_trajectory_v3_device_semantic", "device_code", "run_semantic_hash"),
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
    schema_version: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    source_vlm_backend: Mapped[str] = mapped_column(String(64), default="")
    actions_json: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    source_completion: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    meta_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
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
            "mode": "v3",
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
            "source_vlm_backend": self.source_vlm_backend,
            "actions": self.actions_json or [],
            "source_completion": self.source_completion or {},
            "meta": self.meta_json or {},
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
    function_map_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_retry_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_retry_max: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

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
            "function_map_context_chars": len(self.function_map_context or ""),
            "requested_retry_max": self.requested_retry_max,
            "effective_retry_max": self.effective_retry_max or 0,
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
    cache_mode: Mapped[str] = mapped_column(String(8), default="off", server_default="off")
    requested_retry_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_retry_max: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

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
            "cacheMode": self.cache_mode or "off",
            "requested_retry_max": self.requested_retry_max,
            "effective_retry_max": self.effective_retry_max or 0,
            "retryMax": self.effective_retry_max or 0,
            "attempts": self.attempts or 0,
            "state": self.state,
            "status_reason": self.status_reason or None,
            "run_id": self.run_id,
            "device_serial": self.device_serial,
            "report_url": report_url,
            "enqueued_at": self.enqueued_at.isoformat() if self.enqueued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class AppPackage(Base):
    """应用分发上传包。

    只保存 Server 落盘所需的最小信息；created_at 用于区分同名重复上传包。
    """

    __tablename__ = "app_packages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    tasks: Mapped[list["AppInstallTask"]] = relationship(
        back_populates="package", cascade="all, delete-orphan", lazy="noload"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "platform": self.platform,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AppInstallTask(Base):
    """一次应用批量安装任务。"""

    __tablename__ = "app_install_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    package_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("app_packages.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(16), default="running", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    package: Mapped[AppPackage] = relationship(back_populates="tasks")
    items: Mapped[list["AppInstallTaskItem"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="noload"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "package_id": self.package_id,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class AppInstallTaskItem(Base):
    """应用安装任务中的单设备结果。"""

    __tablename__ = "app_install_task_items"
    __table_args__ = (
        Index("ix_app_install_items_task_state", "task_id", "state"),
        Index("ix_app_install_items_serial_state", "serial", "state"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    task_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("app_install_tasks.id", ondelete="CASCADE"), index=True
    )
    serial: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    timeout_sec: Mapped[int] = mapped_column(Integer, default=600)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[AppInstallTask] = relationship(back_populates="items")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "serial": self.serial,
            "platform": self.platform,
            "state": self.state,
            "reason": self.reason or None,
            "message": self.message or "",
            "timeout_sec": self.timeout_sec,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


__all__ = [
    "Device",
    "DeviceAlias",
    "Case",
    "Run",
    "RunStep",
    "RunLog",
    "RunCommand",
    "VlmTrajectoryCache",
    "VlmTrajectoryCacheV2",
    "VlmTrajectoryCacheV3",
    "Submission",
    "SubmissionItem",
    "AppPackage",
    "AppInstallTask",
    "AppInstallTaskItem",
]
