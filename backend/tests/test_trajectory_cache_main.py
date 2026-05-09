import pytest
from sqlalchemy import select

from ai_phone.server import db as db_module
from ai_phone.server.models import Device, Run, RunLog, VlmTrajectoryCache
from ai_phone.server.trajectory_cache import (
    get_dispatch_trajectory_cache,
    save_trajectory_cache_after_success,
)


@pytest.mark.asyncio
async def test_agent_brain_success_run_saves_cache_from_run_logs(_test_engine, session):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-cache-main-ok",
        device_serial="D1",
        goal="点击我的",
        status="success",
        engine="vlm",
        token_summary={"vlm_backend": "doubao_responses"},
    )
    session.add(run)
    session.add_all(
        [
            RunLog(run_id=run.id, step=1, level=1, title="思考", content="点击底部我的 tab"),
            RunLog(
                run_id=run.id,
                step=1,
                level=1,
                title="动作",
                content="click(point='<point>900 950</point>')",
            ),
        ]
    )
    await session.commit()

    cache_key = await save_trajectory_cache_after_success(
        db_module.get_session_factory(),
        run.id,
    )

    assert cache_key
    row = (
        await session.execute(
            select(VlmTrajectoryCache).where(VlmTrajectoryCache.cache_key == cache_key)
        )
    ).scalars().one()
    actions = row.trajectory_json["actions"]
    assert actions[0]["type"] == "click"
    assert actions[0]["source"] == "run_log"
    assert actions[0]["point"] == {"x": 900, "y": 1900}
    assert actions[0]["intent"] == "点击我的"
    assert row.trajectory_json["source_vlm_backend"] == "doubao_responses"


@pytest.mark.asyncio
async def test_dispatch_cache_default_disabled_logs_and_falls_through(
    _test_engine,
    session,
):
    session.add(Device(serial="D1", platform="android", screen_width=1000, screen_height=2000))
    run = Run(
        id="run-cache-main-disabled",
        device_serial="D1",
        goal="点击我的",
        status="pending",
        engine="vlm",
    )
    session.add(run)
    await session.commit()

    cache = await get_dispatch_trajectory_cache(
        session,
        run_id=run.id,
        device_code="D1",
        run_semantic_text="点击我的",
    )
    await session.commit()

    assert cache is None
    logs = (
        await session.execute(
            select(RunLog).where(RunLog.run_id == run.id).order_by(RunLog.id)
        )
    ).scalars().all()
    assert logs[-1].title == "轨迹缓存"
    assert "未开启" in logs[-1].content
