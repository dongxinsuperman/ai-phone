"""iOS 虚拟机接入设备扫描链路的行为约束。

重点不是「能发现」，而是「接进来之后绝不影响现有流程」——见
docs-internal/ios-simulator-plan（iOS虚拟机独立接入方案）.md 的核心铁律。
"""
import pytest

from ai_phone.agent import drivers as drivers_pkg
from ai_phone.agent.drivers import ios_simulator as sim_mod
from ai_phone.agent.drivers.base import DeviceInfo


def _sim_info(serial="SIM-1"):
    return DeviceInfo(
        serial=serial,
        platform="ios_sim",
        brand="Apple",
        model="iPhone 17 Pro",
        os_version="26.0.1",
        status="online",
        extra={"device_kind": "virtual", "is_virtual": True, "vm_platform": "ios"},
    )


def _android_info(serial="ANDROID-1"):
    return DeviceInfo(serial=serial, platform="android", status="online")


@pytest.fixture
def stub_scans(monkeypatch):
    """把三端真实扫描全部替换成桩，只留 Android 一台做「现有流程」的参照物。"""
    monkeypatch.setattr(drivers_pkg, "list_android_devices", lambda **k: [_android_info()])
    monkeypatch.setattr(drivers_pkg, "list_ios_devices", lambda **k: [])
    monkeypatch.setattr(drivers_pkg, "list_harmony_devices", lambda **k: [])
    monkeypatch.setattr(drivers_pkg, "list_ios_simulators", lambda **k: [_sim_info()])


# --------------------------------------------------------------------------
# 接入顺序与隔离
# --------------------------------------------------------------------------
def test_simulators_appended_after_existing_platforms(stub_scans):
    """虚拟机追加在末尾，不打乱既有平台的顺序与内容。"""
    devices = drivers_pkg.list_all_devices()
    assert [d.platform for d in devices] == ["android", "ios_sim"]


def test_simulator_scan_exception_does_not_break_other_platforms(monkeypatch, stub_scans):
    def _raise(**kwargs):
        raise RuntimeError("虚拟机扫描炸了")

    monkeypatch.setattr(drivers_pkg, "list_ios_simulators", _raise)
    devices = drivers_pkg.list_all_devices()
    assert [d.platform for d in devices] == ["android"]


# --------------------------------------------------------------------------
# 只认平台自己启动的实例（对齐 Android VM）
# --------------------------------------------------------------------------
def test_scan_path_requests_managed_only(monkeypatch, stub_scans):
    """设备上报路径必须走 managed_only 语义，不得把宿主上所有虚拟机都带出来。"""
    seen = {}

    def _spy(include_offline=False, managed_only=True):
        seen["managed_only"] = managed_only
        return []

    monkeypatch.setattr(drivers_pkg, "list_ios_simulators", _spy)
    drivers_pkg.list_all_devices()
    assert seen["managed_only"] is True


def test_no_managed_instances_means_no_devices(monkeypatch):
    """纳管表为空时（生命周期能力落地前的现状）设备池里不能出现任何虚拟机。"""
    sim_mod.reset_managed_for_tests()
    monkeypatch.setattr(
        sim_mod, "list_simulators", lambda *a, **k: [_raw_sim("USER-STARTED")]
    )
    assert sim_mod.list_ios_simulators() == []


def test_only_managed_instances_are_reported(monkeypatch):
    sim_mod.reset_managed_for_tests()
    monkeypatch.setattr(
        sim_mod,
        "list_simulators",
        lambda *a, **k: [_raw_sim("OURS"), _raw_sim("USER-STARTED")],
    )
    sim_mod.mark_managed("OURS")
    try:
        assert [d.serial for d in sim_mod.list_ios_simulators()] == ["OURS"]
    finally:
        sim_mod.reset_managed_for_tests()


def test_unmark_managed_removes_device_from_pool(monkeypatch):
    sim_mod.reset_managed_for_tests()
    monkeypatch.setattr(sim_mod, "list_simulators", lambda *a, **k: [_raw_sim("OURS")])
    sim_mod.mark_managed("OURS")
    assert len(sim_mod.list_ios_simulators()) == 1
    sim_mod.unmark_managed("OURS")
    assert sim_mod.list_ios_simulators() == []


