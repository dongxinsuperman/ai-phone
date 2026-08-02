"""iOS Simulator 宿主能力探查（Agent 侧）。

对标 :mod:`ai_phone.agent.android_vm.capability`，返回结构完全一致::

    {"ok": bool, "reason": str, "warning": str, "details": {...}}

回答的问题只有一个：**这台 Agent 能不能承接「某机型 × 某系统版本」这台虚拟机。**

职责边界（方案 §6.5.4.1）：机型清单与版本区间属于 **Server 侧官方目录**
（`ios_sim_catalog_snapshots`，由仓库 bundle 的快照导入）；本模块只回答
「这台 Agent 实际装了哪些 runtime、这个组合起不起得来、还剩多少资源」。

硬条件（``ok=False``，对标 Android 把 system image 当硬条件）：

1. 非 macOS 宿主
2. ``xcrun simctl`` 不可用（未装 Xcode / ``xcode-select`` 指向无效目录）
3. 缺 ``xcodebuild``——驱动虚拟机还需要为其编译一次 WDA
4. 未配置 WDA 工程目录——同上
5. 目标 runtime 未安装（等价于 Android 缺 system image）
6. 目标机型不存在
7. 机型与 runtime 组合非法

软提示（``ok=True`` + ``warning``，对标 Android 的内存策略）：内存 / 磁盘偏低
只提醒不拦截。macOS 的 available 统计偏保守（compressed / cached 可回收却不计入），
硬挡会误杀其实能起的机器。

**「机型 × 版本」合法性以 runtime 自带的 ``supportedDeviceTypes`` 为准**，不自己
按版本区间推算——苹果在 ``simctl list runtimes -j`` 里直接给了这张表，比推算权威。
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ai_phone.config import get_settings
# 内存探测直接复用 Android 那份，与鸿蒙的做法一致（harmony_vm/capability.py 也是
# 显式 import android_vm 的同名函数）——同一件事没必要写三遍。
from ai_phone.agent.android_vm.capability import available_memory_mb


_CMD_TIMEOUT = 20.0

# 单台空闲虚拟机的内存开销参考值。来源：本机实测（Apple M4，iPhone 17 Pro /
# iOS 26.0.1），启动前后宿主可用内存差值约 1529 MB，取整 1536。
# 与 Android/鸿蒙不同，虚拟机不是虚拟机、没有可配置的 RAM 分配（方案 §6.5.3），
# 因此这里是「经验开销」而不是「分配额度」。
PER_INSTANCE_MB = 1536


@dataclass
class IosSimTools:
    """本机 iOS 虚拟机工具链。"""

    xcrun: str
    xcodebuild: str
    developer_dir: str = ""
    xcode_version: str = ""


@dataclass
class SimRuntime:
    """一个已安装的 iOS runtime（等价于 Android 的 system image）。"""

    identifier: str
    name: str
    version: str
    build: str
    is_available: bool
    supported_device_types: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identifier": self.identifier,
            "name": self.name,
            "version": self.version,
            "build": self.build,
            "is_available": self.is_available,
            "supported_device_types": list(self.supported_device_types),
        }


@dataclass
class SimDeviceType:
    """一个可用机型及其官方支持的系统版本区间。"""

    identifier: str
    name: str
    product_family: str
    min_runtime_version: int
    max_runtime_version: int
    min_runtime_version_string: str
    max_runtime_version_string: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identifier": self.identifier,
            "name": self.name,
            "product_family": self.product_family,
            "min_runtime_version": self.min_runtime_version,
            "max_runtime_version": self.max_runtime_version,
            "min_runtime_version_string": self.min_runtime_version_string,
            "max_runtime_version_string": self.max_runtime_version_string,
        }


# ---------------------------------------------------------------------------
# 工具链发现
# ---------------------------------------------------------------------------
def _run(cmd: List[str], timeout: float = _CMD_TIMEOUT) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def find_ios_sim_tools() -> Tuple[Optional[IosSimTools], List[str]]:
    """定位工具链，返回 ``(tools, missing)``。

    与 Android 不同，这里**不扫描候选安装目录**：``xcrun`` / ``xcodebuild`` 由
    Xcode Command Line Tools 固定装在 ``/usr/bin``，位置不会漂；真正会出问题的是
    ``xcode-select`` 指向了无效目录，那属于「装了但不可用」，由 simctl 探测暴露。
    """
    if platform.system() != "Darwin":
        return None, ["macOS"]

    missing: List[str] = []
    xcrun = shutil.which("xcrun") or ""
    if not xcrun:
        missing.append("xcrun")
    # xcodebuild 是硬条件：驱动虚拟机需要先为它编译一次 WDA（方案 §4.2）
    xcodebuild = shutil.which("xcodebuild") or ""
    rc, developer_dir, _ = _run(["xcode-select", "-p"], timeout=10.0)
    if rc == 0 and developer_dir:
        candidate = Path(developer_dir) / "usr" / "bin" / "xcodebuild"
        if candidate.exists():
            xcodebuild = str(candidate)
    else:
        developer_dir = ""
    if not xcodebuild:
        missing.append("xcodebuild")

    if missing:
        return None, missing

    xcode_version = ""
    rc, out, _ = _run([xcodebuild, "-version"], timeout=30.0)
    if rc == 0 and out:
        xcode_version = out.splitlines()[0].strip()

    return IosSimTools(
        xcrun=xcrun,
        xcodebuild=xcodebuild,
        developer_dir=developer_dir,
        xcode_version=xcode_version,
    ), []


# ---------------------------------------------------------------------------
# runtime / devicetype 清单
# ---------------------------------------------------------------------------
_IOS_RUNTIME_PREFIX = "com.apple.CoreSimulator.SimRuntime.iOS-"


def list_installed_runtimes(xcrun: str = "") -> List[SimRuntime]:
    """列出已安装的 **iOS** runtime。失败返回空列表，不抛异常。

    watchOS / tvOS / xrOS runtime 一律过滤——它们不属于本项目的设备语义。
    """
    binary = xcrun or shutil.which("xcrun") or "xcrun"
    rc, out, err = _run([binary, "simctl", "list", "runtimes", "-j"])
    if rc != 0 or not out:
        logger.debug("simctl list runtimes 失败 rc={} err={}", rc, err[:200])
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        logger.debug("simctl list runtimes 输出非法 JSON：{}", exc)
        return []

    result: List[SimRuntime] = []
    for item in payload.get("runtimes") or []:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("identifier") or "")
        if not identifier.startswith(_IOS_RUNTIME_PREFIX):
            continue
        supported = [
            str(dt.get("identifier") or "")
            for dt in (item.get("supportedDeviceTypes") or [])
            if isinstance(dt, dict) and dt.get("identifier")
        ]
        result.append(
            SimRuntime(
                identifier=identifier,
                name=str(item.get("name") or ""),
                version=str(item.get("version") or ""),
                build=str(item.get("buildversion") or ""),
                is_available=bool(item.get("isAvailable", False)),
                supported_device_types=supported,
            )
        )
    return result


def list_device_types(xcrun: str = "") -> List[SimDeviceType]:
    """列出本机 Xcode 自带的 iPhone / iPad 机型及其官方版本区间。

    这些机型随 Xcode 安装，无需单独下载（方案 §1.8.2）。Server 侧目录存的是同一
    份数据的快照；这里上报是为了让 Server 能察觉「Agent 的 Xcode 版本与目录快照
    不一致」。
    """
    binary = xcrun or shutil.which("xcrun") or "xcrun"
    rc, out, err = _run([binary, "simctl", "list", "devicetypes", "-j"])
    if rc != 0 or not out:
        logger.debug("simctl list devicetypes 失败 rc={} err={}", rc, err[:200])
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return []

    result: List[SimDeviceType] = []
    for item in payload.get("devicetypes") or []:
        if not isinstance(item, dict):
            continue
        family = str(item.get("productFamily") or "")
        if family not in ("iPhone", "iPad"):
            continue
        identifier = str(item.get("identifier") or "")
        if not identifier:
            continue
        result.append(
            SimDeviceType(
                identifier=identifier,
                name=str(item.get("name") or ""),
                product_family=family,
                min_runtime_version=int(item.get("minRuntimeVersion") or 0),
                max_runtime_version=int(item.get("maxRuntimeVersion") or 0),
                min_runtime_version_string=str(item.get("minRuntimeVersionString") or ""),
                max_runtime_version_string=str(item.get("maxRuntimeVersionString") or ""),
            )
        )
    return result


def decode_runtime_version(value: int) -> str:
    """苹果的版本整数 → 可读串。编码为 ``major<<16 | minor<<8 | patch``。

    实测校验：``1703936 → 26.0.0``、``1050880 → 16.9.0``、``720896 → 11.0.0``，
    与官方给的 ``*VersionString`` 逐一吻合。
    """
    v = int(value)
    return f"{v >> 16}.{(v >> 8) & 0xFF}.{v & 0xFF}"


# ---------------------------------------------------------------------------
# 磁盘
# ---------------------------------------------------------------------------
def _devices_root() -> Path:
    return Path.home() / "Library" / "Developer" / "CoreSimulator" / "Devices"


def available_disk_mb() -> Optional[int]:
    """虚拟机实例目录所在卷的剩余空间（MB）。探测不到返回 None。"""
    try:
        root = _devices_root()
        probe = root if root.exists() else Path.home()
        usage = shutil.disk_usage(str(probe))
        return int(usage.free / (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 探查主入口
# ---------------------------------------------------------------------------
def probe_ios_sim_capability(
    requirement: Dict[str, Any],
    *,
    current_instances: int,
    max_instances: int,
) -> Dict[str, Any]:
    """探查本机能否承接 ``requirement`` 描述的虚拟机。

    Args:
        requirement: 至少含 ``device_type``（机型 identifier）与 ``runtime``
            （runtime identifier，或 ``26.0`` 这种版本串）。
        current_instances: 本 Agent 当前已受管的虚拟机数量。
        max_instances: 参考上限（软提示，不拦截，与 Android 一致）。

    Returns:
        ``{"ok", "reason", "warning", "details"}``，结构与
        :func:`~ai_phone.agent.android_vm.capability.probe_android_vm_capability`
        一致，Server 侧可用同一套渲染逻辑。
    """
    requested_device_type = str(requirement.get("device_type") or "").strip()
    requested_runtime = str(requirement.get("runtime") or "").strip()

    details: Dict[str, Any] = {
        "host_os": platform.system(),
        "host_machine": platform.machine(),
        "requested_device_type": requested_device_type,
        "requested_runtime": requested_runtime,
        "current_instances": current_instances,
        "max_instances": max_instances,
        "per_instance_mb": PER_INSTANCE_MB,
    }

    # —— 硬条件 1/2/3：宿主与工具链 ——
    tools, missing = find_ios_sim_tools()
    if missing:
        details["missing_tools"] = missing
        if missing == ["macOS"]:
            reason = "iOS 虚拟机只能跑在 macOS 上"
        else:
            reason = (
                f"缺少工具：{', '.join(missing)}。"
                "请安装 Xcode 并执行 "
                "`sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`"
            )
        return {"ok": False, "reason": reason, "warning": "", "details": details}
    assert tools is not None
    details["tools"] = {
        "xcrun": tools.xcrun,
        "xcodebuild": tools.xcodebuild,
        "developer_dir": tools.developer_dir,
        "xcode_version": tools.xcode_version,
    }

    # —— 硬条件 4：WDA 工程 ——
    # 光能开机没用：没有 WDA 就没法截图和点击，这台设备进了池子也是废的。
    # 对齐 Android 把「镜像」当硬条件的思路——这是本平台的等价前置。
    settings = get_settings()
    wda_dir = settings.wda_project_dir
    wda_ok = bool(wda_dir) and (Path(wda_dir) / "WebDriverAgent.xcodeproj").is_dir()
    details["wda_project_dir"] = str(wda_dir or "")
    if not wda_ok:
        return {
            "ok": False,
            "reason": (
                "未配置可用的 WebDriverAgent 工程目录（AI_PHONE_WDA_PROJECT_DIR）。"
                "虚拟机起来后需要 WDA 才能截图和操作；仓库已 vendored 在 "
                "third_party/WebDriverAgent"
            ),
            "warning": "",
            "details": details,
        }

    # —— 硬条件 5：runtime（等价 Android 的 system image）——
    runtimes = list_installed_runtimes(tools.xcrun)
    details["installed_runtimes"] = [r.to_dict() for r in runtimes]
    usable = [r for r in runtimes if r.is_available]
    if not usable:
        return {
            "ok": False,
            "reason": (
                "本机没有已安装的 iOS runtime。请在 Xcode → Settings → Components "
                "下载一个 iOS 虚拟机运行时"
            ),
            "warning": "",
            "details": details,
        }

    matched = _match_runtime(usable, requested_runtime)
    if requested_runtime and matched is None:
        return {
            "ok": False,
            "reason": (
                f"缺少 iOS runtime：{requested_runtime}（本机已装："
                f"{', '.join(r.name for r in usable) or '无'}）"
            ),
            "warning": "",
            "details": details,
        }
    if matched is not None:
        details["matched_runtime"] = matched.to_dict()

    # —— 硬条件 6/7：机型存在性与组合合法性 ——
    device_types = list_device_types(tools.xcrun)
    details["device_type_count"] = len(device_types)
    if requested_device_type:
        known = {dt.identifier: dt for dt in device_types}
        dt = known.get(requested_device_type)
        if dt is None:
            return {
                "ok": False,
                "reason": (
                    f"本机 Xcode 不认识该机型：{requested_device_type}"
                    f"（Xcode 版本 {tools.xcode_version or '未知'}，"
                    f"共 {len(device_types)} 个可用机型）"
                ),
                "warning": "",
                "details": details,
            }
        details["matched_device_type"] = dt.to_dict()
        # 组合合法性以 runtime 自带的 supportedDeviceTypes 为准（官方直给，
        # 比按 min/max 区间推算权威）
        if matched is not None and requested_device_type not in matched.supported_device_types:
            return {
                "ok": False,
                "reason": (
                    f"机型「{dt.name}」不支持 {matched.name}"
                    f"（该机型官方支持区间 "
                    f"{dt.min_runtime_version_string} ~ "
                    f"{dt.max_runtime_version_string or '无上限'}）"
                ),
                "warning": "",
                "details": details,
            }

    # —— 软提示：内存与磁盘（只提醒，不拦截）——
    warnings: List[str] = []
    avail_mb = available_memory_mb()
    details["available_memory_mb"] = avail_mb
    min_free_mb = int(getattr(settings, "ios_sim_min_free_mb", 2048))
    details["min_free_mb"] = min_free_mb
    if avail_mb is not None and avail_mb < PER_INSTANCE_MB + min_free_mb:
        warnings.append(
            f"当前 Agent 已受管 {current_instances} 台虚拟机，可用内存约 {avail_mb}MB 偏低。"
            f"一台空闲虚拟机实测约占 {PER_INSTANCE_MB}MB，建议预留 "
            f"{PER_INSTANCE_MB + min_free_mb}MB。"
        )

    disk_mb = available_disk_mb()
    details["available_disk_mb"] = disk_mb
    if disk_mb is not None and disk_mb < 5120:
        warnings.append(
            f"虚拟机实例目录所在卷剩余约 {disk_mb}MB 偏低；实例数据会随使用增长。"
        )

    if max_instances > 0 and current_instances >= max_instances:
        warnings.append(
            f"已达参考上限（{current_instances}/{max_instances} 台）。"
            "该上限仅作提示，不阻止下发。"
        )

    warning = " ".join(warnings)
    details["warning"] = warning
    return {
        "ok": True,
        "reason": warning or "可用",
        "warning": warning,
        "details": details,
    }


def _match_runtime(runtimes: List[SimRuntime], requested: str) -> Optional[SimRuntime]:
    """按 identifier 精确匹配，退而按版本串前缀匹配。

    允许 Server 下发 ``26.0`` 这种人类可读值，而不强制写全 identifier；
    但 identifier 优先，避免 ``26.0`` 同时匹上 ``26.0.1`` 与 ``26.0.2`` 时选错。
    """
    if not requested:
        return None
    for rt in runtimes:
        if rt.identifier == requested:
            return rt
    want = requested.strip().lower()
    for rt in runtimes:
        if rt.version.lower() == want or rt.name.lower() == want:
            return rt
    for rt in runtimes:
        if rt.version.lower().startswith(want + ".") or rt.name.lower().startswith(want):
            return rt
    return None
