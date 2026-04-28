"""FastAPI Depends 依赖：DB session + LockStore。"""
from __future__ import annotations

from typing import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session_factory
from ..hub import Hub
from ..lockstore import DeviceLockStore, get_default_lock_store


async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """每请求新 Session；提交由 endpoint 显式做。"""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def lock_store(request: Request) -> DeviceLockStore:
    """优先从 app.state 取，方便测试替换；没有时回退全局单例。"""
    store = getattr(request.app.state, "lock_store", None)
    if isinstance(store, DeviceLockStore):
        return store
    return get_default_lock_store()


def hub(request: Request) -> Hub:
    h = getattr(request.app.state, "hub", None)
    if isinstance(h, Hub):
        return h
    h = Hub()
    request.app.state.hub = h
    return h


DBSession = Depends(db_session)
LockStoreDep = Depends(lock_store)
HubDep = Depends(hub)
