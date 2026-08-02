"""iOS 虚拟机的独立 ORM 模型。

结构对齐 ``harmony_vm/models.py``（独立包、独立表、独立初始化），字段则按 iOS 的
实际可配项裁剪——**只有机型与系统版本两项**（方案 §6.5.2）：

```text
Android  api_level / abi / system_image / 屏幕 / 密度 / 方向 / RAM / CPU / GPU / 网络 …
鸿蒙      device_type / os_version / fold_state / memory_gb / storage_gb / screen_profile
iOS Sim  device_type / runtime                      ← 苹果只给了这两个旋钮
```

相比鸿蒙**少两张表**（方案 §6.5.5）：

- 没有 ``*_port_leases``：虚拟机 serial 是 UDID 天然全局唯一，WDA 端口纯 Agent
  本机事务，不需要 Server 全局租约，也就没有 lease_token 与 quarantine 那套机制。
- 没有 ``*_settings``：鸿蒙那张表存的是「所有虚拟机共用一个 UDID」的报备变通，
  iOS 虚拟机 UDID 由 simctl 生成、本就唯一，无此问题。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IosSimVmInstance(Base):
    """一台受管 iOS 虚拟机的配置与当前运行态。

    **实例是常驻的**（方案 §6.5.1）：一条记录对应 Agent 上一台长期存在的虚拟机，
    可反复启停，已装 App 与数据跨启停留存。同一条记录始终对应同一台实例——
    Agent 侧靠虚拟机名 ``aiphone_sim_<id>`` 锚定身份。
    """

    __tablename__ = "ios_sim_vm_instances"
    __table_args__ = (
        Index("ix_ios_sim_vm_instances_state", "state"),
        Index("ix_ios_sim_vm_instances_agent", "assigned_agent_id"),
        Index("ix_ios_sim_vm_instances_udid", "udid"),
        Index("ix_ios_sim_vm_instances_alias", "alias"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    # —— 用户唯一需要选的两项 ——
    device_type: Mapped[str] = mapped_column(String(255), default="")
    device_type_name: Mapped[str] = mapped_column(String(128), default="")
    runtime: Mapped[str] = mapped_column(String(255), default="")
    runtime_name: Mapped[str] = mapped_column(String(128), default="")
    os_version: Mapped[str] = mapped_column(String(64), default="")
    # 预留：将来若苹果放开更多可配项，加在 config_json 里，不动表结构
    config_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    # —— 运行态 ——
    state: Mapped[str] = mapped_column(String(32), default="draft")
    assigned_agent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # 虚拟机 UDID，即通用设备层的 serial。由 simctl create 生成，创建后固定。
    udid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Agent 本机分配的 WDA / 镜像端口，仅供展示与排障——**不是 Server 分配的**，
    # Server 不做端口池管理（方案 §6.5.5）。
    wda_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mjpeg_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    runtime_state: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "alias": self.alias,
            "device_type": self.device_type,
            "device_type_name": self.device_type_name,
            "runtime": self.runtime,
            "runtime_name": self.runtime_name,
            "os_version": self.os_version,
            "config_json": self.config_json or {},
            "state": self.state,
            "assigned_agent_id": self.assigned_agent_id,
            "udid": self.udid,
            "wda_port": self.wda_port,
            "mjpeg_port": self.mjpeg_port,
            "runtime_state": self.runtime_state or {},
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


class IosSimCatalogSnapshot(Base):
    """官方机型目录快照。

    与 ``harmony_vm_catalog_snapshots`` 同构：内容来自仓库 bundle 的
    ``official_catalog.json``（由 ``scripts/export_ios_sim_catalog.py`` 生成），
    在 DB 初始化时导入。**Server 侧目录只回答「有哪些机型、各自支持哪些系统版本」**；
    「这台 Agent 装了哪些 runtime」由 Agent 能力探查实时上报（方案 §6.5.4.1）。

    单行表：``id='official'``。刷新即整体覆盖，不做多版本并存——目录是一份完整
    快照，没有增量合并的语义。
    """

    __tablename__ = "ios_sim_catalog_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="official")
    xcode_version: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(255), default="")
    collected_at: Mapped[str] = mapped_column(String(64), default="")
    device_type_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        payload = self.payload or {}
        return {
            "id": self.id,
            "xcode_version": self.xcode_version,
            "source": self.source,
            "collected_at": self.collected_at,
            "device_type_count": self.device_type_count,
            "device_types": payload.get("device_types") or [],
            "official_runtimes": payload.get("official_runtimes") or [],
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# 供 db.init_ios_sim_db 在隔离事务里建表用。列在这里而不是靠 module 扫描，
# 是为了让「哪些表属于 iOS 虚拟机」这件事在代码里一目了然（对齐鸿蒙 HARMONY_TABLES）。
IOS_SIM_TABLES = (
    IosSimVmInstance.__table__,
    IosSimCatalogSnapshot.__table__,
)
