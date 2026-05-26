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
