"""别名业务规则：整批校验、反查 serial。

准入（scheduler ``submit``）唯一入口是 :func:`validate_aliases`，一次过完成两步：

1. **别名存在性**：所有 ``deviceAlias`` 必须命中 ``device_aliases`` 表
2. **平台联动性**：别名反查到的 serial 若在 ``devices`` 表里有 platform 记录，
   必须与 item 声明的 platform 一致；否则拒绝。
   ——"先绑后现"容忍：serial 暂时不在 devices 表（还没上线过）时 platform 未知，
     不做武断判定，放过；等调度挑选阶段目标设备上线后再按平台自然分流即可。

两种错误分别抛独立子类，准入层据此映射 rejectReason：
``UnknownAliasError → unknown_device_alias``
``AliasPlatformMismatchError → device_alias_platform_mismatch``

调度 ``_pick_device_for_item`` 另外走 :func:`get_serial_by_alias` 拿被 pin 的 serial。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Device, DeviceAlias


class AliasError(RuntimeError):
    """别名业务异常基类。"""


class UnknownAliasError(AliasError):
    """别名不在 ``device_aliases`` 表里。"""


class AliasPlatformMismatchError(AliasError):
    """别名命中，但反查到的 serial 对应设备不是请求声明的 platform。

    典型场景：item ``platform=android`` 却传了一个绑在 iPhone 上的别名。
    这种情况若放行，调度会永远在 android 池里找不到该 serial（它在 ios 池），
    整个 item 一直 queued 直到 submission 3h 硬超时——体感就是"卡死"。
    准入阶段直接拒绝。
    """


async def get_serial_by_alias(session: AsyncSession, alias: str) -> Optional[str]:
    """按别名反查 serial；找不到返回 ``None``。"""
    if not alias:
        return None
    res = await session.execute(
        select(DeviceAlias.serial).where(DeviceAlias.alias == alias)
    )
    row = res.first()
    return row[0] if row else None


async def validate_aliases(
    session: AsyncSession,
    items: Iterable[Tuple[str, str]],
) -> Dict[str, str]:
    """整批校验 ``(alias, expected_platform)`` 列表。

    - 空 alias / None 自动忽略（代表"不指定设备"，调度自由挑）
    - 别名命中性错误 → :class:`UnknownAliasError`（消息含全部未命中别名）
    - 平台不匹配错误 → :class:`AliasPlatformMismatchError`（消息含全部冲突对）
    - 通过时返回 ``{alias: serial}``，供上游可选缓存；不关心返回值也无妨

    "先绑后现"容忍：别名指向的 serial 当前不在 ``devices`` 表里（还没上线过）
    时 platform 未知，不做 mismatch 判定，放过。等 serial 真上线、调度阶段
    再按实际 platform 自然归位。

    空串会被 strip 成 None；重复的 ``(alias, platform)`` 会去重后再查。
    """
    # 清洗 + 去重
    clean: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for raw_alias, raw_platform in items:
        if not raw_alias:
            continue
        alias = raw_alias.strip()
        platform = (raw_platform or "").strip().lower()
        if not alias:
            continue
        key = (alias, platform)
        if key in seen:
            continue
        seen.add(key)
        clean.append(key)

    if not clean:
        return {}

    aliases = sorted({alias for alias, _ in clean})

    # Step 1 · 查 device_aliases，筛未命中
    res = await session.execute(
        select(DeviceAlias.alias, DeviceAlias.serial).where(
            DeviceAlias.alias.in_(aliases)
        )
    )
    alias_to_serial: Dict[str, str] = {row.alias: row.serial for row in res.all()}
    missing = [a for a in aliases if a not in alias_to_serial]
    if missing:
        raise UnknownAliasError(
            "unknown_device_alias: " + ", ".join(sorted(missing))
        )

    # Step 2 · 查 devices，补 platform；不在 devices 表的 serial 保持 None（先绑后现）
    serials = sorted(set(alias_to_serial.values()))
    res = await session.execute(
        select(Device.serial, Device.platform).where(Device.serial.in_(serials))
    )
    serial_to_platform: Dict[str, str] = {
        row.serial: (row.platform or "").strip().lower() for row in res.all()
    }

    mismatches: List[str] = []
    for alias, expected_platform in clean:
        if not expected_platform:
            continue  # 调用方没声明 platform，不校验（当前 scheduler 不会走到这条）
        serial = alias_to_serial[alias]
        actual_platform = serial_to_platform.get(serial)
        if actual_platform is None:
            # 先绑后现：devices 表没这台机器的记录，不做 mismatch 判定
            continue
        if actual_platform != expected_platform:
            mismatches.append(
                f"{alias}(绑定设备 serial={serial} 平台={actual_platform}，"
                f"与 item 声明 platform={expected_platform} 不一致)"
            )
    if mismatches:
        raise AliasPlatformMismatchError(
            "device_alias_platform_mismatch: " + "; ".join(mismatches)
        )

    return alias_to_serial
