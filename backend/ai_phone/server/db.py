"""异步 SQLAlchemy 2.x engine + session 管理。

设计：
- 运行时用 asyncpg 连 PostgreSQL（生产 & 本地都是 PG）
- 单测用 aiosqlite 起内存库，走同一套 Base.metadata
- 模型避免使用 PG 专属类型（用 JSON 而非 JSONB，用 String 存 UUID），保证两套引擎
  都能 `Base.metadata.create_all()` 一次起表

M1 暂不引 Alembic，依赖 ``init_db()`` 在应用启动时 ``create_all`` 建表；M4 收尾
阶段再切到正式迁移链路。
"""
from __future__ import annotations

from typing import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ai_phone.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类，子类定义见 :mod:`ai_phone.server.models`。"""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(db_url: str | None = None, echo: bool = False) -> AsyncEngine:
    """按 URL 新建或复用全局 engine。传 ``db_url`` 可覆盖配置（测试用）。"""
    global _engine, _session_factory
    if _engine is not None and db_url is None:
        return _engine

    effective = db_url or get_settings().db_url
    logger.info("初始化数据库 engine: {}", _mask_dsn(effective))

    kwargs: dict = {"echo": echo, "future": True}
    is_sqlite_mem = effective.startswith("sqlite+aiosqlite://") and (
        ":memory:" in effective or "mode=memory" in effective
    )
    if is_sqlite_mem:
        # sqlite 内存库：必须走 StaticPool 才能让多个 session 共享同一份数据
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False, "uri": True}
    else:
        kwargs["pool_pre_ping"] = True

    _engine = create_async_engine(effective, **kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends 入口：每个请求一个新 AsyncSession。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def init_db() -> None:
    """开发环境自动建表。生产应走 Alembic，但 M1 先不引入。

    注意：``Base.metadata.create_all`` 只会"建缺的表"，**不会**给已有表加新列。
    给 ORM 新增字段后若启动报 ``column ... does not exist``，按本仓库约定走
    手工 SQL：先在 PG 里 ``ALTER TABLE``，再重启服务（model 侧由开发补齐）。
    当前已登记的手工 SQL 见 ``架构设计.md`` 的"DB 手工迁移清单"。
    """
    engine = get_engine()
    # 延迟到此处才 import，避免循环依赖（models 依赖 Base，Base 在本文件）
    from ai_phone.server import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库建表完成")


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("数据库 engine 已释放")
    _engine = None
    _session_factory = None


def _mask_dsn(dsn: str) -> str:
    """隐藏密码后再输出到日志，例如 postgresql+asyncpg://user:***@host/db。"""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds_host = rest.split("@", 1)
    if len(creds_host) != 2 or ":" not in creds_host[0]:
        return dsn
    user = creds_host[0].split(":", 1)[0]
    return f"{scheme}://{user}:***@{creds_host[1]}"
