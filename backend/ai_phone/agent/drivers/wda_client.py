"""轻量 WebDriverAgent (WDA) HTTP 客户端。

为什么不用 ``facebook-wda``：
- 它把同步 ``requests`` 拖进来，且 API 风格是"链式 selector"，对自动化平台
  这种"裸坐标 tap/swipe"用法不直观
- 我们已经依赖 ``httpx``，自己包一层 200 行能搞定，依赖更干净
- 失败语义可控：所有异常统一抛 ``WdaError``，便于上层 fallback / 日志着色

参考协议：
- WDA REST 文档：https://github.com/appium/WebDriverAgent/wiki/Queries
- 真机调用规则：先 ``POST /session`` 拿 sessionId，后续大部分接口都需要 sid
- 端点假定运行在 ``http://127.0.0.1:8100``（usbmux 转发后），多设备时上层
  负责给每台设备分配一个不冲突的本地端口

线程模型：
- 内部用 ``httpx.Client``（同步），每个 IosDriver 实例持有一个
- HTTP 请求在驱动方法内同步发出；外层 (_handle_input) 在线程池里调，不会阻塞
  asyncio 事件循环
"""
from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from loguru import logger


class WdaError(RuntimeError):
    """WDA 调用错误。包括 HTTP 非 200、JSON 解析失败、status != 0。"""


class WdaSessionError(WdaError):
    """WDA session 失效（404 invalid session id / Session does not exist）。

    触发时机：WDA 进程被重启、xcuitest crash、session 被外部 DELETE、iOS 系统
    回收 background app 导致 runner 退出等。此异常抛出时 ``_session_id`` 已被
    清空，上层用 :func:`_auto_recover_session` 装饰器可以无感重试一次——重试
    走 ``_ensure_session`` 会自动 ``POST /session`` 新建一个。
    """


def _looks_like_session_gone(status_code: int, body: str) -> bool:
    """识别 WDA 返回的 session 失效信号。

    WDA 新版本 (Appium 2.x) 形如：
        HTTP/1.1 404 Not Found
        { "value": { "error": "invalid session id",
                     "message": "Session does not exist", ... } }

    老版本偶尔返回 500 + error=invalid session id。这里两种都兜。
    """
    if status_code not in (404, 500):
        return False
    b = (body or "").lower()
    return ("invalid session id" in b) or ("session does not exist" in b)


