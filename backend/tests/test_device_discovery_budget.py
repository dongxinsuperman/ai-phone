"""Discovery fault injection: no real transport or simulator is accessed."""
import threading

import pytest

from ai_phone.agent import drivers
from ai_phone.agent.drivers.base import DeviceInfo


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
