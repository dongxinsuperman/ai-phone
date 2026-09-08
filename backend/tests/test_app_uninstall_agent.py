from __future__ import annotations

import pytest

from ai_phone.agent.app_uninstall import platforms
from ai_phone.agent.app_uninstall.handler import _run_app_uninstall
from ai_phone.shared import protocol as P


def test_uninstall_android_checks_exact_package_and_confirms_absent(monkeypatch):
    class FakeDevice:
        installed = True

        def shell(self, args, timeout):
            if args == ["pm", "list", "packages", "-3"]:
                return "package:com.example.demo\n" if self.installed else ""
            if args == ["pm", "uninstall", "com.example.demo"]:
                self.installed = False
                return "Success"
            raise AssertionError(args)

    device = FakeDevice()
    monkeypatch.setattr(platforms.adb, "device", lambda serial: device)

    result = platforms.uninstall_android("ANDROID-1", "com.example.demo", 60)

    assert result == (True, "", "卸载成功")


def test_uninstall_android_missing_exact_package_returns_not_found(monkeypatch):
    class FakeDevice:
        def shell(self, args, timeout):
            return "package:com.example.demo.pro\n"

    monkeypatch.setattr(platforms.adb, "device", lambda serial: FakeDevice())

    success, reason, _message = platforms.uninstall_android(
        "ANDROID-1", "com.example.demo", 60
    )

    assert success is False
    assert reason == platforms.APP_NOT_FOUND


def test_uninstall_harmony_uses_hdc_and_confirms_absent(monkeypatch):
    installed = {"com.example.demo"}
    calls = []

    monkeypatch.setattr(
        platforms, "_harmony_user_apps", lambda serial: set(installed)
    )

    def fake_hdc_run(*args, **kwargs):
        calls.append((args, kwargs))
        installed.clear()
        return "Uninstall bundle successfully."

    monkeypatch.setattr(platforms, "hdc_run", fake_hdc_run)

    success, reason, _message = platforms.uninstall_harmony(
        "HARMONY-1", "com.example.demo", 60
    )

    assert success is True
    assert reason == ""
    assert calls == [
        (
            ("uninstall", "com.example.demo"),
            {"serial": "HARMONY-1", "timeout": 60, "check": True},
        )
    ]


def test_uninstall_ios_uses_installation_proxy_and_confirms_absent(monkeypatch):
    class FakeInstallationProxy:
        def __init__(self, lockdown):
            self.installed = True
            self.closed = False

        def connect(self):
            return None

        def get_apps(self, application_type, bundle_identifiers):
            assert application_type == "User"
            assert bundle_identifiers == ["com.example.demo"]
            return {"com.example.demo": {}} if self.installed else {}

        def uninstall(self, package_name):
            assert package_name == "com.example.demo"
            self.installed = False

        def close(self):
            self.closed = True

    success, reason, _message = platforms._uninstall_ios_with_lockdown(
        FakeInstallationProxy,
        object(),
        "com.example.demo",
        60,
    )

    assert success is True
    assert reason == ""


def test_uninstall_ios_sim_uses_simctl_and_confirms_absent(monkeypatch):
    installed = {"com.example.demo"}
    calls = []
    monkeypatch.setattr(
        platforms, "_ios_sim_user_apps", lambda serial: set(installed)
    )

    def fake_simctl_run(*args, **kwargs):
        calls.append((args, kwargs))
        installed.clear()
        return ""

    monkeypatch.setattr(platforms, "simctl_run", fake_simctl_run)

    success, reason, _message = platforms.uninstall_ios_sim(
        "SIM-1", "com.example.demo", 60
    )

    assert success is True
    assert reason == ""
    assert calls == [
        (
            ("uninstall", "SIM-1", "com.example.demo"),
            {"timeout": 60.0},
        )
    ]


@pytest.mark.asyncio
async def test_agent_handler_sends_correlated_result(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)
            return True

    monkeypatch.setattr(
        "ai_phone.agent.app_uninstall.handler.uninstall_by_platform",
        lambda platform, serial, package_name, timeout_sec: (
            False,
            platforms.APP_NOT_FOUND,
            "设备中无法找到对应的 App",
        ),
    )
    client = FakeClient()

    await _run_app_uninstall(
        client,
        {
            "type": P.MSG_APP_UNINSTALL_START,
            "request_id": "req-1",
            "serial": "A1",
            "platform": "android",
            "package_name": "com.example.demo",
            "timeout_sec": 60,
        },
    )

    assert client.sent == [
        {
            "type": P.MSG_APP_UNINSTALL_RESULT,
            "request_id": "req-1",
            "serial": "A1",
            "platform": "android",
            "package_name": "com.example.demo",
            "success": False,
            "reason": "app_not_found",
            "message": "设备中无法找到对应的 App",
        }
    ]


@pytest.mark.parametrize("platform", ["android", "harmony", "ios", "ios_sim"])
def test_platform_dispatch_has_all_install_channels(monkeypatch, platform):
    called = []
    uninstallers = {
        "android": "uninstall_android",
        "harmony": "uninstall_harmony",
        "ios": "uninstall_ios",
        "ios_sim": "uninstall_ios_sim",
    }
    for name in uninstallers.values():
        monkeypatch.setattr(
            platforms,
            name,
            lambda serial, package_name, timeout_sec, _name=name: (
                called.append(_name) or (True, "", "ok")
            ),
        )

    result = platforms.uninstall_by_platform(
        platform, "serial-1", "com.example.demo", 60
    )

    assert result == (True, "", "ok")
    assert called == [uninstallers[platform]]
