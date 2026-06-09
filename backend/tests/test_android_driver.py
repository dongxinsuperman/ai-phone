"""AndroidDriver 单元测试：用 MagicMock 注入 AdbDevice，验证命令映射是否与
Sonic AndroidTouchHandler / AndroidDeviceBridgeTool 对齐，不依赖真实设备。"""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from ai_phone.agent.drivers import android as android_mod
from ai_phone.agent.drivers.android import AndroidDriver
from ai_phone.agent.drivers.base import DeviceInfo


def _make_driver() -> tuple[AndroidDriver, MagicMock]:
    fake = MagicMock()
    fake.serial = "EMU-TEST"
    fake.window_size.return_value = MagicMock(width=1080, height=1920)
    fake.rotation.return_value = 0
    fake.getprop.side_effect = lambda k: {
        "ro.product.brand": "Xiaomi",
        "ro.product.model": "Mi 10",
        "ro.build.version.release": "12",
    }.get(k, "")
    img = Image.new("RGB", (100, 200), color=(255, 0, 0))
    fake.screenshot.return_value = img
    driver = AndroidDriver(fake)
    return driver, fake


def test_window_size_returns_tuple():
    driver, fake = _make_driver()
    assert driver.window_size() == (1080, 1920)
    fake.window_size.assert_called_once()


def test_window_size_falls_back_to_last_success():
    driver, fake = _make_driver()
    assert driver.window_size() == (1080, 1920)
    fake.window_size.side_effect = RuntimeError("rotation get failed")
    assert driver.window_size() == (1080, 1920)


def test_window_size_falls_back_to_screenshot_when_no_cache():
    fake = MagicMock()
    fake.serial = "EMU-TEST-SHOT"
    fake.window_size.side_effect = RuntimeError("rotation get failed")
    fake.rotation.return_value = 0
    fake.screenshot.return_value = Image.new("RGB", (1200, 800), color=(0, 0, 0))
    driver = AndroidDriver(fake, setup_power=False)
    assert driver.window_size() == (1200, 800)


def test_window_size_falls_back_to_wm_size_after_screenshot_failure():
    fake = MagicMock()
    fake.serial = "EMU-TEST-WM"
    fake.window_size.side_effect = RuntimeError("rotation get failed")
    fake.screenshot.side_effect = RuntimeError("screenshot failed")
    fake.shell.return_value = "Physical size: 1080x2400\n"
    fake.rotation.return_value = 0
    driver = AndroidDriver(fake, setup_power=False)
    assert driver.window_size() == (1080, 2400)


def test_window_size_wm_size_swaps_when_landscape():
    fake = MagicMock()
    fake.serial = "EMU-TEST-WM-LAND"
    fake.window_size.side_effect = RuntimeError("rotation get failed")
    fake.screenshot.side_effect = RuntimeError("screenshot failed")
    fake.shell.return_value = "Physical size: 1080x2400\n"
    fake.rotation.return_value = 1
    driver = AndroidDriver(fake, setup_power=False)
    assert driver.window_size() == (2400, 1080)


def test_click_maps_to_adb_click():
    driver, fake = _make_driver()
    driver.click(100, 200)
    fake.click.assert_called_once_with(100, 200)


def test_long_press_uses_swipe_with_duration_seconds():
    driver, fake = _make_driver()
    driver.long_press(50, 60, duration_ms=1500)
    fake.swipe.assert_called_once_with(50, 60, 50, 60, duration=1.5)


def test_swipe_converts_ms_to_seconds():
    driver, fake = _make_driver()
    driver.swipe(10, 20, 30, 40, duration_ms=500)
    fake.swipe.assert_called_once_with(10, 20, 30, 40, duration=0.5)


def test_press_home_and_back_use_keyevent():
    driver, fake = _make_driver()
    driver.press_home()
    driver.press_back()
    fake.keyevent.assert_any_call(3)
    fake.keyevent.assert_any_call(4)


def test_prepare_for_run_wakes_and_dismisses_keyguard(monkeypatch):
    fake = MagicMock()
    fake.serial = "EMU-WAKE"
    driver = AndroidDriver(fake, setup_power=False)
    monkeypatch.setattr(
        android_mod,
        "get_settings",
        lambda: SimpleNamespace(
            android_wake_before_run_settle_ms=0,
        ),
    )

    driver.prepare_for_run()

    fake.keyevent.assert_called_once_with(224)
    fake.shell.assert_called_once_with("wm dismiss-keyguard")


def test_prepare_for_run_swallows_wake_errors(monkeypatch):
    fake = MagicMock()
    fake.serial = "EMU-WAKE-ERR"
    fake.keyevent.side_effect = RuntimeError("adb offline")
    driver = AndroidDriver(fake, setup_power=False)
    monkeypatch.setattr(
        android_mod,
        "get_settings",
        lambda: SimpleNamespace(android_wake_before_run_settle_ms=0),
    )

    driver.prepare_for_run()

    fake.keyevent.assert_called_once_with(224)
    fake.shell.assert_called_once_with("wm dismiss-keyguard")


def test_open_android_driver_respects_setup_stay_awake_env(monkeypatch):
    fake = MagicMock()
    fake.serial = "EMU-POWER-OFF"
    monkeypatch.setattr(android_mod.adb, "device", lambda serial: fake)
    monkeypatch.setattr(
        android_mod,
        "get_settings",
        lambda: SimpleNamespace(android_setup_stay_awake=False),
    )

    driver = android_mod.open_android_driver("EMU-POWER-OFF")

    assert isinstance(driver, AndroidDriver)
    fake.shell.assert_not_called()


