"""REST API 路由器：按资源维度拆文件，统一在 :func:`include_routers` 注入。"""
from fastapi import FastAPI

from . import (
    analytics,
    cases,
    config,
    device_aliases,
    devices,
    files,
    runs,
    server_brain,
    submissions,
)
from ..submissions.public_routes import router as public_submissions_router


def include_routers(app: FastAPI) -> None:
    app.include_router(devices.router)            # /api/devices（含匿名 /available）
    app.include_router(cases.router)
    app.include_router(runs.router)
    app.include_router(files.router)
    app.include_router(config.router)             # /api/config（前端功能开关快照）
    app.include_router(submissions.router)        # /api/internal/submissions（Bearer）
    app.include_router(device_aliases.router)     # /api/internal/device-aliases（Bearer）
    app.include_router(analytics.router)          # /api/internal/analytics（Bearer）
    app.include_router(server_brain.router)       # /api/internal/server-brain（Bearer，next 分支专属）
    app.include_router(public_submissions_router)  # /api/submissions（匿名，对外）
