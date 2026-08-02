"""iOS Simulator 设备发现。

与 :mod:`ai_phone.agent.drivers.ios`（iOS **真机**）完全独立，不共享任何函数、
缓存或全局状态。真机走 usbmux / lockdown，虚拟机跑在宿主本机上，两条路径刻意
保持隔离——详见
``docs-internal/ios-simulator-plan（iOS虚拟机独立接入方案）.md`` §3。

本模块当前只有发现能力。``IosSimulatorDriver`` / ``open_ios_simulator_driver``
会在方案 M2 阶段补进本文件，届时结构与 ``android.py`` / ``harmony.py`` 一致
（一个平台一个模块，内含 list + Driver + open）。

实现口径按方案 §0.3：**一等对照物是 Android 虚拟机，不是鸿蒙。** 具体地——

- ``extra`` 字段照抄 ``android_vm/manager.py`` 的 ``decorate_devices``，不自创命名
- 只上报 ``Booted`` 实例。Android 侧未启动的 AVD 根本不会出现在 ``adb devices``
  里，虚拟机同理：``Shutdown`` 的只是一份机型模板，不是一台可用设备
- ``include_offline`` 语义与 ``list_android_devices`` 对齐
- **只上报本平台自己启动的实例**，用户手工在 Xcode 里开的一律不进设备池。这条
  对齐 Android VM ``decorate_devices`` 的 ``elif info.serial.startswith("emulator-"):
  continue``——未纳管的虚拟机直接丢弃，不给它伪装成一台可调度设备的机会。
  鸿蒙当年保留了手工实例，那是历史包袱，iOS 侧不沿用。
"""
from __future__ import annotations

import threading
from typing import List, Set

from loguru import logger

from .base import DeviceInfo
from .simctl import (
    SimulatorDevice,
    device_type_names,
    device_type_screens,
    list_simulators,
)


# 内部隔离用的平台标识。**对外展示必须折叠回 "iOS"**——ai-phone 永远只有
# Android / iOS / HarmonyOS 三端，虚拟机是 iOS 端下的一种设备形态，不是第四端。
# 详见方案 §6.1.1。之所以在 Agent 内部单独取值，是为了让虚拟机绕开 iOS 真机
# 那三个按 platform == "ios" 过滤的 USB 专用钩子（§3.1）。
PLATFORM_IOS_SIM = "ios_sim"

# 产品层面归属的平台，进 extra.vm_platform。对齐鸿蒙 VM 的 vm_platform 维度。
_VM_PLATFORM = "ios"

_BRAND = "Apple"


# ---------------------------------------------------------------------------
# 纳管登记表
# ---------------------------------------------------------------------------
# 只有本平台亲手启动的虚拟机才允许进设备池。对齐 Android VM：
# ``android_vm/manager.py`` 的 decorate_devices 用 ``serial_to_runtime`` 判定，
# 不在表里的 ``emulator-*`` 一律 continue 掉。
#
# 本表当前**恒为空**——生命周期能力要到方案 M3 才有，在那之前平台一台虚拟机都
# 启动不了，因此设备池里也不会出现任何虚拟机。这是刻意的：宁可什么都不显示，
# 也不把同事自己在 Xcode 里开的虚拟机拉进来。
# M3 的 IosSimulatorManager 会在 boot 成功 / 重连认领时调 mark_managed。
_managed_udids: Set[str] = set()
_managed_lock = threading.Lock()


def mark_managed(udid: str) -> None:
    """登记一台由本平台启动的虚拟机。供 M3 的 manager 调用。"""
    if not udid:
        return
    with _managed_lock:
        _managed_udids.add(udid)


def unmark_managed(udid: str) -> None:
    """撤销登记。停止 / 删除实例时调用。"""
    with _managed_lock:
        _managed_udids.discard(udid)


def is_managed(udid: str) -> bool:
    with _managed_lock:
        return udid in _managed_udids


def managed_udids() -> Set[str]:
    with _managed_lock:
        return set(_managed_udids)


def reset_managed_for_tests() -> None:
    with _managed_lock:
        _managed_udids.clear()


