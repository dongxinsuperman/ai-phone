"""三端 readiness 探活实现。

设计准则（严格，v1 不可变）：
1. **只读、旁路**：不复用也不修改任何 Driver / Mirror / Runner 主路径；
   即便导入了已有模块，也仅用它们的"读操作"（比如 adbutils 建立一个独立
   device handle 发 shell 命令），不碰任何持久缓存。
2. **不自救**：不触发解锁、不触发 WDA 重启、不触发 hmdriver2 重连。
3. **失败容忍**：任何 probe 异常都被吞掉，统一返回 ``driver_probe_failed``。
4. **超时可控**：每次 probe 都强制在 ``readiness_probe_timeout_sec`` 内返回，
   防止单台设备卡死拖垮整个轮询器。

每个 probe 通过 :meth:`BaseProbe.probe` 返回 :class:`ProbeOutcome`，上层
supervisor 再结合"连续失败阈值"决定是否真的降级。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from ai_phone.config import get_settings


# ---------------------------------------------------------------------------
# 结果 dataclass
# ---------------------------------------------------------------------------
@dataclass
class ProbeOutcome:
    """单次 probe 返回结果。

    - ``ready=True`` 表示本次探测通过，``not_ready_reason`` 与 ``hint`` 必为 None / ""。
    - ``ready=False`` 表示本次探测未通过，``not_ready_reason`` 必填。
    """

    ready: bool
    not_ready_reason: Optional[str] = None  # v1 五个枚举之一（见 protocol.py）
    hint: str = ""


def _ok() -> ProbeOutcome:
    return ProbeOutcome(ready=True, not_ready_reason=None, hint="")


def _fail(reason: str, hint: str = "") -> ProbeOutcome:
    return ProbeOutcome(ready=False, not_ready_reason=reason, hint=hint)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------
class BaseProbe:
    """每个平台一种 probe，serial 在构造时绑定。

    子类只需覆盖 :meth:`_do_probe`。:meth:`probe` 负责统一包一层异常 / 超时。
    """

    platform: str = "base"

    def __init__(self, serial: str, *, timeout_sec: float = 3.0) -> None:
        self.serial = serial
        self.timeout_sec = float(timeout_sec)

    async def probe(self) -> ProbeOutcome:
        try:
            return await asyncio.wait_for(self._do_probe(), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            logger.debug(
                "[readiness:{}:{}] probe 超时 (>{:.1f}s)",
                self.platform, self.serial, self.timeout_sec,
            )
            return _fail("driver_probe_failed", f"probe 超时 {self.timeout_sec:.1f}s")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[readiness:{}:{}] probe 异常：{}",
                self.platform, self.serial, exc,
            )
            return _fail("driver_probe_failed", str(exc))

    async def _do_probe(self) -> ProbeOutcome:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Android：adb + 锁屏检测
# ---------------------------------------------------------------------------
class AndroidProbe(BaseProbe):
    """Android readiness 判据：

    - ``online``：adbutils 能读到该 serial，且状态为 ``device`` / ``unauthorized`` 外；
    - ``ready`` 增量：
        * 屏幕已亮（``dumpsys power`` 里 ``Display Power: state=ON``）
        * 未锁（``dumpsys window`` 里 ``mDreamingLockscreen=false`` / 没有 keyguard showing）

    统一用 ``adb shell dumpsys`` 一次拿所有需要的东西，避免多次 roundtrip。
    """

    platform = "android"

    async def _do_probe(self) -> ProbeOutcome:
        # adb 操作全是阻塞 I/O，扔到线程池不阻塞事件循环
        return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self) -> ProbeOutcome:
        try:
            from adbutils import adb  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"adbutils 未安装：{exc}")

        # 独立 device handle，不复用 AndroidDriver 缓存；adbutils 的 device() 很轻
        try:
            device = adb.device(serial=self.serial)
        except Exception as exc:  # noqa: BLE001
            return _fail("adb_offline", f"adb device 找不到：{exc}")

        # 1) 先摸一条最轻的 shell，确认 adb 通路活着；不通 → adb_offline
        try:
            echo = device.shell("echo ok")
        except Exception as exc:  # noqa: BLE001
            return _fail("adb_offline", f"shell 失败：{exc}")
        if "ok" not in (echo or ""):
            return _fail("adb_offline", f"shell 返回异常：{echo!r}")

        # 2) 屏幕电源状态（dumpsys power 含 "Display Power: state=..."）
        try:
            power_out = device.shell("dumpsys power | grep -E 'Display Power|mWakefulness='")
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"dumpsys power 失败：{exc}")
        if "state=OFF" in power_out or "state=DOZE" in power_out:
            if get_settings().android_screen_off_dispatchable:
                logger.debug(
                    "[readiness:android:{}] screen off but dispatchable by env",
                    self.serial,
                )
                return _ok()
            return _fail("screen_locked", "屏幕熄屏")

        # 3) 锁屏状态。mDreamingLockscreen 在 Android 10+ 一直可用；Android 14+
        #    很多 ROM 改叫 mKeyguardOccluded / mShowingLockscreen。我们接受任一
        #    "显式处于锁屏态"的信号。
        try:
            win_out = device.shell(
                "dumpsys window | grep -E 'mDreamingLockscreen|mShowingLockscreen|mKeyguardOccluded'"
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"dumpsys window 失败：{exc}")
        # 有任一锁屏标志为 true 时，还要区分"普通锁屏外壳"和"安全认证锁"。
        # Samsung / One UI AOD 会返回 mDreamingLockscreen=true，但 secure=false 且
        # deviceLocked=0；这种能被 Run 前 KEYCODE_WAKEUP + dismiss-keyguard 收掉。
        if _android_keyguard_showing(win_out):
            settings = get_settings()
            if (
                settings.android_screen_off_dispatchable
                and settings.android_wake_before_run
                and _android_keyguard_can_be_dismissed(device)
            ):
                logger.debug(
                    "[readiness:android:{}] non-secure keyguard but dispatchable by env",
                    self.serial,
                )
                return _ok()
            return _fail("screen_locked", "锁屏中，请解锁 Android")

        return _ok()


def _android_keyguard_showing(win_out: str) -> bool:
    return (
        "mDreamingLockscreen=true" in win_out
        or "mShowingLockscreen=true" in win_out
        or "showing=true" in win_out
    )


def _android_keyguard_can_be_dismissed(device) -> bool:
    """Return True only for non-secure keyguard layers.

    This is intentionally conservative: if the secure/deviceLocked signals cannot
    be read, readiness stays not-ready and asks the user to unlock manually.
    """

    try:
        policy_out = device.shell(
            "dumpsys window policy | grep -E 'showing=|secure=|screenState=|interactiveState='"
        )
        trust_out = device.shell(
            "dumpsys trust | grep -E 'deviceLocked|strongAuthRequired|trusted|trustManaged'"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[readiness:android] keyguard security probe failed: {}", exc)
        return False

    if "secure=true" in policy_out:
        return False
    if "deviceLocked=1" in trust_out or "deviceLocked=true" in trust_out:
        return False
    if "strongAuthRequired=0x0" not in trust_out:
        return False
    return "secure=false" in policy_out and (
        "deviceLocked=0" in trust_out or "deviceLocked=false" in trust_out
    )


# ---------------------------------------------------------------------------
# iOS：WDA /status + /wda/locked + MJPEG 9100
# ---------------------------------------------------------------------------
class IosProbe(BaseProbe):
    """iOS readiness 判据：

    - ``ready`` 增量：
        * 已有 WDA 客户端（:mod:`ai_phone.agent.drivers.ios._WDA_CLIENT_MAP` 登记过）
        * ``GET /status`` 返回 200
        * ``GET /wda/locked`` 返回 ``value=false``

    v1 先只读 ``_WDA_CLIENT_MAP`` 拿 base_url，不反向触发 launcher。
    WDA 还没起来 → ``wda_not_ready``。

    **特别注意**：MJPEG 9100 端口是"按需"资源——只在 browser 订阅 mirror 时
    usbmux 才会把设备 9100 端口转发到本地，没人看镜像时本地 9100 本来就不通。
    因此这里 **不** 把 9100 连通性作为 readiness 判据，否则会把"没人在看
    镜像的健康设备"误标为未就绪。能不能推流是 mirror 的事，和"能不能被派单"
    是两码事。
    """

    platform = "ios"

    async def _do_probe(self) -> ProbeOutcome:
        return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self) -> ProbeOutcome:
        try:
            from ai_phone.agent.drivers.ios import _WDA_CLIENT_MAP  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _fail("wda_not_ready", f"iOS 支持未启用：{exc}")

        cli = _WDA_CLIENT_MAP.get(self.serial)
        if cli is None:
            return _fail("wda_not_ready", self._wda_not_ready_hint())

        base_url = getattr(cli, "base_url", None) or ""
        if not base_url:
            return _fail("wda_not_ready", "WDA 客户端无 base_url")

        try:
            import httpx  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"httpx 未安装：{exc}")

        try:
            resp = httpx.get(f"{base_url.rstrip('/')}/status", timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            return _fail("wda_not_ready", f"/status 不通：{exc}")
        if resp.status_code != 200:
            return _fail("wda_not_ready", f"/status HTTP {resp.status_code}")

        try:
            locked_resp = httpx.get(f"{base_url.rstrip('/')}/wda/locked", timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"/wda/locked 不通：{exc}")
        if locked_resp.status_code != 200:
            return _fail("driver_probe_failed", f"/wda/locked HTTP {locked_resp.status_code}")
        try:
            locked_val = bool(locked_resp.json().get("value"))
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"/wda/locked 解析失败：{exc}")
        if locked_val:
            if get_settings().ios_screen_off_dispatchable:
                logger.debug(
                    "[readiness:ios:{}] locked/screen off but dispatchable by env",
                    self.serial,
                )
                return _ok()
            return _fail("screen_locked", "iPhone 锁屏中，请解锁")

        return _ok()

    @staticmethod
    def _wda_not_ready_hint() -> str:
        try:
            from ai_phone.agent.drivers.ios_wda_lifecycle import (  # noqa: PLC0415
                get_ios_wda_lifecycle_policy,
            )

            policy = get_ios_wda_lifecycle_policy()
        except Exception:  # noqa: BLE001
            return "WDA 还没起，插上设备后会自动预热"

        if policy.is_stable:
            if policy.allow_initial_spawn_in_stable:
                return "WDA 还没起；stable 模式不会插线预热，请进入工作台或跑任务触发本次 USB 会话首次启动"
            return "WDA 还没起；stable attach-only 模式要求先手动启动 WDA"
        return "WDA 还没起，插上设备后会自动预热"


# ---------------------------------------------------------------------------
# HarmonyOS：hdc shell 屏幕状态 + hdc targets 在线
# ---------------------------------------------------------------------------
class HarmonyProbe(BaseProbe):
    """HarmonyOS readiness 判据（v1 简化版）：

    - ``ready`` 增量：
        * ``hdc list targets`` 能看到该 serial
        * ``hdc shell hidumper -s PowerManagerService -a -s`` 里能解出"屏幕亮"
          （"Current State: AWAKE" 或 "Screen On: true"）

    v1 暂不主动 probe hmdriver2 socket —— 那条通路握手代价较高，且在执行时会
    自动 reconnect（已经在 driver 里做了）。屏幕息屏是目前最主要的"online 但
    不能跑"场景，先拦这个。
    """

    platform = "harmony"

    async def _do_probe(self) -> ProbeOutcome:
        return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self) -> ProbeOutcome:
        try:
            from ai_phone.agent.drivers.hdc import hdc_run, hdc_shell  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"hdc 模块未启用：{exc}")

        # 1) hdc list targets 确认设备还在线
        try:
            targets = hdc_run("list", "targets", timeout=self.timeout_sec)
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"hdc list targets 失败：{exc}")
        if self.serial not in (targets or ""):
            # 真正拔线时上层 device_provider 自己会发 hello 把设备摘掉；
            # 这里走到只可能是 hdc 短暂抖动
            return _fail("driver_probe_failed", "hdc list targets 未见该 serial")

        # 2) 屏幕状态：hidumper 输出里一般有 "Current State: AWAKE/INACTIVE/..."
        try:
            out = hdc_shell(
                self.serial,
                "hidumper -s PowerManagerService -a -s",
                timeout=self.timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_probe_failed", f"hidumper 失败：{exc}")
        if out and _harmony_screen_is_off(out):
            if get_settings().harmony_screen_off_dispatchable:
                logger.debug(
                    "[readiness:harmony:{}] screen off but dispatchable by env",
                    self.serial,
                )
                return _ok()
            return _fail("screen_locked", "鸿蒙设备屏幕息屏中，请点亮并保持亮屏")

        return _ok()


def _harmony_screen_is_off(hidumper_out: str) -> bool:
    """从 PowerManagerService 的 -a -s 输出判断屏幕是否处于熄屏/睡眠态。"""
    text = hidumper_out or ""
    # 尽量宽松：命中任意一条明显"关屏"关键字就判为 off
    for needle in (
        "Current State: INACTIVE",
        "Current State: SLEEP",
        "Current State: Standby",
        "Screen On: false",
        "screenOn: false",
    ):
        if needle in text:
            return True
    return False


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_probe_for(platform: str, serial: str, *, timeout_sec: float = 3.0) -> Optional[BaseProbe]:
    """按 platform 建对应 probe；未知平台返回 None（上层默认视为 ready）。"""
    if platform == "android":
        return AndroidProbe(serial, timeout_sec=timeout_sec)
    if platform == "ios":
        return IosProbe(serial, timeout_sec=timeout_sec)
    if platform == "harmony":
        return HarmonyProbe(serial, timeout_sec=timeout_sec)
    return None
