"""RemoteDriver：BaseDriver 的 Server 侧 RPC 实现。

VLMRunner 既有逻辑只跟 BaseDriver 抽象耦合；本文件提供一个保持完全相同
**同步** 接口、但底下走 ``driver_command`` / ``driver_result`` 的实现，让 Server
进程上的 VLMRunner 能像本地驱动一样调用远端 Agent 上挂着的真实设备。

线程模型
--------

::

    [VLMRunner thread N (executor)]
        driver.click(x, y)
            │  asyncio.run_coroutine_threadsafe(_call_remote(...), main_loop)
            ▼
    [Server main event loop]
        DriverRpcWaiter.register → send_fn(driver_command) → await Future

    [VLMRunner thread N]
        cf.result(timeout)  ← 阻塞等结果
        decode result / raise RemoteDriverError on error

约束
----

- RemoteDriver 必须由独立线程池调用（方案 6.10.6）；不能在主 event loop 协程里
  直接 ``driver.click()``，那会自己 await 自己。
- ``send_fn`` 由调用方注入，签名 ``async (payload: dict) -> bool``：
  返回 ``True`` 表示 Agent 在线且 WS 已成功 ``send_json``；返回 ``False`` 表示
  Agent 已离线，本次 RPC 立即抛 :class:`RemoteDriverAgentOfflineError`。
- 截图 / 字节型返回固定走 ``{"encoding":"base64","mime":..,"data":..}``，本类负责解码。

不做的事（v2 PoC）
------------------

- 不在客户端做缓存（同一 step 多次 ``window_size`` 直接发多次 RPC，简单优先）
- 不做断路器 / 退避（方案 6.10.6：先跑、观察、再决定，不预设限流）
- 不在本类做 metrics 埋点（由 ServerRunEmitter 用 ``rpc_elapsed_ms`` 落表）
"""
from __future__ import annotations

import asyncio
import base64
import time
import uuid
from concurrent.futures import Future as CFuture
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ai_phone.agent.drivers.base import BaseDriver, DeviceInfo
from ai_phone.agent.runner.stability import StabilityResult
from ai_phone.shared.protocol import (
    MSG_DRIVER_COMMAND,
    DriverCommandMsg,
    DriverMethod,
)

from .rpc import (
    DriverRpcWaiter,
    RemoteDriverAgentOfflineError,
    RemoteDriverNetworkError,
)


# ---------------------------------------------------------------------------
# 默认软超时（毫秒）。建议值，调用方可按 method 覆盖。
# 见 protocol.py 的 driver_command.deadline_ms 注释。
# ---------------------------------------------------------------------------
DEFAULT_DEADLINE_MS: Dict[str, int] = {
    "prepare_for_run": 8_000,
    "screenshot_png": 10_000,
    "screenshot_jpeg": 10_000,
    "wait_stable_screenshot_jpeg": 20_000,
    "click": 5_000,
    "double_click": 5_000,
    "long_press": 8_000,
    "swipe": 8_000,
    "type_text": 10_000,
    "press_home": 5_000,
    "press_back": 5_000,
    "press_keycode": 5_000,
    # 取尺寸常被坐标换算隐式调用（click/scroll/drag 前都可能触发）。
    # 设备或 Agent 正忙时 3s 太激进，会把原本可恢复的动作直接打成执行失败。
    "window_size": 10_000,
    "rotation": 3_000,
    "list_third_party_packages": 15_000,
    "list_all_packages": 15_000,
    "activate_app": 15_000,
    "terminate_app": 10_000,
    "current_app": 5_000,
    "device_info": 5_000,
    "scroll": 10_000,
}
# 给 future.result(timeout) 留 1s 网络往返冗余
_TIMEOUT_OVERHEAD_MS = 1_000


SendFn = Callable[[Dict[str, Any]], Awaitable[bool]]
CommandSentHook = Callable[[Dict[str, Any]], Awaitable[None]]
CommandFinishedHook = Callable[
    [Dict[str, Any], Optional[Dict[str, Any]], Optional[BaseException], int],
    Awaitable[None],
]


