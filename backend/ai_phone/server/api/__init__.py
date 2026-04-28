"""REST API 路由器：按资源维度拆文件，统一在 :func:`include_routers` 注入。"""
from fastapi import FastAPI

from . import analytics, cases, device_aliases, devices, files, runs, submissions
from ..submissions.public_routes import router as public_submissions_router


def include_routers(app: FastAPI) -> None:
    app.include_router(devices.router)            # /api/devices（含匿名 /available）
    app.include_router(cases.router)
    app.include_router(runs.router)
    app.include_router(files.router)
    app.include_router(submissions.router)        # /api/internal/submissions（Bearer）
    app.include_router(device_aliases.router)     # /api/internal/device-aliases（Bearer）
    app.include_router(analytics.router)          # /api/internal/analytics（Bearer）
    app.include_router(public_submissions_router)  # /api/submissions（匿名，对外）
