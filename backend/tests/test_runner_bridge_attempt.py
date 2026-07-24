from __future__ import annotations

from typing import Any, Dict, List

import pytest

import asyncio

from ai_phone.agent.runner.events import (
    EVT_RUN_FINISH,
    EVT_SCREENSHOT,
    EVT_STEP_END,
    log_event,
    make_event,
)
from ai_phone.agent.runner_bridge import RunnerBridge


@pytest.mark.asyncio
async def test_runner_bridge_forwards_attempt_on_agent_brain_events():
    sent: List[Dict[str, Any]] = []

    async def _send(payload: Dict[str, Any]) -> bool:
        sent.append(dict(payload))
        return True

    bridge = RunnerBridge(
        run_id="attempt-run",
        serial="S1",
        ws_send=_send,
        server_http_base="http://test",
        attempt=2,
    )

    bridge.emit(log_event("attempt-run", 1, "log", "hello"))
    bridge.emit(
        make_event(
            EVT_STEP_END,
            "attempt-run",
            step=1,
            thought="done",
            action="click(point='<point>1 1</point>')",
            action_type="click",
            elapsed_ms=12,
        )
    )
    bridge.emit(
        make_event(
            EVT_RUN_FINISH,
            "attempt-run",
            ok=True,
            reason="finished: done",
            steps=1,
            elapsed_ms=34,
            token_stats={"total_tokens": 1},
        )
    )
    await bridge.aclose()

    assert [payload["type"] for payload in sent] == ["log", "step_done", "run_done"]
    assert all(payload["attempt"] == 2 for payload in sent)


@pytest.mark.asyncio
async def test_runner_bridge_runs_final_hook_before_run_done_once():
    sent: List[Dict[str, Any]] = []
    order: List[str] = []

    async def _send(payload: Dict[str, Any]) -> bool:
        order.append(payload["type"])
        sent.append(dict(payload))
        return True

    async def _sleep() -> None:
        order.append("sleep")

    bridge = RunnerBridge(
        run_id="sleep-run",
        serial="S1",
        ws_send=_send,
        server_http_base="http://test",
        before_run_done=_sleep,
    )
    await bridge.send_run_done({"type": "run_done", "run_id": "sleep-run"})
    await bridge.send_run_done({"type": "run_done", "run_id": "sleep-run"})
    await bridge.aclose()

    assert order == ["sleep", "run_done", "run_done"]
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_runner_bridge_keeps_step_end_before_next_step_log_under_slow_upload(
    monkeypatch,
):
    """回归：截图上传慢时，本步 step_done 仍排在下一步日志之前（不被插队）。

    旧并发模型下，after 截图 task 上传慢、step_end task 等它，而下一步「━━ 第 N 步 ━━」
    日志 task 立即入队 → web 实时日志里「上一步完成」被甩到「下一步开始」之后。串行
    链修复后，入可靠队列顺序严格 == emit 调用顺序，本用例守住这条不回退。
    """
    sent: List[Dict[str, Any]] = []

    async def _send(payload: Dict[str, Any]) -> bool:
        sent.append(dict(payload))
        return True

    async def _slow_upload(self, data, step, phase, *, attempts=3):  # noqa: ANN001
        await asyncio.sleep(0.05)  # 模拟截图上传慢（比轻量日志慢）
        return f"http://u/{step}-{phase}.jpg"

    monkeypatch.setattr(RunnerBridge, "_upload_with_retry", _slow_upload)

    bridge = RunnerBridge(
        run_id="r", serial="S1", ws_send=_send, server_http_base="http://test",
    )
    # emit 顺序：step1 的 after 截图（慢上传）→ step1 完成 → step2 开始日志（轻量、快）
    bridge.emit(make_event(EVT_SCREENSHOT, "r", step=1, phase="after", bytes=b"x", ts=1))
    bridge.emit(
        make_event(
            EVT_STEP_END, "r", step=1, thought="t",
            action="click(point='<point>1 1</point>')", action_type="click", elapsed_ms=1,
        )
    )
    bridge.emit(log_event("r", 1, "━━ 第 2 步 ━━", "段=1", step=2))
    await bridge.aclose()

    types_in_order = [p["type"] for p in sent]
    step_done_idx = types_in_order.index("step_done")
    next_log_idx = next(
        i for i, p in enumerate(sent) if p["type"] == "log" and p.get("step") == 2
    )
    assert step_done_idx < next_log_idx, types_in_order
