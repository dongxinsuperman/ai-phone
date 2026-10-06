"""V3 platform sharing, mixed-version safety, and generation-bound invalidation."""
from copy import deepcopy
from datetime import datetime, timezone
import asyncio

import pytest
from sqlalchemy import func, select

from ai_phone.shared import protocol as P
from ai_phone.server import db
from ai_phone.server.models import Device, Run, RunLog, VlmTrajectoryCacheV3
from ai_phone.server.hub import Hub
from ai_phone.server.runner.dispatch import RunDispatchService
from ai_phone.server.trajectory_cache.repository import store_trajectory_cache_archive
from ai_phone.server.trajectory_cache.service import build_cache_key
from ai_phone.server.trajectory_cache.snapshot import build_cache_snapshot
from ai_phone.server.trajectory_cache.v3_identity import build_v3_platform_cache_key
from ai_phone.server.trajectory_cache.v3_service import (
    get_active_trajectory_cache_v3, delete_trajectory_cache_v3_for_run,
    mark_trajectory_cache_v3_suspect, V3_BINDING_LOG_TITLE,
)
from ai_phone.server.retry import attempt_context
from ai_phone.shared.scroll_gesture import seed_scroll_cache_key


GOAL = "进入设置，向下浏览设置列表并打开关于手机"


def archive(device="A", platform="android", *, shared=True, goal=GOAL, source="source-A"):
    return {
        "cache_mode": "v3", "device_code": device, "run_semantic_text": goal,
        "source_run_id": source, "platform": platform,
        "actions": [{"index": 1, "action_id": "a1", "type": "scroll", "direction": "down",
                     "amount": 1, "point": {"x": 360, "y": 640},
                     "scroll_gesture_version": 1, "scroll_type": "singleAction",
                     "plan_intent": "滑动设置列表"}],
        "meta": {"cache_scope": "platform"} if shared else {},
    }


@pytest.fixture
async def sf(_test_engine):
    factory = db.get_session_factory()
    async with factory() as session:
        session.add_all([Device(serial="A", platform="android"), Device(serial="B", platform="android"),
                         Device(serial="I", platform="ios"), Device(serial="IS", platform="ios_sim"),
                         Device(serial="H", platform="harmony"), Device(serial="U", platform="unknown")])
        await session.commit()
    return factory


async def lookup(sf, device="B", **kwargs):
    return await get_active_trajectory_cache_v3(
        sf, device_code=device, run_semantic_text=GOAL, allow_platform_cache=True, **kwargs,
    )


async def bind(sf, run_id="replay-B", *, device="B", attempt=1):
    async with sf() as session:
        run = await session.get(Run, run_id)
        if run is None:
            session.add(Run(id=run_id, device_serial=device, goal=GOAL, effective_cache_mode="v3"))
            await session.commit()
    return await build_cache_snapshot(
        sf, device_serial=device, goal=GOAL, effective_cache_mode="v3",
        allow_platform_cache=True, run_id=run_id, attempt=attempt,
    )


def test_platform_keys_ignore_device_and_separate_platforms_and_case_text():
    android = build_v3_platform_cache_key(platform="android", run_semantic_text=GOAL)
    assert android == build_v3_platform_cache_key(platform=" Android ", run_semantic_text=GOAL)
    assert android[0] != build_cache_key(device_code="android", run_semantic_text=GOAL, schema_version=3)[0]
    assert android[0] != build_cache_key(device_code="platform:android", run_semantic_text=GOAL, schema_version=3)[0]
    assert android[0] != build_v3_platform_cache_key(platform="ios", run_semantic_text=GOAL)[0]
    assert android[0] != build_v3_platform_cache_key(platform="harmony", run_semantic_text=GOAL)[0]
    assert android[0] != build_v3_platform_cache_key(platform="android", run_semantic_text=GOAL + "然后返回")[0]
    assert build_v3_platform_cache_key(platform="ios_sim", run_semantic_text=GOAL) == (
        build_v3_platform_cache_key(platform="ios", run_semantic_text=GOAL)
    )
    assert android[2] == build_cache_key(device_code="A", run_semantic_text=GOAL, schema_version=3)[2]
    with pytest.raises(ValueError):
        build_v3_platform_cache_key(platform="unknown", run_semantic_text=GOAL)


