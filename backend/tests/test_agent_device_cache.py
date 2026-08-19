from types import SimpleNamespace

import pytest

from ai_phone.agent import main as agent_main
from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.health import supervisor as supervisor_mod
from ai_phone.agent.health.probe import ProbeOutcome
from ai_phone.agent.health.supervisor import ReadinessSupervisor


def test_record_serial_platform_prunes_stale_devices():
    agent_main._serial_platform.clear()
    agent_main._serial_screen_size.clear()
    agent_main._serial_product_type.clear()
    try:
        agent_main._serial_platform.update({"OLD": "android", "S1": "android"})
        agent_main._serial_screen_size.update({"OLD": (1, 1), "S1": (720, 1280)})
        agent_main._serial_product_type.update({"OLD": "old-model", "S1": "old"})

        agent_main._record_serial_platform(
            [
                DeviceInfo(
                    serial="S1",
                    platform="android",
                    model="new-model",
                    screen_width=1080,
                    screen_height=2400,
                )
            ]
        )

        assert agent_main._serial_platform == {"S1": "android"}
        assert agent_main._serial_screen_size == {"S1": (1080, 2400)}
        assert agent_main._serial_product_type == {"S1": "new-model"}
    finally:
        agent_main._serial_platform.clear()
        agent_main._serial_screen_size.clear()
        agent_main._serial_product_type.clear()


def test_harmony_snapshot_keeps_single_scan_miss_then_removes_at_threshold():
    agent_main._reset_harmony_snapshot_for_tests()
    try:
        h1 = DeviceInfo(serial="H1", platform="harmony", model="old-h1")
        h2 = DeviceInfo(serial="H2", platform="harmony", model="old-h2")
        first = agent_main._apply_harmony_snapshot_debounce([h1, h2])
        assert {d.serial for d in first} == {"H1", "H2"}
        assert agent_main._is_harmony_snapshot_stale("H1") is False
        assert agent_main._is_harmony_snapshot_stale("H2") is False

        # 本轮只漏 H1：H1 用快照保留，H2 必须使用当前新数据。
        h2_new = DeviceInfo(serial="H2", platform="harmony", model="new-h2")
        second = agent_main._apply_harmony_snapshot_debounce([h2_new])
        by_serial = {d.serial: d for d in second}
        assert set(by_serial) == {"H1", "H2"}
        assert by_serial["H2"].model == "new-h2"
        assert agent_main._is_harmony_snapshot_stale("H1") is True
        assert agent_main._is_harmony_snapshot_stale("H2") is False

        # 第 2 轮缺失仍保留；连续第 3 轮才确认真正移除。
        third = agent_main._apply_harmony_snapshot_debounce([h2_new])
        assert {d.serial for d in third} == {"H1", "H2"}
        assert agent_main._is_harmony_snapshot_stale("H1") is True
        fourth = agent_main._apply_harmony_snapshot_debounce([h2_new])
        assert {d.serial for d in fourth} == {"H2"}
        assert agent_main._is_harmony_snapshot_stale("H1") is False
    finally:
        agent_main._reset_harmony_snapshot_for_tests()


@pytest.mark.asyncio
async def test_retained_harmony_snapshot_stops_dispatch_after_first_probe_failure(
    monkeypatch,
):
    """真拔线时卡片可防抖保留，但第一次定向 probe 失败就必须 ready=False。"""

    class Probe:
        outcome = ProbeOutcome(ready=True)

        async def probe(self):
            return self.outcome

    visible = []
    sent = []

    async def sender(msg):
        sent.append(dict(msg))
        return True

    monkeypatch.setattr(
        supervisor_mod,
        "get_settings",
        lambda: SimpleNamespace(
            readiness_probe_timeout_sec=3.0,
            readiness_fail_threshold=3,
        ),
    )
    monkeypatch.setattr(
        supervisor_mod,
        "build_probe_for",
        lambda _platform, _serial, timeout_sec: Probe(),  # noqa: ARG005
    )

    agent_main._reset_harmony_snapshot_for_tests()
    try:
        visible[:] = agent_main._apply_harmony_snapshot_debounce(
            [DeviceInfo(serial="H1", platform="harmony")]
        )
        readiness = ReadinessSupervisor(
            device_lister=lambda: [(d.serial, d.platform) for d in visible],
            send_message=sender,
            scan_stale=lambda serial, platform: (
                platform == "harmony"
                and agent_main._is_harmony_snapshot_stale(serial)
            ),
        )
        await readiness._tick_once()
        assert readiness._states[("H1", "harmony")].ready is True

        # 全局扫描漏了设备，但定向 probe 仍通：卡片和派单资格都保留。
        visible[:] = agent_main._apply_harmony_snapshot_debounce([])
        await readiness._tick_once()
        assert agent_main._is_harmony_snapshot_stale("H1") is True
        assert readiness._states[("H1", "harmony")].ready is True

        # 设备真拔线：定向 probe 也失败，必须立即停止派单。
        Probe.outcome = ProbeOutcome(
            ready=False,
            not_ready_reason="driver_probe_failed",
            hint="device disconnected",
        )
        await readiness._tick_once()

        state = readiness._states[("H1", "harmony")]
        assert visible, "第一轮漏扫时 UI 快照应仍保留"
        assert state.consecutive_fail == 1
        assert state.ready is False, "缓存设备不得借失败阈值继续派单"
        assert sent[-1]["ready"] is False
    finally:
        agent_main._reset_harmony_snapshot_for_tests()


def test_harmony_snapshot_debounce_does_not_touch_other_platforms():
    agent_main._reset_harmony_snapshot_for_tests()
    try:
        android = DeviceInfo(serial="A1", platform="android")
        ios = DeviceInfo(serial="I1", platform="ios")
        out = agent_main._apply_harmony_snapshot_debounce([android, ios])
        assert out == [android, ios]
    finally:
        agent_main._reset_harmony_snapshot_for_tests()
