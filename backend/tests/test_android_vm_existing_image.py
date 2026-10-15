"""Existing AVD image identity regressions; all SDK/AVD data is temporary and native calls are fake."""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_phone.agent.android_vm import manager as mod


IMAGE = "system-images;android-33;google_apis;arm64-v8a"
AVD = "aiphone_vm_a"


@pytest.fixture
def avd(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native operation is forbidden in image identity tests")

    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    monkeypatch.setattr(mod.subprocess, "Popen", forbidden)
    monkeypatch.setattr(mod.psutil, "process_iter", lambda *_a, **_k: iter(()))
    monkeypatch.setattr(mod, "_is_port_free", lambda _: True)
    avd_home, sdk = tmp_path / "avds", tmp_path / "sdk"
    monkeypatch.setenv("ANDROID_AVD_HOME", str(avd_home))
    directory = avd_home / f"{AVD}.avd"
    directory.mkdir(parents=True)
    config = directory / "config.ini"
    config.write_text("image.sysdir.1=system-images/android-33/google_apis/arm64-v8a/\nhw.lcd.width=100\ndisk.dataPartition.size=6442450944\n")
    (directory / "userdata-qemu.img").write_bytes(b"existing user data must survive")
    index = avd_home / f"{AVD}.ini"
    index.write_text(f"avd.ini.encoding=UTF-8\npath={directory}\npath.rel=avd/{AVD}.avd\n")
    image_dir = sdk / "system-images" / "android-33" / "google_apis" / "arm64-v8a"
    image_dir.mkdir(parents=True)
    package = image_dir / "package.xml"
    package.write_text(f'<repository xmlns="urn:sdk"><localPackage path="{IMAGE}"><revision><major>17</major></revision></localPackage></repository>')
    tools = mod.AndroidVmTools("/isolated/adb", "/isolated/emulator", "/isolated/avdmanager", sdk_root=str(sdk))
    manager = mod.AndroidVmManager(runtime_dir=tmp_path / "runtime")
    monkeypatch.setattr(mod, "find_android_tools", lambda: (tools, []))
    monkeypatch.setattr(manager, "probe", lambda _: {"ok": True})
    monkeypatch.setattr(manager, "_choose_port", lambda **_: 5554)
    listing = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"Name: {AVD}\nPath: {directory}\n", stderr=""))
    monkeypatch.setattr(mod.subprocess, "run", listing)
    return SimpleNamespace(manager=manager, tools=tools, root=tmp_path, config=config, directory=directory,
                           index=index, package=package, image_dir=image_dir, sdk=sdk, listing=listing)


def ensure(avd, image=IMAGE, **extra):
    return avd.manager._ensure_avd(
        avd.tools, avd_name=AVD, system_image=image, width=1080, height=1920, density=420,
        orientation="portrait", ram_mb=4096, vm_heap_mb=0, internal_storage_mb=0,
        sdcard_mb=0, hardware={}, require_existing=True, **extra,
    )


