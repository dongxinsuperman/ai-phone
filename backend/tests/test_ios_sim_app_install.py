"""向 iOS 虚拟机分发应用。

两件事必须分清：iOS 真机装 ``.ipa``（USB / lockdown），虚拟机装 ``.app``
（宿主 simctl）。两种包互不通用，因此在应用分发里是两种包、各自只对自己那类设备
可见——设备筛选那条 ``pkg.platform == dev.platform`` 等式天然做到了这点。
"""
import plistlib
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from ai_phone.agent.app_install.ios_sim import install_sim_app
from ai_phone.server.app_install import storage


# --------------------------------------------------------------------------
# 造包
# --------------------------------------------------------------------------
def _make_app_zip(path: Path, *, app_name="Demo.app", prefix="", extra=()):
    with zipfile.ZipFile(path, "w") as z:
        base = f"{prefix}{app_name}"
        z.writestr(f"{base}/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.x.demo"}))
        z.writestr(f"{base}/Demo", b"\xcf\xfa\xed\xfe fake macho")
        for name, data in extra:
            z.writestr(name, data)
    return path


# --------------------------------------------------------------------------
# Server 侧：包识别
#
# .app 扩展名归鸿蒙（那是 HarmonyOS 的 App Pack 格式，已经在用），不能被抢。
# 虚拟机的 xxx.app 是目录、传不了，约定压成 zip，按**内容**识别。
# --------------------------------------------------------------------------
def test_known_extensions_keep_their_platform():
    assert storage.platform_from_filename("a.apk") == "android"
    assert storage.platform_from_filename("a.ipa") == "ios"
    assert storage.platform_from_filename("a.hap") == "harmony"
    assert storage.platform_from_filename("a.app") == "harmony", "不能把 .app 从鸿蒙抢走"


def test_zip_defers_to_content_inspection():
    assert storage.platform_from_filename("a.zip") == ""


def test_unknown_extension_rejected():
    with pytest.raises(HTTPException) as exc:
        storage.platform_from_filename("a.exe")
    assert exc.value.status_code == 400


def test_app_zip_detected_as_ios_sim(tmp_path):
    z = _make_app_zip(tmp_path / "demo.zip")
    assert storage.is_simulator_app_zip(z) is True
    assert storage.platform_from_file("demo.zip", z) == "ios_sim"


def test_app_zip_detected_when_nested_in_folder(tmp_path):
    """压 .app 的父目录也认——用户怎么压的都有。"""
    z = _make_app_zip(tmp_path / "demo.zip", prefix="Build/Products/")
    assert storage.is_simulator_app_zip(z) is True


def test_finder_zip_with_macosx_still_detected(tmp_path):
    z = _make_app_zip(
        tmp_path / "demo.zip", extra=[("__MACOSX/._Demo.app", b"junk")]
    )
    assert storage.is_simulator_app_zip(z) is True


def test_zip_without_app_bundle_is_rejected(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "hi")
    assert storage.is_simulator_app_zip(z) is False
    with pytest.raises(HTTPException) as exc:
        storage.platform_from_file("x.zip", z)
    assert "Info.plist" in str(exc.value.detail)


def test_zip_with_plain_info_plist_is_not_an_app(tmp_path):
    """光有 Info.plist 不算——必须在 xxx.app 目录里。"""
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Payload/Info.plist", b"<plist/>")
    assert storage.is_simulator_app_zip(z) is False


def test_corrupt_zip_is_not_crashing(tmp_path):
    z = tmp_path / "bad.zip"
    z.write_bytes(b"not a zip at all")
    assert storage.is_simulator_app_zip(z) is False


# --------------------------------------------------------------------------
# Agent 侧：解包与安装
# --------------------------------------------------------------------------
def test_install_calls_simctl_with_bundle_path(tmp_path, monkeypatch):
    z = _make_app_zip(tmp_path / "demo.zip")
    calls = []
    monkeypatch.setattr(
        "ai_phone.agent.app_install.ios_sim.simctl_run",
        lambda *a, **k: calls.append((a, k)),
    )
    ok, reason, message = install_sim_app("SIM-1", z, 120)

    assert ok is True, message
    assert reason == ""
    args = calls[0][0]
    assert args[0] == "install" and args[1] == "SIM-1"
    assert args[2].endswith("Demo.app")


