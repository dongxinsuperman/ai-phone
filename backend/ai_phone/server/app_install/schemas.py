from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class CreateTaskRequest(BaseModel):
    package_id: str = Field(..., min_length=1)
    serials: List[str] = Field(..., min_length=1)
