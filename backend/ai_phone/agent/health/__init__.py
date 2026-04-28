"""Agent 侧 Readiness Gate（设备可调度入口）—— v1 第 1 梯队。

本包专注于回答一个问题：
    "这台 online 的设备，此刻真的能被派单去跑 item 吗？"

和已有 DEVICE_STATUS（WDA 启动进度）相比：
- DEVICE_STATUS 是 iOS 专属的"上线动作进度条"，事件驱动、只在有变化时发；
- Readiness 是三端统一的、稳态下持续进行的探活；
- 二者在 Hub 端并行保存、互不覆盖。

严格约束（v1 不变）：
- 只读、旁路；不修改也不复用任何 Driver/Mirror/Runner 主路径；
- 探活失败**不自救**——不尝试解锁、不尝试重启 WDA、不尝试重连 hmdriver2；
- 只上报状态，让 Server 侧决定"派不派单"。

外部入口：
- :class:`ReadinessSupervisor` —— 后台 asyncio 任务，轮询所有已知 serial。
"""
from __future__ import annotations

from .probe import (
    BaseProbe,
    ProbeOutcome,
    AndroidProbe,
    IosProbe,
    HarmonyProbe,
    build_probe_for,
)
from .supervisor import ReadinessSupervisor

__all__ = [
    "BaseProbe",
    "ProbeOutcome",
    "AndroidProbe",
    "IosProbe",
    "HarmonyProbe",
    "build_probe_for",
    "ReadinessSupervisor",
]