@pytest.mark.asyncio
async def test_android_a_cache_hits_android_b_but_not_ios_or_harmony(sf):
    source = archive()
    original = deepcopy(source)
    key = await store_trajectory_cache_archive(sf, archive=source)
    hit = await lookup(sf)
    assert hit["cache_key"] == key
    assert hit["device_code"] == "A"  # 来源追溯，不是命中条件。
    assert hit["actions"] == source["actions"]
    assert hit["meta"]["cache_revision"]
    assert source == original
    assert await lookup(sf, "I") is None
    assert await lookup(sf, "H") is None
    assert await lookup(sf, "U", platform="android") is None  # 不用提示覆盖登记平台。


@pytest.mark.asyncio
async def test_ios_real_and_simulator_share_family(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive("IS", "ios_sim"))
    assert (await lookup(sf, "I"))["cache_key"] == key
    assert (await lookup(sf, "IS"))["platform"] == "ios"


@pytest.mark.asyncio
async def test_registered_platform_wins_over_wrong_archive_hint(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive("I", "android"))
    assert key == seed_scroll_cache_key(build_v3_platform_cache_key(platform="ios", run_semantic_text=GOAL)[0])
    assert await lookup(sf, "B") is None


@pytest.mark.asyncio
async def test_unknown_platform_keeps_device_scope(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive("U", "android"))
    assert key == seed_scroll_cache_key(build_cache_key(device_code="U", run_semantic_text=GOAL, schema_version=3)[0])
    hit = await lookup(sf, "U")
    assert "cache_scope" not in hit["meta"] and "cache_revision" not in hit["meta"]
    assert await lookup(sf, "B") is None


@pytest.mark.asyncio
async def test_legacy_archives_and_callers_stay_device_bound(sf):
    legacy = await store_trajectory_cache_archive(sf, archive=archive(shared=False))
    assert (await lookup(sf, "A"))["cache_key"] == legacy
    assert await lookup(sf, "B") is None
    shared = await store_trajectory_cache_archive(sf, archive=archive())
    # 老调用默认不启用共享，仍拿 A 的旧 key；B 不会拿到 A 或共享缓存。
    assert (await get_active_trajectory_cache_v3(sf, device_code="A", run_semantic_text=GOAL))["cache_key"] == legacy
    assert await get_active_trajectory_cache_v3(sf, device_code="B", run_semantic_text=GOAL) is None
    assert (await lookup(sf, "A"))["cache_key"] == shared


@pytest.mark.asyncio
async def test_same_family_upsert_uses_one_row_and_preserves_source_provenance(sf):
    first = await store_trajectory_cache_archive(sf, archive=archive())
    revision = (await lookup(sf))["meta"]["cache_revision"]
    second = await store_trajectory_cache_archive(sf, archive=archive("B", source="source-B"))
    assert first == second
    hit = await lookup(sf, "A")
    assert hit["device_code"] == "B" and hit["source_run_id"] == "source-B"
    assert hit["meta"]["cache_revision"] != revision
    async with sf() as session:
        assert (await session.execute(select(func.count()).select_from(VlmTrajectoryCacheV3))).scalar_one() == 1


@pytest.mark.asyncio
async def test_parallel_platform_upserts_do_not_race_unique_insert(sf):
    keys = await asyncio.gather(*[
        store_trajectory_cache_archive(sf, archive=archive("A" if n % 2 else "B", source=f"run-{n}"))
        for n in range(12)
    ])
    assert len(set(keys)) == 1
    async with sf() as session:
        assert (await session.execute(select(func.count()).select_from(VlmTrajectoryCacheV3))).scalar_one() == 1


@pytest.mark.asyncio
async def test_shared_suspect_and_failure_delete_target_actual_hit(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive())
    await bind(sf)
    assert await mark_trajectory_cache_v3_suspect(sf, cache_key=key, run_id="replay-B", reason="miss") == 1
    assert await lookup(sf, "A") is None
    assert await delete_trajectory_cache_v3_for_run(sf, "replay-B") == 1


@pytest.mark.asyncio
async def test_late_failure_does_not_invalidate_new_shared_generation(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive())
    old_snapshot = await bind(sf)
    await store_trajectory_cache_archive(sf, archive=archive("B", source="new-success"))
    assert await mark_trajectory_cache_v3_suspect(sf, cache_key=key, run_id="replay-B", reason="late") == 0
    assert await delete_trajectory_cache_v3_for_run(sf, "replay-B") == 0
    hit = await lookup(sf)
    assert hit["meta"]["cache_revision"] != old_snapshot["meta"]["cache_revision"]
    assert hit["source_run_id"] == "new-success"


