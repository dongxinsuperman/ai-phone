"""Host-independent Harmony lifecycle regressions; no real tools/devices used."""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.harmony_vm import capability as cap
from ai_phone.agent.harmony_vm import manager as lifecycle
from ai_phone.agent.harmony_vm.manager import HarmonyVmManager, HarmonyVmRuntime
from ai_phone.agent.harmony_vm.registry import (
    managed_vm_owner,
    register_managed_vm,
    unregister_managed_vm,
)


@pytest.fixture(autouse=True)
def isolated_capability_cache(monkeypatch):
    monkeypatch.setattr(cap, "_STATIC_PROBE_CACHE", {})
    # Any accidentally unmocked invocation must fail instead of reaching the
    # developer's HDC server or the existing desktop Emulator fleet.
    def no_tools(*_args, **_kwargs):
        raise AssertionError("this test must not invoke a real Harmony tool")
    monkeypatch.setattr(cap, "_run", no_tools)
    monkeypatch.setattr(cap, "hdc_list_targets", no_tools)
    monkeypatch.setattr(cap, "hdc_run", no_tools)
    monkeypatch.setattr(lifecycle, "hdc_list_targets", no_tools)
    monkeypatch.setattr(lifecycle, "hdc_run", no_tools)
    monkeypatch.setattr(lifecycle, "find_harmony_tools", no_tools)
    monkeypatch.setattr(cap, "_emulator_config_files", lambda: iter(()))


