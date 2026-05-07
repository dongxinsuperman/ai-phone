"""Hub 单元测试：纯内存路由 + 广播。

这里用一个极小的 FakeWS，模拟 send_json 行为，不起真正的 FastAPI。
"""
from __future__ import annotations

import pytest

from ai_phone.server.hub import Hub


class FakeWS:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list = []
        self.fail = fail
        self.closed: bool = False

    async def send_json(self, payload) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append(payload)

    async def close(self, *_, **__) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_register_and_route_by_serial():
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("a1", "mac", "Darwin", ws)
    await hub.set_devices("a1", {"S1", "S2"})

    assert hub.agent_id_for_serial("S1") == "a1"
    ok = await hub.send_to_serial("S1", {"type": "start_run", "run_id": "r1"})
    assert ok and ws.sent[-1]["run_id"] == "r1"


@pytest.mark.asyncio
async def test_snapshot_includes_agent_timestamps():
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("a1", "mac", "Darwin", ws)
    before = hub.snapshot()["agents"][0]["last_seen_at"]
    hub.touch_agent("a1")
    after = hub.snapshot()["agents"][0]["last_seen_at"]
    assert after >= before
    assert hub.snapshot()["agents"][0]["connected_at"] <= after


@pytest.mark.asyncio
async def test_unregister_clears_routes():
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("a1", "mac", "Darwin", ws)
    await hub.set_devices("a1", {"S1"})
    await hub.bind_run("run-x", "a1")

    await hub.unregister_agent("a1")
    assert hub.agent_id_for_serial("S1") is None
    assert hub.agent_id_for_run("run-x") is None
    assert await hub.send_to_agent("a1", {"x": 1}) is False


@pytest.mark.asyncio
async def test_re_register_replaces_old():
    hub = Hub()
    old = FakeWS()
    new = FakeWS()
    await hub.register_agent("a1", "n", "x", old)
    await hub.set_devices("a1", {"S1"})

    await hub.register_agent("a1", "n", "x", new)
    await hub.set_devices("a1", {"S1"})
    assert old.closed is True

    ok = await hub.send_to_serial("S1", {"k": 1})
    assert ok and new.sent[-1] == {"k": 1}


@pytest.mark.asyncio
async def test_broadcast_to_subscribers():
    hub = Hub()
    w1, w2, w3 = FakeWS(), FakeWS(), FakeWS()
    await hub.subscribe("S1", w1)
    await hub.subscribe("S1", w2)
    await hub.subscribe("S2", w3)

    n = await hub.broadcast_to_serial("S1", {"type": "log", "msg": "hi"})
    assert n == 2
    assert len(w1.sent) == 1 and len(w2.sent) == 1 and len(w3.sent) == 0

    await hub.unsubscribe("S1", w1)
    n = await hub.broadcast_to_serial("S1", {"x": 1})
    assert n == 1


@pytest.mark.asyncio
async def test_broadcast_swallows_broken_sub():
    hub = Hub()
    ok_ws = FakeWS()
    bad_ws = FakeWS(fail=True)
    await hub.subscribe("S1", ok_ws)
    await hub.subscribe("S1", bad_ws)

    n = await hub.broadcast_to_serial("S1", {"a": 1})
    assert n == 1  # 仅 ok_ws 计入


@pytest.mark.asyncio
async def test_set_devices_diff_correctly():
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("a1", "n", "x", ws)
    await hub.set_devices("a1", {"S1", "S2"})
    await hub.set_devices("a1", {"S2", "S3"})  # S1 去掉，S3 加入

    assert hub.agent_id_for_serial("S1") is None
    assert hub.agent_id_for_serial("S2") == "a1"
    assert hub.agent_id_for_serial("S3") == "a1"


@pytest.mark.asyncio
async def test_bind_and_route_run():
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("a1", "n", "x", ws)
    await hub.bind_run("r1", "a1")
    assert hub.agent_id_for_run("r1") == "a1"
    ok = await hub.send_to_run("r1", {"type": "stop_run"})
    assert ok and ws.sent[-1]["type"] == "stop_run"
    await hub.unbind_run("r1")
    assert hub.agent_id_for_run("r1") is None
