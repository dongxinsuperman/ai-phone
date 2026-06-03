from __future__ import annotations

import ai_phone.agent.drivers.ios as ios_mod
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


def test_ios_list_apps_uses_fresh_lockdown_not_driver_lockdown(monkeypatch):
    stale_lockdown = object()
    fresh_lockdown = object()
    driver = IosDriver("IOS-1", stale_lockdown, object())
    used_lockdowns: list[object] = []
    closed_lockdowns: list[object] = []

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    monkeypatch.setattr(
        driver,
        "_open_fresh_lockdown_for_app_listing",
        lambda: fresh_lockdown,
    )
    monkeypatch.setattr(
        driver,
        "_close_lockdown",
        lambda lockdown: closed_lockdowns.append(lockdown),
    )

    def fake_fetch(lockdown, application_type: str) -> list[str]:
        used_lockdowns.append(lockdown)
        assert application_type == "User"
        return ["com.yangcong345.match"]

    monkeypatch.setattr(driver, "_fetch_apps_via_lockdown", fake_fetch)

    packages = driver._list_apps(application_type="User")

    assert packages == ["com.yangcong345.match"]
    assert used_lockdowns == [fresh_lockdown]
    assert stale_lockdown not in used_lockdowns
    assert closed_lockdowns == [fresh_lockdown]


def test_ios_list_apps_closes_fresh_lockdown_when_fetch_fails(monkeypatch):
    fresh_lockdown = object()
    driver = _driver()
    closed_lockdowns: list[object] = []

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    monkeypatch.setattr(
        driver,
        "_open_fresh_lockdown_for_app_listing",
        lambda: fresh_lockdown,
    )
    monkeypatch.setattr(
        driver,
        "_close_lockdown",
        lambda lockdown: closed_lockdowns.append(lockdown),
    )

    def fake_fetch(lockdown, application_type: str) -> list[str]:
        assert lockdown is fresh_lockdown
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(driver, "_fetch_apps_via_lockdown", fake_fetch)

    try:
        driver._list_apps(application_type="User")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "type=User" in message
    assert "BrokenPipeError" in message
    assert closed_lockdowns == [fresh_lockdown]


def test_ios_open_fresh_lockdown_for_app_listing_disables_autopair(monkeypatch):
    driver = _driver()
    calls: list[dict[str, object]] = []

    def fake_create_using_usbmux(**kwargs):
        calls.append(kwargs)
        return "fresh-lockdown"

    monkeypatch.setattr(
        ios_mod,
        "_import_pmd3",
        lambda: (None, fake_create_using_usbmux, None, None),
    )
    monkeypatch.setattr(ios_mod, "_maybe_sync", lambda value, timeout=30.0: value)

    lockdown = driver._open_fresh_lockdown_for_app_listing()

    assert lockdown == "fresh-lockdown"
    assert calls == [{"serial": "IOS-1", "autopair": False}]
