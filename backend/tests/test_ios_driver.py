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


class _FakeWda:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.launched: list[str] = []
        self.raise_on_terminate: Exception | None = None
        self.raise_on_launch: Exception | None = None

    def terminate_app(self, bundle_id: str) -> None:
        if self.raise_on_terminate is not None:
            raise self.raise_on_terminate
        self.terminated.append(bundle_id)

    def launch_app(self, bundle_id: str) -> None:
        if self.raise_on_launch is not None:
            raise self.raise_on_launch
        self.launched.append(bundle_id)


def test_ios_terminate_app_falls_back_to_wda_when_no_tunneld(monkeypatch):
    """iOS 15/16 无 DVT 通道（rsd 为 None）时，应回落 WDA terminate 而非直接报错。"""
    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    # terminate 后目标已不在前台 → 复核通过
    monkeypatch.setattr(driver, "current_app", lambda: "com.apple.springboard")
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    driver.terminate_app("com.guanghe.ycmathEnterpriseTest")

    assert fake_wda.terminated == ["com.guanghe.ycmathEnterpriseTest"]


def test_ios_terminate_app_wda_fallback_raises_when_app_stays_foreground(monkeypatch):
    """回落 WDA terminate 后目标仍在前台（iOS 17+ 静默拒绝）应判失败。"""
    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    monkeypatch.setattr(driver, "current_app", lambda: "com.guanghe.ycmathEnterpriseTest")
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    try:
        driver.terminate_app("com.guanghe.ycmathEnterpriseTest")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "仍在前台" in message
    assert fake_wda.terminated == ["com.guanghe.ycmathEnterpriseTest"]


def test_ios_terminate_app_wda_fallback_propagates_wda_error(monkeypatch):
    """WDA terminate 自身抛错时，回落路径要翻成 RuntimeError 让上层判失败。"""
    fake_wda = _FakeWda()
    fake_wda.raise_on_terminate = RuntimeError("WDA 500")
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "_try_get_tunneld_rsd", lambda: None)
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    try:
        driver.terminate_app("com.guanghe.ycmathEnterpriseTest")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "回落 WDA terminate 失败" in message


def test_ios_activate_app_success_when_app_reaches_foreground(monkeypatch):
    """launch 后目标已在前台 → 复核通过，不抛错。"""
    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    monkeypatch.setattr(driver, "current_app", lambda: "com.guanghe.ycmathEnterpriseTest")
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    driver.activate_app("com.guanghe.ycmathEnterpriseTest")

    assert fake_wda.launched == ["com.guanghe.ycmathEnterpriseTest"]


def test_ios_activate_app_raises_when_app_never_foreground(monkeypatch):
    """launch 后前台始终不是目标（静默失败/被弹窗挡住）→ 超时判失败。"""
    import itertools

    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    # 前台始终是 SpringBoard，配合快进时钟让轮询窗口迅速耗尽
    monkeypatch.setattr(driver, "current_app", lambda: "com.apple.springboard")
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)
    clock = itertools.count(0.0, 1.0)
    monkeypatch.setattr(ios_mod.time, "monotonic", lambda: next(clock))

    try:
        driver.activate_app("com.guanghe.ycmathEnterpriseTest")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "未切到前台" in message
    assert fake_wda.launched == ["com.guanghe.ycmathEnterpriseTest"]


def test_ios_activate_app_treats_current_app_error_as_success(monkeypatch):
    """current_app 复核自身异常（WDA 抖动）不应阻断 launch，按成功处理。"""
    fake_wda = _FakeWda()
    driver = IosDriver("IOS-1", object(), fake_wda)

    def boom() -> str:
        raise RuntimeError("WDA activeAppInfo 抖动")

    monkeypatch.setattr(driver, "current_app", boom)
    monkeypatch.setattr(ios_mod.time, "sleep", lambda *_: None)

    driver.activate_app("com.guanghe.ycmathEnterpriseTest")

    assert fake_wda.launched == ["com.guanghe.ycmathEnterpriseTest"]
