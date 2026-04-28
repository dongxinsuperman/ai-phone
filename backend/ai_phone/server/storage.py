"""文件落盘：按日期分桶 + UUID 命名，返回稳定可访问的 ``/files/...`` URL。

生产后续可换对象存储；API 保持不变即可。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from ai_phone.config import get_settings


@dataclass
class SavedFile:
    id: str
    rel_path: str           # "2026-04-18/abc123.jpg"
    abs_path: Path
    size: int
    content_type: str

    @property
    def url(self) -> str:
        return f"/files/{self.rel_path}"


_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "application/octet-stream": ".bin",
}


def _pick_ext(content_type: str) -> str:
    return _EXT_BY_MIME.get(content_type.lower(), ".bin")


def _storage_root() -> Path:
    root = Path(get_settings().storage_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_bytes(
    data: bytes,
    content_type: str = "image/jpeg",
    *,
    ts: Optional[float] = None,
) -> SavedFile:
    """同步落盘；screenshot 通常 <100KB，不需要走 aiofiles。"""
    if not data:
        raise ValueError("empty payload")

    root = _storage_root()
    stamp = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")
    bucket = root / stamp
    bucket.mkdir(parents=True, exist_ok=True)

    fid = secrets.token_hex(8)
    ext = _pick_ext(content_type)
    rel = f"{stamp}/{fid}{ext}"
    abs_path = root / rel
    abs_path.write_bytes(data)
    logger.debug("文件落盘 {} ({} B)", rel, len(data))
    return SavedFile(
        id=fid,
        rel_path=rel,
        abs_path=abs_path,
        size=len(data),
        content_type=content_type,
    )


def mount_static(app) -> None:
    """把 storage_dir 以 ``/files`` 前缀 mount 到 FastAPI。"""
    from fastapi.staticfiles import StaticFiles

    root = _storage_root()
    app.mount("/files", StaticFiles(directory=str(root)), name="files")
