"""M4 片3a：Agent 侧 V3 缓存回放编排（run_v3_replay）单测。

验证命中 V3 缓存后的本地回放编排三条终态分支（不依赖真机 / 真 VLM，全 mock）：
- 回放成功 + 断言 PASS → run_done(result=pass, trajectory_cache_v3_pass)，不标 suspect；
- 回放失败 → 发 MSG_CACHE_SUSPECT + run_done(result=error)；
- 回放成功但断言 FAIL → 发 MSG_CACHE_SUSPECT + run_done(result=assert_fail)。
"""
from __future__ import annotations

import pytest

from ai_phone.agent.trajectory_cache import orchestrate
from ai_phone.agent.trajectory_cache.assertion import CacheAssertionResult
from ai_phone.agent.trajectory_cache.replay import ReplayResult


class _FakeBridge:
    def __init__(self) -> None:
        self.run_done: list = []
        self.suspects: list = []
        self.logs: list = []

    def emit(self, evt) -> None:
        self.logs.append(evt)

    async def send_run_done(self, payload) -> None:
        self.run_done.append(dict(payload))

    async def send_cache_suspect(self, payload) -> None:
        self.suspects.append(dict(payload))


def _make_runner(result, *, final_frame=b"\xff\xd8final"):
    class _FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self):
            return result

        async def capture_final_frame(self):
            return final_frame

    return _FakeRunner


def _make_verifier(assertion):
    class _FakeVerifier:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def verify(self, **kwargs):
            return assertion

    return _FakeVerifier


_SNAPSHOT = {
    "cache_mode": "v3",
    "schema_version": 3,
    "cache_key": "ck-v3-abcdef0123456789",
    "actions": [{"type": "click", "plan_intent": "tap search", "action_id": "a1"}],
    "source_completion": {"task_done": True},
    "meta": {"plan": "x"},
    "source_vlm_backend": "doubao_responses",
}


class _Settings:
    vlm_backend = "doubao_responses"


def _patch(monkeypatch, *, replay_result, assertion):
    monkeypatch.setattr(
        "ai_phone.agent.trajectory_cache.v3_replay.V3ReplayRunner",
        _make_runner(replay_result),
    )
    monkeypatch.setattr(
        "ai_phone.agent.trajectory_cache.assertion.CacheReplayAssertionVerifier",
        _make_verifier(assertion),
    )


