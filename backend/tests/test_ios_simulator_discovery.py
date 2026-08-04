"""发现层的字段映射与状态过滤。

这些用例一律显式传 ``managed_only=False``——它们验的是「扫到之后怎么翻译」，
与「是否本平台启动」无关。纳管过滤本身在 test_ios_simulator_integration.py 里
单独覆盖。
"""
import pytest

from ai_phone.agent.drivers import ios_simulator as sim_mod
from ai_phone.agent.drivers.simctl import SimulatorDevice


_DT_17PRO = "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"
_DT_IPAD = "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-11-inch-M4-8GB"


@pytest.fixture(autouse=True)
def _stub_device_type_names(monkeypatch):
    """机型名与屏幕规格都用桩，避免用例依赖本机 Xcode 实际装了哪些机型。"""
    from ai_phone.agent.drivers.simctl import DeviceTypeScreen

    monkeypatch.setattr(
        sim_mod,
        "device_type_names",
        lambda *a, **k: {
            _DT_17PRO: "iPhone 17 Pro",
            _DT_IPAD: "iPad Pro 11-inch (M4)",
        },
    )
    monkeypatch.setattr(
        sim_mod,
        "device_type_screens",
        lambda *a, **k: {
            _DT_17PRO: DeviceTypeScreen(width=1206, height=2622, scale=3),
            _DT_IPAD: DeviceTypeScreen(width=1668, height=2420, scale=2),
        },
    )


def _sim(udid="UDID-1", name="iPhone 17 Pro", state="Booted", available=True,
         runtime_name="iOS 26.0", runtime_version="26.0.1",
         device_type_id=_DT_17PRO):
    return SimulatorDevice(
        udid=udid,
        name=name,
        state=state,
        runtime_id="com.apple.CoreSimulator.SimRuntime.iOS-26-0",
        runtime_name=runtime_name,
        runtime_version=runtime_version,
        device_type_id=device_type_id,
        is_available=available,
    )


def _patch(monkeypatch, sims):
    monkeypatch.setattr(sim_mod, "list_simulators", lambda *a, **k: sims)


# --------------------------------------------------------------------------
# DeviceInfo 字段映射
# --------------------------------------------------------------------------
def test_maps_core_fields(monkeypatch):
    _patch(monkeypatch, [_sim()])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.serial == "UDID-1"
    assert info.platform == "ios_sim"
    assert info.brand == "Apple"
    assert info.model == "iPhone 17 Pro"
    assert info.os_version == "26.0.1"
    assert info.status == "online"


def test_extra_matches_android_vm_convention(monkeypatch):
    """extra 三件套必须与 android_vm/manager.py decorate_devices 完全一致。"""
    _patch(monkeypatch, [_sim()])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.extra["device_kind"] == "virtual"
    assert info.extra["is_virtual"] is True
    assert info.extra["vm_platform"] == "ios"


def test_does_not_fake_managed_instance_identity(monkeypatch):
    """未纳管的虚拟机不得带 vm_instance_id / vm_name（方案 §0.3）。"""
    _patch(monkeypatch, [_sim()])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert "vm_instance_id" not in info.extra
    assert "vm_name" not in info.extra


def test_carries_simulator_specific_metadata(monkeypatch):
    _patch(monkeypatch, [_sim()])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.extra["sim_runtime"] == "iOS 26.0"
    assert info.extra["sim_runtime_id"].endswith("iOS-26-0")
    assert info.extra["sim_device_type"].endswith("iPhone-17-Pro")
    assert info.extra["sim_state"] == "Booted"


def test_falls_back_to_runtime_name_when_version_missing(monkeypatch):
    _patch(monkeypatch, [_sim(runtime_version="")])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.os_version == "iOS 26.0"


# --------------------------------------------------------------------------
# 屏幕尺寸
#
# 这一项必须在**发现阶段**就有值。前端手动点击按「设备像素」换算坐标，拿不到就
# 退化成用镜像画面尺寸当基准，而镜像是缩放过的，点击会整体偏向左上。
# VLM 那条路不受影响（用未缩放截图），但两条路都要准。
# --------------------------------------------------------------------------
def test_reports_screen_size_at_discovery(monkeypatch):
    _patch(monkeypatch, [_sim()])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert (info.screen_width, info.screen_height) == (1206, 2622)


def test_reports_screen_size_for_ipad(monkeypatch):
    _patch(monkeypatch, [_sim(device_type_id=_DT_IPAD)])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert (info.screen_width, info.screen_height) == (1668, 2420)


def test_screen_size_is_portrait_fixed_not_current_orientation(monkeypatch):
    """报的必须是**固定竖屏**尺寸，与真机 lockdown 同性质。

    报「当前方向」的尺寸会随业务转屏变化，存进设备记录就成了错的——横屏时存下
    2622×1206，转回竖屏后这条记录就一直是错的。旋转由前端按镜像画面方向换算。
    """
    _patch(monkeypatch, [_sim()])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.screen_width < info.screen_height, "必须是竖屏方向（短边为宽）"


