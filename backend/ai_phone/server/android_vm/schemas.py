"""Pydantic schemas for Android VM management."""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class AndroidVmCreateReq(BaseModel):
    name: str = Field("", max_length=128)  # 兼容字段：恒等于 alias，可空（身份锚点是 vm_id）
    alias: str = Field("", max_length=128)
    profile_ref_type: str = Field("custom", pattern="^(real_device|coverage_strategy|custom|copied)$")
    profile_ref_id: str = Field("", max_length=64)
    profile_id: str = Field("", max_length=64)
    profile_name: str = Field("", max_length=128)
    config_version: int = Field(1, ge=1, le=99)
    config_json: Dict[str, Any] = Field(default_factory=dict)
    capability_marks: Dict[str, Any] = Field(default_factory=dict)
    api_level: int = Field(35, ge=21, le=99)
    abi: str = Field("auto", pattern="^(auto|arm64|arm64-v8a|x86_64)$")
    system_type: str = Field("google_apis", pattern="^(google_apis|default)$")
    system_image: str = Field("", max_length=255)
    screen_width: int = Field(1080, ge=320, le=7680)
    screen_height: int = Field(2400, ge=320, le=7680)
    density: int = Field(420, ge=120, le=800)
    orientation: str = Field("portrait", pattern="^(portrait|landscape)$")


class AndroidVmPatchReq(BaseModel):
    name: Optional[str] = Field(None, max_length=128)  # 忽略：name 恒镜像 alias
    alias: Optional[str] = Field(None, max_length=128)
    profile_ref_type: Optional[str] = Field(None, pattern="^(real_device|coverage_strategy|custom|copied)$")
    profile_ref_id: Optional[str] = Field(None, max_length=64)
    profile_id: Optional[str] = Field(None, max_length=64)
    profile_name: Optional[str] = Field(None, max_length=128)
    config_version: Optional[int] = Field(None, ge=1, le=99)
    config_json: Optional[Dict[str, Any]] = None
    capability_marks: Optional[Dict[str, Any]] = None
    api_level: Optional[int] = Field(None, ge=21, le=99)
    abi: Optional[str] = Field(None, pattern="^(auto|arm64|arm64-v8a|x86_64)$")
    system_type: Optional[str] = Field(None, pattern="^(google_apis|default)$")
    system_image: Optional[str] = Field(None, max_length=255)
    screen_width: Optional[int] = Field(None, ge=320, le=7680)
    screen_height: Optional[int] = Field(None, ge=320, le=7680)
    density: Optional[int] = Field(None, ge=120, le=800)
    orientation: Optional[str] = Field(None, pattern="^(portrait|landscape)$")


class AndroidVmDispatchReq(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)


class AndroidVmProbeResp(BaseModel):
    request_id: str
    agents: list[Dict[str, Any]]


class AndroidDeviceProfileImportReq(BaseModel):
    csv_text: str = Field(..., min_length=1)
    source_url: str = Field("", max_length=512)
    collected_at: Optional[str] = Field(None, max_length=64)
