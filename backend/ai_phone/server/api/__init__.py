"""REST API 路由器：按资源维度拆文件，统一在 :func:`include_routers` 注入。"""
from fastapi import APIRouter, FastAPI

from . import (
    analytics,
    agents,
    cases,
    config,
    device_aliases,
    devices,
    files,
    runs,
    submissions,
)
from ..app_install import router as app_install_router
from ..android_vm.api import catalog_router as android_vm_catalog_router
from ..android_vm.api import router as android_vm_instances_router
from ..device_config import router as device_config_router
from ..submissions.public_routes import router as public_submissions_router

android_vm_router = APIRouter()
android_vm_router.include_router(android_vm_instances_router)
android_vm_router.include_router(android_vm_catalog_router)


def include_routers(app: FastAPI) -> None:
    app.include_router(devices.router)            # /api/devices（含匿名 /available）
    app.include_router(agents.router)             # /api/agents（在线 Agent 只读快照）
    app.include_router(cases.router)
    app.include_router(runs.router)
    app.include_router(files.router)
    app.include_router(config.router)             # /api/config（前端功能开关快照）
    app.include_router(submissions.router)        # /api/internal/submissions（Bearer）
    app.include_router(device_aliases.router)     # /api/internal/device-aliases（Bearer）
    app.include_router(analytics.router)          # /api/internal/analytics（Bearer）
    app.include_router(device_config_router)      # /api/device-wake-policies（设备 wake 策略）
    app.include_router(app_install_router)        # /api/app-install（应用包上传与分发安装）
    app.include_router(android_vm_router)         # /api/internal/vm/instances（Android 虚拟机）
    app.include_router(public_submissions_router)  # /api/submissions（匿名，对外）
