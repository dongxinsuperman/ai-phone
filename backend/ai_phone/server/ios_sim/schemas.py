"""iOS 虚拟机 REST 的请求/响应 schema。

字段之少是本平台的特点，不是遗漏：``simctl create`` 只接受名字、机型、系统版本三个
参数，实例配置文件里也只有 9 个键、无一与内存/CPU/存储相关（方案 §6.5.2、§6.5.3）。

对比另两端的创建表单：

```text
Android  api_level / abi / system_image / 屏幕 / 密度 / 方向 / RAM / CPU / GPU / 网络 / 快照 …
鸿蒙      device_type / os_version / fold_state / memory_gb / storage_gb / screen_profile
iOS Sim  alias / device_type / runtime            ← 就这三个
```
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class IosSimVmCreateReq(BaseModel):
    """创建一台受管虚拟机。

    ``alias`` 必填且唯一——与 Android / 鸿蒙一致，别名是人识别设备的唯一凭据。
    ``device_type`` 与 ``runtime`` 必填：不给默认值是刻意的，避免"用户没选清楚就
    造出一台机型和系统版本都不确定的设备"。
    """

    alias: str = Field(min_length=1, max_length=128, description="设备别名，唯一")
    device_type: str = Field(
        min_length=1, max_length=255,
        description="机型 identifier，如 com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
    )
    runtime: str = Field(
        min_length=1, max_length=255,
        description="系统版本 runtime identifier，如 com.apple.CoreSimulator.SimRuntime.iOS-26-0",
    )
    name: Optional[str] = Field(default=None, max_length=128, description="展示名，缺省取 alias")
    # 预留：苹果将来若放开更多可配项，加在这里，不改表结构
    config_json: Dict[str, Any] = Field(default_factory=dict)


class IosSimVmPatchReq(BaseModel):
    """修改配置。

    **机型与系统版本是创建时按官方目录校验后锁定的，之后不可改**——与鸿蒙
    ``CATALOG_LOCKED_FIELDS`` 完全一致。鸿蒙那边的理由同样适用：允许在 PATCH 里改
    等于绕过整套兼容校验（机型 × 版本区间），能改出一台起不来的配置，而且直到下发
    才暴露。要换配置就新建一台，不在 PATCH 里放暗道。

    传了不同值会返回 409；传相同值视为无变化，静默通过（幂等）。
    """

    alias: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, max_length=128)
    # 下面两个字段保留是为了让「传了但值相同」这种幂等调用不报 422；值不同则 409。
    device_type: Optional[str] = Field(default=None, min_length=1, max_length=255)
    runtime: Optional[str] = Field(default=None, min_length=1, max_length=255)
    config_json: Optional[Dict[str, Any]] = None


class IosSimVmDispatchReq(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
