import json

from ai_phone.agent.drivers import simctl as simctl_mod


_IOS_RT = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
_WATCH_RT = "com.apple.CoreSimulator.SimRuntime.watchOS-11-0"
_TV_RT = "com.apple.CoreSimulator.SimRuntime.tvOS-18-0"


def _device(udid: str, name: str = "iPhone 17 Pro", state: str = "Shutdown", **kw):
    base = {
        "udid": udid,
        "name": name,
        "state": state,
        "isAvailable": True,
        "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
    }
    base.update(kw)
    return base


def _runtimes():
    return {
        _IOS_RT: {
            "identifier": _IOS_RT,
            "version": "26.0.1",
            "name": "iOS 26.0",
            "platform": "iOS",
            "isAvailable": True,
        }
    }


# --------------------------------------------------------------------------
# parse_devices_json
# --------------------------------------------------------------------------
def test_parse_reads_core_fields():
    payload = {"devices": {_IOS_RT: [_device("UDID-1", state="Booted")]}}
    devices = simctl_mod.parse_devices_json(payload, _runtimes())
    assert len(devices) == 1
    d = devices[0]
    assert d.udid == "UDID-1"
    assert d.name == "iPhone 17 Pro"
    assert d.state == "Booted"
    assert d.is_booted is True
    assert d.runtime_id == _IOS_RT
    assert d.runtime_name == "iOS 26.0"
    assert d.runtime_version == "26.0.1"
    assert d.is_available is True


def test_parse_drops_non_ios_runtimes():
    payload = {
        "devices": {
            _IOS_RT: [_device("IOS-1")],
            _WATCH_RT: [_device("WATCH-1", name="Apple Watch")],
            _TV_RT: [_device("TV-1", name="Apple TV")],
        }
    }
    devices = simctl_mod.parse_devices_json(payload, _runtimes())
    assert [d.udid for d in devices] == ["IOS-1"]


def test_parse_falls_back_to_readable_runtime_name_when_runtime_unknown():
    payload = {"devices": {_IOS_RT: [_device("UDID-1")]}}
    devices = simctl_mod.parse_devices_json(payload, runtimes={})
    assert devices[0].runtime_name == "iOS 26.0"
    assert devices[0].runtime_version == ""


def test_parse_skips_entries_without_udid():
    payload = {"devices": {_IOS_RT: [_device(""), {"name": "x"}, _device("OK")]}}
    devices = simctl_mod.parse_devices_json(payload, _runtimes())
    assert [d.udid for d in devices] == ["OK"]


def test_parse_tolerates_empty_and_malformed_payloads():
    assert simctl_mod.parse_devices_json({}, {}) == []
    assert simctl_mod.parse_devices_json({"devices": {}}, {}) == []
    assert simctl_mod.parse_devices_json({"devices": {_IOS_RT: None}}, {}) == []
    assert simctl_mod.parse_devices_json({"devices": {_IOS_RT: ["junk"]}}, {}) == []


# --------------------------------------------------------------------------
# list_simulators —— 重点验 fail-closed：任何失败都只能返回 []，不能抛
# --------------------------------------------------------------------------
def _patch(monkeypatch, *, available=True, run=None, runtimes=None):
    monkeypatch.setattr(simctl_mod, "simctl_available", lambda: available)
    if run is not None:
        monkeypatch.setattr(simctl_mod, "simctl_run", run)
    monkeypatch.setattr(simctl_mod, "_get_runtimes", lambda *a, **k: runtimes or _runtimes())


