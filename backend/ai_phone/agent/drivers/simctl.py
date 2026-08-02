"""iOS Simulator 连接底座：``xcrun simctl`` CLI 薄封装。

类比：

- Android 的 ``adbutils.adb`` 封装 ``adb`` 二进制
- HarmonyOS 的 :mod:`ai_phone.agent.drivers.hdc` 封装 ``hdc`` 二进制
- 本模块封装 ``xcrun simctl``

和 iOS **真机**底座（``pymobiledevice3`` + usbmux）没有任何关系，也不共享任何
状态。真机走 USB / lockdown，虚拟机跑在宿主本机上，两条路径在本项目里刻意
保持完全独立——详见
``docs-internal/ios-simulator-plan（iOS虚拟机独立接入方案）.md`` §3。

设计约束（对应方案铁律「绝不影响现有流程」）：

1. **零项目内依赖**：除 ``loguru`` 外不 import 任何 ai_phone 模块，不读全局
   配置，不碰任何既有全局状态。
2. **只读**：本模块**不提供** ``boot`` / ``shutdown`` / ``create`` / ``delete``
   等生命周期命令。当前阶段 ai-phone 只做只读发现，不纳管用户手工启动的
   虚拟机，避免误关别人正在用的实例。生命周期能力留到方案 M3 阶段再加。
3. **fail-closed 且绝不上抛**：:func:`list_simulators` 内部吞掉所有异常并返回
   空列表。它会被挂进 ``list_all_devices()`` 这条三端公用的扫描链路，一旦抛
   异常或卡住就会连带拖垮 Android / iOS 真机 / HarmonyOS 的设备发现。
4. **每次调用都带超时**：理由同上。

环境前提：仅 macOS 且已安装 Xcode（``xcode-select -p`` 指向有效 Developer
目录）。非 macOS 宿主上 :func:`simctl_available` 恒为 ``False``，上层直接跳过。
"""
from __future__ import annotations

import json
import platform as _platform
import plistlib
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


# simctl 本机实测单次调用约 60ms（Apple M4）。10s 超时是给「Xcode 首次启动
# CoreSimulatorService」这类冷启动留的余量，正常路径远远用不到。
_DEFAULT_TIMEOUT = 10.0

# runtime 列表只在装/删 Xcode runtime 时才变，属于分钟级都嫌频繁的静态数据。
# 设备扫描默认 5s 一轮，缓存掉可以把每轮的 subprocess 调用从 2 次降到 1 次。
_RUNTIME_CACHE_TTL = 60.0

# 只认 iOS runtime。watchOS / tvOS / xrOS 的虚拟机也会出现在同一份 JSON 里，
# 它们不属于本项目的设备语义，必须在底座这一层就滤掉。
_IOS_RUNTIME_PREFIX = "com.apple.CoreSimulator.SimRuntime.iOS-"

_XCRUN_BIN: Optional[str] = None
_XCRUN_PROBED = False
_XCRUN_USABLE = False
_PROBE_LOCK = threading.Lock()

_runtime_cache: Optional[Dict[str, Dict[str, Any]]] = None
_runtime_cache_ts: float = 0.0
_runtime_cache_lock = threading.Lock()

# 「不可用」这件事每轮扫描都会命中，info 级会刷屏。只在状态翻转时打一次。
_unavailable_logged = False


class SimctlError(RuntimeError):
    """``xcrun simctl`` 执行失败时抛出。保留原始 stdout / stderr 便于排障。"""

    def __init__(self, cmd: List[str], returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"simctl 执行失败 cmd={cmd!r} rc={returncode} stderr={stderr.strip()!r}"
        )


@dataclass(frozen=True)
class SimulatorDevice:
    """``xcrun simctl list devices -j`` 解析出的一台虚拟机记录。

    字段直接对应 simctl JSON，不做业务加工——转成项目的 ``DeviceInfo`` 是上层
    发现函数的事，本模块只负责如实反映 simctl 说了什么。
    """

    udid: str
    name: str
    state: str  # "Booted" | "Shutdown" | "Booting" | "Shutting Down" | ...
    runtime_id: str  # com.apple.CoreSimulator.SimRuntime.iOS-26-0
    runtime_name: str  # "iOS 26.0"；runtime 已卸载时回退成 runtime_id 的可读形式
    runtime_version: str  # "26.0.1"；查不到时为空串
    device_type_id: str  # com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro
    is_available: bool

    @property
    def is_booted(self) -> bool:
        return self.state.strip().lower() == "booted"


