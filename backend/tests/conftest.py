"""全局 pytest fixtures。

- 对于 Server API 测试，给每个测试起一个独立的 aiosqlite in-memory 库，
  避免测试间串数据，也不依赖本机 PG。
- 通过替换 ``ai_phone.server.db`` 的全局 engine 注入测试库。
- VLMRunner 的"断言系统"会在 finished() 后再调一次 VLM；测试里默认旁路掉，
  否则会真打外网（.env 里通常配着真实 vlm_api_key），让所有 finished 测试
  都把行为改成"VLM 复核"。这不是单元测试该负责的事，统一在 fixture 里
  替换为"直接 SKIP，回退采纳主 VLM 结果"，老测试零改动。
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ai_phone.agent.runner import vlm_loop as _vlm_loop_module
from ai_phone.server import db as db_module
from ai_phone.server import lockstore as lockstore_module
from ai_phone.server.api import include_routers
from ai_phone.server.db import Base
from ai_phone.server.lockstore import DeviceLockStore


@pytest.fixture(autouse=True)
def _bypass_finished_assertion_system(monkeypatch):
    """单测里把断言系统短路成 SKIP，避免真发 HTTP。

    生产路径完全保留：``_verify_finished_assertion`` 在配置缺失/调用失败时
    本身就走 SKIP→回退主 VLM 结果，本 fixture 等价于强制走这条分支。
    """

    async def _stub(self, **kwargs):  # noqa: ANN001
        return ("SKIP", "test bypass: 断言系统在单测里禁用")

    monkeypatch.setattr(
        _vlm_loop_module.VLMRunner,
        "_verify_finished_assertion",
        _stub,
    )


@pytest_asyncio.fixture
async def _test_engine() -> AsyncIterator[None]:
    """每个测试建一个独立的 aiosqlite 库；结束后清理 engine。"""
    # 独立 DB 名保证测试间隔离
    import uuid

    db_url = f"sqlite+aiosqlite:///file:memdb-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    # 重置全局
    await db_module.dispose_engine()
    db_module.init_engine(db_url=db_url)

    from ai_phone.server import models  # noqa: F401  # 注册模型

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        await db_module.dispose_engine()
        lockstore_module.reset_default_lock_store()


@pytest_asyncio.fixture
async def app(_test_engine) -> FastAPI:
    """每个测试新起一个 FastAPI app，避免路由 / 中间件污染。"""
    from fastapi import FastAPI

    a = FastAPI()
    a.state.lock_store = DeviceLockStore()
    include_routers(a)
    return a


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def session(_test_engine):
    """方便需要直接操作 DB 的测试预置数据。"""
    factory = db_module.get_session_factory()
    async with factory() as s:
        yield s
