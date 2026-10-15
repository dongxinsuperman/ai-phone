"""Independent, native-free cache review probes. Run with backend on PYTHONPATH."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from ai_phone.agent import main as agent
from ai_phone.agent.drivers import android
from ai_phone.agent.drivers.base import DeviceInfo


SERIAL = "emulator-5554"
MAPS = (
    "_driver_cache", "_serial_platform", "_serial_screen_size", "_serial_product_type",
    "_serial_vm_ids", "_android_vm_cache_identities", "_android_vm_seen_identities",
    "_serial_lifecycle_epochs",
)


def snapshot(vm_id: str, *, width: int = 0, model: str = ""):
    return [DeviceInfo(
        serial=SERIAL, platform="android", model=model,
        screen_width=width, screen_height=1280 if width else 0,
        extra={"vm_instance_id": vm_id} if vm_id else {},
    )]


def isolate(stack: ExitStack):
    for name in MAPS:
        stack.enter_context(patch.object(agent, name, {}))
    stack.enter_context(patch.object(android, "_STAY_AWAKE_DONE", set()))
    stack.enter_context(patch.object(agent, "get_settings", lambda: SimpleNamespace(android_setup_stay_awake=True)))


def first_managed_identity():
    with ExitStack() as stack:
        isolate(stack)
        power_calls = []
        native_id = {"value": "unlabelled-a"}
        stack.enter_context(patch.object(
            android.AndroidDriver, "_setup_stay_awake",
            lambda driver: power_calls.append(driver._device.vm_id),
        ))

        def open_driver(serial, platform, **kwargs):
            assert platform == "android"
            return android.AndroidDriver(
                SimpleNamespace(serial=serial, vm_id=native_id["value"]),
                setup_power=kwargs.get("setup_power", True),
            )

        stack.enter_context(patch.object(agent, "_open_driver_by_platform", open_driver))
        agent._record_serial_platform(snapshot("", width=640, model="old-a"))
        old = agent._get_or_open_driver(SERIAL)
        native_id["value"] = "vm-b"
        agent._record_serial_platform(snapshot("vm-b"))
        assert SERIAL not in agent._serial_screen_size
        assert SERIAL not in agent._serial_product_type
        new = agent._get_or_open_driver(SERIAL)
        assert new is not old
        assert power_calls == ["unlabelled-a", "vm-b"], power_calls
        return {"case": "first_managed_identity", "power_calls": power_calls, "old_metadata_removed": True}


def same_vm_lifecycle_epoch():
    with ExitStack() as stack:
        isolate(stack)
        power_calls = []
        stack.enter_context(patch.object(
            android.AndroidDriver, "_setup_stay_awake",
            lambda driver: power_calls.append(driver),
        ))
        def open_driver(serial, platform, **kwargs):
            assert platform == "android"
            return android.AndroidDriver(
                SimpleNamespace(serial=serial, vm_id="same-vm"),
                setup_power=kwargs.get("setup_power", True),
            )
        stack.enter_context(patch.object(agent, "_open_driver_by_platform", open_driver))
        agent._record_serial_platform(snapshot("same-vm", width=640, model="old-settings"))
        old = agent._get_or_open_driver(SERIAL)
        old._adb_kb_ready = True
        old._last_window_size = (640, 1280)
        agent._serial_lifecycle_epochs[SERIAL] = agent._serial_lifecycle_epochs.get(SERIAL, 0) + 1
        # No new device snapshot is needed for a lifecycle epoch change.
        new = agent._get_or_open_driver(SERIAL)
        assert new is not old and new._adb_kb_ready is False
        assert new._last_window_size is None
        assert SERIAL not in agent._serial_screen_size
        assert SERIAL not in agent._serial_product_type
        assert power_calls == [old, new]
        return {"case": "same_vm_new_epoch", "new_driver": True, "actual_setup_count": len(power_calls)}


def stale_marker_before_new_constructor(*, aba: bool = False):
    with ExitStack() as stack:
        isolate(stack)
        power_calls, power_drivers = [], []
        entered, publish_stale_marker = threading.Event(), threading.Event()
        stale_marker_written, finish_old_open = threading.Event(), threading.Event()
        first = {"pending": True}
        def setup_power(driver):
            power_calls.append(driver._device.vm_id)
            power_drivers.append(driver)
        stack.enter_context(patch.object(android.AndroidDriver, "_setup_stay_awake", setup_power))

        def open_driver(serial, platform, **kwargs):
            assert platform == "android"
            vm_id = agent._serial_vm_ids[serial]
            is_old = first["pending"]
            first["pending"] = False
            driver = android.AndroidDriver(
                SimpleNamespace(serial=serial, vm_id=vm_id),
                setup_power=kwargs.get("setup_power", True),
            )
            if is_old:
                entered.set()
                assert publish_stale_marker.wait(3)
                # Model a previous serial-only constructor's late completion
                # after the scan cleared A's marker. No native command runs.
                android._STAY_AWAKE_DONE.add(serial)
                stale_marker_written.set()
                assert finish_old_open.wait(3)
            return driver

        stack.enter_context(patch.object(agent, "_open_driver_by_platform", open_driver))
        agent._record_serial_platform(snapshot("vm-a"))
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = workers.submit(agent._get_or_open_driver, SERIAL)
            assert entered.wait(3)
            try:
                agent._record_serial_platform(snapshot("vm-b"))
                if aba:
                    agent._record_serial_platform(snapshot("vm-a"))
                publish_stale_marker.set()
                assert stale_marker_written.wait(3)
                new = agent._get_or_open_driver(SERIAL)
            finally:
                publish_stale_marker.set()
                finish_old_open.set()
            try:
                pending.result(3)
            except RuntimeError as exc:
                assert "identity_changed" in str(exc)
            else:
                raise AssertionError("late old constructor returned a driver")
        expected_id = "vm-a" if aba else "vm-b"
        assert agent._driver_cache[SERIAL] is new
        assert new._device.vm_id == expected_id
        assert power_calls[-1] == expected_id, power_calls
        assert any(driver is new for driver in power_drivers), "current generation never applied power setup"
        assert SERIAL in android._STAY_AWAKE_DONE
        return {"case": "ABA" if aba else "late_A_before_B", "power_calls": power_calls, "new_cache_preserved": True}


if __name__ == "__main__":
    print(json.dumps({"results": [
        first_managed_identity(),
        stale_marker_before_new_constructor(),
        stale_marker_before_new_constructor(aba=True),
        same_vm_lifecycle_epoch(),
    ]}, ensure_ascii=False, indent=2))