def test_parallel_registry_publication_preserves_every_vm(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    barrier = threading.Barrier(8)
    replace = lifecycle.os.replace

    def slow_replace(source, target):
        time.sleep(0.005)
        replace(source, target)

    monkeypatch.setattr(lifecycle.os, "replace", slow_replace)

    def publish(index):
        manager._known[str(index)] = {"created": True, "lease_token": f"lease-{index}"}
        barrier.wait(timeout=2)
        manager._save_registry()

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(publish, range(8)))
    assert json.loads(manager.registry_path.read_text()) == manager._known
    assert len(manager._known) == 8
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_registry_publication_keeps_previous_complete_file(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    manager._known = {"original": {"created": True}}
    manager._save_registry()
    manager._known["new"] = {"created": True}

    def fail_replace(*_args):
        raise OSError("disk replacement failed")

    monkeypatch.setattr(lifecycle.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk replacement failed"):
        manager._save_registry()
    assert json.loads(manager.registry_path.read_text()) == {"original": {"created": True}}
    assert list(tmp_path.glob("*.tmp")) == []


def _runtime(tmp_path):
    return HarmonyVmRuntime(
        vm_id="transport-vm", name="transport", instance_name="aiphone_harmony_transport_vm",
        instance_path=str(tmp_path / "instances"), image_root="", hdc_port=12341,
        hdc_serial="127.0.0.1:12341", lease_token="lease-transport", ready=True,
        observed_request_id="start-operation",
        process=SimpleNamespace(poll=lambda: None),
    )


async def test_scan_loss_retains_runtime_and_lease_then_reports_recovery(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    manager._runtimes[runtime.vm_id] = runtime
    manager._known[runtime.vm_id] = runtime.persistent_dict()
    register_managed_vm(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    messages = []

    async def send(msg):
        messages.append(msg)
        return True

    monkeypatch.setattr(manager, "stop_sync", lambda *_args: pytest.fail("scan loss must not stop VM"))
    try:
        client = SimpleNamespace(send=send)
        assert await manager.sweep_vanished_vms(client, set()) == 0
        assert await manager.sweep_vanished_vms(client, set()) == 1
        assert await manager.sweep_vanished_vms(client, set()) == 0
        assert messages[0]["state"] == "error"
        assert messages[0]["reason"] == "transport_unavailable"
        assert messages[0]["observed_request_id"] == "start-operation"
        assert messages[0]["details"]["cleanup_confirmed"] is False
        assert manager._runtimes[runtime.vm_id] is runtime
        assert managed_vm_owner(runtime.hdc_serial) == (runtime.vm_id, runtime.lease_token)
        assert manager.decorate_devices([DeviceInfo(serial="physical-usb", platform="harmony")])[0].serial == "physical-usb"

        identity = f"harmony-vm:{runtime.vm_id}"
        assert await manager.sweep_vanished_vms(client, {identity}) == 1
        assert messages[-1]["reason"] == "transport_recovered"
        assert messages[-1]["state"] == "running"
        assert messages[-1]["lease_token"] == runtime.lease_token
        assert messages[-1]["observed_request_id"] == "start-operation"
        assert runtime.missing_ticks == 0
    finally:
        unregister_managed_vm(runtime.vm_id, runtime.lease_token)


def test_stale_scan_cannot_report_a_replaced_runtime(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    old = _runtime(tmp_path)
    old.missing_ticks = 2
    manager._runtimes[old.vm_id] = _runtime(tmp_path)
    assert manager._presence_status(old, present=True) is None


def test_transport_missing_runtime_cannot_claim_successful_start(tmp_path):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    runtime.missing_ticks = 2
    manager._runtimes[runtime.vm_id] = runtime
    with pytest.raises(RuntimeError, match="transport_unavailable"):
        manager.start_sync({"vm_id": runtime.vm_id, "assigned_port": runtime.hdc_port, "lease_token": runtime.lease_token})


async def test_start_handler_finishes_only_after_terminal_report(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def send(msg):
        messages.append(msg)

    async def finish(_client, _msg):
        started.set()
        await release.wait()

    monkeypatch.setattr(manager, "_start_and_report", finish)
    task = asyncio.create_task(manager.handle_start(SimpleNamespace(send=send), {"vm_id": "one"}))
    await started.wait()
    assert not task.done()
    assert messages[0]["state"] == "starting"
    release.set()
    await task


@pytest.mark.parametrize("condition,error", [("missing", "instance_missing"), ("changed", "config_changed")])
def test_restart_never_silently_recreates_private_data(tmp_path, monkeypatch, condition, error):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    msg = {"vm_id": "preserve", "assigned_port": 12342, "lease_token": "lease-preserve", "device_type": "Phone"}
    config = manager.instance_path / "aiphone_harmony_preserve" / "config.ini"
    if condition == "changed":
        config.parent.mkdir(parents=True)
        config.write_text("private=data")
    manager._known["preserve"] = {
        "created": True, "instance_path": str(manager.instance_path),
        "last_config": {"device_type": "Tablet"},
    }
    monkeypatch.setattr(manager, "probe", lambda _msg: {"ok": True})
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(manager, "_resolve_image_root", lambda _msg: "")
    monkeypatch.setattr(manager, "_port_conflict", lambda *_args: "")
    monkeypatch.setattr(manager, "_create_instance", lambda *_args: pytest.fail("must not recreate"))
    monkeypatch.setattr(manager, "_delete_instance_definition", lambda *_args, **_kw: pytest.fail("must not delete"))
    monkeypatch.setattr(manager, "_stop_runtime", lambda *_args, **_kw: {"cleanup_confirmed": False})
    try:
        with pytest.raises(RuntimeError, match=error):
            manager.start_sync(msg)
        assert manager._known["preserve"]["created"] is True
        if condition == "changed":
            assert config.read_text() == "private=data"
    finally:
        unregister_managed_vm("preserve", "lease-preserve")


@pytest.mark.parametrize("os_version,api,expected", [
    ("HarmonyOS 6.0.31", "23", "HarmonyOS 6.0.31(23)"),
    ("HarmonyOS 6.0.31 (23)", "23", "HarmonyOS 6.0.31(23)"),
    ("6.0.1(21) beta 1", "21", "HarmonyOS 6.0.1(21) Beta1"),
    ("HarmonyOS 6.0.31(23)", "20", ""),
])
def test_image_version_normalization_preserves_api_and_beta(os_version, api, expected):
    assert cap.format_os_version(os_version, api) == expected


def test_image_listing_keeps_distinct_architectures_and_normalizes_spelling():
    # Representative JSON shapes; this is not a live Windows certification.
    raw = json.dumps({"Phone": [
        {"deviceType": "Phone", "osVersion": "HarmonyOS 6.0.31(23)", "abi": "arm"},
        {"device_type": "phone", "os_version": "HarmonyOS 6.0.31 (23)", "cpuArch": "x64"},
    ]})
    rows = cap._normalize_images(raw, downloaded=True)
    assert {row["os_version"] for row in rows} == {"HarmonyOS 6.0.31(23)"}
    assert {row["abi"] for row in rows} == {"arm64", "x86_64"}


def _installed_image(tmp_path, abi="arm"):
    image = tmp_path / "system-image" / "HarmonyOS-6.0.31" / f"phone_all_{abi}"
    image.mkdir(parents=True, exist_ok=True)
    (image / "info.json").write_text(json.dumps({"apiVersion": "23", "abi": abi, "version": "6.1.0.115"}))
    (image / "system.img").write_bytes(b"fixture-system")
    return image


@pytest.mark.parametrize("abi,resolved", [("arm", "arm64"), ("x86_64", "x86_64")])
def test_shared_phone_image_covers_display_families_without_online_catalog(tmp_path, abi, resolved):
    _installed_image(tmp_path, abi)
    ok, raw = cap.list_downloaded_images(cap.HarmonyVmTools("/fake/Emulator"), {
        "device_type": "Foldable", "os_version": "HarmonyOS 6.0.31(23)", "abi": resolved,
    }, image_roots=[tmp_path])
    assert ok is True
    rows = json.loads(raw)
    assert {row["device_type"] for row in rows} == {"Phone", "Foldable", "WideFold", "TripleFold"}
    assert {row["abi"] for row in rows} == {resolved}


def _mock_static_environment(tmp_path, monkeypatch):
    image = _installed_image(tmp_path)
    tool = tmp_path / "Emulator"
    tool.write_text("fixture-tool")
    calls = []
    monkeypatch.setattr(cap, "host_abi", lambda: "arm64")
    monkeypatch.setattr(cap, "find_harmony_tools", lambda: (cap.HarmonyVmTools(str(tool)), []))
    monkeypatch.setattr(cap, "hdc_available", lambda: True)
    monkeypatch.setattr(cap, "install_harmony_fport_patch", lambda: (True, "patched"))
    monkeypatch.setattr(cap, "patch_state", lambda: {"installed": True})
    monkeypatch.setattr(cap, "discover_harmony_image_roots", lambda _roots: [str(tmp_path)])
    monkeypatch.setattr(cap, "dry_run_create", lambda *_args, **_kwargs: (calls.append("create") is None, ""))
    monkeypatch.setattr(cap, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(cap, "_listener_ports", lambda: set())
    monkeypatch.setattr(cap, "_forwarded_ports", lambda _targets: set())
    monkeypatch.setattr(cap, "available_memory_mb", lambda: 64000)
    monkeypatch.setattr(cap, "_license_status", lambda _tools: pytest.fail("must not require all agreements"))
    return image, tool, calls


def _probe():
    return cap.probe_harmony_vm_capability({
        "device_type": "Foldable", "os_version": "HarmonyOS 6.0.31(23)", "abi": "auto",
    }, current_instances=2, max_instances=8)


def test_static_cache_reuses_creation_but_always_reads_live_ports_and_memory(tmp_path, monkeypatch):
    _image, _tool, calls = _mock_static_environment(tmp_path, monkeypatch)
    first = _probe()
    monkeypatch.setattr(cap, "_listener_ports", lambda: {12345})
    monkeypatch.setattr(cap, "available_memory_mb", lambda: 500)
    second = _probe()
    assert first["ok"] and second["ok"]
    assert calls == ["create"]
    assert first["details"]["static_probe_cached"] is False
    assert second["details"]["static_probe_cached"] is True
    assert first["details"]["local_excluded_ports"] == []
    assert second["details"]["local_excluded_ports"] == [12345]
    assert second["details"]["available_memory_mb"] == 500


@pytest.mark.parametrize("change", ["tool", "metadata", "removed_image", "expiry"])
def test_static_cache_invalidates_environment_changes(tmp_path, monkeypatch, change):
    image, tool, calls = _mock_static_environment(tmp_path, monkeypatch)
    assert _probe()["ok"]
    if change == "tool":
        tool.write_text("updated-fixture-tool")
    elif change == "metadata":
        (image / "info.json").write_text('{"apiVersion":"23","abi":"arm","version":"new-build"}')
    elif change == "removed_image":
        (image / "system.img").unlink()
        monkeypatch.setattr(cap, "_run", lambda *_args, **_kwargs: (1, "offline"))
    else:
        cap._STATIC_PROBE_CACHE = {key: (0, result) for key, (_, result) in cap._STATIC_PROBE_CACHE.items()}
    after = _probe()
    if change == "removed_image":
        assert not after["ok"]
        assert calls == ["create"]
    else:
        assert after["ok"]
        assert calls == ["create", "create"]
        assert after["details"]["static_probe_cached"] is False


def test_architecture_mismatch_does_not_pass_installed_image_check(tmp_path, monkeypatch):
    _image, _tool, calls = _mock_static_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "host_abi", lambda: "x86_64")
    monkeypatch.setattr(cap, "_run", lambda *_a, **_kw: (1, "offline"))
    result = _probe()
    assert not result["ok"]
    assert "x86_64" in result["reason"]
    assert calls == []


def test_failed_create_is_not_cached(tmp_path, monkeypatch):
    _image, _tool, calls = _mock_static_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(cap, "dry_run_create", lambda *_a, **_kw: (False, "incomplete image"))
    assert not _probe()["ok"]
    assert cap._STATIC_PROBE_CACHE == {}
    monkeypatch.setattr(cap, "dry_run_create", lambda *_a, **_kw: (True, ""))
    assert _probe()["ok"]
    assert calls == []


def test_dry_run_has_bounded_commands_and_rejects_cleanup_failure(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return (0, "Device create success") if "-create" in args else (1, "delete failed")
    monkeypatch.setattr(cap, "_run", run)
    ok, reason = cap.dry_run_create(cap.HarmonyVmTools("/fake/Emulator"), {
        "device_type": "Phone", "os_version": "HarmonyOS 6.0.31(23)",
    })
    assert ok is False
    assert "清理未确认" in reason
    assert [kwargs["timeout"] for _, kwargs in calls] == [15.0, 5.0]


def test_live_port_probe_failure_cannot_return_cached_success(tmp_path, monkeypatch):
    _mock_static_environment(tmp_path, monkeypatch)
    assert _probe()["ok"]
    def fail(_targets):
        raise TimeoutError("fport scan timed out")
    monkeypatch.setattr(cap, "_forwarded_ports", fail)
    result = _probe()
    assert not result["ok"]
    assert "端口占用" in result["reason"]


def test_fport_probe_skips_offline_targets_and_bounds_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(cap, "hdc_run", lambda *_args, **kw: calls.append(kw) or "tcp:16556 tcp:8012")
    ports = cap._forwarded_ports([
        SimpleNamespace(serial="stale", status="Offline"),
        SimpleNamespace(serial="live", status="Connected"),
    ])
    assert ports == {16556}
    assert len(calls) == 1 and calls[0]["serial"] == "live"
    assert calls[0]["timeout"] <= 1.0


async def test_orphan_inventory_marks_pending_and_unverified_vms_unknown(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(HarmonyVmManager, "orphan_cleanup", property(lambda _self: True))
    manager._known = {
        "pending": {"cleanup_pending": True},
        "unverified": {"ready": True},
        "missing": {"created": True, "instance_name": "aiphone_harmony_missing", "instance_path": str(manager.instance_path)},
    }
    messages = []
    async def send(msg):
        messages.append(msg)
    assert await manager.report_orphan_reconcile(SimpleNamespace(agent_id="fixture", send=send)) == 0
    assert messages[0]["inventory_complete"] is True
    assert messages[0]["unknown_vm_ids"] == ["pending", "unverified"]


@pytest.mark.parametrize("condition", ["untracked", "corrupt_registry"])
def test_unknown_inventory_cannot_prove_server_instance_absent(tmp_path, condition):
    if condition == "corrupt_registry":
        (tmp_path / "registry.json").write_text("incomplete{")
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    if condition == "untracked":
        (manager.instance_path / "aiphone_harmony_unknown").mkdir(parents=True)
    complete, _untracked = manager._inventory_completeness()
    assert complete is False


@pytest.mark.parametrize("saved,active,port_free,expected", [
    (False, "", True, False),
    (True, "", True, True),
    (True, None, True, False),
    (True, "127.0.0.1:12341", True, False),
    (True, "", False, False),
])
def test_lost_stop_ack_recovery_requires_positive_cleanup_evidence(
    tmp_path, monkeypatch, saved, active, port_free, expected
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    known = {"ready": False, "cleanup_confirmed": saved, "hdc_serial": "127.0.0.1:12341"}
    monkeypatch.setattr(manager, "_active_instance_serial", lambda *_a: active)
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: port_free)
    assert manager._stopped_cleanup_confirmed("one", known) is expected


def test_unreadable_registry_cannot_be_overwritten_by_start_or_delete(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text("broken{")
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    with pytest.raises(RuntimeError, match="registry_unreadable"):
        manager.start_sync({"vm_id": "new"})
    assert manager.stop_sync("old")["ok"] is False
    assert manager.delete_sync("old")["ok"] is False
    assert registry.read_text() == "broken{"


def test_owned_process_still_running_prevents_false_stop_confirmation(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    def no_exit(**_kwargs):
        raise TimeoutError("process refuses to exit")
    runtime.process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=no_exit, kill=lambda: None)
    manager._runtimes[runtime.vm_id] = runtime
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(lifecycle, "hdc_run", lambda *_a, **_kw: "")
    monkeypatch.setattr(manager, "_remove_fport", lambda _runtime: None)
    monkeypatch.setattr(manager, "_wait_removed", lambda *_a: (True, "removed"))
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_a: (True, []))
    result = manager.stop_sync(runtime.vm_id)
    assert result["ok"] is False
    assert result["details"]["cleanup_mode"] == "process_still_running"
    assert manager._known[runtime.vm_id]["cleanup_pending"] is True
    assert manager._known[runtime.vm_id]["cleanup_confirmed"] is False


@pytest.fixture
def failed_binding_environment(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    state = {"actions": [], "stop_ok": True, "delete_ok": True, "create_stage": "success"}
    msg = {
        "vm_id": "firstbind", "request_id": "bind-operation", "assigned_port": 12343,
        "lease_token": "bind-lease", "device_type": "Phone", "os_version": "6.0.31",
        "api_version": "23", "rollback_new_instance": True,
    }
    monkeypatch.setattr(manager, "probe", lambda _msg: {"ok": True})
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(manager, "_resolve_image_root", lambda _msg: "")
    monkeypatch.setattr(manager, "_port_conflict", lambda *_args: "")
    monkeypatch.setattr(lifecycle, "_apply_instance_uuid", lambda *_args: None)
    monkeypatch.setattr(manager, "_remove_fport", lambda _runtime: None)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    monkeypatch.setattr(manager, "_wait_removed", lambda *_args: (state["stop_ok"], "removed" if state["stop_ok"] else "unconfirmed"))
    monkeypatch.setattr(lifecycle, "hdc_run", lambda *_args, **_kwargs: "")

    def create(_tool, runtime, _msg):
        state["actions"].append("create")
        if state["create_stage"] == "before_definition":
            raise RuntimeError("create failed")
        config = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
        config.parent.mkdir(parents=True)
        config.write_text("user-data=keep\n")
        if state["create_stage"] == "partial_definition":
            raise RuntimeError("create failed")

    class Process:
        returncode = None
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = 0
        def wait(self, **_kwargs):
            return self.returncode

    def spawn(*_args, **_kwargs):
        state["actions"].append("spawn")
        return Process()

    def boot_failure(*_args, **_kwargs):
        raise RuntimeError("boot failed")

    def native(args, **_kwargs):
        assert args[0] == "/fake/Emulator"
        assert args[2] == "aiphone_harmony_firstbind"
        if args[1] == "-delete":
            state["actions"].append("delete")
            assert args[args.index("-instancePath") + 1] == str(manager.instance_path)
            if state["delete_ok"]:
                shutil.rmtree(manager.instance_path / "aiphone_harmony_firstbind", ignore_errors=True)
            else:
                return SimpleNamespace(returncode=1, stdout="delete failed", stderr="")
        else:
            assert args[1] == "-stop"
            state["actions"].append("stop")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_create_instance", create)
    monkeypatch.setattr(lifecycle, "_spawn_harmony_emulator", spawn)
    monkeypatch.setattr(manager, "_wait_hdc", boot_failure)
    monkeypatch.setattr(lifecycle.subprocess, "run", native)
    yield manager, msg, state
    unregister_managed_vm(msg["vm_id"], msg["lease_token"])


@pytest.mark.parametrize("stage", ["success", "before_definition", "partial_definition"])
def test_first_bind_failure_rolls_back_only_its_new_definition_once(failed_binding_environment, stage):
    manager, msg, state = failed_binding_environment
    state["create_stage"] = stage
    with pytest.raises(RuntimeError, match="failed") as caught:
        manager.start_sync(msg)
    details = caught.value.cleanup_details
    assert details["rollback_completed"] is True
    assert details["cleanup_confirmed"] is True
    assert details["instance_created"] is False
    assert details["created_in_attempt"] is (stage != "before_definition")
    assert "details" not in details
    assert state["actions"].count("stop") == 1
    assert state["actions"].count("delete") == 1
    assert msg["vm_id"] not in manager._known
    assert msg["vm_id"] not in manager._runtimes
    assert not (manager.instance_path / "aiphone_harmony_firstbind").exists()


@pytest.mark.parametrize("failure", ["stop", "delete"])
async def test_failed_first_bind_cleanup_retains_exact_orphan_for_reconnect(
    failed_binding_environment, monkeypatch, failure
):
    manager, msg, state = failed_binding_environment
    state[f"{failure}_ok"] = False
    with pytest.raises(RuntimeError, match="boot failed") as caught:
        manager.start_sync(msg)
    details = caught.value.cleanup_details
    assert details["rollback_completed"] is False
    assert details["cleanup_confirmed"] is False
    assert details["instance_created"] is True
    assert manager._known[msg["vm_id"]]["rollback_pending"] is True
    assert state["actions"].count("stop") == 1
    assert state["actions"].count("delete") == (0 if failure == "stop" else 1)
    messages = []
    async def send(payload):
        messages.append(payload)
    monkeypatch.setattr(HarmonyVmManager, "orphan_cleanup", property(lambda _self: True))
    assert await manager.report_orphan_reconcile(SimpleNamespace(agent_id="isolated", send=send)) == 1
    row = messages[0]["instances"][0]
    assert row["state"] == "error"
    assert row["lease_token"] == "bind-lease"
    assert row["hdc_serial"] == "127.0.0.1:12343"
    assert row["details"]["cleanup_confirmed"] is False
    previous_actions = list(state["actions"])
    with pytest.raises(RuntimeError, match="cleanup_required"):
        manager.start_sync(msg)
    assert state["actions"] == previous_actions


@pytest.mark.parametrize("rollback_flag", [False, True])
@pytest.mark.parametrize("legacy_registry", [False, True])
def test_bound_restart_failure_never_deletes_existing_userdata(failed_binding_environment, rollback_flag, legacy_registry):
    manager, msg, state = failed_binding_environment
    msg.update(rollback_new_instance=rollback_flag, require_existing_instance=True)
    config = manager.instance_path / "aiphone_harmony_firstbind" / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("old-user-data=retain\n")
    manager._known[msg["vm_id"]] = {
        "created": True, "instance_path": str(manager.instance_path),
        "instance_name": "aiphone_harmony_firstbind",
        "last_config": lifecycle._persistent_message_config(msg),
    }
    if legacy_registry:
        manager._known[msg["vm_id"]].pop("created")
    with pytest.raises(RuntimeError, match="boot failed") as caught:
        manager.start_sync(msg)
    assert state["actions"] == ["spawn", "stop"]
    assert config.read_text() == "old-user-data=retain\n"
    assert caught.value.cleanup_details["instance_created"] is True
    assert caught.value.cleanup_details["created_in_attempt"] is False
    assert caught.value.cleanup_details["rollback_new_instance"] is False
    assert manager._known[msg["vm_id"]]["created"] is True
    # Stop and reconnect retain the last exact lease identity for Server's
    # stale-lease fence; they never acknowledge whichever lease is current.
    assert manager._known[msg["vm_id"]]["lease_token"] == "bind-lease"
    assert manager._known[msg["vm_id"]]["hdc_serial"] == "127.0.0.1:12343"


def test_required_existing_instance_is_never_created_from_missing_registry(failed_binding_environment):
    manager, msg, state = failed_binding_environment
    msg["require_existing_instance"] = True
    with pytest.raises(RuntimeError, match="instance_owner_unverified") as caught:
        manager.start_sync(msg)
    assert "create" not in state["actions"]
    assert "delete" not in state["actions"]
    assert caught.value.cleanup_details["instance_created"] is False


def test_precreation_rejection_does_not_claim_an_instance_exists(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    monkeypatch.setattr(manager, "probe", lambda _msg: {"ok": False, "reason": "image missing"})
    with pytest.raises(RuntimeError, match="image missing") as caught:
        manager.start_sync({"vm_id": "no-definition", "assigned_port": 12344, "lease_token": "unused", "rollback_new_instance": True})
    assert caught.value.cleanup_details == {
        "instance_created": False, "cleanup_confirmed": True, "created_in_attempt": False,
    }
    assert manager._known == {}


async def test_first_bind_error_reports_flat_terminal_cleanup_evidence(failed_binding_environment):
    manager, msg, _state = failed_binding_environment
    messages = []
    async def send(payload):
        messages.append(payload)
        return True
    async def refresh():
        return None
    await manager.handle_start(SimpleNamespace(send=send, refresh_devices=refresh), msg)
    assert [payload["state"] for payload in messages] == ["starting", "error"]
    terminal = messages[-1]
    assert terminal["request_id"] == "bind-operation"
    assert terminal["lease_token"] == "bind-lease"
    assert terminal["details"]["cleanup_confirmed"] is True
    assert terminal["details"]["rollback_completed"] is True
    assert terminal["details"]["instance_created"] is False
    assert terminal["details"]["created_in_attempt"] is True
    assert "details" not in terminal["details"]


@pytest.fixture
def startup_clock(monkeypatch):
    clock = {"now": 100.0}
    def sleep(seconds):
        clock["now"] += seconds
    fake_time = SimpleNamespace(monotonic=lambda: clock["now"], time=time.time, sleep=sleep)
    monkeypatch.setattr(lifecycle, "time", fake_time)
    monkeypatch.setattr(cap, "time", fake_time)
    return clock


@pytest.mark.parametrize("existing", [False, True])
def test_spent_probe_budget_never_creates_or_spawns_and_preserves_existing_data(
    failed_binding_environment, startup_clock, monkeypatch, existing
):
    manager, msg, state = failed_binding_environment
    msg["operation_timeout_sec"] = 0.1
    config = manager.instance_path / "aiphone_harmony_firstbind" / "config.ini"
    if existing:
        config.parent.mkdir(parents=True)
        config.write_text("keep-old-data")
        manager._known[msg["vm_id"]] = {
            "created": True, "instance_name": "aiphone_harmony_firstbind",
            "instance_path": str(manager.instance_path),
        }
        msg.update(require_existing_instance=True, rollback_new_instance=False)
    def slow_probe(_msg):
        startup_clock["now"] += 0.2
        return {"ok": True}
    monkeypatch.setattr(manager, "probe", slow_probe)
    with pytest.raises(RuntimeError, match="startup deadline exceeded"):
        manager.start_sync(msg)
    assert state["actions"] == []
    assert config.read_text() == "keep-old-data" if existing else not config.exists()


def test_late_creation_result_cannot_spawn_after_deadline(failed_binding_environment, startup_clock, monkeypatch):
    manager, msg, state = failed_binding_environment
    msg["operation_timeout_sec"] = 0.1
    create = manager._create_instance
    def late_create(*args):
        create(*args)
        startup_clock["now"] += 0.2
    monkeypatch.setattr(manager, "_create_instance", late_create)
    with pytest.raises(RuntimeError, match="startup deadline exceeded") as caught:
        manager.start_sync(msg)
    assert state["actions"] == ["create", "stop", "delete"]
    assert caught.value.cleanup_details["rollback_completed"] is True
    assert msg["vm_id"] not in manager._known


@pytest.mark.parametrize("slow_phase", ["driver", "fold"])
def test_late_driver_or_fold_result_cannot_succeed_or_delete_bound_data(
    failed_binding_environment, startup_clock, monkeypatch, slow_phase
):
    manager, msg, state = failed_binding_environment
    msg.update(operation_timeout_sec=0.1, require_existing_instance=True, rollback_new_instance=False)
    config = manager.instance_path / "aiphone_harmony_firstbind" / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("keep-old-data")
    manager._known[msg["vm_id"]] = {
        "created": True, "instance_name": "aiphone_harmony_firstbind",
        "instance_path": str(manager.instance_path), "last_config": lifecycle._persistent_message_config(msg),
    }
    driver = SimpleNamespace(
        close=lambda: state["actions"].append("close-driver"),
        get_raw_driver=lambda: SimpleNamespace(_client=SimpleNamespace(local_port=16557)),
    )
    def driver_ready(_runtime, *, deadline):
        assert deadline == pytest.approx(100.1)
        if slow_phase == "driver":
            startup_clock["now"] += 0.2
        return driver
    def fold_ready(*_args):
        state["actions"].append("fold")
        startup_clock["now"] += 0.2
    monkeypatch.setattr(manager, "_wait_hdc", lambda *_a, **_kw: None)
    monkeypatch.setattr(manager, "_wait_driver", driver_ready)
    monkeypatch.setattr(manager, "_apply_fold_state", fold_ready)
    with pytest.raises(RuntimeError, match="startup deadline exceeded"):
        manager.start_sync(msg)
    assert "delete" not in state["actions"]
    assert "create" not in state["actions"]
    assert state["actions"].count("stop") == 1
    assert state["actions"].count("close-driver") == 1
    assert ("fold" in state["actions"]) is (slow_phase == "fold")
    assert config.read_text() == "keep-old-data"


def test_native_create_receives_only_remaining_operation_budget(tmp_path, startup_clock, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    runtime.operation_deadline = 100.4
    calls = []
    def create(_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(lifecycle.subprocess, "run", create)
    manager._create_instance("/fake/Emulator", runtime, {"device_type": "Phone", "os_version": "6.0.31", "api_version": "23"})
    assert calls[0]["timeout"] == pytest.approx(0.4)


def test_probe_create_budget_expiry_still_cleans_once_but_cannot_succeed(startup_clock, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args[1], kwargs["timeout"]))
        if args[1] == "-create":
            startup_clock["now"] += 0.5
            return 0, "Device create success"
        return 0, "Device delete success"
    monkeypatch.setattr(cap, "_run", run)
    with pytest.raises(TimeoutError, match="startup deadline exceeded"):
        cap.dry_run_create(cap.HarmonyVmTools("/fake/Emulator"), {
            "device_type": "Phone", "os_version": "6.0.31", "api_version": "23", "_operation_deadline": 100.4,
        })
    assert calls == [("-create", pytest.approx(0.4)), ("-delete", 5.0)]


def test_tool_discovery_stops_after_one_late_native_call(tmp_path, startup_clock, monkeypatch):
    tool = tmp_path / "Emulator"
    tool.write_text("fixture")
    tool.chmod(0o700)
    calls = []
    monkeypatch.setattr(cap, "_emulator_candidates", lambda: iter((str(tool), "/unused/Emulator")))
    def run(_args, **kwargs):
        calls.append(kwargs["timeout"])
        startup_clock["now"] += 0.5
        return 0, "HarmonyOS -imageList -screenProfileList -hdcPort"
    monkeypatch.setattr(cap, "_run", run)
    with pytest.raises(TimeoutError, match="startup deadline exceeded"):
        cap.find_harmony_tools(deadline=100.4)
    assert calls == [pytest.approx(0.4)]


async def test_executor_queue_delay_cannot_restart_expired_start_budget(
    failed_binding_environment, startup_clock, monkeypatch
):
    manager, msg, state = failed_binding_environment
    msg["operation_timeout_sec"] = 0.1
    messages = []
    async def queued_worker(func, *args, **kwargs):
        startup_clock["now"] += 0.2
        return func(*args, **kwargs)
    async def send(payload):
        messages.append(payload)
    async def refresh():
        return None
    monkeypatch.setattr(lifecycle.asyncio, "to_thread", queued_worker)
    await manager.handle_start(SimpleNamespace(send=send, refresh_devices=refresh), msg)
    assert [payload["state"] for payload in messages] == ["starting", "error"]
    assert "startup deadline exceeded" in messages[-1]["error"]
    assert messages[-1]["details"]["cleanup_confirmed"] is True
    assert state["actions"] == []


async def test_real_worker_timeout_preserves_terminal_cleanup_evidence(
    failed_binding_environment, monkeypatch
):
    manager, msg, state = failed_binding_environment
    def probe_timeout(_msg):
        raise TimeoutError("native probe deadline exceeded")
    monkeypatch.setattr(manager, "probe", probe_timeout)
    messages = []
    async def send(payload):
        messages.append(payload)
    async def refresh():
        return None
    # Do not replace asyncio.to_thread: Python may normalize a TimeoutError
    # crossing its real executor boundary and lose attached attributes.
    await manager.handle_start(SimpleNamespace(send=send, refresh_devices=refresh), msg)
    assert messages[-1]["state"] == "error"
    assert messages[-1]["details"] == {
        "instance_created": False, "created_in_attempt": False, "cleanup_confirmed": True,
    }
    assert state["actions"] == []


@pytest.mark.parametrize("source", ["new", "preexisting", "reused_running"])
async def test_actual_agent_terminal_provenance_controls_server_abandoned_cleanup(
    failed_binding_environment, monkeypatch, source
):
    from ai_phone.server.harmony_vm.models import HarmonyVmInstance
    from ai_phone.server.vm_lifecycle import (
        begin_operation, cleanup_abandoned_first_bind, fail_attempt,
        record_creation_provenance,
    )
    from ai_phone.shared import protocol as P

    manager, msg, _state = failed_binding_environment
    monkeypatch.setattr(manager, "_wait_hdc", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_wait_driver", lambda *_args, **_kwargs: SimpleNamespace(
        get_raw_driver=lambda: SimpleNamespace(_client=SimpleNamespace(local_port=16558)),
        close=lambda: None,
    ))
    messages = []
    async def send(payload):
        messages.append(payload)
    async def refresh():
        return None
    client = SimpleNamespace(send=send, refresh_devices=refresh)
    config = manager.instance_path / "aiphone_harmony_firstbind" / "config.ini"
    if source == "preexisting":
        config.parent.mkdir(parents=True)
        config.write_text("old-private-data")
        manager._known[msg["vm_id"]] = {
            "created": True, "instance_name": "aiphone_harmony_firstbind",
            "instance_path": str(manager.instance_path),
            "last_config": lifecycle._persistent_message_config(msg),
            # A previous attempt's positive bit must never authorize deleting
            # old data during this different request.
            "created_in_attempt": True, "observed_request_id": "previous-request",
        }
    elif source == "reused_running":
        await manager.handle_start(client, {**msg, "request_id": "previous-request"})
        assert messages[-1]["details"]["created_in_attempt"] is True

    vm = HarmonyVmInstance(id=msg["vm_id"], name="fixture", alias="fixture", state="draft", runtime={})
    msg["request_id"] = begin_operation(
        vm, "start", first_bind=True, target_agent_id="isolated", timeout_sec=30,
    )
    # Server has already failed the first-bind transaction when the real
    # Agent completion arrives. Only the host calls are replaced by fixtures.
    fail_attempt(vm, "operation_timeout", "fixture deadline expired")
    sent = []
    async def send_to_agent(agent_id, payload):
        sent.append((agent_id, payload))
        return True
    hub = SimpleNamespace(send_to_agent=send_to_agent)
    try:
        await manager.handle_start(client, msg)
        terminal = messages[-1]
        assert terminal["state"] == "running"
        assert terminal["request_id"] == msg["request_id"]
        assert terminal["details"]["created_in_attempt"] is (source == "new")
        runtime = manager._runtimes[msg["vm_id"]]
        assert runtime.observed_request_id == msg["request_id"]
        assert runtime.created_in_attempt is (source == "new")
        record_creation_provenance(vm, terminal)
        cleaned = await cleanup_abandoned_first_bind(hub, vm, "isolated", terminal["details"], terminal)
        assert cleaned is (source == "new")
        assert vm.assigned_agent_id is None
        if source == "new":
            assert len(sent) == 1
            assert sent[0][1]["type"] == P.MSG_HARMONY_VM_DELETE
            assert sent[0][1]["vm_id"] == msg["vm_id"]
            assert sent[0][1]["hdc_serial"] == terminal["hdc_serial"]
            assert sent[0][1]["lease_token"] == terminal["lease_token"]
            assert not await cleanup_abandoned_first_bind(hub, vm, "isolated", terminal["details"], terminal)
            assert len(sent) == 1
        else:
            assert sent == []
            assert config.is_file()
            if source == "preexisting":
                assert config.read_text() == "old-private-data"
    finally:
        manager.stop_sync(msg["vm_id"], lease_token=msg["lease_token"])


@pytest.mark.parametrize("target_status,bindable,expected,count", [
    (None, True, (True, "removed"), 1),
    ("Offline", True, (True, "offline_hdc_target"), 4),
    ("Offline", False, (False, "unconfirmed"), 30),
    ("Connected", True, (False, "unconfirmed"), 30),
])
def test_exit_evidence_preserves_existing_removal_gates(
    tmp_path, startup_clock, monkeypatch, target_status, bindable, expected, count
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    serial = "127.0.0.1:12341"
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [] if target_status is None else [
        SimpleNamespace(serial=serial, status=target_status),
    ])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: bindable)
    assert manager._wait_removed(serial, 12341) == expected
    evidence = manager._last_removal_evidence
    assert evidence["sample_count"] == count
    assert evidence["last_sample"]["target_status"] == (target_status.lower() if target_status else "absent")
    assert evidence["last_sample"]["port_bindable"] is bindable
    assert evidence["budget_sec"] == 15.0
    assert evidence["elapsed_sec"] <= 15.0
    assert "samples" not in evidence  # bounded summary, not an ever-growing log


def test_exit_evidence_explains_too_few_offline_samples_without_loosening_gate(
    tmp_path, startup_clock, monkeypatch
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    def slow_targets():
        startup_clock["now"] += 8.0
        return [SimpleNamespace(serial="127.0.0.1:12341", status="Offline")]
    monkeypatch.setattr(lifecycle, "hdc_list_targets", slow_targets)
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    assert manager._wait_removed("127.0.0.1:12341", 12341) == (False, "unconfirmed")
    evidence = manager._last_removal_evidence
    assert evidence["sample_count"] == 2
    assert evidence["max_sample_duration_sec"] == 8.0
    assert evidence["max_offline_free_samples"] == 2
    assert evidence["last_sample"]["port_bindable"] is True


def test_exit_evidence_records_query_failure_as_unknown(tmp_path, startup_clock, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    def denied():
        raise PermissionError("HDC query denied")
    monkeypatch.setattr(lifecycle, "hdc_list_targets", denied)
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: pytest.fail("no successful target query"))
    assert manager._wait_removed("127.0.0.1:12341", 12341) == (False, "unconfirmed")
    evidence = manager._last_removal_evidence
    assert evidence["last_sample"]["target_status"] == "unknown"
    assert evidence["last_sample"]["port_bindable"] is None
    assert evidence["last_sample"]["error"] == "PermissionError:HDC query denied"


@pytest.mark.parametrize("target_status,bindable,expected_ok", [
    (None, True, True), ("Offline", True, True), ("Offline", False, False),
])
def test_no_target_remove_diagnostic_does_not_override_synchronous_exit_facts(
    tmp_path, startup_clock, monkeypatch, target_status, bindable, expected_ok
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    runtime.process = SimpleNamespace(poll=lambda: 0)
    manager._runtimes[runtime.vm_id] = runtime
    manager._known[runtime.vm_id] = {**runtime.persistent_dict(), "created": True}
    config = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("fixture-private-data")
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(manager, "_remove_fport", lambda _runtime: None)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    monkeypatch.setattr(lifecycle, "hdc_run", lambda *_args, **_kwargs: "[Fail]No target available")
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [] if target_status is None else [
        SimpleNamespace(serial=runtime.hdc_serial, status=target_status),
    ])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: bindable)
    commands, stop_results = [], []
    def native(args, **_kwargs):
        assert args[0] == "/fake/Emulator"
        assert args[2] == runtime.instance_name
        commands.append(args[1])
        if args[1] == "-delete":
            assert args[args.index("-instancePath") + 1] == runtime.instance_path
            shutil.rmtree(config.parent)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(lifecycle.subprocess, "run", native)
    original_stop = manager.stop_sync
    def capture_stop(*args, **kwargs):
        result = original_stop(*args, **kwargs)
        stop_results.append(result)
        return result
    monkeypatch.setattr(manager, "stop_sync", capture_stop)
    result = manager.delete_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    assert result["ok"] is expected_ok
    assert stop_results[0]["error"] == "hdc tconn -remove: [Fail]No target available"
    assert stop_results[0]["ok"] is expected_ok
    assert commands == (["-stop", "-delete"] if expected_ok else ["-stop"])
    evidence = result["details"]["exit_evidence"]
    assert evidence["last_sample"]["target_status"] == (target_status.lower() if target_status else "absent")
    assert evidence["last_sample"]["port_bindable"] is bindable
    assert evidence["process_handle_present"] is True
    assert evidence["process_return_code"] == 0
    if expected_ok:
        assert not config.parent.exists()
        assert runtime.vm_id not in manager._known
    else:
        assert config.is_file()
        assert manager._known[runtime.vm_id]["cleanup_pending"] is True
        assert manager._known[runtime.vm_id]["exit_evidence"] == evidence


def test_pending_registry_cleanup_reports_fresh_absence_evidence_without_repeating_stop(
    tmp_path, startup_clock, monkeypatch
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    manager._known[runtime.vm_id] = {
        **runtime.persistent_dict(), "created": True, "ready": False, "cleanup_pending": True,
    }
    config = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("fixture-private-data")
    monkeypatch.setattr(manager, "_active_instance_serial", lambda *_args: "")
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [SimpleNamespace(serial=runtime.hdc_serial, status="Offline")])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    commands = []
    def native(args, **_kwargs):
        assert args[0] == "/fake/Emulator" and args[1] == "-delete"
        assert args[2] == runtime.instance_name
        assert args[args.index("-instancePath") + 1] == runtime.instance_path
        commands.append(args[1])
        shutil.rmtree(config.parent)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(lifecycle.subprocess, "run", native)
    result = manager.delete_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    assert result["ok"] is True
    assert commands == ["-delete"]
    evidence = result["details"]["exit_evidence"]
    assert evidence["source"] == "registry_instance_absence"
    assert evidence["instance_process_status"] == "absent"
    assert evidence["last_sample"]["target_status"] == "offline"
    assert evidence["last_sample"]["port_bindable"] is True
    assert not config.parent.exists()


def test_confirmed_stopped_record_rechecks_facts_without_repeating_native_stop(tmp_path, monkeypatch):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    manager._known[runtime.vm_id] = {
        **runtime.persistent_dict(), "ready": False, "cleanup_pending": False, "cleanup_confirmed": True,
    }
    inspected = []
    monkeypatch.setattr(manager, "_active_instance_serial", lambda *_args: inspected.append(True) or "")
    monkeypatch.setattr(lifecycle, "managed_vm_owner", lambda _serial: None)
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_args: (True, []))
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    assert result["ok"] is True and result["reason"] == "not_running"
    assert result["details"]["cleanup_confirmed"] is True
    assert inspected == [True]
    assert result["details"]["exit_evidence"]["instance_process_status"] == "absent"


@pytest.mark.parametrize("native_state", ["unknown", "port_busy"])
@pytest.mark.parametrize("stored_confirmation", [False, True])
def test_legacy_stopped_bit_does_not_override_unknown_or_busy_native_state(tmp_path, monkeypatch, native_state, stored_confirmation):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    manager._known[runtime.vm_id] = {
        **runtime.persistent_dict(), "ready": False, "cleanup_pending": False,
        "cleanup_confirmed": stored_confirmation,
    }
    checked = []
    monkeypatch.setattr(manager, "_active_instance_serial", lambda *_args: checked.append("process") or (None if native_state == "unknown" else ""))
    monkeypatch.setattr(lifecycle, "managed_vm_owner", lambda _serial: None)
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [SimpleNamespace(serial=runtime.hdc_serial, status="Offline")])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: False)
    result = manager.stop_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    assert checked == ["process"]
    assert result["ok"] is False and result["details"]["cleanup_confirmed"] is False
    assert result["reason"] == ("instance_owner_unverified" if native_state == "unknown" else "instance_absence_unconfirmed")


@pytest.mark.parametrize("process_state", ["absent", "running", "unknown"])
@pytest.mark.parametrize("action", ["stop", "delete"])
def test_reclaimed_instance_requires_post_stop_process_evidence(
    tmp_path, monkeypatch, process_state, action
):
    manager = HarmonyVmManager(runtime_dir=tmp_path)
    runtime = _runtime(tmp_path)
    runtime.process = None
    manager._runtimes[runtime.vm_id] = runtime
    manager._known[runtime.vm_id] = {**runtime.persistent_dict(), "created": True}
    config = Path(runtime.instance_path) / runtime.instance_name / "config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("private userdata must survive an unconfirmed stop")
    commands, inspected = [], []

    def native(args, **_kwargs):
        assert args[0] == "/fake/Emulator" and args[2] == runtime.instance_name
        commands.append(args[1])
        if args[1] == "-delete":
            shutil.rmtree(config.parent)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def inspect(vm_id, known):
        assert vm_id == runtime.vm_id and known["instance_path"] == runtime.instance_path
        assert commands == ["-stop"], "process must be sampled after native stop"
        inspected.append(True)
        return {"absent": "", "running": runtime.hdc_serial, "unknown": None}[process_state]

    monkeypatch.setattr(lifecycle, "find_harmony_tools", lambda: (cap.HarmonyVmTools("/fake/Emulator"), []))
    monkeypatch.setattr(lifecycle.subprocess, "run", native)
    monkeypatch.setattr(lifecycle, "hdc_run", lambda *_a, **_k: "[Fail]No target available")
    monkeypatch.setattr(lifecycle, "hdc_list_targets", lambda: [])
    monkeypatch.setattr(manager, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(manager, "_remove_fport", lambda _runtime: None)
    monkeypatch.setattr(manager, "_cleanup_crash_service", lambda *_a: (True, []))
    monkeypatch.setattr(manager, "_active_instance_serial", inspect)
    operation = manager.stop_sync if action == "stop" else manager.delete_sync
    result = operation(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
    expected_ok = process_state == "absent"
    assert inspected == [True]
    assert result["ok"] is expected_ok
    assert result["details"]["cleanup_confirmed"] is expected_ok
    assert commands == (["-stop", "-delete"] if expected_ok and action == "delete" else ["-stop"])
    evidence = result["details"]["exit_evidence"]
    assert evidence["process_handle_present"] is False
    assert evidence["instance_process_status"] == process_state
    assert evidence["instance_process_sampled_unix"] > 0
    if not expected_ok:
        assert config.is_file()
        assert manager._known[runtime.vm_id]["cleanup_pending"] is True
        assert manager._known[runtime.vm_id]["cleanup_confirmed"] is False