@pytest.mark.asyncio
async def test_first_run_miss_failure_cannot_delete_another_devices_new_cache(sf):
    assert await bind(sf) is None
    await store_trajectory_cache_archive(sf, archive=archive())
    assert await delete_trajectory_cache_v3_for_run(sf, "replay-B") == 0
    assert await lookup(sf) is not None


@pytest.mark.asyncio
async def test_no_binding_cannot_mark_shared_cache_or_delete_it_by_device(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive())
    async with sf() as session:
        session.add(Run(id="old-run", device_serial="B", goal=GOAL))
        await session.commit()
    assert await mark_trajectory_cache_v3_suspect(sf, cache_key=key, run_id="old-run", reason="unknown") == 0
    assert await delete_trajectory_cache_v3_for_run(sf, "old-run") == 0
    assert await lookup(sf) is not None


@pytest.mark.asyncio
async def test_binding_is_attempt_scoped_and_persisted_in_existing_logs(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive())
    await bind(sf, attempt=1)
    await store_trajectory_cache_archive(sf, archive=archive(source="new-generation"))
    await bind(sf, attempt=2)
    assert await mark_trajectory_cache_v3_suspect(sf, cache_key=key, run_id="replay-B", reason="late", attempt=1) == 0
    with attempt_context(2):
        assert await delete_trajectory_cache_v3_for_run(sf, "replay-B") == 1
    async with sf() as session:
        logs = (await session.execute(select(RunLog).where(RunLog.title == V3_BINDING_LOG_TITLE))).scalars().all()
        assert [row.attempt for row in logs] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [2, 3])
async def test_agent_suspect_message_targets_retry_binding_and_preserves_newer_cache(sf, attempt):
    from types import SimpleNamespace

    from ai_phone.agent.trajectory_cache.orchestrate import _mark_suspect
    from ai_phone.server.ws.agent_ws import _dispatch

    key = await store_trajectory_cache_archive(sf, archive=archive(source="first-generation"))
    await bind(sf, attempt=1)
    await store_trajectory_cache_archive(sf, archive=archive(source="retry-generation"))
    await bind(sf, attempt=attempt)
    messages = []
    hub = SimpleNamespace(touch_agent=lambda agent_id: None)

    class Bridge:
        async def send_cache_suspect(self, payload):
            messages.append(payload)
            # The receiving worker does not inherit the Agent's attempt context.
            with attempt_context(1):
                await _dispatch(hub, None, "unit-agent", payload)

    await _mark_suspect(Bridge(), run_id="replay-B", attempt=attempt,
                        cache_key=key, reason="retry rescue exhausted")
    assert messages[0]["attempt"] == attempt
    assert await lookup(sf) is None

    # A later successful run may publish a new shared cache. A late old message
    # must still be constrained by the original attempt's bound revision.
    await store_trajectory_cache_archive(sf, archive=archive(source="newer-generation"))
    with attempt_context(1):
        await _dispatch(hub, None, "unit-agent", messages[0])
    assert (await lookup(sf))["source_run_id"] == "newer-generation"
    async with sf() as session:
        logs = (await session.execute(select(RunLog).where(
            RunLog.run_id == "replay-B", RunLog.title == "V3轨迹缓存",
        ).order_by(RunLog.id))).scalars().all()
        assert [row.attempt for row in logs] == [attempt, attempt]
        assert "changed=1" in logs[0].content
        assert "changed=0" in logs[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [2, 3])
@pytest.mark.parametrize("explicit", [False, True])
async def test_v3_delete_log_uses_explicit_attempt_or_existing_context(sf, attempt, explicit):
    await store_trajectory_cache_archive(sf, archive=archive())
    await bind(sf, attempt=attempt)
    with attempt_context(1 if explicit else attempt):
        kwargs = {"attempt": attempt} if explicit else {}
        assert await delete_trajectory_cache_v3_for_run(sf, "replay-B", **kwargs) == 1
    async with sf() as session:
        row = (await session.execute(select(RunLog).where(
            RunLog.run_id == "replay-B", RunLog.title == "V3轨迹缓存",
        ))).scalars().one()
        assert row.attempt == attempt
        assert "deleted=1" in row.content