def test_list_returns_empty_when_simctl_unavailable(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("simctl 不可用时不应执行任何命令")

    _patch(monkeypatch, available=False, run=_boom)
    assert simctl_mod.list_simulators() == []


def test_list_parses_booted_and_shutdown(monkeypatch):
    payload = {
        "devices": {
            _IOS_RT: [
                _device("BOOTED-1", state="Booted"),
                _device("SHUT-1", state="Shutdown"),
            ]
        }
    }
    _patch(monkeypatch, run=lambda *a, **k: json.dumps(payload))
    assert {d.udid for d in simctl_mod.list_simulators()} == {"BOOTED-1", "SHUT-1"}
    assert [d.udid for d in simctl_mod.list_simulators(booted_only=True)] == ["BOOTED-1"]


def test_list_swallows_simctl_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise simctl_mod.SimctlError(["xcrun", "simctl"], 1, "", "boom")

    _patch(monkeypatch, run=_raise)
    assert simctl_mod.list_simulators() == []


def test_list_swallows_invalid_json(monkeypatch):
    _patch(monkeypatch, run=lambda *a, **k: "not json at all")
    assert simctl_mod.list_simulators() == []


def test_list_swallows_non_dict_json(monkeypatch):
    _patch(monkeypatch, run=lambda *a, **k: "[1, 2, 3]")
    assert simctl_mod.list_simulators() == []


def test_list_swallows_unexpected_exception(monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("完全意料之外的错误")

    _patch(monkeypatch, run=_raise)
    assert simctl_mod.list_simulators() == []


def test_list_returns_empty_on_blank_output(monkeypatch):
    _patch(monkeypatch, run=lambda *a, **k: "")
    assert simctl_mod.list_simulators() == []


# --------------------------------------------------------------------------
# 非 macOS 宿主
# --------------------------------------------------------------------------
def test_unavailable_on_non_darwin(monkeypatch):
    simctl_mod.reset_probe_cache_for_tests()
    monkeypatch.setattr(simctl_mod._platform, "system", lambda: "Linux")
    try:
        assert simctl_mod.simctl_available() is False
        assert simctl_mod.list_simulators() == []
    finally:
        simctl_mod.reset_probe_cache_for_tests()


def test_readable_runtime_name():
    assert simctl_mod._readable_runtime_name(_IOS_RT) == "iOS 26.0"
    assert (
        simctl_mod._readable_runtime_name(
            "com.apple.CoreSimulator.SimRuntime.iOS-17-4"
        )
        == "iOS 17.4"
    )


# --------------------------------------------------------------------------
# 机型屏幕规格
#
# 设备发现阶段就要拿到屏幕尺寸（否则手动点击坐标系整体偏移），而虚拟机不像真机
# 能读 lockdown。出路是机型 bundle 自带的 profile.plist——纯本地文件，不需要
# 设备开机、更不需要 WDA。
# --------------------------------------------------------------------------
def _write_profile(root, name, *, width, height, scale):
    import plistlib

    bundle = root / f"{name}.simdevicetype"
    res = bundle / "Contents" / "Resources"
    res.mkdir(parents=True)
    (res / "profile.plist").write_bytes(
        plistlib.dumps(
            {
                "mainScreenWidth": width,
                "mainScreenHeight": height,
                "mainScreenScale": scale,
            }
        )
    )
    return bundle


def test_read_profile_screen(tmp_path):
    bundle = _write_profile(tmp_path, "iPhone 11", width=828, height=1792, scale=2)
    screen = simctl_mod._read_profile_screen(bundle)
    assert screen == simctl_mod.DeviceTypeScreen(width=828, height=1792, scale=2)


def test_read_profile_screen_normalizes_to_portrait(tmp_path):
    """个别机型（尤其 iPad）有把宽高写反的先例，统一归一化成竖屏。"""
    bundle = _write_profile(tmp_path, "iPad X", width=2420, height=1668, scale=2)
    screen = simctl_mod._read_profile_screen(bundle)
    assert (screen.width, screen.height) == (1668, 2420)


def test_read_profile_screen_missing_file(tmp_path):
    assert simctl_mod._read_profile_screen(tmp_path / "nope.simdevicetype") is None


def test_read_profile_screen_bad_plist(tmp_path):
    bundle = tmp_path / "X.simdevicetype"
    res = bundle / "Contents" / "Resources"
    res.mkdir(parents=True)
    (res / "profile.plist").write_bytes(b"not a plist at all")
    assert simctl_mod._read_profile_screen(bundle) is None


def test_read_profile_screen_zero_size_rejected(tmp_path):
    """尺寸为 0 等于没读到，不能当有效值——下游会拿它算坐标。"""
    bundle = _write_profile(tmp_path, "Y", width=0, height=0, scale=2)
    assert simctl_mod._read_profile_screen(bundle) is None


def test_device_type_screens_maps_by_identifier(monkeypatch, tmp_path):
    bundle = _write_profile(tmp_path, "iPhone 11", width=828, height=1792, scale=2)
    payload = {
        "devicetypes": [
            {
                "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-11",
                "name": "iPhone 11",
                "bundlePath": str(bundle),
            }
        ]
    }
    simctl_mod.reset_probe_cache_for_tests()
    monkeypatch.setattr(
        simctl_mod, "simctl_run", lambda *a, **k: __import__("json").dumps(payload)
    )
    try:
        screens = simctl_mod.device_type_screens(force_refresh=True)
        got = screens["com.apple.CoreSimulator.SimDeviceType.iPhone-11"]
        assert (got.width, got.height, got.scale) == (828, 1792, 2)
    finally:
        simctl_mod.reset_probe_cache_for_tests()


def test_device_type_screens_skips_unreadable_entries(monkeypatch, tmp_path):
    """读不出规格的机型直接不进表，不塞一个假值进去。"""
    payload = {
        "devicetypes": [
            {"identifier": "com.x.Ghost", "bundlePath": str(tmp_path / "nope")},
        ]
    }
    simctl_mod.reset_probe_cache_for_tests()
    monkeypatch.setattr(
        simctl_mod, "simctl_run", lambda *a, **k: __import__("json").dumps(payload)
    )
    try:
        assert simctl_mod.device_type_screens(force_refresh=True) == {}
    finally:
        simctl_mod.reset_probe_cache_for_tests()


def test_device_type_screens_falls_back_to_cache_on_failure(monkeypatch, tmp_path):
    bundle = _write_profile(tmp_path, "iPhone 11", width=828, height=1792, scale=2)
    payload = {
        "devicetypes": [
            {"identifier": "com.x.P", "bundlePath": str(bundle)},
        ]
    }
    simctl_mod.reset_probe_cache_for_tests()
    try:
        monkeypatch.setattr(
            simctl_mod, "simctl_run", lambda *a, **k: __import__("json").dumps(payload)
        )
        first = simctl_mod.device_type_screens(force_refresh=True)
        assert "com.x.P" in first

        def boom(*_a, **_k):
            raise RuntimeError("simctl down")

        monkeypatch.setattr(simctl_mod, "simctl_run", boom)
        assert simctl_mod.device_type_screens(force_refresh=True) == first
    finally:
        simctl_mod.reset_probe_cache_for_tests()
