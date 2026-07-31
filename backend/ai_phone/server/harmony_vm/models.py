"""Independent ORM models for managed HarmonyOS virtual machines."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ai_phone.shared.harmony_identity import harmony_udid_from_uuid

from ..db import Base


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HarmonyVmInstance(Base):
    __tablename__ = "harmony_vm_instances"
    __table_args__ = (
        Index("ix_harmony_vm_instances_state", "state"),
        Index("ix_harmony_vm_instances_agent", "assigned_agent_id"),
        Index("ix_harmony_vm_instances_hdc_serial", "hdc_serial"),
        Index("ix_harmony_vm_instances_alias", "alias"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_short_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), default="Phone")
    os_version: Mapped[str] = mapped_column(String(64), default="")
    api_version: Mapped[str] = mapped_column(String(64), default="")
    abi: Mapped[str] = mapped_column(String(32), default="auto")
    image_id: Mapped[str] = mapped_column(String(255), default="")
    screen_profile: Mapped[str] = mapped_column(String(128), default="")
    screen_width: Mapped[int] = mapped_column(Integer, default=1080)
    screen_height: Mapped[int] = mapped_column(Integer, default=2340)
    density: Mapped[int] = mapped_column(Integer, default=420)
    screen_size_in: Mapped[str] = mapped_column(String(32), default="")
    memory_gb: Mapped[int] = mapped_column(Integer, default=4)
    storage_gb: Mapped[int] = mapped_column(Integer, default=8)
    boot_mode: Mapped[str] = mapped_column(String(32), default="cold")
    config_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="draft")
    assigned_agent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    hdc_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hdc_serial: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    runtime: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        runtime = dict(self.runtime or {})
        history = runtime.get("lease_history")
        if isinstance(history, list):
            runtime["lease_history"] = [
                {
                    key: value
                    for key, value in item.items()
                    if key != "lease_token"
                }
                for item in history
                if isinstance(item, dict)
            ]
        return {
            "id": self.id,
            "name": self.name,
            "alias": self.alias,
            "device_type": self.device_type,
            "os_version": self.os_version,
            "api_version": self.api_version,
            "abi": self.abi,
            "image_id": self.image_id,
            "screen_profile": self.screen_profile,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "density": self.density,
            "screen_size_in": self.screen_size_in,
            "memory_gb": self.memory_gb,
            "storage_gb": self.storage_gb,
            "boot_mode": self.boot_mode,
            "config_json": self.config_json or {},
            "state": self.state,
            "assigned_agent_id": self.assigned_agent_id,
            "hdc_port": self.hdc_port,
            "hdc_serial": self.hdc_serial,
            "runtime": runtime,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


class HarmonyVmPortLease(Base):
    """Operational lease row.

    A released lease is deleted after its audit event is copied to the instance
    runtime history.  Keeping released rows would make ``UNIQUE(port)`` prevent
    reuse forever.
    """

    __tablename__ = "harmony_vm_port_leases"
    __table_args__ = (
        Index("ix_harmony_vm_port_leases_agent", "agent_id"),
        Index("ix_harmony_vm_port_leases_state", "state"),
    )

    port: Mapped[int] = mapped_column(Integer, primary_key=True)
    vm_id: Mapped[Optional[str]] = mapped_column(
        String(32), unique=True, nullable=True, index=True
    )
    agent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    quarantine_reason: Mapped[str] = mapped_column(String(128), default="")
    quarantine_details_json: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "port": self.port,
            "vm_id": self.vm_id,
            "agent_id": self.agent_id,
            "lease_token": self.lease_token,
            "state": self.state,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_error": self.last_error,
            "quarantine_reason": self.quarantine_reason,
            "quarantine_details": self.quarantine_details_json or {},
        }


class HarmonyVmCatalogSnapshot(Base):
    """Server-owned DevEco official catalog.

    The catalog is replaced atomically as one small snapshot. Agent-local
    downloaded image state is deliberately not stored here.
    """

    __tablename__ = "harmony_vm_catalog_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_type: Mapped[str] = mapped_column(
        String(64), default="deveco_emulator_official"
    )
    source_url: Mapped[str] = mapped_column(String(512), default="")
    collected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    emulator_version: Mapped[str] = mapped_column(String(128), default="")
    device_types_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    images_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    screen_profiles_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        images = list(self.images_json or [])
        profiles = list(self.screen_profiles_json or [])
        creatable_images = [
            item
            for item in images
            if isinstance(item, dict) and item.get("creatable", True) is not False
        ]
        return {
            "ok": bool(images and profiles),
            "reason": "" if images and profiles else "Server 尚未导入 DevEco 官方目录",
            "source": {
                "type": self.source_type or "",
                "url": self.source_url or "",
                "collected_at": (
                    self.collected_at.isoformat() if self.collected_at else None
                ),
                "emulator_version": self.emulator_version or "",
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            },
            "device_types": list(self.device_types_json or []),
            "images": images,
            "screen_profiles": profiles,
            "stats": {
                "images": len(images),
                "creatable_images": len(creatable_images),
                "unavailable_images": len(images) - len(creatable_images),
                "screen_profiles": len(profiles),
            },
            "source_policy": {
                "owner": "server",
                "mode": "replace_official_snapshot",
                "agent_catalog_aggregation": False,
                "bundled_preset_on_empty_db": True,
                "empty_means_initialization_failed": True,
                "builtin_fallback": False,
            },
        }


class HarmonyAppPackageMeta(Base):
    __tablename__ = "harmony_app_package_meta"

    package_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("app_packages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    abi_set: Mapped[str] = mapped_column(String(64), default="none")
    abi_state: Mapped[str] = mapped_column(String(32), default="resolved")
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_error: Mapped[str] = mapped_column(Text, default="")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "package_id": self.package_id,
            "abi_set": self.abi_set,
            "abi_state": self.abi_state,
            "parsed_at": self.parsed_at.isoformat() if self.parsed_at else None,
            "last_error": self.last_error,
        }


class HarmonyVmSetting(Base):
    """Global Harmony VM knobs that are not per-instance.

    ``id='global'`` stores the active value. ``retired_*`` rows retain old shared
    UUIDs so existing instances can explicitly return to independent identity.
    """

    __tablename__ = "harmony_vm_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="global")
    # 固定实例 UUID。DevEco 的设备 UDID 就是实例 config.ini 里 uuid 字段拼出来的，
    # 每次 -create 随机生成，导致每台虚拟机 UDID 都不同，内测包必须逐台报备。
    # 填上 global 行就让所有鸿蒙虚拟机共用一个 UDID，报备一次即可。
    instance_uuid: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_uuid": self.instance_uuid or "",
            "device_udid": harmony_udid_from_uuid(self.instance_uuid or ""),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


HARMONY_TABLES = (
    HarmonyVmInstance.__table__,
    HarmonyVmPortLease.__table__,
    HarmonyVmCatalogSnapshot.__table__,
    HarmonyVmSetting.__table__,
    HarmonyAppPackageMeta.__table__,
)

__all__ = [
    "HARMONY_TABLES",
    "HarmonyAppPackageMeta",
    "HarmonyVmCatalogSnapshot",
    "HarmonyVmInstance",
    "HarmonyVmPortLease",
    "HarmonyVmSetting",
    "harmony_udid_from_uuid",
]
