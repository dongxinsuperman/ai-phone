"""Agent 进程级可靠上报队列（Distributed Agent Brain · M3）。

执行脑下沉回 Agent 后，日志 / 步骤 / 终态要像 next 一样"不丢、不乱、不重复"地
送达 Server。本模块就是那个"统一收发室"：

- **单队列**：所有 run 的上报（log / step_done / run_done）共用一个进程级队列。
- **一个 drain worker 串行发**：严格按入队顺序逐条发，发成功才出队，发失败（WS 断，
  ``send`` 返回 False）停在队头、等重连后从队头续发 —— 天然保序、且补发不乱。
- **跨 run 存活**：队列挂在进程上、不挂在单个 run 的 bridge 上。所以"某个 run 在断网
  期间结束、bridge 已销毁"时，它没发出去的 ``run_done`` 仍留在这里，等重连补发，
  不会丢。这正是 per-run 队列解决不了、必须上提到进程级的关键场景。
- **不阻塞执行**：入队只是 append（O(1)），VLM 主循环照常 fire-and-forget。
- **去重交给 Server**：每条带 ``event_id``，Server 按 ``event_id`` 去重，所以补发
  造成的重复不会重复落库。本层不做 ACK（保持简单），"发成功即出队"。

边界（如实记账）：Agent 进程崩溃 = 队列随之消失（内存队列），靠 M6 心跳兜底，不在
本层范围；队列超 ``max_queue`` 上限会丢最老消息（极端长断网的内存保护）。所以准确
表述是"**Agent 活着且队列未溢出时可靠**"，不是"绝不丢"。
"""
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

from loguru import logger

SendFn = Callable[[Dict[str, Any]], Awaitable[bool]]


class ReliableReporter:
    def __init__(self, send: SendFn, *, max_queue: int = 5000) -> None:
        self._send = send
        self._queue: Deque[Dict[str, Any]] = deque()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._max = max_queue
        self._seq = 0
        self._worker: Optional[asyncio.Task] = None
        self._stopped = False

    # ------------------------------------------------------------------
    # 入队 / 状态
    # ------------------------------------------------------------------
    async def enqueue(self, msg: Dict[str, Any]) -> None:
        """把一条上报放进队尾，分配 event_id（去重键）+ seq（入队序）。

        几乎不阻塞（只拿锁 append）。唤醒 worker 去发。
        """
        async with self._lock:
            self._seq += 1
            msg.setdefault("event_id", uuid.uuid4().hex)
            msg["seq"] = self._seq
            self._queue.append(msg)
            self._trim_locked()
        self._wake.set()

    def _trim_locked(self) -> None:
        """超上限丢最老（须持锁）。极端长断网的内存兜底，正常不触发。"""
        while len(self._queue) > self._max:
            dropped = self._queue.popleft()
            logger.error(
                "可靠上报队列超上限 {}，丢弃最老消息 seq={} type={}",
                self._max, dropped.get("seq"), dropped.get("type"),
            )

    def pending(self) -> int:
        return len(self._queue)

    def notify_connected(self) -> None:
        """WS（重）连后调用：唤醒 worker 立即从队头续发。"""
        self._wake.set()

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def flush(self) -> None:
        """串行把队头逐条发出：发成功才出队；发失败（断连）停在队头返回，等下次。

        全程持锁，保证严格保序、且不和 enqueue 交错出队。worker 与（测试里的）
        手动 flush 都走这里。
        """
        async with self._lock:
            while self._queue:
                head = self._queue[0]
                ok = False
                try:
                    ok = bool(await self._send(head))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("可靠上报发送异常，停在队头等重连：{}", exc)
                    return
                if not ok:
                    # 连接断：停在队头，保序等待下次 flush（重连 notify 或周期重试）
                    return
                self._queue.popleft()

    # ------------------------------------------------------------------
    # 后台 worker 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stopped = False
            self._worker = asyncio.create_task(self._loop(), name="reliable-reporter")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._worker is not None:
            try:
                await self._worker
            except Exception:  # noqa: BLE001
                pass
            self._worker = None

    async def _loop(self) -> None:
        while not self._stopped:
            await self.flush()
            if self._stopped:
                break
            self._wake.clear()
            if self._queue:
                # flush 没发完（断连），等重连 notify_connected 唤醒；兜底 2s 周期重试，
                # 避免没人 notify 时永久挂起。
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            else:
                # 队列已空：阻塞等下一条 enqueue / 重连唤醒，不空转。
                await self._wake.wait()
