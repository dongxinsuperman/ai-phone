"""HDC fport ls/rm are global even with -t; never clean another target."""
from types import SimpleNamespace

import pytest

from ai_phone.agent.harmony_vm import manager as mod
from ai_phone.agent.harmony_vm.registry import register_managed_vm, unregister_managed_vm


@pytest.mark.parametrize("cached_port", [None, 16556, 16558])
def test_cleanup_uses_listed_target_owner_not_cached_port(tmp_path, monkeypatch, cached_port):
    manager = mod.HarmonyVmManager(runtime_dir=tmp_path)
    runtime = SimpleNamespace(vm_id="cleanup-A", lease_token="A-token",
                              hdc_serial="127.0.0.1:10011", driver_fport_port=cached_port)
    # Real SDK output captured during the personal two-VM experiment.
    forwards = {16556: runtime.hdc_serial, 16557: runtime.hdc_serial,
                16558: "127.0.0.1:10012", 16559: "HARMONY-PHYSICAL"}
    removed = []

    def hdc(*args, **kwargs):
        assert kwargs["serial"] == runtime.hdc_serial
        if args == ("fport", "ls"):
            return "\r\n".join(f"{owner}    tcp:{port} tcp:8012    [Forward]"
                                for port, owner in forwards.items())
        assert args[:2] == ("fport", "rm")
        port = int(args[2][4:]); removed.append(port)
        # Matches HDC: -t does NOT scope this deletion to the named target.
        forwards.pop(port)
        return "Remove forward ruler success"

    register_managed_vm(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    try:
        monkeypatch.setattr(mod, "hdc_run", hdc)
        manager._remove_fport(runtime)
        assert sorted(removed) == [16556, 16557]
        assert forwards == {16558: "127.0.0.1:10012", 16559: "HARMONY-PHYSICAL"}
    finally:
        unregister_managed_vm(runtime.vm_id, runtime.lease_token)


@pytest.mark.parametrize("output", ["", "tcp:16556 tcp:8012", "other tcp:16556 tcp:8012 [Forward]"])
def test_no_verified_owner_means_no_global_deletion(tmp_path, monkeypatch, output):
    manager = mod.HarmonyVmManager(runtime_dir=tmp_path)
    runtime = SimpleNamespace(vm_id="unverified", lease_token="old",
                              hdc_serial="127.0.0.1:10011", driver_fport_port=16556)
    calls = []
    monkeypatch.setattr(mod, "hdc_run", lambda *args, **kwargs: calls.append(args) or output)
    manager._remove_fport(runtime)
    assert calls == [("fport", "ls")]


def test_old_vm_cannot_clean_reassigned_hdc_address(tmp_path, monkeypatch):
    manager = mod.HarmonyVmManager(runtime_dir=tmp_path)
    runtime = SimpleNamespace(vm_id="old", lease_token="old-token", hdc_serial="127.0.0.1:10011",
                              driver_fport_port=16556)
    register_managed_vm("new-owner", runtime.hdc_serial, "new-token")
    monkeypatch.setattr(mod, "hdc_run", lambda *args, **kwargs: pytest.fail("new owner must not be touched"))
    try:
        manager._remove_fport(runtime)
    finally:
        unregister_managed_vm("new-owner", "new-token")


def test_allocation_cannot_reassign_a_listed_port_before_removal(tmp_path, monkeypatch):
    import threading
    from ai_phone.agent.harmony_vm.fport import _ALLOC_LOCK

    manager = mod.HarmonyVmManager(runtime_dir=tmp_path)
    runtime = SimpleNamespace(vm_id="race-A", lease_token="race-token",
                              hdc_serial="127.0.0.1:10011", driver_fport_port=16556)
    forwards = {16556: "A"}
    waiting, acquired = threading.Event(), threading.Event()

    def allocate_to_b():
        waiting.set()
        with _ALLOC_LOCK:
            forwards[16556] = "B"
            acquired.set()

    worker = threading.Thread(target=allocate_to_b)
    def hdc(*args, **kwargs):
        if args == ("fport", "ls"):
            worker.start(); assert waiting.wait(1)
            acquired.wait(.05)
            return runtime.hdc_serial + " tcp:16556 tcp:8012 [Forward]"
        assert args == ("fport", "rm", "tcp:16556", "tcp:8012")
        forwards.pop(16556)
        return ""

    monkeypatch.setattr(mod, "hdc_run", hdc)
    try:
        manager._remove_fport(runtime)
    finally:
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert acquired.is_set() and forwards == {16556: "B"}
