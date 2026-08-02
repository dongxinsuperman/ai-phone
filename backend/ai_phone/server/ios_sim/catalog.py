"""官方机型目录的加载与查询。

目录内容来自仓库 bundle 的 ``official_catalog.json``，由
``scripts/export_ios_sim_catalog.py`` 从 ``xcrun simctl list devicetypes -j``
导出。与鸿蒙 ``harmony_vm/catalog.py`` + ``official_catalog.json`` 同构。

职责边界（方案 §6.5.4.1）：**本模块只回答「有哪些机型、各自支持哪些系统版本」。**
「这台 Agent 装了哪些 runtime」是 Agent 能力探查的事，Server 不猜也不缓存。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


_BUNDLED = Path(__file__).with_name("official_catalog.json")

# 苹果的版本整数编码：major<<16 | minor<<8 | patch。
# 实测校验：1703936→26.0.0、1050880→16.9.0、720896→11.0.0，与官方给的
# *VersionString 逐一吻合（方案 §1.8.2）。
_NO_MAX = 4294967295


class CatalogError(RuntimeError):
    """目录文件缺失或格式非法。"""


@lru_cache(maxsize=1)
def load_bundled_catalog() -> Dict[str, Any]:
    """读取仓库 bundle 的目录快照。缺失或非法直接抛错——目录是硬依赖。"""
    if not _BUNDLED.is_file():
        raise CatalogError(
            f"官方机型目录缺失：{_BUNDLED}。"
            "请运行 `python -m scripts.export_ios_sim_catalog` 生成"
        )
    try:
        payload = json.loads(_BUNDLED.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CatalogError(f"官方机型目录格式非法：{_BUNDLED}（{exc}）") from exc
    device_types = payload.get("device_types")
    if not isinstance(device_types, list) or not device_types:
        raise CatalogError(f"官方机型目录里没有任何机型：{_BUNDLED}")
    return payload


def reset_cache_for_tests() -> None:
    load_bundled_catalog.cache_clear()


def encode_version(version: str) -> int:
    """``"26.0.1"`` → 苹果的版本整数。解析失败返回 0。"""
    parts = (version or "").strip().split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return 0
    return (major << 16) | (minor << 8) | patch


def decode_version(value: int) -> str:
    v = int(value)
    return f"{v >> 16}.{(v >> 8) & 0xFF}.{v & 0xFF}"


def device_types(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    payload = catalog or load_bundled_catalog()
    return list(payload.get("device_types") or [])


def find_device_type(
    identifier: str, catalog: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    for item in device_types(catalog):
        if item.get("identifier") == identifier:
            return item
    return None


def supports_version(device_type: Dict[str, Any], version: str) -> bool:
    """机型是否支持某个系统版本（按官方给的 min/max 区间判断）。

    注意这是 **Server 侧的预校验**，用于在前端就挡掉不可能的组合、少一次无效下发。
    **最终判据仍是 Agent 探查**——那边用 runtime 自带的 ``supportedDeviceTypes``，
    是苹果直给的权威表（方案 §6.5.4）。两者不一致时以 Agent 为准。
    """
    encoded = encode_version(version)
    if encoded <= 0:
        return False
    low = int(device_type.get("min_runtime_version") or 0)
    high = int(device_type.get("max_runtime_version") or _NO_MAX)
    return low <= encoded <= high


def compatible_device_types(
    version: str, catalog: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """列出支持指定系统版本的机型。前端左侧选择器用。"""
    return [dt for dt in device_types(catalog) if supports_version(dt, version)]


def catalog_summary(catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = catalog or load_bundled_catalog()
    dts = device_types(payload)
    families: Dict[str, int] = {}
    for dt in dts:
        family = str(dt.get("product_family") or "unknown")
        families[family] = families.get(family, 0) + 1
    return {
        "xcode_version": str(payload.get("xcode_version") or ""),
        "source": str(payload.get("source") or ""),
        "collected_at": str(payload.get("collected_at") or ""),
        "device_type_count": len(dts),
        "families": families,
    }


async def ensure_catalog_row(session: Any) -> None:
    """把 bundle 的目录写进 ``ios_sim_catalog_snapshots``（整体覆盖）。

    在 DB 初始化时调用。与鸿蒙 ``init_harmony_vm_db`` 写 bundled manifest 同构：
    目录是一份完整快照，刷新即覆盖，不做增量合并。
    """
    from .models import IosSimCatalogSnapshot  # noqa: PLC0415

    payload = load_bundled_catalog()
    summary = catalog_summary(payload)
    row = await session.get(IosSimCatalogSnapshot, "official")
    if row is None:
        row = IosSimCatalogSnapshot(id="official")
        session.add(row)
    row.xcode_version = summary["xcode_version"]
    row.source = summary["source"]
    row.collected_at = summary["collected_at"]
    row.device_type_count = summary["device_type_count"]
    row.payload = payload
    logger.info(
        "iOS 虚拟机官方机型目录已导入：{} 个机型（{}）",
        summary["device_type_count"],
        summary["xcode_version"] or "未知 Xcode 版本",
    )
