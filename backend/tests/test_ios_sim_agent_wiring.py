"""iOS 虚拟机在 agent/main.py 里的接线检查。

这类 bug 的特点是「单测全绿但线上什么都不发生」——handler 漏注册、方法名写错、
钩子挂错位置，都不会被业务单测覆盖。这里做接线层面的静态与结构检查。
"""
import inspect
import re
from pathlib import Path

import pytest

from ai_phone.agent.ios_sim import IosSimVmManager
from ai_phone.shared import protocol as P

_MAIN = Path(inspect.getfile(__import__("ai_phone.agent.main", fromlist=["x"])))
_SOURCE = _MAIN.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 四个下发消息必须都注册了 handler
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "msg_const",
    [
        "MSG_IOS_SIM_VM_CAPABILITY_PROBE",
        "MSG_IOS_SIM_VM_START",
        "MSG_IOS_SIM_VM_STOP",
        "MSG_IOS_SIM_VM_DELETE",
    ],
)
def test_message_handler_registered(msg_const):
    assert f"client.on(P.{msg_const}," in _SOURCE, (
        f"{msg_const} 没有在 agent/main.py 注册 handler——Server 下发后 Agent 会静默丢弃"
    )


def test_all_ios_sim_downstream_messages_are_covered():
    """协议里所有「Server → Agent」的 iOS 虚拟机消息都必须被注册，一个不漏。"""
    downstream = {
        name
        for name in dir(P)
        if name.startswith("MSG_IOS_SIM_VM_")
        and name.endswith(("_PROBE", "_START", "_STOP", "_DELETE"))
    }
    assert downstream, "协议里应当有 iOS 虚拟机的下发消息常量"
    missing = [n for n in sorted(downstream) if f"client.on(P.{n}," not in _SOURCE]
    assert not missing, f"以下下发消息未注册 handler：{missing}"


# --------------------------------------------------------------------------
# main.py 调用的方法必须在 manager 上真实存在（防方法名写错）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method",
    [
        "handle_capability_probe",
        "handle_start",
        "handle_stop",
        "handle_delete",
        "decorate_devices",
        "reconcile_running_vms_sync",
        "report_reclaimed_vms",
        "report_orphan_reconcile",
        "sweep_vanished_vms",
    ],
)
def test_manager_exposes_method(method):
    assert callable(getattr(IosSimVmManager, method, None)), (
        f"IosSimVmManager 缺少 {method}——main.py 里调了会 AttributeError"
    )


def test_main_only_calls_existing_manager_methods():
    """扫源码里所有 _ios_sim_manager.xxx 调用，逐个核对方法存在。"""
    called = set(re.findall(r"_ios_sim_manager\.(\w+)", _SOURCE))
    assert called, "main.py 应当调用 iOS 虚拟机 manager"
    missing = [m for m in sorted(called) if not hasattr(IosSimVmManager, m)]
    assert not missing, f"main.py 调了不存在的方法：{missing}"


# --------------------------------------------------------------------------
# 三个生命周期钩子的挂载位置
# --------------------------------------------------------------------------
def test_reclaim_hook_runs_before_hello():
    """必须挂 on_pre_hello：认领会填纳管表，而设备上报只认纳管表里的 UDID。

    晚一步，首次 hello 的设备列表就会漏掉仍在运行的实例。
    """
    assert "client.on_pre_hello(_ios_sim_reclaim_before_hello)" in _SOURCE


def test_reclaim_and_reconcile_report_on_connect():
    assert "client.on_connect(_ios_sim_reclaim_on_connect)" in _SOURCE
    assert "client.on_connect(_ios_sim_orphan_reconcile_on_connect)" in _SOURCE


def test_liveness_sweep_on_rescan():
    assert "client.on_rescan(_ios_sim_liveness_sweep_on_rescan)" in _SOURCE


def test_decorate_devices_is_in_device_provider():
    """设备上报路径必须过 decorate，否则卡片上没有 vm_id 与别名。"""
    provider = _SOURCE.split("def _device_provider(")[1].split("\ndef ")[0]
    assert "_ios_sim_manager.decorate_devices(infos)" in provider


def test_manager_is_instantiated():
    assert "_ios_sim_manager = IosSimVmManager(" in _SOURCE


def test_manager_gets_driver_cache_dropper():
    """manager 必须拿到摘 driver 缓存的回调，与鸿蒙同形。

    manager 不认识 agent 的 _driver_cache，只能由 main 注入。漏注入不会报错，
    只会在「停止 → 启动 → 进工作台」时让镜像挂掉，属于典型的接线漏档。
    """
    assert "drop_driver_cache=_drop_ios_sim_driver_cache" in _SOURCE
    assert "def _drop_ios_sim_driver_cache(serial: str) -> None:" in _SOURCE


# --------------------------------------------------------------------------
# 铁律：每个 iOS 虚拟机钩子都必须独立 try 住，不能拖垮其他平台
# --------------------------------------------------------------------------
def test_every_ios_sim_hook_is_guarded():
    """任一钩子抛异常都不能中断 hello / rescan / connect 这些公共流程。"""
    for func in (
        "_ios_sim_reclaim_before_hello",
        "_ios_sim_reclaim_on_connect",
        "_ios_sim_orphan_reconcile_on_connect",
        "_ios_sim_liveness_sweep_on_rescan",
    ):
        body = _SOURCE.split(f"async def {func}(")[1].split("\n    client.on")[0]
        assert "try:" in body and "except Exception" in body, (
            f"{func} 未包 try/except——异常会拖垮公共流程，违反铁律"
        )


def test_decorate_devices_call_is_guarded():
    provider = _SOURCE.split("def _device_provider(")[1].split("\ndef ")[0]
    segment = provider.split("_ios_sim_manager is not None")[1]
    assert "try:" in segment and "except Exception" in segment


