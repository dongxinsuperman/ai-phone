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
# async：内部走 ws_client.send，返回本次是否真正写入 WS。
MessageSender = Callable[[Dict], Awaitable[bool]]


class _State:
    """每个设备的内存状态。

    ``ready`` 是最近一次 **广播出去** 的状态。**默认 False**——新建状态视作
    "尚未被 probe 盖章"，必须等首轮 probe 成功才能翻 True。这条改动堵的是
    "设备 online + probe 未完 → 调度器派单到未准备好设备" 的历史窗口（详见
    docs/ios-setup（iOS接入指南）.md）。

    ``ever_ready`` 标记本设备**历史上是否曾经被 probe 证明为 ready**。
    它决定 ``_tick_once`` 失败分支走哪条路径：
      - ``False`` （还没成功过）：单次 probe 失败 = 立即降级 ready=False，不走防抖
        阈值（避免让"从未 ready 的新设备" 借防抖窗口冒充 ready）。
      - ``True``（已经 ready 过）：保持原"连续失败到阈值才降级"的稳态防刷语义。

    ``consecutive_fail`` 是连续探测失败次数，仅用于"已 ready 设备的降级防抖"。
    """

    __slots__ = ("ready", "reason", "hint", "consecutive_fail", "ever_ready")

    def __init__(self) -> None:
        self.ready: bool = False
        # 用合法的协议枚举做"首次未盖章" 兜底（NotReadyReason 见 shared/protocol.py）。
        # 首轮 probe 跑完后会被真实结果覆盖，前端看不到这个值（_maybe_send 防抖会
        # 在第一次 probe 后才决定要不要广播）。
        self.reason: Optional[str] = "driver_probe_failed"
        self.hint: str = ""
        self.consecutive_fail: int = 0
        self.ever_ready: bool = False


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

    def mark_all_dirty(self) -> None:
        """Force the next tick to resend readiness for known devices.

        Server-side readiness is an in-memory snapshot tied to the current WS
        connection. If the Agent reconnects while the local probe state did not
        change, the normal de-dupe path would otherwise skip the resend and the
        UI/scheduler could keep a stale not_ready value.
        """
        self._last_sent.clear()

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

        # 1.5) iOS USB 扫描不可信短路（配合 _apply_ios_snapshot_freshness）
        #
        # 背景：drivers/ios.py 的 _IOS_SCAN_LAST_OK 在 usbmuxd 抖动（list_devices
        # 抛异常或单次返空未达 streak 阈值）时会标记为 False；此时
        # _apply_ios_snapshot_freshness 会把上一份可信 iOS 快照重新喂给
        # _record_serial_platform，让 UI / hub 维持稳定，但 USB 通路本身是
        # 可疑的——继续做 WDA probe 既不准（多半超时）也没意义。
        #
        # 更要紧的是漏洞防护：_State 默认乐观初始化 ready=True + 防抖阈值=3，
        # 意味着新接入设备 / 抖动恢复期内 probe 失败也不会立刻降级；若快照保鲜
        # 同时让设备保持 status=online，调度器 _pick_device 看到 online+ready=True
        # 就会派单到一台 USB 状态可疑的 iOS。短路逻辑：scan 不可信 → 把所有 iOS
        # 设备状态强制写成 ready=False / reason=usb_scan_unreliable，
        # 跳过本轮 probe；scan 恢复 → 下一轮走正常 probe 路径，
        # _maybe_send 自然演化回 ready=True。
        #
        # auto 模式同样会经过 _apply_ios_snapshot_freshness，本短路对 auto 无害——
        # 最多让 auto 模式抖动期间延后 1~2 个 tick 才派单，不影响自愈兜底。
        ios_scan_unreliable = False
        try:
            from ai_phone.agent.drivers.ios import was_last_ios_scan_ok  # noqa: PLC0415

            ios_scan_unreliable = not was_last_ios_scan_ok()
        except Exception:  # noqa: BLE001
            # iOS 模块整体不可用（环境没装 pmd3 / 平台禁用）→ 没有 iOS 设备需要
            # 保护，安全 no-op
            ios_scan_unreliable = False

        probe_targets: List[DeviceKey] = list(snapshot)
        if ios_scan_unreliable:
            ios_keys = [k for k in snapshot if k[1] == "ios"]
            for key in ios_keys:
                state = self._states.setdefault(key, _State())
                # 不动 consecutive_fail / ever_ready：这不是一次 probe 失败，是 USB
                # 通路暂不可信。等 scan 恢复后由正常 probe 路径接管计数与盖章。
                # reason 必须落在 shared/protocol.py NotReadyReason 枚举内——
                # 用 driver_probe_failed 兜底，USB 抖动的具体描述写在 hint 里。
                state.ready = False
                state.reason = "driver_probe_failed"
                state.hint = "iOS USB 扫描暂不可信，调度暂停派单；通常几秒内自愈"
                await self._maybe_send(key, state)
            if ios_keys:
                probe_targets = [k for k in snapshot if k[1] != "ios"]
                logger.debug(
                    "[readiness] iOS USB scan 不可信，跳过本轮 iOS probe，"
                    "强制 {} 个 iOS 设备 ready=False（driver_probe_failed / USB 抖动）",
                    len(ios_keys),
                )

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
            *[_probe_one(k) for k in probe_targets], return_exceptions=False
        )

        # 3) 结合连续失败阈值，决定是否升/降级 + 是否上报
        #
        # 防抖语义（v2，配合 §12.1 P1 修复）：
        #   - 设备**从未**被 probe 证明为 ready（ever_ready=False）→ 单次 probe
        #     失败立即广播 ready=False。新设备/agent 重启后 / 抖动恢复后第一轮
        #     probe 失败不再借"未达阈值"窗口冒充 ready，调度器 _pick_device
        #     不会被乐观默认骗到。
        #   - 设备**曾经** ready 过（ever_ready=True）→ 沿用原"连续失败到阈值
        #     才降级"的稳态防刷语义，避免一次偶发 probe 失败把已 ready 的设备
        #     翻车，UI/调度器不会被抖动刷屏。
        for key, outcome in results:
            if outcome is None:
                continue
            state = self._states.setdefault(key, _State())

            if outcome.ready:
                state.consecutive_fail = 0
                state.ever_ready = True
                new_ready = True
                new_reason: Optional[str] = None
                new_hint = ""
            else:
                state.consecutive_fail += 1
                # 首次盖章前 OR 累计失败到阈值 → 立刻降级；其它情况保持上次广播态。
                if state.consecutive_fail >= fail_threshold or not state.ever_ready:
                    new_ready = False
                    new_reason = outcome.not_ready_reason
                    new_hint = outcome.hint
                else:
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
            sent = await self._send(msg)
            if not sent:
                logger.debug(
                    "[readiness] device_readiness 未发送成功 key={} ready={} reason={}",
                    key,
                    state.ready,
                    state.reason,
                )
                return
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
