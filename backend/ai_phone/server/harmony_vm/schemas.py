"""Pydantic schemas for the independent Harmony VM API."""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class HarmonyVmCreateReq(BaseModel):
    name: str = Field("", max_length=128)
    alias: str = Field("", max_length=128)
    device_type: str = Field("Phone", max_length=64)
    os_version: str = Field("", max_length=64)
    api_version: str = Field("", max_length=64)
    abi: str = Field("auto", pattern="^(auto|arm64|arm64-v8a|x86_64)$")
    image_id: str = Field("", max_length=255)
    screen_profile: str = Field("", max_length=128)
    # 折叠屏初始形态。仅 Foldable 支持——WideFold / TripleFold / 2in1 Foldable
    # 的内部状态模型不同且未实测，不提供看似可选实际无效的选项。
    fold_state: Literal["unfolded", "folded"] = "unfolded"
    screen_width: int = Field(1080, ge=320, le=7680)
    screen_height: int = Field(2340, ge=320, le=7680)
    density: int = Field(420, ge=120, le=800)
    screen_size_in: str = Field("", max_length=32)
    memory_gb: int = Field(4, ge=2, le=32)
    storage_gb: int = Field(8, ge=2, le=1023)
    # 产品固定为完整冷启动。Literal 会让绕过前端传 snapshot/reset 的请求显式失败，
    # 不做静默改写，避免调用方误以为其选择已经生效。
    boot_mode: Literal["cold"] = "cold"
    config_json: Dict[str, Any] = Field(default_factory=dict)


class HarmonyVmPatchReq(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    alias: Optional[str] = Field(None, max_length=128)
    device_type: Optional[str] = Field(None, max_length=64)
    os_version: Optional[str] = Field(None, max_length=64)
    api_version: Optional[str] = Field(None, max_length=64)
    abi: Optional[str] = Field(None, pattern="^(auto|arm64|arm64-v8a|x86_64)$")
    image_id: Optional[str] = Field(None, max_length=255)
    screen_profile: Optional[str] = Field(None, max_length=128)
    screen_width: Optional[int] = Field(None, ge=320, le=7680)
    screen_height: Optional[int] = Field(None, ge=320, le=7680)
    density: Optional[int] = Field(None, ge=120, le=800)
    screen_size_in: Optional[str] = Field(None, max_length=32)
    memory_gb: Optional[int] = Field(None, ge=2, le=32)
    storage_gb: Optional[int] = Field(None, ge=2, le=1023)
    boot_mode: Optional[Literal["cold"]] = None
    config_json: Optional[Dict[str, Any]] = None


class HarmonyVmDispatchReq(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)


class HarmonyVmForceReleaseReq(BaseModel):
    confirmed: bool = False
    reason: str = Field(..., min_length=8, max_length=500)


class HarmonyVmSettingReq(BaseModel):
    # 留空表示关闭该能力，回到 DevEco 每台随机生成 uuid 的默认行为。
    # 非空则必须是标准 UUID：格式错了会算出一个无效 UDID，报备的值对不上，
    # 而且要等虚拟机起来才发现，所以在入口就拦掉。
    instance_uuid: str = Field(
        "",
        max_length=64,
        pattern=(
            r"^$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        ),
    )


class HarmonyVmCatalogImportReq(BaseModel):
    manifest: Dict[str, Any]
    source_url: str = Field("", max_length=512)
    collected_at: Optional[str] = Field(None, max_length=64)