def test_unknown_device_type_falls_back_to_zero(monkeypatch):
    """查不到规格就报 0——与真机 lockdown 读不到时一致，不猜一个假尺寸。"""
    _patch(monkeypatch, [_sim(device_type_id="com.apple.CoreSimulator.SimDeviceType.Unknown")])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.screen_width == 0 and info.screen_height == 0


# --------------------------------------------------------------------------
# 过滤规则
# --------------------------------------------------------------------------
def test_only_booted_by_default(monkeypatch):
    _patch(monkeypatch, [
        _sim("BOOTED", state="Booted"),
        _sim("SHUT", state="Shutdown"),
        _sim("BOOTING", state="Booting"),
    ])
    assert [i.serial for i in sim_mod.list_ios_simulators(managed_only=False)] == ["BOOTED"]


def test_include_offline_brings_templates_as_offline(monkeypatch):
    _patch(monkeypatch, [
        _sim("BOOTED", state="Booted"),
        _sim("SHUT", state="Shutdown"),
    ])
    infos = sim_mod.list_ios_simulators(include_offline=True, managed_only=False)
    assert [(i.serial, i.status) for i in infos] == [
        ("BOOTED", "online"),
        ("SHUT", "offline"),
    ]


def test_unavailable_runtime_never_reported(monkeypatch):
    _patch(monkeypatch, [_sim("BROKEN", state="Shutdown", available=False)])
    assert sim_mod.list_ios_simulators(managed_only=False) == []
    assert sim_mod.list_ios_simulators(include_offline=True, managed_only=False) == []


def test_iphone_and_ipad_both_reported(monkeypatch):
    _patch(monkeypatch, [
        _sim("PHONE", device_type_id=_DT_17PRO),
        _sim("PAD", device_type_id=_DT_IPAD),
    ])
    infos = sim_mod.list_ios_simulators(managed_only=False)
    assert [i.model for i in infos] == ["iPhone 17 Pro", "iPad Pro 11-inch (M4)"]


def test_model_uses_real_device_type_not_instance_name(monkeypatch):
    """受管实例的 name 是 aiphone_sim_<vmid>，绝不能当机型上报给前端。"""
    _patch(monkeypatch, [_sim("U", name="aiphone_sim_abc123", device_type_id=_DT_17PRO)])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.model == "iPhone 17 Pro"
    assert info.extra["sim_instance_name"] == "aiphone_sim_abc123"


def test_model_falls_back_to_instance_name_when_type_unknown(monkeypatch):
    _patch(monkeypatch, [_sim("U", name="某台机器", device_type_id="unknown.type")])
    (info,) = sim_mod.list_ios_simulators(managed_only=False)
    assert info.model == "某台机器"


# --------------------------------------------------------------------------
# fail-closed：绝不能把异常抛进三端公用扫描链路
# --------------------------------------------------------------------------
def test_swallows_simctl_layer_exception(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("底座炸了")

    monkeypatch.setattr(sim_mod, "list_simulators", _raise)
    assert sim_mod.list_ios_simulators(managed_only=False) == []


def test_skips_single_bad_entry_but_keeps_others(monkeypatch):
    good = _sim("GOOD")
    _patch(monkeypatch, [good, good])
    calls = {"n": 0}
    real = sim_mod._to_device_info

    def _flaky(sim):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("转换失败")
        return real(sim)

    monkeypatch.setattr(sim_mod, "_to_device_info", _flaky)
    infos = sim_mod.list_ios_simulators(managed_only=False)
    assert [i.serial for i in infos] == ["GOOD"]


def test_empty_when_no_simulators(monkeypatch):
    _patch(monkeypatch, [])
    assert sim_mod.list_ios_simulators(managed_only=False) == []


# --------------------------------------------------------------------------
# 没有纳管实例的宿主：必须与本能力上线前完全等价，一次 simctl 都不许 fork
# --------------------------------------------------------------------------
def test_no_managed_instance_never_touches_simctl(monkeypatch):
    """纳管表为空时结果必然是空列表，不该为此付出 subprocess 代价。

    list_ios_simulators 挂在 Android / iOS 真机 / Harmony 公用的扫描链路上，
    5 秒一轮。若把纳管过滤放在 simctl 之后，一台根本没有虚拟机的 Agent 每天
    要凭空多跑约 1.7 万次 xcrun 与 CoreSimulatorService XPC 往返。
    """
    def _boom(*a, **k):
        raise AssertionError("纳管表为空却仍然调用了 simctl")

    monkeypatch.setattr(sim_mod, "list_simulators", _boom)
    monkeypatch.setattr(sim_mod, "managed_udids", lambda: set())

    assert sim_mod.list_ios_simulators() == []
    assert sim_mod.list_ios_simulators(include_offline=True) == []


def test_managed_instance_present_still_scans(monkeypatch):
    """有纳管实例时短路必须失效，否则虚拟机自己就发现不了了。"""
    _patch(monkeypatch, [_sim("U1"), _sim("U2")])
    monkeypatch.setattr(sim_mod, "managed_udids", lambda: {"U2"})

    assert [i.serial for i in sim_mod.list_ios_simulators()] == ["U2"]
