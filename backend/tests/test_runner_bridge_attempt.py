from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ai_phone.agent.runner.events import EVT_RUN_FINISH, EVT_STEP_END, log_event, make_event
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

