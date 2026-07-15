"""iOS 驱动：``pymobiledevice3`` 拿设备元信息 + 截图，``WebDriverAgent`` 走触控/输入/应用。

总体设计原则——和 Android 路径"对称"：

1. ``BaseDriver`` 实现完全对齐 Android：上层 ``runner`` / ``handle_input`` 不感知平台
2. 坐标系：iOS 内部用 *逻辑点 (point)* 而非物理像素；本类对外暴露的 ``window_size``
   按 ``point × scale`` 折算回物理像素，与 Android ``window_size`` 语义一致——
   这样 VLM 0~999 归一化坐标 / 浏览器手势坐标都不需要按平台分支
3. 平台特化全部封在本模块；``pymobiledevice3`` 在所有方法**内部** lazy import，
   没装 ``ios`` extras 时 import 本模块仍然成功（设备发现只是返回空列表）
4. WDA 假定运行在 ``http://127.0.0.1:{port}``，端口由 ``open_ios_driver`` 分配，
   并通过 ``_UsbmuxPortForwarder`` 透过 usbmuxd 连进设备侧 8100

WDA 启动方式（2026-04 切换到 Xcode/XCTest 主线）：
- 主路径：``IosWdaXcodeLauncher`` 用 ``xcodebuild test -allowProvisioningUpdates``
  在 agent 启动时自动拉起真机上的 WDA XCTest runner；``-allowProvisioningUpdates``
  让免费 Apple ID 的 7 天签名每次自动续上
- 兼容路径：用户自己在 Xcode 里 Cmd+U 起好 WDA + ``iproxy 8100:8100``
  → launcher.start() 会 HTTP 探测到已有 WDA，直接 attach，不再重复启动
- 历史：``go-ios runwda`` 在 iOS 26 上撞 XCTest Error 103 无法打通，已全面废弃
"""
from __future__ import annotations

import asyncio
import inspect
import io
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image
from loguru import logger

from ...config import get_settings
from .base import BaseDriver, DeviceInfo, InstalledApp
from .ios_wda_launcher import IosWdaXcodeLauncher, _developer_app_trust_hint, _probe_wda_http
from .wda_client import WdaClient, WdaError


# WDA 在 iOS 内部监听的端口
_WDA_DEVICE_PORT = 8100


# ---------------------------------------------------------------------------
# pymobiledevice3 lazy import 工具
# ---------------------------------------------------------------------------
def _import_pmd3():
    """统一入口的 lazy import；返回 ``(usbmux, create_lockdown, screenshot_svc, ip_svc)``。

    ``create_lockdown(serial=udid) -> LockdownClient`` 是个工厂函数，自动适配
    pmd3 多版本 API：

    - 1.43+ 提供 ``pymobiledevice3.lockdown.create_using_usbmux``
    - 1.42 等老版本只能直接 ``LockdownClient(serial=udid)``

    任何一个 import 失败都 raise ``ImportError``，调用方负责捕获并降级。
    """
    try:
        from pymobiledevice3 import usbmux as _usbmux  # noqa: PLC0415
        from pymobiledevice3 import lockdown as _lockdown_mod  # noqa: PLC0415
        from pymobiledevice3.services.screenshot import ScreenshotService as _ss  # noqa: PLC0415
        from pymobiledevice3.services.installation_proxy import InstallationProxyService as _ip  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise ImportError(
            "iOS 接入需要 pymobiledevice3。请 pip install -e \".[ios]\" 后重启 agent。"
            f"原始错误：{exc}"
        ) from exc

    if hasattr(_lockdown_mod, "create_using_usbmux"):
        _create = _lockdown_mod.create_using_usbmux
    else:
        # 1.42 及更早：直接构造 LockdownClient
        _LockdownClient = _lockdown_mod.LockdownClient

        def _create(serial: str = None, **kwargs):  # type: ignore[no-redef]
            return _LockdownClient(serial=serial, **kwargs)

    return _usbmux, _create, _ss, _ip


# ---------------------------------------------------------------------------
# pmd3 9.x async → sync 适配（全局长寿命 loop 模型）
# ---------------------------------------------------------------------------
# pmd3 9.x 把 ``usbmux.list_devices`` / ``select_device`` / ``connect_port``
# 等接口全改成 ``async def``，并且 **stateful 对象**（``LockdownClient``、
# ``ServiceConnection`` 等）会把 ``StreamReader/Writer`` 绑在创建时所在的
# event loop 上。
#
# 这意味着不能"每次 ``asyncio.run`` 开关 loop"——上次创建的 lockdown 在新
# loop 里调 ``get_value()`` 会抛 ``Future attached to a different loop``。
#
# 所以这里用一个**单线程后台 loop**：
#   - 启动一个 daemon 线程跑专属 ``loop.run_forever()``
#   - 所有 pmd3 coroutine 都通过 ``run_coroutine_threadsafe`` 提交到这个 loop
#   - sync 调用方用 ``future.result()`` 阻塞等
# 这样所有 pmd3 对象的生命周期都共享同一个 loop，state 一致、永不漂。
_PMD3_LOOP: Optional[asyncio.AbstractEventLoop] = None
_PMD3_LOOP_LOCK = threading.Lock()


def _get_pmd3_loop() -> asyncio.AbstractEventLoop:
    global _PMD3_LOOP  # noqa: PLW0603
    with _PMD3_LOOP_LOCK:
        if _PMD3_LOOP is not None and not _PMD3_LOOP.is_closed():
            return _PMD3_LOOP
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_runner, daemon=True, name="pmd3-loop").start()
        _PMD3_LOOP = loop
        return loop


def _maybe_sync(value: Any, timeout: float = 30.0) -> Any:
    """如果 ``value`` 是 coroutine/awaitable，丢到全局 pmd3 loop 同步等结果。
    否则原样返回（兼容老版 sync API）。

    ``timeout`` 默认 30s——pmd3 大多数 lockdown 调用 < 1s；DVT screenshot
    单次 < 2s；只有 mount/install 等大动作才会逼近上限。超时会抛 ``TimeoutError``。
    """
    if not inspect.isawaitable(value):
        return value
    loop = _get_pmd3_loop()
    fut = asyncio.run_coroutine_threadsafe(_await_it(value), loop)
    return fut.result(timeout=timeout)


async def _await_it(awaitable: Any) -> Any:
    """``run_coroutine_threadsafe`` 严格要 coroutine，不接受任意 awaitable；
    包一层把 awaitable / Future / Task 都拍平。"""
    return await awaitable


