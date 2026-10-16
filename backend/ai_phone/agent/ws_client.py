"""Agent 侧 WS 客户端，负责：
- 连接 Server、发送 hello、自动重连（指数退避）
- 周期心跳 + 设备扫描上报
- 接收 Server 消息 → 分发给注册的 handler
- 提供 ``send(payload)`` 接口给 runner bridge 推事件上行

保持最小可用；不处理画面帧（M2 再加）。
"""
from __future__ import annotations

import asyncio
import copy
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
# 每轮设备重扫后回调，带上当前在线 serial 集合（复用 rescan 节拍，零额外扫描）。
RescanHandler = Callable[["AgentWSClient", set], Awaitable[None]]
DeviceProvider = Callable[[], List[Dict[str, Any]]]  # 无参，返回 device dict 列表

_BACKOFF_STEPS = (1.0, 2.0, 5.0, 10.0, 15.0)
_DEFAULT_AGENT_WS_PATH = "/ws/agent"

# Only VM control work leaves the receive lane. Run, input, mirror and config
# handlers keep their existing ordering. A lane is one VM, or one platform's
# capability probe; slow discovery must not prevent stop_run / pong processing.
_VM_MESSAGES = {
    P.MSG_VM_CAPABILITY_PROBE: ("android", P.MSG_VM_CAPABILITY),
    P.MSG_HARMONY_VM_CAPABILITY_PROBE: ("harmony", P.MSG_HARMONY_VM_CAPABILITY),
    P.MSG_IOS_SIM_VM_CAPABILITY_PROBE: ("ios_sim", P.MSG_IOS_SIM_VM_CAPABILITY),
    **{t: ("android", P.MSG_VM_STATUS) for t in (P.MSG_VM_START, P.MSG_VM_STOP, P.MSG_VM_DELETE)},
    **{t: ("harmony", P.MSG_HARMONY_VM_STATUS) for t in (P.MSG_HARMONY_VM_START, P.MSG_HARMONY_VM_STOP, P.MSG_HARMONY_VM_DELETE)},
    **{t: ("ios_sim", P.MSG_IOS_SIM_VM_STATUS) for t in (P.MSG_IOS_SIM_VM_START, P.MSG_IOS_SIM_VM_STOP, P.MSG_IOS_SIM_VM_DELETE)},
}
_VM_STATUS_TYPES = {P.MSG_VM_STATUS, P.MSG_HARMONY_VM_STATUS, P.MSG_IOS_SIM_VM_STATUS}
_VM_OUTBOX_LIMIT = 128


