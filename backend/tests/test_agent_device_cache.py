from ai_phone.agent import main as agent_main
from ai_phone.agent.drivers.base import DeviceInfo


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

        # 本轮只漏 H1：H1 用快照保留，H2 必须使用当前新数据。
        h2_new = DeviceInfo(serial="H2", platform="harmony", model="new-h2")
        second = agent_main._apply_harmony_snapshot_debounce([h2_new])
        by_serial = {d.serial: d for d in second}
        assert set(by_serial) == {"H1", "H2"}
        assert by_serial["H2"].model == "new-h2"

        # 第 2 轮缺失仍保留；连续第 3 轮才确认真正移除。
        third = agent_main._apply_harmony_snapshot_debounce([h2_new])
        assert {d.serial for d in third} == {"H1", "H2"}
        fourth = agent_main._apply_harmony_snapshot_debounce([h2_new])
        assert {d.serial for d in fourth} == {"H2"}
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
