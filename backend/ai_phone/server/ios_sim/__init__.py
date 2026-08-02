"""iOS Simulator 虚拟机管理（Server 侧）。

本包与 :mod:`ai_phone.server.android_vm` / :mod:`ai_phone.server.harmony_vm` 刻意
保持独立，三条实现只在通用设备 / Hub 层再次汇合。隔离范围与鸿蒙一致：独立表、
独立 API 前缀、独立协议命名空间、独立前端 Tab。

实现口径见 ``docs-internal/ios-simulator-plan（iOS虚拟机独立接入方案）.md``：

- §0.3  一等对照物是 Android 虚拟机，鸿蒙只作第二样本
- §6.5  生命周期设计（常驻实例、三字段表单、无端口租约）
- §6.5.6 数据库：独立表 + 独立隔离初始化 + additive-only 迁移
"""
from .models import (  # noqa: F401
    IOS_SIM_TABLES,
    IosSimCatalogSnapshot,
    IosSimVmInstance,
)

__all__ = [
    "IOS_SIM_TABLES",
    "IosSimCatalogSnapshot",
    "IosSimVmInstance",
]
