"""M4 片1：命中缓存 → start_run 下发快照（build_cache_snapshot）单测。

验证：三套表 to_dict 结构差异被抹平（V1/V2 嵌 trajectory_json、V3 扁平）；命中组装
回放载荷 + 预取图清单；off / 未命中降级返回 None（照常首跑）。
"""
from __future__ import annotations

import pytest

from ai_phone.server.trajectory_cache.service import build_cache_key
from ai_phone.server.trajectory_cache.snapshot import build_cache_snapshot


@pytest.mark.asyncio
async def test_snapshot_v3_hit_flat(_test_engine):
    from ai_phone.server.db import get_session_factory
    from ai_phone.server.models import VlmTrajectoryCacheV3

    device, goal = "S1", "打开微信发消息"
    ck, _norm, sh = build_cache_key(device_code=device, run_semantic_text=goal, schema_version=3)
    sf = get_session_factory()
    async with sf() as s:
        s.add(VlmTrajectoryCacheV3(
            cache_key=ck, device_code=device, run_semantic_hash=sh, run_semantic_text=goal,
            schema_version=3, status="active",
            actions_json=[{"type": "click", "plan_intent": "tap search", "action_id": "a1"}],
            source_completion={"task_done": True},
            meta_json={"plan": "x"},
        ))
        await s.commit()

    snap = await build_cache_snapshot(sf, device_serial=device, goal=goal, effective_cache_mode="v3")
    assert snap is not None
    assert snap["cache_mode"] == "v3"
    assert snap["schema_version"] == 3
    assert len(snap["actions"]) == 1
    assert snap["meta"] == {"plan": "x"}
    assert snap["state_landmarks"] == []  # V3 主路径无路标


@pytest.mark.asyncio
async def test_snapshot_v2_hit_flattens_trajectory_json_with_landmark_url(_test_engine):
    from ai_phone.server.db import get_session_factory
    from ai_phone.server.models import VlmTrajectoryCacheV2

    device, goal = "S2", "打开设置"
    ck, _norm, sh = build_cache_key(device_code=device, run_semantic_text=goal, schema_version=2)
    sf = get_session_factory()
    async with sf() as s:
        s.add(VlmTrajectoryCacheV2(
            cache_key=ck, device_code=device, run_semantic_hash=sh, run_semantic_text=goal,
            schema_version=2, status="active",
            trajectory_json={
                "actions": [{"type": "click", "action_id": "a1", "point": {"x": 5, "y": 6}}],
                "state_landmarks": [
                    {"action_id": "a1", "image_url": "/files/runs/r1/a1.jpg", "image_sha256": "abc"}
                ],
                "source_completion": {"task_done": True},
            },
        ))
        await s.commit()

    snap = await build_cache_snapshot(sf, device_serial=device, goal=goal, effective_cache_mode="v2")
    assert snap is not None
    assert snap["cache_mode"] == "v2"
    assert len(snap["actions"]) == 1  # 从 trajectory_json 抹平取出
    assert len(snap["state_landmarks"]) == 1
    # 证据图 URL 直接在 state_landmarks 里，Agent 据此自取预取（不再下发独立 manifest）
    assert snap["state_landmarks"][0]["image_url"] == "/files/runs/r1/a1.jpg"
    assert "artifacts" not in snap


@pytest.mark.asyncio
async def test_snapshot_off_and_miss_return_none(_test_engine):
    from ai_phone.server.db import get_session_factory

    sf = get_session_factory()
    # off：直接 None（不查缓存）
    assert await build_cache_snapshot(
        sf, device_serial="S1", goal="g", effective_cache_mode="off"
    ) is None
    # 开了 v3 但库里无命中：None，调用方照常首跑
    assert await build_cache_snapshot(
        sf, device_serial="S1", goal="g", effective_cache_mode="v3"
    ) is None