def test_lifecycle_api_matches_android_and_harmony():
    """三端 manager 的生命周期方法名必须逐字一致。

    名字不一致就是「特殊化」——同一件事在三处叫三个名字，日后改动很容易漏掉一端。
    本项目的口径是：技术上有差异的地方才分开，纯命名一律对齐。

    iOS 允许多出的方法只有 ``list_managed_vmids``（对应 Android 的
    ``list_managed_avd_vmids``，AVD 是安卓专有词，不该出现在 iOS 侧）。
    """
    from ai_phone.agent.android_vm.manager import AndroidVmManager
    from ai_phone.agent.harmony_vm.manager import HarmonyVmManager

    def public(cls):
        return {
            name
            for name in dir(cls)
            if not name.startswith("_") and callable(getattr(cls, name))
        }

    android, harmony, ios = (
        public(AndroidVmManager),
        public(HarmonyVmManager),
        public(IosSimVmManager),
    )
    shared = android & harmony
    missing = sorted(shared - ios)
    assert not missing, f"iOS 缺少三端共有的方法（或起了别的名字）：{missing}"

    # iOS 允许多出来的方法，每一条都要有技术差异做理由：
    #   list_managed_vmids  —— 纳管表是 iOS 特有的（UDID 由 simctl 生成，不像
    #                          Android 的 serial 能由端口算出来）
    #   restart_wda_sync    —— WDA 是 iOS 特有的控制通道，会卡死且能单独重起；
    #                          Android/鸿蒙的控制通道（adb / hdc）没有对应物
    allowed_extra = ["list_managed_vmids", "restart_wda_sync"]
    extra = sorted(ios - android - harmony)
    assert extra == allowed_extra, f"iOS 多出了不该有的方法：{extra}"


def test_no_stop_all_on_exit():
    """常驻语义：Agent 退出不关虚拟机，留着下次重连认领。

    与 Android / 鸿蒙一致——两者也都没在退出路径挂 stop_all。
    """
    tail = _SOURCE.split("except KeyboardInterrupt:")[-1]
    assert "_ios_sim_manager.stop_all()" not in tail


# --------------------------------------------------------------------------
# 进度回调必须转发给虚拟机
#
# 典型的「接线漏一档」：open_ios_simulator_driver 收 on_status、drivers.open_driver
# 转发 **kwargs、镜像会话也造好了 reporter，唯独 _get_or_open_driver 的条件只写了
# platform == "ios"，于是整套进度机制一次都没触发过——冷启动 8.8 秒全程白屏。
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# WDA 卡死自愈的三道守卫
#
# 这个动作会掐掉当前 WDA session，误触发的代价是让一个本来在跑的任务失败。
# 三道守卫缺一不可，且必须都在 main 里，不能指望调用方自觉。
# --------------------------------------------------------------------------
def test_self_heal_is_wired_into_readiness():
    assert "self_heal=_readiness_self_heal" in _SOURCE
    assert "def _readiness_self_heal(serial: str, platform: str) -> bool:" in _SOURCE


def test_self_heal_guards_are_present():
    """三道守卫：只碰虚拟机、没有 run 在跑、没有工作台会话。"""
    body = _SOURCE.split("def _readiness_self_heal(")[1].split("\n    readiness =")[0]

    assert 'if platform != "ios_sim":' in body, (
        "缺少平台守卫——真机的 stable 策略明确禁止自动重启 WDA"
    )
    assert "if supervisor.is_busy(serial):" in body, (
        "缺少 run 守卫，会打断正在跑的任务"
    )
    assert "if mirror_sup.get_session(serial) is not None:" in body, "缺少工作台守卫"

    # 三道都必须 return False 而不是只打日志
    assert body.count("return False") >= 3


def test_self_heal_only_restarts_wda_not_the_vm():
    """自愈只重起 WDA。关机重开要几十秒还会丢现场，不该在这里做。"""
    body = _SOURCE.split("def _readiness_self_heal(")[1].split("\n    readiness =")[0]
    assert "restart_wda_sync" in body
    for forbidden in ("stop_sync", "delete_sync", "start_sync"):
        assert forbidden not in body, f"自愈里不该出现 {forbidden}"


@pytest.mark.parametrize("platform", ["ios", "ios_sim"])
def test_on_status_forwarded_for_both_ios_channels(monkeypatch, platform):
    from ai_phone.agent import main as main_mod

    seen = {}

    def fake_open(serial, plat, **kwargs):
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(main_mod, "_open_driver_by_platform", fake_open)
    monkeypatch.setitem(main_mod._serial_platform, "S1", platform)
    main_mod._driver_cache.pop("S1", None)

    def reporter(*_a):
        pass

    try:
        main_mod._get_or_open_driver("S1", on_status=reporter)
    finally:
        main_mod._driver_cache.pop("S1", None)

    assert seen["kwargs"].get("on_status") is reporter, (
        f"{platform} 的 on_status 没有转发下去，前端将全程收不到启动进度"
    )


def test_on_status_not_forwarded_to_platforms_that_reject_it(monkeypatch):
    """Android / 鸿蒙的 open_driver 不接这个参数，传了会 TypeError。"""
    from ai_phone.agent import main as main_mod

    seen = {}

    def fake_open(serial, plat, **kwargs):
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(main_mod, "_open_driver_by_platform", fake_open)
    monkeypatch.setitem(main_mod._serial_platform, "S2", "harmony")
    main_mod._driver_cache.pop("S2", None)

    try:
        main_mod._get_or_open_driver("S2", on_status=lambda *_a: None)
    finally:
        main_mod._driver_cache.pop("S2", None)

    assert "on_status" not in seen["kwargs"]
