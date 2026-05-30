from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from loguru import logger

from ai_phone.agent.drivers.ios import _import_pmd3, _maybe_sync

InstallResult = Tuple[bool, str, str]


def install_ipa(serial: str, package_path: Path, timeout_sec: int) -> InstallResult:
    """通过 pymobiledevice3 InstallationProxy 安装 IPA。

    优先尝试 tunneld/RSD；没有 tunneld 时回落到 usbmux lockdown。安装成败只取
    InstallationProxy 的返回/异常，不再额外查询 bundle id。
    """
    try:
        _usbmux, create_lockdown, _screenshot_svc, installation_proxy = _import_pmd3()
    except Exception as exc:  # noqa: BLE001
        return False, "install_failed", f"{type(exc).__name__}: {exc}"

    timeout = max(60, int(timeout_sec))
    last_exc: Optional[BaseException] = None

    rsd = _try_get_tunneld_rsd(serial)
    if rsd is not None:
        try:
            _install_via_lockdown(installation_proxy, rsd, package_path, timeout)
            logger.info("app_install ios install serial={} via=tunneld+RSD", serial)
            return True, "", "安装成功"
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("app_install ios via tunneld+RSD 失败 serial={}: {}", serial, exc)
        finally:
            _close_quietly(rsd)

    lockdown = None
    try:
        lockdown = _maybe_sync(create_lockdown(serial=serial), timeout=30)
        _install_via_lockdown(installation_proxy, lockdown, package_path, timeout)
        logger.info("app_install ios install serial={} via=usbmux", serial)
        return True, "", "安装成功"
    except Exception as exc:  # noqa: BLE001
        last_exc = exc
    finally:
        _close_quietly(lockdown)

    hint = ""
    exc_name = type(last_exc).__name__ if last_exc is not None else "Error"
    if "NotPaired" in exc_name or "InvalidService" in exc_name:
        hint = "；iOS 17+ 可能需要先启动 pymobiledevice3 remote tunneld 并完成信任"
    return False, "install_failed", f"{exc_name}: {last_exc}{hint}"


def _install_via_lockdown(
    installation_proxy: Any,
    lockdown: Any,
    package_path: Path,
    timeout: int,
) -> None:
    ip = installation_proxy(lockdown=lockdown)
    _maybe_sync(ip.connect(), timeout=30)
    try:
        _maybe_sync(ip.install_from_local(str(package_path)), timeout=timeout)
    finally:
        _close_quietly(ip)


def _try_get_tunneld_rsd(serial: str) -> Any:
    try:
        from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("app_install ios tunneld API 不可用 serial={}: {}", serial, exc)
        return None
    try:
        return _maybe_sync(get_tunneld_device_by_udid(serial), timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.debug("app_install ios 查询 tunneld 失败 serial={}: {}", serial, exc)
        return None


def _close_quietly(obj: Any) -> None:
    if obj is None:
        return
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        _maybe_sync(close(), timeout=5)
    except Exception:  # noqa: BLE001
        pass
