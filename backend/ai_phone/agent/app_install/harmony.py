from __future__ import annotations

from pathlib import Path
from typing import Tuple

from ai_phone.agent.drivers.hdc import HdcError, hdc_run
from loguru import logger

InstallResult = Tuple[bool, str, str]


def install_hap(serial: str, package_path: Path, timeout_sec: int) -> InstallResult:
    """通过 hdc install 安装 HarmonyOS 包，hdc 命令成功即视为成功。"""
    try:
        out = hdc_run(
            "install",
            "-r",
            str(package_path),
            serial=serial,
            timeout=max(30, int(timeout_sec)),
            check=True,
        )
        logger.info("app_install harmony hdc install serial={} output={}", serial, out or "(空)")
        return True, "", out or "安装成功"
    except HdcError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip()
        return False, "install_failed", msg or f"hdc install 失败 rc={exc.returncode}"
    except Exception as exc:  # noqa: BLE001
        return False, "install_failed", f"{type(exc).__name__}: {exc}"
