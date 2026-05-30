"""M4 片2：Server 薄存储——接收 Agent 回传成品缓存并 upsert（repository）单测。

回放 / 归档下沉 Agent 后，Server 侧只剩"接收成品 → 算 cache_key → 写库"这一薄层。
本测验证该闭环：

- V2/V3 round-trip：回传成品后能被命中查询（``get_active_*``）取回；
- cache_key 由 Server 统一计算（device + goal + schema），与命中查询口径一致；
- 幂等：同 (device, goal) 重复回传只刷新同一行；
- 非法 mode / 缺字段 / 空 actions 一律丢弃返回 None（不写脏数据）。
"""
from __future__ import annotations

import pytest

from ai_phone.server.trajectory_cache.repository import store_trajectory_cache_archive
from ai_phone.server.trajectory_cache.service import get_active_trajectory_cache_v2
from ai_phone.server.trajectory_cache.v3_service import get_active_trajectory_cache_v3


@pytest.mark.asyncio
async def test_v3_archive_roundtrip_then_hit(_test_engine):
    from ai_phone.server.db import get_session_factory

    sf = get_session_factory()
    device, goal = "ARC-V3", "打开微信发消息"
    key = await store_trajectory_cache_archive(
        sf,
        archive={
            "cache_mode": "v3",
            "device_code": device,
            "run_semantic_text": goal,
            "source_run_id": "run-1",
            "platform": "android",
            "actions": [{"type": "click", "plan_intent": "tap search", "action_id": "a1"}],
            "source_completion": {"task_done": True},
            "meta": {"plan": "x"},
            "source_vlm_backend": "doubao_responses",
        },
    )
    assert key  # 写入成功返回 cache_key
    hit = await get_active_trajectory_cache_v3(sf, device_code=device, run_semantic_text=goal)
    assert hit is not None
    assert hit["cache_key"] == key  # Server 算的 key 与命中查询口径一致
    assert len(hit["actions"]) == 1
    assert hit["meta"]["plan"] == "x"


@pytest.mark.asyncio
async def test_v2_archive_roundtrip_then_hit(_test_engine):
    from ai_phone.server.db import get_session_factory

    sf = get_session_factory()
    device, goal = "ARC-V2", "打开设置"
    key = await store_trajectory_cache_archive(
        sf,
        archive={
            "cache_mode": "v2",
            "device_code": device,
            "run_semantic_text": goal,
            "source_run_id": "run-2",
            "trajectory_json": {
                "actions": [{"type": "click", "action_id": "a1", "point": {"x": 5, "y": 6}}],
                "state_landmarks": [{"action_id": "a1", "image_url": "/files/r/a1.jpg"}],
                "source_completion": {"task_done": True},
            },
        },
    )
    assert key
    hit = await get_active_trajectory_cache_v2(sf, device_code=device, run_semantic_text=goal)
    assert hit is not None
    assert hit["cache_key"] == key
    tj = hit["trajectory_json"]
    assert len(tj["actions"]) == 1
    assert len(tj["state_landmarks"]) == 1


@pytest.mark.asyncio
async def test_archive_upsert_is_idempotent(_test_engine):
    from sqlalchemy import func, select

    from ai_phone.server.db import get_session_factory
    from ai_phone.server.models import VlmTrajectoryCacheV3

    sf = get_session_factory()
    device, goal = "ARC-IDEM", "打开相机"
    archive = {
        "cache_mode": "v3",
        "device_code": device,
        "run_semantic_text": goal,
        "actions": [{"type": "click", "action_id": "a1"}],
    }
    k1 = await store_trajectory_cache_archive(sf, archive=dict(archive))
    k2 = await store_trajectory_cache_archive(sf, archive=dict(archive))
    assert k1 == k2  # 同 (device, goal) → 同 cache_key
    async with sf() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(VlmTrajectoryCacheV3)
                .where(VlmTrajectoryCacheV3.cache_key == k1)
            )
        ).scalar_one()
    assert count == 1  # 幂等：只一行


@pytest.mark.asyncio
async def test_archive_invalid_and_empty_return_none(_test_engine):
    from ai_phone.server.db import get_session_factory

    sf = get_session_factory()
    # 非法 mode
    assert await store_trajectory_cache_archive(
        sf,
        archive={"cache_mode": "vX", "device_code": "d", "run_semantic_text": "g", "actions": [{"type": "click"}]},
    ) is None
    # 缺 device_code
    assert await store_trajectory_cache_archive(
        sf,
        archive={"cache_mode": "v3", "run_semantic_text": "g", "actions": [{"type": "click"}]},
    ) is None
    # 空 actions（V3）
    assert await store_trajectory_cache_archive(
        sf,
        archive={"cache_mode": "v3", "device_code": "d", "run_semantic_text": "g", "actions": []},
    ) is None
    # 空 actions（V2 trajectory_json）
    assert await store_trajectory_cache_archive(
        sf,
        archive={"cache_mode": "v2", "device_code": "d", "run_semantic_text": "g", "trajectory_json": {"actions": []}},
    ) is None
