from __future__ import annotations

from ai_phone.agent.drivers.ios import IosDriver


def _driver() -> IosDriver:
    return IosDriver("IOS-1", object(), object())


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
