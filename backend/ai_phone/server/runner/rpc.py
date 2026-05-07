"""DriverRpcWaiter：Server 大脑架构的跨进程 driver_command/driver_result 撮合。

关系图：

::

    [VLMRunner thread]                    [Server main event loop]
        RemoteDriver.click()
            │
            └─ run_coroutine_threadsafe ──▶ DriverRpcWaiter.register(msg_id) ─▶ Future
                                              │                                  ▲
            ◀─ Future.result(timeout) ────────┤                                  │
                                              ▼                                  │
                                          send_to_agent(driver_command)          │
                                              ⋯ 网络往返 ⋯                         │
                              [agent_ws] 收 driver_result ─▶ waiter.resolve(msg_id)

设计要点
--------

- **不在 Waiter 里做 timeout**：调用方（RemoteDriver）用 ``Future.result(timeout)``
  自己看死线，超时后调 :meth:`DriverRpcWaiter.discard` 把 entry 清掉。
  这样 Waiter 只负责 ``msg_id ↔ Future`` 的注册 / 解析 / 取消，不引内部定时器。
- **批量按 run_id 取消**：Run 终止时一句 ``cancel_run(run_id, ...)`` 把所有
  在飞的 RPC 全部 set_exception，避免 Future 残留。
- **resolve 容忍未知 msg_id**：Agent 重连可能补发已经超时丢弃的 driver_result，
  这种情况只记一行 warn 不抛错（防御性）。
- **不引背压 / 拒绝**：见方案 6.10.6 决策。

异常族
------

所有 RPC 失败都映射到 :class:`RemoteDriverError` 的子类，调用方只接它就够：

- :class:`RemoteDriverModelError`        — 一级桶 ``model``
- :class:`RemoteDriverDeviceError`       — 一级桶 ``device``
- :class:`RemoteDriverNetworkError`      — 一级桶 ``network``（含 RPC 超时 / 发送失败）
- :class:`RemoteDriverAgentOfflineError` — 一级桶 ``agent_offline``

每个异常都自带 ``error_class`` / ``message`` / ``traceback`` / ``message_id``，
方便 ServerRunEmitter 直接落 ``run_logs.error_class`` / ``run_logs.error_category``。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from loguru import logger

from ai_phone.shared.protocol import DriverErrorCategory, DriverErrorPayload


# ---------------------------------------------------------------------------
# 异常族
# ---------------------------------------------------------------------------
class RemoteDriverError(RuntimeError):
    """RemoteDriver 所有跨进程错误的基类。

    构造参数都对应 :data:`DriverErrorPayload`，方便从 driver_result.error 直接构造。
    """

    category: DriverErrorCategory = "device"

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "",
        traceback: str = "",
        message_id: str = "",
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.error_class: str = error_class or self.__class__.__name__
        self.traceback: str = traceback
        self.message_id: str = message_id

    def to_payload(self) -> Dict[str, Any]:
        """落库 / 日志友好的 dict 形态。"""
        return {
            "category": self.category,
            "error_class": self.error_class,
            "message": self.message,
            "message_id": self.message_id,
            # traceback 可能很长，落 dict 时截断；完整内容仍留在 self.traceback
            "traceback": self.traceback[:2000] if self.traceback else "",
        }


class RemoteDriverModelError(RemoteDriverError):
    category = "model"


class RemoteDriverDeviceError(RemoteDriverError):
    category = "device"


class RemoteDriverNetworkError(RemoteDriverError):
    category = "network"


class RemoteDriverAgentOfflineError(RemoteDriverError):
    category = "agent_offline"


_CATEGORY_TO_EXC: Dict[str, type[RemoteDriverError]] = {
    "model": RemoteDriverModelError,
    "device": RemoteDriverDeviceError,
    "network": RemoteDriverNetworkError,
    "agent_offline": RemoteDriverAgentOfflineError,
}


def build_error_from_payload(
    payload: Optional[DriverErrorPayload], *, message_id: str = ""
) -> RemoteDriverError:
    """从 ``driver_result.error`` 构造对应的 RemoteDriverError 子类实例。

    payload 缺失 / 字段不全时退回 ``RemoteDriverDeviceError`` + 'UnknownError'。
    """
    payload = payload or {}
    category = str(payload.get("category", "device"))
    exc_cls = _CATEGORY_TO_EXC.get(category, RemoteDriverDeviceError)
    return exc_cls(
        message=str(payload.get("message", "") or "(no message)"),
        error_class=str(payload.get("error_class", "") or "UnknownError"),
        traceback=str(payload.get("traceback", "") or ""),
        message_id=message_id,
    )


# ---------------------------------------------------------------------------
# DriverRpcWaiter
# ---------------------------------------------------------------------------
class _WaiterEntry:
    """一次在飞 RPC 的注册记录。"""

    __slots__ = ("future", "run_id", "method", "registered_at")

    def __init__(
        self,
        future: "asyncio.Future[Any]",
        run_id: str,
        method: str,
        registered_at: float,
    ) -> None:
        self.future = future
        self.run_id = run_id
        self.method = method
        self.registered_at = registered_at


class DriverRpcWaiter:
    """msg_id → Future 撮合器（线程安全的入口仅 :meth:`resolve`）。

    必须运行在 Server 主 event loop 上（FastAPI 的 loop），所有方法都假定被
    那个 loop 调用。从其他线程（VLMRunner 工作线程）发起的注册请求要先经过
    ``loop.call_soon_threadsafe`` 或 ``run_coroutine_threadsafe`` 转回主 loop。
    """

    def __init__(self, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop = loop  # 仅用于断言；None 表示懒绑定
        self._entries: Dict[str, _WaiterEntry] = {}

    # -- 生命周期 ----------------------------------------------------------
    @property
    def in_flight(self) -> int:
        return len(self._entries)

    def in_flight_for_run(self, run_id: str) -> int:
        return sum(1 for e in self._entries.values() if e.run_id == run_id)

    # -- 注册 / 清理 -------------------------------------------------------
    def register(
        self, message_id: str, *, run_id: str, method: str
    ) -> "asyncio.Future[Any]":
        """登记一个等待者；调用方拿 future 后自己 wait_for / future.result(timeout)。"""
        if message_id in self._entries:
            raise ValueError(f"重复注册 message_id={message_id}")
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Any]" = loop.create_future()
        self._entries[message_id] = _WaiterEntry(
            future=future,
            run_id=run_id,
            method=method,
            registered_at=loop.time(),
        )
        return future

    def discard(self, message_id: str) -> None:
        """调用方主动清理（如自己已经 timeout）。

        清理 entry 的同时取消未完成的 Future，唤醒仍在 await 的
        ``_send_and_wait`` 协程。迟到的 driver_result 会因为 entry 不存在
        被 resolve() 丢弃并记录 warn。
        """
        entry = self._entries.pop(message_id, None)
        if entry is not None and not entry.future.done():
            entry.future.cancel()

    # -- 解析 / 取消 -------------------------------------------------------
    def resolve(self, driver_result: Dict[str, Any]) -> bool:
        """把 ``driver_result`` 派发给对应的 Future。

        返回 True 表示成功匹配；False 表示 msg_id 已不存在（已超时清理 / 重复
        投递），调用方按 warn 处理。
        """
        message_id = driver_result.get("message_id", "")
        if not message_id:
            logger.warning("driver_result 缺 message_id，丢弃：{}", driver_result)
            return False
        entry = self._entries.pop(message_id, None)
        if entry is None:
            logger.warning(
                "driver_result 命中已过期 entry message_id={}（可能 RPC 已超时被清掉）",
                message_id,
            )
            return False
        if entry.future.done():
            logger.warning("driver_result 命中的 future 已完成 message_id={}", message_id)
            return False

        if driver_result.get("ok"):
            entry.future.set_result(driver_result)
        else:
            err = build_error_from_payload(
                driver_result.get("error"), message_id=message_id
            )
            entry.future.set_exception(err)
        return True

    def fail(self, message_id: str, exc: BaseException) -> bool:
        """主动让某条 RPC 失败（用于 Server 侧明确知道发送失败 / Agent 离线）。"""
        entry = self._entries.pop(message_id, None)
        if entry is None or entry.future.done():
            return False
        entry.future.set_exception(exc)
        return True

    def cancel_run(self, run_id: str, *, reason: str = "run cancelled") -> int:
        """批量取消某个 run 上的所有在飞 RPC，返回被取消的条数。

        每条被取消的 RPC 都会抛 :class:`RemoteDriverNetworkError`（语义上属于
        "Server 端主动放弃等待"，归到 network 桶；如果 Run 取消是因为 Agent
        离线，外层会另外把 Run 标成 agent_offline，不冲突）。
        """
        n = 0
        for msg_id, entry in list(self._entries.items()):
            if entry.run_id != run_id:
                continue
            if not entry.future.done():
                entry.future.set_exception(
                    RemoteDriverNetworkError(
                        f"{reason} (method={entry.method})",
                        error_class="RpcCancelled",
                        message_id=msg_id,
                    )
                )
                n += 1
            self._entries.pop(msg_id, None)
        return n

    def cancel_all(self, *, reason: str = "server shutting down") -> int:
        """清场（用于 Server shutdown）。"""
        n = 0
        for msg_id, entry in list(self._entries.items()):
            if not entry.future.done():
                entry.future.set_exception(
                    RemoteDriverNetworkError(
                        f"{reason} (method={entry.method})",
                        error_class="RpcCancelled",
                        message_id=msg_id,
                    )
                )
                n += 1
        self._entries.clear()
        return n


__all__ = [
    "DriverRpcWaiter",
    "RemoteDriverError",
    "RemoteDriverModelError",
    "RemoteDriverDeviceError",
    "RemoteDriverNetworkError",
    "RemoteDriverAgentOfflineError",
    "build_error_from_payload",
]
