from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Callable, Dict, Optional, Set, Tuple

from adbutils import adb

from ai_phone.agent.app_install.ios import (
    _close_quietly,
    _try_get_tunneld_rsd,
)
from ai_phone.agent.drivers.hdc import HdcError, hdc_run, hdc_shell
from ai_phone.agent.drivers.ios import _import_pmd3, _maybe_sync
from ai_phone.agent.drivers.simctl import SimctlError, simctl_run

UninstallResult = Tuple[bool, str, str]

APP_NOT_FOUND = "app_not_found"
UNINSTALL_FAILED = "uninstall_failed"
_WDA_RUNNER_SUFFIX = ".xctrunner"


def uninstall_by_platform(
    platform: str,
    serial: str,
    package_name: str,
    timeout_sec: int,
) -> UninstallResult:
    uninstaller = {
        "android": uninstall_android,
        "harmony": uninstall_harmony,
        "ios": uninstall_ios,
        "ios_sim": uninstall_ios_sim,
    }.get(platform)
    if uninstaller is None:
        return False, "platform_unsupported", f"不支持的平台: {platform}"
    return uninstaller(serial, package_name, timeout_sec)


def _wait_absent(list_apps: Callable[[], Set[str]], package_name: str) -> bool:
    for attempt in range(5):
        if package_name not in list_apps():
            return True
        if attempt < 4:
            time.sleep(0.4)
    return False


def _parse_android_packages(raw: str) -> Set[str]:
    out: Set[str] = set()
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            package_name = line.removeprefix("package:").strip()
            if package_name:
                out.add(package_name)
    return out


def uninstall_android(serial: str, package_name: str, timeout_sec: int) -> UninstallResult:
    device = adb.device(serial=serial)

    def list_apps() -> Set[str]:
        raw = str(device.shell(["pm", "list", "packages", "-3"], timeout=20) or "")
        return _parse_android_packages(raw)

    try:
        if package_name not in list_apps():
            return False, APP_NOT_FOUND, "设备中无法找到对应的 App"
        raw = str(
            device.shell(
                ["pm", "uninstall", package_name],
                timeout=max(30, int(timeout_sec)),
            )
            or ""
        ).strip()
        if "Success" not in raw:
            return False, UNINSTALL_FAILED, raw or "pm uninstall 未返回 Success"
        if not _wait_absent(list_apps, package_name):
            return False, UNINSTALL_FAILED, "卸载命令成功，但重新查询后 App 仍然存在"
        return True, "", "卸载成功"
    except Exception as exc:  # noqa: BLE001
        return False, UNINSTALL_FAILED, f"{type(exc).__name__}: {exc}"


def _harmony_user_apps(serial: str) -> Set[str]:
    raw = hdc_shell(serial, "bm dump -a", timeout=20)
    out: Set[str] = set()
    for line in raw.replace("\r", "").splitlines():
        package_name = line.strip()
        if not package_name or package_name.startswith("ID:"):
            continue
        # 与现有 HarmonyDriver 的第三方应用口径一致，并额外排除系统命名空间。
        if package_name.startswith(("com.huawei.", "com.ohos.", "ohos.")):
            continue
        out.add(package_name)
    return out


def uninstall_harmony(serial: str, package_name: str, timeout_sec: int) -> UninstallResult:
    try:
        if package_name not in _harmony_user_apps(serial):
            return False, APP_NOT_FOUND, "设备中无法找到对应的 App"
        raw = hdc_run(
            "uninstall",
            package_name,
            serial=serial,
            timeout=max(30, int(timeout_sec)),
            check=True,
        )
        if not _wait_absent(lambda: _harmony_user_apps(serial), package_name):
            return False, UNINSTALL_FAILED, "卸载命令成功，但重新查询后 App 仍然存在"
        return True, "", raw or "卸载成功"
    except HdcError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        return False, UNINSTALL_FAILED, message or "hdc uninstall 失败"
    except Exception as exc:  # noqa: BLE001
        return False, UNINSTALL_FAILED, f"{type(exc).__name__}: {exc}"


