"""设备驱动包：抽象 + Android (adbutils) + iOS (pymobiledevice3 + WDA) + Harmony (hdc + hmdriver2)。

iOS / Harmony 两支都走"按需 lazy import"——第三方库在各自可选 extras 里，
没装也不会让本模块导入失败。设备发现会自动跳过未启用的那一类。
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Lock
from typing import Dict, List

from loguru import logger

from .base import BaseDriver, DeviceInfo
from .android import AndroidDriver, list_android_devices, open_android_driver

# iOS 虚拟机只依赖标准库 + simctl CLI，没有可选 extras 的问题。这里仍然包一层
# try/except，是为了兑现方案铁律「绝不影响现有流程」：即使本模块自身出问题，
# 也不能让整个 drivers 包 import 失败、进而拖垮 Android / iOS 真机 / Harmony。
try:  # pragma: no cover
    from .ios_simulator import (  # noqa: F401
        PLATFORM_IOS_SIM,
        list_ios_simulators,
        mark_managed,
        unmark_managed,
    )
    from .ios_simulator_driver import (  # noqa: F401
        IosSimulatorDriver,
        open_ios_simulator_driver,
    )
    _IOS_SIM_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    logger.warning("iOS 虚拟机模块加载失败，已禁用该能力（不影响其他平台）：{}", exc)
    PLATFORM_IOS_SIM = "ios_sim"  # type: ignore[assignment]
    IosSimulatorDriver = None  # type: ignore[assignment]

    def list_ios_simulators(  # type: ignore[misc]
        include_offline: bool = False, managed_only: bool = True
    ) -> List[DeviceInfo]:
        return []

    def mark_managed(udid: str) -> None:  # type: ignore[misc]
        return None

    def unmark_managed(udid: str) -> None:  # type: ignore[misc]
        return None

    def open_ios_simulator_driver(udid: str, **_kw):  # type: ignore[misc]
        raise RuntimeError(
            "iOS 虚拟机支持加载失败，无法打开驱动。请查看 agent 启动日志里的 "
            "「iOS 虚拟机模块加载失败」一行定位原因。"
        )

    _IOS_SIM_AVAILABLE = False

# iOS 这一支可能因为未装 pymobiledevice3 import 失败；不强制要求
try:  # pragma: no cover
    from .ios import IosDriver, list_ios_devices, open_ios_driver  # noqa: F401
    _IOS_AVAILABLE = True
except Exception:  # noqa: BLE001
    IosDriver = None  # type: ignore[assignment]

    def list_ios_devices(include_offline: bool = False) -> List[DeviceInfo]:  # type: ignore[misc]
        return []

    def open_ios_driver(udid: str, **_kw):  # type: ignore[misc]
        raise RuntimeError(
            "iOS 支持未启用：请 pip install -e \".[ios]\" 后重启 agent。"
        )

    _IOS_AVAILABLE = False


# Harmony 同 iOS 走 lazy import —— 未装 hmdriver2 / hdc 不可用都不让模块 import 炸；
# hdc 没有时 list_harmony_devices 返空列表，open_harmony_driver 会抛明确错误
try:  # pragma: no cover
    from .harmony import (  # noqa: F401
        HarmonyDriver,
        list_harmony_devices,
        open_harmony_driver,
    )
    _HARMONY_AVAILABLE = True
except Exception:  # noqa: BLE001
    HarmonyDriver = None  # type: ignore[assignment]

    def list_harmony_devices(include_offline: bool = False) -> List[DeviceInfo]:  # type: ignore[misc]
        return []

    def open_harmony_driver(serial: str, **_kw):  # type: ignore[misc]
        raise RuntimeError(
            "HarmonyOS 支持未启用：请 pip install -e \"backend[harmony]\" "
            "并确保 hdc 在 PATH（DevEco Studio 自带，需要手动配环境变量）后重启 agent。"
        )

    _HARMONY_AVAILABLE = False


_SCAN_BUDGET_SEC = 5.0
_SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="device-discovery")
_SCAN_LOCK = Lock()
_SCAN_PENDING: Dict[str, tuple[bool, Future]] = {}
_SCAN_LAST: Dict[tuple, List[DeviceInfo]] = {}
_SCAN_OK: Dict[str, bool] = {}


def was_last_device_scan_ok(platform: str) -> bool:
    """False means a retained snapshot, not evidence that the device is gone.

    Readiness still performs a directed probe, but must not give retained
    devices the normal multi-failure grace period if that probe also fails.
    """
    return _SCAN_OK.get(platform, True)


def list_all_devices(include_offline: bool = False) -> List[DeviceInfo]:
    """Bounded platform discovery, preserving the existing output order.

    Four platforms may scan concurrently, but a platform never receives a
    second scan while its first is still running. A hung native driver cannot
    grow the executor queue on each refresh. Timeout/error retains its previous
    snapshot and marks it untrusted; lifecycle code must check instance/process
    evidence, not interpret a missing discovery result as a shutdown.
    """
    scanners = (
        ("android", list_android_devices),
        ("ios", list_ios_devices),
        ("harmony", list_harmony_devices),
        (PLATFORM_IOS_SIM, list_ios_simulators),
    )
    with _SCAN_LOCK:
        submitted = []
        for platform, scanner in scanners:
            if platform not in _SCAN_PENDING:
                future = _SCAN_EXECUTOR.submit(scanner, include_offline=include_offline)
                _SCAN_PENDING[platform] = (
                    include_offline,
                    future,
                )
                submitted.append(future)
        # A scan that already exceeded its budget gets no new five-second wait
        # on every refresh; collect it only once its native work has finished.
        wait(submitted, timeout=_SCAN_BUDGET_SEC)
        out: List[DeviceInfo] = []
        for platform, _ in scanners:
            key = (platform, include_offline)
            pending_offline, pending = _SCAN_PENDING[platform]
            if pending.done():
                _SCAN_PENDING.pop(platform)
                try:
                    _SCAN_LAST[(platform, pending_offline)] = list(pending.result())
                    _SCAN_OK[platform] = pending_offline == include_offline
                except Exception as exc:  # noqa: BLE001
                    _SCAN_OK[platform] = False
                    logger.warning("{} 设备扫描失败，暂留上次快照：{}", platform, exc)
            else:
                _SCAN_OK[platform] = False
                logger.warning("{} 设备扫描超时，等待在途扫描结束", platform)
            out.extend(_SCAN_LAST.get(key, []))
        return out


def open_driver(serial: str, platform: str, **kwargs) -> BaseDriver:
    """按 platform 路由到对应 driver 工厂。serial 全局唯一即可，平台标签由
    上层（agent main 持有的 platform map）按设备发现时记录。

    ``**kwargs`` 当前：
    - iOS ``on_status``：WDA 启动进度回调
    - iOS 虚拟机 ``on_status``：同上（首次要编译 WDA，进度反馈尤其有用）
    - Harmony：预留（暂无特殊 kwarg）
    Android driver 不接受额外参数，会静默忽略。
    """
    if platform == "android":
        return open_android_driver(serial)
    if platform == "ios":
        return open_ios_driver(serial, **kwargs)
    if platform == "harmony":
        return open_harmony_driver(serial, **kwargs)
    if platform == PLATFORM_IOS_SIM:
        return open_ios_simulator_driver(serial, **kwargs)
    raise ValueError(f"未知 platform: {platform}")


__all__ = [
    "BaseDriver",
    "DeviceInfo",
    "AndroidDriver",
    "list_android_devices",
    "open_android_driver",
    "IosDriver",
    "list_ios_devices",
    "open_ios_driver",
    "HarmonyDriver",
    "list_harmony_devices",
    "open_harmony_driver",
    "PLATFORM_IOS_SIM",
    "IosSimulatorDriver",
    "list_ios_simulators",
    "open_ios_simulator_driver",
    "mark_managed",
    "unmark_managed",
    "list_all_devices",
    "was_last_device_scan_ok",
    "open_driver",
]
