"""设备驱动包：抽象 + Android (adbutils) + iOS (pymobiledevice3 + WDA) + Harmony (hdc + hmdriver2)。

iOS / Harmony 两支都走"按需 lazy import"——第三方库在各自可选 extras 里，
没装也不会让本模块导入失败。设备发现会自动跳过未启用的那一类。
"""
from __future__ import annotations

from typing import List

from .base import BaseDriver, DeviceInfo
from .android import AndroidDriver, list_android_devices, open_android_driver

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
            "并确保 hdc 在 PATH（详见 HarmonyOS环境配置笔记.md）后重启 agent。"
        )

    _HARMONY_AVAILABLE = False


def list_all_devices(include_offline: bool = False) -> List[DeviceInfo]:
    """合并扫描 Android + iOS + Harmony。供 agent 上线广播 / device 列表使用。

    顺序：Android 先（adb 通常最快）→ iOS → Harmony；前端按 platform 排序自己玩。
    任一平台扫描失败都不影响其他平台（各自 try/except）。
    """
    out: List[DeviceInfo] = []
    out.extend(list_android_devices(include_offline=include_offline))
    out.extend(list_ios_devices(include_offline=include_offline))
    out.extend(list_harmony_devices(include_offline=include_offline))
    return out


def open_driver(serial: str, platform: str, **kwargs) -> BaseDriver:
    """按 platform 路由到对应 driver 工厂。serial 全局唯一即可，平台标签由
    上层（agent main 持有的 platform map）按设备发现时记录。

    ``**kwargs`` 当前：
    - iOS ``on_status``：WDA 启动进度回调
    - Harmony：预留（暂无特殊 kwarg）
    Android driver 不接受额外参数，会静默忽略。
    """
    if platform == "android":
        return open_android_driver(serial)
    if platform == "ios":
        return open_ios_driver(serial, **kwargs)
    if platform == "harmony":
        return open_harmony_driver(serial, **kwargs)
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
    "list_all_devices",
    "open_driver",
]