class RemoteDriver(BaseDriver):
    """跑在 Server 上的 BaseDriver；所有方法转 driver_command。

    Parameters
    ----------
    serial : str
        目标设备 serial。
    agent_id : str
        Run 启动时绑定的 Agent ID（``runs.agent_id_at_start``）。Agent 重连
        换 ID 不影响本实例（Run finalizer 会负责终止）。
    waiter : DriverRpcWaiter
        Server 端唯一的 RPC 撮合器。
    send_fn : async (payload) -> bool
        发送 driver_command 的函数；通常是 ``hub.send_to_agent`` 的偏特化。
        注入式好测：mock 直接传内存版即可。
    loop : asyncio.AbstractEventLoop
        Server 主 event loop；同步方法用 ``run_coroutine_threadsafe`` 切回此 loop。
    run_id : str
        所属 Run id；写进每条 driver_command，供 Waiter / 日志按 run 维度过滤。
    platform : str
        给 BaseDriver.platform 字段，纯 metadata；不影响行为。
    """

    def __init__(
        self,
        *,
        serial: str,
        agent_id: str,
        waiter: DriverRpcWaiter,
        send_fn: SendFn,
        loop: asyncio.AbstractEventLoop,
        run_id: str,
        platform: str = "android",
        on_command_sent: Optional[CommandSentHook] = None,
        on_command_finished: Optional[CommandFinishedHook] = None,
    ) -> None:
        self.serial = serial
        self.platform = platform
        self.agent_id = agent_id
        self.run_id = run_id
        self._waiter = waiter
        self._send_fn = send_fn
        self._loop = loop
        self._on_command_sent = on_command_sent
        self._on_command_finished = on_command_finished

    # ------------------------------------------------------------------
    # 内部：发出一条 driver_command 并阻塞等结果
    # ------------------------------------------------------------------
    def _call(
        self,
        method: DriverMethod,
        params: Optional[Dict[str, Any]] = None,
        *,
        deadline_ms: Optional[int] = None,
    ) -> Any:
        """同步 RPC 调用入口；本身阻塞当前线程直到 Agent 回复 / 超时。

        返回 ``driver_result`` 的 ``result`` 字段（bytes 已解码、tuple 已还原）。
        失败抛 :class:`RemoteDriverError` 子类。
        """
        deadline_ms = deadline_ms or DEFAULT_DEADLINE_MS.get(method, 10_000)
        message_id = uuid.uuid4().hex[:16]
        call_started = time.monotonic()
        payload: DriverCommandMsg = {
            "type": MSG_DRIVER_COMMAND,
            "message_id": message_id,
            "run_id": self.run_id,
            "serial": self.serial,
            "method": method,
            "params": dict(params or {}),
            "deadline_ms": int(deadline_ms),
        }

        # 1) 把"注册 + 发送"组合成一个协程，扔到主 loop 上跑
        cf: CFuture[Dict[str, Any]] = asyncio.run_coroutine_threadsafe(
            self._send_and_wait(message_id, payload, method),
            self._loop,
        )

        # 2) 当前线程阻塞等结果。给 future 留比 deadline 多 1s 网络冗余
        wall_timeout_s = (deadline_ms + _TIMEOUT_OVERHEAD_MS) / 1000.0
        try:
            driver_result = cf.result(timeout=wall_timeout_s)
        except FuturesTimeoutError as exc:
            # 我们自己进入了 hard timeout：先取消投到主 loop 的协程，再同步等
            # waiter 清掉 entry 并取消其 Future。否则 _send_and_wait 会继续
            # await 一个永远不会完成的 Future，形成挂起 task。
            cf.cancel()
            timeout_exc = RemoteDriverNetworkError(
                f"RPC 等待超时 method={method} deadline_ms={deadline_ms}",
                error_class="RpcTimeout",
                message_id=message_id,
            )
            try:
                asyncio.run_coroutine_threadsafe(
                    _discard_async(self._waiter, message_id), self._loop
                ).result(timeout=2.0)
            except Exception:  # noqa: BLE001 — discard 失败也不该掩盖原超时
                pass
            if self._on_command_finished is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._on_command_finished(
                            dict(payload),
                            None,
                            timeout_exc,
                            int(wall_timeout_s * 1000),
                        ),
                        self._loop,
                    ).result(timeout=2.0)
                except Exception:  # noqa: BLE001 — 记录失败也不掩盖原超时
                    pass
            raise timeout_exc from exc

        try:
            return self._decode_result(method, driver_result)
        except BaseException as exc:
            # driver_result.ok=true 只代表 Agent 执行成功；若返回结构在 Server
            # 解码阶段才发现异常，也要回写 run_commands 为失败，避免排障时
            # 看到"命令成功但 Run 失败"的割裂。
            if self._on_command_finished is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._on_command_finished(
                            dict(payload),
                            driver_result,
                            exc,
                            int((time.monotonic() - call_started) * 1000),
                        ),
                        self._loop,
                    ).result(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            raise

    async def _send_and_wait(
        self,
        message_id: str,
        payload: DriverCommandMsg,
        method: str,
    ) -> Dict[str, Any]:
        """主 loop 上跑：注册 waiter → 发送 → 等 Future。"""
        future = self._waiter.register(message_id, run_id=self.run_id, method=method)
        started = time.monotonic()
        finish_notified = False
        try:
            if self._on_command_sent is not None:
                await self._on_command_sent(dict(payload))
            ok = await self._send_fn(dict(payload))
            if not ok:
                # 发送失败 = Agent 离线 / WS 不可用；不再等 driver_result
                self._waiter.discard(message_id)
                exc = RemoteDriverAgentOfflineError(
                    f"Agent {self.agent_id} 不可达，driver_command 未发出",
                    error_class="AgentOffline",
                    message_id=message_id,
                )
                if self._on_command_finished is not None:
                    await self._on_command_finished(
                        dict(payload), None, exc, int((time.monotonic() - started) * 1000)
                    )
                    finish_notified = True
                raise exc

            # 等待 driver_result。这里不再加超时——外层调用线程已经用
            # cf.result(timeout) 设了硬超时；等不到时外层会 cancel 当前协程，
            # 并通过 discard 取消这个 Future。
            result = await future
            if self._on_command_finished is not None:
                await self._on_command_finished(
                    dict(payload), result, None, int((time.monotonic() - started) * 1000)
                )
                finish_notified = True
            return result
        except asyncio.CancelledError:
            self._waiter.discard(message_id)
            raise
        except BaseException as exc:
            self._waiter.discard(message_id)
            if self._on_command_finished is not None and not finish_notified:
                await self._on_command_finished(
                    dict(payload), None, exc, int((time.monotonic() - started) * 1000)
                )
            raise

    # ------------------------------------------------------------------
    # 内部：driver_result.result 解码
    # ------------------------------------------------------------------
    def _decode_result(
        self, method: DriverMethod, driver_result: Dict[str, Any]
    ) -> Any:
        result = driver_result.get("result")

        # 1) 字节型返回（截图）：{"encoding":"base64","mime":...,"data":...}
        if method in ("screenshot_png", "screenshot_jpeg"):
            if not isinstance(result, dict) or "data" not in result:
                raise RemoteDriverNetworkError(
                    f"{method} 返回结构异常：{result!r}",
                    error_class="MalformedResult",
                    message_id=driver_result.get("message_id", ""),
                )
            try:
                return base64.b64decode(result["data"])
            except Exception as exc:
                raise RemoteDriverNetworkError(
                    f"{method} base64 解码失败：{exc}",
                    error_class="MalformedResult",
                    message_id=driver_result.get("message_id", ""),
                ) from exc

        # 1.5) Agent 近端稳定检测：{"image": <base64 jpeg>, "checks": ...}
        if method == "wait_stable_screenshot_jpeg":
            if not isinstance(result, dict):
                raise RemoteDriverNetworkError(
                    f"{method} 返回结构异常：{result!r}",
                    error_class="MalformedResult",
                    message_id=driver_result.get("message_id", ""),
                )
            image = result.get("image")
            image_bytes: Optional[bytes] = None
            if image is not None:
                if not isinstance(image, dict) or "data" not in image:
                    raise RemoteDriverNetworkError(
                        f"{method}.image 返回结构异常：{image!r}",
                        error_class="MalformedResult",
                        message_id=driver_result.get("message_id", ""),
                    )
                try:
                    image_bytes = base64.b64decode(image["data"])
                except Exception as exc:
                    raise RemoteDriverNetworkError(
                        f"{method} image base64 解码失败：{exc}",
                        error_class="MalformedResult",
                        message_id=driver_result.get("message_id", ""),
                    ) from exc
            logs = result.get("logs")
            return StabilityResult(
                image_bytes,
                bool(result.get("stable")),
                int(result.get("elapsed_ms") or 0),
                int(result.get("checks") or 0),
                logs=list(logs) if isinstance(logs, list) else [],
                reused_frame=bool(result.get("reused_frame")),
            )

        # 2) window_size：[w, h] → (w, h)
        if method == "window_size":
            if not isinstance(result, (list, tuple)) or len(result) != 2:
                raise RemoteDriverNetworkError(
                    f"window_size 返回结构异常：{result!r}",
                    error_class="MalformedResult",
                    message_id=driver_result.get("message_id", ""),
                )
            return (int(result[0]), int(result[1]))

        # 3) device_info：dict → DeviceInfo
        if method == "device_info":
            if not isinstance(result, dict):
                raise RemoteDriverNetworkError(
                    f"device_info 返回结构异常：{result!r}",
                    error_class="MalformedResult",
                    message_id=driver_result.get("message_id", ""),
                )
            return _device_info_from_dict(result)

        # 4) 其余：直接透传
        return result

    # ------------------------------------------------------------------
    # BaseDriver 实现
    # ------------------------------------------------------------------
    def prepare_for_run(self) -> None:
        self._call("prepare_for_run")

    # —— 屏幕信息 —— #
    def window_size(self) -> Tuple[int, int]:
        return self._call("window_size")

    def rotation(self) -> int:
        return int(self._call("rotation"))

    # —— 截图 —— #
    def screenshot_png(self) -> bytes:
        return self._call("screenshot_png")

    def screenshot_jpeg(
        self, quality: int = 25, max_side: Optional[int] = None
    ) -> bytes:
        return self._call(
            "screenshot_jpeg",
            {"quality": int(quality), "max_side": max_side},
        )

    def wait_stable_screenshot_jpeg(
        self,
        quality: int = 25,
        max_side: Optional[int] = None,
        *,
        enabled: Optional[bool] = None,
        total_timeout_s: Optional[float] = None,
        poll_interval_s: Optional[float] = None,
        threshold: Optional[float] = None,
        roi_threshold: Optional[float] = None,
        black_threshold: Optional[float] = None,
        strategy: str = "vlm_phash",
    ) -> StabilityResult:
        return self._call(
            "wait_stable_screenshot_jpeg",
            {
                "quality": int(quality),
                "max_side": max_side,
                "enabled": enabled,
                "total_timeout_s": total_timeout_s,
                "poll_interval_s": poll_interval_s,
                "threshold": threshold,
                "roi_threshold": roi_threshold,
                "black_threshold": black_threshold,
                "strategy": strategy,
            },
            deadline_ms=DEFAULT_DEADLINE_MS["wait_stable_screenshot_jpeg"],
        )

    # —— 触控 —— #
    def click(self, x: int, y: int) -> None:
        self._call("click", {"x": int(x), "y": int(y)})

    def double_click(self, x: int, y: int, interval_ms: int = 100) -> None:
        # 跨进程：交给 Agent 本地合成（一次 RPC vs 两次 RPC）
        self._call(
            "double_click",
            {"x": int(x), "y": int(y), "interval_ms": int(interval_ms)},
        )

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        self._call(
            "long_press",
            {"x": int(x), "y": int(y), "duration_ms": int(duration_ms)},
        )

    def swipe(
        self, sx: int, sy: int, ex: int, ey: int, duration_ms: int = 500
    ) -> None:
        self._call(
            "swipe",
            {
                "sx": int(sx),
                "sy": int(sy),
                "ex": int(ex),
                "ey": int(ey),
                "duration_ms": int(duration_ms),
            },
        )

    # —— 输入 & 按键 —— #
    def type_text(self, text: str) -> None:
        self._call("type_text", {"text": str(text)})

    def press_home(self) -> None:
        self._call("press_home")

    def press_back(self) -> None:
        self._call("press_back")

    def press_keycode(self, code: int) -> None:
        self._call("press_keycode", {"code": int(code)})

    # —— 应用 —— #
    def list_third_party_packages(self) -> List[str]:
        result = self._call("list_third_party_packages")
        return list(result or [])

    def list_all_packages(self) -> List[str]:
        result = self._call("list_all_packages")
        return list(result or [])

    def activate_app(self, package_name: str) -> None:
        self._call("activate_app", {"package_name": str(package_name)})

    def terminate_app(self, package_name: str) -> None:
        self._call("terminate_app", {"package_name": str(package_name)})

    def current_app(self) -> str:
        return str(self._call("current_app") or "")

    # —— 基础信息 —— #
    def device_info(self) -> DeviceInfo:
        return self._call("device_info")

    # —— 派生：scroll 走一次 RPC，让 Agent 端复用 BaseDriver 默认实现 —— #
    def scroll(
        self,
        direction: str,
        center: Optional[Tuple[int, int]] = None,
        amount: int = 1,
    ) -> None:
        # JSON 不传 tuple；center=None 直接传 null
        center_payload = (
            [int(center[0]), int(center[1])] if center is not None else None
        )
        self._call(
            "scroll",
            {
                "direction": str(direction),
                "center": center_payload,
                "amount": int(amount),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _discard_async(waiter: DriverRpcWaiter, message_id: str) -> None:
    """同步版 discard 的协程包装，便于 run_coroutine_threadsafe.result 等待。"""
    waiter.discard(message_id)


def _device_info_from_dict(data: Dict[str, Any]) -> DeviceInfo:
    """从 dict 还原 DeviceInfo；缺字段用 dataclass 默认值兜底。"""
    return DeviceInfo(
        serial=str(data.get("serial", "")),
        platform=str(data.get("platform", "")),
        brand=str(data.get("brand", "") or ""),
        model=str(data.get("model", "") or ""),
        os_version=str(data.get("os_version", "") or ""),
        screen_width=int(data.get("screen_width", 0) or 0),
        screen_height=int(data.get("screen_height", 0) or 0),
        status=str(data.get("status", "online") or "online"),
        extra=dict(data.get("extra", {}) or {}),
    )


__all__ = ["RemoteDriver", "DEFAULT_DEADLINE_MS"]
