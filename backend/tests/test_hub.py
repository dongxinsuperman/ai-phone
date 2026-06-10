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
async def test_unregister_ignores_stale_connection_ws():
    """同 id 重连后，被替换的旧连接（带旧 ws）调 unregister 不应误删新连接。

    这是断连孤儿回收的前置正确性：新连接仍在线 → has_agent 仍 True → 回收旁路
    才能正确判定"同进程已重连、不该回收在跑的 Run"。
    """
    hub = Hub()
    old = FakeWS()
    new = FakeWS()
    await hub.register_agent("a1", "n", "x", old)
    await hub.set_devices("a1", {"S1"})
    # 同 id 重连：当前登记被替换为 new
    await hub.register_agent("a1", "n", "x", new)
    await hub.set_devices("a1", {"S1"})

    # 旧连接的 finally 带着旧 ws 调 unregister —— 必须被身份校验拦下，不动新连接路由
    conn = await hub.unregister_agent("a1", ws=old)
    assert conn is None
    assert hub.has_agent("a1") is True
    assert hub.agent_id_for_serial("S1") == "a1"
    ok = await hub.send_to_serial("S1", {"k": 1})
    assert ok and new.sent[-1] == {"k": 1}


@pytest.mark.asyncio
async def test_unregister_matching_ws_clears():
    """当前连接自己断开（ws 匹配）正常注销并清路由。"""
    hub = Hub()
    ws = FakeWS()
    await hub.register_agent("a1", "n", "x", ws)
    await hub.set_devices("a1", {"S1"})
    await hub.bind_run("r1", "a1")

    conn = await hub.unregister_agent("a1", ws=ws)
    assert conn is not None
    assert hub.has_agent("a1") is False
    assert hub.agent_id_for_serial("S1") is None
    assert hub.agent_id_for_run("r1") is None


@pytest.mark.asyncio
async def test_reregister_migrates_run_routing():
    """同 id 重连：在飞 Run 的路由要迁移到新连接，不丢 run 管理权。

    覆盖 Codex 指出的缺口：register_agent 替换旧连接时若把 run_ids 清掉、不迁移，
    会导致 stop_run 发不到、且新连接再断时孤儿回收抓不到这条仍在跑的 Run。
    """
    hub = Hub()
    old = FakeWS()
    new = FakeWS()
    await hub.register_agent("a1", "n", "x", old)
    await hub.bind_run("r1", "a1")

    # 同 id 重连（网络抖动、同进程）
    await hub.register_agent("a1", "n", "x", new)

    # 1) run→agent 路由仍在，且 send_to_run 能发到"新" ws
    assert hub.agent_id_for_run("r1") == "a1"
    ok = await hub.send_to_run("r1", {"type": "stop_run", "run_id": "r1"})
    assert ok and new.sent[-1]["type"] == "stop_run"

    # 2) 新连接随后断开时，_on_disconnect 能从 conn.run_ids 拿到这条 Run 去回收
    conn = await hub.unregister_agent("a1", ws=new)
    assert conn is not None and "r1" in conn.run_ids


@pytest.mark.asyncio
async def test_register_picks_up_runs_bound_while_offline():
    """重启恢复时 bind_run 发生在 Agent 还没连上之时（conn.run_ids 写不上）；

    待 Agent 连上、register_agent 建新连接时，应把 _run_to_agent 里所有指向该 agent_id
    的 run 并入新 conn.run_ids —— 否则该连接日后断开时 _on_disconnect 取不到这些 run，
    孤儿回收会漏掉（Codex P1）。
    """
    hub = Hub()
    # 模拟恢复：Agent 尚未连上就 bind（只写 _run_to_agent，conn 不存在）
    await hub.bind_run("R1", "a1")
    assert hub.agent_id_for_run("R1") == "a1"

    # Agent 连上
    ws = FakeWS()
    await hub.register_agent("a1", "n", "x", ws)

    # 这条 run 应被并入新连接的 run_ids
    conn = await hub.unregister_agent("a1", ws=ws)
    assert conn is not None
    assert "R1" in conn.run_ids


@pytest.mark.asyncio
async def test_has_agent_reflects_registration():
    hub = Hub()
    ws = FakeWS()
    assert hub.has_agent("a1") is False
    await hub.register_agent("a1", "n", "x", ws)
    assert hub.has_agent("a1") is True
    await hub.unregister_agent("a1", ws=ws)
    assert hub.has_agent("a1") is False


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
