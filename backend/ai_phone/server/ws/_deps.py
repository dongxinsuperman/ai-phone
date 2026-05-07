"""WS 用的依赖：从 app.state 取 hub，请求相关的也走 Depends。"""
from __future__ import annotations

from fastapi import WebSocket

from ..hub import Hub
from ..lockstore import DeviceLockStore
from ..runner.rpc import DriverRpcWaiter
from ..runner.service import ServerRunnerService


def get_hub(ws: WebSocket) -> Hub:
    hub = getattr(ws.app.state, "hub", None)
    if hub is None:
        hub = Hub()
        ws.app.state.hub = hub
    return hub


def get_lock_store(ws: WebSocket) -> DeviceLockStore:
    store = getattr(ws.app.state, "lock_store", None)
    if store is None:
        store = DeviceLockStore()
        ws.app.state.lock_store = store
    return store


def get_driver_rpc_waiter(ws: WebSocket) -> DriverRpcWaiter:
    waiter = getattr(ws.app.state, "driver_rpc_waiter", None)
    if not isinstance(waiter, DriverRpcWaiter):
        waiter = DriverRpcWaiter()
        ws.app.state.driver_rpc_waiter = waiter
    return waiter


def get_server_runner_service(ws: WebSocket) -> ServerRunnerService | None:
    svc = getattr(ws.app.state, "server_runner_service", None)
    return svc if isinstance(svc, ServerRunnerService) else None
