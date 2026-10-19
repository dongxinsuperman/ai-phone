"""Fold initialization ordering and the existing startup deadline; no real devices."""
from types import SimpleNamespace

import pytest

from ai_phone.agent.drivers import hdc
from ai_phone.agent.harmony_vm import capability, manager as mod


@pytest.fixture
def fold_env(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=100.0)
    fake_time = SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    )
    monkeypatch.setattr(mod, "time", fake_time)
    monkeypatch.setattr(capability, "time", fake_time)
    runtime = mod.HarmonyVmRuntime(
        vm_id="fold-timing", name="fold-timing",
        instance_name="aiphone_harmony_fold_timing", instance_path=str(tmp_path),
        image_root="", hdc_port=10001, hdc_serial="127.0.0.1:10001",
        lease_token="fold-timing", operation_deadline=120.0,
    )
    calls = []
    monkeypatch.setattr(hdc, "hdc_shell", lambda serial, command, **kw: (
        calls.append((clock.now, command, kw["timeout"])) or ""
    ))
    message = {"device_type": "Foldable", "config_json": {
        "fold": {"initial_state": "folded"}, "folded_screen": {"width": 1080},
    }}
    runtime.driver = SimpleNamespace(window_size=lambda: (1080, 2504))
    return mod.HarmonyVmManager(runtime_dir=tmp_path), runtime, message, clock, calls


@pytest.mark.parametrize("state,size,argument", [
    ("folded", (1080, 2504), "-m"),
    ("unfolded", (2224, 2496), "-f"),
])
def test_fold_command_follows_initialization_interval(fold_env, state, size, argument):
    manager, runtime, message, clock, calls = fold_env
    message["config_json"]["fold"]["initial_state"] = state
    runtime.driver.window_size = lambda: size

    manager._apply_fold_state(runtime, message)

    assert calls == [(105.0, "hidumper -s DisplayManagerService -a " + argument, 15.0)]
    assert clock.now == 107.0  # Original two live-size confirmations still apply.
    assert runtime.operation_deadline == 120.0


def test_expired_budget_does_not_send_fold_command(fold_env):
    manager, runtime, message, clock, calls = fold_env
    runtime.operation_deadline = 103.0

    with pytest.raises(TimeoutError, match="startup deadline exceeded"):
        manager._apply_fold_state(runtime, message)

    assert clock.now == 103.0
    assert calls == []


def test_non_foldable_does_not_wait(fold_env):
    manager, runtime, message, clock, calls = fold_env
    message["device_type"] = "Phone"

    manager._apply_fold_state(runtime, message)

    assert clock.now == 100.0
    assert calls == []
