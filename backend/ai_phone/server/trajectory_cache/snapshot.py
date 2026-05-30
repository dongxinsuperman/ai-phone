"""命中缓存 → start_run 下发快照（Distributed Agent Brain · M4 片1）。

Server 在派发前按 run 的 effective_cache_mode 查命中；命中则把缓存回放载荷（actions /
state_landmarks / ephemeral_meta 已含证据图 URL）组装成 CacheSnapshot 随 start_run 一次性
下发，Agent 据此自取预取、本地回放。命中查询 + 成品存储留 Server，回放 + 归档在 Agent。

三套表 to_dict 结构不同（已核对）：
- V1 / V2：内容嵌在 ``trajectory_json``（actions / state_landmarks / source_completion）。
- V3：扁平 ``actions`` + ``source_completion`` + ``meta``，无 state_landmarks。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger

from .mode import CACHE_MODE_OFF
from .service import (
    get_active_trajectory_cache_v1,
    get_active_trajectory_cache_v2,
)
from .v3_service import get_active_trajectory_cache_v3

_LOOKUP = {
    "v1": get_active_trajectory_cache_v1,
    "v2": get_active_trajectory_cache_v2,
    "v3": get_active_trajectory_cache_v3,
}


def _payload_of(hit: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """抹平三套 to_dict 差异，取出 actions / state_landmarks / source_completion / meta。"""
    if mode in ("v1", "v2"):
        tj = hit.get("trajectory_json") or {}
        return {
            "actions": tj.get("actions") or [],
            "state_landmarks": tj.get("state_landmarks") or [],
            "source_completion": tj.get("source_completion") or {},
            "meta": {},
        }
    # v3：扁平
    return {
        "actions": hit.get("actions") or [],
        "state_landmarks": [],
        "source_completion": hit.get("source_completion") or {},
        "meta": hit.get("meta") or {},
    }


async def build_cache_snapshot(
    session_factory,
    *,
    device_serial: str,
    goal: str,
    effective_cache_mode: str,
) -> Optional[Dict[str, Any]]:
    """命中则返回随 start_run 下发的 CacheSnapshot；off / 未命中返回 None。"""
    mode = str(effective_cache_mode or CACHE_MODE_OFF).strip().lower()
    lookup = _LOOKUP.get(mode)
    if lookup is None or not (device_serial and goal):
        return None
    hit = await lookup(session_factory, device_code=device_serial, run_semantic_text=goal)
    if not hit:
        return None

    payload = _payload_of(hit, mode)
    snapshot: Dict[str, Any] = {
        "cache_mode": mode,
        "schema_version": hit.get("schema_version"),
        "cache_key": hit.get("cache_key"),
        "actions": payload["actions"],
        "state_landmarks": payload["state_landmarks"],
        "source_completion": payload["source_completion"],
        "meta": payload["meta"],
        # V3 回放 locator/rescue 按录制时的主 VLM backend 选模型与 prompt；缺失则
        # Agent 用本机 settings.vlm_backend 兜底（单 backend 部署等价）。
        "source_vlm_backend": str(hit.get("source_vlm_backend") or ""),
    }
    logger.info(
        "缓存命中下发 mode={} device={} actions={} landmarks={}",
        mode, device_serial, len(snapshot["actions"]), len(snapshot["state_landmarks"]),
    )
    return snapshot


__all__ = ["build_cache_snapshot"]
