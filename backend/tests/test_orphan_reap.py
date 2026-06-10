"""断连孤儿 Run 回收旁路的决策门测试（不依赖 DB）。

只验证"什么时候该回收 / 什么时候该跳过"这层新增的判定逻辑：
- 同 agent_id 在宽限期后仍在线（同进程网络抖动已重连）→ 跳过，绝不回收在跑的 Run
- 该 agent_id 宽限期后确实不在线（进程重启 / 真死）→ 对其名下每条 Run 触发回收

单条 Run 的落终态本身复用既有 ``_finalize_run`` + ``scheduler.on_run_done``（已有
run_done 测试 + 真机端到端验证覆盖），这里用桩替换以隔离决策逻辑。
"""
from __future__ import annotations

import pytest

from ai_phone.server.hub import Hub
from ai_phone.server.ws import agent_ws


class _FakeWS:
    async def send_json(self, payload) -> None:  # pragma: no cover - 不触发
        pass

    async def close(self, *_, **__) -> None:  # pragma: no cover - 不触发
        pass


@pytest.mark.asyncio
async def test_reap_skips_when_same_agent_reconnected(monkeypatch):
    """宽限期后同 agent_id 仍在线 → 跳过回收（不误杀仍在本地执行的 Run）。"""
    hub = Hub()
    await hub.register_agent("a1", "n", "x", _FakeWS())  # 同 id 已（重）连在线

    reaped: list[str] = []

    async def _fake_reap_one(_hub, run_id):
        reaped.append(run_id)

    monkeypatch.setattr(agent_ws, "_reap_one_orphan_run", _fake_reap_one)

    await agent_ws._reap_disconnected_runs(hub, "a1", {"r1", "r2"}, grace_sec=0.0)

    assert reaped == []  # has_agent("a1") 为 True → 一条都不回收


@pytest.mark.asyncio
async def test_reap_fires_when_agent_absent(monkeypatch):
    """宽限期后该 agent_id 不在线 → 对其名下每条 Run 触发回收。"""
    hub = Hub()  # 空：a1 没有重连回来

    reaped: list[str] = []

    async def _fake_reap_one(_hub, run_id):
        reaped.append(run_id)

    monkeypatch.setattr(agent_ws, "_reap_one_orphan_run", _fake_reap_one)

    await agent_ws._reap_disconnected_runs(hub, "a1", {"r1", "r2"}, grace_sec=0.0)

    assert sorted(reaped) == ["r1", "r2"]


@pytest.mark.asyncio
async def test_reap_noop_on_empty_run_set(monkeypatch):
    """没有在飞 Run 时直接返回，不做任何事。"""
    hub = Hub()
    reaped: list[str] = []

    async def _fake_reap_one(_hub, run_id):
        reaped.append(run_id)

    monkeypatch.setattr(agent_ws, "_reap_one_orphan_run", _fake_reap_one)

    await agent_ws._reap_disconnected_runs(hub, "a1", set(), grace_sec=0.0)

    assert reaped == []
