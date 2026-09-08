from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Set

from loguru import logger

from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.shared import protocol as P

from .platforms import UNINSTALL_FAILED, uninstall_by_platform

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_TASKS: Set[asyncio.Task] = set()


async def handle_app_uninstall_start(client: AgentWSClient, msg: Dict[str, Any]) -> None:
    """收到卸载请求后后台执行，避免阻塞 Agent WebSocket 收包。"""
    task = asyncio.create_task(
        _run_app_uninstall(client, dict(msg)),
        name=f"app-uninstall-{msg.get('request_id') or msg.get('serial') or 'unknown'}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _run_app_uninstall(client: AgentWSClient, msg: Dict[str, Any]) -> None:
    request_id = str(msg.get("request_id") or "")
    serial = str(msg.get("serial") or "").strip()
    platform = str(msg.get("platform") or "").strip().lower()
    package_name = str(msg.get("package_name") or "").strip()
    timeout_sec = _safe_timeout(msg.get("timeout_sec"))

    success = False
    reason = UNINSTALL_FAILED
    message = ""
    try:
        if not request_id or not serial or not platform:
            reason = "bad_request"
            message = "卸载请求参数缺失"
        elif not _PACKAGE_NAME_RE.fullmatch(package_name):
            reason = "bad_request"
            message = "package_name 格式无效"
        else:
            success, reason, message = await asyncio.wait_for(
                asyncio.to_thread(
                    uninstall_by_platform,
                    platform,
                    serial,
                    package_name,
                    timeout_sec,
                ),
                timeout=max(30, timeout_sec + 5),
            )
    except asyncio.TimeoutError:
        success = False
        reason = "timeout"
        message = f"卸载超过 {timeout_sec}s 未完成"
    except Exception as exc:  # noqa: BLE001
        success = False
        reason = UNINSTALL_FAILED
        message = f"{type(exc).__name__}: {exc}"

    payload = {
        "type": P.MSG_APP_UNINSTALL_RESULT,
        "request_id": request_id,
        "serial": serial,
        "platform": platform,
        "package_name": package_name,
        "success": success,
        "reason": "" if success else reason,
        "message": message or ("卸载成功" if success else "卸载失败"),
    }
    sent = await client.send(payload)
    logger.info(
        "app_uninstall result sent={} request_id={} serial={} success={} reason={}",
        sent,
        request_id,
        serial,
        success,
        payload["reason"],
    )


def _safe_timeout(value: Any) -> int:
    try:
        return max(10, min(60, int(value or 60)))
    except Exception:  # noqa: BLE001
        return 60


__all__ = ["handle_app_uninstall_start"]