def test_managed_only_false_is_available_for_manager_reconcile(monkeypatch):
    """manager 做重连认领 / 容量探查时需要看到全量，但这条路不用于设备上报。"""
    sim_mod.reset_managed_for_tests()
    monkeypatch.setattr(sim_mod, "list_simulators", lambda *a, **k: [_raw_sim("ANY")])
    assert [d.serial for d in sim_mod.list_ios_simulators(managed_only=False)] == ["ANY"]


def _raw_sim(udid):
    from ai_phone.agent.drivers.simctl import SimulatorDevice

    return SimulatorDevice(
        udid=udid,
        name="iPhone 17 Pro",
        state="Booted",
        runtime_id="com.apple.CoreSimulator.SimRuntime.iOS-26-0",
        runtime_name="iOS 26.0",
        runtime_version="26.0.1",
        device_type_id="com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
        is_available=True,
    )


# --------------------------------------------------------------------------
# open_driver 路由：已接入 Driver
# --------------------------------------------------------------------------
def test_open_driver_routes_to_simulator_factory(monkeypatch):
    seen = {}

    def _fake(serial, **kwargs):
        seen["serial"] = serial
        seen["kwargs"] = kwargs
        return "driver-sentinel"

    monkeypatch.setattr(drivers_pkg, "open_ios_simulator_driver", _fake)
    got = drivers_pkg.open_driver("SIM-1", "ios_sim", on_status=None)
    assert got == "driver-sentinel"
    assert seen["serial"] == "SIM-1"
    assert "on_status" in seen["kwargs"]


def test_open_driver_still_rejects_unknown_platform():
    with pytest.raises(ValueError):
        drivers_pkg.open_driver("X", "windows_phone")


# --------------------------------------------------------------------------
# 派单：对外三端不变，虚拟机并入 iOS 池
#
# M1/M2 阶段这里曾有两道锁（不在 ALLOWED_PLATFORMS、没有 readiness probe），
# 因为那时平台还不能自己启动实例，开放了也无设备可派。M3 打通生命周期后解锁，
# 口径与 Android / 鸿蒙对齐：虚拟设备与真机同池。
# --------------------------------------------------------------------------
def test_submission_platforms_stay_at_three():
    """ios_sim 是**内部**平台值，绝不能出现在提交接口的平台枚举里。

    对外只有三端。加进去等于让调用方多出一个平台概念，与产品口径冲突。
    """
    from ai_phone.server.scheduler.service import ALLOWED_PLATFORMS

    assert ALLOWED_PLATFORMS == ("android", "ios", "harmony")
    assert "ios_sim" not in ALLOWED_PLATFORMS


def test_ios_submission_can_land_on_simulator():
    """提交 ios 时，候选设备要同时包含真机与虚拟机。

    与 Android 一致——Android 虚拟机的 Device.platform 本来就是 android，
    和真机同池。iOS 虚拟机因为内部必须与真机分开才记成 ios_sim，派单时得还原。
    """
    from ai_phone.server.scheduler.service import device_platforms_for

    assert device_platforms_for("ios") == ("ios", "ios_sim")


def test_other_platforms_are_not_widened():
    from ai_phone.server.scheduler.service import device_platforms_for

    assert device_platforms_for("android") == ("android",)
    assert device_platforms_for("harmony") == ("harmony",)


def test_simulator_has_readiness_probe():
    """有 probe 才会上报 readiness，调度器 _pick_device 才可能选中它。"""
    from ai_phone.agent.health.probe import IosSimProbe, build_probe_for

    probe = build_probe_for("ios_sim", "SIM-1")
    assert isinstance(probe, IosSimProbe)
    assert probe.platform == "ios_sim"
    for platform in ("android", "ios", "harmony"):
        assert build_probe_for(platform, "X") is not None


def test_simulator_probe_does_not_use_real_device_client_map():
    """虚拟机探针必须读自己的登记表，不能碰真机那张（挂着拔插生命周期策略）。"""
    import inspect

    from ai_phone.agent.health.probe import IosSimProbe

    src = inspect.getsource(IosSimProbe._probe_sync)
    assert "_WDA_CLIENT_MAP" not in src
    assert "get_sim_endpoint" in src


# --------------------------------------------------------------------------
# 协议
# --------------------------------------------------------------------------
def test_protocol_platform_literal_includes_ios_sim():
    from typing import get_args

    from ai_phone.shared.protocol import Platform

    args = get_args(Platform)
    assert "ios_sim" in args
    for platform in ("android", "ios", "harmony"):
        assert platform in args