def _auto_recover_session(fn: Callable) -> Callable:
    """业务方法装饰器：首次 :class:`WdaSessionError` 自动重建 session 重试一次。

    只重试一次，避免 WDA 进程彻底挂掉时无限循环；若第二次仍 session 失效，
    异常透出，让上层（driver readiness / 调度）决定 kill/restart xcodebuild。
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except WdaSessionError as exc:
            logger.warning(
                "WDA session 失效，自动重建后重试一次: {}", exc
            )
            return fn(self, *args, **kwargs)

    return wrapper


@dataclass
class WdaSize:
    width: int
    height: int


class WdaClient:
    """单设备 WDA HTTP 客户端，线程安全（内部一把锁串行化请求）。

    生命周期：
        client = WdaClient("http://127.0.0.1:8100")
        client.create_session(bundle_id="com.apple.Preferences")  # 可选
        client.tap(100, 200)
        ...
        client.close()

    SessionId 内部缓存；接口失败 (不是 7=NoSuchElement / 13=Unknown) 时自动尝试
    重建 session。多数移动端"全屏触控"接口（``/wda/tap`` 等）即使没显式
    create_session 也会走默认 system session。
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            headers={"Accept": "application/json"},
        )
        self._session_id: Optional[str] = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._session_id:
                try:
                    self._client.delete(f"/session/{self._session_id}")
                except Exception:  # noqa: BLE001
                    pass
                self._session_id = None
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------------
    # 基础 HTTP
    # ------------------------------------------------------------------
    def _request(
        self, method: str, path: str, json: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """发送请求并返回 JSON 体。``status != 0`` 视为业务错误抛 ``WdaError``。"""
        try:
            resp = self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise WdaError(f"WDA 网络错误 {method} {path}: {exc}") from exc
        if resp.status_code >= 500:
            raise WdaError(
                f"WDA 5xx {method} {path}: {resp.status_code} {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise WdaError(
                f"WDA 响应非 JSON {method} {path}: {resp.text[:200]}"
            ) from exc
        # WDA 标准响应：{ value: ..., sessionId: ..., status: 0 }；新版已移除 status 字段。
        status = payload.get("status")
        if status is not None and status != 0:
            value = payload.get("value")
            raise WdaError(
                f"WDA status={status} {method} {path} value={value}"
            )
        # 新版用 HTTP 4xx 表示业务错
        if resp.status_code >= 400:
            # 先识别"session 失效"——把 self._session_id 清掉，下一次
            # _ensure_session 会自动重建；装饰器 _auto_recover_session 负责
            # 对业务方法做一次无感 retry
            if _looks_like_session_gone(resp.status_code, resp.text):
                self._session_id = None
                raise WdaSessionError(
                    f"WDA session gone {method} {path}: "
                    f"{resp.status_code} {resp.text[:200]}"
                )
            raise WdaError(
                f"WDA 4xx {method} {path}: {resp.status_code} {resp.text[:200]}"
            )
        return payload

    # ------------------------------------------------------------------
    # 健康
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """``/status`` 不需要 session，可以做 readiness 探测。"""
        with self._lock:
            return self._request("GET", "/status").get("value") or {}

    def wait_ready(self, timeout: float = 30.0, interval: float = 0.5) -> None:
        """轮询 ``/status`` 直到 WDA 就绪。

        每 10 秒打一条 INFO 告诉用户还剩多少时间。

        **超时该调多大**：xcodebuild test 首次跑会完整编译 WebDriverAgent
        工程（包含 Facebook 那批 swift 依赖），冷启动 1~3 分钟正常；
        二次启动走 incremental build 10~20s。默认 300s 覆盖首次冷启动。
        """
        deadline = time.time() + timeout
        last_exc: Optional[Exception] = None
        last_log = 0.0
        start = time.time()
        while time.time() < deadline:
            try:
                v = self.status()
                # WDA 不同版本字段不一样，存在即视作就绪
                if v:
                    return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            now = time.time()
            if now - last_log >= 10.0:
                remain = int(deadline - now)
                logger.info(
                    "WDA 还没就绪，已等 {:.0f}s（最多再等 {}s）— 上次错误：{}\n"
                    "  → 如果 xcodebuild test 还在编译，正常等；首次冷启动最长 3 分钟\n"
                    "  → 如果 iPhone 上没看到 Automation Running，可能签名/信任证书没走通",
                    now - start, remain, last_exc,
                )
                last_log = now
            time.sleep(interval)
        raise WdaError(f"WDA 未在 {timeout}s 内就绪: {last_exc}")

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
    def create_session(
        self,
        bundle_id: Optional[str] = None,
        capabilities: Optional[Dict[str, Any]] = None,
    ) -> str:
        """新建 session。bundle_id 为空时走 default app（不切前台）。"""
        caps: Dict[str, Any] = {}
        if bundle_id:
            caps["bundleId"] = bundle_id
        if capabilities:
            caps.update(capabilities)
        body = {
            "capabilities": {
                "alwaysMatch": caps,
                "firstMatch": [{}],
            }
        }
        with self._lock:
            payload = self._request("POST", "/session", body)
            value = payload.get("value") or {}
            sid = value.get("sessionId") or payload.get("sessionId")
            if not sid:
                raise WdaError(f"WDA create_session 无 sessionId: {payload}")
            self._session_id = sid
            logger.info("WDA session 已建 sid={} bundle={}", sid, bundle_id)
            return sid

    def _ensure_session(self) -> str:
        if self._session_id:
            return self._session_id
        return self.create_session()

    # ------------------------------------------------------------------
    # 屏幕信息
    # ------------------------------------------------------------------
    @_auto_recover_session
    def window_size(self) -> WdaSize:
        """返回 ``/window/size``。WDA 报的是逻辑点（point），不是物理像素。

        要拿物理像素还要乘以 ``scale`` (``/wda/screen``)，由上层 driver 决定。
        """
        sid = self._ensure_session()
        payload = self._request("GET", f"/session/{sid}/window/size")
        v = payload.get("value") or {}
        return WdaSize(width=int(v.get("width", 0)), height=int(v.get("height", 0)))

    @_auto_recover_session
    def screen_scale(self) -> float:
        """``/wda/screen`` 返回 ``{ statusBarSize, scale }``，scale 是 point→pixel 倍率。"""
        sid = self._ensure_session()
        try:
            payload = self._request("GET", f"/session/{sid}/wda/screen")
        except WdaSessionError:
            raise  # 让 _auto_recover_session 接到
        except WdaError:
            return 1.0
        v = payload.get("value") or {}
        try:
            return float(v.get("scale", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @_auto_recover_session
    def orientation(self) -> str:
        """``PORTRAIT`` / ``LANDSCAPE`` / ``UIA_DEVICE_ORIENTATION_*``。"""
        sid = self._ensure_session()
        payload = self._request("GET", f"/session/{sid}/orientation")
        return str(payload.get("value") or "")

    # ------------------------------------------------------------------
    # 触控（坐标全部按 point 传，不是物理像素）
    # ------------------------------------------------------------------
    @_auto_recover_session
    def tap(self, x: float, y: float) -> None:
        """单击。

        历史坑：老版 WDA 是 ``POST /wda/tap/0``（``/0`` 是 element index 占位位，
        ``0`` 代表 screen 而非具体 element）。Appium WebDriverAgent 近年在
        iOS 17+ 适配时把路由改成 ``POST /wda/tap``（不带 index）——老路径
        直接 404 ``unknown command``。iOS 26 + 现役 WDA 只认新路径，所以我们
        统一走新路径；老 WDA（2020 年前）如果复现 404，只能回退老路径。
        """
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/tap",
            {"x": float(x), "y": float(y)},
        )

    @_auto_recover_session
    def double_tap(self, x: float, y: float) -> None:
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/doubleTap",
            {"x": float(x), "y": float(y)},
        )

    @_auto_recover_session
    def long_press(self, x: float, y: float, duration_s: float = 1.0) -> None:
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/touchAndHold",
            {"x": float(x), "y": float(y), "duration": float(duration_s)},
        )

    @_auto_recover_session
    def swipe(
        self,
        sx: float,
        sy: float,
        ex: float,
        ey: float,
        duration_s: float = 0.5,
    ) -> None:
        """``/wda/dragfromtoforduration``：单指连续拖动，最稳定的滑动接口。"""
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/dragfromtoforduration",
            {
                "fromX": float(sx),
                "fromY": float(sy),
                "toX": float(ex),
                "toY": float(ey),
                "duration": float(duration_s),
            },
        )

    # ------------------------------------------------------------------
    # 输入 & 按键
    # ------------------------------------------------------------------
    @_auto_recover_session
    def type_text(self, text: str) -> None:
        """直接给当前聚焦的输入框打字。WDA 走 IOHIDEvent，中文也可以（系统级输入法）。"""
        if not text:
            return
        sid = self._ensure_session()
        # ``value`` 是单字数组；keys 接受字符串
        self._request(
            "POST",
            f"/session/{sid}/wda/keys",
            {"value": list(text)},
        )

    @_auto_recover_session
    def dismiss_keyboard(self) -> None:
        """关闭软键盘（iOS WDA 端点）。

        与 Android ``input text`` / Harmony hmdriver 的"直接注入字符不弹键盘"
        语义对齐——iOS WDA 走 IOHIDEvent 必然弹软键盘且不会自动收起，常导致
        键盘遮挡下方按钮（"完成"/"提交"等）让 VLM 后续点击失效。``type_text``
        之后统一调一次本接口主动收起。

        Appium WDA 的标准路由是 ``POST /session/{sid}/wda/keyboard/dismiss``。
        老版本 WDA 没有该端点会回 404，本方法吞掉异常退化为 no-op，确保
        升级路径平滑。
        """
        sid = self._ensure_session()
        try:
            self._request("POST", f"/session/{sid}/wda/keyboard/dismiss", {})
        except WdaError as exc:
            # 老 WDA / 非全屏键盘场景拿不到 dismiss 按钮都会抛；不致命
            logger.debug("dismiss_keyboard 忽略: {}", exc)

    @_auto_recover_session
    def press_button(self, name: str) -> None:
        """``home`` / ``volumeup`` / ``volumedown``。WDA 端按字符串识别。"""
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/pressButton",
            {"name": name},
        )

    # ------------------------------------------------------------------
    # 截图（WDA 自带，独立于 lockdown / DVT 通道）
    # ------------------------------------------------------------------
    @_auto_recover_session
    def screenshot(self) -> bytes:
        """``GET /screenshot``：返回 base64 编码的 PNG 字节。

        这个接口不挑 session（root 路由也支持），且**不走 lockdown / DVT**，
        只要 WDA 进程活着就能拿图——这是 iOS 17+ 在 tunneld/DDI 没配好时唯一
        可靠的截图通道。
        """
        import base64  # noqa: PLC0415
        with self._lock:
            try:
                payload = self._request("GET", "/screenshot")
            except WdaSessionError:
                raise  # 交给 _auto_recover_session 重建后重试
            except WdaError:
                # 某些旧版 WDA 只在 session 内暴露 screenshot
                sid = self._ensure_session()
                payload = self._request("GET", f"/session/{sid}/screenshot")
        b64 = payload.get("value") or ""
        if not b64:
            raise WdaError("WDA /screenshot 返回空 value")
        return base64.b64decode(b64)

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------
    @_auto_recover_session
    def launch_app(self, bundle_id: str) -> None:
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/apps/launch",
            {"bundleId": bundle_id},
        )

    @_auto_recover_session
    def terminate_app(self, bundle_id: str) -> None:
        sid = self._ensure_session()
        self._request(
            "POST",
            f"/session/{sid}/wda/apps/terminate",
            {"bundleId": bundle_id},
        )

    @_auto_recover_session
    def active_app(self) -> Dict[str, Any]:
        """``/wda/activeAppInfo``：``{ bundleId, name, processArguments }``。"""
        sid = self._ensure_session()
        payload = self._request("GET", f"/session/{sid}/wda/activeAppInfo")
        return payload.get("value") or {}

    @_auto_recover_session
    def device_info(self) -> Dict[str, Any]:
        """``/wda/device/info``：``{ name, model, system version ...}``"""
        sid = self._ensure_session()
        try:
            payload = self._request("GET", f"/session/{sid}/wda/device/info")
        except WdaSessionError:
            raise
        except WdaError:
            return {}
        return payload.get("value") or {}

    @_auto_recover_session
    def lock(self) -> None:
        sid = self._ensure_session()
        self._request("POST", f"/session/{sid}/wda/lock", {})

    @_auto_recover_session
    def unlock(self) -> None:
        sid = self._ensure_session()
        self._request("POST", f"/session/{sid}/wda/unlock", {})

    # ------------------------------------------------------------------
    # Appium Settings（运行时调 WDA 内部参数，主要给 mjpeg server 用）
    # ------------------------------------------------------------------
    @_auto_recover_session
    def update_appium_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /session/<sid>/appium/settings``：动态调 WDA 行为开关。

        WDA mjpeg server 相关键（Appium WDA 文档）：

        - ``mjpegServerScreenshotQuality``：1-100，JPEG 质量
        - ``mjpegServerFramerate``：1-60，目标帧率
        - ``mjpegScalingFactor``：1-100，缩放百分比（80 = 输出 80% 长边）
        - ``mjpegFixOrientation``：bool，true 强制输出竖屏

        关键：这些值在 ``capabilities`` 里塞进去 ``newSession`` 也认（WDA 看到
        前缀就转给 settings），但 settings API 更稳；某些 WDA 版本对 cap
        命名严格度不一样。两条路都试一下兜底。
        """
        if not settings:
            return {}
        sid = self._ensure_session()
        payload = self._request(
            "POST",
            f"/session/{sid}/appium/settings",
            {"settings": dict(settings)},
        )
        return payload.get("value") or {}

    @_auto_recover_session
    def is_locked(self) -> bool:
        sid = self._ensure_session()
        try:
            payload = self._request("GET", f"/session/{sid}/wda/locked")
            return bool(payload.get("value"))
        except WdaSessionError:
            raise
        except WdaError:
            return False


__all__ = ["WdaClient", "WdaSize", "WdaError", "WdaSessionError"]