def _resolve_xcrun() -> Optional[str]:
    """返回 ``xcrun`` 路径；非 macOS 或找不到时返回 None。

    不做 hdc 那样的「扫默认安装路径 + 打 PATH 补丁」：``xcrun`` 由 Xcode
    Command Line Tools 装到 ``/usr/bin/xcrun``，位置固定且必在 PATH 中，
    找不到就是真的没装，没有需要兜底的场景。
    """
    global _XCRUN_BIN
    if _XCRUN_BIN is not None:
        return _XCRUN_BIN
    if _platform.system() != "Darwin":
        return None
    hit = shutil.which("xcrun")
    if hit:
        _XCRUN_BIN = hit
    return hit


def simctl_available() -> bool:
    """``xcrun simctl`` 是否真的能用。结果缓存，不可用时也不抛异常。

    只判断 ``xcrun`` 存在是不够的：Mac 上 ``/usr/bin/xcrun`` 由 Command Line
    Tools 提供，但没装完整 Xcode 或 ``xcode-select`` 指向无效目录时，
    ``xcrun simctl`` 会直接失败。所以这里跑一次真实探测并缓存结果。
    """
    global _XCRUN_PROBED, _XCRUN_USABLE, _unavailable_logged
    if _XCRUN_PROBED:
        return _XCRUN_USABLE
    with _PROBE_LOCK:
        if _XCRUN_PROBED:
            return _XCRUN_USABLE
        usable = False
        reason = ""
        xcrun = _resolve_xcrun()
        if xcrun is None:
            reason = (
                "非 macOS 宿主"
                if _platform.system() != "Darwin"
                else "PATH 里找不到 xcrun（未安装 Xcode Command Line Tools）"
            )
        else:
            try:
                proc = subprocess.run(
                    [xcrun, "simctl", "help"],
                    capture_output=True,
                    text=True,
                    timeout=_DEFAULT_TIMEOUT,
                    encoding="utf-8",
                    errors="replace",
                )
                usable = proc.returncode == 0
                if not usable:
                    reason = (proc.stderr or proc.stdout or "").strip()[:200]
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"

        _XCRUN_USABLE = usable
        _XCRUN_PROBED = True
        if usable:
            logger.info("iOS Simulator 底座可用：{}", xcrun)
        elif not _unavailable_logged:
            _unavailable_logged = True
            logger.debug("iOS Simulator 底座不可用，跳过虚拟机扫描：{}", reason)
        return _XCRUN_USABLE


def reset_probe_cache_for_tests() -> None:
    """清空探测与各类缓存。仅供测试使用。"""
    global _XCRUN_BIN, _XCRUN_PROBED, _XCRUN_USABLE, _unavailable_logged
    global _runtime_cache, _runtime_cache_ts
    global _devicetype_cache, _devicetype_cache_ts
    global _screen_cache, _screen_cache_ts
    _XCRUN_BIN = None
    _XCRUN_PROBED = False
    _XCRUN_USABLE = False
    _unavailable_logged = False
    with _runtime_cache_lock:
        _runtime_cache = None
        _runtime_cache_ts = 0.0
    with _devicetype_cache_lock:
        _devicetype_cache = None
        _devicetype_cache_ts = 0.0
    with _screen_cache_lock:
        _screen_cache = None
        _screen_cache_ts = 0.0


