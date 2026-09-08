"""同步 App 卸载请求与 Agent 回执的进程内 rendezvous。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ai_phone.shared import protocol as P

from .hub import Hub


class AppUninstallDispatchError(RuntimeError):
    """卸载消息无法发送到设备所属 Agent。"""


class AppUninstallTimeoutError(TimeoutError):
    """等待 Agent 卸载结果超时。"""


@dataclass
class _PendingUninstall:
    serial: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Dict[str, Any]] = None


class AppUninstallWaiter:
    """用 request_id 把同步 HTTP 请求与异步 Agent WebSocket 回执关联起来。"""

    def __init__(self) -> None:
        self._pending: Dict[str, _PendingUninstall] = {}

    async def request(
        self,
        *,
        hub: Hub,
        request_id: str,
        serial: str,
        platform: str,
        package_name: str,
        timeout_sec: float,
        wait_timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        response_timeout = wait_timeout_sec or timeout_sec
        state = _PendingUninstall(serial=serial)
        self._pending[request_id] = state
        try:
            sent = await hub.send_to_serial(
                serial,
                {
                    "type": P.MSG_APP_UNINSTALL_START,
                    "request_id": request_id,
                    "serial": serial,
                    "platform": platform,
                    "package_name": package_name,
                    "timeout_sec": int(timeout_sec),
                },
            )
            if not sent:
                raise AppUninstallDispatchError("发送给设备所属 Agent 失败")
            try:
                await asyncio.wait_for(state.event.wait(), timeout=response_timeout)
            except asyncio.TimeoutError as exc:
                raise AppUninstallTimeoutError(
                    f"等待 Agent 卸载结果超过 {response_timeout:g} 秒"
                ) from exc
            if state.result is None:
                raise AppUninstallTimeoutError("Agent 卸载结果为空")
            return dict(state.result)
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, msg: Dict[str, Any]) -> bool:
        request_id = str(msg.get("request_id") or "")
        state = self._pending.get(request_id)
        if state is None:
            return False
        if str(msg.get("serial") or "") != state.serial:
            return False
        state.result = dict(msg)
        state.event.set()
        return True

    def reset_for_tests(self) -> None:
        self._pending.clear()


_WAITER = AppUninstallWaiter()


def get_app_uninstall_waiter() -> AppUninstallWaiter:
    return _WAITER


__all__ = [
    "AppUninstallDispatchError",
    "AppUninstallTimeoutError",
    "AppUninstallWaiter",
    "get_app_uninstall_waiter",
]
