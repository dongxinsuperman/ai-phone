"""/api/agents：当前在线 Agent 的只读快照。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter

from ..hub import Hub
from ._deps import HubDep

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents(hub: Hub = HubDep) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).timestamp()
    out: List[Dict[str, Any]] = []
    for item in hub.snapshot().get("agents", []):
        connected_at = _iso_from_ts(item.get("connected_at"))
        last_seen_at = _iso_from_ts(item.get("last_seen_at"))
        last_seen_ts = float(item.get("last_seen_at") or 0.0)
        out.append(
            {
                "agent_id": item.get("agent_id"),
                "agent_name": item.get("agent_name"),
                "host_os": item.get("host_os"),
                "connected_at": connected_at,
                "last_seen_at": last_seen_at,
                "last_seen_age_ms": max(0, int((now - last_seen_ts) * 1000))
                if last_seen_ts
                else None,
                "serials": item.get("serials") or [],
                "run_ids": item.get("run_ids") or [],
                "device_count": len(item.get("serials") or []),
                "running_count": len(item.get("run_ids") or []),
            }
        )
    out.sort(key=lambda x: str(x.get("agent_name") or x.get("agent_id") or ""))
    return out


def _iso_from_ts(value: Any) -> str | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
