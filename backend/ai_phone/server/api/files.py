"""/api/files：Agent 上传截图等二进制的入口。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from ..storage import save_bytes

router = APIRouter(prefix="/api/files", tags=["files"])


# Agent 多个并发上传时每次 <1MB；FastAPI 默认 multipart 上限够用
@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(
    file: UploadFile = File(...),
    content_type: str = Form("image/jpeg"),
) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    saved = save_bytes(data, content_type=content_type)
    return {
        "id": saved.id,
        "url": saved.url,
        "size": saved.size,
        "content_type": saved.content_type,
    }