# ---------------------------------------------------------------------------
# 端口转发：把本地 TCP 端口透传到设备 USB 通道上的 WDA
# ---------------------------------------------------------------------------
class _UsbmuxPortForwarder:
    """单设备的本地端口 → usbmux 端口转发，纯 Python 实现。

    线程模型：
        - 主线程 ``start()`` 起一个 daemon listener 线程
        - 每个 accept 起一个 daemon ``_pump`` 线程（双向 splice）
        - ``stop()`` 关 listener；存量连接靠 daemon 退出兜底

    实现注意：
        - pymobiledevice3 提供 ``usbmux.connect_port(udid, port)`` 拿一个已连
          上设备目标端口的 socket（实际上是和 usbmuxd 之间的 socket，usbmuxd
          帮忙打通到设备）
        - 浏览器侧 / WDA HTTP client 用普通 ``connect(127.0.0.1, local_port)``
        - 不依赖 ``iproxy`` / ``socat``，跨平台一致

    fail-fast：listener 起不起来直接抛；个别 pump 线程异常只 debug 日志，
    避免单连接挂掉影响整体。
    """

    def __init__(self, udid: str, local_port: int, device_port: int = _WDA_DEVICE_PORT) -> None:
        self.udid = udid
        self.local_port = local_port
        self.device_port = device_port
        self._stopped = False
        self._listen_sock: Optional[socket.socket] = None
        self._listen_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._listen_sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.local_port))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"iOS 端口转发 listener bind 失败 udid={self.udid} "
                f"local={self.local_port}: {exc}"
            ) from exc
        sock.listen(8)
        self._listen_sock = sock
        self._listen_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name=f"ios-fwd-{self.udid}-{self.local_port}",
        )
        self._listen_thread.start()
        logger.info(
            "iOS 端口转发已启动 udid={} 127.0.0.1:{} → device:{}",
            self.udid, self.local_port, self.device_port,
        )

    def stop(self) -> None:
        self._stopped = True
        sock = self._listen_sock
        self._listen_sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    def _accept_loop(self) -> None:
        listen = self._listen_sock
        if listen is None:
            return
        # WDA 没启动时上游会一直返回 ConnectionFailedError(Number=3)，每秒一次刷屏
        # 没意义。这里做计数节流：第一次 warning，后续每 30 次记一次。
        upstream_fail_count = 0
        while not self._stopped:
            try:
                client, _ = listen.accept()
            except OSError:
                break
            try:
                upstream = self._open_upstream()
                upstream_fail_count = 0
            except Exception as exc:  # noqa: BLE001
                upstream_fail_count += 1
                if upstream_fail_count == 1 or upstream_fail_count % 30 == 0:
                    logger.warning(
                        "iOS 端口转发上游连接失败 udid={} 累计={} 次（WDA 是否已启动？）：{}",
                        self.udid, upstream_fail_count, exc,
                    )
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            threading.Thread(
                target=self._pump, args=(client, upstream), daemon=True,
            ).start()
            threading.Thread(
                target=self._pump, args=(upstream, client), daemon=True,
            ).start()

    def _open_upstream(self) -> socket.socket:
        """通过 usbmux 连到设备的 ``device_port``，返回一个普通 socket。

        pmd3 9.x 兼容（与 4.x 不同的几点）：

        - ``usbmux.connect_port`` 被删了；新姿势是
          ``MuxDevice.connect(port)`` 返回 socket
        - ``select_device`` / ``MuxDevice.connect`` 都是 ``async def``，
          这里用 ``_maybe_sync`` 桥接到当前同步线程
        - 老 ``create_mux`` + ``mux.connect(dev, port)`` 的两步法已废弃
        """
        from pymobiledevice3 import usbmux  # noqa: PLC0415

        dev = _maybe_sync(usbmux.select_device(udid=self.udid))
        if dev is None:
            raise RuntimeError(f"udid {self.udid} 不在 usbmux 设备列表里")

        return _maybe_sync(dev.connect(self.device_port))

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(8192)
                if not data:
                    break
                dst.sendall(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass


# 全局端口分配（同一 agent 进程内 udid → local_port 1:1 复用）。
# 起点走 settings.wda_local_port，默认 8100。多设备时严格递增。
_PORT_ALLOC_LOCK = threading.Lock()
_PORT_ALLOC_MAP: Dict[str, int] = {}
_NEXT_PORT: Optional[int] = None

# 全局已就绪的 WdaClient 索引（udid → client），供 mirror 等同进程其它模块复用，
# **避免自己再 new 一个 WdaClient 建新 session 把 driver 的 session 顶掉**。
# driver.close() 时会从这里移除；没建成功不会进来。
_WDA_CLIENT_MAP: Dict[str, "WdaClient"] = {}

# iOS lockdown 元信息缓存（udid → {platform/brand/model/os_version/screen_*}）。
# 意义：iOS 18/26+ 在设备锁屏时会让 lockdown StartSession 报 PasswordProtected，
# 即使 pair record 有 EscrowBag 也没用——这是 iOS 本身收紧的限制，不是配对问题。
# 一旦某台设备至少被读到过一次，就把它的元信息存这里；后续锁屏期间直接复用，
# 让设备卡片保持存在（status=locked），用户点亮屏幕后下次 rescan 自动升回 online。
# 只保存 DeviceInfo 的字段快照（dict），刻意不保存 DeviceInfo 实例本身，避免
# status/extra 被串改。key 在设备拔出 / agent 重启时自动淘汰（进程级缓存）。
_IOS_META_CACHE: Dict[str, Dict[str, Any]] = {}


def _wda_alive(udid: str) -> bool:
    """WDA 是否已就绪：有 client 且 ``/status`` 通。

    这是"设备事实可用"的金标准——哪怕 lockdown 此刻抽风报 ``PasswordProtected``
    （iOS 18/26 锁屏 + 某些 session 老化场景），只要 WDA 活着我们就能点击、截图、
    跑 VLM，没必要把卡片降成 unauthorized 把用户拦在门外。
    """
    cli = _WDA_CLIENT_MAP.get(udid)
    if cli is None:
        return False
    try:
        return bool(cli.status())
    except Exception:  # noqa: BLE001
        return False


def _alloc_local_port(udid: str) -> int:
    global _NEXT_PORT  # noqa: PLW0603
    with _PORT_ALLOC_LOCK:
        if udid in _PORT_ALLOC_MAP:
            return _PORT_ALLOC_MAP[udid]
        if _NEXT_PORT is None:
            _NEXT_PORT = int(get_settings().wda_local_port or 8100)
        port = _NEXT_PORT
        _NEXT_PORT += 1
        _PORT_ALLOC_MAP[udid] = port
        return port


# ---------------------------------------------------------------------------
# DVT Screenshot 的 duck-typed 包装，让它对外 API 和 pmd3 ``ScreenshotService``
# 等价：``.take_screenshot()`` 返回 awaitable，``.close()`` 释放底层连接。
# 这样 ``IosDriver._ensure_screenshot_svc`` 返回值对调用方是透明的。
# ---------------------------------------------------------------------------
class _DvtScreenshotSvc:
    def __init__(self, provider, shooter) -> None:  # noqa: ANN001
        self._provider = provider
        self._shooter = shooter

    def take_screenshot(self):
        # pmd3 9.x: DVT Screenshot.get_screenshot() 返回 coroutine，交给
        # _maybe_sync 丢到 _PMD3_LOOP await。和老 ScreenshotService.take_screenshot
        # 行为一致
        return self._shooter.get_screenshot()

    def close(self) -> None:
        try:
            _maybe_sync(self._shooter.close())
        except Exception:  # noqa: BLE001
            pass
        try:
            _maybe_sync(self._provider.close())
        except Exception:  # noqa: BLE001
            pass


class _WdaScreenshotSvc:
    """WDA HTTP ``/screenshot`` 的 duck-typed 包装，接口对齐 ``_DvtScreenshotSvc``。

    这条路不依赖 tunneld / DDI；只要 WDA 跑着就能出图，是 iOS 17+ 在没配
    tunneld 时的首选截图通道。返回的是 PNG 字节，调用方拿到后当作
    ``take_screenshot()`` 的 awaitable 结果处理——``_maybe_sync`` 对
    非 awaitable 的值会原样返回，行为等价。
    """

    def __init__(self, wda: WdaClient) -> None:
        self._wda = wda

    def take_screenshot(self) -> bytes:
        return self._wda.screenshot()

    def close(self) -> None:  # noqa: D401
        # WdaClient 的生命周期由 IosDriver.close() 管，这里不重复释放
        pass


# ---------------------------------------------------------------------------
# IosDriver
# ---------------------------------------------------------------------------
class IosDriver(BaseDriver):
    """iOS 设备驱动。每个 udid 一个实例，内部封 lockdown + WDA HTTP。"""

    platform = "ios"

    def __init__(
        self,
        udid: str,
        lockdown,  # noqa: ANN001 - LockdownClient
        wda: WdaClient,
        forwarder: Optional[_UsbmuxPortForwarder] = None,
        launcher: Optional[IosWdaXcodeLauncher] = None,
    ) -> None:
        self.serial = udid
        self._lockdown = lockdown
        self._wda = wda
        self._forwarder = forwarder
        # xcodebuild test 子进程的 launcher；close() 时要一并停
        self._launcher = launcher
        # 截图服务延迟创建
        self._screenshot_svc = None

        # WDA 报告的 point 坐标系 → 物理像素需要乘 scale；缓存一次
        self._scale: Optional[float] = None
        # 同一 Run 的 close_app + open_app 必须看到同一份动态应用目录，避免重复走
        # installation_proxy，也避免两次查询间安装状态变化导致匹配漂移。
        self._installed_apps_cache: Optional[List[InstalledApp]] = None

    # ------------------------------------------------------------------
    # Run 前准备
    # ------------------------------------------------------------------
    def prepare_for_run(self) -> None:
        """Run 前只调用 WDA unlock；不复用带 press_home fallback 的旧唤醒路径。"""
        try:
            self._wda.unlock()
            logger.info("iOS Run 前唤醒：wda.unlock serial={}", self.serial)
        except Exception as exc:  # noqa: BLE001
            logger.warning("iOS prepare_for_run unlock 失败 serial={}: {}", self.serial, exc)

        settle_s = max(
            0,
            int(getattr(get_settings(), "ios_wake_before_run_settle_ms", 500)),
        ) / 1000.0
        if settle_s > 0:
            time.sleep(settle_s)

    # ------------------------------------------------------------------
    # 屏幕信息
    # ------------------------------------------------------------------
    def _get_scale(self) -> float:
        if self._scale is None:
            try:
                self._scale = self._wda.screen_scale() or 1.0
            except Exception:  # noqa: BLE001
                self._scale = 1.0
        return self._scale

    def window_size(self) -> Tuple[int, int]:
        """物理像素的 (width, height)。

        WDA ``/window/size`` 返回逻辑点；要乘 scale 才能和 Android 那条
        "device pixel" 坐标系对齐。我们对外只暴露物理像素，让上层完全不管平台。
        """
        try:
            sz = self._wda.window_size()
            scale = self._get_scale()
            return int(round(sz.width * scale)), int(round(sz.height * scale))
        except Exception as exc:  # noqa: BLE001
            logger.warning("WDA window_size 失败 udid={}: {}", self.serial, exc)
            # 兜底：从 lockdown 读
            try:
                w = int(_maybe_sync(self._lockdown.get_value(domain="com.apple.mobile.iTunes", key="ScreenWidth")) or 0)
                h = int(_maybe_sync(self._lockdown.get_value(domain="com.apple.mobile.iTunes", key="ScreenHeight")) or 0)
                return w, h
            except Exception:  # noqa: BLE001
                return 0, 0

    def rotation(self) -> int:
        try:
            o = self._wda.orientation()
        except Exception:
            return 0
        # WDA 返回 'PORTRAIT' / 'LANDSCAPE' / 'UIA_DEVICE_ORIENTATION_*'
        m = {
            "PORTRAIT": 0,
            "LANDSCAPE": 1,
            "UIA_DEVICE_ORIENTATION_PORTRAIT": 0,
            "UIA_DEVICE_ORIENTATION_LANDSCAPELEFT": 1,
            "UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN": 2,
            "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT": 3,
        }
        return m.get(o, 0)

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    # iOS 17+ 已废掉 lockdown 老服务 ``com.apple.mobile.screenshotr``，
    # pmd3 ``ScreenshotService(lockdown=...)`` 在 iOS 17+ 会 ``InvalidService``。
    # 唯一可用的路径是 DVT 上的 ``Screenshot`` instrument（和镜像链同款），
    # 必须经 tunneld + RSD 拿 RemoteServiceDiscoveryService。
    #
    # 我们优先走 DVT，失败才回退 lockdown（iOS ≤ 16 还能用老路径）。
    # DVT provider + Screenshot 是长连接，全程复用；close() 里统一关。
    #
    # 多连接并发：mirror 已经开一条 DVT Screenshot 连接做镜像推流，driver
    # 再开一条做 VLM/按需截图是两条独立的 socket，pmd3 允许共存，实测在
    # iOS 26 上 OK；如果未来出现"同时两个 DVT 串扰"的报错，再让两条连接
    # 共享同一个 shooter（需改 mirror/driver 之间的生命周期）。
    def _ensure_screenshot_svc(self):
        if self._screenshot_svc is not None:
            return self._screenshot_svc
        # 优先走 WDA ``/screenshot``：iOS 17+ 最稳，不依赖 tunneld / DDI；
        # 只要 WDA 活着（mirror 一路正常跑）VLM 截图就一定拿得到
        svc = self._open_wda_screenshot_svc()
        # DVT 作为次选：走 tunneld + RSD，需要 DDI 挂好；某些场景下比 WDA 快
        if svc is None:
            svc = self._open_dvt_screenshot_svc()
        # lockdown screenshotr：iOS ≤ 16 fallback，iOS 17+ 会 InvalidService
        if svc is None:
            svc = self._open_lockdown_screenshot_svc()
        if svc is None:
            raise RuntimeError(
                "iOS 截图服务不可用：WDA / DVT / lockdown 都失败了。"
                "检查 WDA 是否跑着（web 镜像能看到就说明 WDA 活着），"
                "或 iOS 17+ 起好 tunneld + 挂 DDI。"
            )
        self._screenshot_svc = svc
        return self._screenshot_svc

    def _open_wda_screenshot_svc(self):
        """走 WDA ``/screenshot``。依赖 WDA 进程已经启动。"""
        try:
            # 先快速探活一把，避免 WDA 没起来时把 _screenshot_svc 记成坏的
            self._wda.status()
        except Exception as exc:  # noqa: BLE001
            logger.debug("udid={} WDA screenshot 探活失败（回退 DVT）：{}", self.serial, exc)
            return None
        logger.info("udid={} 截图通道=WDA(/screenshot)", self.serial)
        return _WdaScreenshotSvc(self._wda)

    def _open_dvt_screenshot_svc(self):
        """走 iOS 17+ 的 DVT Screenshot instrument（via tunneld + RSD）。"""
        try:
            from pymobiledevice3.tunneld.api import (  # noqa: PLC0415
                get_tunneld_device_by_udid,
            )
            from pymobiledevice3.services.dvt.instruments.dvt_provider import (  # noqa: PLC0415
                DvtProvider,
            )
            from pymobiledevice3.services.dvt.instruments.screenshot import (  # noqa: PLC0415
                Screenshot,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("udid={} DVT Screenshot 模块不可用: {}", self.serial, exc)
            return None
        try:
            rsd = _maybe_sync(get_tunneld_device_by_udid(self.serial))
            if rsd is None:
                logger.warning(
                    "udid={} tunneld 没有这台设备；iOS 17+ 请先跑 "
                    "`sudo pymobiledevice3 remote tunneld`",
                    self.serial,
                )
                return None
            provider = DvtProvider(lockdown=rsd)
            _maybe_sync(provider.connect())
            shooter = Screenshot(provider)
            _maybe_sync(shooter.connect())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "udid={} DVT Screenshot 建链失败: {}（DDI 是否挂上？"
                "`pymobiledevice3 mounter auto-mount` 后重试）",
                self.serial, exc,
            )
            return None
        logger.info("udid={} 截图通道=DVT(Screenshot instrument)", self.serial)
        return _DvtScreenshotSvc(provider=provider, shooter=shooter)

    def _open_lockdown_screenshot_svc(self):
        """iOS 16 及以下的 fallback：lockdown ``com.apple.mobile.screenshotr``。"""
        try:
            from pymobiledevice3.services.screenshot import ScreenshotService  # noqa: PLC0415
            svc = ScreenshotService(lockdown=self._lockdown)
            try:
                _maybe_sync(svc.connect())
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("udid={} lockdown screenshotr 不可用: {}", self.serial, exc)
            return None
        logger.info("udid={} 截图通道=lockdown(screenshotr)", self.serial)
        return svc

    def screenshot_png(self) -> bytes:
        svc = self._ensure_screenshot_svc()
        return _maybe_sync(svc.take_screenshot())

    def screenshot_jpeg(self, quality: int = 25, max_side: Optional[int] = None) -> bytes:
        png = self.screenshot_png()
        with Image.open(io.BytesIO(png)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max_side and max(img.size) > max_side:
                ratio = max_side / float(max(img.size))
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()

    # ------------------------------------------------------------------
    # 触控（注意 WDA 接口要 *点*，我们对外是 *像素*，要除回 scale）
    # ------------------------------------------------------------------
    def _px_to_pt(self, x: int, y: int) -> Tuple[float, float]:
        scale = self._get_scale() or 1.0
        return x / scale, y / scale

    def click(self, x: int, y: int) -> None:
        px, py = self._px_to_pt(x, y)
        self._wda.tap(px, py)

    def double_click(self, x: int, y: int, interval_ms: int = 100) -> None:
        # 用 WDA 的原生 double tap，比两次 click 稳
        px, py = self._px_to_pt(x, y)
        try:
            self._wda.double_tap(px, py)
        except WdaError:
            super().double_click(x, y, interval_ms)

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        px, py = self._px_to_pt(x, y)
        self._wda.long_press(px, py, duration_s=max(0.05, duration_ms / 1000.0))

    def swipe(
        self, sx: int, sy: int, ex: int, ey: int, duration_ms: int = 500
    ) -> None:
        psx, psy = self._px_to_pt(sx, sy)
        pex, pey = self._px_to_pt(ex, ey)
        self._wda.swipe(psx, psy, pex, pey, duration_s=max(0.05, duration_ms / 1000.0))

    # ------------------------------------------------------------------
    # 输入 & 按键
    # ------------------------------------------------------------------
    def type_text(self, text: str) -> None:
        """文本输入。

        与 Android (``input text`` 直接 IME 注入) / Harmony (``hmdriver.input_text``)
        的语义对齐：写完字立即收起软键盘，让后续 VLM 决策看到的截图是"键盘
        已落"的状态。iOS WDA 走 IOHIDEvent，每次 type_text 必然弹起软键盘
        且不会自动收，常常遮挡"完成 / 提交"等下方按钮，导致 VLM 后续无法
        点击业务按钮。
        """
        if not text:
            return
        self._wda.type_text(text)
        try:
            self._wda.dismiss_keyboard()
        except Exception as exc:  # noqa: BLE001
            # 极端兜底：老 WDA / 非常规键盘场景失败都不影响输入本身
            logger.debug("[ios] dismiss_keyboard 忽略 udid={}: {}", self.serial, exc)

    def press_home(self) -> None:
        self._wda.press_button("home")

    def press_back(self) -> None:
        """iOS 没原生"返回键"。约定用左边缘向右 swipe 模拟系统级"返回手势"。

        这是 iOS 14+ 在大多数 NavigationController 里通用的返回手势；不是所有
        app 都支持（比如游戏 / 全屏 webview）。如果应用层有自己的返回按钮，
        VLM 该自己判断点哪个区域，不应依赖本方法。
        """
        w, h = self.window_size()
        if w <= 0 or h <= 0:
            return
        sy = h // 2
        self.swipe(2, sy, max(40, int(w * 0.45)), sy, duration_ms=200)

    def press_keycode(self, code: int) -> None:
        """iOS 不支持 Android 风格的 keycode；这里只为 BACK / HOME / APP_SWITCH 做 mapping。"""
        # 与 Android KEYCODE 对齐的少数几个：3=HOME, 4=BACK, 187=APP_SWITCH
        if code == 3:
            self.press_home()
            return
        if code == 4:
            self.press_back()
            return
        if code == 187:
            self.press_app_switch()
            return
        raise NotImplementedError(f"iOS 不支持 keycode={code}")

    def press_app_switch(self) -> None:
        """打开 iOS 的「最近使用的 App / App Switcher」。

        iOS 全面屏（Face ID 机型）的手势是：**从底部中点慢速上滑到约 55%
        屏幕高度，并在那停留约 1 秒**。只上滑不停的话会直接回桌面（等于 HOME）。
        WDA 没有直接 API，用 swipe + 足够长的 duration 来逼近"停住"效果：

        - 起点：底部中间 (w/2, h-1)
        - 终点：上 55% (w/2, int(h*0.55))
        - duration 1200ms——关键：慢速才会进 App Switcher，快速就变回 Home

        iOS ≤ 16 有 Home 键的老机型没这个手势；实测 iPhone 8 一类需要改为
        双击 Home。目前默认只支持 Face ID 机型的手势。
        """
        w, h = self.window_size()
        if w <= 0 or h <= 0:
            logger.warning("[ios] press_app_switch 拿不到屏幕尺寸，退化为 HOME")
            self.press_home()
            return
        sx = w // 2
        sy = max(0, h - 1)
        ey = int(h * 0.55)
        # 1200ms 慢速上滑，经验值，短于 800ms 会触发"回桌面"
        self.swipe(sx, sy, sx, ey, duration_ms=1200)

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------
    def list_third_party_packages(self) -> List[str]:
        return list(self._list_app_records(application_type="User"))

    def list_all_packages(self) -> List[str]:
        return list(self._list_all_app_records())

    def list_installed_apps(self) -> List[InstalledApp]:
        """动态读取 iOS 当前应用的显示名与 bundle ID。

        ``installation_proxy`` 返回 ``bundle_id -> metadata``；苹果将
        ``CFBundleDisplayName`` 定义为主屏图标下的用户可见名称，缺失时使用
        ``CFBundleName``。两者都由当前设备 App 自己提供，不接受任何外部映射。
        """
        if self._installed_apps_cache is not None:
            return list(self._installed_apps_cache)

        records = self._list_all_app_records()
        apps = [
            InstalledApp(
                display_name=self._display_name_from_record(bundle_id, metadata),
                package_name=bundle_id,
            )
            for bundle_id, metadata in records.items()
        ]
        self._installed_apps_cache = apps
        return list(apps)

    def _list_all_app_records(self) -> Dict[str, Dict[str, Any]]:
        """分段读取 User/System，避免 ``Any`` 在部分低版本设备上失效。"""
        records: Dict[str, Dict[str, Any]] = {}
        errors: List[str] = []
        succeeded = False
        for application_type in ("User", "System"):
            try:
                part = self._list_app_records(application_type=application_type)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{application_type}: {type(exc).__name__}: {exc}")
                logger.warning(
                    "iOS list_all_packages 分段失败 udid={} type={}: {}",
                    self.serial,
                    application_type,
                    exc,
                )
                continue
            succeeded = True
            records.update(part)

        if succeeded:
            return records

        detail = "; ".join(errors) if errors else "no result"
        raise RuntimeError(
            f"iOS 列应用失败 udid={self.serial} type=User/System: {detail}"
        )

    @staticmethod
    def _display_name_from_record(bundle_id: str, metadata: Dict[str, Any]) -> str:
        for key in ("CFBundleDisplayName", "CFBundleName"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return bundle_id

    def _list_apps(self, *, application_type: str) -> List[str]:
        """iOS 取已装应用 bundle_id 列表。"""
        return list(self._list_app_records(application_type=application_type))

    def _list_app_records(self, *, application_type: str) -> Dict[str, Dict[str, Any]]:
        """iOS 取已装应用的 ``bundle_id -> metadata`` 目录。

        iOS 17+ 把 ``com.apple.mobile.installation_proxy`` 列为 trusted lockdown
        service：USB usbmuxd lockdown 通道直接 connect 会被 ``NotPairedError``
        打回，必须走 tunneld 提供的 RemoteServiceDiscovery（RSD）lockdown。
        本函数策略：

        1. **优先**走 tunneld + RSD（iOS 17+ 唯一可行通道）
        2. RSD 不可用时回落到 usbmux fresh lockdown 短连接（兼容 iOS 16 /
           没起 tunneld 的环境，同时避免复用已老化的 ``self._lockdown``）
        3. 全部失败时**不再吞异常返回空列表**，而是带原因 raise RuntimeError，
           交由 vlm_loop 上层翻成「执行失败」RunLog，避免前端只看到含糊的
           「无法获取设备应用列表」却不知道该开 tunneld。

        附注：实测同一台 iOS 17+ 设备上，tunneld+RSD 与 usbmux 两路通道返回的
        app 集合是一致的（差集为 0），所以不再做"两路合并去重"——多一次 IPC
        没有收益，反而拖慢 close_app/open_app 起跑线。
        """
        last_exc: Optional[BaseException] = None

        rsd = self._try_get_tunneld_rsd()
        if rsd is not None:
            try:
                apps = self._fetch_app_records_via_lockdown(rsd, application_type)
                logger.info(
                    "iOS list_apps udid={} type={} via=tunneld+RSD count={}",
                    self.serial,
                    application_type,
                    len(apps),
                )
                return apps
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "iOS list_apps via tunneld+RSD 失败 udid={} type={}: {}",
                    self.serial,
                    application_type,
                    exc,
                )
            finally:
                try:
                    _maybe_sync(rsd.close())
                except Exception:  # noqa: BLE001
                    pass

        fresh_lockdown = None
        try:
            fresh_lockdown = self._open_fresh_lockdown_for_app_listing()
            apps = self._fetch_app_records_via_lockdown(fresh_lockdown, application_type)
            logger.info(
                "iOS list_apps udid={} type={} via=usbmux/fresh-lockdown count={}",
                self.serial,
                application_type,
                len(apps),
            )
            return apps
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        finally:
            if fresh_lockdown is not None:
                self._close_lockdown(fresh_lockdown)

        hint = ""
        exc_name = type(last_exc).__name__ if last_exc is not None else "未知"
        if "NotPaired" in exc_name:
            hint = (
                "（iOS 17+ 需要 tunneld：在另一终端跑 "
                "`sudo pymobiledevice3 remote tunneld` 并在 iPhone 上完成"
                " Remote Pairing 确认弹窗，必要时先在 设置 → 隐私与安全性 → "
                "开发者模式 中打开 Developer Mode）"
            )
        raise RuntimeError(
            f"iOS 列应用失败 udid={self.serial} type={application_type}: "
            f"{exc_name}: {last_exc}{hint}"
        )

    def _open_fresh_lockdown_for_app_listing(self):
        """为 installation_proxy 列应用创建短生命周期 lockdown 连接。

        ``open_ios_driver`` 创建的 ``self._lockdown`` 可能在长时间运行后被
        iPhone/usbmuxd 关闭。列 App 是低频操作，使用短连接比复用旧连接更稳。
        """
        _, create_using_usbmux, _, _ = _import_pmd3()
        try:
            return _maybe_sync(create_using_usbmux(serial=self.serial, autopair=False))
        except TypeError as exc:
            if "autopair" not in str(exc):
                raise
            return _maybe_sync(create_using_usbmux(serial=self.serial))

    def _close_lockdown(self, lockdown) -> None:  # noqa: ANN001
        close_fn = getattr(lockdown, "close", None)
        if not callable(close_fn):
            return
        try:
            _maybe_sync(close_fn())
        except Exception:  # noqa: BLE001
            pass

    def _try_get_tunneld_rsd(self):
        """尝试从 tunneld 拿到 RSD device。失败一律返回 None（让上层走回落）。

        失败按 ``DEBUG`` 级别记录，不打 warning：tunneld 没启动是合法状态
        （iOS 16 / 用户暂未配置），不应该刷 warning 日志干扰排查。
        """
        try:
            from pymobiledevice3.tunneld.api import (  # noqa: PLC0415
                get_tunneld_device_by_udid,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("udid={} pmd3 tunneld API 不可用: {}", self.serial, exc)
            return None
        try:
            rsd = _maybe_sync(get_tunneld_device_by_udid(self.serial))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "udid={} 查询 tunneld 失败（未起 tunneld？）: {}",
                self.serial,
                exc,
            )
            return None
        if rsd is None:
            logger.debug(
                "udid={} tunneld 没有这台设备；iOS 17+ 请先跑 "
                "`sudo pymobiledevice3 remote tunneld`",
                self.serial,
            )
        return rsd

    def _fetch_app_records_via_lockdown(
        self,
        lockdown,  # noqa: ANN001
        application_type: str,
    ) -> Dict[str, Dict[str, Any]]:
        """通过官方 installation_proxy 保留完整应用元数据。"""
        from pymobiledevice3.services.installation_proxy import (  # noqa: PLC0415
            InstallationProxyService,
        )
        ip = InstallationProxyService(lockdown=lockdown)
        _maybe_sync(ip.connect())
        try:
            apps = _maybe_sync(ip.get_apps(application_type=application_type)) or {}
        finally:
            try:
                _maybe_sync(ip.close())
            except Exception:  # noqa: BLE001
                pass
        return {
            str(bundle_id): metadata if isinstance(metadata, dict) else {}
            for bundle_id, metadata in apps.items()
            if bundle_id
        }

    def activate_app(self, package_name: str) -> None:
        self._wda.launch_app(package_name)

    def terminate_app(self, package_name: str) -> None:
        """命令级杀进程：优先 DVT ProcessControl，DVT 不可用时回落 WDA terminate。

        WDA 的 ``POST /wda/apps/terminate`` 在 iOS 17+ / 18 / 26 上对**前台 app**
        经常返回 success 但 SpringBoard 静默拒绝（API 行为，不是 bug），表现是
        close_app 日志看着成功、屏幕没变、VLM 反复重试到 case 终止。

        所以**首选**走 DVT 的 ``ProcessControl`` instrument 直接 kill 进程，
        等同 Xcode Instruments / iOS Simulator 杀 app 的官方通道，命令级执行，
        不依赖 SpringBoard 拒绝/同意：

        1. 拿 tunneld 提供的 RSD lockdown（iOS 17+ 唯一可走的 DVT 通道）
        2. 起 DvtProvider + ProcessControl（需要 DDI 已挂在设备上）
        3. ``process_identifier_for_bundle_identifier(bundle)`` 拿 pid
           - pid <= 0：进程不在跑，直接当成功（语义对齐 force-stop）
           - pid > 0：``kill(pid)`` 真杀
        4. 释放 ProcessControl / DvtProvider / RSD

        但 iOS 15 / 16 这类**没有 RSD/DVT 通道**的低版本设备拿不到 tunneld，
        DVT 路径根本走不通；这些设备上 WDA ``/wda/apps/terminate`` 实测可靠
        （iPad6 / iPhone iOS 15.7 已验证）。因此 DVT 前提缺失（tunneld 没起 /
        DDI 未挂 / 模块或通道异常）时**回落到 WDA terminate**，而不是直接判失败。
        回落后用 ``current_app`` 复核前台是否真的切走，避免重新踩到 iOS 17+
        "静默成功" 的老坑。
        """
        rsd = self._try_get_tunneld_rsd()
        if rsd is None:
            # iOS 15/16 无 RSD/DVT 通道：tunneld 必然为空，直接走 WDA 回落
            logger.info(
                "iOS terminate_app: tunneld 不可用，回落 WDA terminate "
                "udid={} bundle={}（iOS 15/16 无 DVT 通道，属正常）",
                self.serial, package_name,
            )
            self._terminate_app_via_wda(package_name)
            return

        try:
            try:
                from pymobiledevice3.services.dvt.instruments.dvt_provider import (  # noqa: PLC0415
                    DvtProvider,
                )
                from pymobiledevice3.services.dvt.instruments.process_control import (  # noqa: PLC0415
                    ProcessControl,
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"pymobiledevice3 DVT ProcessControl 模块不可用：{exc}"
                ) from exc

            provider = DvtProvider(lockdown=rsd)
            _maybe_sync(provider.connect())
            pc = ProcessControl(provider)
            _maybe_sync(pc.connect())
            try:
                pid_raw = _maybe_sync(
                    pc.process_identifier_for_bundle_identifier(package_name)
                )
                pid = int(pid_raw or 0)
                if pid <= 0:
                    logger.info(
                        "iOS terminate_app: 进程未在跑，视为成功 udid={} bundle={}",
                        self.serial, package_name,
                    )
                    return
                _maybe_sync(pc.kill(pid))
                logger.info(
                    "iOS terminate_app: 已 kill udid={} bundle={} pid={}",
                    self.serial, package_name, pid,
                )
            finally:
                try:
                    _maybe_sync(pc.close())
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            # DVT 通道异常（模块缺失 / DDI 未挂 / 连接失败等）：回落 WDA terminate。
            # 高版本设备走到这里说明 DVT 本就不通，WDA 是唯一兜底；低版本设备本就靠 WDA。
            hint = ""
            if "DDI" in str(exc) or "DeveloperDiskImage" in str(exc) or "PersonalizedImage" in str(exc):
                hint = (
                    "（DDI 似乎没挂上：跑 `sudo pymobiledevice3 mounter "
                    "auto-mount --udid <udid>` 一次；重启手机或电脑后需要重挂）"
                )
            logger.warning(
                "iOS terminate_app: DVT 路径失败，回落 WDA terminate "
                "udid={} bundle={}: {}: {}{}",
                self.serial, package_name, type(exc).__name__, exc, hint,
            )
            self._terminate_app_via_wda(package_name)
        finally:
            try:
                _maybe_sync(rsd.close())
            except Exception:  # noqa: BLE001
                pass

    def _terminate_app_via_wda(self, package_name: str) -> None:
        """回落路径：WDA ``POST /wda/apps/terminate`` + 前台复核。

        iOS 15 / 16 没有 DVT 通道，这是唯一可靠的命令级关闭路径，实测可用。
        iOS 17+ 上 WDA terminate 对前台 app 可能返回 success 但被 SpringBoard
        静默拒绝，所以杀完用 ``current_app`` 复核：若目标 app 仍在前台 → 判失败，
        由上层翻成「执行失败」RunLog，避免假成功让 VLM 空转。
        """
        try:
            self._wda.terminate_app(package_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"iOS terminate_app 回落 WDA terminate 失败 udid={self.serial} "
                f"bundle={package_name}: {type(exc).__name__}: {exc}"
            ) from exc

        # 给 SpringBoard 一点切换时间再复核前台，避免刚下发就读到旧前台
        time.sleep(0.5)
        try:
            front = self.current_app()
        except Exception:  # noqa: BLE001
            # current_app 自身异常不应阻断：terminate 已下发，按成功处理
            front = ""
        if front == package_name:
            raise RuntimeError(
                f"iOS terminate_app 回落 WDA terminate 后目标仍在前台 "
                f"udid={self.serial} bundle={package_name}：可能被 SpringBoard "
                "静默拒绝（iOS 17+ 常见）；请起 tunneld 并挂 DDI 改走 DVT 通道"
            )
        logger.info(
            "iOS terminate_app: WDA terminate 成功 udid={} bundle={}",
            self.serial, package_name,
        )

    def current_app(self) -> str:
        try:
            info = self._wda.active_app() or {}
            return str(info.get("bundleId") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    def device_info(self) -> DeviceInfo:
        def _get(key: str, default: str = "") -> str:
            try:
                return str(_maybe_sync(self._lockdown.get_value(key=key)) or default)
            except Exception:
                return default

        brand = "Apple"
        model = _get("ProductType") or _get("HardwareModel") or _get("DeviceClass")
        os_version = _get("ProductVersion")
        # 走 WDA 拿尺寸（已是物理像素）
        w, h = self.window_size()
        return DeviceInfo(
            serial=self.serial,
            platform=self.platform,
            brand=brand,
            model=model,
            os_version=os_version,
            screen_width=w,
            screen_height=h,
            status="online",
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        # 先从全局映射里摘掉，避免 mirror 等模块在 driver 关闭后还拿到个失效 client
        with _PORT_ALLOC_LOCK:
            _WDA_CLIENT_MAP.pop(self.serial, None)
        try:
            self._wda.close()
        except Exception:  # noqa: BLE001
            pass
        # 截图服务（DVT 或 lockdown）在这里统一释放；DVT 路径下 provider 是
        # 长连接的 USB socket，不关会留悬挂 DTX reader
        if self._screenshot_svc is not None:
            try:
                close_fn = getattr(self._screenshot_svc, "close", None)
                if callable(close_fn):
                    res = close_fn()
                    if res is not None:
                        _maybe_sync(res)
            except Exception:  # noqa: BLE001
                pass
            self._screenshot_svc = None
        if self._forwarder is not None:
            try:
                self._forwarder.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._launcher is not None:
            try:
                self._launcher.stop()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 设备发现 + 上线
# ---------------------------------------------------------------------------
# 最近一次 list_ios_devices 是否拿到了"可信的 USB 扫描结果"。
#
# 背景：macOS 上 usbmuxd 偶尔会抖一下，常见两种症状：
#   1. ``usbmux.list_devices()`` 抛异常 / 超时
#   2. **更隐蔽**：调用成功、返回 ``[]`` 空列表（socket 正常，只是 usbmuxd
#      内部状态短暂不返回设备），常见持续几百毫秒到几秒
# 在 stable 模式下，agent 是根据"udid 是否还在扫描结果里"来判断"USB 是否
# 拔出"的；如果一次扫描抖动被当成"全部拔出"，会误清 spawn 状态机里的"已
# spawn 标记"，下一次成功扫描时所有 udid 又被视为"新插入" → agent 又允许
# spawn 一次 WDA，打破方案 §6.1 "只有物理拔插才能让 agent 重新触碰
# WDA/XCTest"的核心承诺。
#
# 因此对外提供一个布尔信号 ``was_last_ios_scan_ok()``，**只有"扫描结果是
# 可信的关于 USB 上有/没有 iOS 设备的判定"**才是 True。可信的两种情形：
#   * 本轮看到 ≥1 个 udid（无歧义，肯定可信）
#   * 本轮空列表 + 上一轮也是空（持续空稳定）
#   * 本轮空列表 + 上轮有 udid + **连续 _EMPTY_STREAK_THRESHOLD 次确认**
#     才认定"真拔出"——单次空被视为抖动，外层不更新拔插会话状态
# 任何 ``list_devices()`` 抛异常、pmd3 ImportError 都直接标 False。
#
# 注意：ImportError（pmd3 没装）也算"不可信"，但因为 iOS 平台禁用是稳态、
# 列表恒为空、``_last_seen_ios_serials`` 也恒为空，实际不会触发误清；标 False
# 只是为了语义干净。
_IOS_SCAN_LAST_OK: bool = False
_IOS_SCAN_LOCK = threading.Lock()

# 上一轮"被认定可信"的扫描看到的 udid 集合。用于判断"本轮空列表是持续空
# 还是从有到无"。注意它与 ``main.py`` 的 ``_last_seen_ios_serials`` 不同：
# 后者是"上次 _emit_ios_disconnect_events 看到的可信 udid"，前者是本模块
# 内部的扫描状态机变量。两者分别维护，互不耦合。
_IOS_LAST_GOOD_SCAN_UDIDS: Set[str] = set()
# 连续空扫描次数计数。只在"上轮有 udid + 本轮空"的转换里递增；任意一次
# 非空扫描都清零。达到阈值后才把空列表当作"真拔出"。
_IOS_SCAN_EMPTY_STREAK: int = 0
# 需要连续多少次空才认定真拔出。2 次即可——rescan 间隔约几秒，真拔出延迟
# 一轮被识别完全可以接受；usbmuxd 抖动很少连续两轮都恰好空。
_EMPTY_STREAK_THRESHOLD = 2


def was_last_ios_scan_ok() -> bool:
    """返回最近一次 ``list_ios_devices`` 是否拿到可信扫描结果。

    用法见 ``_IOS_SCAN_LAST_OK`` 文档。**仅用于"是否可以根据扫描结果更新
    USB 插拔会话状态"这一判断**，不要拿它做别的策略。
    """
    with _IOS_SCAN_LOCK:
        return _IOS_SCAN_LAST_OK


def _set_ios_scan_ok(ok: bool) -> None:
    global _IOS_SCAN_LAST_OK
    with _IOS_SCAN_LOCK:
        _IOS_SCAN_LAST_OK = bool(ok)


def _update_ios_scan_streak(raw_udids: Set[str]) -> None:
    """根据本轮原始 udid 集合更新空扫描计数 + scan_ok 信号。

    决策表（详见 ``_IOS_SCAN_LAST_OK`` 文档头）：

    ============= =================  =====================================
    上轮有 udid   本轮 raw_udids     scan_ok / 状态变迁
    ============= =================  =====================================
    -             非空               True，清空 streak，更新 last_good
    False         空                 True（持续空稳定），streak 保持 0
    True          空（streak<阈值-1） False（疑似抖动，外层跳过 disconnect）
    True          空（streak≥阈值-1） True（确认真拔出），清 last_good
    ============= =================  =====================================
    """
    global _IOS_LAST_GOOD_SCAN_UDIDS, _IOS_SCAN_EMPTY_STREAK, _IOS_SCAN_LAST_OK
    with _IOS_SCAN_LOCK:
        if raw_udids:
            _IOS_SCAN_EMPTY_STREAK = 0
            _IOS_LAST_GOOD_SCAN_UDIDS = set(raw_udids)
            _IOS_SCAN_LAST_OK = True
            return
        if not _IOS_LAST_GOOD_SCAN_UDIDS:
            # 上轮也空 → 持续空稳定状态，可信
            _IOS_SCAN_EMPTY_STREAK = 0
            _IOS_SCAN_LAST_OK = True
            return
        # 上轮有 udid，本轮空 → 进入"疑似抖动 / 待确认拔出"窗口
        _IOS_SCAN_EMPTY_STREAK += 1
        if _IOS_SCAN_EMPTY_STREAK >= _EMPTY_STREAK_THRESHOLD:
            # 连续 N 次空，认定真拔出
            confirmed_count = len(_IOS_LAST_GOOD_SCAN_UDIDS)
            _IOS_LAST_GOOD_SCAN_UDIDS = set()
            _IOS_SCAN_EMPTY_STREAK = 0
            _IOS_SCAN_LAST_OK = True
            logger.info(
                "iOS USB 扫描连续 {} 次返回空 → 认定 {} 个 udid 真拔出，"
                "将由 disconnect 钩子清空对应 stable spawn 标记",
                _EMPTY_STREAK_THRESHOLD, confirmed_count,
            )
            return
        # 第一次空：疑似 usbmuxd 抖动，标不可信，让外层完全跳过
        # 本轮 disconnect 判定（不更新 _last_seen_ios_serials）
        _IOS_SCAN_LAST_OK = False
        logger.debug(
            "iOS USB 扫描返回空但上轮有 {} 个 udid，疑似 usbmuxd 抖动，"
            "本轮标不可信（streak={}/{}）；下一轮再确认",
            len(_IOS_LAST_GOOD_SCAN_UDIDS),
            _IOS_SCAN_EMPTY_STREAK,
            _EMPTY_STREAK_THRESHOLD,
        )


def _reset_ios_scan_state_for_tests() -> None:
    """测试钩子：清空扫描状态机的所有变量。仅供单测使用。"""
    global _IOS_LAST_GOOD_SCAN_UDIDS, _IOS_SCAN_EMPTY_STREAK, _IOS_SCAN_LAST_OK
    with _IOS_SCAN_LOCK:
        _IOS_LAST_GOOD_SCAN_UDIDS = set()
        _IOS_SCAN_EMPTY_STREAK = 0
        _IOS_SCAN_LAST_OK = False


def list_ios_devices(include_offline: bool = False) -> List[DeviceInfo]:
    """扫描 USB 上的 iOS 设备，返回 ``DeviceInfo`` 列表。

    不实际打开 WDA / 端口转发，只读 lockdown 里的元信息。WDA 那一步在
    ``open_ios_driver`` 时才做，避免每次设备扫描都启动 WDA。

    本函数同时维护模块级的 ``_IOS_SCAN_LAST_OK`` 信号——见
    ``was_last_ios_scan_ok`` 文档；stable 模式下的 USB 拔插会话状态机依赖
    它防止 usbmux 抖动（包括"成功返回空列表"这种隐蔽抖动）被误判为"全部
    拔出"。
    """
    try:
        usbmux, create_using_usbmux, _, _ = _import_pmd3()
    except ImportError as exc:
        # pmd3 没装 = iOS 平台禁用稳态。标 False 让上层 disconnect 钩子也不
        # 误以为这是"一次成功的空扫描"；实际 _last_seen_ios_serials 也恒为空，
        # 没什么状态可清，但语义保持一致更省心。
        _set_ios_scan_ok(False)
        logger.debug("跳过 iOS 设备扫描：{}", exc)
        return []

    infos: List[DeviceInfo] = []
    try:
        # pmd3 9.x: list_devices 是 async；老版是 sync。统一过 _maybe_sync
        devices = _maybe_sync(usbmux.list_devices()) or []
    except Exception as exc:  # noqa: BLE001
        # usbmux 抛异常：标 False 后**立刻 return**，让上层完全跳过本轮的
        # disconnect 钩子，绝不更新 _last_seen_ios_serials。下一轮 rescan
        # 成功后再恢复正常。
        _set_ios_scan_ok(False)
        logger.warning("usbmux list_devices 失败：{}", exc)
        return []

    # 只认 USB 物理连接的设备：usbmux 也会列出"过去配对、现在同 WiFi 网络可达"的设备
    # （connection_type='Network'），那不是本机插着的真机，排除掉——否则设备总览会冒出
    # 一台连不上 WDA 的"幽灵 iOS 设备"。本函数语义本就是"扫描 USB 上的 iOS 设备"。
    devices = [
        d
        for d in devices
        if str(getattr(d, "connection_type", "") or "").strip().lower() != "network"
    ]

    # 关键修复（Codex P1 二次审查）：成功调用 + 空列表的隐蔽抖动也得防。
    # 把"原始 udid 集合"先抽出来喂给扫描状态机，由后者决定本轮是否可信。
    # 注意：这里只看 raw_udids 而不是后面的 infos——infos 还会因为 lockdown
    # 抽风而退化成 offline/unauthorized，跟"USB 上是否物理在场"是两回事。
    raw_udids: Set[str] = set()
    for dev in devices:
        udid = getattr(dev, "serial", None) or getattr(dev, "udid", None)
        if udid:
            raw_udids.add(str(udid))
    _update_ios_scan_streak(raw_udids)

    for dev in devices:
        udid = getattr(dev, "serial", None) or getattr(dev, "udid", None)
        if not udid:
            continue
        try:
            # pmd3 9.x: create_using_usbmux 是 async；4.x 是 sync。
            # 设备总览的后台 rescan 必须是只读探测，不能触发 iOS pairing 流程；
            # 否则 pair record 写入异常（如 SavePairRecordFailed）时，agent 会每轮
            # 扫描都把“信任此电脑”弹窗重新打出来。真正需要用户交互的配对留给
            # open_ios_driver / Xcode 等显式启动链路处理。
            ld = _maybe_sync(create_using_usbmux(serial=udid, autopair=False))
        except Exception as exc:  # noqa: BLE001
            # iOS 18/26 起，锁屏状态下连 StartSession 也会返回 PasswordProtected，
            # 即使 pair record 里有 EscrowBag。这是 iOS 本身的限制，不是配对问题。
            # 策略（按优先级）：
            #   1. WDA 活着 → 设备事实可用，直接标 online，用 cache + WDA 补元信息
            #   2. 之前成功读过元信息 → 沿用缓存 + status=locked，卡片不消失
            #   3. 首次插入 + 锁屏 / 未信任 → unauthorized + 原因提示
            msg = str(exc)
            low = msg.lower()
            is_locked = ("password" in low) or ("pairingdialog" in low)
            is_trust = ("pair" in low and "password" not in low) or ("trust" in low)

            cached = _IOS_META_CACHE.get(udid)

            # Fast path：WDA 已就绪就不该被 lockdown 拦在门外
            if _wda_alive(udid):
                wda_info: Dict[str, Any] = {}
                try:
                    cli = _WDA_CLIENT_MAP.get(udid)
                    if cli is not None:
                        wda_info = cli.device_info() or {}
                except Exception:  # noqa: BLE001
                    wda_info = {}

                model = ((cached or {}).get("model") or wda_info.get("name")
                         or wda_info.get("model") or "")
                os_ver = ((cached or {}).get("os_version")
                          or wda_info.get("systemVersion") or "")
                sw = int((cached or {}).get("screen_width") or 0)
                sh = int((cached or {}).get("screen_height") or 0)

                info = DeviceInfo(
                    serial=udid,
                    platform="ios",
                    brand="Apple",
                    model=str(model),
                    os_version=str(os_ver),
                    screen_width=sw,
                    screen_height=sh,
                    status="online",
                )
                # 刷一下缓存快照
                _IOS_META_CACHE[udid] = {
                    "serial": udid,
                    "platform": "ios",
                    "brand": "Apple",
                    "model": info.model,
                    "os_version": info.os_version,
                    "screen_width": info.screen_width,
                    "screen_height": info.screen_height,
                }
                infos.append(info)
                logger.debug(
                    "iOS udid={} lockdown 抽风但 WDA 活着，按 online 上报（err={}）",
                    udid, exc,
                )
                continue

            if is_locked and cached is not None:
                info = DeviceInfo(**{**cached, "status": "locked"})
                info.extra = {"reason": "iPhone 当前锁屏：点亮屏幕 + Face ID/密码解锁即可恢复"}
                infos.append(info)
                logger.debug("iOS udid={} 锁屏，沿用缓存元信息上报", udid)
                continue

            if is_locked:
                status = "unauthorized"
                reason = (
                    "iPhone 锁屏：请解锁屏幕 + 保持亮屏（建议「设置→显示与亮度→自动锁定→永不」）"
                )
            elif is_trust:
                status = "unauthorized"
                reason = "iPhone 未信任本电脑：请解锁 iPhone，并在弹窗点「信任此电脑」"
            else:
                status = "offline"
                reason = msg
            infos.append(
                DeviceInfo(
                    serial=udid,
                    platform="ios",
                    brand="Apple",
                    status=status,
                    extra={"reason": reason},
                )
            )
            logger.warning("iOS lockdown 连接失败 udid={}: {}", udid, exc)
            continue

        def _get(key: str, default: str = "") -> str:
            try:
                return str(_maybe_sync(ld.get_value(key=key)) or default)
            except Exception:
                return default

        # 这里的 screen_width/height 走 lockdown 兜底（不一定准；WDA 起来后会刷新）
        try:
            sw = int(_maybe_sync(ld.get_value(domain="com.apple.mobile.iTunes", key="ScreenWidth")) or 0)
            sh = int(_maybe_sync(ld.get_value(domain="com.apple.mobile.iTunes", key="ScreenHeight")) or 0)
        except Exception:
            sw = sh = 0

        os_ver = _get("ProductVersion")
        # iOS 17+ 大量 service（截图 / dvt / DDI）走 RemoteXPC，必须先开 Developer Mode
        # 这里只是日志提示，不阻断列表返回
        try:
            major = int((os_ver or "0").split(".", 1)[0])
        except Exception:
            major = 0
        if major >= 17:
            dev_mode_on = _check_developer_mode(ld)
            if not dev_mode_on:
                logger.warning(
                    "iOS {} 设备 udid={} 未开启 Developer Mode；截图 / WDA 自动启动 / DDI 全部不可用。"
                    "请在 iPhone：设置 → 隐私与安全性 → 开发者模式 → 打开（需重启）。",
                    os_ver, udid,
                )

        info = DeviceInfo(
            serial=udid,
            platform="ios",
            brand="Apple",
            model=_get("ProductType") or _get("DeviceClass"),
            os_version=os_ver,
            screen_width=sw,
            screen_height=sh,
            status="online",
        )
        # 把成功拿到的元信息存一份快照，下次锁屏时复用（避免卡片消失）
        _IOS_META_CACHE[udid] = {
            "serial": udid,
            "platform": "ios",
            "brand": "Apple",
            "model": info.model,
            "os_version": info.os_version,
            "screen_width": info.screen_width,
            "screen_height": info.screen_height,
        }
        infos.append(info)
    return infos


def _check_developer_mode(lockdown) -> bool:  # noqa: ANN001
    """探测 Developer Mode 是否已开。失败默认返回 ``True``——只是个友好提示，
    探测失败别因此把整台设备判定为不可用。

    pmd3 4.x 上稳定的查法是 ``MobileImageMounterService.query_developer_mode_status()``。
    """
    try:
        from pymobiledevice3.services.mobile_image_mounter import (  # noqa: PLC0415
            MobileImageMounterService,
        )
        svc = MobileImageMounterService(lockdown=lockdown)
        # pmd3 9.x：要先 connect；4.x 是同步且 connect 在 ctor 里
        try:
            _maybe_sync(svc.connect())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Developer Mode 状态探测连接失败：{}", exc)
            return True
        try:
            return bool(_maybe_sync(svc.query_developer_mode_status()))
        finally:
            try:
                _maybe_sync(svc.close())
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("Developer Mode 状态探测失败：{}", exc)
    return True


def _ios_three_layer_self_check(udid: str, wda: WdaClient) -> None:
    """WDA 三层可用性自检，对应 ``IOS_WDA_XCODE_RUNBOOK`` 方向 C。

    目的是在 agent 上线前**排除假阳性**：WDA HTTP 通了不代表控制链真的活。
    历史上踩过 ``/status`` 返回 ready 但 ``/session`` 建不起来（XCTest
    runner 已死但 HTTP 服务还在）、``create_session`` 成功但所有
    ``/session/<sid>/...`` 子接口全 404 的情况。

    三层判断：
      L1. ``/status``     → ready（端口通 + XCTest runner 活）
      L2. ``/session``    → 拿到 sessionId（控制会话可建）
      L3. ``/window/size``→ 读到非 0 尺寸（session 可调用子接口）

    **刻意不做真实 tap/swipe**——自检阶段触摸屏幕会误点桌面图标或弹窗，
    对用户不友好。``window/size`` 已经能证明控制链活且无副作用。
    """
    # L1 wait_ready 已经做过了，这里不重复。直接进 L2。
    try:
        sid = wda.create_session()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"iOS 三层自检 L2 失败 udid={udid}：/session 建立失败 - {exc}\n"
            f"→ 常见原因：XCTest runner 已经退出（手机上 Automation Running 消失？）\n"
            f"  或 iOS 系统弹了未处理的权限框把 WDA 挡住"
        ) from exc

    try:
        size = wda.window_size()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"iOS 三层自检 L3 失败 udid={udid}：/window/size 读取失败 - {exc}\n"
            f"→ session {sid} 建立成功但调子接口 404，控制链实际不活"
        ) from exc

    if size.width <= 0 or size.height <= 0:
        raise RuntimeError(
            f"iOS 三层自检 L3 失败 udid={udid}：/window/size 返回空尺寸 {size}"
        )

    logger.info(
        "iOS 三层自检通过 udid={} sid={} size={}x{} point",
        udid, sid, size.width, size.height,
    )


def open_ios_driver(
    udid: str,
    wait_wda_s: Optional[float] = None,
    on_status: Optional[Any] = None,
) -> IosDriver:
    """根据 udid 打开一个 IosDriver。

    启动链（2026-04 重构）：
        1. lockdown 连接（读设备元信息）
        2. 端口分配 + usbmux 端口转发（绕过 iproxy）
           - 若本地端口已被占且指向 WDA（用户在跑 iproxy）→ 复用，不起 forwarder
        3. ``IosWdaXcodeLauncher.start()``：attach（已有 WDA） / spawn（xcodebuild test） / disabled
        4. ``WdaClient.wait_ready`` 轮询 /status
        5. 三层自检（/status → /session → /window/size）
        6. 返回 ``IosDriver`` 实例；close() 时连带停 forwarder + launcher
    """
    settings = get_settings()
    timeout = wait_wda_s if wait_wda_s is not None else float(settings.wda_startup_timeout_s)

    usbmux, create_using_usbmux, _, _ = _import_pmd3()
    try:
        ld = _maybe_sync(create_using_usbmux(serial=udid))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"iOS lockdown 连接失败 udid={udid}: {exc}") from exc

    local_port = _alloc_local_port(udid)
    forwarder: Optional[_UsbmuxPortForwarder] = _UsbmuxPortForwarder(udid, local_port)
    try:
        forwarder.start()
    except Exception as exc:
        # 端口被占 → 看是不是用户的 iproxy 已经指向 WDA；是就复用
        if _probe_wda_http(local_port, timeout_s=0.8):
            logger.info(
                "udid={} 本地 127.0.0.1:{} 已被占且响应 WDA，"
                "推测是用户在跑 iproxy/手工转发 → 复用，不再起内置 forwarder",
                udid, local_port,
            )
            forwarder = None
        else:
            raise RuntimeError(
                f"iOS 端口转发启动失败 udid={udid} local={local_port}: {exc}"
            ) from exc

    # iOS WDA 生命周期策略：launcher 本身不感知 auto/stable，只读布尔。
    # auto 下 allow_runtime_drop_respawn/allow_preflight_deadlock_respawn 都为 True，
    # 行为与本字段引入前完全等价；stable 下两个开关都为 False，preflight 死锁只
    # 刷 need_unlock 提示、runtime_drop 只发 device_status 让浏览器报错等人工。
    # 详见 docs/ios-setup（iOS接入指南）.md。
    from .ios_wda_lifecycle import (  # noqa: PLC0415
        StableWdaUnavailable,
        get_ios_wda_lifecycle_policy,
    )

    policy = get_ios_wda_lifecycle_policy()
    launcher = IosWdaXcodeLauncher(
        udid=udid,
        project_dir=settings.wda_project_dir,
        scheme=settings.wda_scheme,
        local_probe_port=local_port,
        on_status=on_status,
        bundle_id=settings.wda_bundle_id,
        team_id=settings.wda_team_id,
        allow_runtime_drop_respawn=policy.allow_runtime_drop_respawn(),
        allow_preflight_deadlock_respawn=policy.allow_preflight_deadlock_respawn(),
    )
    # §7.5.1 spawn 状态机：auto 永远允许；stable 下本次"USB 插入会话"内最多一次
    # （或严格 attach-only 子方案下永远不允许）。launcher 只读布尔，决策在 policy。
    spawn_allowed = policy.allow_spawn(udid, reason="open_driver")
    mode = launcher.start(allow_spawn=spawn_allowed)
    logger.info(
        "udid={} WDA launcher 模式={} local_port={} lifecycle={} spawn_allowed={}",
        udid, mode, local_port, policy.mode.value, spawn_allowed,
    )
    # stable + 本次会话已 spawn 过 + 当前 attach 又失败 → 真挂，让上层显式上报浏览器，
    # 而不是在 wait_ready 卡 timeout 后吐一坨 launcher disabled 文案。
    if (
        policy.is_stable
        and mode == "disabled"
        and not spawn_allowed
    ):
        if forwarder is not None:
            forwarder.stop()
        launcher.stop()
        raise StableWdaUnavailable(policy.stable_unavailable_message(
            "本次 USB 插入会话内 WDA 已失效；请拔出 USB 并重新插入设备走一遍人工准备"
        ))

    wda = WdaClient(f"http://127.0.0.1:{local_port}")
    try:
        wda.wait_ready(timeout=timeout)
        # 关掉 launcher 里的锁屏 watcher（如果起过），避免 WDA 已就绪后还刷提示
        launcher.mark_ready()
        # §7.5.1 spawn 状态机：只在"真正走了 spawn 分支 + WDA ready"两件事都满足时
        # 才记 record_spawned。spawn 失败（rc!=0 / wait_ready 超时）不记，
        # 让用户根据错误提示走完人工准备后下次 open 还能 spawn。auto 下本调用
        # 是 no-op（policy 内部不维护集合）。
        if mode == "spawn":
            policy.record_spawned(udid)
    except Exception as exc:  # noqa: BLE001
        if callable(on_status):
            try:
                on_status(
                    "error",
                    "WDA 启动失败",
                    f"WDA 在 {timeout}s 内未就绪：{exc}。\n"
                    "请检查 iPhone 是否解锁、是否已信任此 Mac、开发者模式是否开启、USB 线是否正常。\n"
                    + _developer_app_trust_hint(),
                    0,
                )
            except Exception:  # noqa: BLE001
                pass
        if forwarder is not None:
            forwarder.stop()
        launcher.stop()
        raise RuntimeError(
            f"WDA 未在 {timeout}s 内就绪 udid={udid} local_port={local_port}: {exc}\n"
            f"→ launcher 模式={mode}\n"
            f"→ 如果 mode=disabled，请在 .env 设 AI_PHONE_WDA_PROJECT_DIR 指向 WebDriverAgent 工程目录，\n"
            f"  或先在 Xcode 里 Cmd+U 起好 WDA + 另开终端 `iproxy {local_port}:{local_port}`"
        ) from exc

    if settings.wda_self_check:
        try:
            _ios_three_layer_self_check(udid, wda)
        except Exception:
            if forwarder is not None:
                forwarder.stop()
            launcher.stop()
            raise
    else:
        # 不做自检时至少建一把 session（很多 WDA 接口隐式依赖 sid）
        try:
            wda.create_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WDA create_session 失败（继续，部分接口走默认 session）：{}", exc)

    drv = IosDriver(
        udid=udid,
        lockdown=ld,
        wda=wda,
        forwarder=forwarder,
        launcher=launcher,
    )
    # 把 driver 的已就绪 wda 客户端放到全局 map，供 mirror（ios_capture_mjpeg）
    # 复用已有 session 推 appium settings，避免自己建 session 顶掉 driver 的那把
    with _PORT_ALLOC_LOCK:
        _WDA_CLIENT_MAP[udid] = wda
    logger.info("iOS driver 已上线 udid={} local_port={} launcher={}", udid, local_port, mode)
    return drv


__all__ = [
    "IosDriver",
    "list_ios_devices",
    "open_ios_driver",
]