def simctl_run(
    *args: str,
    timeout: float = _DEFAULT_TIMEOUT,
    check: bool = True,
) -> str:
    """执行 ``xcrun simctl <args...>`` 并返回 stdout（已 strip）。

    Args:
        args: 跟在 ``simctl`` 后的参数序列。例：``simctl_run("list", "devices", "-j")``
        timeout: 秒。超时抛 :class:`SimctlError`（rc=-1）。
        check: True 时非零返回码抛 :class:`SimctlError`。

    Raises:
        SimctlError: 非零返回码 / 超时 / xcrun 不存在
    """
    xcrun = _resolve_xcrun()
    if xcrun is None:
        raise SimctlError(
            ["xcrun"], -2, "",
            "xcrun 找不到。iOS Simulator 需要 macOS + Xcode；"
            "请确认 `xcode-select -p` 指向有效的 Developer 目录",
        )
    cmd: List[str] = [xcrun, "simctl", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise SimctlError(cmd, -1, "", f"timeout after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise SimctlError(cmd, -2, "", f"binary not found: {exc}") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if check and proc.returncode != 0:
        raise SimctlError(cmd, proc.returncode, stdout, stderr)
    return stdout


def _fetch_runtimes() -> Dict[str, Dict[str, Any]]:
    """拉一次 ``simctl list runtimes -j``，返回 ``{identifier: runtime_dict}``。"""
    raw = simctl_run("list", "runtimes", "-j")
    data = json.loads(raw) if raw else {}
    out: Dict[str, Dict[str, Any]] = {}
    for rt in data.get("runtimes") or []:
        ident = str(rt.get("identifier") or "").strip()
        if ident:
            out[ident] = rt
    return out


def _get_runtimes(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """带 TTL 缓存的 runtime 表。失败时返回上一次缓存（没有则空表）。

    runtime 信息只用于给设备补上可读版本号，缺失不影响设备本身能否被发现，
    所以这里失败不上抛，退化成「版本号为空」即可。
    """
    global _runtime_cache, _runtime_cache_ts
    now = time.monotonic()
    with _runtime_cache_lock:
        fresh = (
            _runtime_cache is not None
            and (now - _runtime_cache_ts) < _RUNTIME_CACHE_TTL
        )
        if fresh and not force_refresh:
            return _runtime_cache  # type: ignore[return-value]
    try:
        fetched = _fetch_runtimes()
    except Exception as exc:  # noqa: BLE001
        logger.debug("simctl list runtimes 失败，沿用旧缓存：{}", exc)
        with _runtime_cache_lock:
            return dict(_runtime_cache or {})
    with _runtime_cache_lock:
        _runtime_cache = fetched
        _runtime_cache_ts = now
        return fetched


_devicetype_cache: Optional[Dict[str, str]] = None
_devicetype_cache_ts: float = 0.0
_devicetype_cache_lock = threading.Lock()

_screen_cache: Optional[Dict[str, "DeviceTypeScreen"]] = None
_screen_cache_ts: float = 0.0
_screen_cache_lock = threading.Lock()


def device_type_names(force_refresh: bool = False) -> Dict[str, str]:
    """``{deviceTypeIdentifier: 人类可读机型名}``，带 TTL 缓存。

    用途：虚拟机实例的 ``name`` 是**实例名**（受管实例形如
    ``aiphone_sim_<vmid>``），不是机型。上报设备时 ``model`` 必须是真实机型
    （iPhone 16e），否则设备卡片会显示一串内部标识。机型名只能从 devicetypes
    查，所以这里缓存一份映射。

    机型清单只在装/换 Xcode 时才变，缓存 TTL 与 runtime 表一致。失败返回上次
    缓存（没有则空表），退化成沿用实例名，不影响设备发现本身。
    """
    global _devicetype_cache, _devicetype_cache_ts
    now = time.monotonic()
    with _devicetype_cache_lock:
        fresh = (
            _devicetype_cache is not None
            and (now - _devicetype_cache_ts) < _RUNTIME_CACHE_TTL
        )
        if fresh and not force_refresh:
            return _devicetype_cache  # type: ignore[return-value]
    try:
        raw = simctl_run("list", "devicetypes", "-j")
        payload = json.loads(raw) if raw else {}
        mapping = {
            str(dt.get("identifier") or ""): str(dt.get("name") or "")
            for dt in (payload.get("devicetypes") or [])
            if isinstance(dt, dict) and dt.get("identifier")
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("simctl list devicetypes 失败，沿用旧缓存：{}", exc)
        with _devicetype_cache_lock:
            return dict(_devicetype_cache or {})
    with _devicetype_cache_lock:
        _devicetype_cache = mapping
        _devicetype_cache_ts = now
        return mapping


@dataclass(frozen=True)
class DeviceTypeScreen:
    """机型的固定屏幕规格。``width`` / ``height`` 是**竖屏**物理像素。"""

    width: int
    height: int
    scale: int


def device_type_screens(force_refresh: bool = False) -> Dict[str, DeviceTypeScreen]:
    """``{deviceTypeIdentifier: 屏幕规格}``，带 TTL 缓存。

    **为什么需要它**：设备发现阶段必须能报出屏幕尺寸，否则前端拿不到设备像素，
    手动点击会退化成拿镜像画面尺寸当基准，坐标系整体偏移（方案 §1.11）。
    而虚拟机不像真机能从 lockdown 直接读——只能靠 WDA，可发现阶段又不启动 WDA。

    出路是机型自带的 ``profile.plist``：``simctl list devicetypes`` 的每条记录都带
    ``bundlePath``，里面的 ``mainScreenWidth/Height/Scale`` 就是这台机型的固定规格。
    纯本地文件、不需要设备开机、更不需要 WDA——与真机读 lockdown 处在同一层级。

    **报的是竖屏尺寸，不随旋转变**，与真机 lockdown 的 ScreenWidth/Height 同性质。
    旋转由前端按镜像画面方向自动换算宽高（真机已经跑通的那条路），设备记录里不该
    出现「当前方向」这种会变的量。
    """
    global _screen_cache, _screen_cache_ts
    now = time.monotonic()
    with _screen_cache_lock:
        fresh = (
            _screen_cache is not None
            and (now - _screen_cache_ts) < _RUNTIME_CACHE_TTL
        )
        if fresh and not force_refresh:
            return _screen_cache  # type: ignore[return-value]

    mapping: Dict[str, DeviceTypeScreen] = {}
    try:
        raw = simctl_run("list", "devicetypes", "-j")
        payload = json.loads(raw) if raw else {}
        for dt in payload.get("devicetypes") or []:
            if not isinstance(dt, dict):
                continue
            identifier = str(dt.get("identifier") or "")
            bundle = str(dt.get("bundlePath") or "")
            if not identifier or not bundle:
                continue
            screen = _read_profile_screen(Path(bundle))
            if screen is not None:
                mapping[identifier] = screen
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取机型屏幕规格失败，沿用旧缓存：{}", exc)
        with _screen_cache_lock:
            return dict(_screen_cache or {})

    with _screen_cache_lock:
        _screen_cache = mapping
        _screen_cache_ts = now
        return mapping


def _read_profile_screen(bundle_path: Path) -> Optional[DeviceTypeScreen]:
    """从机型 bundle 的 ``profile.plist`` 读屏幕规格。任何异常都返回 None。"""
    plist = bundle_path / "Contents" / "Resources" / "profile.plist"
    try:
        payload = plistlib.loads(plist.read_bytes())
    except Exception:  # noqa: BLE001
        return None
    try:
        width = int(payload.get("mainScreenWidth") or 0)
        height = int(payload.get("mainScreenHeight") or 0)
        scale = int(payload.get("mainScreenScale") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    # profile.plist 给的就是竖屏方向，但不同 Xcode 版本里个别机型（尤其 iPad）
    # 有写反的先例。统一归一化，保证「短边是宽」这条不变量成立。
    if width > height:
        width, height = height, width
    return DeviceTypeScreen(width=width, height=height, scale=max(1, scale))


def _readable_runtime_name(runtime_id: str) -> str:
    """``...SimRuntime.iOS-26-0`` → ``iOS 26.0``。runtime 已卸载时的兜底可读名。"""
    tail = runtime_id.rsplit(".", 1)[-1]  # iOS-26-0
    parts = tail.split("-")
    if len(parts) >= 2:
        return f"{parts[0]} {'.'.join(parts[1:])}"
    return tail or runtime_id


def parse_devices_json(
    devices_payload: Dict[str, Any],
    runtimes: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[SimulatorDevice]:
    """把 ``simctl list devices -j`` 的 JSON 解析成 :class:`SimulatorDevice` 列表。

    拆成独立函数是为了让解析逻辑可以脱离 subprocess 单测。只保留 iOS runtime，
    watchOS / tvOS / xrOS 一律丢弃。
    """
    runtimes = runtimes or {}
    out: List[SimulatorDevice] = []
    for runtime_id, entries in (devices_payload.get("devices") or {}).items():
        rid = str(runtime_id)
        if not rid.startswith(_IOS_RUNTIME_PREFIX):
            continue
        rt = runtimes.get(rid) or {}
        runtime_name = str(rt.get("name") or "").strip() or _readable_runtime_name(rid)
        runtime_version = str(rt.get("version") or "").strip()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            udid = str(entry.get("udid") or "").strip()
            if not udid:
                continue
            out.append(
                SimulatorDevice(
                    udid=udid,
                    name=str(entry.get("name") or "").strip(),
                    state=str(entry.get("state") or "").strip(),
                    runtime_id=rid,
                    runtime_name=runtime_name,
                    runtime_version=runtime_version,
                    device_type_id=str(entry.get("deviceTypeIdentifier") or "").strip(),
                    is_available=bool(entry.get("isAvailable", False)),
                )
            )
    return out


def list_simulators(booted_only: bool = False) -> List[SimulatorDevice]:
    """列出本机的 iOS 虚拟机。**任何失败都返回空列表，绝不抛异常。**

    这是本模块唯一会被三端公用扫描链路调用的入口，fail-closed 是硬要求：
    见模块 docstring 约束 3。

    Args:
        booted_only: 只返回 ``state == "Booted"`` 的实例。

    Returns:
        iOS 虚拟机列表；``simctl`` 不可用、超时、JSON 变形等一律返回 ``[]``。
    """
    if not simctl_available():
        return []
    try:
        raw = simctl_run("list", "devices", "-j")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            logger.debug("simctl list devices 返回了非对象 JSON，忽略本轮")
            return []
        devices = parse_devices_json(payload, _get_runtimes())
    except SimctlError as exc:
        logger.debug("simctl 扫描失败（本轮按无虚拟机处理）：{}", exc)
        return []
    except json.JSONDecodeError as exc:
        logger.debug("simctl list devices 输出不是合法 JSON（本轮忽略）：{}", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("simctl 扫描出现未预期错误（本轮按无虚拟机处理）：{}", exc)
        return []

    if booted_only:
        devices = [d for d in devices if d.is_booted]
    return devices
