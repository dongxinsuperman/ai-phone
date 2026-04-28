"""设备别名模块：serial ↔ 友好名一对一映射。

对外契约（v1.5）：调用 ``/api/submissions`` 传 ``deviceAlias`` 时：

- 别名必须命中本模块的表 —— 未命中整批 400 ``unknown_device_alias``
- 别名对应 serial 若在 ``devices`` 表里有 platform 记录，必须与 item.platform
  一致 —— 不一致整批 400 ``device_alias_platform_mismatch``
  （"先绑后现"：serial 尚未上线时 platform 未知，此校验放过）
- 不传 alias 时照常走"池子随便挑"

模块边界：

- :mod:`.store` — 纯数据访问（CRUD + 查别名 / 查 serial）
- :mod:`.service` — 上层调用（整批校验 / 冲突检查）
"""
from .service import (
    AliasError,
    AliasPlatformMismatchError,
    UnknownAliasError,
    get_serial_by_alias,
    validate_aliases,
)
from .store import (
    create_or_update_alias,
    delete_alias,
    get_alias_by_alias,
    get_alias_by_serial,
    list_aliases,
)

__all__ = [
    "AliasError",
    "AliasPlatformMismatchError",
    "UnknownAliasError",
    "create_or_update_alias",
    "delete_alias",
    "get_alias_by_alias",
    "get_alias_by_serial",
    "get_serial_by_alias",
    "list_aliases",
    "validate_aliases",
]
