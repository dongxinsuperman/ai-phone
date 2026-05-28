from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from fastapi import HTTPException, UploadFile

from ai_phone.config import get_settings

_PLATFORM_BY_EXT = {
    ".apk": "android",
    ".hap": "harmony",
    ".app": "harmony",
    ".ipa": "ios",
}


def platform_from_filename(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    platform = _PLATFORM_BY_EXT.get(suffix)
    if not platform:
        raise HTTPException(status_code=400, detail="只支持 .apk / .hap / .app / .ipa 包")
    return platform


def _safe_filename(filename: str) -> str:
    name = Path(filename or "app-package").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "app-package"


def _package_root() -> Path:
    root = Path(get_settings().storage_dir).resolve() / "app-install"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def save_upload(file: UploadFile) -> Tuple[str, str, str]:
    """流式保存上传包，返回 (filename, platform, storage_path)。"""
    filename = _safe_filename(file.filename or "")
    platform = platform_from_filename(filename)
    stamp = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%d")
    bucket = _package_root() / stamp
    bucket.mkdir(parents=True, exist_ok=True)
    target = bucket / f"{secrets.token_hex(8)}-{filename}"

    size = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                fh.write(chunk)
    finally:
        await file.close()

    if size <= 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="empty file")
    return filename, platform, str(target)
