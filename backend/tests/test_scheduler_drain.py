"""调度核心 ``_drain_once``：每条 item 只在『它要的设备都被占用』时才排队。

回归守卫的核心是队头阻塞（head-of-line blocking）：``device_alias_pool`` 粒度下，
队头那条 item 点名要的设备忙，**不应**阻塞它后面、要其它空闲设备的 item。曾经
``_drain_once`` 在队头派不出去时直接 ``break``，把整条平台队列停摆——空闲设备干等，
后面的 item 永远轮不到。本文件把正确语义钉死。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Device, DeviceAlias, Submission, SubmissionItem
from ai_phone.server.scheduler.service import SubmissionScheduler


class _FakeDispatch:
    """打桩派发：记录每次派到哪台设备，不碰真实 WS。

    ``fail_serials`` 里的设备会返回 ``dispatched=False``（模拟发 Agent 失败），
    用来验证『派 Agent 失败』的 item 本轮不会被重抓。
    """

    def __init__(self, fail_serials: set[str] | None = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail_serials = set(fail_serials or set())

    async def wait_until_not_running(self, run_id: str) -> bool:  # noqa: ARG002
        return True

    async def dispatch(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        ok = kwargs.get("serial") not in self.fail_serials
        return {"dispatched": ok, "execution_mode": "agent_brain"}

    async def stop(self, run_id: str) -> bool:  # noqa: ARG002
        return True

    @property
    def dispatched_serials(self) -> List[str]:
        return [c.get("serial") for c in self.calls]


def _make_scheduler(
    factory, *, fail_serials: set[str] | None = None
) -> tuple[SubmissionScheduler, DeviceLockStore, Hub, _FakeDispatch]:
    lock_store = DeviceLockStore()
    hub = Hub()
    dispatch = _FakeDispatch(fail_serials=fail_serials)
    sched = SubmissionScheduler(
        hub=hub,
        lock_store=lock_store,
        session_factory=factory,
        dispatch_service=dispatch,  # type: ignore[arg-type]
    )
    return sched, lock_store, hub, dispatch


def _mark_ready(hub: Hub, serial: str, agent_id: str) -> None:
    """把一台设备标成『有 agent + readiness.ready』，让 _pick_device 认它可用。"""
    hub._serial_to_agent[serial] = agent_id  # noqa: SLF001  # 测试直接注入路由
    hub.set_device_readiness(serial, {"ready": True, "platform": "android"})


async def _seed_device(factory, serial: str, *, platform: str = "android") -> None:
    async with factory() as s:
        s.add(Device(serial=serial, platform=platform, status="online"))
        await s.commit()


async def _seed_alias(factory, alias: str, serial: str) -> None:
    async with factory() as s:
        s.add(DeviceAlias(serial=serial, alias=alias))
        await s.commit()


async def _seed_item(
    factory,
    *,
    item_id: str,
    submission_id: str,
    pool: List[str] | None = None,
    platform: str = "android",
) -> None:
    async with factory() as s:
        if await s.get(Submission, submission_id) is None:
            s.add(Submission(id=submission_id, state="accepted"))
        s.add(
            SubmissionItem(
                id=item_id,
                submission_id=submission_id,
                case_id=item_id,
                platform=platform,
                run_content="rc",
                state="queued",
                device_alias_pool=list(pool) if pool else None,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_head_of_line_blocked_item_does_not_stall_followers(_test_engine):
    """核心回归：队头点名要的设备忙，后面要空闲设备的 item 仍应被派出去。

    A（要忙着的 S_BUSY）排在 B（要空闲的 S_FREE）前面。正确行为：A 继续排队、
    B 立即派发到 S_FREE。修复前的 ``break`` 会让 B 一起卡住。
    """
    factory = db_module.get_session_factory()
    await _seed_device(factory, "S_BUSY")
    await _seed_device(factory, "S_FREE")
    await _seed_alias(factory, "aBusy", "S_BUSY")
    await _seed_alias(factory, "aFree", "S_FREE")
    await _seed_item(factory, item_id="A", submission_id="sub1", pool=["aBusy"])
    await _seed_item(factory, item_id="B", submission_id="sub1", pool=["aFree"])

    sched, lock_store, hub, dispatch = _make_scheduler(factory)
    _mark_ready(hub, "S_BUSY", "agent1")
    _mark_ready(hub, "S_FREE", "agent1")
    # S_BUSY 被占用
    await lock_store.acquire(
        "S_BUSY", holder="someone", holder_type="auto", ttl_seconds=600, meta={}
    )
    # 入队顺序：A 在前、B 在后
    sched._queues["android"] = ["A", "B"]

    await sched._drain_once()

    # B 派到了 S_FREE；A 没被派
    assert dispatch.dispatched_serials == ["S_FREE"]
    # A 仍在队列里排队（它要的 S_BUSY 还忙），B 已出队
    assert sched._queues["android"] == ["A"]

    async with factory() as s:
        a = await s.get(SubmissionItem, "A")
        b = await s.get(SubmissionItem, "B")
        assert a.state == "queued"
        assert b.state == "running"
        assert b.device_serial == "S_FREE"


@pytest.mark.asyncio
async def test_no_available_device_short_circuits_without_dispatch(_test_engine):
    """该平台一台可用设备都没有：整段短路，谁都不派、全部保持 queued。"""
    factory = db_module.get_session_factory()
    await _seed_device(factory, "S1")
    await _seed_item(factory, item_id="A", submission_id="sub1")
    await _seed_item(factory, item_id="B", submission_id="sub1")

    sched, lock_store, hub, dispatch = _make_scheduler(factory)
    _mark_ready(hub, "S1", "agent1")
    # 唯一设备被占用 → 无可用设备
    await lock_store.acquire(
        "S1", holder="someone", holder_type="auto", ttl_seconds=600, meta={}
    )

    assert await sched._has_available_device("android") is False

    sched._queues["android"] = ["A", "B"]
    await sched._drain_once()

    assert dispatch.calls == []
    # 顺序与内容都不变，等设备空出来下一轮再派
    assert sched._queues["android"] == ["A", "B"]
    async with factory() as s:
        assert (await s.get(SubmissionItem, "A")).state == "queued"
        assert (await s.get(SubmissionItem, "B")).state == "queued"


@pytest.mark.asyncio
async def test_unpooled_item_takes_any_free_device(_test_engine):
    """不指定设备的 item：平台里有空闲设备就任挑一台派出。"""
    factory = db_module.get_session_factory()
    await _seed_device(factory, "S1")
    await _seed_item(factory, item_id="A", submission_id="sub1")

    sched, lock_store, hub, dispatch = _make_scheduler(factory)
    _mark_ready(hub, "S1", "agent1")
    sched._queues["android"] = ["A"]

    await sched._drain_once()

    assert dispatch.dispatched_serials == ["S1"]
    assert sched._queues["android"] == []
    async with factory() as s:
        assert (await s.get(SubmissionItem, "A")).state == "running"


@pytest.mark.asyncio
async def test_non_queued_item_is_dropped_from_queue(_test_engine):
    """队列里残留的非 queued item（已取消/超时）应被剔除、不派发。"""
    factory = db_module.get_session_factory()
    await _seed_device(factory, "S1")
    # A 已 cancelled（但 id 还残留在内存队列里）；B 正常 queued
    await _seed_item(factory, item_id="B", submission_id="sub1")
    async with factory() as s:
        s.add(
            SubmissionItem(
                id="A",
                submission_id="sub1",
                case_id="A",
                platform="android",
                run_content="rc",
                state="cancelled",
                status_reason="cancelled_by_request",
            )
        )
        await s.commit()

    sched, lock_store, hub, dispatch = _make_scheduler(factory)
    _mark_ready(hub, "S1", "agent1")
    sched._queues["android"] = ["A", "B"]

    await sched._drain_once()

    # A 被剔除（非 queued），B 派到 S1
    assert dispatch.dispatched_serials == ["S1"]
    assert sched._queues["android"] == []
    async with factory() as s:
        assert (await s.get(SubmissionItem, "A")).state == "cancelled"
        assert (await s.get(SubmissionItem, "B")).state == "running"


@pytest.mark.asyncio
async def test_dispatch_failed_item_not_retried_in_same_pass(_test_engine):
    """派 Agent 失败（dispatch_failed）的 item 本轮只试一次，不在同一趟里反复重抓。

    这是 Codex review [P2] 的回归守卫：``dispatch_failed`` 与『设备暂时被占』是两
    类原因，前者本轮重抓只会重复建失败 Run、重复发失败。单趟扫描保证每条最多试
    一次——F 派 Agent 失败后保持 queued、等下一次 tick，本轮不再重抓。
    """
    factory = db_module.get_session_factory()
    await _seed_device(factory, "S_FAIL")
    await _seed_device(factory, "S_OK")
    await _seed_alias(factory, "aFail", "S_FAIL")
    await _seed_alias(factory, "aOk", "S_OK")
    await _seed_item(factory, item_id="F", submission_id="sub1", pool=["aFail"])
    await _seed_item(factory, item_id="G", submission_id="sub1", pool=["aOk"])

    # S_FAIL 的 dispatch 永远失败（模拟发 Agent 失败）
    sched, lock_store, hub, dispatch = _make_scheduler(factory, fail_serials={"S_FAIL"})
    _mark_ready(hub, "S_FAIL", "agent1")
    _mark_ready(hub, "S_OK", "agent1")
    sched._queues["android"] = ["F", "G"]

    await sched._drain_once()

    # 关键断言：S_FAIL 这一轮只被尝试派发了一次（旧的队尾轮转会试两次）
    assert dispatch.dispatched_serials.count("S_FAIL") == 1
    assert "S_OK" in dispatch.dispatched_serials
    # F 回滚保持 queued（等下一次 tick）；G 已派发出队
    assert sched._queues["android"] == ["F"]
    async with factory() as s:
        f = await s.get(SubmissionItem, "F")
        g = await s.get(SubmissionItem, "G")
        assert f.state == "queued"
        assert g.state == "running"
