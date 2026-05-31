from __future__ import annotations

from types import SimpleNamespace

from ai_phone.agent.drivers import ios as ios_module
from ai_phone.agent.drivers.ios import IosDriver


def _driver() -> IosDriver:
    return IosDriver("IOS-1", object(), object())


class _FakeLockdown:
    def get_value(self, domain=None, key=None):  # noqa: ANN001
        if domain == "com.apple.mobile.iTunes" and key == "ScreenWidth":
            return 1179
        if domain == "com.apple.mobile.iTunes" and key == "ScreenHeight":
            return 2556
        if key == "ProductVersion":
            return "16.7"
        if key == "ProductType":
            return "iPhone15,2"
        return ""


def test_ios_list_devices_ignores_network_pairing_devices(monkeypatch):
    ios_module._reset_ios_scan_state_for_tests()
    ios_module._IOS_META_CACHE.clear()
    opened: list[tuple[str, bool]] = []

    class FakeUsbmux:
        def list_devices(self):
            return [
                SimpleNamespace(serial="USB-1", connection_type="USB"),
                SimpleNamespace(serial="WIFI-1", connection_type="Network"),
            ]

    def fake_create_using_usbmux(*, serial: str, autopair: bool = True):
        opened.append((serial, autopair))
        return _FakeLockdown()

    monkeypatch.setattr(
        ios_module,
        "_import_pmd3",
        lambda: (FakeUsbmux(), fake_create_using_usbmux, None, None),
    )

    devices = ios_module.list_ios_devices()

    assert [d.serial for d in devices] == ["USB-1"]
    assert devices[0].status == "online"
    assert opened == [("USB-1", False)]
    assert ios_module.was_last_ios_scan_ok() is True


def test_ios_list_all_packages_uses_user_and_system_not_any(monkeypatch):
    driver = _driver()
    calls: list[str] = []

    def fake_list_apps(*, application_type: str) -> list[str]:
        calls.append(application_type)
        if application_type == "User":
            return ["com.yangcong345.student"]
        if application_type == "System":
            return ["com.apple.Preferences"]
        raise AssertionError(f"unexpected application_type={application_type}")

    monkeypatch.setattr(driver, "_list_apps", fake_list_apps)

    packages = driver.list_all_packages()

    assert calls == ["User", "System"]
    assert "com.yangcong345.student" in packages
    assert "com.apple.Preferences" in packages


def test_ios_list_all_packages_keeps_user_apps_when_system_breaks(monkeypatch):
    driver = _driver()

    def fake_list_apps(*, application_type: str) -> list[str]:
        if application_type == "User":
            return ["com.yangcong345.student"]
        if application_type == "System":
            raise BrokenPipeError(32, "Broken pipe")
        raise AssertionError(f"unexpected application_type={application_type}")

    monkeypatch.setattr(driver, "_list_apps", fake_list_apps)

    packages = driver.list_all_packages()

    assert "com.yangcong345.student" in packages
    assert "com.apple.Preferences" in packages


def test_ios_list_all_packages_raises_when_both_segments_fail(monkeypatch):
    driver = _driver()

    def fake_list_apps(*, application_type: str) -> list[str]:
        raise BrokenPipeError(32, f"{application_type} pipe")

    monkeypatch.setattr(driver, "_list_apps", fake_list_apps)

    try:
        driver.list_all_packages()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "type=User/System" in message
    assert "User: BrokenPipeError" in message
    assert "System: BrokenPipeError" in message
