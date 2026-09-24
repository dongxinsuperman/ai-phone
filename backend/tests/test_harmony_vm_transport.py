"""Public Harmony VM identity must not turn a reused HDC port into A's device."""
from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from ai_phone.agent.drivers import hdc as hdc_module
from ai_phone.agent.drivers import harmony as harmony_module
from ai_phone.agent.harmony_vm.registry import (
    register_managed_vm,
    unregister_managed_vm,
)
from ai_phone.agent.harmony_vm.manager import HarmonyVmManager, HarmonyVmRuntime


HDC = "127.0.0.1:10007"


def test_harmony_vm_transport_uses_current_hdc_and_rejects_old_identity(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(hdc_module, "_resolve_hdc_binary", lambda: "/fake/hdc")

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(hdc_module.subprocess, "run", fake_run)
    opened = []
    monkeypatch.setattr(
        harmony_module,
        "HarmonyDriver",
        lambda serial, **kwargs: opened.append((serial, kwargs)) or object(),
    )
    register_managed_vm("A", HDC, "lease-A")
    register_managed_vm("B", HDC, "lease-B")
    try:
        assert hdc_module.hdc_run("shell", "echo ok", serial="harmony-vm:B") == "ok"
        assert calls == [["/fake/hdc", "-t", HDC, "shell", "echo ok"]]
        harmony_module.open_harmony_driver("harmony-vm:B")
        assert opened[0][0] == HDC
        assert opened[0][1]["logical_identity"] == "harmony-vm:B"
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            hdc_module.hdc_run("shell", "echo old", serial="harmony-vm:A")
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            harmony_module.open_harmony_driver("harmony-vm:A")
        assert len(calls) == 1
    finally:
        unregister_managed_vm("A", "lease-A")
        unregister_managed_vm("B", "lease-B")


def test_old_vm_cached_driver_is_rejected_after_port_reuse():
    from ai_phone.agent import main as agent_main

    identity = "harmony-vm:A"
    previous = agent_main._driver_cache.get(identity)
    agent_main._driver_cache[identity] = SimpleNamespace(serial=HDC)
    register_managed_vm("A", HDC, "lease-A")
    register_managed_vm("B", HDC, "lease-B")
    try:
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            agent_main._get_or_open_driver(identity)
    finally:
        if previous is None:
            agent_main._driver_cache.pop(identity, None)
        else:
            agent_main._driver_cache[identity] = previous
        unregister_managed_vm("A", "lease-A")
        unregister_managed_vm("B", "lease-B")


def test_old_vm_in_flight_driver_cannot_click_new_port_owner():
    driver = object.__new__(harmony_module.HarmonyDriver)
    driver.serial = HDC
    driver._logical_identity = "harmony-vm:A"
    driver._heal_lock = threading.RLock()
    register_managed_vm("A", HDC, "lease-A")
    register_managed_vm("B", HDC, "lease-B")
    try:
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            driver.click(1, 2)
    finally:
        unregister_managed_vm("A", "lease-A")
        unregister_managed_vm("B", "lease-B")


def test_old_vm_mirror_drops_frames_after_port_reuse(monkeypatch):
    import ai_phone.config as config_module
    from ai_phone.agent.mirror import build_harmony_streamer

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(
            harmony_mirror_backend="screenshot",
            harmony_mirror_fps=8,
            harmony_mirror_jpeg_quality=55,
            harmony_mirror_long_edge=720,
        ),
    )
    frames = []
    driver = SimpleNamespace(serial=HDC, screenshot_jpeg=lambda **_kwargs: b"jpeg")
    register_managed_vm("A", HDC, "lease-A")
    try:
        streamer = build_harmony_streamer(
            serial="harmony-vm:A",
            driver=driver,
            on_jpeg=lambda *frame: frames.append(frame),
            log_tag="A",
        )
        streamer._on_jpeg(b"A", 1, 1)
        assert frames == [(b"A", 1, 1)]

        register_managed_vm("B", HDC, "lease-B")
        streamer._on_jpeg(b"B", 1, 1)
        assert frames == [(b"A", 1, 1)]
        with pytest.raises(RuntimeError, match="managed_harmony_vm_not_current"):
            streamer._grab_frame()
    finally:
        unregister_managed_vm("A", "lease-A")
        unregister_managed_vm("B", "lease-B")


def test_agreement_error_requires_evidence_from_current_boot_log(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    log = tmp_path / "logs" / "A" / "emulator.log"
    log.parent.mkdir(parents=True)
    old = b"Please agree to the agreement first\n"
    log.write_bytes(old)
    runtime = HarmonyVmRuntime(
        vm_id="A",
        name="A",
        instance_name="aiphone_harmony_A",
        instance_path=str(tmp_path / "instances"),
        image_root="",
        hdc_port=10007,
        hdc_serial=HDC,
        lease_token="lease-A",
        process=SimpleNamespace(poll=lambda: 1, returncode=1),
        log_start_offset=len(old),
    )
    with pytest.raises(RuntimeError, match="emulator exited during boot: 1"):
        manager._wait_hdc(runtime)

    log.write_bytes(old + b"Please agree to the agreement first\n")
    with pytest.raises(RuntimeError, match="harmony_vm_agreement_required"):
        manager._wait_hdc(runtime)