def test_terminate_app_uses_am_force_stop():
    driver, fake = _make_driver()
    driver.terminate_app("com.tencent.mm")
    fake.shell.assert_called_with("am force-stop com.tencent.mm")


def test_activate_app_real_device_uses_monkey_first():
    # 真机（serial 非 emulator-）：维持原 monkey 优先逻辑，零行为变化
    driver, fake = _make_driver()  # _make_driver 默认 serial=EMU-TEST → 真机路径
    fake.shell.return_value = "Events injected: 1\n## Network stats"
    driver.activate_app("com.tencent.mm")
    first = fake.shell.call_args_list[0][0][0]
    assert "monkey -p com.tencent.mm" in first


def test_activate_app_real_device_raises_when_no_activity():
    # 真机原逻辑：monkey aborted + app_info 取不到 → 抛错（与历史一致）
    driver, fake = _make_driver()
    fake.shell.return_value = "** No activities found to run, monkey aborted."
    fake.app_info.side_effect = Exception("not installed")
    with pytest.raises(RuntimeError) as exc:
        driver.activate_app("com.not.exist")
    assert "com.not.exist" in str(exc.value)


def test_activate_app_emulator_uses_am_start_and_verifies_foreground():
    # 虚拟机（serial emulator-）：解析组件 + am start，并确认前台真的切到目标
    driver, fake = _make_driver()
    driver.serial = "emulator-5554"  # 切到虚拟机路径

    def _shell(cmd, *a, **k):
        if "resolve-activity" in cmd:
            return "com.tencent.mm/.ui.LauncherUI"
        if cmd.startswith("am start"):
            return "Starting: Intent { cmp=com.tencent.mm/.ui.LauncherUI }"
        return ""

    fake.shell.side_effect = _shell
    fake.app_current.return_value = SimpleNamespace(package="com.tencent.mm")

    driver.activate_app("com.tencent.mm")

    calls = [c[0][0] for c in fake.shell.call_args_list]
    assert any("am start -n com.tencent.mm/.ui.LauncherUI" in c for c in calls)
    assert not any(c.startswith("monkey") for c in calls)  # 前台已确认，不走 monkey 兜底


def test_activate_app_emulator_raises_when_foreground_never_matches():
    # 虚拟机：am start / monkey 都没把目标拉到前台 → 抛错，杜绝假成功
    driver, fake = _make_driver()
    driver.serial = "emulator-5554"
    fake.shell.return_value = ""
    fake.app_info.side_effect = Exception("not installed")
    driver._wait_foreground = lambda *a, **k: False  # 前台始终不是目标
    with pytest.raises(RuntimeError) as exc:
        driver.activate_app("com.not.exist")
    assert "com.not.exist" in str(exc.value)


def test_list_third_party_packages_parses_output():
    driver, fake = _make_driver()
    fake.shell.return_value = (
        "package:com.tencent.mm\n"
        "package:com.alibaba.android.rimet\n"
        "\n"
        "package:com.example.foo"
    )
    pkgs = driver.list_third_party_packages()
    assert pkgs == [
        "com.tencent.mm",
        "com.alibaba.android.rimet",
        "com.example.foo",
    ]


def test_screenshot_png_returns_png_bytes():
    driver, _ = _make_driver()
    data = driver.screenshot_png()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_screenshot_jpeg_returns_jpeg_and_respects_max_side():
    driver, _ = _make_driver()
    data = driver.screenshot_jpeg(quality=25, max_side=50)
    # JPEG magic
    assert data[:3] == b"\xff\xd8\xff"
    img = Image.open(io.BytesIO(data))
    assert max(img.size) <= 50


def test_device_info_reads_props_and_size():
    driver, _ = _make_driver()
    info = driver.device_info()
    assert isinstance(info, DeviceInfo)
    assert info.serial == "EMU-TEST"
    assert info.platform == "android"
    assert info.brand == "Xiaomi"
    assert info.model == "Mi 10"
    assert info.os_version == "12"
    assert info.screen_width == 1080
    assert info.screen_height == 1920
    assert info.status == "online"


def test_scroll_down_direction_swipes_from_bottom_to_top():
    driver, fake = _make_driver()
    driver.scroll("down")
    fake.swipe.assert_called_once()
    (sx, sy, ex, ey) = fake.swipe.call_args[0][:4]
    # 新语义：down = 向下浏览看下方内容 → 手指由下往上，所以 sy > ey
    assert sx == ex
    assert sy > ey


def test_scroll_up_inverts_direction():
    driver, fake = _make_driver()
    driver.scroll("up")
    (sx, sy, ex, ey) = fake.swipe.call_args[0][:4]
    # 新语义：up = 向上回顶看上方内容 → 手指由上往下，所以 sy < ey
    assert sx == ex
    assert sy < ey


def test_type_text_noop_on_empty():
    driver, fake = _make_driver()
    driver.type_text("")
    fake.send_keys.assert_not_called()


def test_type_text_sends_ascii():
    driver, fake = _make_driver()
    driver.type_text("hello")
    fake.send_keys.assert_called_once_with("hello")
