"""M3 可靠上报单测（Distributed Agent Brain）。

覆盖四块：
- 进程级 ReliableReporter（统一收发室）：断连留存不丢、重连按序补发、补发中途再断
  停在队头续发、超容量丢最老。
- bridge × reporter 集成：**run 结束、bridge 销毁后，未发出的终态仍在全局队列不丢**
  （这是 per-run 队列解决不了、必须上提进程级的关键场景 P1-B）；无 reporter 时退化直发。
- Server 端去重：run_logs / run_steps 按 (run_id, attempt, event_id) 去重，老链路不变。
- 截图上传失败重试；Server 终态幂等。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from ai_phone.agent.reliable_reporter import ReliableReporter
from ai_phone.agent.runner_bridge import RunnerBridge


# =============================================================================
# ReliableReporter：断连不丢 + 重连保序补发
# =============================================================================
@pytest.mark.asyncio
async def test_reporter_retains_on_disconnect_then_flushes_in_order():
    sent: List[Dict[str, Any]] = []
    link = {"up": False}  # 初始断连

    async def _send(msg: Dict[str, Any]) -> bool:
        if not link["up"]:
            return False  # client.send 断连时返回 False（不抛异常）
        sent.append(dict(msg))
        return True

    r = ReliableReporter(_send)

    # 断连期间入队 3 条 → 全部留存、一条没发出
    await r.enqueue({"type": "log", "title": "a"})
    await r.enqueue({"type": "log", "title": "b"})
    await r.enqueue({"type": "step_done", "step": 1})
    assert sent == []
    assert r.pending() == 3

    # 连接恢复 → flush 串行按入队序补发；每条都带 event_id + 递增 seq
    link["up"] = True
    await r.flush()
    assert r.pending() == 0
    assert [m["seq"] for m in sent] == [1, 2, 3]
    assert all(m.get("event_id") for m in sent)
    assert [m.get("title") or m.get("step") for m in sent] == ["a", "b", 1]


@pytest.mark.asyncio
async def test_reporter_flush_stops_at_head_on_redisconnect_and_resumes():
    """补发途中再次断连：停在队头，剩余按序保留，下次重连续发。"""
    sent: List[Dict[str, Any]] = []
    mode = {"v": "fail"}  # fail=全断 / one=只发一条再断 / ok=全通

    async def _send(msg: Dict[str, Any]) -> bool:
        if mode["v"] == "fail":
            return False
        if mode["v"] == "one":
            if sent:
                return False
            sent.append(dict(msg))
            return True
        sent.append(dict(msg))
        return True

    r = ReliableReporter(_send)
    for title in ("a", "b", "c"):
        await r.enqueue({"type": "log", "title": title})
    assert r.pending() == 3

    mode["v"] = "one"
    await r.flush()
    assert [m["title"] for m in sent] == ["a"]
    assert r.pending() == 2

    sent.clear()
    mode["v"] = "ok"
    await r.flush()
    assert [m["title"] for m in sent] == ["b", "c"]
    assert r.pending() == 0


@pytest.mark.asyncio
async def test_reporter_trims_oldest_when_over_capacity():
    """极端长断网保护：超容量丢最老，保留最新 N 条且保序。"""
    sent: List[Dict[str, Any]] = []
    link = {"up": False}

    async def _send(msg: Dict[str, Any]) -> bool:
        if not link["up"]:
            return False
        sent.append(dict(msg))
        return True

    r = ReliableReporter(_send, max_queue=3)
    for i in range(5):
        await r.enqueue({"type": "log", "i": i})
    assert r.pending() == 3  # 丢了最老 2 条

    link["up"] = True
    await r.flush()
    assert [m["i"] for m in sent] == [2, 3, 4]  # 保留最新 3 条、保序


@pytest.mark.asyncio
async def test_reporter_worker_drains_when_connected():
    """后台 drain worker：连接可用时自动把队列发空。"""
    sent: List[Dict[str, Any]] = []

    async def _send(msg: Dict[str, Any]) -> bool:
        sent.append(dict(msg))
        return True

    r = ReliableReporter(_send)
    r.start()
    r.notify_connected()
    await r.enqueue({"type": "log", "title": "a"})

    for _ in range(20):  # 给 worker 机会跑（最多 ~0.2s）
        if r.pending() == 0:
            break
        await asyncio.sleep(0.01)
    await r.stop()

    assert [m["title"] for m in sent] == ["a"]


# =============================================================================
# bridge × reporter 集成：P1-B —— run 结束、bridge 销毁后，终态仍不丢
# =============================================================================
@pytest.mark.asyncio
async def test_run_done_survives_after_bridge_closed():
    """断网期间 run 结束 → 终态进全局 reporter；bridge.aclose() 后它仍在，重连补发。"""
    sent: List[Dict[str, Any]] = []
    link = {"up": False}

    async def _send(msg: Dict[str, Any]) -> bool:
        if not link["up"]:
            return False
        sent.append(dict(msg))
        return True

    reporter = ReliableReporter(_send)
    bridge = RunnerBridge(
        run_id="r1",
        serial="S1",
        ws_send=_send,
        server_http_base="http://test",
        attempt=1,
        reporter=reporter,
    )

    # 断网期间 run 结束，旁路发终态
    await bridge.send_run_done(
        {"type": "run_done", "run_id": "r1", "result": "error", "message": "boom"}
    )
    # run 结束：bridge 销毁。关键断言——终态没随 bridge 丢，还在全局队列里
    await bridge.aclose()
    assert reporter.pending() == 1

    # 重连补发，终态送达
    link["up"] = True
    await reporter.flush()
    assert len(sent) == 1
    assert sent[0]["type"] == "run_done"
    assert reporter.pending() == 0


@pytest.mark.asyncio
async def test_bridge_without_reporter_sends_directly():
    """无 reporter 时退化为直接发（best-effort），兼容不关心可靠性的单测。"""
    sent: List[Dict[str, Any]] = []

    async def _send(msg: Dict[str, Any]) -> bool:
        sent.append(dict(msg))
        return True

    bridge = RunnerBridge(
        run_id="r1",
        serial="S1",
        ws_send=_send,
        server_http_base="http://test",
        attempt=1,
    )
    await bridge._reliable_send({"type": "log", "title": "x"})
    assert [m["title"] for m in sent] == ["x"]
    await bridge.aclose()


# =============================================================================
# Server 端去重：按 (run_id, attempt, event_id) 不重复落库
# =============================================================================
@pytest.mark.asyncio
async def test_persist_log_dedup_by_event_id(_test_engine, session):
    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import RunLog

    msg = {
        "run_id": "rid-dedup",
        "attempt": 1,
        "event_id": "ev-1",
        "level": 1,
        "title": "t",
        "content": "c",
    }
    await agent_ws._persist_log(dict(msg))
    await agent_ws._persist_log(dict(msg))  # 断线补发的重复：应被去重

    rows = (
        await session.execute(select(RunLog).where(RunLog.run_id == "rid-dedup"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_id == "ev-1"


@pytest.mark.asyncio
async def test_persist_log_without_event_id_keeps_old_behavior(_test_engine, session):
    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import RunLog

    msg = {"run_id": "rid-noev", "attempt": 1, "level": 1, "title": "t", "content": "c"}
    await agent_ws._persist_log(dict(msg))
    await agent_ws._persist_log(dict(msg))  # 无 event_id → 老链路，不去重

    rows = (
        await session.execute(select(RunLog).where(RunLog.run_id == "rid-noev"))
    ).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_persist_step_dedup_by_event_id(_test_engine, session):
    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import RunStep

    msg = {
        "run_id": "rid-step",
        "attempt": 1,
        "step": 1,
        "event_id": "ev-step-1",
        "thought": "th",
        "action": "click",
        "action_type": "click",
        "elapsed_ms": 10,
    }
    await agent_ws._persist_step(dict(msg))
    await agent_ws._persist_step(dict(msg))  # 重复 step_done 补发：去重

    rows = (
        await session.execute(select(RunStep).where(RunStep.run_id == "rid-step"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_id == "ev-step-1"


# =============================================================================
# 截图上传失败重试
# =============================================================================
@pytest.mark.asyncio
async def test_upload_retry_recovers_on_second_attempt(monkeypatch):
    import ai_phone.agent.runner_bridge as rb_mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(rb_mod.asyncio, "sleep", _no_sleep)

    bridge = RunnerBridge(
        run_id="r1", serial="S1", ws_send=lambda _p: None,
        server_http_base="http://test", attempt=1,
    )
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"url": "http://x/s.jpg"}

    async def _post(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")  # 首次瞬时失败
        return _Resp()

    monkeypatch.setattr(bridge._http, "post", _post)

    url = await bridge._upload_with_retry(b"jpegbytes", step=1, phase="before", attempts=3)
    assert url == "http://x/s.jpg"
    assert calls["n"] == 2

    await bridge._http.aclose()


@pytest.mark.asyncio
async def test_upload_retry_exhausted_returns_empty(monkeypatch):
    import ai_phone.agent.runner_bridge as rb_mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(rb_mod.asyncio, "sleep", _no_sleep)

    bridge = RunnerBridge(
        run_id="r1", serial="S1", ws_send=lambda _p: None,
        server_http_base="http://test", attempt=1,
    )

    async def _always_fail(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(bridge._http, "post", _always_fail)

    url = await bridge._upload_with_retry(b"x", step=2, phase="after", attempts=2)
    assert url == ""

    await bridge._http.aclose()


# =============================================================================
# Server 端终态幂等：重复 run_done 不重复落终态 / 不重复触发缓存归档
# =============================================================================
@pytest.mark.asyncio
async def test_finalize_run_idempotent_on_duplicate_run_done(
    _test_engine, session, monkeypatch
):
    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import Run

    archive_calls = {"n": 0}

    def _stub_finalize(*_a, **_k):
        archive_calls["n"] += 1

    monkeypatch.setattr(
        "ai_phone.server.trajectory_cache.finalize.schedule_trajectory_cache_finalize",
        _stub_finalize,
    )

    session.add(Run(id="fr1", device_serial="S1", goal="g", status="running"))
    await session.commit()

    msg = {
        "run_id": "fr1",
        "attempt": 1,
        "result": "finished",
        "message": "ok",
        "steps": 3,
        "elapsed_ms": 100,
        "token_stats": {"total_tokens": 5},
    }

    assert await agent_ws._finalize_run("fr1", dict(msg)) is True
    assert await agent_ws._finalize_run("fr1", dict(msg)) is False
    assert archive_calls["n"] == 1

    session.expire_all()
    refreshed = await session.get(Run, "fr1")
    assert refreshed.status == "success"
    assert refreshed.steps == 3
    assert refreshed.finished_at is not None


# =============================================================================
# 时序保真（M4 地基）：原始事件时间透传，缓存归档 timing 不被断线补发污染
# =============================================================================
@pytest.mark.asyncio
async def test_bridge_forwards_original_event_ts():
    """bridge 把 make_event 的原始 ts 透传进 MSG_LOG / MSG_STEP_DONE（不再丢）。"""
    from ai_phone.agent.runner.events import EVT_STEP_END, log_event, make_event

    captured: List[Dict[str, Any]] = []

    class _FakeReporter:
        async def enqueue(self, msg: Dict[str, Any]) -> None:
            captured.append(dict(msg))

    bridge = RunnerBridge(
        run_id="r1", serial="S1", ws_send=lambda _p: None,
        server_http_base="http://test", attempt=1, reporter=_FakeReporter(),
    )

    log_evt = log_event("r1", 1, "思考", "内容", step=1)
    await bridge._forward_log(log_evt)
    step_evt = make_event(
        EVT_STEP_END, "r1", step=1, thought="t", action="click(...)",
        action_type="click", elapsed_ms=5,
    )
    await bridge._forward_step_end(step_evt)

    log_msg = next(m for m in captured if m["type"] == "log")
    step_msg = next(m for m in captured if m["type"] == "step_done")
    assert log_msg["ts"] == log_evt["ts"]  # 原样透传
    assert step_msg["ts"] == step_evt["ts"]

    await bridge.aclose()


@pytest.mark.asyncio
async def test_persist_log_uses_event_ts_not_receive_time(_test_engine, session):
    """Server 用消息里的原始 ts 落 RunLog.ts，而非接收时间——断线补发也保真。"""
    from datetime import datetime, timezone

    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import RunLog

    event_ms = 1_700_000_000_123  # 一个固定的"很久以前"的原始事件时间（毫秒）
    await agent_ws._persist_log({
        "run_id": "rid-ts", "attempt": 1, "level": 1,
        "title": "t", "content": "c", "ts": event_ms,
    })
    row = (
        await session.execute(select(RunLog).where(RunLog.run_id == "rid-ts"))
    ).scalars().first()
    assert row is not None
    # SQLite 读回是 naive datetime（丢 tz，生产 PG 会保留），比较值即可。
    expected = datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
    assert row.ts.replace(tzinfo=None) == expected.replace(tzinfo=None)
    assert row.ts.year == 2023  # 用了原始事件时间，而非 now()（2026）


# =============================================================================
# 体验优化：log / step 改「先广播后后台保序落库」——最终落库 + 保序，语义不变
# =============================================================================
@pytest.mark.asyncio
async def test_enqueue_persist_writes_all_in_order(_test_engine, session):
    """_enqueue_persist：后台单 worker 按 FIFO 把入队的落库动作全部执行且保序。

    这是「把广播与落库解耦」（agent_ws 收 log/step 先广播给浏览器、落库丢后台）
    的正确性保障——落库结果必须和原同步串行一致：不丢、不乱序。
    """
    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import RunLog

    # 模块级 worker / 队列跨测试与 event loop 复用会踩 asyncio「task 绑旧 loop」坑，
    # 这里显式重置，保证 worker 在当前测试的 loop 上重建。
    agent_ws._persist_queue = None
    agent_ws._persist_worker = None
    try:
        for i in range(5):
            agent_ws._enqueue_persist(
                lambda n=i: agent_ws._persist_log(
                    {
                        "run_id": "rid-async-order",
                        "attempt": 1,
                        "event_id": f"ev-{n}",
                        "level": 1,
                        "title": f"t{n}",
                        "content": "c",
                        "step": n,
                    }
                )
            )
        assert agent_ws._persist_queue is not None
        await asyncio.wait_for(agent_ws._persist_queue.join(), timeout=5.0)

        rows = (
            await session.execute(
                select(RunLog)
                .where(RunLog.run_id == "rid-async-order")
                .order_by(RunLog.step)
            )
        ).scalars().all()
        # 5 条全部落库（不丢）且严格按入队顺序（不乱序）
        assert [r.title for r in rows] == ["t0", "t1", "t2", "t3", "t4"]
    finally:
        w = agent_ws._persist_worker
        if w is not None and not w.done():
            w.cancel()
        agent_ws._persist_queue = None
        agent_ws._persist_worker = None


@pytest.mark.asyncio
async def test_drain_persist_queue_waits_until_all_written(_test_engine, session):
    """收口补丁：_drain_persist_queue 返回后，积压的 step 必须已全部落库。

    run_done 收尾会立刻生成 HTML 报告（读 RunStep/RunLog），drain 保证此刻数据齐全。
    """
    from ai_phone.server.ws import agent_ws
    from ai_phone.server.models import RunStep

    agent_ws._persist_queue = None
    agent_ws._persist_worker = None
    try:
        for i in range(4):
            agent_ws._enqueue_persist(
                lambda n=i: agent_ws._persist_step(
                    {
                        "run_id": "rid-drain",
                        "attempt": 1,
                        "step": n + 1,
                        "event_id": f"ev-drain-{n}",
                        "thought": "t",
                        "action": "click",
                        "action_type": "click",
                        "elapsed_ms": 1,
                    }
                )
            )
        # drain 返回时，4 条 step 必须都已落库（报告读得到完整数据）
        await agent_ws._drain_persist_queue(timeout=5.0)
        rows = (
            await session.execute(
                select(RunStep).where(RunStep.run_id == "rid-drain")
            )
        ).scalars().all()
        assert len(rows) == 4
    finally:
        w = agent_ws._persist_worker
        if w is not None and not w.done():
            w.cancel()
        agent_ws._persist_queue = None
        agent_ws._persist_worker = None
