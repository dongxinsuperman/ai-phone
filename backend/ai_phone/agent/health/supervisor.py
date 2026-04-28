"""ReadinessSupervisor —— 旁路 probe 轮询 + 上报。

生命周期：
- 在 agent 启动时 `start()`；在 agent 关闭时 `stop()`。
- 内部一个 asyncio task 每 ``readiness_poll_sec`` 秒跑一轮，按当前已知的
  (serial, platform) 列表并发调用各自的 probe；
- 维护 "连续失败计数 + 最近上报状态" 两份内存；连续失败到阈值才真正降级；
- 状态**发生变化**（ready↔not_ready 或 reason 变了）时才 send 一条
  :data:`MSG_DEVICE_READINESS`；稳态期静默，避免刷 WS。

外部依赖（全是只读 callable，不持有 Driver / Mirror 对象引用）：
- ``device_lister()`` —— 返回当前设备快照，形如 [(serial, platform), ...]
  我们从 ws_client 已经拿在手里的 _record_serial_platform 派生即可。
- ``send_message(dict)`` —— 把一条 :data:`MSG_DEVICE_READINESS` 丢回 Server。

Supervisor 不关心"设备集合怎么变"，每轮都按最新快照重新迭代。
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from ai_phone.config import get_settings
from ai_phone.shared import protocol as P

from .probe import BaseProbe, ProbeOutcome, build_probe_for


# (serial, platform)
DeviceKey = Tuple[str, str]

# 注入的"取设备快照"回调 —— 返回 [(serial, platform), ...]。同步函数，轻量调用即可。
DeviceLister = Callable[[], Iterable[Tuple[str, str]]]

# 注入的"发消息"回调 —— 接收一个 dict（已按 MSG_DEVICE_READINESS schema 组装好）。
# async：内部走 ws_client.send。
MessageSender = Callable[[Dict], Awaitable[None]]


class _State:
    """每个设备的内存状态。

    ``ready`` 是最近一次 **广播出去** 的状态（初始化为 True，假设 online 即 ready
    直到 probe 打脸）；``consecutive_fail`` 是连续探测失败次数，用于防抖。
    """

    __slots__ = ("ready", "reason", "hint", "consecutive_fail")

    def __init__(self) -> None:
        self.ready: bool = True
        self.reason: Optional[str] = None
        self.hint: str = ""
        self.consecutive_fail: int = 0


class ReadinessSupervisor:
    def __init__(
        self,
        *,
        device_lister: DeviceLister,
        send_message: MessageSender,
    ) -> None:
        self._device_lister = device_lister
        self._send = send_message
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._states: Dict[DeviceKey, _State] = {}
        # 记录上次已发送 readiness 的 (ready, reason) 元组，变化才发；初值 None 会强制第一次 send。
        self._last_sent: Dict[DeviceKey, Tuple[bool, Optional[str]]] = {}

    # ---------------- lifecycle ----------------
    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if self._task is not None and not self._task.done():
            return
        settings = get_settings()
        if not settings.readiness_enabled:
            logger.info("[readiness] 已关闭 (AI_PHONE_READINESS_ENABLED=false)，不启动 supervisor")
            return
        loop = loop or asyncio.get_event_loop()
        self._stop = asyncio.Event()
        self._task = loop.create_task(self._run(), name="readiness-supervisor")
        logger.info(
            "[readiness] supervisor 启动 | interval={:.1f}s fail_threshold={} timeout={:.1f}s",
            settings.readiness_poll_sec,
            settings.readiness_fail_threshold,
            settings.readiness_probe_timeout_sec,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    # ---------------- main loop ----------------
    async def _run(self) -> None:
        settings = get_settings()
        interval = float(settings.readiness_poll_sec)

        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._tick_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[readiness] tick 异常：{}", exc)

            # 固定周期的 drift-safe sleep
            elapsed = time.monotonic() - started
            remain = max(0.5, interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=remain)
            except asyncio.TimeoutError:
                continue
            else:
                break  # _stop 被 set

        logger.info("[readiness] supervisor 已退出")

    async def _tick_once(self) -> None:
        settings = get_settings()
        timeout_sec = float(settings.readiness_probe_timeout_sec)
        fail_threshold = int(settings.readiness_fail_threshold)

        try:
            snapshot: List[DeviceKey] = [
                (str(s), str(p))
                for (s, p) in self._device_lister()
                if s and p
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[readiness] 取设备快照失败：{}", exc)
            return

        # 1) 清理已经不存在的设备（拔线）
        current_keys = set(snapshot)
        for k in list(self._states.keys()):
            if k not in current_keys:
                self._states.pop(k, None)
                self._last_sent.pop(k, None)

        # 2) 为每个设备建 probe 并并发执行
        async def _probe_one(key: DeviceKey) -> Tuple[DeviceKey, Optional[ProbeOutcome]]:
            serial, platform = key
            probe: Optional[BaseProbe] = build_probe_for(
                platform, serial, timeout_sec=timeout_sec
            )
            if probe is None:
                # 未知平台：默认视为 ready，不上报
                return key, None
            outcome = await probe.probe()
            return key, outcome

        results = await asyncio.gather(
            *[_probe_one(k) for k in snapshot], return_exceptions=False
        )

        # 3) 结合连续失败阈值，决定是否升/降级 + 是否上报
        for key, outcome in results:
            if outcome is None:
                continue
            state = self._states.setdefault(key, _State())

            if outcome.ready:
                # 成功 → 清计数，状态置回 ready
                state.consecutive_fail = 0
                new_ready = True
                new_reason: Optional[str] = None
                new_hint = ""
            else:
                state.consecutive_fail += 1
                if state.consecutive_fail >= fail_threshold:
                    new_ready = False
                    new_reason = outcome.not_ready_reason
                    new_hint = outcome.hint
                else:
                    # 还没到阈值，保持上次已广播的状态
                    new_ready = state.ready
                    new_reason = state.reason
                    new_hint = state.hint

            state.ready = new_ready
            state.reason = new_reason
            state.hint = new_hint

            await self._maybe_send(key, state)

    async def _maybe_send(self, key: DeviceKey, state: _State) -> None:
        prev = self._last_sent.get(key)
        cur = (state.ready, state.reason)
        if prev == cur:
            return

        serial, platform = key
        msg = {
            "type": P.MSG_DEVICE_READINESS,
            "serial": serial,
            "platform": platform,
            "ready": state.ready,
            "not_ready_reason": state.reason,
            "hint": state.hint,
            "fail_streak": state.consecutive_fail,
            "ts": time.time(),
        }
        try:
            await self._send(msg)
            self._last_sent[key] = cur
            if state.ready:
                logger.info(
                    "[readiness] {}:{} 恢复 ready",
                    platform, serial,
                )
            else:
                logger.warning(
                    "[readiness] {}:{} 降级 not_ready reason={} hint={!r} fail_streak={}",
                    platform, serial, state.reason, state.hint, state.consecutive_fail,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[readiness] 发送 device_readiness 失败 key={}：{}", key, exc)
