from __future__ import annotations

import ai_phone.agent.drivers.ios as ios_mod
from ai_phone.agent.drivers.ios import IosDriver


def _driver() -> IosDriver:
    return IosDriver("IOS-1", object(), object())


def test_ios_list_all_packages_uses_user_and_system_not_any(monkeypatch):
    driver = _driver()
    calls: list[str] = []

    def fake_list_app_records(*, application_type: str) -> dict[str, dict]:
        calls.append(application_type)
        if application_type == "User":
            return {"com.example.student": {}}
        if application_type == "System":
            return {"com.apple.Preferences": {}}
        raise AssertionError(f"unexpected application_type={application_type}")

    monkeypatch.setattr(driver, "_list_app_records", fake_list_app_records)

    packages = driver.list_all_packages()

    assert calls == ["User", "System"]
    assert "com.example.student" in packages
    assert "com.apple.Preferences" in packages


def test_ios_list_all_packages_keeps_user_apps_when_system_breaks(monkeypatch):
    driver = _driver()

    def fake_list_app_records(*, application_type: str) -> dict[str, dict]:
        if application_type == "User":
            return {"com.example.student": {}}
        if application_type == "System":
            raise BrokenPipeError(32, "Broken pipe")
        raise AssertionError(f"unexpected application_type={application_type}")

    monkeypatch.setattr(driver, "_list_app_records", fake_list_app_records)

    packages = driver.list_all_packages()

    assert "com.example.student" in packages
    assert packages == ["com.example.student"]


def test_ios_list_all_packages_raises_when_both_segments_fail(monkeypatch):
    driver = _driver()

    def fake_list_app_records(*, application_type: str) -> dict[str, dict]:
        raise BrokenPipeError(32, f"{application_type} pipe")

    monkeypatch.setattr(driver, "_list_app_records", fake_list_app_records)

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

    def fake_fetch(lockdown, application_type: str) -> dict[str, dict]:
        used_lockdowns.append(lockdown)
        assert application_type == "User"
        return {"com.example.match": {"CFBundleDisplayName": "debug-build"}}

    monkeypatch.setattr(driver, "_fetch_app_records_via_lockdown", fake_fetch)

    packages = driver._list_apps(application_type="User")

    assert packages == ["com.example.match"]
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

    def fake_fetch(lockdown, application_type: str) -> dict[str, dict]:
        assert lockdown is fresh_lockdown
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(driver, "_fetch_app_records_via_lockdown", fake_fetch)

    try:
        driver._list_apps(application_type="User")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "type=User" in message
    assert "BrokenPipeError" in message
    assert closed_lockdowns == [fresh_lockdown]


def test_ios_list_installed_apps_uses_display_name_then_short_name(monkeypatch):
    driver = _driver()
    monkeypatch.setattr(
        driver,
        "_list_all_app_records",
        lambda: {
            "com.example.debug": {
                "CFBundleDisplayName": "debug-build",
                "CFBundleName": "debug",
            },
            "com.example.shortname": {"CFBundleName": "short-build"},
            "com.apple.Preferences": {},
        },
    )

    apps = driver.list_installed_apps()

    assert [(app.display_name, app.package_name) for app in apps] == [
        ("debug-build", "com.example.debug"),
        ("short-build", "com.example.shortname"),
        ("com.apple.Preferences", "com.apple.Preferences"),
    ]


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


class _FakeWda:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.raise_on_terminate: Exception | None = None

    def terminate_app(self, bundle_id: str) -> None:
        if self.raise_on_terminate is not None:
            raise self.raise_on_terminate
        self.terminated.append(bundle_id)


def test_ios_terminate_app_falls_back_to_wda_when_no_tunneld(monkeypatch):
    """iOS 15/16 无 DVT 通道（rsd 为 None）时，应回落 WDA terminate 而非直接报错。"""
    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    # terminate 后目标已不在前台 → 复核通过
    monkeypatch.setattr(driver, "current_app", lambda: "com.apple.springboard")
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    driver.terminate_app("com.example.testapp")

    assert fake_wda.terminated == ["com.example.testapp"]


def test_ios_terminate_app_wda_fallback_raises_when_app_stays_foreground(monkeypatch):
    """回落 WDA terminate 后目标仍在前台（iOS 17+ 静默拒绝）应判失败。"""
    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    monkeypatch.setattr(driver, "current_app", lambda: "com.example.testapp")
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    try:
        driver.terminate_app("com.example.testapp")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "仍在前台" in message
    assert fake_wda.terminated == ["com.example.testapp"]


def test_ios_terminate_app_wda_fallback_propagates_wda_error(monkeypatch):
    """WDA terminate 自身抛错时，回落路径要翻成 RuntimeError 让上层判失败。"""
    fake_wda = _FakeWda()
    fake_wda.raise_on_terminate = RuntimeError("WDA 500")
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    try:
        driver.terminate_app("com.example.testapp")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "回落 WDA terminate 失败" in message
