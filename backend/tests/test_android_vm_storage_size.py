"""Preserve SDK-normalized data capacity using temporary files and fake native operations only."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.android_vm import manager as mod


AVD = "aiphone_vm_a"


@pytest.fixture
def existing_avd(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native operation is forbidden in storage tests")

    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_a, **_k: iter(()))
    monkeypatch.setattr(mod, "_is_port_free", lambda _: True)
    avd_home = tmp_path / "avds"
    directory = avd_home / f"{AVD}.avd"
    directory.mkdir(parents=True)
    monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))
    config = directory / "config.ini"
    config.write_text(config_text("6442450944"))
    witness = directory / "userdata-qemu.img"
    witness.write_bytes(b"preserve existing data")
    tools = mod.AndroidVmTools("/isolated/adb", "/isolated/emulator", "/isolated/avdmanager")
    manager = mod.AndroidVmManager(runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(mod, "find_android_tools", lambda: (tools, []))
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_choose_port", lambda **_: 5554)
    # Image/path proofs have a separate suite; leave the actual ensure/write/error paths intact here.
    monkeypatch.setattr(mod, "_verify_existing_avd_image", lambda *_a, **_kw: None)
    listing = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"Name: {AVD}\nPath: {directory}\n", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", listing)
    return SimpleNamespace(manager=manager, tools=tools, config=config, witness=witness, listing=listing)


def config_text(size):
    return ("# SDK-created formatting must survive an unchanged restart\n"
            f"disk.dataPartition.size = {size}\n"
            "hw.lcd.width = 540\nhw.lcd.height = 960\nhw.lcd.density = 240\n"
            "hw.initialOrientation = portrait\nhw.accelerometer = yes\n"
            "hw.camera.back = emulated\nhw.camera.front = none\nhw.dPad = no\n"
            "hw.gps = yes\nhw.gyroscope = yes\nhw.keyboard = no\nhw.sensors.proximity = no\n")


def write(avd, requested=4096, **extra):
    avd.manager._write_avd_screen_config(
        avd.tools, avd_name=AVD, width=540, height=960, density=240, orientation="portrait",
        internal_storage_mb=requested, preserve_data_size=True, **extra,
    )


def file_state(path):
    return (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) if path.exists() else None


@pytest.mark.parametrize("old", ["6442450944", "6144M", "6144m", "6G", "6g", "6291456K", "6291456k"])
@pytest.mark.parametrize("requested", [0, 4096, 6144])
def test_existing_smaller_equal_or_unspecified_request_preserves_original_file(existing_avd, old, requested):
    avd = existing_avd
    avd.config.write_text(config_text(old))
    before = file_state(avd.config)
    witness = file_state(avd.witness)
    write(avd, requested)
    assert file_state(avd.config) == before
    assert file_state(avd.witness) == witness
    avd.listing.assert_not_called()


def test_sdk_utf8_bom_noop_is_not_rewritten(existing_avd):
    avd = existing_avd
    avd.config.write_text(config_text("6442450944"), encoding="utf-8-sig")
    before = file_state(avd.config)
    write(avd)
    assert file_state(avd.config) == before


@pytest.mark.parametrize("old", ["6442450944", "6144M", "6g"])
def test_explicit_larger_request_uses_existing_megabyte_write_contract(existing_avd, old):
    avd = existing_avd
    avd.config.write_text(config_text(old))
    witness = file_state(avd.witness)
    write(avd, 8192)
    assert "disk.dataPartition.size=8192M\n" in avd.config.read_text()
    assert file_state(avd.witness) == witness


def test_other_changed_settings_do_not_lower_preserved_capacity(existing_avd):
    avd = existing_avd
    write(avd, ram_mb=8192)
    config = avd.config.read_text()
    assert "disk.dataPartition.size=6442450944\n" in config
    assert "hw.ramSize=8192\n" in config


@pytest.mark.parametrize("old", [None, "", "0", "-6G", "6.0G", "6 GB", "6GB", "6GiB", "1e9",
                                  "unknown", "9223372036854775808", "9223372036854775807G", "9" * 5000])
def test_unknown_capacity_rejects_before_any_write(existing_avd, old):
    avd = existing_avd
    if old is None:
        avd.config.write_text("hw.lcd.width=540\n")
    else:
        avd.config.write_text(config_text(old))
    before, witness = file_state(avd.config), file_state(avd.witness)
    with pytest.raises(mod._ExistingAvdConfigError, match="数据容量.*未修改或启动"):
        write(avd, ram_mb=8192)
    assert file_state(avd.config) == before
    assert file_state(avd.witness) == witness


@pytest.mark.parametrize("second", ["6442450944", "4096M"])
def test_duplicate_capacity_is_not_resolved_by_last_value_wins(existing_avd, second):
    avd = existing_avd
    avd.config.write_text(config_text("6442450944") + f"disk.dataPartition.size={second}\n")
    before = file_state(avd.config)
    with pytest.raises(mod._ExistingAvdConfigError):
        write(avd)
    assert file_state(avd.config) == before


@pytest.mark.parametrize("failure", ["missing", "unreadable", "invalid_encoding", "no_path"])
def test_unreadable_existing_config_is_not_recreated(existing_avd, monkeypatch, failure):
    avd = existing_avd
    if failure == "missing":
        avd.config.unlink()
    elif failure == "invalid_encoding":
        avd.config.write_bytes(b"disk.dataPartition.size=6G\n\xff")
    before = file_state(avd.config)
    if failure == "unreadable":
        monkeypatch.setattr(type(avd.config), "read_text", Mock(side_effect=PermissionError("isolated denied")))
    elif failure == "no_path":
        monkeypatch.setattr(mod, "_avd_config_path", lambda *_: None)
    with pytest.raises(mod._ExistingAvdConfigError):
        write(avd)
    assert file_state(avd.config) == before


def test_new_creation_does_not_inherit_existing_capacity_protection(existing_avd):
    avd = existing_avd
    avd.witness.unlink()  # only a freshly generated SDK config exists before the first launch
    avd.listing.return_value.stdout = ""
    assert avd.manager._ensure_avd(
        avd.tools, avd_name=AVD, system_image="test-image", width=540, height=960, density=240,
        orientation="portrait", ram_mb=0, vm_heap_mb=0, internal_storage_mb=4096,
        sdcard_mb=0, hardware={}, create_only=True,
    ) is True
    assert "disk.dataPartition.size=4096M\n" in avd.config.read_text()
    assert [call.args[0][1:3] for call in avd.listing.call_args_list] == [["list", "avd"], ["create", "avd"]]


@pytest.mark.parametrize("exit_confirmed", [True, False])
def test_existing_start_capacity_failure_keeps_data_without_stop_or_delete(existing_avd, monkeypatch, exit_confirmed):
    avd = existing_avd
    avd.config.write_text(config_text("unknown"))
    before, witness = file_state(avd.config), file_state(avd.witness)
    stop, delete = Mock(side_effect=AssertionError("must not stop")), Mock(side_effect=AssertionError("must not delete"))
    monkeypatch.setattr(avd.manager, "stop_sync", stop)
    monkeypatch.setattr(avd.manager, "delete_sync", delete)
    if not exit_confirmed:
        # 前置Stopped核验后，容量拒绝时的只读退出证据可以变为未知。
        inventory = iter([([], True), ([], False)])
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: next(inventory, ([], False)))
    with pytest.raises(mod.VmStartFailure, match="数据容量") as error:
        avd.manager.start_sync({"vm_id": "a", "assigned_port": 5554, "require_existing_instance": True,
                                "internal_storage_mb": 4096})
    assert error.value.cleanup_confirmed is exit_confirmed
    assert error.value.created_in_attempt is False
    assert "a" not in avd.manager._runtimes
    assert "a" in avd.manager._created_vm_ids
    assert file_state(avd.config) == before
    assert file_state(avd.witness) == witness
    stop.assert_not_called()
    delete.assert_not_called()


@pytest.mark.parametrize("policy,flag", [
    ("save", None), ("discard_changes", "-no-snapshot-save"),
    ("cold_boot", "-no-snapshot-load"), ("no_snapshot", "-no-snapshot"),
])
def test_original_failure_restart_preserves_size_and_all_snapshot_flags(existing_avd, monkeypatch, policy, flag):
    avd = existing_avd
    before, witness = file_state(avd.config), file_state(avd.witness)
    popen = Mock(return_value=SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(avd.manager, "_wait_boot_completed", lambda *_: None)
    monkeypatch.setattr(avd.manager, "_provision_device", lambda *_: None)
    monkeypatch.setattr(avd.manager, "_emulator_avd_name", lambda *_: AVD)
    result = avd.manager.start_sync({
        "vm_id": "a", "assigned_port": 5554, "require_existing_instance": True,
        "screen_width": 540, "screen_height": 960, "density": 240,
        "internal_storage_mb": 4096, "snapshot_policy": policy,
    })
    try:
        assert result["details"]["created_in_attempt"] is False
        assert result["details"]["internal_storage_mb"] == 4096  # remains the request, not observed capacity
        assert result["details"]["snapshot_policy"] == policy
        args = popen.call_args.args[0]
        assert [arg for arg in args if arg.startswith("-no-snapshot")] == ([flag] if flag else [])
        assert "-wipe-data" not in args
        assert file_state(avd.config) == before
        assert file_state(avd.witness) == witness
        assert avd.listing.call_count == 1
        assert avd.listing.call_args.args[0][1:] == ["list", "avd"]
    finally:
        avd.manager._runtimes["a"].log_file.close()