@pytest.mark.asyncio
async def test_corrupt_binding_fails_closed(sf):
    await store_trajectory_cache_archive(sf, archive=archive())
    await bind(sf)
    async with sf() as session:
        row = (await session.execute(select(RunLog).where(RunLog.title == V3_BINDING_LOG_TITLE))).scalars().one()
        row.content = "invalid-json"
        await session.commit()
    assert await delete_trajectory_cache_v3_for_run(sf, "replay-B") == 0
    assert await lookup(sf) is not None


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, **kwargs):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities,hit", [([], False), ([P.CAP_V3_PLATFORM_CACHE], True)])
async def test_actual_dispatch_gates_shared_cache_by_agent_capability(sf, capabilities, hit):
    await store_trajectory_cache_archive(sf, archive=archive())
    async with sf() as session:
        session.add(Run(id="dispatch-B", device_serial="B", goal=GOAL, effective_cache_mode="v3"))
        await session.commit()
    socket = Socket()
    hub = Hub()
    await hub.register_agent("agent-B", "B", "test", socket)
    hub.set_agent_capabilities("agent-B", capabilities)
    result = await RunDispatchService(hub=hub, session_factory=sf).dispatch(
        run_id="dispatch-B", serial="B", agent_id="agent-B", goal=GOAL,
        function_map_context="原始Map，必须完整保留", engine="vlm", dispatch_source="api",
    )
    assert result["dispatched"]
    payload = socket.sent[-1]
    assert ("cache_snapshot" in payload) is hit
    assert payload["goal"] == GOAL
    assert payload["function_map_context"] == "原始Map，必须完整保留"
    assert payload["device_serial"] == "B" and payload["cache_mode"] == "v3"


@pytest.mark.asyncio
async def test_agent_capability_does_not_leak_across_reconnect_or_bad_hello():
    hub = Hub()
    await hub.register_agent("same", "same", "test", Socket())
    hub.set_agent_capabilities("same", [P.CAP_V3_PLATFORM_CACHE])
    assert hub.agent_supports("same", P.CAP_V3_PLATFORM_CACHE)
    hub.set_agent_capabilities("same", P.CAP_V3_PLATFORM_CACHE)  # 非列表不能误认为支持。
    assert not hub.agent_supports("same", P.CAP_V3_PLATFORM_CACHE)
    hub.set_agent_capabilities("same", [P.CAP_V3_PLATFORM_CACHE])
    await hub.register_agent("same", "same", "test", Socket())
    assert not hub.agent_supports("same", P.CAP_V3_PLATFORM_CACHE)


