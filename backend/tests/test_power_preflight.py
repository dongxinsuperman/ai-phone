from __future__ import annotations

from types import SimpleNamespace

from ai_phone.agent.drivers import harmony as harmony_mod
from ai_phone.agent.drivers import ios as ios_mod
from ai_phone.agent.health import probe as probe_mod


def test_harmony_prepare_for_run_uses_pure_hdc_wakeup_and_swipe(monkeypatch):
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_hdc_shell(serial, cmd, **_kwargs):
        calls.append(f"{serial}:{cmd}")
        if "hidumper" in cmd:
            return "Current State: INACTIVE"
        return ""

    monkeypatch.setattr(harmony_mod, "hdc_shell", fake_hdc_shell)
    monkeypatch.setattr(harmony_mod.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        harmony_mod,
        "get_settings",
        lambda: SimpleNamespace(
            harmony_wake_before_run=True,
            harmony_wake_settle_ms=0,
            harmony_wake_swipe_enabled=True,
            harmony_wake_swipe_settle_ms=0,
        ),
    )

    harmony_mod.prepare_harmony_for_run("H1", screen_size=(1080, 2504), swipe=True)

    assert calls == [
        "H1:hidumper -s PowerManagerService -a -s",
        "H1:power-shell wakeup",
        "H1:uitest uiInput swipe 540 2053 540 876 1500",
    ]
    assert sleeps == []


def test_harmony_prepare_for_run_defaults_to_no_swipe_without_policy(monkeypatch):
    calls: list[str] = []

    def fake_hdc_shell(serial, cmd, **_kwargs):
        calls.append(f"{serial}:{cmd}")
        if "hidumper" in cmd:
            return "Current State: INACTIVE"
        return ""

    monkeypatch.setattr(harmony_mod, "hdc_shell", fake_hdc_shell)
    monkeypatch.setattr(
        harmony_mod,
        "get_settings",
        lambda: SimpleNamespace(
            harmony_wake_before_run=True,
            harmony_wake_settle_ms=0,
            harmony_wake_swipe_enabled=True,
            harmony_wake_swipe_settle_ms=0,
        ),
    )

    harmony_mod.prepare_harmony_for_run("H1", screen_size=(1080, 2504))

    assert calls == ["H1:power-shell wakeup"]


def test_harmony_prepare_for_run_skips_swipe_when_already_lit(monkeypatch):
    calls: list[str] = []

    def fake_hdc_shell(serial, cmd, **_kwargs):
        calls.append(f"{serial}:{cmd}")
        if "hidumper" in cmd:
            return "Current State: AWAKE"
        return ""

    monkeypatch.setattr(harmony_mod, "hdc_shell", fake_hdc_shell)
    monkeypatch.setattr(
        harmony_mod,
        "get_settings",
        lambda: SimpleNamespace(
            harmony_wake_before_run=True,
            harmony_wake_settle_ms=0,
            harmony_wake_swipe_enabled=True,
            harmony_wake_swipe_settle_ms=0,
        ),
    )

    harmony_mod.prepare_harmony_for_run("H1", screen_size=(1080, 2504), swipe=True)

    assert calls == [
        "H1:hidumper -s PowerManagerService -a -s",
        "H1:power-shell wakeup",
    ]


def test_android_screen_off_can_be_dispatchable_by_env(monkeypatch):
    class FakeDevice:
        def shell(self, cmd: str) -> str:
            if cmd == "echo ok":
                return "ok"
            if "dumpsys power" in cmd:
                return "Display Power: state=OFF\n"
            raise AssertionError(f"unexpected command: {cmd}")

    class FakeAdb:
        def device(self, serial: str):
            assert serial == "A1"
            return FakeDevice()

    monkeypatch.setattr(probe_mod, "get_settings", lambda: SimpleNamespace(android_screen_off_dispatchable=True))
    monkeypatch.setattr("adbutils.adb", FakeAdb())

    outcome = probe_mod.AndroidProbe("A1")._probe_sync()

    assert outcome.ready is True


def test_harmony_screen_off_can_be_dispatchable_by_env(monkeypatch):
    def fake_hdc_run(*args, **_kwargs):
        assert args == ("list", "targets")
        return "H1"

    def fake_hdc_shell(serial, cmd, **_kwargs):
        assert serial == "H1"
        assert cmd == "hidumper -s PowerManagerService -a -s"
        return "Current State: INACTIVE"

    monkeypatch.setattr(
        probe_mod,
        "get_settings",
        lambda: SimpleNamespace(harmony_screen_off_dispatchable=True),
    )
    monkeypatch.setattr("ai_phone.agent.drivers.hdc.hdc_run", fake_hdc_run)
    monkeypatch.setattr("ai_phone.agent.drivers.hdc.hdc_shell", fake_hdc_shell)

    outcome = probe_mod.HarmonyProbe("H1")._probe_sync()

    assert outcome.ready is True


def test_ios_screen_locked_can_be_dispatchable_by_env(monkeypatch):
    class FakeResp:
        def __init__(self, value=None):
            self.status_code = 200
            self._value = value

        def json(self):
            return {"value": self._value}

    def fake_get(url, **_kwargs):
        if url.endswith("/status"):
            return FakeResp()
        if url.endswith("/wda/locked"):
            return FakeResp(True)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(
        probe_mod,
        "get_settings",
        lambda: SimpleNamespace(ios_screen_off_dispatchable=True),
    )
    monkeypatch.setattr("httpx.get", fake_get)
    ios_mod._WDA_CLIENT_MAP["I1"] = SimpleNamespace(base_url="http://wda")
    try:
        outcome = probe_mod.IosProbe("I1")._probe_sync()
    finally:
        ios_mod._WDA_CLIENT_MAP.pop("I1", None)

    assert outcome.ready is True


def test_ios_prepare_for_run_only_unlocks(monkeypatch):
    calls: list[str] = []

    class FakeWda:
        def unlock(self):
            calls.append("unlock")

        def press_button(self, name):
            calls.append(f"press:{name}")

    monkeypatch.setattr(
        ios_mod,
        "get_settings",
        lambda: SimpleNamespace(ios_wake_before_run_settle_ms=0),
    )
    driver = ios_mod.IosDriver("I1", object(), FakeWda())

    driver.prepare_for_run()

    assert calls == ["unlock"]
