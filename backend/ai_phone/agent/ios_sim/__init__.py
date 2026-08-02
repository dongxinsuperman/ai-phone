"""iOS Simulator 虚拟机管理（Agent 侧）。

本包与 :mod:`ai_phone.agent.android_vm` / :mod:`ai_phone.agent.harmony_vm` 刻意
保持独立，三条链路只在通用设备层再次汇合。实现口径见
``docs-internal/ios-simulator-plan（iOS虚拟机独立接入方案）.md`` §0.3：
**一等对照物是 Android 虚拟机。**
"""
from .capability import (  # noqa: F401
    IosSimTools,
    find_ios_sim_tools,
    probe_ios_sim_capability,
)
from .manager import (  # noqa: F401
    IosSimVmManager,
    SimVmRuntime,
    managed_sim_name,
    vmid_from_sim_name,
)

__all__ = [
    "IosSimTools",
    "find_ios_sim_tools",
    "probe_ios_sim_capability",
    "IosSimVmManager",
    "SimVmRuntime",
    "managed_sim_name",
    "vmid_from_sim_name",
]
