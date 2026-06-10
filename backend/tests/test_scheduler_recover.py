"""Server 重启恢复：把 DB 里仍 running 的 item 的设备占用 + track 重建回来。

覆盖的缺口：Server 重启会丢内存里的设备锁和 _runs track，若不恢复，调度器会把
后续 queued item 误派到"其实正被旧 Run 占用"的设备上，被 Agent is_busy 弹回 →
后续队列被一串 device busy 误判失败。本测试验证 `_recover_running_from_db` 会在
启动时按 DB 现状把设备重新上锁并重建 track。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Run, Submission, SubmissionItem
from ai_phone.server.scheduler.service import SubmissionScheduler


def _make_scheduler(factory) -> tuple[SubmissionScheduler, DeviceLockStore]:
    lock_store = DeviceLockStore()
    sched = SubmissionScheduler(hub=Hub(), lock_store=lock_store, session_factory=factory)
    return sched, lock_store


@pytest.mark.asyncio
async def test_recover_running_relocks_device_and_rebuilds_track(_test_engine):
    factory = db_module.get_session_factory()
    async with factory() as s:
        s.add(Submission(id="sub1", state="accepted"))
        s.add(
            Run(
                id="R1",
                device_serial="6f4ca782",
                agent_id="agentX",
                goal="g",
                status="running",
            )
        )
        s.add(
            SubmissionItem(
                id="item1",
                submission_id="sub1",
                case_id="C1",
                platform="android",
                run_content="rc",
                state="running",
                device_serial="6f4ca782",
                run_id="R1",
            )
        )
        await s.commit()

    sched, lock_store = _make_scheduler(factory)

    # 模拟重启后的空白态：内存里既无锁、无 track、无下行路由
    assert lock_store.peek("6f4ca782") is None
    assert sched.snapshot()["running"] == {}
    assert sched._hub.agent_id_for_run("R1") is None

    await sched._recover_running_from_db()

    # 设备被重新上锁，holder = sched-<item_id>（与正常派发同一约定）
    lock = lock_store.peek("6f4ca782")
    assert lock is not None
    assert lock.holder == "sched-item1"
    assert lock.holder_type == "auto"

    # 在跑 track 重建，指向该设备 / item
    assert "R1" in sched._runs
    assert sched._runs["R1"].serial == "6f4ca782"
    assert sched._runs["R1"].item_id == "item1"
    # track 的 lock_token 与锁一致 → 日后 on_run_done 能按 token 正确释放
    assert sched._runs["R1"].lock_token == lock.token
    # [P1-a] run→agent 下行路由恢复 → 重启后 cancel/超时 stop_run 仍能投达
    assert sched._hub.agent_id_for_run("R1") == "agentX"


@pytest.mark.asyncio
async def test_recover_reconciles_item_when_run_already_terminal(_test_engine):
    """半提交脏数据：Run 已终态、item 还停 running（崩在 finalize 与 on_run_done 之间）。

    恢复时应**直接把 item 收口**（按 Run 终态落 item 终态），而**不**重新上锁/建 track —
    否则会把一台其实空闲的设备锁死、卡住批次。
    """
    factory = db_module.get_session_factory()
    async with factory() as s:
        s.add(Submission(id="sub1", state="accepted"))
        s.add(
            Run(
                id="R2",
                device_serial="6f4ca782",
                agent_id="agentX",
                goal="g",
                status="success",
                finished_at=datetime.now(timezone.utc),
            )
        )
        s.add(
            SubmissionItem(
                id="item2",
                submission_id="sub1",
                case_id="C1",
                platform="android",
                run_content="rc",
                state="running",  # 脏：Run 已 success，但 item 没来得及更新
                device_serial="6f4ca782",
                run_id="R2",
            )
        )
        await s.commit()

    sched, lock_store = _make_scheduler(factory)
    await sched._recover_running_from_db()

    # 不占设备、不建 track
    assert lock_store.snapshot() == {}
    assert sched._runs == {}

    # item 被收口成与 Run 一致的终态
    async with factory() as s:
        it = await s.get(SubmissionItem, "item2")
        assert it.state == "success"
        assert it.status_reason == "completed"
        assert it.finished_at is not None


@pytest.mark.asyncio
async def test_recover_terminal_failed_run_does_not_retry(_test_engine):
    """Run 已 failed 且 retry 额度未耗尽，但 item 仍 running（半提交脏数据）。

    重启对账应把它「收尸」成 failed —— **不重跑、不占设备**。这是回归守卫：曾经为
    保留 retry 改走 on_run_done，结果失败+有额度时会无锁重派、把设备误判空闲。
    """
    factory = db_module.get_session_factory()
    async with factory() as s:
        s.add(Submission(id="sub1", state="accepted"))
        s.add(
            Run(
                id="R3",
                device_serial="6f4ca782",
                agent_id="agentX",
                goal="g",
                status="failed",
                reason="boom",
                finished_at=datetime.now(timezone.utc),
                effective_retry_max=3,
            )
        )
        s.add(
            SubmissionItem(
                id="item3",
                submission_id="sub1",
                case_id="C1",
                platform="android",
                run_content="rc",
                state="running",
                device_serial="6f4ca782",
                run_id="R3",
                effective_retry_max=3,
            )
        )
        await s.commit()

    sched, lock_store = _make_scheduler(factory)
    await sched._recover_running_from_db()

    # 不重跑、不占设备、不建 track
    assert lock_store.snapshot() == {}
    assert sched._runs == {}
    # item 直接收尸成 failed（而非被拉回 running 走 retry）
    async with factory() as s:
        it = await s.get(SubmissionItem, "item3")
        assert it.state == "failed"
        assert it.finished_at is not None


@pytest.mark.asyncio
async def test_recover_ignores_queued_items(_test_engine):
    """只恢复 running；queued item 不应被上锁、不应进 track。"""
    factory = db_module.get_session_factory()
    async with factory() as s:
        s.add(Submission(id="sub1", state="accepted"))
        s.add(
            SubmissionItem(
                id="q1",
                submission_id="sub1",
                case_id="C1",
                platform="android",
                run_content="rc",
                state="queued",
            )
        )
        await s.commit()

    sched, lock_store = _make_scheduler(factory)
    await sched._recover_running_from_db()

    assert sched._runs == {}
    assert lock_store.snapshot() == {}


@pytest.mark.asyncio
async def test_recover_skips_running_without_serial_or_run(_test_engine):
    """running 但缺 device_serial / run_id 的脏数据：跳过，不报错、不上锁。"""
    factory = db_module.get_session_factory()
    async with factory() as s:
        s.add(Submission(id="sub1", state="accepted"))
        s.add(
            SubmissionItem(
                id="bad1",
                submission_id="sub1",
                case_id="C1",
                platform="android",
                run_content="rc",
                state="running",
                device_serial=None,
                run_id=None,
            )
        )
        await s.commit()

    sched, lock_store = _make_scheduler(factory)
    await sched._recover_running_from_db()

    assert sched._runs == {}
    assert lock_store.snapshot() == {}
