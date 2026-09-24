"""Run 后息屏不得改变受管鸿蒙折叠 VM 的外屏形态。"""
from __future__ import annotations

from ai_phone.agent.drivers import harmony as harmony_mod
from ai_phone.agent.harmony_vm.registry import (
    register_managed_vm,
    unregister_managed_vm,
    vm_device_identity,
)


def _driver(vm_id: str, serial: str) -> harmony_mod.HarmonyDriver:
    # 电源 hook 只需 serial/identity；不在单测里启动 hmdriver2 或真实 HDC。
    driver = object.__new__(harmony_mod.HarmonyDriver)
    driver.serial = serial
    driver._logical_identity = vm_device_identity(vm_id) if vm_id else ""  # noqa: SLF001
    return driver


def test_managed_folded_vm_restores_outer_screen_while_staying_asleep(monkeypatch):
    vm_id, serial, lease = "fold-sleep-change", "127.0.0.1:19111", "lease-change"
    register_managed_vm(vm_id, serial, lease, folded_screen_width=1080)
    try:
        driver = _driver(vm_id, serial)
        state = {"width": 1080, "lit": True}
        commands: list[str] = []
        widths: list[int] = []

        def shell(actual_serial, command, **_kwargs):
            assert actual_serial == serial
            commands.append(command)
            if command == "power-shell suspend":
                state["lit"] = False
            elif command == "hidumper -s DisplayManagerService -a -m":
                assert state["lit"] is False
                state["width"] = 1080
            else:
                raise AssertionError(f"unexpected shell command: {command}")
            return ""

        def window_size():
            width = state["width"]
            widths.append(width)
            # suspend 的副作用可能延迟出现，首次读数仍是旧外屏宽。
            if len(widths) == 1:
                state["width"] = 2224
            return width, 2504

        driver.window_size = window_size
        monkeypatch.setattr(harmony_mod, "hdc_shell", shell)
        monkeypatch.setattr(harmony_mod, "_harmony_screen_is_lit", lambda _s: state["lit"])
        monkeypatch.setattr(harmony_mod.time, "sleep", lambda _seconds: None)

        driver.sleep_after_run()

        assert commands == [
            "power-shell suspend",
            "hidumper -s DisplayManagerService -a -m",
        ]
        assert widths == [1080, 2224, 2224, 1080, 1080, 1080]
        assert state == {"width": 1080, "lit": False}
    finally:
        unregister_managed_vm(vm_id, lease)


def test_managed_folded_vm_does_not_reapply_when_outer_screen_survives(monkeypatch):
    vm_id, serial, lease = "fold-sleep-stable", "127.0.0.1:19112", "lease-stable"
    register_managed_vm(vm_id, serial, lease, folded_screen_width=1080)
    try:
        driver = _driver(vm_id, serial)
        state = {"lit": True}
        commands: list[str] = []

        def shell(actual_serial, command, **_kwargs):
            assert actual_serial == serial
            commands.append(command)
            assert command == "power-shell suspend"
            state["lit"] = False
            return ""

        driver.window_size = lambda: (1080, 2504)
        monkeypatch.setattr(harmony_mod, "hdc_shell", shell)
        monkeypatch.setattr(harmony_mod, "_harmony_screen_is_lit", lambda _s: state["lit"])
        monkeypatch.setattr(harmony_mod.time, "sleep", lambda _seconds: None)

        driver.sleep_after_run()

        assert commands == ["power-shell suspend"]
        assert state["lit"] is False
    finally:
        unregister_managed_vm(vm_id, lease)


def test_fold_restore_does_not_claim_success_if_screen_lights_up(monkeypatch):
    vm_id, serial, lease = "fold-sleep-relit", "127.0.0.1:19115", "lease-relit"
    register_managed_vm(vm_id, serial, lease, folded_screen_width=1080)
    try:
        driver = _driver(vm_id, serial)
        state = {"width": 1080, "lit": True}
        warnings: list[str] = []

        def shell(_serial, command, **_kwargs):
            if command == "power-shell suspend":
                state.update(width=2224, lit=False)
            elif command == "hidumper -s DisplayManagerService -a -m":
                state.update(width=1080, lit=True)
            else:
                raise AssertionError(f"unexpected shell command: {command}")
            return ""

        driver.window_size = lambda: (state["width"], 2504)
        monkeypatch.setattr(harmony_mod, "hdc_shell", shell)
        monkeypatch.setattr(harmony_mod, "_harmony_screen_is_lit", lambda _s: state["lit"])
        monkeypatch.setattr(harmony_mod.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(
            harmony_mod.logger,
            "warning",
            lambda _message, *_args: warnings.append(str(_args[-1])),
        )

        driver.sleep_after_run()

        assert any("managed_folded_harmony_vm_sleep_state_mismatch" in row for row in warnings)
    finally:
        unregister_managed_vm(vm_id, lease)


def test_reused_hdc_port_rejects_stale_folded_vm_before_any_power_command(monkeypatch):
    serial = "127.0.0.1:19113"
    register_managed_vm("fold-sleep-old", serial, "lease-old", folded_screen_width=1080)
    register_managed_vm("fold-sleep-new", serial, "lease-new", folded_screen_width=1080)
    try:
        old_driver = _driver("fold-sleep-old", serial)
        commands: list[str] = []
        monkeypatch.setattr(
            harmony_mod,
            "hdc_shell",
            lambda _serial, command, **_kwargs: commands.append(command),
        )

        old_driver.sleep_after_run()

        assert commands == []
    finally:
        unregister_managed_vm("fold-sleep-old", "lease-old")
        unregister_managed_vm("fold-sleep-new", "lease-new")


def test_unmanaged_harmony_device_keeps_original_suspend_only(monkeypatch):
    serial = "HARMONY-PHYSICAL"
    driver = _driver("", serial)
    commands: list[str] = []
    driver.window_size = lambda: (_ for _ in ()).throw(AssertionError("not folded"))
    monkeypatch.setattr(
        harmony_mod,
        "hdc_shell",
        lambda _serial, command, **_kwargs: commands.append(command) or "",
    )
    monkeypatch.setattr(
        harmony_mod,
        "_harmony_screen_is_lit",
        lambda _serial: (_ for _ in ()).throw(AssertionError("not folded")),
    )

    driver.sleep_after_run()

    assert commands == ["power-shell suspend"]


def test_managed_non_folded_vm_keeps_original_suspend_only(monkeypatch):
    vm_id, serial, lease = "straight-sleep", "127.0.0.1:19114", "lease-straight"
    register_managed_vm(vm_id, serial, lease)
    try:
        driver = _driver(vm_id, serial)
        commands: list[str] = []
        driver.window_size = lambda: (_ for _ in ()).throw(AssertionError("not folded"))
        monkeypatch.setattr(
            harmony_mod,
            "hdc_shell",
            lambda _serial, command, **_kwargs: commands.append(command) or "",
        )

        driver.sleep_after_run()

        assert commands == ["power-shell suspend"]
    finally:
        unregister_managed_vm(vm_id, lease)
