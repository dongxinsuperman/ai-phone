"""FastAPI 应用工厂。

生命周期：
- startup  → init engine、create_all、注入 LockStore 和 Hub 到 app.state、mount /files
- shutdown → dispose engine
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from ai_phone import __version__
from ai_phone.config import get_settings

from sqlalchemy import delete

from .api import include_routers
from .app_install import AppInstallTimeoutScanner
from .db import dispose_engine, get_session_factory, init_db, init_engine
from .hub import Hub
from .lockstore import DeviceLockStore
from .models import Device
from .runner.dispatch import RunDispatchService
from .scheduler import SubmissionScheduler, set_scheduler
from .storage import mount_static
from .submissions import make_publisher
from .ws import include_ws


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_engine()
    try:
        await init_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据库初始化失败（通常是 PG 未启动）：{}", exc)

    # 设备列表是实时视图，server 重启后清空——等 agent hello 重新上报。
    # 没有 agent 在线的"幽灵设备"行没有任何意义（既不能控制也不能镜像）。
    try:
        factory = get_session_factory()
        async with factory() as s:
            res = await s.execute(delete(Device))
            await s.commit()
            if res.rowcount:
                logger.info("启动清理：删除残留 device 行 {} 条", res.rowcount)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动清理 device 表失败（忽略）：{}", exc)

    # 虚拟机运行态同样是"重启后待重新认领"的：把非 draft 的 VM 归零为 agent_offline，
    # 绑定关系保留。必须在开始接收 Agent 上报【之前】做（此刻还没 Agent 能连上）——
    # 否则会与认领竞态。之后 Agent 重连各自认领回真实态；永不回来的就停在 agent_offline。
    try:
        from .android_vm.service import reset_vm_states_on_startup

        factory = get_session_factory()
        async with factory() as s:
            n = await reset_vm_states_on_startup(s)
            await s.commit()
            if n:
                logger.info("启动重置：{} 台虚拟机运行态归零为 agent_offline，待 Agent 重新认领", n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动重置虚拟机状态失败（忽略）：{}", exc)

    app.state.lock_store = DeviceLockStore()
    app.state.hub = Hub()
    app.state.run_dispatch_service = RunDispatchService(
        hub=app.state.hub,
        session_factory=get_session_factory(),
    )

    # v1 第 2 梯队：启动 SubmissionScheduler。调度器事件驱动 + 2s 兜底 tick；
    # 停止流程放在 finally 里，保证 shutdown 时 drain_loop/timeout_loop 都收掉。
    # 第 3 梯队挂广播 publisher：按 AI_PHONE_BROADCAST_BACKEND 选 stdout/kafka；
    # 广播只发终态，scheduler 不依赖 publisher 存活。
    publisher = make_publisher(settings)
    logger.info("[broadcast] publisher={} backend={}", publisher.name, settings.broadcast_backend)
    scheduler = SubmissionScheduler(
        hub=app.state.hub,
        lock_store=app.state.lock_store,
        session_factory=get_session_factory(),
        publisher=publisher,
        dispatch_service=app.state.run_dispatch_service,
    )
    await scheduler.start()
    app.state.scheduler = scheduler
    app.state.publisher = publisher
    set_scheduler(scheduler)

    app_install_scanner = AppInstallTimeoutScanner(get_session_factory())
    await app_install_scanner.start()
    app.state.app_install_scanner = app_install_scanner

    logger.info("ai-phone server 启动完毕 | env={}", settings.env)
    try:
        yield
    finally:
        try:
            await app_install_scanner.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关停 app install scanner 异常（忽略）：{}", exc)
        try:
            await scheduler.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关停 scheduler 异常（忽略）：{}", exc)
        try:
            await publisher.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关停 publisher 异常（忽略）：{}", exc)
        set_scheduler(None)
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="ai-phone server",
        version=__version__,
        description="VLM 视觉自动化平台 - Server 端",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def _healthz() -> dict:
        return {"status": "ok", "version": __version__, "env": settings.env}

    app.add_api_route("/healthz", _healthz, methods=["GET"], tags=["meta"])
    app.add_api_route("/api/healthz", _healthz, methods=["GET"], tags=["meta"])

    include_routers(app)
    include_ws(app)
    mount_static(app)
    return app


app = create_app()
