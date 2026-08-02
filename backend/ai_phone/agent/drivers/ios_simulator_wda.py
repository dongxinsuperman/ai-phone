"""iOS Simulator 上的 WebDriverAgent 启动器。

与 iOS **真机**的 :class:`~.ios_wda_launcher.IosWdaXcodeLauncher` 完全独立，一行
代码都不共享。真机那条链路（常驻 ``xcodebuild test`` + 签名 + preflight 自愈 +
usbmux 端口转发）保持原样不动——这是方案铁律，见
``docs-internal/ios-simulator-plan（iOS虚拟机独立接入方案）.md`` §3。

启动方式（方案 §4.2 第三档，已实测，见 §1.8）：**不跑常驻 ``xcodebuild``**。

```text
① 一次性 build-for-testing        产出 WebDriverAgentRunner-Runner.app，全实例共享
   -destination "generic/platform=iOS Simulator"   通用架构，不绑定具体实例
   CODE_SIGNING_ALLOWED=NO                         虚拟机不需要任何证书
   -derivedDataPath <独立目录>                      §3.3：绝不能用 Xcode 默认目录，
                                                    否则与真机 xcodebuild 争编译锁
② xcrun simctl install <udid> Runner.app          像装普通 App
③ xcrun simctl launch <udid> <runner-bundle-id>   像开普通 App，命令 0.4s 返回
   SIMCTL_CHILD_USE_PORT / MJPEG_SERVER_PORT      §3.2：每实例端口确定性指定
④ 轮询 http://127.0.0.1:<port>/status 到 ready
```

关键实测结论（§1.8）：

- **不需要 WebDriverAgent v13+**。仓库 vendored 的 v12.0.0 直接可用，因此
  ``third_party/WebDriverAgent`` 不必升级，真机链路零影响。
- **runner bundle id 必须动态读**，不能硬编码。本仓库产物是
  ``com.example.wda.xctrunner``（或 ``.env`` 里 ``AI_PHONE_WDA_BUNDLE_ID`` 派生的
  值），不是 Appium 文档里的 ``com.facebook.WebDriverAgentRunner.xctrunner``。
- **虚拟机版必须保留内嵌 ``Frameworks/XC*.framework``**。删除内嵌框架是**真机专属**
  要求，虚拟机上反而需要它们。

fail-closed：任一步失败直接抛错并暴露原因，**不静默回退到 xcodebuild test**
（方案 §4.2 降级路径）。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from loguru import logger

from ...config import get_settings
from .simctl import SimctlError, list_simulators, simctl_run


# ---------------------------------------------------------------------------
# 端口域（方案 §3.2）
# ---------------------------------------------------------------------------
# 必须与 iOS 真机的端口分配完全错开：
#   真机 WDA   drivers/ios.py _alloc_local_port，从 settings.wda_local_port
#              （默认 8100）起逐台递增，无上界 → 实际占用 8100+
#   真机 MJPEG mirror/ios_capture_mjpeg.py _grab_free_local_port，bind(0) 由 OS
#              从 ephemeral 段分配（macOS 49152~65535）
# 所以虚拟机取 8300+ / 9300+：与 8100 段留 100 个端口的缓冲，且远离 ephemeral 段。
# 不做随机兜底——受管实例的端口必须确定可查，否则多实例会串流。
_SIM_WDA_PORT_BASE = 8300
_SIM_WDA_PORT_LIMIT = 8399
_SIM_MJPEG_PORT_BASE = 9300
_SIM_MJPEG_PORT_LIMIT = 9399

_ports_lock = threading.Lock()
_udid_to_ports: Dict[str, Tuple[int, int]] = {}


@dataclass(frozen=True)
class SimulatorPorts:
    wda: int
    mjpeg: int


def _preferred_slot(udid: str) -> int:
    """UDID → 首选端口槽位。同一台设备永远算出同一个值。"""
    span = _SIM_WDA_PORT_LIMIT - _SIM_WDA_PORT_BASE + 1
    digest = hashlib.sha1(udid.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % span


# --------------------------------------------------------------------------
# 端口预留表（落盘）
#
# **光靠哈希定槽位不够**。哈希会撞，撞了就退回线性探测，而探测结果取决于
# *本次进程内的分配顺序*——Agent 一重启映射就重算，顺序一变两台就换号：
#
# ```text
# UDID-11 与 UDID-13 首选槽位都是 61
# 顺序 11→13:  11=8361  13=8362
# 顺序 13→11:  13=8361  11=8362      ← 换人了
# ```
#
# 100 个槽位放 8 台，至少发生一次碰撞的概率约 25%——不是罕见路径。换号之后
# 身份校验会拦住「操作错设备」，但代价是那台起不来，正好打在并发这个目标上。
#
# 所以把映射写盘：端口成为实例的固有属性，与任何顺序无关。生命周期跟着实例走
# ——**停止不归还**（实例和数据都还在，下次还是这台），**删除才归还**。
# --------------------------------------------------------------------------
class SimulatorWdaError(RuntimeError):
    """虚拟机 WDA 构建 / 启动失败。"""


_reservations_cache: Optional[Dict[str, int]] = None


def _reservations_path() -> Path:
    # 与 default_build_dir 同一个落点习惯：agent 本地运行时产物放 storage_dir。
    # 必须绝对化——storage_dir 默认是相对路径 ./data，而调用方的 cwd 不确定。
    storage = Path(get_settings().storage_dir).expanduser().resolve()
    return storage / "ios_sim_ports.json"


def _load_reservations() -> Dict[str, int]:
    """读端口预留表。

    **文件不存在 = 空表**（首次运行的正常情况）；**文件存在但读不动 = 抛错**。

    这两者必须分开。读不动却当空表继续，等于把预留机制悄悄关掉：所有号重新按
    哈希分配，碰撞的那几台就会换号——而持久化本来就是为了根除换号。表一旦损坏，
    受影响的不是一台而是全部，静默降级只会把问题散开到难以归因的地方。

    抛错确实会挡住启动，但那是可操作的：日志里写清楚删掉这个文件即可重建。
    """
    global _reservations_cache
    if _reservations_cache is not None:
        return _reservations_cache
    path = _reservations_path()
    if not path.is_file():
        _reservations_cache = {}
        return _reservations_cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SimulatorWdaError(
            f"iOS 虚拟机端口预留表损坏，无法解析：{path}（{exc}）。"
            "删除该文件后重启 Agent 可重建；但已在跑的实例可能因此换号，"
            "建议先把它们停掉"
        ) from exc
    if not isinstance(raw, dict):
        raise SimulatorWdaError(
            f"iOS 虚拟机端口预留表格式不对（顶层应为对象）：{path}。删除后可重建"
        )
    data: Dict[str, int] = {}
    for udid, port in raw.items():
        try:
            port_i = int(port)
        except (TypeError, ValueError):
            continue
        if _SIM_WDA_PORT_BASE <= port_i <= _SIM_WDA_PORT_LIMIT:
            data[str(udid)] = port_i
    _reservations_cache = data
    return data


def _save_reservations(data: Dict[str, int]) -> None:
    """原子写盘。写不进去就抛错，不静默放过。

    写失败意味着这次分配的号只活在内存里，下次重启会重算——碰撞的那几台就换号。
    调用方（分配端口）此刻还没把设备启动起来，抛错的代价只是这一次启动失败，
    而放过的代价是留下一个要到下次重启才发作、且很难归因的隐患。
    """
    global _reservations_cache
    path = _reservations_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        raise SimulatorWdaError(
            f"写入 iOS 虚拟机端口预留表失败：{path}（{exc}）。"
            "端口预留必须落盘，否则重启后可能与其他实例换号"
        ) from exc
    _reservations_cache = data


def allocate_ports(udid: str) -> SimulatorPorts:
    """为一台虚拟机分配 (WDA, MJPEG) 端口对；同 udid 永远得到同一对。

    **按 UDID 哈希定槽位，不是「取第一个空位」。** 后者会让端口取决于
    *本次进程内的分配顺序*，而那个顺序在 Agent 重启后会变：

    ```text
    启动阶段   谁先被点启动谁先拿号   B→8300  A→8301
    重启认领   按 simctl 列出顺序     A→8300  B→8301   ← 换人了
    ```

    换人的后果不是「端口错了」这么轻——``is_alive()`` 只探「这个端口上有没有
    ready 的 WDA」，A 探 8300 时 B 的 WDA 会应答，于是两台都被判 ready，
    **操作 A 实际驱动了 B**。两台设备 + 一次 Agent 重启就能踩到。

    哈希定槽位让端口成为设备的固有属性，与分配顺序无关，重启后自然对得上。
    槽位冲突时线性探测（同一进程内，仍保证不重复）。
    """
    with _ports_lock:
        hit = _udid_to_ports.get(udid)
        if hit is not None:
            return SimulatorPorts(*hit)

        reservations = _load_reservations()
        reserved = reservations.get(udid)
        if reserved is not None:
            wda = int(reserved)
            mjpeg = _SIM_MJPEG_PORT_BASE + (wda - _SIM_WDA_PORT_BASE)
            _udid_to_ports[udid] = (wda, mjpeg)
            return SimulatorPorts(wda, mjpeg)

        got = _take_free_slot(udid, reservations)
        if got is not None:
            return got

        # 扫不到空位先别急着报错：预留表里可能全是**幽灵**——实例早已不存在，
        # 号却还占着。踩过一次：测试污染写进了真实预留表，100 个槽位全被不存在
        # 的 UDID 占满，报「端口耗尽」而现场只有两台虚拟机，完全对不上号。
        # 清掉幽灵再试一次，比让人去手工删文件强得多。
        pruned = _prune_ghost_reservations(reservations, keep=udid)
        if pruned:
            logger.warning(
                "iOS 虚拟机端口域扫不到空位，已清理 {} 条幽灵预留（实例已不存在）后重试",
                pruned,
            )
            got = _take_free_slot(udid, reservations)
            if got is not None:
                return got
    raise RuntimeError(
        f"iOS 虚拟机端口域已耗尽（{_SIM_WDA_PORT_BASE}~{_SIM_WDA_PORT_LIMIT}）；"
        "同时在跑的虚拟机过多，或存在未释放的端口泄漏。"
        "注意端口预留跟随实例生命周期：只有删除实例才归还，停止不归还"
    )


def _take_free_slot(
    udid: str, reservations: Dict[str, int]
) -> Optional[SimulatorPorts]:
    """从首选槽位起线性探测一个空位并落盘。找不到返回 ``None``。

    调用方必须已持有 ``_ports_lock``。
    """
    span = _SIM_WDA_PORT_LIMIT - _SIM_WDA_PORT_BASE + 1
    # 已占 = 本进程已发出的 ∪ 盘上已预留的（别人的）。少算后者，碰撞时就会
    # 把别人常驻的号发出去。
    used_wda = {p[0] for p in _udid_to_ports.values()}
    used_wda |= {int(p) for owner, p in reservations.items() if owner != udid}
    start = _preferred_slot(udid)
    for step in range(span):
        wda = _SIM_WDA_PORT_BASE + (start + step) % span
        if wda in used_wda:
            continue
        mjpeg = _SIM_MJPEG_PORT_BASE + (wda - _SIM_WDA_PORT_BASE)
        _udid_to_ports[udid] = (wda, mjpeg)
        reservations[udid] = wda
        try:
            _save_reservations(reservations)
        except Exception:
            # 落盘失败就当这次没分配过：留着内存绑定会让本进程用一个盘上
            # 不存在的号，重启后必然对不上，反而更难查
            _udid_to_ports.pop(udid, None)
            reservations.pop(udid, None)
            raise
        return SimulatorPorts(wda, mjpeg)
    return None


def _prune_ghost_reservations(reservations: Dict[str, int], *, keep: str) -> int:
    """删掉「实例已经不存在」的预留，返回清理条数。

    以 ``simctl`` 的实例清单为准。查不到清单时**一条都不删**——宁可继续报端口
    耗尽，也不能因为一次查询失败就把在跑设备的号收回去，那会让它们下次启动换号。
    """
    try:
        alive = {dev.udid for dev in list_simulators()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理幽灵端口预留时查询实例清单失败，跳过清理：{}", exc)
        return 0
    ghosts = [u for u in reservations if u != keep and u not in alive]
    if not ghosts:
        return 0
    for u in ghosts:
        reservations.pop(u, None)
        _udid_to_ports.pop(u, None)
    try:
        _save_reservations(reservations)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理幽灵端口预留后落盘失败（本次仍按内存生效）：{}", exc)
    return len(ghosts)


def release_ports(udid: str) -> None:
    """释放**进程内**绑定，保留盘上的预留。

    对应「停止」：虚拟机实例和数据都还在，下次启动还是同一台设备，端口理应
    还是同一个。预留留着，重启后 :func:`allocate_ports` 原样取回。
    """
    with _ports_lock:
        _udid_to_ports.pop(udid, None)


def drop_port_reservation(udid: str) -> None:
    """连盘上的预留一起归还，端口回池。

    对应「删除」：实例本身不存在了，再占着号只会挤占端口域。
    """
    with _ports_lock:
        _udid_to_ports.pop(udid, None)
        reservations = _load_reservations()
        if reservations.pop(udid, None) is not None:
            _save_reservations(reservations)


def peek_ports(udid: str) -> Optional[SimulatorPorts]:
    with _ports_lock:
        hit = _udid_to_ports.get(udid)
        return SimulatorPorts(*hit) if hit else None


def reset_ports_for_tests() -> None:
    global _reservations_cache
    with _ports_lock:
        _udid_to_ports.clear()
        _reservations_cache = None


# ---------------------------------------------------------------------------
# WDA 产物构建（全实例共享一份）
# ---------------------------------------------------------------------------
_RUNNER_APP_NAME = "WebDriverAgentRunner-Runner.app"
_build_lock = threading.Lock()


def _find_xcodebuild() -> Optional[str]:
    """定位 ``xcodebuild``。优先 ``xcode-select -p`` 指向的 Xcode。

    与真机 launcher 里的同名函数逻辑一致但**不复用**——两条链路刻意不互相 import，
    避免将来改动一边影响另一边。
    """
    try:
        proc = subprocess.run(
            ["xcode-select", "-p"], capture_output=True, text=True, timeout=10
        )
        developer_dir = (proc.stdout or "").strip()
        if developer_dir:
            candidate = Path(developer_dir) / "usr" / "bin" / "xcodebuild"
            if candidate.exists():
                return str(candidate)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("xcodebuild")


def default_build_dir() -> Path:
    """WDA 虚拟机产物的 derivedDataPath。

    **必须是独立目录**（方案 §3.3）：真机 launcher 不指定 ``-derivedDataPath``，
    用的是 Xcode 默认位置；同一个工程的虚拟机构建默认也会落进同一个目录，两个
    xcodebuild 并发时会争编译锁与 ModuleCache。

    落点沿用项目惯例——agent 本地运行时产物放 ``storage_dir`` 下，与鸿蒙的
    ``storage_dir / "harmony_vm_runtime"`` 同构。不用 ``/tmp``：重启即丢，
    每次都要重编 24 秒。

    **必须返回绝对路径。** ``storage_dir`` 默认是相对路径 ``./data``，而
    ``xcodebuild`` 是以 WDA 工程目录为 cwd 执行的——直接把相对路径传给
    ``-derivedDataPath`` 会让产物落进 ``third_party/WebDriverAgent/`` 里面，
    污染 vendored 源码目录且后续找不到产物。
    """
    return _abs_build_dir(Path(get_settings().storage_dir) / "ios_sim_wda_build")


def _abs_build_dir(path: Path) -> Path:
    """把 build 目录规范成绝对路径。理由见 :func:`default_build_dir`。"""
    return Path(path).expanduser().resolve()


def runner_app_path(build_dir: Optional[Path] = None) -> Path:
    base = _abs_build_dir(build_dir) if build_dir else default_build_dir()
    return base / "Build" / "Products" / "Debug-iphonesimulator" / _RUNNER_APP_NAME


def read_runner_bundle_id(app_path: Path) -> str:
    """从产物 ``Info.plist`` 读 runner 的 bundle id。

    **不得硬编码**：本仓库产物是 ``com.example.wda.xctrunner``（或由
    ``AI_PHONE_WDA_BUNDLE_ID`` 派生），与 Appium 文档里的
    ``com.facebook.WebDriverAgentRunner.xctrunner`` 不同。``simctl launch`` 必须
    拿到准确值，否则报「找不到该 App」。
    """
    plist = app_path / "Info.plist"
    try:
        with plist.open("rb") as fp:
            data = plistlib.load(fp)
    except Exception as exc:  # noqa: BLE001
        raise SimulatorWdaError(f"读取 WDA runner Info.plist 失败：{plist}（{exc}）") from exc
    bundle_id = str(data.get("CFBundleIdentifier") or "").strip()
    if not bundle_id:
        raise SimulatorWdaError(f"WDA runner Info.plist 里没有 CFBundleIdentifier：{plist}")
    return bundle_id


def build_wda_for_simulator(
    *,
    project_dir: Optional[Path] = None,
    scheme: Optional[str] = None,
    bundle_id: Optional[str] = None,
    build_dir: Optional[Path] = None,
    force: bool = False,
    timeout: float = 600.0,
) -> Path:
    """构建（或复用）虚拟机版 WDA 产物，返回 ``Runner.app`` 路径。

    产物**全实例共享**：``-destination "generic/platform=iOS Simulator"`` 产出
    通用架构包，不绑定任何具体虚拟机。实测约 24 秒，之后直接复用。

    Args:
        force: 忽略已有产物强制重编。工程改动后用。
    """
    settings = get_settings()
    raw_proj = project_dir if project_dir else settings.wda_project_dir
    # 注意：``AI_PHONE_WDA_PROJECT_DIR=``（空串）会被 pydantic 解析成 ``Path(".")``，
    # 而 ``Path(".")`` 是 truthy，光靠 ``if not raw_proj`` 抓不住。不把这种情况
    # 归到"未配置"，用户会收到「WebDriverAgent.xcodeproj 不存在于 .」这种莫名其妙
    # 的报错，完全看不出是漏配了环境变量。
    if not raw_proj or str(raw_proj).strip() in ("", "."):
        raise SimulatorWdaError(
            "未配置 WDA 工程目录（AI_PHONE_WDA_PROJECT_DIR）。"
            "仓库已 vendored 在 third_party/WebDriverAgent"
        )
    proj = Path(raw_proj)
    if not (proj / "WebDriverAgent.xcodeproj").is_dir():
        raise SimulatorWdaError(f"WebDriverAgent.xcodeproj 不存在于 {proj}")

    # 绝对化后再交给 xcodebuild：它的 cwd 是 WDA 工程目录，相对路径会解析错
    out_dir = _abs_build_dir(build_dir) if build_dir else default_build_dir()
    app = runner_app_path(out_dir)

    with _build_lock:
        if app.is_dir() and not force:
            logger.debug("复用已有的虚拟机版 WDA 产物：{}", app)
            return app

        xcodebuild = _find_xcodebuild()
        if xcodebuild is None:
            raise SimulatorWdaError(
                "找不到 xcodebuild。请安装 Xcode 并执行 "
                "`sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            xcodebuild,
            "build-for-testing",
            "-project",
            "WebDriverAgent.xcodeproj",
            "-scheme",
            scheme or settings.wda_scheme,
            # 通用架构：一次构建供所有虚拟机实例使用，不绑定具体 udid
            "-destination",
            "generic/platform=iOS Simulator",
            "-derivedDataPath",
            str(out_dir),
            # 虚拟机不需要任何证书；显式关掉签名，顺带绕开 device-only 描述文件
            "CODE_SIGNING_ALLOWED=NO",
            "COMPILER_INDEX_STORE_ENABLE=NO",
        ]
        # 沿用真机同一个 bundle id（方案 §1.9）。两边装在不同「设备」里，
        # 命名空间互相独立，同名不冲突，维护面也不扩大。
        effective_bundle_id = bundle_id or settings.wda_bundle_id
        if effective_bundle_id:
            cmd.append(f"PRODUCT_BUNDLE_IDENTIFIER={effective_bundle_id}")

        logger.info("构建虚拟机版 WDA：cwd={} cmd={}", proj, " ".join(cmd))
        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(proj),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise SimulatorWdaError(f"构建虚拟机版 WDA 超时（{timeout}s）") from exc

        if proc.returncode != 0:
            tail = "\n".join((proc.stdout or "").splitlines()[-30:])
            raise SimulatorWdaError(
                f"构建虚拟机版 WDA 失败 rc={proc.returncode}\n{tail}\n{(proc.stderr or '')[-1000:]}"
            )
        if not app.is_dir():
            raise SimulatorWdaError(f"构建成功但产物不存在：{app}")

        logger.info(
            "虚拟机版 WDA 构建完成，耗时 {:.1f}s：{}", time.monotonic() - started, app
        )
        return app


# ---------------------------------------------------------------------------
# 启动器
# ---------------------------------------------------------------------------
# iPad 路径的常驻 xcodebuild 子进程表。与真机 launcher 同一套做法：不 kill 的话
# XCTest 会在虚拟机里留下 session，下次启动报「A session already exists」。
_XCODEBUILD_PROCS: Dict[str, subprocess.Popen] = {}
_XCODEBUILD_LOCK = threading.Lock()


def _remember_xcodebuild(udid: str, proc: subprocess.Popen) -> None:
    with _XCODEBUILD_LOCK:
        _XCODEBUILD_PROCS[udid] = proc


def _forget_xcodebuild(udid: str) -> None:
    with _XCODEBUILD_LOCK:
        _XCODEBUILD_PROCS.pop(udid, None)


def _kill_all_xcodebuild() -> None:
    with _XCODEBUILD_LOCK:
        procs = list(_XCODEBUILD_PROCS.items())
        _XCODEBUILD_PROCS.clear()
    for udid, proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
            logger.info("Agent 退出，终止 iPad WDA 的 xcodebuild udid={}", udid)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_kill_all_xcodebuild)


def _probe_wda_device_name(port: int, timeout_s: float = 2.0) -> Optional[str]:
    """读 ``/wda/device/info`` 的 ``name``，即虚拟机实例名（``aiphone_sim_<vmid>``）。

    这是 WDA 唯一能反映「我跑在哪台设备上」的字段——``/status`` 里只有机型族
    （``device: "iphone"``），区分不了两台同机型的实例。
    """
    url = f"http://127.0.0.1:{port}/wda/device/info"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    value = payload.get("value")
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    return name or None


def _prune_test_results(out_dir: Path, keep: int = 3) -> None:
    """清掉旧的 xcresult 结果包，只留最近几个。

    WDA 是常驻服务不是一次性测试，这些包除了排障没别的用处，但每次启动都会产出，
    不清理就只增不减。实测单个包可达几十 MB。
    """
    logs = out_dir / "Logs" / "Test"
    try:
        bundles = sorted(
            (p for p in logs.glob("*.xcresult")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in bundles[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("清理旧测试结果包失败（忽略）：{}", exc)


def _probe_wda(port: int, timeout_s: float = 1.5) -> Optional[dict]:
    """探一次 ``/status``；通了返回 value 字典，否则 None。"""
    url = f"http://127.0.0.1:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    value = payload.get("value")
    return value if isinstance(value, dict) else None


def is_ipad(udid: str) -> bool:
    """这台虚拟机是不是 iPad。查不到按 iPhone 处理（轻量路径是默认）。"""
    try:
        for dev in list_simulators():
            if dev.udid == udid:
                return ".ipad-" in (dev.device_type_id or "").lower()
    except Exception as exc:  # noqa: BLE001
        logger.debug("判断 udid={} 是否 iPad 失败，按 iPhone 处理：{}", udid, exc)
    return False


class IosSimulatorWdaLauncher:
    """单台虚拟机的 WDA 生命周期。

    典型用法::

        launcher = IosSimulatorWdaLauncher(udid)
        launcher.start()          # 构建（首次）→ install → launch → 等 ready
        ...                       # launcher.wda_url 给 WdaClient 用
        launcher.stop()

    **两条启动路，按机型自动选**（方案 §1.8.0）：

    ```text
    iPhone  simctl install + simctl launch     无常驻进程，WDA 由 launchd_sim 托管
    iPad    xcodebuild test-without-building   与 iOS 真机同族，需常驻子进程守着
    ```

    iPad 之所以不能走轻量路，是 ``simctl launch`` 独立自举时 XCTest 会先做完整的
    UI 测试初始化——其中「把 runner 切到后台」这步在 iPad 上必然 30 秒超时，WDA 的
    HTTP 服务永远起不来。换 ``xcodebuild`` 后时序不同（先跑测试、后处理切后台），
    实测 3 秒就绪。根因与实验记录见方案 §1.8.0。

    iPad 这条路的行为**与 iOS 真机完全一致**（见 :class:`~.ios_wda_launcher.
    IosWdaXcodeLauncher`）：子进程生命周期 = WDA 生命周期，Agent 退出即回收，
    重启后需要重新拉起。这不是 iPad 特殊，是 xcodebuild 这一族的固有性质。
    """

    def __init__(
        self,
        udid: str,
        *,
        project_dir: Optional[Path] = None,
        scheme: Optional[str] = None,
        bundle_id: Optional[str] = None,
        build_dir: Optional[Path] = None,
        ports: Optional[SimulatorPorts] = None,
        expect_sim_name: str = "",
    ) -> None:
        """
        Args:
            expect_sim_name: 本设备的实例名，用于校验端口上的 WDA 是不是自己人。
                **调用方知道就一定要传**——manager 手里的 ``aiphone_sim_<vm_id>``
                是算出来的、必然正确，比让 launcher 回头去 simctl 查一遍可靠：
                查询会失败，失败就只能降级成不校验，而那正是身份校验要防的场景。
                留空时才回退到查询。
        """
        self.udid = udid
        self._project_dir = project_dir
        self._scheme = scheme
        self._bundle_id = bundle_id
        self._build_dir = build_dir
        self._expect_sim_name = (expect_sim_name or "").strip()
        self.ports = ports or allocate_ports(udid)
        self._runner_bundle_id: Optional[str] = None
        # 仅 iPad 路径使用：常驻的 xcodebuild 子进程
        self._xcodebuild_proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    @property
    def wda_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.wda}"

    @property
    def runner_bundle_id(self) -> Optional[str]:
        return self._runner_bundle_id

    def is_alive(self, *, expect_sim_name: str = "") -> bool:
        """端口上是否有一个**属于本设备**的、ready 的 WDA。

        ``expect_sim_name`` 给定时会校验身份。**这道校验不是多余的**：
        端口只是个约定，端口上应答的未必就是这台设备的 WDA。哈希定槽位已经消除了
        最常见的漂移，但槽位冲突、用户手工起过 WDA、端口被别的实例占用等情况仍会
        让「端口通」与「是这台」脱钩。认领时一旦认错，两台设备就互换了身份——
        操作 A 实际驱动 B，而且界面上看不出任何异常。宁可判为未就绪、让用户点一次
        启动，也不能默认端口上的就是自己人。
        """
        value = _probe_wda(self.ports.wda)
        if not (value and value.get("ready")):
            return False
        if not expect_sim_name:
            return True
        actual = _probe_wda_device_name(self.ports.wda)
        if actual is None:
            # 读不到身份就不敢认——同样按「宁可判未就绪」处理
            logger.debug(
                "udid={} 端口 {} 上的 WDA 读不出设备名，不予认领",
                self.udid, self.ports.wda,
            )
            return False
        if actual != expect_sim_name:
            logger.warning(
                "端口 {} 上的 WDA 属于「{}」而非「{}」，拒绝认领（避免两台设备互换身份）",
                self.ports.wda, actual, expect_sim_name,
            )
            return False
        return True

    def _sim_name(self) -> str:
        """本设备的实例名。构造时给了就直接用，没给才去查；查不到返回空串。"""
        if self._expect_sim_name:
            return self._expect_sim_name
        try:
            for dev in list_simulators():
                if dev.udid == self.udid:
                    return dev.name or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("查询 udid={} 实例名失败：{}", self.udid, exc)
        return ""

    def _require_sim_name(self) -> str:
        """拿不到期望身份就抛错。**所有会认领端口上 WDA 的路径都必须先过这里。**

        身份校验只在「知道自己叫什么」时才成立。空名字传给 ``is_alive`` 会被当成
        「不校验」直接放行——那恰好把「simctl 短暂查询失败」这种最可能与端口冲突
        同时发生的时刻，变成了没有防护的时刻。
        """
        name = self._sim_name()
        if not name:
            raise SimulatorWdaError(
                f"无法确定 udid={self.udid} 的期望实例名，拒绝认领端口 "
                f"{self.ports.wda} 上的 WDA（不做无身份校验的启动）。"
                "构造 launcher 时应传入 expect_sim_name"
            )
        return name

    # ------------------------------------------------------------------
    def start(self, *, wait_ready_s: Optional[float] = None, force_build: bool = False) -> None:
        """构建（首次）→ install → launch → 轮询到 ready。失败直接抛错。"""
        settings = get_settings()
        timeout = (
            wait_ready_s
            if wait_ready_s is not None
            else float(settings.wda_startup_timeout_s)
        )

        app = build_wda_for_simulator(
            project_dir=self._project_dir,
            scheme=self._scheme,
            bundle_id=self._bundle_id,
            build_dir=self._build_dir,
            force=force_build,
        )
        self._runner_bundle_id = read_runner_bundle_id(app)

        # 已经在跑就不重装重启，直接复用（等价真机 launcher 的 attach 优先）。
        # 同样要校验身份：复用到别人的 WDA，等于这台设备从此驱动的是另一台。
        #
        # 用 _require_sim_name 而不是 _sim_name：空名字传给 is_alive 会被当成
        # 「不校验」直接放行，那样复用这条路就绕过了身份校验——而它比新起 WDA
        # 更危险，因为它连 _wait_ready 都不会走到。
        if self.is_alive(expect_sim_name=self._require_sim_name()):
            logger.info(
                "udid={} 端口 {} 上已有本设备 ready 的 WDA，直接复用",
                self.udid, self.ports.wda,
            )
            return

        if is_ipad(self.udid):
            self._start_via_xcodebuild(timeout)
            return

        self._install(app)
        self._launch()
        self._wait_ready(timeout)

    def stop(self) -> None:
        """停掉 WDA。不删除已安装的 App（下次启动可省掉 install）。"""
        proc = self._xcodebuild_proc
        if proc is not None:
            self._xcodebuild_proc = None
            _forget_xcodebuild(self.udid)
            try:
                proc.terminate()
                logger.info("udid={} 已终止 xcodebuild pid={}", self.udid, proc.pid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("udid={} 终止 xcodebuild 失败（忽略）：{}", self.udid, exc)
            return

        bundle_id = self._runner_bundle_id
        if not bundle_id:
            return
        try:
            simctl_run("terminate", self.udid, bundle_id, check=False, timeout=15.0)
        except SimctlError as exc:
            logger.debug("udid={} 终止 WDA 失败（忽略）：{}", self.udid, exc)

    # ------------------------------------------------------------------
    # iPad 路径：xcodebuild test-without-building
    # ------------------------------------------------------------------
    def _start_via_xcodebuild(self, timeout: float) -> None:
        xcodebuild = _find_xcodebuild()
        if xcodebuild is None:
            raise SimulatorWdaError("找不到 xcodebuild")
        out_dir = _abs_build_dir(self._build_dir) if self._build_dir else default_build_dir()
        xctestrun = self._prepare_xctestrun(out_dir)

        cmd = [
            xcodebuild,
            "test-without-building",
            "-xctestrun",
            str(xctestrun),
            "-destination",
            f"platform=iOS Simulator,id={self.udid}",
            # 结果包与日志写进虚拟机自己的目录。**不加这行会写进
            # ~/Library/Developer/Xcode/DerivedData**——那是 iOS 真机 xcodebuild
            # 用的同一个根目录，而且每个 xctestrun 路径会各建一个新 hash 目录，
            # 一台实例一个、只增不减。实测半天就堆出 9 个目录、几百 MB。
            "-derivedDataPath",
            str(out_dir),
        ]
        _prune_test_results(out_dir)
        logger.info("udid={} iPad 走 xcodebuild 启动 WDA：{}", self.udid, " ".join(cmd))
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=str(xctestrun.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            raise SimulatorWdaError(f"启动 xcodebuild 失败 udid={self.udid}：{exc}") from exc

        self._xcodebuild_proc = proc
        _remember_xcodebuild(self.udid, proc)
        try:
            self._wait_ready(timeout)
        except Exception:
            self.stop()
            raise

    def _prepare_xctestrun(self, out_dir: Path) -> Path:
        """按本实例的端口生成一份专属 xctestrun。

        两个约束：

        1. **必须放在原 xctestrun 同目录**。文件里的 ``__TESTROOT__`` 占位符是相对
           xctestrun 自身位置解析的，挪到别处会让 xcodebuild 找不到测试产物，报
           「Cannot test target」。
        2. **端口只能写进 ``EnvironmentVariables``**。``xcodebuild`` 不把自己的环境
           变量传给被测进程，``SIMCTL_CHILD_*`` 那套也只对 ``simctl launch`` 有效。
        """
        products = out_dir / "Build" / "Products"
        candidates = sorted(products.glob("*.xctestrun"))
        base = next((p for p in candidates if not p.name.startswith("aiphone-")), None)
        if base is None:
            raise SimulatorWdaError(
                f"找不到 xctestrun（{products}）。iPad 路径依赖 build-for-testing 的产物"
            )

        try:
            payload = plistlib.loads(base.read_bytes())
        except Exception as exc:  # noqa: BLE001
            raise SimulatorWdaError(f"解析 xctestrun 失败 {base}：{exc}") from exc

        for key, cfg in payload.items():
            if key.startswith("__") or not isinstance(cfg, dict):
                continue
            env = dict(cfg.get("EnvironmentVariables") or {})
            env["USE_PORT"] = str(self.ports.wda)
            env["MJPEG_SERVER_PORT"] = str(self.ports.mjpeg)
            cfg["EnvironmentVariables"] = env

        target = base.with_name(f"aiphone-{self.udid}.xctestrun")
        target.write_bytes(plistlib.dumps(payload))
        return target

    # ------------------------------------------------------------------
    def _installed_app_path(self) -> Optional[Path]:
        """虚拟机里已装的 runner 路径；没装返回 ``None``（约 0.1 秒）。"""
        if not self._runner_bundle_id:
            return None
        try:
            out = simctl_run(
                "get_app_container", self.udid, self._runner_bundle_id, "app",
                timeout=20.0,
            )
        except SimctlError:
            # 没装时 simctl 直接非零退出，这是正常分支，不是故障
            return None
        return Path(out) if out else None

    def _install_is_current(self, app: Path) -> bool:
        """已装的那份是不是当前产物。

        比 mtime：产物重编后 ``.app`` 的时间戳会更新，装进去的那份不会，据此判断
        需不需要重装。**取不到时间戳一律判 False**——多装一次只是慢几秒，装漏了
        会让虚拟机跑在旧 WDA 上，那种问题极难从现象倒推。
        """
        installed = self._installed_app_path()
        if installed is None or not installed.is_dir():
            return False
        try:
            built_at = app.stat().st_mtime
            installed_at = installed.stat().st_mtime
        except OSError:
            return False
        return installed_at >= built_at

    def _install(self, app: Path) -> None:
        """装 WDA。已装且是当前产物就跳过。

        ``simctl install`` 实测约 4.5 秒，而冷启动总共才 8.8 秒——它是最大的一块，
        且绝大多数情况下纯属重复劳动（产物全实例共享，装过一次就在虚拟机里了）。
        用 0.1 秒的 ``get_app_container`` 换掉它。
        """
        if self._install_is_current(app):
            logger.info(
                "udid={} 已装的 WDA runner 即当前产物，跳过安装：{}",
                self.udid, self._runner_bundle_id,
            )
            return
        try:
            simctl_run("install", self.udid, str(app), timeout=120.0)
        except SimctlError as exc:
            raise SimulatorWdaError(
                f"安装 WDA 到虚拟机失败 udid={self.udid}：{exc}"
            ) from exc
        logger.info("udid={} 已安装 WDA runner：{}", self.udid, self._runner_bundle_id)

    def _launch(self) -> None:
        """``simctl launch`` 起 WDA。端口经 ``SIMCTL_CHILD_*`` 传给子进程。

        ``SIMCTL_CHILD_`` 前缀是 simctl 的约定：带该前缀的环境变量会去掉前缀后
        注入被启动的 App 进程。WDA 读 ``USE_PORT`` / ``MJPEG_SERVER_PORT`` 决定
        监听端口——这正是多实例端口隔离的实现手段（方案 §3.2）。
        """
        xcrun = shutil.which("xcrun")
        if xcrun is None:
            raise SimulatorWdaError("找不到 xcrun")
        assert self._runner_bundle_id  # start() 里已保证

        env = dict(os.environ)
        env["SIMCTL_CHILD_USE_PORT"] = str(self.ports.wda)
        env["SIMCTL_CHILD_MJPEG_SERVER_PORT"] = str(self.ports.mjpeg)

        cmd = [
            xcrun, "simctl", "launch",
            "--terminate-running-process",
            self.udid,
            self._runner_bundle_id,
        ]
        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=60.0,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise SimulatorWdaError(f"启动 WDA 超时 udid={self.udid}") from exc
        if proc.returncode != 0:
            raise SimulatorWdaError(
                f"启动 WDA 失败 udid={self.udid} rc={proc.returncode} "
                f"stderr={(proc.stderr or '').strip()!r}"
            )
        logger.info(
            "udid={} 已启动 WDA：{} → wda={} mjpeg={}",
            self.udid, (proc.stdout or "").strip(), self.ports.wda, self.ports.mjpeg,
        )

    def _wait_ready(self, timeout: float) -> None:
        """轮询到端口上出现**本设备**的 ready WDA。

        两道校验缺一不可，对应两种认错设备的方式：

        1. ``ios.simulatorVersion`` 存在 → 是虚拟机的 WDA，不是真机的
           （防端口域与真机 8100 段冲突）
        2. ``/wda/device/info`` 的实例名匹配 → 是**这一台**虚拟机

        第 2 道曾经只有 ``is_alive`` 有、这里没有，两处标准不一致埋了个静默错误：
        两台虚拟机的 UDID 哈希撞到同一槽位时，端口按分配顺序决定，Agent 重启后
        顺序一变两台就换号。换号后本设备的 WDA 绑不上端口（被对方占着），而这里
        只要看到「一个虚拟机的 WDA」就收货，于是把对方的 WDA 登记成自己的——
        **界面显示操作 A，实际驱动 B，且毫无异常迹象**。

        **拿不到期望身份时失败关闭，不降级成不校验。** 降级看着温和，实际是把
        「simctl 短暂查询失败」这种最可能与端口冲突同时发生的时刻，恰好变成了
        没有防护的时刻。正常路径上 manager 会把算好的实例名传进来，根本不需要
        查询；走到这里还拿不到，说明调用方没接线，那是 bug，应该暴露。
        """
        expect = self._require_sim_name()
        deadline = time.monotonic() + max(1.0, timeout)
        mismatch: str = ""
        while time.monotonic() < deadline:
            value = _probe_wda(self.ports.wda)
            if value and value.get("ready"):
                sim_ver = (value.get("ios") or {}).get("simulatorVersion")
                # /status 带 ios.simulatorVersion 是虚拟机独有的标志；真机不返回。
                # 拿它做一次身份校验，防止端口被真机 WDA 意外占用时认错设备。
                if not sim_ver:
                    raise SimulatorWdaError(
                        f"端口 {self.ports.wda} 上的 WDA 不是虚拟机实例"
                        "（/status 缺少 ios.simulatorVersion），疑似端口域与真机冲突"
                    )
                actual = _probe_wda_device_name(self.ports.wda)
                if actual != expect:
                    # 不立刻抛：本设备的 WDA 可能还在起，端口上暂时是别人的
                    # 应答。继续等到超时，用最后一次观测结果给出可操作的报错。
                    mismatch = actual or "(读不出设备名)"
                    time.sleep(0.5)
                    continue
                logger.info(
                    "udid={} WDA ready（simulator {}）{}",
                    self.udid, sim_ver, self.wda_url,
                )
                return
            time.sleep(0.5)
        if mismatch:
            raise SimulatorWdaError(
                f"端口 {self.ports.wda} 上的 WDA 属于「{mismatch}」而非「{expect}」"
                f"（udid={self.udid}）。两台虚拟机的端口槽位发生了冲突，"
                "本设备的 WDA 起不来；请停掉其中一台后重试"
            )
        raise SimulatorWdaError(
            f"等待 WDA ready 超时 udid={self.udid} port={self.ports.wda}（{timeout}s）。"
            "排查方向：虚拟机是否已 boot、runner 是否被系统杀掉、端口是否被占"
        )
