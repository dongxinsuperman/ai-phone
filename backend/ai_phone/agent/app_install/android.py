from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Tuple

from adbutils import adb
from loguru import logger

InstallResult = Tuple[bool, str, str]


def install_apk(serial: str, package_path: Path, timeout_sec: int) -> InstallResult:
    """通过 adb push + pm install 安装 APK，结果完全以命令反馈为准。"""
    device = adb.device(serial=serial)
    suffix = package_path.suffix.lower() or ".apk"
    remote = f"/data/local/tmp/ai-phone-install-{uuid.uuid4().hex[:8]}{suffix}"
    try:
        logger.info("app_install android push serial={} local={} remote={}", serial, package_path, remote)
        device.push(str(package_path), remote)
        out = str(
            device.shell(
                ["pm", "install", "-r", "-t", "-g", remote],
                timeout=max(30, int(timeout_sec)),
            )
            or ""
        ).strip()
        logger.info("app_install android pm install serial={} output={}", serial, out or "(空)")
        if re.search(r"\bSuccess\b", out):
            return True, "", "安装成功"
        return False, "install_failed", out or "pm install 未返回 Success"
    except Exception as exc:  # noqa: BLE001
        return False, "install_failed", f"{type(exc).__name__}: {exc}"
    finally:
        try:
            device.shell(["rm", "-f", remote], timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.debug("app_install android 清理临时包失败 serial={} remote={}: {}", serial, remote, exc)
