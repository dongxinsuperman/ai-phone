"""Run against an isolated PostgreSQL database, never the application database.

AI_PHONE_TEST_POSTGRES_URL must name a disposable DB: this module creates/drops
the V3 table there. The normal suite skips it when the URL is absent.
"""
import asyncio
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_phone.server.models import VlmTrajectoryCacheV3
from ai_phone.server.trajectory_cache.repository import _upsert_v3


@pytest.mark.skipif(not os.getenv("AI_PHONE_TEST_POSTGRES_URL"), reason="requires isolated PostgreSQL")
@pytest.mark.asyncio
async def test_postgres_parallel_first_insert_and_update():
    engine = create_async_engine(os.environ["AI_PHONE_TEST_POSTGRES_URL"], pool_size=12, max_overflow=0)
    sql = []
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, context, executemany: sql.append(statement))
    writers = 12
    arrivals = 0
    ready = asyncio.Event()

    class RacingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal arrivals
            result = await super().execute(statement, *args, **kwargs)
            if not getattr(self, "first_lookup_done", False):
                self.first_lookup_done = True
                arrivals += 1
                if arrivals == writers:
                    ready.set()
                # All writers see the missing row before any insertion.
                await asyncio.wait_for(ready.wait(), timeout=15)
            return result

    async def write(factory, number):
        return await _upsert_v3(
            factory, {"device_code": "test", "platform": "android", "source_run_id": str(number),
                      "actions": [{"type": "press_home", "index": number}], "meta": {"writer": number}},
            cache_key="parallel-test", normalized_goal="test", semantic_hash="test",
            now=datetime.now(timezone.utc),
        )

    created = False
    try:
        async with engine.begin() as conn:
            await conn.run_sync(VlmTrajectoryCacheV3.__table__.create)
        created = True
        racing = async_sessionmaker(engine, class_=RacingSession, expire_on_commit=False)
        assert set(await asyncio.wait_for(
            asyncio.gather(*(write(racing, i) for i in range(writers))), timeout=30,
        )) == {"parallel-test"}
        normal = async_sessionmaker(engine, expire_on_commit=False)
        async with normal() as session:
            rows = (await session.execute(select(VlmTrajectoryCacheV3))).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert int(row.source_run_id) == row.actions_json[0]["index"] == row.meta_json["writer"]
        await write(normal, 99)
        async with normal() as session:
            row = (await session.execute(select(VlmTrajectoryCacheV3))).scalar_one()
            assert row.source_run_id == "99" and row.meta_json == {"writer": 99}
        assert any("ROLLBACK TO SAVEPOINT" in s for s in sql)
        assert any("FOR UPDATE" in s for s in sql)
        assert not any("ON CONFLICT" in s for s in sql)
    finally:
        if created:
            async with engine.begin() as conn:
                await conn.run_sync(VlmTrajectoryCacheV3.__table__.drop)
        await engine.dispose()
