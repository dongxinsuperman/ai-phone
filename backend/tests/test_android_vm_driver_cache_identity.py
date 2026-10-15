"""Managed Android cache identity regressions; all drivers/devices are isolated fakes."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent import main as agent_main
from ai_phone.agent import drivers as drivers_package
from ai_phone.agent.drivers import android as android_module
from ai_phone.agent.drivers.base import DeviceInfo


SERIAL = "emulator-5554"


@pytest.fixture
def cache_environment(monkeypatch):
    for name in ("_driver_cache", "_serial_platform", "_serial_screen_size", "_serial_product_type", "_serial_vm_ids", "_serial_lifecycle_epochs"):
        monkeypatch.setattr(agent_main, name, {})
    for name in ("_android_vm_cache_identities", "_android_vm_seen_identities"):
        if hasattr(agent_main, name):
            monkeypatch.setattr(agent_main, name, {})
    monkeypatch.setattr(android_module, "_STAY_AWAKE_DONE", set())
    monkeypatch.setattr(agent_main, "get_settings", lambda: SimpleNamespace(android_setup_stay_awake=True))
    power_calls = []
    opened = []

    def setup_power(driver):
        power_calls.append((driver.serial, driver._device.vm_id))

    def open_driver(serial, platform, **kwargs):
        assert platform == "android"
        device = SimpleNamespace(
            serial=serial, vm_id=agent_main._serial_vm_ids.get(serial, ""),
            epoch=agent_main._serial_lifecycle_epochs.get(serial, 0),
        )
        driver = android_module.AndroidDriver(device, setup_power=kwargs.get("setup_power", True))
        opened.append(driver)
        return driver

    monkeypatch.setattr(android_module.AndroidDriver, "_setup_stay_awake", setup_power)
    monkeypatch.setattr(agent_main, "_open_driver_by_platform", open_driver)
    return SimpleNamespace(opened=opened, power_calls=power_calls, open_driver=open_driver)


def snapshot(vm_id: str, serial: str = SERIAL):
    return [DeviceInfo(serial=serial, platform="android", extra={"vm_instance_id": vm_id} if vm_id else {})]


@pytest.mark.parametrize("scan_gap", [False, True])
def test_vm_b_reusing_serial_does_not_inherit_vm_a_driver(cache_environment, scan_gap):
    agent_main._record_serial_platform(snapshot("vm-a"))
    old = agent_main._get_or_open_driver(SERIAL)
    old._adb_kb_ready = True
    old._last_window_size = (640, 1280)
    if scan_gap:
        agent_main._record_serial_platform([])
        assert SERIAL not in agent_main._serial_vm_ids
    agent_main._record_serial_platform(snapshot("vm-b"))
    new = agent_main._get_or_open_driver(SERIAL)
    assert new is not old, "VM B inherited VM A's cached AndroidDriver"
    assert new._adb_kb_ready is False
    assert new._last_window_size is None
    assert cache_environment.power_calls == [(SERIAL, "vm-a"), (SERIAL, "vm-b")]


def test_same_vm_survives_snapshot_gap_without_reinitializing(cache_environment):
    agent_main._record_serial_platform(snapshot("vm-a"))
    driver = agent_main._get_or_open_driver(SERIAL)
    driver._adb_kb_ready = True
    agent_main._record_serial_platform([])
    with pytest.raises(RuntimeError, match="identity_unavailable"):
        agent_main._get_or_open_driver(SERIAL)
    assert len(cache_environment.opened) == 1
    assert SERIAL in android_module._STAY_AWAKE_DONE
    agent_main._record_serial_platform(snapshot("vm-a"))
    assert agent_main._get_or_open_driver(SERIAL) is driver
    assert driver._adb_kb_ready is True
    assert cache_environment.power_calls == [(SERIAL, "vm-a")]


def test_scan_only_power_marker_resets_after_missing_then_new_vm(cache_environment):
    agent_main._record_serial_platform(snapshot("vm-a"))
    android_module._STAY_AWAKE_DONE.update({SERIAL, "USB-OTHER"})
    agent_main._record_serial_platform([])
    agent_main._record_serial_platform(snapshot("vm-b"))
    assert agent_main._driver_cache == {}
    assert android_module._STAY_AWAKE_DONE == {"USB-OTHER"}
    assert cache_environment.power_calls == []
    agent_main._get_or_open_driver(SERIAL)
    assert cache_environment.power_calls == [(SERIAL, "vm-b")]


def test_snapshot_identity_change_does_not_close_or_construct_driver(cache_environment):
    agent_main._record_serial_platform(snapshot("vm-a"))
    old = agent_main._get_or_open_driver(SERIAL)
    old.close = Mock(side_effect=AssertionError("scan must not close native resources"))
    agent_main._record_serial_platform(snapshot("vm-b"))
    old.close.assert_not_called()
    assert SERIAL not in agent_main._driver_cache
    assert cache_environment.opened == [old]
    assert cache_environment.power_calls == [(SERIAL, "vm-a")]


def test_identity_change_does_not_keep_missing_old_screen_metadata(cache_environment):
    old_info = snapshot("vm-a")[0]
    old_info.model = "old-model"
    old_info.screen_width, old_info.screen_height = (640, 1280)
    agent_main._record_serial_platform([old_info])
    agent_main._record_serial_platform(snapshot("vm-b"))
    assert SERIAL not in agent_main._serial_screen_size
    assert SERIAL not in agent_main._serial_product_type


def test_identity_change_resets_only_the_reused_serial(cache_environment):
    other = "emulator-5556"
    physical = "USB-REAL"
    agent_main._record_serial_platform(snapshot("vm-a") + snapshot("vm-c", other) + snapshot("", physical))
    old = agent_main._get_or_open_driver(SERIAL)
    untouched = agent_main._get_or_open_driver(other)
    real = agent_main._get_or_open_driver(physical)
    agent_main._record_serial_platform(snapshot("vm-b") + snapshot("vm-c", other) + snapshot("", physical))
    assert SERIAL not in android_module._STAY_AWAKE_DONE
    assert {other, physical} <= android_module._STAY_AWAKE_DONE
    assert agent_main._get_or_open_driver(other) is untouched
    assert agent_main._get_or_open_driver(physical) is real
    assert agent_main._get_or_open_driver(SERIAL) is not old


@pytest.mark.parametrize("serial", ["USB-REAL", "192.0.2.10:5555"])
def test_untagged_physical_device_cache_and_power_policy_are_unchanged(cache_environment, serial):
    agent_main._record_serial_platform(snapshot("", serial))
    old = agent_main._get_or_open_driver(serial)
    old._adb_kb_ready = True
    old._last_window_size = (1080, 2400)
    agent_main._record_serial_platform([])
    agent_main._record_serial_platform(snapshot("", serial))
    assert agent_main._get_or_open_driver(serial) is old
    assert old._adb_kb_ready is True and old._last_window_size == (1080, 2400)
    assert cache_environment.power_calls == [(serial, "")]
    assert serial not in agent_main._android_vm_seen_identities
    assert serial not in agent_main._android_vm_cache_identities


@pytest.mark.parametrize("final_identity", ["vm-b", "vm-a", "missing"])
def test_identity_change_during_construction_is_rejected_without_retry(cache_environment, monkeypatch, final_identity):
    agent_main._record_serial_platform(snapshot("vm-a"))

    def changing_open(serial, platform, **kwargs):
        old = cache_environment.open_driver(serial, platform, **kwargs)
        agent_main._record_serial_platform(snapshot("vm-b") if final_identity != "missing" else [])
        if final_identity == "vm-a":
            agent_main._record_serial_platform(snapshot("vm-a"))
        # A slow old constructor may write its power marker after the snapshot changed.
        android_module._STAY_AWAKE_DONE.add(serial)
        return old

    monkeypatch.setattr(agent_main, "_open_driver_by_platform", changing_open)
    with pytest.raises(RuntimeError, match="identity_changed"):
        agent_main._get_or_open_driver(SERIAL)
    assert len(cache_environment.opened) == 1
    assert SERIAL not in agent_main._driver_cache
    if final_identity != "missing":
        assert SERIAL not in android_module._STAY_AWAKE_DONE
    else:
        assert SERIAL in android_module._STAY_AWAKE_DONE  # no proven replacement identity


@pytest.mark.parametrize("initial_identity", ["vm-a", ""])
def test_failed_old_constructor_cannot_leave_power_marker_for_new_vm(cache_environment, monkeypatch, initial_identity):
    agent_main._record_serial_platform(snapshot(initial_identity))

    def failing_open(serial, platform, **kwargs):
        cache_environment.open_driver(serial, platform, **kwargs)
        agent_main._record_serial_platform(snapshot("vm-b"))
        android_module._STAY_AWAKE_DONE.add(serial)
        raise RuntimeError("old constructor failed after initialization")

    monkeypatch.setattr(agent_main, "_open_driver_by_platform", failing_open)
    with pytest.raises(RuntimeError, match="identity_changed"):
        agent_main._get_or_open_driver(SERIAL)
    assert len(cache_environment.opened) == 1
    assert SERIAL not in android_module._STAY_AWAKE_DONE
    assert SERIAL not in agent_main._driver_cache


def test_late_old_constructor_does_not_erase_new_vm_cache_or_power_marker(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-a"))
    entered, release = threading.Event(), threading.Event()

    def slow_old_open(serial, platform, **kwargs):
        if agent_main._serial_vm_ids.get(serial) != "vm-a":
            return cache_environment.open_driver(serial, platform, **kwargs)
        old = android_module.AndroidDriver(SimpleNamespace(serial=serial, vm_id="vm-a"), setup_power=False)
        entered.set()
        assert release.wait(2)
        android_module._STAY_AWAKE_DONE.add(serial)
        return old

    monkeypatch.setattr(agent_main, "_open_driver_by_platform", slow_old_open)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(agent_main._get_or_open_driver, SERIAL)
        assert entered.wait(2)
        try:
            # The scan is not blocked by the native driver's slow construction.
            pool.submit(agent_main._record_serial_platform, snapshot("vm-b")).result(2)
            new = agent_main._get_or_open_driver(SERIAL)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="identity_changed"):
            pending.result(2)
    assert agent_main._get_or_open_driver(SERIAL) is new
    assert SERIAL in android_module._STAY_AWAKE_DONE
    assert cache_environment.power_calls == [(SERIAL, "vm-b")]


def test_cache_hit_rechecks_identity_after_status_callback(cache_environment):
    agent_main._record_serial_platform(snapshot("vm-a"))
    old = agent_main._get_or_open_driver(SERIAL)

    def callback(*_args):
        agent_main._record_serial_platform(snapshot("vm-b"))

    with pytest.raises(RuntimeError, match="identity_changed"):
        agent_main._get_or_open_driver(SERIAL, on_status=callback)
    assert cache_environment.opened == [old]
    assert SERIAL not in agent_main._driver_cache


def test_driver_built_before_first_vm_identity_arrived_is_not_published(cache_environment, monkeypatch):
    def changing_open(serial, platform, **kwargs):
        old = cache_environment.open_driver(serial, platform, **kwargs)
        agent_main._record_serial_platform(snapshot("vm-b"))
        return old

    monkeypatch.setattr(agent_main, "_open_driver_by_platform", changing_open)
    with pytest.raises(RuntimeError, match="identity_changed"):
        agent_main._get_or_open_driver(SERIAL)
    assert len(cache_environment.opened) == 1
    assert SERIAL not in agent_main._driver_cache
    assert SERIAL not in android_module._STAY_AWAKE_DONE


def test_unlabelled_cached_driver_is_not_reused_for_managed_vm(cache_environment):
    agent_main._record_serial_platform(snapshot("vm-a"))
    old = cache_environment.open_driver(SERIAL, "android")
    old._adb_kb_ready = True
    agent_main._driver_cache[SERIAL] = old
    new = agent_main._get_or_open_driver(SERIAL)
    assert new is not old
    assert new._adb_kb_ready is False


def test_first_managed_identity_drops_unowned_power_and_screen_metadata(cache_environment):
    old_info = snapshot("")[0]
    old_info.model = "old-model"
    old_info.screen_width, old_info.screen_height = (640, 1280)
    agent_main._record_serial_platform([old_info])
    old = agent_main._get_or_open_driver(SERIAL)
    assert SERIAL in android_module._STAY_AWAKE_DONE
    agent_main._record_serial_platform(snapshot("vm-b"))
    assert SERIAL not in android_module._STAY_AWAKE_DONE
    assert SERIAL not in agent_main._serial_screen_size
    assert SERIAL not in agent_main._serial_product_type
    assert SERIAL not in agent_main._driver_cache
    new = agent_main._get_or_open_driver(SERIAL)
    assert new is not old
    assert cache_environment.power_calls == [(SERIAL, ""), (SERIAL, "vm-b")]


def test_late_legacy_constructor_marker_cannot_skip_vm_b_power_setup(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-a"))
    a_power_done, release_a_setup, a_marker_written, release_a_return = [threading.Event() for _ in range(4)]

    def setup(driver):
        cache_environment.power_calls.append((driver.serial, driver._device.vm_id))
        if driver._device.vm_id == "vm-a":
            a_power_done.set()
            assert release_a_setup.wait(2)

    def legacy_a_constructor():
        # Real constructor, representing a discovery/legacy path that still owns a serial-only marker.
        old = android_module.AndroidDriver(SimpleNamespace(serial=SERIAL, vm_id="vm-a"))
        a_marker_written.set()
        assert release_a_return.wait(2)
        return old

    monkeypatch.setattr(android_module.AndroidDriver, "_setup_stay_awake", setup)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(legacy_a_constructor)
        try:
            assert a_power_done.wait(2)
            agent_main._record_serial_platform(snapshot("vm-b"))
            release_a_setup.set()
            assert a_marker_written.wait(2)
            assert SERIAL in android_module._STAY_AWAKE_DONE  # A's late marker is present before B construction.
            new = agent_main._get_or_open_driver(SERIAL)
        finally:
            release_a_setup.set()
            release_a_return.set()
        old = pending.result(2)
    assert new is not old
    assert new._device.vm_id == "vm-b"
    assert cache_environment.power_calls == [(SERIAL, "vm-a"), (SERIAL, "vm-b")]
    assert agent_main._get_or_open_driver(SERIAL) is new
    assert SERIAL in android_module._STAY_AWAKE_DONE


def test_slow_managed_vm_a_power_completion_cannot_replace_vm_b_completion(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-a"))
    entered, release = threading.Event(), threading.Event()

    def setup(driver):
        cache_environment.power_calls.append((driver.serial, driver._device.vm_id))
        if driver._device.vm_id == "vm-a":
            entered.set()
            assert release.wait(2)

    monkeypatch.setattr(android_module.AndroidDriver, "_setup_stay_awake", setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(agent_main._get_or_open_driver, SERIAL)
        try:
            assert entered.wait(2)
            pool.submit(agent_main._record_serial_platform, snapshot("vm-b")).result(2)
            new = agent_main._get_or_open_driver(SERIAL)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="identity_changed"):
            pending.result(2)
    assert cache_environment.power_calls == [(SERIAL, "vm-a"), (SERIAL, "vm-b")]
    assert agent_main._get_or_open_driver(SERIAL) is new
    assert SERIAL in android_module._STAY_AWAKE_DONE


def test_managed_power_failure_has_no_retry_or_published_completion(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-b"))
    android_module._STAY_AWAKE_DONE.add(SERIAL)  # stale marker must not bypass the real attempt
    setup = Mock(side_effect=RuntimeError("power setup failed"))
    monkeypatch.setattr(android_module.AndroidDriver, "_setup_stay_awake", setup)
    with pytest.raises(RuntimeError, match="power setup failed"):
        agent_main._get_or_open_driver(SERIAL)
    setup.assert_called_once()
    assert len(cache_environment.opened) == 1
    assert SERIAL not in agent_main._driver_cache
    assert SERIAL not in android_module._STAY_AWAKE_DONE


def test_managed_power_setting_disabled_stays_disabled(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-b"))
    monkeypatch.setattr(agent_main, "get_settings", lambda: SimpleNamespace(android_setup_stay_awake=False))
    driver = agent_main._get_or_open_driver(SERIAL)
    assert agent_main._get_or_open_driver(SERIAL) is driver
    assert cache_environment.power_calls == []
    assert SERIAL not in android_module._STAY_AWAKE_DONE


def test_driver_factory_forwards_explicit_power_override_and_preserves_default(cache_environment, monkeypatch):
    monkeypatch.setattr(android_module, "get_settings", lambda: SimpleNamespace(android_setup_stay_awake=True))
    monkeypatch.setattr(android_module.adb, "device", lambda serial: SimpleNamespace(serial=serial, vm_id="factory"))
    explicit = drivers_package.open_driver(SERIAL, "android", setup_power=False, on_status=lambda *_: None)
    assert isinstance(explicit, android_module.AndroidDriver)
    assert cache_environment.power_calls == []
    assert SERIAL not in android_module._STAY_AWAKE_DONE
    physical = drivers_package.open_driver("USB-FACTORY", "android")
    assert isinstance(physical, android_module.AndroidDriver)
    assert cache_environment.power_calls == [("USB-FACTORY", "factory")]


def test_same_vm_new_lifecycle_epoch_reopens_driver_without_new_snapshot(cache_environment):
    agent_main._record_serial_platform(snapshot("vm-a"))
    old = agent_main._get_or_open_driver(SERIAL)
    old._adb_kb_ready = True
    old._last_window_size = (640, 1280)
    agent_main._serial_lifecycle_epochs[SERIAL] = 2
    new = agent_main._get_or_open_driver(SERIAL)
    assert new is not old
    assert new._adb_kb_ready is False and new._last_window_size is None
    assert new._device.epoch == 2
    assert cache_environment.power_calls == [(SERIAL, "vm-a"), (SERIAL, "vm-a")]


def test_lifecycle_epoch_change_during_driver_construction_rejects_old_result(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-a"))

    def changing_open(serial, platform, **kwargs):
        old = cache_environment.open_driver(serial, platform, **kwargs)
        agent_main._serial_lifecycle_epochs[serial] = 1
        return old

    monkeypatch.setattr(agent_main, "_open_driver_by_platform", changing_open)
    with pytest.raises(RuntimeError, match="identity_changed"):
        agent_main._get_or_open_driver(SERIAL)
    assert agent_main._driver_cache == {}
    assert cache_environment.power_calls == []


def test_slow_old_epoch_power_cannot_replace_new_epoch_completion(cache_environment, monkeypatch):
    agent_main._record_serial_platform(snapshot("vm-a"))
    entered, release = threading.Event(), threading.Event()
    initialized_epochs = []

    def setup(driver):
        initialized_epochs.append(driver._device.epoch)
        if driver._device.epoch == 0:
            entered.set()
            assert release.wait(2)

    monkeypatch.setattr(android_module.AndroidDriver, "_setup_stay_awake", setup)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(agent_main._get_or_open_driver, SERIAL)
        try:
            assert entered.wait(2)
            agent_main._serial_lifecycle_epochs[SERIAL] = 2
            new = agent_main._get_or_open_driver(SERIAL)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="identity_changed"):
            pending.result(2)
    assert initialized_epochs == [0, 2]
    assert new._device.epoch == 2
    assert agent_main._get_or_open_driver(SERIAL) is new
    assert SERIAL in android_module._STAY_AWAKE_DONE


def test_ordinary_physical_device_ignores_vm_lifecycle_epoch(cache_environment):
    serial = "USB-REAL"
    agent_main._record_serial_platform(snapshot("", serial))
    old = agent_main._get_or_open_driver(serial)
    agent_main._serial_lifecycle_epochs[serial] = 2
    assert agent_main._get_or_open_driver(serial) is old
    assert cache_environment.power_calls == [(serial, "")]