def _to_device_info(sim: SimulatorDevice) -> DeviceInfo:
    """``SimulatorDevice`` → ``DeviceInfo``。

    ``screen_width`` / ``screen_height`` 报**竖屏物理像素**，取自机型自带的
    ``profile.plist``（见 :func:`~.simctl.device_type_screens`）。

    这一项**必须在发现阶段就报出来**，不能等驱动打开：前端手动点击是按
    「设备像素」换算坐标的，拿不到就退化成用镜像画面尺寸当基准，而镜像是缩放过的
    （长边 1280），于是点击整体偏向左上、点不准。VLM 那条路不受影响——它用的是
    未缩放的截图，坐标空间自始至终一致。两条路都要准，所以这里必须有值。

    与真机同性质：真机报的是 lockdown 的 ``ScreenWidth/Height``，也是固定竖屏值。
    **不报「当前方向」的尺寸**——那个会随业务转屏变化，存进设备记录就成了错的。
    业务自己转横屏时，由前端按镜像画面方向自动对调宽高（真机已跑通的那条路）。
    """
    screen = device_type_screens().get(sim.device_type_id)
    # model 必须是**真实机型**（iPhone 16e），不能用 sim.name——那是实例名，
    # 受管实例形如 aiphone_sim_<vmid>，直接上报会让设备卡片显示一串内部标识。
    # 机型名从 devicetypes 映射查（带缓存）；查不到才退回实例名。
    model = device_type_names().get(sim.device_type_id) or sim.name
    return DeviceInfo(
        serial=sim.udid,
        platform=PLATFORM_IOS_SIM,
        brand=_BRAND,
        model=model,
        os_version=sim.runtime_version or sim.runtime_name,
        screen_width=screen.width if screen else 0,
        screen_height=screen.height if screen else 0,
        status="online" if sim.is_booted else "offline",
        extra={
            # —— 与 android_vm/manager.py decorate_devices 完全一致的三件套 ——
            "device_kind": "virtual",
            "is_virtual": True,
            "vm_platform": _VM_PLATFORM,
            # 注意：这里**没有** vm_instance_id / vm_name。对齐 Android——
            # 那两个字段由 VM manager 的 decorate_devices 补，不在发现层伪造。
            # 见 android_vm/manager.py decorate_devices。
            # —— 虚拟机特有元信息，供前端与排障使用 ——
            "sim_runtime": sim.runtime_name,
            "sim_runtime_id": sim.runtime_id,
            "sim_device_type": sim.device_type_id,
            "sim_state": sim.state,
            # 实例名单独放这里：受管实例是 aiphone_sim_<vmid>，排障时要看得到
            "sim_instance_name": sim.name,
        },
    )


def list_ios_simulators(
    include_offline: bool = False,
    managed_only: bool = True,
) -> List[DeviceInfo]:
    """扫描本机 iOS 虚拟机，返回 ``DeviceInfo`` 列表。**绝不抛异常。**

    本函数会被挂进 ``list_all_devices()``——那是 Android / iOS 真机 /
    HarmonyOS 三端公用的扫描链路。一旦抛异常或卡住，会连带拖垮其它平台的设备
    发现，因此这里与底座 :func:`~.simctl.list_simulators` 一样 fail-closed。

    Args:
        include_offline: 与 ``list_android_devices`` 同义。``False``（默认）只
            返回已启动（``Booted``）的虚拟机；``True`` 额外把未启动的机型模板
            以 ``status="offline"`` 带出来。
        managed_only: 默认 ``True``——只返回**本平台亲手启动**的实例，用户手工
            在 Xcode 里开的一律丢弃（对齐 Android VM）。设为 ``False`` 可拿到
            宿主上全部虚拟机，仅供 manager 做重连认领、容量探查与本地调试使用，
            **不要用在设备上报路径上**。

    Returns:
        设备列表；simctl 不可用或扫描失败时返回 ``[]``。
    """
    try:
        sims = list_simulators()
    except Exception as exc:  # noqa: BLE001
        # 底座已经 fail-closed，这里是第二道保险：宁可少报设备，也绝不让异常
        # 冒泡到三端公用的扫描链路。
        logger.warning("iOS 虚拟机扫描出现未预期错误（本轮按无设备处理）：{}", exc)
        return []

    allowed = managed_udids() if managed_only else None

    infos: List[DeviceInfo] = []
    for sim in sims:
        # runtime 未安装的机型模板压根启动不了，不是一台设备，任何情况下都不上报
        if not sim.is_available:
            continue
        if not sim.is_booted and not include_offline:
            continue
        # 未纳管的实例直接丢弃：不是平台启动的，就不是平台的设备
        if allowed is not None and sim.udid not in allowed:
            continue
        try:
            infos.append(_to_device_info(sim))
        except Exception as exc:  # noqa: BLE001
            logger.warning("iOS 虚拟机 {} 信息转换失败，跳过：{}", sim.udid, exc)
    return infos
