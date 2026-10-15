"""Safety regression for a live exact-owned Emulator without a Popen handle.

All native/device calls are fakes. The ordinary stop/delete methods and their
process-identity/HDC-removal checks are real product code.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import psutil
import pytest

from ai_phone.agent.harmony_vm import manager as lifecycle
from ai_phone.agent.harmony_vm import capability as cap
from ai_phone.agent.harmony_vm.registry import register_managed_vm, unregister_managed_vm


fixture_path = Path(lifecycle.__file__).resolve().parents[3] / "tests" / "test_harmony_vm_agent_hardening.py"
spec = importlib.util.spec_from_file_location("independent_harmony_fixture", fixture_path)
hardening = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hardening)
isolated_capability_cache = hardening.isolated_capability_cache


@pytest.mark.parametrize(("origin", "action"), [
    ("registry", "stop"), ("reclaimed", "stop"),
    ("registry", "delete"), ("reclaimed", "delete"),
], ids=["registry-stop", "reclaimed-stop", "registry-delete", "reclaimed-delete"])
def test_live_exact_instance_blocks_success_and_delete_without_process_handle(
    tmp_path, monkeypatch, origin, action,
):
    manager = lifecycle.HarmonyVmManager(runtime_dir=tmp_path)
    original_runtime = hardening._runtime(tmp_path)
    known = {**original_runtime.persistent_dict(), "created": True}
    vm_id, serial, token = original_runtime.vm_id, original_runtime.hdc_serial, original_runtime.lease_token
    manager._known[vm_id] = known
    runtime = manager._runtime_from_known(vm_id, known, serial)
    assert runtime.process is None
    if origin == "reclaimed":
        manager._runtimes[vm_id] = runtime
    register_managed_vm(vm_id, serial, token)
    private_file = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("owned private data must survive failed stop")

    commands, process_samples = [], []
    exact_process = SimpleNamespace(info={
        "name": "Emulator", "cmdline": [
            "/fake/Emulator", "-start", runtime.instance_name,
            "-instancePath", runtime.instance_path, "-hdcPort", str(runtime.hdc_port),
        ],
    })

    def process_iter(_attrs):
        process_samples.append(list(commands))
        # The exact instance remains alive throughout both stop and delete.
        return iter([exact_process])

    def native(args, **_kwargs):
        assert args[0] == "/fake/Emulator" and args[2] == runtime.instance_name
        commands.append(args[1])
        if args[1] == "-stop":
            return SimpleNamespace(returncode=1, stdout="", stderr="fixture stop failed; process still alive")
        assert args[1] == "-delete"
        assert args[args.index("-instancePath") + 1] == runtime.instance_path
        # This only removes our pytest temporary fake definition. No Emulator
        # command is run; it demonstrates the production guard allowed delete.
        shutil.rmtree(private_file.parent)
        return SimpleNamespace(returncode=0, stdout="fixture delete success", stderr="")

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(lifecycle.subprocess, "run", native)
    monkeypatch.setattr(lifecycle, "hdc_run", lambda *_a, **_kw: "[Fail]No target available")
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port, **_kwargs: True)
    monkeypatch.setattr(manager, "_remove_fport", lambda _runtime: None)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    # Do not replace _active_instance_serial, _wait_removed, stop_sync or
    # delete_sync: upstream identity and transport facts are evaluated normally.
    try:
        operation = manager.stop_sync if action == "stop" else manager.delete_sync
        result = operation(vm_id, serial, token)
        lifecycle_process_sample_count = len(process_samples)
        live_after_result = manager._active_instance_serial(vm_id, known)
        print(json.dumps({
            "origin": origin, "action": action, "runtime_process_is_none": True,
            "native_commands": commands, "native_process_samples_in_lifecycle": lifecycle_process_sample_count,
            "exact_process_still_alive": live_after_result == serial,
            "private_definition_exists": private_file.exists(), "result": result,
            "registry_after": manager._known.get(vm_id),
        }, ensure_ascii=False, default=str))
        assert live_after_result == serial, "the fake exact instance must still be alive"
        assert result["ok"] is False, "live exact-owned process was falsely confirmed stopped/deleted"
        assert result["details"]["cleanup_confirmed"] is False
        assert "-delete" not in commands, "delete must not follow unconfirmed native exit"
        assert private_file.is_file()
    finally:
        unregister_managed_vm(vm_id, token)


@pytest.mark.parametrize(("native_state", "action"), [
    ("running", "stop"), ("unknown", "stop"),
    ("running", "delete"), ("unknown", "delete"),
], ids=["legacy-running-stop", "legacy-unknown-stop", "legacy-running-delete", "legacy-unknown-delete"])
def test_old_cleanup_confirmation_cannot_bypass_fresh_process_evidence(
    tmp_path, monkeypatch, native_state, action,
):
    manager = lifecycle.HarmonyVmManager(runtime_dir=tmp_path)
    runtime = hardening._runtime(tmp_path)
    vm_id, serial, token = runtime.vm_id, runtime.hdc_serial, runtime.lease_token
    manager._known[vm_id] = {
        **runtime.persistent_dict(), "created": True,
        "ready": False, "cleanup_pending": False, "cleanup_confirmed": True,
        # This is the old false-positive shape produced by the original case.
        "exit_evidence": {"process_handle_present": False, "process_return_code": None},
    }
    private_file = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("historical private data")
    commands, samples = [], []
    exact_process = SimpleNamespace(info={
        "name": "Emulator", "cmdline": [
            "/fake/Emulator", "-start", runtime.instance_name,
            "-instancePath", runtime.instance_path, "-hdcPort", str(runtime.hdc_port),
        ],
    })

    def process_iter(_attrs):
        samples.append(list(commands))
        if native_state == "unknown":
            raise PermissionError("fixture process inventory is unavailable")
        return iter([exact_process])

    def native(args, **_kwargs):
        assert args[0] == "/fake/Emulator" and args[2] == runtime.instance_name
        commands.append(args[1])
        if args[1] == "-stop":
            return SimpleNamespace(returncode=1, stdout="", stderr="fixture process did not stop")
        assert args[1] == "-delete"
        shutil.rmtree(private_file.parent)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(lifecycle.subprocess, "run", native)
    monkeypatch.setattr(lifecycle, "hdc_run", lambda *_a, **_kw: "[Fail]No target available")
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port, **_kwargs: True)
    monkeypatch.setattr(manager, "_remove_fport", lambda _runtime: None)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    try:
        operation = manager.stop_sync if action == "stop" else manager.delete_sync
        result = operation(vm_id, serial, token)
        print(json.dumps({
            "origin": "legacy_confirmed_registry", "action": action, "native_state": native_state,
            "native_commands": commands, "native_process_samples_in_lifecycle": len(samples),
            "private_definition_exists": private_file.exists(), "result": result,
        }, ensure_ascii=False, default=str))
        assert result["ok"] is False, "a historical confirmation is not proof of current process exit"
        assert result["details"]["cleanup_confirmed"] is False
        assert samples, "explicit stop must recheck an older confirmation"
        assert "-delete" not in commands
        assert private_file.is_file()
        if native_state == "unknown":
            assert commands == [] and result["reason"] == "instance_owner_unverified"
        else:
            assert commands == ["-stop"]
            assert result["details"]["exit_evidence"]["instance_process_status"] == "running"
    finally:
        unregister_managed_vm(vm_id, token)
