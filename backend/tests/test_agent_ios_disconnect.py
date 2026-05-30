from types import SimpleNamespace

from ai_phone.agent import main as agent_main
from ai_phone.agent.drivers import ios as ios_driver


def test_trusted_ios_disconnect_drops_old_driver_cache(monkeypatch):
    closed = []
    disconnected = []

    class FakeDriver:
        platform = "ios"

        def close(self):
            closed.append(True)

    class FakePolicy:
        is_stable = True

        def on_device_disconnected(self, serial):
            disconnected.append(serial)

    monkeypatch.setattr(ios_driver, "was_last_ios_scan_ok", lambda: True)
    monkeypatch.setattr(agent_main, "get_ios_wda_lifecycle_policy", lambda: FakePolicy())
    monkeypatch.setattr(agent_main, "_last_seen_ios_serials", {"S1"})
    agent_main._driver_cache["S1"] = FakeDriver()

    try:
        agent_main._emit_ios_disconnect_events([])
    finally:
        agent_main._driver_cache.pop("S1", None)
        monkeypatch.setattr(agent_main, "_last_seen_ios_serials", set())

    assert disconnected == ["S1"]
    assert closed == [True]
    assert "S1" not in agent_main._driver_cache


def test_ios_disconnect_ignores_untrusted_scan(monkeypatch):
    closed = []

    class FakeDriver:
        platform = "ios"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ios_driver, "was_last_ios_scan_ok", lambda: False)
    monkeypatch.setattr(agent_main, "_last_seen_ios_serials", {"S1"})
    agent_main._driver_cache["S1"] = FakeDriver()

    try:
        agent_main._emit_ios_disconnect_events([])
    finally:
        agent_main._driver_cache.pop("S1", None)
        monkeypatch.setattr(agent_main, "_last_seen_ios_serials", set())

    assert closed == []


def test_ios_disconnect_keeps_cache_for_present_ios(monkeypatch):
    closed = []

    class FakeDriver:
        platform = "ios"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ios_driver, "was_last_ios_scan_ok", lambda: True)
    monkeypatch.setattr(agent_main, "_last_seen_ios_serials", {"S1"})
    agent_main._driver_cache["S1"] = FakeDriver()

    try:
        agent_main._emit_ios_disconnect_events([
            SimpleNamespace(platform="ios", serial="S1"),
        ])
    finally:
        agent_main._driver_cache.pop("S1", None)
        monkeypatch.setattr(agent_main, "_last_seen_ios_serials", set())

    assert closed == []
