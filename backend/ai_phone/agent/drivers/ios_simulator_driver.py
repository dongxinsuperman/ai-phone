"""``IosSimulatorDriver``：iOS 虚拟机的 :class:`~.base.BaseDriver` 实现。

与 iOS **真机**的 :class:`~.ios.IosDriver` 完全独立，不共享实例、缓存或全局状态
（方案 §3.3、§3.4）。真机那条链路一行不改。

为什么不能直接复用 ``IosDriver``：它的构造函数第一个参数就是 ``lockdown``——
usbmux/USB 协议对象，虚拟机根本不存在这个东西；``device_info`` 读 lockdown，
截图兜底走 DVT + tunneld，应用清单走 USB 的 installation_proxy。整个文件里
lockdown / forwarder / usbmux / tunneld 相关代码出现上百处，虚拟机一处也用不上。

分工（方案 §2）：

```text
本类            实现 BaseDriver，把上层调用翻译成 WDA / simctl 调用
WdaClient       现成的 HTTP 客户端，原样复用，不改一行
WDA             跑在虚拟机里，由 IosSimulatorWdaLauncher 负责起停
simctl          补 WDA 拿不到的东西：设备元信息、应用清单、相册写入
```

坐标系与真机完全一致：对外暴露物理像素，内部按 ``scale`` 折算成 WDA 要的逻辑点。
实测 402 × 3 = 1206 与截图实际像素一致（方案 §1.4），因此 VLM 的归一化坐标不需要
任何平台分支。

三处比真机**更简单或更好**的地方，都在对应方法上注明：

- 截图只走 WDA，没有 DVT / lockdown 两层兜底
- ``terminate_app`` 走 ``simctl terminate``，没有真机上 SpringBoard 静默拒绝的老问题
- ``save_screenshot_to_album`` 走 ``simctl addmedia``，不需要真机那套唤起 Siri 的 hack
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
from loguru import logger

from .base import AlbumSaveResult, BaseDriver, DeviceInfo
from .ios_simulator import PLATFORM_IOS_SIM
from .ios_simulator_wda import IosSimulatorWdaLauncher, runner_app_path
from .simctl import SimctlError, list_simulators, simctl_run
from .wda_client import WdaClient, WdaError


# WDA runner 自己也是一个 User 应用，但它是我们的基础设施，不是业务 App。
# 列第三方应用时必须滤掉，否则 VLM 会把它当候选 App。
_WDA_RUNNER_SUFFIX = ".xctrunner"


# ---------------------------------------------------------------------------
# 已就绪虚拟机的 WDA 端点登记表
# ---------------------------------------------------------------------------
# 镜像 streamer 需要两样东西：MJPEG 端口，以及 driver 那把**现成的**
# ``WdaClient``。后者是硬约束——WDA 是单 session 模型，streamer 若自己
# ``POST /session`` 会把 driver 的 session 顶掉，之后 mjpeg 全 502、所有控制
# 全 404（真机上真出过这个事故，见 ios_capture_mjpeg 的注释）。
#
# **为什么不复用真机的 ``ios._WDA_CLIENT_MAP``**：那张表被真机的拔插生命周期
# 策略（``_check_ios_driver_health`` / ``_handle_ios_driver_unhealthy`` /
# ``get_ios_wda_lifecycle_policy``）和 ``health.probe.IosProbe`` 消费。把虚拟机
# 塞进去，等于让真机那套 stable/auto 拔插状态机开始管理虚拟机，也会让 iOS
# readiness probe 把虚拟机当真机探测。独立一张表，两条链路互不可见（§3.4）。
@dataclass(frozen=True)
class SimWdaEndpoint:
    """一台已就绪虚拟机的 WDA 接入点。"""

    wda_port: int
    mjpeg_port: int
    client: WdaClient


_SIM_ENDPOINTS: Dict[str, SimWdaEndpoint] = {}
_SIM_ENDPOINTS_LOCK = threading.Lock()


def register_sim_endpoint(udid: str, endpoint: SimWdaEndpoint) -> None:
    with _SIM_ENDPOINTS_LOCK:
        _SIM_ENDPOINTS[udid] = endpoint


def unregister_sim_endpoint(udid: str) -> None:
    with _SIM_ENDPOINTS_LOCK:
        _SIM_ENDPOINTS.pop(udid, None)


def get_sim_endpoint(udid: str) -> Optional[SimWdaEndpoint]:
    """取一台虚拟机的 WDA 端点；driver 未就绪时返回 ``None``。

    调用方（镜像 streamer）拿不到就该明确失败，不要自己猜端口或新建 client。
    """
    with _SIM_ENDPOINTS_LOCK:
        return _SIM_ENDPOINTS.get(udid)


class IosSimulatorDriver(BaseDriver):
    """单台 iOS 虚拟机的驱动。每个 udid 一个实例。"""

    platform = PLATFORM_IOS_SIM

    def __init__(
        self,
        udid: str,
        wda: WdaClient,
        launcher: Optional[IosSimulatorWdaLauncher] = None,
    ) -> None:
        self.serial = udid
        self._wda = wda
        # 起 WDA 的 launcher；close() 时一并停。注意它内部没有常驻子进程，
        # WDA 由虚拟机自己的 launchd_sim 托管（方案 §1.8）。
        self._launcher = launcher
        self._scale: Optional[float] = None
        # simctl 元信息缓存：机型名与系统版本在实例生命周期内不会变
        self._sim_meta: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------
    # Run 前后钩子
    # ------------------------------------------------------------------
    def prepare_for_run(self) -> None:
        """显式空实现。

        真机在这里调 ``wda.unlock()`` 唤醒 iPhone——那是为了应对物理设备息屏 /
        锁屏。虚拟机**没有电池，也不会自动息屏**，不需要唤醒；调 unlock 只是白
        跑一次 HTTP。这里明确写成空实现而不是继承默认，是为了让读代码的人知道
        「这是想清楚后的决定」，不是漏了。
        """
        return None

    def sleep_after_run(self) -> None:
        """显式空实现。真机在这里 ``wda.lock()`` 省电，虚拟机无此必要。"""
        return None

    # ------------------------------------------------------------------
    # 屏幕信息
    # ------------------------------------------------------------------
    def _get_scale(self) -> float:
        """屏幕缩放比，成功读到才缓存。

        **失败值绝不能缓存**。scale 是逻辑点换算物理像素的乘数，2x / 3x 设备上
        错成 1.0 会让 ``window_size`` 少报一半到三分之二，而截图仍然是真实分辨率——
        画面看着完全正常，点击坐标却整体偏移，且在这个 driver 的余生里都不会
        自愈。一次瞬时的 WDA 抖动不该留下这种后果，下次调用重读即可。
        """
        if self._scale is None:
            try:
                scale = self._wda.screen_scale()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "读取屏幕缩放比失败 udid={}，本次按 1.0 计算，下次重试：{}",
                    self.serial, exc,
                )
                return 1.0
            if not scale:
                # WDA 返回 0 / None：同样不缓存，留给下次
                logger.warning("屏幕缩放比读到空值 udid={}，本次按 1.0 计算", self.serial)
                return 1.0
            self._scale = scale
        return self._scale

    def window_size(self) -> Tuple[int, int]:
        """物理像素的 ``(width, height)``。

        WDA ``/window/size`` 返回逻辑点，乘 ``scale`` 折算成物理像素，与 Android
        的 device pixel 坐标系对齐。真机在 WDA 失败时会回落读 lockdown；虚拟机
        没有 lockdown，失败就是失败，返回 (0, 0) 让上层按拿不到尺寸处理。
        """
        try:
            size = self._wda.window_size()
            scale = self._get_scale()
            return int(round(size.width * scale)), int(round(size.height * scale))
        except Exception as exc:  # noqa: BLE001
            logger.warning("WDA window_size 失败 udid={}: {}", self.serial, exc)
            return 0, 0

    def rotation(self) -> int:
        try:
            orientation = self._wda.orientation()
        except Exception:  # noqa: BLE001
            return 0
        mapping = {
            "PORTRAIT": 0,
            "LANDSCAPE": 1,
            "UIA_DEVICE_ORIENTATION_PORTRAIT": 0,
            "UIA_DEVICE_ORIENTATION_LANDSCAPELEFT": 1,
            "UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN": 2,
            "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT": 3,
        }
        return mapping.get(str(orientation).upper(), 0)

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    def screenshot_png(self) -> bytes:
        """走 WDA ``/screenshot``。

        比真机简单一层：真机是「WDA 优先 → DVT 兜底 → lockdown 兜底」三级，后两
        级都依赖 USB / tunneld。虚拟机上 WDA 就在宿主 localhost，通不了说明 WDA
        本身有问题，兜底也救不了，所以只留一条路。
        """
        return self._wda.screenshot()

    def screenshot_jpeg(self, quality: int = 25, max_side: Optional[int] = None) -> bytes:
        png = self.screenshot_png()
        with Image.open(io.BytesIO(png)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max_side and max(img.size) > max_side:
                ratio = max_side / float(max(img.size))
                img = img.resize(
                    (int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()

    _ALBUM_METHOD = "WDA screenshot + simctl addmedia"

    def save_screenshot_to_album(self) -> AlbumSaveResult:
        """截图并存进虚拟机的「照片」。

        这条**比真机干净得多**。真机没有把文件塞进照片库的开发者 API，现有实现
        只能唤起 Siri 说「截屏」，靠 SpringBoard 代劳，还要求设备开了 Siri。
        虚拟机有原生命令 ``simctl addmedia``，命令级、可验证、无需任何设备端设置。

        **图像不能用 ``simctl io screenshot`` 取**：实测它**不跟随屏幕方向**——
        业务转到横屏时它仍然吐竖屏尺寸的图（828×1792），存进相册就是一张倒着的图。
        改用 ``self.screenshot_png()``（WDA 通道），它与设备当前方向一致，横屏时
        返回 1792×828。用户要的就是「手机怎么截图就怎么截图」。
        """
        tmp_path = ""
        try:
            png = self.screenshot_png()
            fd, tmp_path = tempfile.mkstemp(prefix="ai-phone-sim-shot-", suffix=".png")
            with os.fdopen(fd, "wb") as fh:
                fh.write(png)
            simctl_run("addmedia", self.serial, tmp_path, timeout=60.0)
        except SimctlError as exc:
            return AlbumSaveResult(
                ok=False,
                platform=self.platform,
                supported=True,
                method=self._ALBUM_METHOD,
                error=f"写入虚拟机相册失败：{exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return AlbumSaveResult(
                ok=False,
                platform=self.platform,
                supported=True,
                method=self._ALBUM_METHOD,
                error=f"写入虚拟机相册异常：{type(exc).__name__}: {exc}",
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return AlbumSaveResult(
            ok=True,
            platform=self.platform,
            method=self._ALBUM_METHOD,
        )

    # ------------------------------------------------------------------
    # 触控（WDA 接口收逻辑点，对外是物理像素，要除回 scale）
    # ------------------------------------------------------------------
    def _px_to_pt(self, x: int, y: int) -> Tuple[float, float]:
        scale = self._get_scale() or 1.0
        return x / scale, y / scale

    def click(self, x: int, y: int) -> None:
        px, py = self._px_to_pt(x, y)
        self._wda.tap(px, py)

    def double_click(self, x: int, y: int, interval_ms: int = 100) -> None:
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
        """输入文本后立即收起软键盘。

        与 Android / Harmony 语义对齐：写完字让后续 VLM 决策看到的是「键盘已落」
        的画面。WDA 走 IOHIDEvent 输入必然弹起软键盘且不自动收，常遮挡下方的
        「完成 / 提交」按钮。
        """
        if not text:
            return
        self._wda.type_text(text)
        try:
            self._wda.dismiss_keyboard()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ios_sim] dismiss_keyboard 忽略 udid={}: {}", self.serial, exc)

    def press_home(self) -> None:
        self._wda.press_button("home")

    def press_back(self) -> None:
        """iOS 没有原生返回键，用左边缘向右滑模拟系统返回手势。

        与真机实现一致。不是所有 App 都响应该手势（游戏 / 全屏 webview 不响应），
        应用层自带返回按钮时 VLM 应自行判断点哪里。
        """
        w, h = self.window_size()
        if w <= 0 or h <= 0:
            return
        sy = h // 2
        self.swipe(2, sy, max(40, int(w * 0.45)), sy, duration_ms=200)

    def press_app_switch(self) -> None:
        """打开 App Switcher：底部中点慢速上滑到约 55% 屏高。

        关键是 duration 要足够慢（1200ms）；快速上滑会变成回桌面。
        """
        w, h = self.window_size()
        if w <= 0 or h <= 0:
            logger.warning("[ios_sim] press_app_switch 拿不到屏幕尺寸，退化为 HOME")
            self.press_home()
            return
        sx = w // 2
        self.swipe(sx, max(0, h - 1), sx, int(h * 0.55), duration_ms=1200)

    def press_keycode(self, code: int) -> None:
        """只映射与 Android 对齐的少数几个：3=HOME、4=BACK、187=APP_SWITCH。"""
        if code == 3:
            self.press_home()
            return
        if code == 4:
            self.press_back()
            return
        if code == 187:
            self.press_app_switch()
            return
        raise NotImplementedError(f"iOS 虚拟机不支持 keycode={code}")

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------
    def _listapps(self) -> Dict[str, Dict[str, Any]]:
        """``simctl listapps`` → ``{bundleId: info}``。

        ``listapps`` 输出的是老式 OpenStep plist，``plistlib`` 解析不了；用
        ``plutil -convert json`` 转成 JSON 再吃。这条路替代了真机的
        installation_proxy（走 USB，虚拟机没有）。
        """
        raw = simctl_run("listapps", self.serial, timeout=60.0)
        if not raw:
            return {}
        try:
            proc = subprocess.run(
                ["plutil", "-convert", "json", "-o", "-", "-"],
                input=raw,
                capture_output=True,
                text=True,
                timeout=30.0,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"plutil 转换 listapps 输出失败：{exc}") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"plutil 转换 listapps 输出失败 rc={proc.returncode}："
                f"{(proc.stderr or '').strip()!r}"
            )
        data = json.loads(proc.stdout or "{}")
        return data if isinstance(data, dict) else {}

    def _apps_by_type(self, application_type: str) -> List[str]:
        try:
            apps = self._listapps()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"获取虚拟机应用列表失败 udid={self.serial}：{exc}"
            ) from exc
        out: List[str] = []
        for bundle_id, info in apps.items():
            if not isinstance(info, dict):
                continue
            if str(info.get("ApplicationType") or "") != application_type:
                continue
            # WDA runner 是我们自己的基础设施，不能当业务 App 交给 VLM
            if str(bundle_id).endswith(_WDA_RUNNER_SUFFIX):
                continue
            out.append(str(bundle_id))
        return sorted(out)

    def list_third_party_packages(self) -> List[str]:
        return self._apps_by_type("User")

    def list_all_packages(self) -> List[str]:
        """User + System 全量。

        注意虚拟机的系统应用比真机**少**：没有电话、App Store、相机、时钟、
        邮件（方案 §1.6）。上层按名字找系统 App 时可能找不到，这是虚拟机的客观
        限制，不做任何伪装或替换。
        """
        return sorted(set(self._apps_by_type("User")) | set(self._apps_by_type("System")))

    def activate_app(self, package_name: str) -> None:
        self._wda.launch_app(package_name)

    def terminate_app(self, package_name: str) -> None:
        """走 ``simctl terminate``，WDA terminate 兜底。

        这里比真机可靠：真机首选 DVT ProcessControl（要 tunneld + DDI），回落
        WDA terminate 时在 iOS 17+ 会遇到「返回 success 但被 SpringBoard 静默
        拒绝」的老问题。虚拟机上 ``simctl terminate`` 是宿主侧命令级操作，不经
        SpringBoard 表决，直接生效。
        """
        try:
            simctl_run("terminate", self.serial, package_name, timeout=30.0)
            return
        except SimctlError as exc:
            # 进程本来就没在跑时 simctl 会报错；语义上等同 force-stop 成功。
            text = f"{exc.stdout} {exc.stderr}".lower()
            if "not running" in text or "found nothing" in text:
                logger.debug(
                    "[ios_sim] terminate_app: {} 本来就没在跑，按成功处理", package_name
                )
                return
            logger.warning(
                "[ios_sim] simctl terminate 失败，回落 WDA terminate udid={} bundle={}：{}",
                self.serial, package_name, exc,
            )

        try:
            self._wda.terminate_app(package_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"终止应用失败 udid={self.serial} bundle={package_name}："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        # 给 SpringBoard 切换时间再复核，避免刚下发就读到旧前台
        time.sleep(0.5)
        try:
            front = self.current_app()
        except Exception:  # noqa: BLE001
            front = ""
        if front == package_name:
            raise RuntimeError(
                f"终止应用后目标仍在前台 udid={self.serial} bundle={package_name}"
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
    def _load_sim_meta(self) -> Dict[str, str]:
        """从 simctl 读机型名与系统版本（替代真机的 lockdown）。缓存一次。"""
        if self._sim_meta is not None:
            return self._sim_meta
        meta = {"model": "", "os_version": ""}
        try:
            for sim in list_simulators():
                if sim.udid == self.serial:
                    meta["model"] = sim.name
                    meta["os_version"] = sim.runtime_version or sim.runtime_name
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取虚拟机 {} 元信息失败（忽略）：{}", self.serial, exc)
        self._sim_meta = meta
        return meta

    def device_info(self) -> DeviceInfo:
        meta = self._load_sim_meta()
        # 尺寸走 WDA（已折算成物理像素）；机型 / 版本走 simctl
        w, h = self.window_size()
        return DeviceInfo(
            serial=self.serial,
            platform=self.platform,
            brand="Apple",
            model=meta.get("model", ""),
            os_version=meta.get("os_version", ""),
            screen_width=w,
            screen_height=h,
            status="online",
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        # 先摘登记，避免镜像 streamer 在 driver 关闭后还拿到失效 client
        # （真机 ``IosDriver.close`` 同样先摘 _WDA_CLIENT_MAP 再关）。
        unregister_sim_endpoint(self.serial)
        try:
            self._wda.close()
        except Exception:  # noqa: BLE001
            pass
        if self._launcher is not None:
            try:
                self._launcher.stop()
            except Exception:  # noqa: BLE001
                pass


def open_ios_simulator_driver(
    udid: str,
    wait_wda_s: Optional[float] = None,
    on_status: Optional[Any] = None,
    **_kwargs: Any,
) -> IosSimulatorDriver:
    """打开一台虚拟机的驱动：起 WDA → 建 WdaClient → 返回 Driver。

    与真机 ``open_ios_driver`` 的差异：**没有 lockdown 握手，也没有端口转发**。
    虚拟机与宿主共享网络栈，WDA 直接监听宿主 ``127.0.0.1:<port>``（方案 §0.2）。

    Args:
        on_status: 可选的进度回调 ``(stage, title, hint, elapsed_ms)``，语义与真机
            一致，用于把「WDA 正在编译」这类阶段推到前端提示条。首次调用需要编译
            WDA（实测约 24 秒），没有反馈的话前端会静默卡住。产物已缓存时整个过程
            只有几秒。

    失败一律抛错，不静默降级——上层据此判定该设备不可用。
    """
    started = time.monotonic()

    def _emit(stage: str, title: str, hint: str) -> None:
        if on_status is None:
            return
        try:
            on_status(stage, title, hint, int((time.monotonic() - started) * 1000))
        except Exception:  # noqa: BLE001
            # 进度上报纯属锦上添花，失败绝不影响驱动打开
            pass

    # 这里只有 udid、拿不到 vm_id，算不出实例名，交给 launcher 回退查询——
    # 不是漏传。查不到时 launcher 会失败关闭，不会无身份校验地启动。
    launcher = IosSimulatorWdaLauncher(udid)
    already_built = runner_app_path().is_dir()
    if already_built:
        _emit("initializing", "虚拟机启动中", "正在启动 WDA…")
    else:
        _emit(
            "compiling",
            "WDA 正在编译",
            "首次使用需要为虚拟机编译一次 WDA（约 25 秒），之后会复用产物",
        )

    try:
        launcher.start(wait_ready_s=wait_wda_s)
    except Exception as exc:  # noqa: BLE001
        _emit("error", "WDA 启动失败", str(exc)[:300])
        raise

    wda = WdaClient(base_url=launcher.wda_url)
    try:
        wda.wait_ready(timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        try:
            wda.close()
        except Exception:  # noqa: BLE001
            pass
        _emit("error", "WDA 握手失败", str(exc)[:300])
        raise RuntimeError(
            f"虚拟机 WDA 已起但客户端握手失败 udid={udid}：{exc}"
        ) from exc

    # 登记端点供镜像复用这把 session（详见 _SIM_ENDPOINTS 注释）
    register_sim_endpoint(
        udid,
        SimWdaEndpoint(
            wda_port=int(launcher.ports.wda),
            mjpeg_port=int(launcher.ports.mjpeg),
            client=wda,
        ),
    )
    _emit("ready", "虚拟机已就绪", "")
    logger.info(
        "虚拟机驱动已就绪 udid={} wda={} mjpeg={}",
        udid, launcher.wda_url, launcher.ports.mjpeg,
    )
    return IosSimulatorDriver(udid, wda, launcher=launcher)
