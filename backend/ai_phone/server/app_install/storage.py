from __future__ import annotations

import re
import secrets
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from fastapi import HTTPException, UploadFile
from loguru import logger

from ai_phone.config import get_settings

# 扩展名足以定平台的几种。注意 ``.app`` 归鸿蒙——那是 HarmonyOS 的 App Pack 格式，
# 已经在用，不能被 iOS 抢走。
_PLATFORM_BY_EXT = {
    ".apk": "android",
    ".hap": "harmony",
    ".app": "harmony",
    ".ipa": "ios",
}

# iOS 虚拟机的产物是 ``MyApp.app`` **目录**，HTTP 传不了目录，只能打成 zip。
# 而 zip 是通用容器，光看扩展名说明不了任何事，所以落盘后开包看内容。
_NEEDS_INSPECTION = {".zip"}

_UNSUPPORTED_DETAIL = (
    "只支持 .apk / .hap / .app / .ipa，以及装 iOS 虚拟机用的 .zip"
    "（zip 里要有 xxx.app/Info.plist）"
)


def platform_from_filename(filename: str) -> str:
    """按扩展名定平台。需要开包才能确定的返回空串，交给 :func:`platform_from_file`。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix in _NEEDS_INSPECTION:
        return ""
    platform = _PLATFORM_BY_EXT.get(suffix)
    if not platform:
        raise HTTPException(status_code=400, detail=_UNSUPPORTED_DETAIL)
    return platform


def is_simulator_app_zip(path: Path) -> bool:
    """zip 里是否装着一个 iOS 虚拟机 ``.app`` bundle。

    **只读 zip 的条目名，不解压**——与鸿蒙 ``parse_hap_abi`` 同样的安全口径：
    上传包是不可信输入，Server 侧一律不落地展开。

    判据是存在 ``<任意路径>/xxx.app/Info.plist``。这是 ``.app`` bundle 的必要构成，
    没有它 ``simctl install`` 一定失败。
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            for name in archive.namelist():
                parts = str(name).replace("\\", "/").split("/")
                if len(parts) < 2 or parts[-1] != "Info.plist":
                    continue
                if parts[-2].lower().endswith(".app"):
                    return True
    except zipfile.BadZipFile:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("检查上传 zip 失败 path={}：{}", path, exc)
        return False
    return False


def platform_from_file(filename: str, path: Path) -> str:
    """先看扩展名，扩展名说了不算时开包看内容。"""
    platform = platform_from_filename(filename)
    if platform:
        return platform
    if is_simulator_app_zip(path):
        return "ios_sim"
    raise HTTPException(
        status_code=400,
        detail=(
            "这个 zip 里没有找到 xxx.app/Info.plist，认不出是什么包。"
            "iOS 虚拟机包请把 Xcode 构建出的 xxx.app 目录整个压成 zip 后上传。"
        ),
    )


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
    # 扩展名就能定平台的，先拒掉明显不支持的类型，别白传几百 MB
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

    if not platform:
        # zip 要开包才知道是什么。认不出就连文件一起删掉，不留垃圾。
        try:
            platform = platform_from_file(filename, target)
        except HTTPException:
            target.unlink(missing_ok=True)
            raise
    return filename, platform, str(target)
