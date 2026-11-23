"""Discovery fault injection: no real transport or simulator is accessed."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from adbutils import AdbDeviceInfo

from ai_phone.agent import drivers
from ai_phone.agent.drivers import android
from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.config import Settings


@pytest.fixture(autouse=True)
def isolated_scanners(monkeypatch):
    monkeypatch.setattr(drivers, "_SCAN_PENDING", {})
    monkeypatch.setattr(drivers, "_SCAN_LAST", {})
    monkeypatch.setattr(drivers, "_SCAN_OK", {})
    for name in (
        "list_android_devices", "list_ios_devices", "list_harmony_devices", "list_ios_simulators"
    ):
        monkeypatch.setattr(drivers, name, lambda **_: [])


def test_platform_scans_run_independently_but_keep_output_order(monkeypatch):
    together = threading.Barrier(4)

    def scan(platform):
        def inner(**_):
            together.wait(timeout=1)
            return [DeviceInfo(serial=platform, platform=platform)]
        return inner

    for platform, name in (
        ("android", "list_android_devices"), ("ios", "list_ios_devices"),
        ("harmony", "list_harmony_devices"), ("ios_sim", "list_ios_simulators"),
    ):
        monkeypatch.setattr(drivers, name, scan(platform))
    assert [d.platform for d in drivers.list_all_devices()] == ["android", "ios", "harmony", "ios_sim"]


@pytest.mark.parametrize("ignore_network", [False, True])
def test_agent_android_network_adb_switch(monkeypatch, ignore_network):
    env_name = "AI_PHONE_ANDROID_IGNORE_NETWORK_ADB"
    if ignore_network:
        monkeypatch.setenv(env_name, "true")
    else:
        monkeypatch.delenv(env_name, raising=False)
    settings = Settings(_env_file=None, android_setup_stay_awake=False)
    monkeypatch.setattr(drivers, "get_settings", lambda: settings)
    monkeypatch.setattr(android, "get_settings", lambda: settings)
    monkeypatch.setattr(drivers, "list_android_devices", android.list_android_devices)

    local_devices = [
        AdbDeviceInfo("usb-phone", "device", tags={"usb": "1-2"}),
        AdbDeviceInfo("emulator-5554", "device"),
        AdbDeviceInfo("usb-offline", "offline", tags={"usb": "1-3"}),
    ]
    network_devices = [
        AdbDeviceInfo("127.0.0.1:56321", "device"),
        AdbDeviceInfo("192.0.2.10:5555", "device"),
        AdbDeviceInfo("adb-wifi._adb-tls-connect._tcp", "device"),
        AdbDeviceInfo("emulator-proxy.local:5555", "offline"),
    ]
    adb_list = Mock(return_value=local_devices + network_devices)
    adb_device = Mock(side_effect=lambda serial: serial)
    monkeypatch.setattr(android.adb, "list", adb_list)
    monkeypatch.setattr(android.adb, "device", adb_device)

    def make_driver(serial, *, setup_power):
        return SimpleNamespace(device_info=lambda: DeviceInfo(serial=serial, platform="android"))

    monkeypatch.setattr(android, "AndroidDriver", make_driver)
    devices = drivers.list_all_devices(include_offline=True)
    expected = local_devices if ignore_network else local_devices + network_devices

    assert [d.serial for d in devices] == [d.serial for d in expected]
    assert adb_device.call_args_list == [call(serial=d.serial) for d in expected if d.state == "device"]
    if ignore_network:
        adb_list.assert_called_once_with(extended=True)
    else:
        adb_list.assert_called_once_with()


def test_timed_out_platform_retains_snapshot_without_repeated_threads(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    android_calls = []
    ios_calls = []
    wait_sizes = []
    original_wait = drivers.wait

    def wait_for_new_work(futures, **kwargs):
        wait_sizes.append(len(futures))
        return original_wait(futures, **kwargs)

    monkeypatch.setattr(drivers, "wait", wait_for_new_work)
    cached = DeviceInfo(serial="A1", platform="android")
    drivers._SCAN_LAST[("android", False)] = [cached]

    def android(**_):
        android_calls.append(True)
        entered.set()
        assert release.wait(2)
        return [DeviceInfo(serial="A2", platform="android")]

    def ios(**_):
        ios_calls.append(True)
        return [DeviceInfo(serial="I1", platform="ios")]

    monkeypatch.setattr(drivers, "list_android_devices", android)
    monkeypatch.setattr(drivers, "list_ios_devices", ios)
    monkeypatch.setattr(drivers, "_SCAN_BUDGET_SEC", 0.01)
    try:
        for _ in range(5):
            devices = drivers.list_all_devices()
            assert [d.serial for d in devices] == ["A1", "I1"]
            assert drivers.was_last_device_scan_ok("android") is False
            assert drivers.was_last_device_scan_ok("ios") is True
        assert entered.is_set()
        assert len(android_calls) == 1
        assert len(ios_calls) == 5
        assert wait_sizes == [4, 3, 3, 3, 3]
    finally:
        release.set()
        drivers._SCAN_PENDING["android"][1].result(timeout=1)
    assert [d.serial for d in drivers.list_all_devices()] == ["A2", "I1"]
    assert drivers.was_last_device_scan_ok("android") is True


def test_failed_scan_is_unknown_not_an_empty_success(monkeypatch):
    drivers._SCAN_LAST[("harmony", False)] = [DeviceInfo(serial="H1", platform="harmony")]

    def failed(**_):
        raise OSError("transport unavailable")

    monkeypatch.setattr(drivers, "list_harmony_devices", failed)
    assert [d.serial for d in drivers.list_all_devices()] == ["H1"]
    assert drivers.was_last_device_scan_ok("harmony") is False


def test_alternate_scan_options_do_not_duplicate_a_hung_platform(monkeypatch):
    release = threading.Event()
    calls = []

    def android(**kwargs):
        calls.append(kwargs)
        assert release.wait(2)
        return [DeviceInfo(serial="A1", platform="android")]

    monkeypatch.setattr(drivers, "list_android_devices", android)
    monkeypatch.setattr(drivers, "_SCAN_BUDGET_SEC", 0.01)
    try:
        drivers.list_all_devices()
        assert drivers.list_all_devices(include_offline=True) == []
        assert len(calls) == 1
    finally:
        release.set()
        drivers._SCAN_PENDING["android"][1].result(timeout=1)
