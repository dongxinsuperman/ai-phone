"""WebSocket 端点包。"""
from fastapi import FastAPI

from . import agent_ws, browser_ws


def include_ws(app: FastAPI) -> None:
    app.include_router(agent_ws.router)
    app.include_router(browser_ws.router)
