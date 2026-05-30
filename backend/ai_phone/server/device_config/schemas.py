from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


ALLOWED_WAKE_POLICY_PLATFORMS = {"harmony"}


def normalize_platform(value: str) -> str:
    return str(value or "").strip().lower()


class DeviceWakePolicyUpsert(BaseModel):
    serial: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=16)
    wake_swipe: bool = False
    remark: str = Field(default="", max_length=1000)

    @field_validator("serial", "remark", mode="before")
    @classmethod
    def _strip_text(cls, value):
        return str(value or "").strip()

    @field_validator("platform", mode="before")
    @classmethod
    def _normalize_platform(cls, value):
        return normalize_platform(value)


class DeviceWakePolicyPatch(BaseModel):
    platform: Optional[str] = Field(default=None, min_length=1, max_length=16)
    wake_swipe: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("remark", mode="before")
    @classmethod
    def _strip_remark(cls, value):
        if value is None:
            return None
        return str(value).strip()

    @field_validator("platform", mode="before")
    @classmethod
    def _normalize_platform(cls, value):
        if value is None:
            return None
        return normalize_platform(value)