def snapshot(avd):
    return {str(path.relative_to(avd.root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in avd.root.rglob("*") if path.is_file()}


def only_list_was_called(avd):
    assert avd.listing.call_count == 1
    assert avd.listing.call_args.args[0] == [avd.tools.avdmanager, "list", "avd"]


@pytest.mark.parametrize("form", ["relative", "absolute", "backslashes", "dotdot", "absolute_without_sdk"])
def test_existing_same_package_accepts_real_sdk_paths(avd, form):
    paths = {
        "relative": "system-images/android-33/google_apis/arm64-v8a/",
        "absolute": str(avd.image_dir),
        "backslashes": "system-images\\android-33\\google_apis\\arm64-v8a\\",
        "dotdot": "system-images/android-33/google_apis/../google_apis/arm64-v8a",
        "absolute_without_sdk": str(avd.image_dir),
    }
    avd.config.write_text(f"image.sysdir.1={paths[form]}\nhw.lcd.width=100\ndisk.dataPartition.size=6442450944\n")
    if form == "absolute_without_sdk":
        avd.tools.sdk_root = ""
    assert ensure(avd) is False
    assert "hw.lcd.width=1080" in avd.config.read_text()
    assert "image.sysdir.1=" + paths[form] in avd.config.read_text()
    only_list_was_called(avd)


@pytest.mark.parametrize("package", [
    "system-images;android-33;google_apis;arm64-v8a",
    "system-images;android-Baklava;google_apis;arm64-v8a",
    "system-images;android-35-ext15;google_apis_playstore;arm64-v8a",
])
def test_package_metadata_controls_identity_without_build_revision_equality(avd, package):
    avd.package.write_text(f'<repository><localPackage path="{package}"><revision><major>999</major></revision></localPackage></repository>')
    assert ensure(avd, package) is False


@pytest.mark.parametrize("requested", [
    "system-images;android-35;google_apis;arm64-v8a",
    "system-images;android-33;default;arm64-v8a",
    "system-images;android-33;google_apis;x86_64",
    "system-images;android-33;google_apis;arm64",
    IMAGE + "-suffix",
    "", "system-images/android-33/google_apis/arm64-v8a",
])
def test_mismatch_fails_before_screen_or_disk_changes(avd, requested, monkeypatch):
    before = snapshot(avd)
    writer = Mock(side_effect=AssertionError("must not write mismatched AVD"))
    monkeypatch.setattr(avd.manager, "_write_avd_screen_config", writer)
    with pytest.raises(mod._ExistingAvdImageError, match="实际镜像.*与请求.*不一致"):
        ensure(avd, requested)
    assert snapshot(avd) == before
    writer.assert_not_called()
    only_list_was_called(avd)


@pytest.mark.parametrize("broken", [
    "missing_config", "no_sysdir", "empty_sysdir", "duplicate_sysdir", "second_sysdir",
    "missing_package", "bad_xml", "duplicate_package", "missing_package_id", "invalid_package_id",
    "relative_without_sdk", "foreign_windows_path", "drive_relative_path",
])
def test_unknown_image_never_guesses_or_modifies_existing_disk(avd, broken):
    if broken == "missing_config":
        avd.config.unlink()
    elif broken in {"no_sysdir", "empty_sysdir", "duplicate_sysdir", "second_sysdir"}:
        avd.config.write_text({
            "no_sysdir": "hw.lcd.width=100\n",
            "empty_sysdir": "image.sysdir.1=\n",
            "duplicate_sysdir": avd.config.read_text() + "image.sysdir.1=another\n",
            "second_sysdir": avd.config.read_text() + "image.sysdir.2=another\n",
        }[broken])
    elif broken == "missing_package":
        avd.package.unlink()
    elif broken in {"bad_xml", "duplicate_package", "missing_package_id", "invalid_package_id"}:
        avd.package.write_text({
            "bad_xml": "<incomplete",
            "duplicate_package": f'<repository><localPackage path="{IMAGE}"/><localPackage path="{IMAGE}"/></repository>',
            "missing_package_id": "<repository><localPackage/></repository>",
            "invalid_package_id": '<repository><localPackage path="system-images;..;google_apis;arm64-v8a"/></repository>',
        }[broken])
    elif broken == "relative_without_sdk":
        avd.tools.sdk_root = ""
    elif broken == "foreign_windows_path":
        avd.config.write_text(r"image.sysdir.1=C:\isolated-missing-sdk\system-images\android-33\google_apis\arm64-v8a")
    else:
        avd.config.write_text(r"image.sysdir.1=C:system-images\android-33\google_apis\arm64-v8a")
    before = snapshot(avd)
    with pytest.raises(mod._ExistingAvdImageError, match="未修改或启动"):
        ensure(avd)
    assert snapshot(avd) == before
    only_list_was_called(avd)


@pytest.mark.parametrize("alias", ["symlink", "dotdot"])
def test_path_spelling_cannot_hide_a_different_actual_package(avd, alias):
    other = avd.sdk / "other" / "arm64-v8a"
    other.mkdir(parents=True)
    (other / "package.xml").write_text('<repository><localPackage path="system-images;android-35;google_apis;arm64-v8a"/></repository>')
    if alias == "symlink":
        avd.package.unlink()
        avd.image_dir.rmdir()
        avd.image_dir.symlink_to(other, target_is_directory=True)
    else:
        avd.config.write_text("image.sysdir.1=system-images/android-33/google_apis/arm64-v8a/../../../../other/arm64-v8a\n")
    before = snapshot(avd)
    with pytest.raises(mod._ExistingAvdImageError, match="实际镜像.*android-35"):
        ensure(avd)
    assert snapshot(avd) == before


@pytest.mark.parametrize("listing", ["missing_path", "duplicate_path", "duplicate_name", "different_path"])
def test_list_must_identify_the_configuration_that_would_be_written(avd, listing):
    other = avd.root / "custom-avd"
    other.mkdir()
    (other / "config.ini").write_text(avd.config.read_text())
    text = {
        "missing_path": f"Name: {AVD}\n",
        "duplicate_path": f"Name: {AVD}\nPath: {avd.directory}\nPath: {avd.directory}\n",
        "duplicate_name": f"Name: {AVD}\nPath: {avd.directory}\nName: {AVD}\nPath: {avd.directory}\n",
        "different_path": f"Name: {AVD}\nPath: {other}\n",
    }[listing]
    avd.listing.return_value.stdout = text
    before = snapshot(avd)
    with pytest.raises(mod._ExistingAvdImageError):
        ensure(avd)
    assert snapshot(avd) == before
    only_list_was_called(avd)


@pytest.mark.parametrize("raw,sdk,expected", [
    (r"system-images\android-33\google_apis\arm64-v8a", r"C:\Android\Sdk", PureWindowsPath("C:/Android/Sdk/system-images/android-33/google_apis/arm64-v8a")),
    ("C:/Android/Sdk/system-images/android-33/google_apis/arm64-v8a", "", PureWindowsPath("C:/Android/Sdk/system-images/android-33/google_apis/arm64-v8a")),
    (r"\\sdk-host\share\system-images\android-33\google_apis\arm64-v8a", "", PureWindowsPath("//sdk-host/share/system-images/android-33/google_apis/arm64-v8a")),
    (r"system-images\android-33\google_apis\arm64-v8a", "/isolated/sdk", PurePosixPath("/isolated/sdk/system-images/android-33/google_apis/arm64-v8a")),
    ("/isolated/sdk/system-images/android-33/google_apis/arm64-v8a", "", PurePosixPath("/isolated/sdk/system-images/android-33/google_apis/arm64-v8a")),
])
def test_cross_platform_path_rules_are_exact_without_native_commands(raw, sdk, expected):
    assert mod._sdk_image_path(raw, sdk) == expected


@pytest.mark.parametrize("raw,sdk", [
    (r"C:system-images\android-33", r"C:\SDK"),
    (r"\system-images\android-33", r"C:\SDK"),
    ("system-images/android-33", ""),
    ("system-images/android-33", "relative-sdk"),
])
def test_ambiguous_path_has_no_cwd_or_drive_fallback(raw, sdk):
    with pytest.raises(ValueError):
        mod._sdk_image_path(raw, sdk)


@pytest.mark.parametrize("exit_confirmed", [True, False])
def test_start_mismatch_never_cleans_old_instance_and_reports_real_exit_fact(avd, monkeypatch, exit_confirmed):
    before = snapshot(avd)
    stop, delete = Mock(side_effect=AssertionError("must not stop")), Mock(side_effect=AssertionError("must not delete"))
    monkeypatch.setattr(avd.manager, "stop_sync", stop)
    monkeypatch.setattr(avd.manager, "delete_sync", delete)
    if not exit_confirmed:
        monkeypatch.setattr(mod, "_find_avd_processes", lambda _: ([], False))
    with pytest.raises(mod.VmStartFailure, match="实际镜像") as error:
        avd.manager.start_sync({"vm_id": "a", "api_level": 35, "abi": "arm64-v8a", "assigned_port": 5554,
                                "require_existing_instance": True, "rollback_new_instance": True})
    assert error.value.cleanup_confirmed is exit_confirmed
    assert error.value.created_in_attempt is False
    assert error.value.instance_preexisting is False
    assert "a" in avd.manager._created_vm_ids
    assert "a" not in avd.manager._runtimes
    assert snapshot(avd) == before
    stop.assert_not_called()
    delete.assert_not_called()


def configure_successful_start(avd, monkeypatch):
    popen = Mock(return_value=SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    monkeypatch.setattr(avd.manager, "_wait_boot_completed", lambda *_: None)
    monkeypatch.setattr(avd.manager, "_provision_device", lambda *_: None)
    monkeypatch.setattr(avd.manager, "_emulator_avd_name", lambda *_: AVD)
    return popen


def test_restoring_effective_configuration_can_start_original_disk(avd, monkeypatch):
    popen = configure_successful_start(avd, monkeypatch)
    message = {"vm_id": "a", "api_level": 35, "abi": "arm64", "assigned_port": 5554,
               "require_existing_instance": True}
    with pytest.raises(mod.VmStartFailure) as error:
        avd.manager.start_sync(message)
    assert error.value.cleanup_confirmed is True
    popen.assert_not_called()
    result = avd.manager.start_sync({**message, "system_image": IMAGE})
    assert result["details"]["system_image"] == IMAGE  # explicit package wins over top-level API 35
    assert result["details"]["created_in_attempt"] is False
    assert (avd.directory / "userdata-qemu.img").read_bytes() == b"existing user data must survive"
    assert popen.call_count == 1
    assert all(call.args[0][1:] == ["list", "avd"] for call in avd.listing.call_args_list)
    avd.manager._runtimes["a"].log_file.close()


@pytest.mark.parametrize("broken", ["image", "missing_index", "duplicate_index_path", "other_index_path"])
def test_reclaimed_live_fast_path_cannot_skip_image_or_path_check(avd, monkeypatch, broken):
    configure_successful_start(avd, monkeypatch)
    rt = mod.VmRuntime("a", "a", "emulator-5554", 5554, None, 0, avd_name=AVD,
                       ready=True, observed_request_id="previous", operation_deadline=42)
    avd.manager._runtimes["a"] = rt
    if broken == "missing_index":
        avd.index.unlink()
    elif broken == "duplicate_index_path":
        avd.index.write_text(f"path={avd.directory}\npath={avd.directory}\n")
    elif broken == "other_index_path":
        other = avd.root / "custom-avd"
        other.mkdir()
        (other / "config.ini").write_text(avd.config.read_text())
        avd.index.write_text(f"path={other}\n")
    before = snapshot(avd)
    with pytest.raises(mod.VmStartFailure):
        avd.manager.start_sync({"vm_id": "a", "system_image": IMAGE if broken != "image" else IMAGE.replace("33", "35"),
                                "request_id": "new"})
    assert avd.manager._runtimes["a"] is rt
    assert rt.ready and not rt.transport_unavailable
    assert rt.observed_request_id == "previous" and rt.operation_deadline == 42
    assert snapshot(avd) == before
    avd.listing.assert_not_called()


def test_reclaimed_same_image_reuses_without_writing_or_sdk_query(avd, monkeypatch):
    configure_successful_start(avd, monkeypatch)
    rt = mod.VmRuntime("a", "a", "emulator-5554", 5554, None, 0, avd_name=AVD, ready=True)
    avd.manager._runtimes["a"] = rt
    before = snapshot(avd)
    result = avd.manager.start_sync({"vm_id": "a", "api_level": 33, "abi": "arm64-v8a", "request_id": "new"})
    assert result["details"]["reused"] is True
    assert rt.observed_request_id == "new"
    assert snapshot(avd) == before
    avd.listing.assert_not_called()


def test_new_instance_still_uses_avdmanager_package_and_first_bind_conflict_never_adopts(avd, monkeypatch):
    avd.listing.return_value.stdout = f"Name: {AVD}\n"  # collision needs no valid config or metadata
    with pytest.raises(RuntimeError, match="首次绑定发现已有同名") as error:
        ensure(avd, create_only=True)
    assert error.value.instance_preexisting is True
    assert error.value.created_in_attempt is False
    avd.listing.reset_mock()
    avd.listing.return_value.stdout = ""
    monkeypatch.setattr(avd.manager, "_write_avd_screen_config", lambda *_a, **_kw: None)
    created = avd.manager._ensure_avd(
        avd.tools, avd_name="aiphone_vm_new", system_image=IMAGE, width=1080, height=1920,
        density=420, orientation="portrait", ram_mb=0, vm_heap_mb=0,
        internal_storage_mb=0, sdcard_mb=0, hardware={}, create_only=True,
    )
    assert created is True
    assert [call.args[0] for call in avd.listing.call_args_list] == [
        [avd.tools.avdmanager, "list", "avd"],
        [avd.tools.avdmanager, "create", "avd", "-n", "aiphone_vm_new", "-k", IMAGE],
    ]