def test_install_finds_bundle_inside_nested_folder(tmp_path, monkeypatch):
    z = _make_app_zip(tmp_path / "demo.zip", prefix="Build/Products/Debug-iphonesimulator/")
    calls = []
    monkeypatch.setattr(
        "ai_phone.agent.app_install.ios_sim.simctl_run",
        lambda *a, **k: calls.append(a),
    )
    assert install_sim_app("SIM-1", z, 120)[0] is True
    assert calls[0][2].endswith("Demo.app")


def test_install_picks_outermost_bundle(tmp_path, monkeypatch):
    """内嵌的 App Extension / Watch App 也是 .app，不能被当成安装目标。"""
    z = tmp_path / "demo.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Demo.app/Info.plist", plistlib.dumps({"a": 1}))
        f.writestr("Demo.app/PlugIns/Widget.appex/Info.plist", plistlib.dumps({"a": 1}))
        f.writestr("Demo.app/Watch/W.app/Info.plist", plistlib.dumps({"a": 1}))
    calls = []
    monkeypatch.setattr(
        "ai_phone.agent.app_install.ios_sim.simctl_run",
        lambda *a, **k: calls.append(a),
    )
    install_sim_app("SIM-1", z, 120)
    assert calls[0][2].endswith("Demo.app")
    assert "Watch" not in calls[0][2]


def test_install_rejects_non_zip(tmp_path):
    p = tmp_path / "a.ipa"
    p.write_bytes(b"definitely not a zip")
    ok, reason, message = install_sim_app("SIM-1", p, 120)
    assert ok is False and reason == "bad_package"
    assert "zip" in message


def test_install_rejects_zip_without_bundle(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "hi")
    ok, reason, message = install_sim_app("SIM-1", z, 120)
    assert ok is False and reason == "bad_package"
    assert ".app" in message


def test_install_surfaces_simctl_error(tmp_path, monkeypatch):
    """simctl 的 stderr 写清了真实原因（架构不符等），要原样带回去。"""
    from ai_phone.agent.drivers.simctl import SimctlError

    z = _make_app_zip(tmp_path / "demo.zip")

    def boom(*_a, **_k):
        raise SimctlError(
            ["simctl", "install"], 1, "", "Unsupported architecture arm64e"
        )

    monkeypatch.setattr("ai_phone.agent.app_install.ios_sim.simctl_run", boom)
    ok, reason, message = install_sim_app("SIM-1", z, 120)
    assert ok is False and reason == "install_failed"
    assert "Unsupported architecture" in message


def test_zip_slip_is_blocked(tmp_path, monkeypatch):
    """包内路径越界必须拒绝——否则上传者能往 Agent 机器任意位置写文件。"""
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Demo.app/Info.plist", plistlib.dumps({"a": 1}))
        f.writestr("../../../../tmp/pwned.txt", b"owned")
    called = []
    monkeypatch.setattr(
        "ai_phone.agent.app_install.ios_sim.simctl_run",
        lambda *a, **k: called.append(a),
    )
    ok, reason, message = install_sim_app("SIM-1", z, 120)
    assert ok is False and reason == "bad_package"
    assert "越界" in message
    assert called == [], "越界包不该走到安装这一步"


def test_workdir_cleaned_after_install(tmp_path, monkeypatch):
    z = _make_app_zip(tmp_path / "demo.zip")
    monkeypatch.setattr(
        "ai_phone.agent.app_install.ios_sim.simctl_run", lambda *a, **k: None
    )
    install_sim_app("SIM-1", z, 120)
    assert not (tmp_path / "unpacked").exists(), "解压目录没清干净"


# --------------------------------------------------------------------------
# 接线
# --------------------------------------------------------------------------
def test_handler_routes_ios_sim_to_simctl_installer():
    import inspect

    from ai_phone.agent.app_install import handler

    src = inspect.getsource(handler._install_by_platform)
    assert '"ios_sim": install_sim_app' in src


def test_real_ios_still_uses_ipa_installer():
    """真机那条路一行不能动。"""
    import inspect

    from ai_phone.agent.app_install import handler

    src = inspect.getsource(handler._install_by_platform)
    assert '"ios": install_ipa' in src