@pytest.mark.asyncio
async def test_v3_replay_success_then_assertion_pass(monkeypatch):
    _patch(
        monkeypatch,
        replay_result=ReplayResult(
            success=True, actions_total=1, actions_executed=1, elapsed_ms=1234,
            final_before_bytes=b"before",
        ),
        assertion=CacheAssertionResult("PASS", "看到结果页"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v3_replay(
        run_id="r1", serial="S1", goal="打开微信", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT, settings=_Settings(),
    )
    assert len(bridge.run_done) == 1
    done = bridge.run_done[0]
    assert done["result"] == "pass"
    assert done["message"].startswith("trajectory_cache_v3_pass:")
    assert done["steps"] == 1
    assert bridge.suspects == []  # 成功不标 suspect


@pytest.mark.asyncio
async def test_v3_replay_failure_marks_suspect_and_errors(monkeypatch):
    _patch(
        monkeypatch,
        replay_result=ReplayResult(
            success=False, actions_total=2, actions_executed=1, failed_index=1,
            error="locator_miss: 找不到搜索框", elapsed_ms=500,
        ),
        assertion=CacheAssertionResult("PASS", "不该被调用"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v3_replay(
        run_id="r2", serial="S1", goal="打开微信", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT, settings=_Settings(),
    )
    assert len(bridge.run_done) == 1
    done = bridge.run_done[0]
    assert done["result"] == "error"
    assert "trajectory_cache_v3_replay_failed" in done["message"]
    assert len(bridge.suspects) == 1
    sus = bridge.suspects[0]
    assert sus["type"] == "cache_suspect"
    assert sus["cache_key"] == _SNAPSHOT["cache_key"]
    assert sus["cache_mode"] == "v3"


@pytest.mark.asyncio
async def test_v3_replay_success_but_assertion_fail_marks_suspect(monkeypatch):
    _patch(
        monkeypatch,
        replay_result=ReplayResult(
            success=True, actions_total=1, actions_executed=1, elapsed_ms=900,
            final_before_bytes=b"before",
        ),
        assertion=CacheAssertionResult("FAIL", "没看到结果"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v3_replay(
        run_id="r3", serial="S1", goal="打开微信", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT, settings=_Settings(),
    )
    assert len(bridge.run_done) == 1
    done = bridge.run_done[0]
    assert done["result"] == "assert_fail"
    assert "assertion_fail" in done["message"]
    assert len(bridge.suspects) == 1
    assert bridge.suspects[0]["reason"].startswith("assertion_fail")


_SNAPSHOT_V2 = {
    "cache_mode": "v2",
    "cache_key": "ck-v2-abcdef0123456789",
    "actions": [{"type": "click", "point": {"x": 5, "y": 6}, "action_id": "a1"}],
    "state_landmarks": [],  # 空 → 跳过预取，不触发 httpx
    "source_completion": {"task_done": True},
    "source_vlm_backend": "doubao_responses",
}


def _patch_v2(monkeypatch, *, replay_result, assertion):
    monkeypatch.setattr(
        "ai_phone.agent.trajectory_cache.replay.V2ReplayRunner",
        _make_runner(replay_result),
    )
    monkeypatch.setattr(
        "ai_phone.agent.trajectory_cache.assertion.CacheReplayAssertionVerifier",
        _make_verifier(assertion),
    )


@pytest.mark.asyncio
async def test_v2_replay_success_then_assertion_pass(monkeypatch):
    _patch_v2(
        monkeypatch,
        replay_result=ReplayResult(
            success=True, actions_total=1, actions_executed=1, elapsed_ms=800,
            final_before_bytes=b"before",
        ),
        assertion=CacheAssertionResult("PASS", "看到结果页"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v2_replay(
        run_id="v2r1", serial="S1", goal="打开设置", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT_V2, settings=_Settings(),
        server_http_base="http://test",
    )
    assert len(bridge.run_done) == 1
    done = bridge.run_done[0]
    assert done["result"] == "pass"
    assert done["message"].startswith("trajectory_cache_pass:")
    assert bridge.suspects == []  # V2 不发 suspect（失败靠删）


@pytest.mark.asyncio
async def test_v2_replay_alignment_miss_assert_fail(monkeypatch):
    _patch_v2(
        monkeypatch,
        replay_result=ReplayResult(
            success=False, actions_total=2, actions_executed=1, failed_index=1,
            error="alignment_miss: 路标对不上", elapsed_ms=400,
        ),
        assertion=CacheAssertionResult("PASS", "不该被调用"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v2_replay(
        run_id="v2r2", serial="S1", goal="打开设置", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT_V2, settings=_Settings(),
        server_http_base="http://test",
    )
    done = bridge.run_done[0]
    assert done["result"] == "assert_fail"
    assert "trajectory_cache_alignment_fail" in done["message"]
    assert bridge.suspects == []  # V2 失败删、不标 suspect


@pytest.mark.asyncio
async def test_v2_replay_other_failure_errors(monkeypatch):
    _patch_v2(
        monkeypatch,
        replay_result=ReplayResult(
            success=False, actions_total=2, actions_executed=1, failed_index=1,
            error="driver_error: 点击失败", elapsed_ms=300,
        ),
        assertion=CacheAssertionResult("PASS", "不该被调用"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v2_replay(
        run_id="v2r3", serial="S1", goal="打开设置", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT_V2, settings=_Settings(),
        server_http_base="http://test",
    )
    done = bridge.run_done[0]
    assert done["result"] == "error"
    assert "trajectory_replay_failed" in done["message"]
    assert bridge.suspects == []


_SNAPSHOT_V1 = {
    "cache_mode": "v1",
    "cache_key": "ck-v1-abcdef0123456789",
    "actions": [{"type": "click", "point": {"x": 5, "y": 6}, "action_id": "a1"}],
    "source_completion": {"task_done": True},
}


def _patch_v1(monkeypatch, *, replay_result, assertion):
    monkeypatch.setattr(
        "ai_phone.agent.trajectory_cache.replay.V1ReplayRunner",
        _make_runner(replay_result),
    )
    monkeypatch.setattr(
        "ai_phone.agent.trajectory_cache.assertion.CacheReplayAssertionVerifier",
        _make_verifier(assertion),
    )


@pytest.mark.asyncio
async def test_v1_replay_success_then_assertion_pass(monkeypatch):
    _patch_v1(
        monkeypatch,
        replay_result=ReplayResult(
            success=True, actions_total=1, actions_executed=1, elapsed_ms=300,
            final_before_bytes=b"before",
        ),
        assertion=CacheAssertionResult("PASS", "看到结果页"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v1_replay(
        run_id="v1r1", serial="S1", goal="打开设置", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT_V1, settings=_Settings(),
    )
    done = bridge.run_done[0]
    assert done["result"] == "pass"
    assert done["message"].startswith("trajectory_cache_pass:")
    assert bridge.suspects == []  # V1 不发 suspect（失败靠删）


@pytest.mark.asyncio
async def test_v1_replay_failure_errors(monkeypatch):
    _patch_v1(
        monkeypatch,
        replay_result=ReplayResult(
            success=False, actions_total=1, actions_executed=0,
            error="driver_error: 点击失败", elapsed_ms=100,
        ),
        assertion=CacheAssertionResult("PASS", "不该被调用"),
    )
    bridge = _FakeBridge()
    await orchestrate.run_v1_replay(
        run_id="v1r2", serial="S1", goal="打开设置", attempt=1,
        driver=object(), bridge=bridge, snapshot=_SNAPSHOT_V1, settings=_Settings(),
    )
    done = bridge.run_done[0]
    assert done["result"] == "error"
    assert "trajectory_replay_failed" in done["message"]
    assert bridge.suspects == []


@pytest.mark.asyncio
async def test_is_cache_hit_helpers():
    assert orchestrate.is_v3_cache_hit({"cache_mode": "v3"}) is True
    assert orchestrate.is_v2_cache_hit({"cache_mode": "v2"}) is True
    assert orchestrate.is_v1_cache_hit({"cache_mode": "v1"}) is True
    assert orchestrate.is_v1_cache_hit({"cache_mode": "v2"}) is False
    assert orchestrate.is_v3_cache_hit({"cache_mode": "v2"}) is False
    assert orchestrate.is_v2_cache_hit({"cache_mode": "v1"}) is False
    assert orchestrate.is_v1_cache_hit(None) is False