def _ios_user_apps(ip: Any, package_name: str) -> Set[str]:
    apps = _maybe_sync(
        ip.get_apps(
            application_type="User",
            bundle_identifiers=[package_name],
        ),
        timeout=30,
    ) or {}
    return {
        str(bundle_id)
        for bundle_id in apps
        if str(bundle_id) and not str(bundle_id).endswith(_WDA_RUNNER_SUFFIX)
    }


def _uninstall_ios_with_lockdown(
    installation_proxy: Any,
    lockdown: Any,
    package_name: str,
    timeout_sec: int,
) -> UninstallResult:
    ip = installation_proxy(lockdown=lockdown)
    _maybe_sync(ip.connect(), timeout=30)
    try:
        def list_apps() -> Set[str]:
            return _ios_user_apps(ip, package_name)

        if package_name not in list_apps():
            return False, APP_NOT_FOUND, "设备中无法找到对应的 App"
        _maybe_sync(ip.uninstall(package_name), timeout=max(30, int(timeout_sec)))
        if not _wait_absent(list_apps, package_name):
            return False, UNINSTALL_FAILED, "卸载命令成功，但重新查询后 App 仍然存在"
        return True, "", "卸载成功"
    finally:
        _close_quietly(ip)


def uninstall_ios(serial: str, package_name: str, timeout_sec: int) -> UninstallResult:
    try:
        _usbmux, create_lockdown, _screenshot_svc, installation_proxy = _import_pmd3()
    except Exception as exc:  # noqa: BLE001
        return False, UNINSTALL_FAILED, f"{type(exc).__name__}: {exc}"

    last_exc: Optional[BaseException] = None
    rsd = _try_get_tunneld_rsd(serial)
    if rsd is not None:
        try:
            return _uninstall_ios_with_lockdown(
                installation_proxy, rsd, package_name, timeout_sec
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        finally:
            _close_quietly(rsd)

    lockdown = None
    try:
        lockdown = _maybe_sync(create_lockdown(serial=serial), timeout=30)
        return _uninstall_ios_with_lockdown(
            installation_proxy, lockdown, package_name, timeout_sec
        )
    except Exception as exc:  # noqa: BLE001
        last_exc = exc
    finally:
        _close_quietly(lockdown)

    exc_name = type(last_exc).__name__ if last_exc is not None else "Error"
    hint = ""
    if "NotPaired" in exc_name or "InvalidService" in exc_name:
        hint = "；iOS 17+ 可能需要先启动 pymobiledevice3 remote tunneld 并完成信任"
    return False, UNINSTALL_FAILED, f"{exc_name}: {last_exc}{hint}"


def _ios_sim_user_apps(serial: str) -> Set[str]:
    raw = simctl_run("listapps", serial, timeout=30.0)
    proc = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", "-"],
        input=raw,
        capture_output=True,
        text=True,
        timeout=30.0,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"plutil 转换 listapps 输出失败 rc={proc.returncode}: "
            f"{(proc.stderr or '').strip()}"
        )
    data: Dict[str, Any] = json.loads(proc.stdout or "{}")
    return {
        str(bundle_id)
        for bundle_id, info in data.items()
        if isinstance(info, dict)
        and str(info.get("ApplicationType") or "") == "User"
        and not str(bundle_id).endswith(_WDA_RUNNER_SUFFIX)
    }


def uninstall_ios_sim(serial: str, package_name: str, timeout_sec: int) -> UninstallResult:
    try:
        if package_name not in _ios_sim_user_apps(serial):
            return False, APP_NOT_FOUND, "设备中无法找到对应的 App"
        simctl_run(
            "uninstall",
            serial,
            package_name,
            timeout=float(max(30, int(timeout_sec))),
        )
        if not _wait_absent(lambda: _ios_sim_user_apps(serial), package_name):
            return False, UNINSTALL_FAILED, "卸载命令成功，但重新查询后 App 仍然存在"
        return True, "", "卸载成功"
    except SimctlError as exc:
        return False, UNINSTALL_FAILED, f"simctl uninstall 失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, UNINSTALL_FAILED, f"{type(exc).__name__}: {exc}"


__all__ = ["uninstall_by_platform"]
