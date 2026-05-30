"""Agent 侧 WS 客户端，负责：
- 连接 Server、发送 hello、自动重连（指数退避）
- 周期心跳 + 设备扫描上报
- 接收 Server 消息 → 分发给注册的 handler
- 提供 ``send(payload)`` 接口给 runner bridge 推事件上行

保持最小可用；不处理画面帧（M2 再加）。
"""
from __future__ import annotations

import asyncio
import json
import math
import platform
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from loguru import logger
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ai_phone.shared import protocol as P

Handler = Callable[["AgentWSClient", Dict[str, Any]], Awaitable[None]]
ConnectHandler = Callable[["AgentWSClient"], Awaitable[None]]
DeviceProvider = Callable[[], List[Dict[str, Any]]]  # 无参，返回 device dict 列表

_BACKOFF_STEPS = (1.0, 2.0, 5.0, 10.0, 15.0)
_DEFAULT_AGENT_WS_PATH = "/ws/agent"


@dataclass
class AgentWSClient:
    ws_url: str
    token: str
    agent_id: str
    agent_name: str
    server_http_base: str = ""
    host_os: str = field(default_factory=lambda: f"{platform.system()} {platform.release()}")

    # 由 Agent 主程序注入
    device_provider: Optional[DeviceProvider] = None
    handlers: Dict[str, Handler] = field(default_factory=dict)
    connect_handlers: List[ConnectHandler] = field(default_factory=list)

    # 心跳 / 重扫描间隔
    ping_interval: float = 15.0
    rescan_interval: float = 5.0
    # serial 集合不变时，也周期性重发一次完整设备快照，刷新 Server DB 里的
    # Device.last_seen_at / 基础元数据。否则首页会只看到旧 DB 行，而 Agent
    # 心跳仍然很新，形成"能进入工作台但设备总览像假死"的分裂状态。
    device_snapshot_refresh_sec: float = 30.0

    # 内部
    _ws: Any = None
    # ⚠ Event 不能在 __init__ 阶段创建。Python 3.9 的 asyncio.Event 会在
    # __init__ 里捕获"当前 event loop"，而我们这里 client 是在 asyncio.run()
    # 之前就实例化了——它会绑到一个 stub loop，等真正进入 run_forever 之后
    # await self._stop.wait() 就会抛 "got Future attached to a different loop"。
    # 所以这里只占位，真正 Event 在 run_forever 里懒创建。
    _stop: Optional[asyncio.Event] = None
    _connected: Optional[asyncio.Event] = None
    _last_serials: set = field(default_factory=set)

    def _ensure_events(self) -> None:
        """在 running loop 里创建 Event，保证它们绑在正确的 loop 上。"""
        if self._stop is None:
            self._stop = asyncio.Event()
        if self._connected is None:
            self._connected = asyncio.Event()

    def on(self, msg_type: str, handler: Handler) -> None:
        self.handlers[msg_type] = handler

    def on_connect(self, handler: ConnectHandler) -> None:
        self.connect_handlers.append(handler)

    async def send(self, payload: Dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except ConnectionClosed:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS send 异常：{}", exc)
            return False

    async def stop(self) -> None:
        self._ensure_events()
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def run_forever(self) -> None:
        """主循环：连接 → 跑会话 → 断开 → 退避 → 重连。"""
        self._ensure_events()
        attempt = 0
        url = _build_url(self.ws_url, self.token)
        while not self._stop.is_set():
            try:
                logger.info("连接 Server WS {}", _mask_token(url))
                async with ws_connect(url, ping_interval=None, max_size=None) as ws:
                    self._ws = ws
                    self._connected.set()
                    attempt = 0
                    await self._on_connected()
                    for handler in list(self.connect_handlers):
                        try:
                            await handler(self)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("connect handler 异常：{}", exc)
                    await self._session_loop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS 会话结束/失败：{}", exc)
            finally:
                self._ws = None
                self._connected.clear()

            if self._stop.is_set():
                break
            delay = _BACKOFF_STEPS[min(attempt, len(_BACKOFF_STEPS) - 1)]
            attempt += 1
            logger.info("{:.1f}s 后重连 (第 {} 次)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    async def _on_connected(self) -> None:
        devices = self.device_provider() if self.device_provider else []
        self._last_serials = {d.get("serial") for d in devices if d.get("serial")}
        await self._send_hello(devices)
        logger.info("hello sent | devices={}", sorted(self._last_serials))

    async def _send_hello(self, devices: List[Dict[str, Any]]) -> bool:
        return await self.send(
            {
                "type": P.MSG_HELLO,
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "host_os": self.host_os,
                "devices": devices,
            }
        )

    async def _session_loop(self) -> None:
        """会话期：并发跑 recv / 心跳 / 设备重扫描。"""
        recv_task = asyncio.create_task(self._recv_loop(), name="ws-recv")
        ping_task = asyncio.create_task(self._ping_loop(), name="ws-ping")
        scan_task = asyncio.create_task(self._rescan_loop(), name="ws-rescan")
        done, pending = await asyncio.wait(
            {recv_task, ping_task, scan_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (ConnectionClosed, asyncio.CancelledError)):
                logger.warning("task {} 异常：{}", t.get_name(), exc)

    async def _recv_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                logger.warning("收到非 JSON 消息，丢弃")
                continue
            t = msg.get("type")
            if t == P.MSG_PING:
                await self.send({"type": P.MSG_PONG, "ts": msg.get("ts")})
                continue
            if t == P.MSG_PONG:
                continue
            handler = self.handlers.get(t)
            if handler is None:
                logger.debug("无 handler 的消息 type={}", t)
                continue
            try:
                await handler(self, msg)
            except Exception as exc:  # noqa: BLE001
                logger.exception("handler {} 异常：{}", t, exc)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            if self._ws is None:
                return
            await self.send({"type": P.MSG_PING, "ts": time.time()})

    async def _rescan_loop(self) -> None:
        if self.device_provider is None:
            logger.warning("rescan_loop: 没有 device_provider，热拔插不工作")
            return
        logger.info("rescan_loop 启动，间隔 {:.1f}s", self.rescan_interval)
        tick = 0
        refresh_ticks = _ticks_for_interval(
            self.rescan_interval, self.device_snapshot_refresh_sec
        )
        while True:
            await asyncio.sleep(self.rescan_interval)
            tick += 1
            if self._ws is None:
                logger.info("rescan_loop 退出：ws 已断开 (tick={})", tick)
                return
            try:
                # 同步函数；理论上 adb.list() ~50ms，pmd3.usbmux.list_devices ~30ms
                # 万一 USB 探测很慢，放线程池避免卡 event loop
                loop = asyncio.get_running_loop()
                devices = await loop.run_in_executor(None, self.device_provider)
            except Exception as exc:  # noqa: BLE001
                logger.exception("device_provider 异常：{}", exc)
                continue
            cur = {d.get("serial") for d in devices if d.get("serial")}
            if cur != self._last_serials:
                logger.info("设备集合变化 {} → {} (tick={})",
                            sorted(self._last_serials), sorted(cur), tick)
                self._last_serials = cur
                await self._send_hello(devices)
            elif tick % refresh_ticks == 0:
                sent = await self._send_hello(devices)
                logger.debug(
                    "rescan_loop keepalive sent={} tick={} serials={}",
                    sent, tick, sorted(cur),
                )


# --------------------------------------------------------------------- helpers

def _ticks_for_interval(scan_interval: float, refresh_interval: float) -> int:
    try:
        scan = max(0.1, float(scan_interval))
        refresh = max(scan, float(refresh_interval))
    except Exception:  # noqa: BLE001
        return 1
    return max(1, int(math.ceil(refresh / scan)))


def normalize_server_address(raw: str) -> tuple[str, str]:
    """把用户输入的 Server 地址归一成 Agent 需要的 WS URL + HTTP base。

    支持三种输入：
    - ``http://server:8000``  -> ``ws://server:8000/ws/agent`` + HTTP base
    - ``https://server``      -> ``wss://server/ws/agent`` + HTTP base
    - ``ws://server/ws/agent`` 直接保留 WS，并反推出 HTTP base

    这样 Agent 启动时只需要一份"Server 地址"，不要求用户理解 WS path。
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("server address is empty")
    if "://" not in value:
        value = f"http://{value}"

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError(f"unsupported server scheme: {parsed.scheme}")
    if not parsed.netloc:
        raise ValueError(f"server address missing host: {raw}")

    if scheme in {"http", "https"}:
        http_scheme = scheme
        ws_scheme = "wss" if scheme == "https" else "ws"
        base_path = _base_path_from_http_path(parsed.path)
        ws_path = _join_path(base_path, _DEFAULT_AGENT_WS_PATH)
    else:
        ws_scheme = scheme
        http_scheme = "https" if scheme == "wss" else "http"
        ws_path = parsed.path if parsed.path and parsed.path != "/" else _DEFAULT_AGENT_WS_PATH
        base_path = _base_path_from_ws_path(ws_path)

    ws_url = urlunsplit((ws_scheme, parsed.netloc, ws_path, "", ""))
    http_base = urlunsplit((http_scheme, parsed.netloc, base_path, "", "")).rstrip("/")
    return ws_url, http_base


def _base_path_from_http_path(path: str) -> str:
    p = (path or "").rstrip("/")
    if not p or p == _DEFAULT_AGENT_WS_PATH:
        return ""
    if p.endswith(_DEFAULT_AGENT_WS_PATH):
        return p[: -len(_DEFAULT_AGENT_WS_PATH)].rstrip("/")
    return p


def _base_path_from_ws_path(path: str) -> str:
    p = (path or "").rstrip("/")
    if p.endswith(_DEFAULT_AGENT_WS_PATH):
        return p[: -len(_DEFAULT_AGENT_WS_PATH)].rstrip("/")
    return ""


def _join_path(base: str, suffix: str) -> str:
    b = (base or "").rstrip("/")
    s = "/" + suffix.strip("/")
    return f"{b}{s}" if b else s

def _build_url(base: str, token: str) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={token}"


def _mask_token(url: str) -> str:
    if "token=" not in url:
        return url
    pre, _, tail = url.partition("token=")
    rest = tail.split("&", 1)
    return pre + "token=***" + ("&" + rest[1] if len(rest) == 2 else "")


def stable_agent_id(name: Optional[str] = None) -> str:
    """优先用 hostname + 随机 4 字节，确保多 Agent 不撞号。同一进程稳定。"""
    host = (name or socket.gethostname()).strip() or "agent"
    return f"{host}-{uuid.uuid4().hex[:8]}"