@pytest.mark.asyncio
async def test_shared_snapshot_replay_locates_coordinates_on_device_b(sf, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from ai_phone.config import Settings
    from ai_phone.agent.trajectory_cache import v3_replay as replay

    await store_trajectory_cache_archive(sf, archive=archive())
    snapshot = await bind(sf)
    settings = Settings(_env_file=None, trajectory_cache_observe_delay_ms=0)
    monkeypatch.setattr(replay, "get_settings", lambda: settings)
    locator = replay.V3PlanLocator(settings=settings)
    monkeypatch.setattr(locator, "is_configured", lambda: True)
    monkeypatch.setattr(locator, "_config", lambda: (
        "doubao_responses", "https://unit.invalid", "unit", "unit", 300,
    ))
    stream = BytesIO()
    Image.new("RGB", (540, 1200), "white").save(stream, "JPEG")
    current_image = stream.getvalue()
    model_calls = []

    async def chat(**kwargs):
        model_calls.append(kwargs)
        return "<point>750 600</point>"

    monkeypatch.setattr(locator, "_chat_single_image", chat)

    class Driver:
        calls = []

        def window_size(self):
            return 1080, 2400

        def scroll(self, direction, point, amount, **kwargs):
            self.calls.append((direction, point, amount))

        scroll_seed = scroll

    driver = Driver()
    runner = replay.V3ReplayRunner(driver=driver, trajectory=snapshot, locator=locator, goal=GOAL)

    async def screenshot(*args, **kwargs):
        return current_image

    async def observe(*args, **kwargs):
        pass

    monkeypatch.setattr(runner, "_wait_stable_for_step", screenshot)
    monkeypatch.setattr(runner, "_screenshot_jpeg", screenshot)
    monkeypatch.setattr(runner, "_observe_after_action", observe)
    result = await runner.run()
    assert result.success
    assert driver.calls == [("down", (810, 1440), 1)]
    assert model_calls[0]["image_bytes"] == current_image
    assert runner.execution_history[0]["action"]["point"] == {"x": 810, "y": 1440}
    assert snapshot["actions"][0]["point"] == {"x": 360, "y": 640}


@pytest.mark.asyncio
async def test_old_scroll_archive_upload_is_rejected(sf):
    old = archive()
    old["actions"][0].pop("scroll_gesture_version")
    assert await store_trajectory_cache_archive(sf, archive=old) is None


@pytest.mark.asyncio
async def test_preexisting_old_scroll_cache_is_obsolete_not_a_hit(sf):
    key, normalized, semantic_hash = build_cache_key(device_code="A", run_semantic_text=GOAL, schema_version=3)
    async with sf() as session:
        session.add(VlmTrajectoryCacheV3(cache_key=key, device_code="A", run_semantic_text=normalized, run_semantic_hash=semantic_hash, source_vlm_backend="doubao_responses", actions_json=[{"type": "scroll", "center": {"x": 360, "y": 640}, "direction": "down"}], status="active"))
        await session.commit()
    assert await lookup(sf, device="A") is None
    async with sf() as session:
        row = (await session.execute(select(VlmTrajectoryCacheV3).where(VlmTrajectoryCacheV3.cache_key == key))).scalars().one()
        assert row.status == "obsolete"


@pytest.mark.asyncio
async def test_legacy_snapshot_mark_then_delete_remains_supported(sf):
    key = await store_trajectory_cache_archive(sf, archive=archive(shared=False))
    await bind(sf, device="A")
    assert await mark_trajectory_cache_v3_suspect(sf, cache_key=key, run_id="replay-B", reason="legacy") == 1
    assert await delete_trajectory_cache_v3_for_run(sf, "replay-B") == 1


@pytest.mark.asyncio
async def test_postgres_v3_upsert_compiles_atomic_conflict_statement():
    from types import SimpleNamespace
    from sqlalchemy.dialects import postgresql
    from ai_phone.server.trajectory_cache.repository import _upsert_v3

    statements = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, stmt):
            statements.append(str(stmt.compile(dialect=postgresql.dialect())))

        async def commit(self):
            pass

    assert await _upsert_v3(
        Session, archive(), cache_key="test-key", normalized_goal=GOAL, semantic_hash="test-hash",
        now=datetime.now(timezone.utc),
    ) == "test-key"
    assert "ON CONFLICT (cache_key) DO UPDATE" in statements[0]


@pytest.mark.asyncio
async def test_agent_loses_capability_during_lookup_falls_back_without_shared_binding(sf, monkeypatch):
    await store_trajectory_cache_archive(sf, archive=archive())
    async with sf() as session:
        session.add(Run(id="reconnect", device_serial="B", goal=GOAL, effective_cache_mode="v3"))
        await session.commit()
    socket = Socket()
    hub = Hub()
    await hub.register_agent("agent-B", "B", "test", socket)
    hub.set_agent_capabilities("agent-B", [P.CAP_V3_PLATFORM_CACHE])
    dispatcher = RunDispatchService(hub=hub, session_factory=sf)
    original = dispatcher._maybe_build_cache_snapshot

    async def lookup_then_downgrade(**kwargs):
        snapshot = await original(**kwargs)
        assert snapshot is not None
        hub.set_agent_capabilities("agent-B", [])
        return snapshot

    monkeypatch.setattr(dispatcher, "_maybe_build_cache_snapshot", lookup_then_downgrade)
    result = await dispatcher.dispatch(
        run_id="reconnect", serial="B", agent_id="agent-B", goal=GOAL,
        engine="vlm", dispatch_source="api",
    )
    assert result["dispatched"] and "cache_snapshot" not in socket.sent[-1]
    assert await delete_trajectory_cache_v3_for_run(sf, "reconnect") == 0
    assert await lookup(sf) is not None


@pytest.mark.asyncio
async def test_finalizer_uses_explicit_done_attempt_not_default_context(sf):
    from ai_phone.server.trajectory_cache.finalize import _BACKGROUND_TASKS
    from ai_phone.server.ws.agent_ws import _finalize_run

    await store_trajectory_cache_archive(sf, archive=archive())
    await bind(sf, attempt=1)
    await store_trajectory_cache_archive(sf, archive=archive(source="attempt-2-source"))
    await bind(sf, attempt=2)
    before = set(_BACKGROUND_TASKS)
    assert await _finalize_run("replay-B", {"result": "assert_fail", "attempt": 2})
    pending = _BACKGROUND_TASKS - before
    assert len(pending) == 1
    await asyncio.gather(*pending)
    assert await lookup(sf) is None