@dataclass
class AgentWSClient:
    ws_url: str
    token: str
    agent_id: str
    agent_name: str
    server_http_base: str = ""
    host_os: str = field(default_factory=lambda: f"{platform.system()} {platform.release()}")
    # Persistent host identity is separate from agent_id (one connection owner).
    host_id: str = ""

    # 由 Agent 主程序注入
    device_provider: Optional[DeviceProvider] = None
    handlers: Dict[str, Handler] = field(default_factory=dict)
    pre_hello_handlers: List[ConnectHandler] = field(default_factory=list)
    connect_handlers: List[ConnectHandler] = field(default_factory=list)
    rescan_handlers: List[RescanHandler] = field(default_factory=list)

    # 心跳 / 重扫描间隔
    ping_interval: float = 15.0
    rescan_interval: float = 5.0
    # serial 集合不变时，也周期性重发一次完整设备快照，刷新 Server DB 里的
    # Device.last_seen_at / 基础元数据。否则首页会只看到旧 DB 行，而 Agent
    # 心跳仍然很新，形成"能进入工作台但设备总览像假死"的分裂状态。
    device_snapshot_refresh_sec: float = 30.0
    vm_command_concurrency: int = 4
    vm_command_pending_limit: int = 32

    # 内部
    _ws: Any = None
    # ⚠ Event 不能在 __init__ 阶段创建。Python 3.9 的 asyncio.Event 会在
    # __init__ 里捕获"当前 event loop"，而我们这里 client 是在 asyncio.run()
    # 之前就实例化了——它会绑到一个 stub loop，等真正进入 run_forever 之后
    # await self._stop.wait() 就会抛 "got Future attached to a different loop"。
    # 所以这里只占位，真正 Event 在 run_forever 里懒创建。
    _stop: Optional[asyncio.Event] = None
    _connected: Optional[asyncio.Event] = None
    _send_failed: Optional[asyncio.Event] = field(default=None, init=False, repr=False)
    _hello_sent: bool = field(default=False, init=False)
    _last_serials: set = field(default_factory=set)
    _last_devices: List[Dict[str, Any]] = field(default_factory=list, init=False)
    _scan_task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)
    _scan_requested: int = field(default=0, init=False)
    _scan_completed: int = field(default=0, init=False)
    _scan_force: bool = field(default=False, init=False)
    _scan_initial_hello: bool = field(default=False, init=False)
    _vm_tasks: set = field(default_factory=set, init=False, repr=False)
    _vm_lanes: Dict[tuple, asyncio.Task] = field(default_factory=dict, init=False, repr=False)
    _vm_slots: Optional[asyncio.Semaphore] = field(default=None, init=False, repr=False)
    _vm_probe_slots: Optional[asyncio.Semaphore] = field(default=None, init=False, repr=False)
    _vm_status_outbox: Dict[tuple, Dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _vm_terminal_operations: Dict[tuple, None] = field(default_factory=dict, init=False, repr=False)

    def _ensure_events(self) -> None:
        """在 running loop 里创建 Event，保证它们绑在正确的 loop 上。"""
        if self._stop is None:
            self._stop = asyncio.Event()
        if self._connected is None:
            self._connected = asyncio.Event()
        if self._send_failed is None:
            self._send_failed = asyncio.Event()

    def on(self, msg_type: str, handler: Handler) -> None:
        self.handlers[msg_type] = handler

    def on_connect(self, handler: ConnectHandler) -> None:
        self.connect_handlers.append(handler)

    def on_pre_hello(self, handler: ConnectHandler) -> None:
        self.pre_hello_handlers.append(handler)

    def on_rescan(self, handler: RescanHandler) -> None:
        """注册"每轮设备重扫后"回调（带当前在线 serial 集合）。复用 rescan 节拍，零额外扫描。"""
        self.rescan_handlers.append(handler)

    async def send(self, payload: Dict[str, Any]) -> bool:
        key = None
        if payload.get("type") in _VM_STATUS_TYPES and payload.get("vm_id"):
            key = self._vm_status_key(payload)
            if not self._buffer_vm_status(key, payload):
                return False  # Never send late progress after a terminal result.
            payload = self._vm_status_outbox[key]
        sent = await self._send_frame(payload)
        if sent and key is not None and self._vm_status_outbox.get(key) is payload:
            self._vm_status_outbox.pop(key, None)
        return sent

    @staticmethod
    def _vm_status_key(payload: Dict[str, Any]) -> tuple:
        return (
            payload["type"], str(payload["vm_id"]),
            str(payload.get("request_id") or payload.get("observed_request_id") or ""),
        )

    def _buffer_vm_status(self, key: tuple, payload: Dict[str, Any]) -> bool:
        terminal = payload.get("state") not in {"starting", "stopping"}
        if not terminal and key in self._vm_terminal_operations:
            return False
        if terminal:
            self._vm_terminal_operations[key] = None
            while len(self._vm_terminal_operations) > _VM_OUTBOX_LIMIT:
                self._vm_terminal_operations.pop(next(iter(self._vm_terminal_operations)))
        # Managers may retain and update nested runtime details after send().
        self._vm_status_outbox[key] = copy.deepcopy(payload)
        while len(self._vm_status_outbox) > _VM_OUTBOX_LIMIT:
            dropped = next(iter(self._vm_status_outbox))
            self._vm_status_outbox.pop(dropped)
            logger.error("VM 状态补发队列已满，移除最早操作 {}，需要对账确认", dropped)
        return True

    async def _flush_vm_statuses(self) -> bool:
        while self._vm_status_outbox:
            key = next(iter(self._vm_status_outbox))
            payload = self._vm_status_outbox[key]
            if not await self._send_frame(payload):
                return False
            # A newer result for this operation can arrive while ws.send drains.
            if self._vm_status_outbox.get(key) is payload:
                self._vm_status_outbox.pop(key)
        return True

    async def _send_frame(self, payload: Dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        if payload.get("type") != P.MSG_HELLO and not self._hello_sent:
            # Accepted native work can finish while the next connection is
            # reclaiming instances. Server requires hello as the first frame;
            # VM results stay in the bounded outbox; the run reporter retains
            # its own queue. Neither may precede the connection's hello.
            return False
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except ConnectionClosed:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS send 异常：{}", exc)
            # A lost lifecycle result must cause reconciliation, not leave a
            # nominally connected Agent with Server stuck at starting/stopping.
            if self._ws is ws:
                self._ensure_events()
                self._send_failed.set()
                self._hello_sent = False
                self._connected.clear()
            try:
                await asyncio.wait_for(ws.close(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
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
                    self._send_failed.clear()
                    self._hello_sent = False
                    self._connected.clear()
                    attempt = 0
                    # These hooks reclaim independent platform managers. Join
                    # every platform before discovery so managed-only filters
                    # never publish an incomplete first snapshot.
                    await asyncio.gather(*(
                        self._call_connect_handler(handler, "pre-hello")
                        for handler in list(self.pre_hello_handlers)
                    ))
                    await self._on_connected()
                    for handler in list(self.connect_handlers):
                        await self._call_connect_handler(handler, "connect")
                        if self._send_failed.is_set():
                            raise ConnectionError("WS failed while reporting recovered devices")
                    await self._session_loop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS 会话结束/失败：{}", exc)
            finally:
                self._ws = None
                self._hello_sent = False
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
    async def _call_connect_handler(self, handler: ConnectHandler, phase: str) -> None:
        try:
            await handler(self)
        except Exception as exc:  # noqa: BLE001
            logger.warning("{} handler 异常：{}", phase, exc)

    async def _on_connected(self) -> None:
        if self.device_provider is None:
            sent = await self._send_hello([], initial=True)
        else:
            sent = await self._refresh_snapshot(force=True, initial=True)
        if not sent:
            raise ConnectionError("initial Agent hello could not be sent")
        if not await self._flush_vm_statuses():
            raise ConnectionError("pending VM status could not be replayed")
        logger.info("hello sent | devices={}", sorted(self._last_serials))

    async def _send_hello(self, devices: List[Dict[str, Any]], *, initial: bool = False) -> bool:
        if not self._hello_sent and not initial:
            return False
        ws = self._ws
        sent = await self.send(
            {
                "type": P.MSG_HELLO,
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "host_os": self.host_os,
                **({"host_id": self.host_id} if self.host_id else {}),
                "devices": devices,
                "capabilities": [P.CAP_V3_PLATFORM_CACHE],
            }
        )
        if sent and self._ws is ws:
            self._hello_sent = True
            self._ensure_events()
            self._connected.set()
        return sent

    async def refresh_devices(self) -> bool:
        """Immediately send a full device snapshot if a provider is configured."""
        if self.device_provider is None:
            return False
        return await self._refresh_snapshot(force=True)

    async def _refresh_snapshot(self, *, force: bool, initial: bool = False) -> bool:
        """One provider at a time, including its shared-map writes and publish.

        A refresh arriving during a scan requests one follow-up scan, so VM
        completion cannot reuse a snapshot taken before that operation. Waiter
        cancellation never abandons a still-running provider thread.
        """
        self._scan_requested += 1
        self._scan_force = self._scan_force or force
        self._scan_initial_hello = self._scan_initial_hello or initial
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(self._scan_and_publish(), name="device-snapshot")
            self._scan_task.add_done_callback(_consume_task_error)
        return await asyncio.shield(self._scan_task)

    async def _scan_and_publish(self) -> bool:
        sent = False
        while self._scan_completed < self._scan_requested:
            requested = self._scan_requested
            force, self._scan_force = self._scan_force, False
            initial, self._scan_initial_hello = self._scan_initial_hello, False
            scanned_ws = self._ws
            devices = await asyncio.to_thread(self.device_provider)
            cur = {d.get("serial") for d in devices if d.get("serial")}
            changed = cur != self._last_serials
            if changed:
                logger.info("设备集合变化 {} → {}", sorted(self._last_serials), sorted(cur))
            self._last_serials = cur
            self._last_devices = devices
            self._scan_completed = requested
            # A canceled session may leave the native scan finishing during
            # reconnect. Only the new connection's requested follow-up may
            # publish there; never send the old session's snapshot first.
            if (changed or force) and self._ws is scanned_ws:
                sent = await self._send_hello(devices, initial=initial)
        return sent

    async def _session_loop(self) -> None:
        """会话期：并发跑 recv / 心跳 / 设备重扫描。"""
        self._ensure_events()
        recv_task = asyncio.create_task(self._recv_loop(), name="ws-recv")
        ping_task = asyncio.create_task(self._ping_loop(), name="ws-ping")
        scan_task = asyncio.create_task(self._rescan_loop(), name="ws-rescan")
        send_failure_task = asyncio.create_task(self._send_failed.wait(), name="ws-send-failure")
        done, pending = await asyncio.wait(
            {recv_task, ping_task, scan_task, send_failure_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            if t.cancelled():
                continue
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
            if t in _VM_MESSAGES:
                await self._dispatch_vm_handler(handler, msg)
            else:
                await self._call_handler(handler, msg)

    async def _call_handler(self, handler: Handler, msg: Dict[str, Any]) -> None:
        try:
            await handler(self, msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("handler {} 异常：{}", msg.get("type"), exc)
            if msg.get("type") in _VM_MESSAGES:
                await self._send_vm_failure(msg, "agent_handler_failed", str(exc))

    async def _send_vm_failure(
        self, msg: Dict[str, Any], reason: str, error: str,
        *, details: Optional[Dict[str, Any]] = None,
    ) -> None:
        _, response_type = _VM_MESSAGES[msg["type"]]
        is_probe = msg["type"].endswith("capability_probe")
        failure_details = {**(details or {}), "cleanup_confirmed": False}
        if (
            reason in {"operation_timeout", "agent_busy"}
            and failure_details.get("operation_not_started") is True
            and msg["type"] in {P.MSG_VM_START, P.MSG_HARMONY_VM_START, P.MSG_IOS_SIM_VM_START}
            and msg.get("rollback_new_instance") is True
            and msg.get("require_existing_instance") is False
        ):
            # Only Server-certified first binding has no previous instance to
            # preserve. No handler ran, so this attempt created no resources;
            # an existing restart/stop or an old payload cannot claim cleanup.
            failure_details.update(cleanup_confirmed=True, instance_created=False)
        await self.send({
            "type": response_type,
            "agent_id": self.agent_id,
            "request_id": msg.get("request_id") or "",
            "vm_id": msg.get("vm_id") or "",
            "lease_token": msg.get("lease_token") or "",
            "ok": False,
            "reason": reason,
            "error": error,
            **({} if is_probe else {
                "state": "error", "details": failure_details,
            }),
        })

    async def _dispatch_vm_handler(self, handler: Handler, msg: Dict[str, Any]) -> None:
        received_at = time.monotonic()
        if msg["type"] in {
            P.MSG_VM_STOP, P.MSG_IOS_SIM_VM_STOP, P.MSG_HARMONY_VM_STOP, P.MSG_VM_DELETE,
        } and "operation_timeout_sec" not in msg:
            # 三端旧停止指令从收件时起共用一次预算；保留 Android 删除的既有默认值。
            # 排队时间必须扣除，不能到 handler 才重新获得完整停止预算。
            msg = {**msg, "operation_timeout_sec": 120.0}
        platform_name, _ = _VM_MESSAGES[msg["type"]]
        is_probe = msg["type"].endswith("capability_probe")
        deadline = None
        if not is_probe and "operation_timeout_sec" in msg:
            try:
                budget = float(msg["operation_timeout_sec"])
                if not math.isfinite(budget):
                    raise ValueError("non-finite operation timeout")
                deadline = received_at + max(0.0, budget)
            except (TypeError, ValueError, OverflowError):
                await self._send_vm_failure(
                    msg, "operation_timeout", "操作时限无效，本次操作未执行",
                    details={"operation_not_started": True},
                )
                return
        if len(self._vm_tasks) >= max(1, self.vm_command_pending_limit):
            # Explicit rejection: never silently drop a destructive command or
            # acknowledge a slot as released when it was never touched.
            await self._send_vm_failure(
                msg, "agent_busy", "Agent 虚拟机操作队列已满，请稍后重试",
                details={"operation_not_started": True},
            )
            return
        if self._vm_slots is None:
            self._vm_slots = asyncio.Semaphore(max(1, self.vm_command_concurrency))
        if self._vm_probe_slots is None:
            self._vm_probe_slots = asyncio.Semaphore(2)
        lane = (platform_name, "probe" if is_probe else str(msg.get("vm_id") or ""))
        previous = self._vm_lanes.get(lane)

        async def run() -> None:
            if previous is not None:
                await asyncio.shield(previous)
            if deadline is not None and time.monotonic() >= deadline:
                await self._send_vm_failure(
                    msg, "operation_timeout", "排队已超过操作时限，本次操作未执行",
                    details={"operation_not_started": True},
                )
                return
            # Slow capability checks cannot consume every lifecycle slot and
            # make stopping an unrelated running VM wait for SDK discovery.
            slots = self._vm_probe_slots if is_probe else self._vm_slots
            async with slots:
                handler_msg = msg
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        await self._send_vm_failure(
                            msg, "operation_timeout", "排队已超过操作时限，本次操作未执行",
                            details={"operation_not_started": True},
                        )
                        return
                    # This is one attempt with one budget. Waiting for a VM
                    # lane or a host slot must not give the native handler a
                    # fresh timeout, and may never start an expired attempt.
                    handler_msg = {**msg, "operation_timeout_sec": remaining}
                await self._call_handler(handler, handler_msg)

        task = asyncio.create_task(run(), name=f"vm-command-{platform_name}-{lane[1]}")
        self._vm_tasks.add(task)
        self._vm_lanes[lane] = task

        def finished(done: asyncio.Task) -> None:
            self._vm_tasks.discard(done)
            if self._vm_lanes.get(lane) is done:
                self._vm_lanes.pop(lane, None)
            _consume_task_error(done)

        task.add_done_callback(finished)

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
        delay = self.rescan_interval
        while True:
            await asyncio.sleep(delay)
            started = time.monotonic()
            tick += 1
            if self._ws is None:
                logger.info("rescan_loop 退出：ws 已断开 (tick={})", tick)
                return
            try:
                await self._refresh_snapshot(force=tick % refresh_ticks == 0)
            except Exception as exc:  # noqa: BLE001
                logger.exception("device_provider 异常：{}", exc)
                delay = max(0.1, self.rescan_interval - (time.monotonic() - started))
                continue
            cur = set(self._last_serials)
            # 复用本轮扫描结果，回调订阅者（如 VM 存活巡检：发现在跑 VM 的 serial 消失即上报 stopped）
            for handler in list(self.rescan_handlers):
                try:
                    await handler(self, cur)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("rescan handler 异常（忽略）：{}", exc)
            delay = max(0.1, self.rescan_interval - (time.monotonic() - started))


# --------------------------------------------------------------------- helpers

def _consume_task_error(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.warning("task {} 异常：{}", task.get_name(), exc)

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
