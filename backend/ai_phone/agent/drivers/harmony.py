"""HarmonyOS 驱动：hdc + hmdriver2 组合实现 BaseDriver。

**风格对齐 Android / iOS**：
- 底座 = ``hdc``（类比 adb / pymobiledevice3 usbmux）
- 主通道 = ``hmdriver2.Driver``（类比 scrcpy-control-socket / WDA HTTP session）
  * 通过设备端 ``uitest`` socket daemon 低延时调用，10-30ms 单次往返
  * Driver 是按 serial 的 singleton，多次 new 返回同一实例（和 _driver_cache 语义一致）
  * ``@cached_property`` 缓存 display_size / display_rotation，**旋转后必须手动失效**
- 补位 = ``hdc shell``（截图兜底 / 设备元信息 / uitest daemon 掉线时的临时恢复）

**对测试团队的承诺**：
- ``get_raw_driver()`` 暴露原生 ``hmdriver2.Driver``，可直接写 XPath / 控件树脚本
- ai-phone 不拦截 hmdriver2 的任何能力，BaseDriver 只是"给 VLM runner 用的窄接口"

**已知边界**（P2 落地后需要在真机上验证 / 补位）：
- ``hmdriver2.long_click`` 没有 duration 参数，固定约 1s；``long_press(x, y, >1000)``
  我们用 swipe 同起止点模拟
- ``hmdriver2.swipe`` 用 speed（px/s）而非 duration；BaseDriver 签名是 duration_ms，
  我们按 distance / duration 反推 speed，并 clamp 到 hmdriver2 的 200-40000 范围
- ``press_home`` / ``press_back`` / ``press_keycode`` 全走 ``KeyCode`` 枚举；int 也
  可以直接穿透，鸿蒙 keycode 不完全等同 Android（Home=1/Back=2 这种差异，详见 proto.py）
- 中文输入：``uitest inputText`` 原生支持 Unicode，不需要像 Android 那样预装 IME
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
from loguru import logger

from ai_phone.config import get_settings

from .base import AlbumSaveResult, BaseDriver, DeviceInfo
from .hdc import (
    HdcError,
    hdc_available,
    hdc_list_targets,
    hdc_shell,
)

# 鸿蒙系统截图触发：模拟"控制中心截图按钮"（trigger_type=control-panel），由系统
# 截图服务 com.huawei.hmos.screenshot（持 CAPTURE_SCREEN + WRITE_IMAGEVIDEO 权限）
# 完成截图并自动存入图库。真机实测 ALN-AL00 / HarmonyOS 6.1.0 可用（方案 §11 Step 5）。
_HM_SCREENSHOT_CMD = (
    "aa start -b com.huawei.hmos.screenshot "
    "-a com.huawei.hmos.screenshot.ServiceExtAbility -m phone "
    "-A com.ohos.systemui.action.TOGGLE --ps trigger_type control-panel"
)
# aa start 是异步的，留点时间让系统截图服务完成截图+入库
_HM_SCREENSHOT_SETTLE_SEC = 2.0

# hmdriver2 是可选 extras，没装不应让本模块 import 失败；真正用到才抛。
try:  # pragma: no cover
    from hmdriver2._client import HmClient  # type: ignore
    from hmdriver2.driver import Driver as HmDriver  # type: ignore
    from hmdriver2.proto import KeyCode as HmKeyCode  # type: ignore
    _HMDRIVER2_AVAILABLE = True
    _HMDRIVER2_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # noqa: BLE001
    HmClient = None  # type: ignore[assignment]
    HmDriver = None  # type: ignore[assignment]
    HmKeyCode = None  # type: ignore[assignment]
    _HMDRIVER2_AVAILABLE = False
    _HMDRIVER2_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


# ----------------------------------------------------------------------
# 关掉 hmdriver2.Driver.__del__ 的全局副作用（2026-04 真机排障血泪）
# ----------------------------------------------------------------------
# 上游实现（hmdriver2 v0.x）长这样：
#
#     def __del__(self):
#         Driver._instance.clear()               # ← 清 **全部** 设备的 singleton
#         if hasattr(self, '_client') and self._client:
#             self._client.release()             # ← 关 sock + hdc fport rm 10000
#
# 两个副作用在多设备 / 自愈重建路径上都会炸：
#   1) ``_instance.clear()``：任意一个 Driver 被 GC 都把 **别的设备** 的 singleton
#      顺手清光，多设备并行用直接乱套；
#   2) ``_client.release()`` → ``rm_forward(local_port, 8012)``：新旧两个 HmClient
#      的 ``local_port`` 都是 hdc 分配的 ``tcp:10000``（同一 serial 的 fport 会
#      复用），旧实例 GC 时调这一下会把 **新实例正在用的 fport 映射** 一起撤掉，
#      新 sock 虽然 connect() 成功，下一次 sendall 就进入死区，recvMsg 返回空串。
#
# 我们在 L2 ``_rebuild_raw`` / L3 ``_respawn_daemon`` 里每次 ``self._raw = HmDriver(...)``
# 都会让旧实例 refcount 归零 → __del__ 触发 → 连环撤新实例的 fport，表现成
# "L2/L3 日志里 Driver.create 成功了，下一条命令立刻 recvMsg 空 / 'NoneType' sendall"。
# 2026-04-22 真机日志 [17:36:53-57] 是一份完整复现。
#
# 处置：模块导入时把 ``Driver.__del__`` 一次性换成 no-op。资源释放完全走我们自己的
# ``HarmonyDriver.close()`` 显式调用（内部 ``_release_hmclient_quietly``）。
# 代价：Python 解释器退出时每个 HmClient 的 socket 靠 GC 自动回收，fport 不再
# 主动 rm——但 hdc server 重启 / 拔 USB 时 fport 自动失效，不会泄漏。
def _neutralize_hmdriver_del_once() -> None:
    """把 ``hmdriver2.Driver.__del__`` 彻底阉掉（幂等，多次调用只生效一次）。"""
    if not _HMDRIVER2_AVAILABLE or HmDriver is None:
        return
    cur = getattr(HmDriver, "__del__", None)
    if cur is not None and getattr(cur, "__aiphone_neutralized__", False):
        return

    def _noop(self: Any) -> None:  # pragma: no cover - 仅被 GC 调
        return

    _noop.__aiphone_neutralized__ = True  # type: ignore[attr-defined]
    try:
        HmDriver.__del__ = _noop  # type: ignore[method-assign]
        logger.debug("hmdriver2.Driver.__del__ 已阉割（去除 _instance.clear + fport 副作用）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hmdriver2.Driver.__del__ monkey-patch 失败：{}；"
            "L2/L3 自愈可能仍会被 __del__ 连环撤 fport", exc,
        )


_neutralize_hmdriver_del_once()


# 息屏策略续约表：serial → 上次成功打 stay-awake 命令的 monotonic 时间戳。
# 真机实测（2026-04-25）：``power-shell timeout -o 86400000`` 设的 override 不
# 能扛住 18 小时长跑——鸿蒙 power 子系统会在某些事件（深度待机、系统服务重启、
# 后台 power policy 重初始化）下把 override 抹掉，回退到设置里的默认 timeout，
# 然后设备就锁屏了（probe 读 PowerManagerService 拿到 INACTIVE/SLEEP）。
#
# 修复：改成"周期续约"——比鸿蒙最长可配置 timeout（15 分钟）短一截，每 10
# 分钟重打一次。复用 rescan_loop 5s 步频，无需新增协程：rescan_loop 每次会 new
# 一个临时 HarmonyDriver 走 __init__，那里检查时间戳够不够旧就好。
_STAY_AWAKE_LAST_AT: dict = {}
_STAY_AWAKE_REFRESH_SEC = 600.0  # 10 min，远小于鸿蒙最长 timeout 15 分钟


# ----------------------------------------------------------------------
# 按 serial 的全局共享状态（并发根因修复）
# ----------------------------------------------------------------------
# hmdriver2.Driver 是「按 serial 的 singleton」，同一台设备的所有 HarmonyDriver
# 实例必须共享**同一个 raw + 同一条 uitest socket + 同一把锁**。只共享锁仍不够：
# 任一 wrapper 完成 L2/L3 后若只替换自己的 ``self._raw``，其它 wrapper 会继续
# 持有已经 release 的旧 raw，再触发一轮无意义自愈。
#
# 此前每个 HarmonyDriver 各自 new 一把 ``threading.RLock()`` 当 _heal_lock：
#   - rescan_loop 每 5s ``new HarmonyDriver`` 探活（自己一把锁）
#   - vlm_loop 用 _driver_cache 里的缓存 driver 点击/滑动（另一把锁）
# 两把锁互不相识 → 两个线程同时 ``sendall`` / ``recv`` 同一条 socket →
# 响应粘包（``Extra data: line 1 column 16``）/ ``Disallow concurrent use`` →
# 被误判成 socket 故障 → 并发 L2/L3 重建 → 新 fport(16557) 与 registry(16556)
# 不一致 → 镜像 fail-closed 永久黑屏。
#
# 根治：按 serial 共享状态，wrapper 的 ``_raw`` 属性只是 state.raw 的视图；任一
# wrapper 重建后，其它 wrapper 立即看到同一个新 raw。RLock 可重入，允许外层
# _call_with_reconnect 与下层 HmClient.invoke 的真实 socket 边界使用同一把锁。
class _SerialDriverState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.raw: Any = None


_SERIAL_STATES_GUARD = threading.Lock()
_SERIAL_STATES: Dict[str, _SerialDriverState] = {}


def _serial_state(serial: str) -> _SerialDriverState:
    """取（或惰性创建）某 serial 的进程内唯一 driver 状态。"""
    with _SERIAL_STATES_GUARD:
        state = _SERIAL_STATES.get(serial)
        if state is None:
            state = _SerialDriverState()
            _SERIAL_STATES[serial] = state
        return state


def _serial_lock(serial: str) -> "threading.RLock":
    """兼容内部调用：返回该 serial 共享状态中的唯一控制锁。"""
    return _serial_state(serial).lock


def _serialize_hmclient_invokes_once() -> None:
    """在 hmdriver2 真正的 send→recv 边界按 serial 串行化。

    ``get_raw_driver()`` 是公开能力，XPath/UiObject 会绕过 HarmonyDriver 的包装
    方法，但最终都落到 HmClient.invoke。锁放在这里才能覆盖所有调用入口。
    """
    if not _HMDRIVER2_AVAILABLE or HmClient is None:
        return
    for method_name in ("invoke", "invoke_captures"):
        original = getattr(HmClient, method_name, None)
        if original is None or getattr(original, "__aiphone_serialized__", False):
            continue

        def _serialized(
            self: Any,
            *args: Any,
            __original: Any = original,
            **kwargs: Any,
        ) -> Any:
            serial = str(getattr(getattr(self, "hdc", None), "serial", "") or "")
            if not serial:
                return __original(self, *args, **kwargs)
            with _serial_lock(serial):
                return __original(self, *args, **kwargs)

        _serialized.__aiphone_serialized__ = True  # type: ignore[attr-defined]
        _serialized.__name__ = getattr(original, "__name__", method_name)
        setattr(HmClient, method_name, _serialized)


_serialize_hmclient_invokes_once()


def _require_hmdriver2() -> None:
    """真正用到 hmdriver2 时才抛；查设备列表这一步不抛（允许只有 hdc 没装 Python 库）。"""
    if not _HMDRIVER2_AVAILABLE:
        raise RuntimeError(
            "HarmonyOS 支持需要 hmdriver2：pip install -e \"backend[harmony]\" "
            f"（import 失败原因：{_HMDRIVER2_IMPORT_ERROR}）"
        )


# hmdriver2 的 KeyCode 枚举是从 ohos.multimodalInput 映射过来的；
# BaseDriver 只关心 HOME / BACK 两个高频按键，其它场景按 int 直接穿透。
# 按需补充：BACKSPACE=2055, ENTER=2054, POWER=18, VOLUME_UP=16, VOLUME_DOWN=17
# （具体值以 hmdriver2.proto.KeyCode 为准；这里不硬编码避免版本漂移）


class HarmonyDriver(BaseDriver):
    """鸿蒙驱动实现。和 Android / iOS 同级。"""

    platform = "harmony"

    @property
    def _raw(self) -> Any:
        """同 serial 所有 wrapper 共享的唯一 hmdriver2.Driver。"""
        return self._state.raw

    @_raw.setter
    def _raw(self, value: Any) -> None:
        self._state.raw = value

    def __init__(self, serial: str, *, setup_power: bool = True):
        _require_hmdriver2()
        self.serial = serial
        # 同 serial 的 wrapper 共享完整状态，而不只共享锁。L2/L3 替换 state.raw
        # 后，rescan / VLM / VM manager 持有的所有 wrapper 会立即看到同一新对象。
        self._state = _serial_state(serial)
        self._heal_lock = self._state.lock

        # 构造本身也走 uitest socket（HmClient.start → _connect_sock + Driver.create
        # invoke），必须与其它线程的 invoke / 自愈互斥，否则 rescan 的临时实例
        # 与运行中实例并发构造会撞 _UITestService.init()（杀 daemon + 推 agent.so）。
        with self._heal_lock:
            # hmdriver2.Driver 是 singleton（按 serial 缓存），首次创建会：
            # 1) 启 HmClient（hdc fport 到设备 uitest socket）
            # 2) 自动在设备上下发 hypium-agent.hap（第一次会稍慢，秒级）
            # 3) invoke "Driver#0" 句柄
            # 失败通常意味着：uitest 服务没起来 / 设备未授权 / hdc 不通
            try:
                if self._raw is None:
                    self._raw = HmDriver(serial)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"hmdriver2.Driver({serial}) 初始化失败：{exc}。"
                    "排查顺序：1) hdc list targets 看到设备且 Connected；"
                    "2) 设备已开发者模式 + USB 调试；"
                    "3) 首次连接 PC 时手机是否信任过；"
                    "4) 试 hdc shell \"aa start -a com.ohos.uitest.ServiceAbility -b com.ohos.uitest\""
                ) from exc

            # 禁自动息屏：HarmonyOS 设置里最长 15 分钟，排队期/长任务里会自己锁屏
            # → uitest daemon 一并挂，再恢复要过 L2/L3 自愈。这里用 hdc 命令提前把
            # 超时关掉，彻底规避问题。
            # 续约策略：rescan_loop 每 5s 都会走这条构造器，距离上次成功打超过
            # _STAY_AWAKE_REFRESH_SEC 才会真正再打一次 hdc shell；中间所有 rescan
            # 都直接跳过，零开销。设备拔插 / agent 重启会因为 dict 里没有时间戳而
            # 立即重打第一次。
            if setup_power:
                last = _STAY_AWAKE_LAST_AT.get(self.serial, 0.0)
                now = time.monotonic()
                is_first = last == 0.0
                if is_first or (now - last) >= _STAY_AWAKE_REFRESH_SEC:
                    # 不管 _setup_stay_awake 内部成败都更新时间戳——失败大概率是 hdc
                    # 不通 / 设备短暂掉线，5s 后立刻重试也是徒劳，等下个续约窗口再试
                    self._setup_stay_awake(first=is_first)
                    _STAY_AWAKE_LAST_AT[self.serial] = now

    # ------------------------------------------------------------------
    # 息屏策略
    # ------------------------------------------------------------------
    def prepare_for_run(self, *, wake_policy: Optional[Dict[str, bool]] = None) -> None:
        """Run 前用纯 hdc 唤醒 HarmonyOS 并进入可操作态。"""
        prepare_harmony_for_run(
            self.serial,
            swipe=bool((wake_policy or {}).get("wake_swipe")),
        )

    def sleep_after_run(self) -> None:
        """让 HarmonyOS 熄屏，不覆盖设备自身的自动熄屏设置。"""
        try:
            out = hdc_shell(
                self.serial,
                "power-shell suspend",
                timeout=5.0,
                check=False,
            )
            if out:
                logger.debug("设备 {} HarmonyOS suspend 输出：{}", self.serial, out[:160])
            logger.info("设备 {} Run 后熄屏：power-shell suspend", self.serial)
        except Exception as exc:  # noqa: BLE001
            logger.warning("设备 {} Run 后熄屏失败：{}", self.serial, exc)

    def _setup_stay_awake(self, *, first: bool) -> None:
        """把 HarmonyOS 的自动息屏关掉。

        用 ``power-shell timeout -o <ms>``——HarmonyOS 自带的 power 管理工具，
        设置屏幕自动熄灭超时。取 24 小时（86400000ms）避免超过 ROM 内部的 int
        上限，但实际生命期由调用方按 ``_STAY_AWAKE_REFRESH_SEC`` 续约——真机
        实测 18 小时后 override 会被系统抹掉，单次设置不可靠。

        实测成功输出格式（2026-04 真机）：

        - ``Set Display Off Timeout Success`` —— 官方文档式（含 "success"）
        - ``Override screen off time to 86400000`` —— 部分机型（不含 "success"，但确实生效）

        **坑**：历史版本用 ``settings put SYSTEM settings.display.screen_off_time``
        做过二级兜底。真机验证发现这条命令会在设备侧触发一次系统服务重启，
        连带 hdc daemon + uitest daemon 一起抖 10+ 秒，我们自己的 L1/L2/L3
        自愈会被拖一整轮。既然 ``power-shell timeout`` 已经能覆盖当前所有机型，
        这条有害的兜底已去除。

        日志策略：``first=True`` 时打 INFO（让用户能看到生效），后续续约打 DEBUG
        避免每 10 分钟一条 INFO 刷屏；任何异常仍按 WARN 抛出。
        """
        _SCREEN_OFF_MAX_MS = 86400000  # 24h

        try:
            out = (hdc_shell(
                self.serial,
                f"power-shell timeout -o {_SCREEN_OFF_MAX_MS}",
                timeout=5.0,
                check=False,
            ) or "").strip()
            lower = out.lower()
            ok_keyword = "success" in lower or "override screen off time" in lower
            if ok_keyword or not out:
                # 有匹配关键字、或空输出（部分机型成功也是空 stdout），都按成功记
                if first:
                    logger.info(
                        "设备 {} 已禁自动息屏（power-shell timeout=24h，每 {:.0f} 分钟续约一次）",
                        self.serial, _STAY_AWAKE_REFRESH_SEC / 60,
                    )
                else:
                    logger.debug("设备 {} stay-awake 续约 ✓", self.serial)
                return
            # 有输出但不识别为成功 —— ROM 不支持，记录原文方便排查
            logger.warning(
                "设备 {} power-shell timeout 输出未识别为成功：{}。"
                "可能 ROM 不支持，设备仍可能按系统设置自动息屏",
                self.serial, out[:160],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "设备 {} power-shell timeout 调用失败：{}；该 ROM 可能不支持，"
                "设备仍可能按系统设置自动息屏", self.serial, exc,
            )

    # ------------------------------------------------------------------
    # socket 自愈（三级）
    # ------------------------------------------------------------------
    # hmdriver2.HmClient 是单 socket 长连，设备息屏 / idle / uitest 抽风 / 旋转事件
    # 都可能让这条 socket 出现 BrokenPipe / ConnectionReset / ConnectionRefused。
    # 不同级别的故障需要不同级别的恢复动作：
    #
    #   L1  socket 级   —— 只挂了这条 TCP，daemon 还活：_reconnect_hmclient()
    #                        重连 ``127.0.0.1:<fport>`` 即可
    #   L2  客户端级    —— Driver#0 句柄也失效了（常见于 uitest 重启但 daemon 健在）：
    #                        _rebuild_raw() 清 singleton + 新 HmDriver
    #   L3  daemon 级   —— **设备侧 uitest daemon 僵尸化**（socket 连不上、port
    #                        Connection refused、client 重建后立刻又 Broken pipe）：
    #                        _respawn_daemon() 先 hdc shell 把 uitest 进程 pkill 掉，
    #                        清 fport，再 new HmDriver 让 hmdriver2 内部
    #                        ``_UITestService.init()`` 重推 agent.so + start-daemon
    #
    # 历史 bug：L2 曾尝试 ``del HmDriver._instances[serial]``（多写了 s），
    # hmdriver2 实际是单数 ``_instance``，所以清缓存始终失败，"整重建"拿回的是
    # 同一个死 singleton。2026-04 真机排障时发现，已经修为正确的 ``_instance``。
    _SOCKET_DEAD_ERRORS = (
        BrokenPipeError,
        ConnectionResetError,
        ConnectionAbortedError,
        ConnectionRefusedError,  # daemon 死透后 connect() 直接 refused
        OSError,                 # 兜底：Errno 9 (Bad fd) / Errno 32 / Errno 54
        # hmdriver2 的 _recv_msg 在 socket 半关（对端 FIN、但本端还能 send）时
        # 返回空 bytearray，上层 ``json.loads("")`` 直接抛 JSONDecodeError。
        # 这等价于 socket 失效，按 L1 级处理让重连救场。
        json.JSONDecodeError,
        # hmdriver2 ``HmClient.release()`` 会 ``self.sock.close(); self.sock = None``
        # （_client.py:152-154）。也就是 None.sendall 不是异常，而是库内部用来
        # 表达"socket 已释放，下次 invoke 请重连"的状态机信号——语义完全等价
        # ConnectionResetError。折叠屏展开 / daemon 瞬断时 release() 会被触发，
        # 随后另一路径（如 VLM click 拿 display_size）的 sendall 必炸 AttributeError，
        # 必须纳入自愈触发让 L1 重连救场，不能裸抛给业务层。_heal_lock 只串行化
        # "重建"这一段，解决不了"前一次 release 留下的 sock=None 被别人先看到"的
        # 时序窗口——这个窗口就是靠这里兜底。
        # 误判风险评估：fn() 目前只转发 hmdriver2 的 API，调用链里除了 sock 相关
        # 没有其他 "None.method" 的合理来源；即使未来 hmdriver2 API 变更导致
        # 其他 AttributeError，至多多跑一轮 L1~L3，不会无限循环（L3 后直接抛）。
        AttributeError,
    )

    # ----- 内部辅助 ---------------------------------------------------

    @staticmethod
    def _invalidate_hmdriver_singleton(serial: str) -> None:
        """清 ``hmdriver2.Driver._instance[serial]``，让下次 ``HmDriver(serial)``
        真正新建实例。单数字段，多写了 s 就静默失效——历史 bug，勿改回去。
        """
        try:
            cache = getattr(HmDriver, "_instance", None)
            if isinstance(cache, dict):
                cache.pop(serial, None)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_raw_from_singleton(self) -> None:
        """让临时 wrapper 跟随同 serial 的最新 hmdriver2 单例。

        ``HarmonyDriver`` 本身不是单例：VM manager、driver cache、rescan_loop
        可以各持一个 wrapper；它们虽然共享 serial 锁，``self._raw`` 却仍是各自
        的普通引用。某个 wrapper 完成 L2/L3 后会替换 hmdriver2 单例，其他
        wrapper 若继续使用旧 raw，就会在下一次调用看到旧 client 的 ``sock=None``
        并多触发一轮 L1。

        因此每次在 serial 锁内调用前，都从上游单例表同步一次最新 raw。只接纳
        已完成初始化的实例，避免构造失败留下的半初始化对象污染正常 wrapper。
        """
        try:
            cache = getattr(HmDriver, "_instance", None)
            current = cache.get(self.serial) if isinstance(cache, dict) else None
            if (
                current is not None
                and current is not getattr(self, "_raw", None)
                and bool(getattr(current, "_initialized", False))
            ):
                self._raw = current
                logger.debug(
                    "harmony serial={} wrapper 已同步到最新 Driver 单例", self.serial
                )
        except Exception:  # noqa: BLE001
            pass

    def _release_hmclient_quietly(self) -> None:
        """优雅关 HmClient：关 socket + rm fport；失败都吞掉，L3 路径不能因为
        "清理失败"阻塞恢复动作。
        """
        try:
            raw = getattr(self, "_raw", None)
            client = getattr(raw, "_client", None) if raw is not None else None
            if client is None:
                return
            try:
                client.release()  # hmdriver2 内部：关 socket + hdc fport rm
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    # ----- fport 同步：自愈换端口后回写 registry（否则镜像 fail-closed）------

    def _current_local_port(self) -> Optional[int]:
        """当前 HmClient 实际使用的本地 fport；拿不到返回 None。"""
        try:
            raw = getattr(self, "_raw", None)
            client = getattr(raw, "_client", None) if raw is not None else None
            if client is None:
                return None
            port = int(client.local_port)
            return port if 1 <= port <= 65535 else None
        except Exception:  # noqa: BLE001
            return None

    def current_fport(self) -> Optional[int]:
        """对外（镜像重连用）：取当前控制通道 fport，持锁读，避免读到重建中间态。"""
        with self._heal_lock:
            self._refresh_raw_from_singleton()
            return self._current_local_port()

    def reconcile_managed_fport(self) -> int:
        """锁内对齐当前 raw 与 managed registry，并返回唯一可信端口。

        registry 以同一个 HDC serial 为键；当前 raw 也由该 serial 构造，因此这里
        可以安全回写同键端口。无法读取、写回或二次校验失败时直接报错，镜像继续
        fail-closed，绝不扫描其它设备的 fport。
        """
        with self._heal_lock:
            self._refresh_raw_from_singleton()
            port = self._current_local_port()
            if port is None:
                raise RuntimeError(
                    f"harmony_driver_fport_unavailable:serial={self.serial}"
                )

            from ai_phone.agent.harmony_vm.registry import (  # noqa: PLC0415
                managed_fport,
                set_managed_fport,
            )

            is_managed, old = managed_fport(self.serial)
            if is_managed and old != port:
                set_managed_fport(self.serial, port)
                logger.info(
                    "harmony serial={} fport 锁内对账：{} → {}",
                    self.serial, old, port,
                )
            if is_managed and managed_fport(self.serial) != (True, port):
                raise RuntimeError(
                    "managed_harmony_vm_fport_reconcile_failed:"
                    f"serial={self.serial}:actual={port}"
                )
            return port

    def _sync_managed_fport(self) -> None:
        """L2/L3 重建后 fport 可能变（旧映射未被 __del__ 主动回收，新实例拿下一个
        号），把新端口回写受管 VM registry，否则 registry 仍记旧端口，镜像工厂
        ``expected != actual`` 永久 fail-closed（黑屏）。

        registry 内部只对「已登记的受管 serial」生效（``set_managed_fport`` 自带
        ``if serial in _MANAGED_FPORTS`` 守卫），非受管设备（真机）写入自动忽略。
        惰性导入避免 drivers → harmony_vm 的潜在环。
        """
        try:
            self.reconcile_managed_fport()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony serial={} 自愈后 fport 回写失败（镜像可能需下次重建才恢复）：{}",
                self.serial, exc,
            )

    # ----- L1：socket 级重连 -----------------------------------------

    def _reconnect_hmclient(self) -> None:
        """强制重连 hmdriver2 的 HmClient socket，不重建 Driver 实例。"""
        try:
            client = self._raw._client  # noqa: SLF001
            if client is None:
                return
            try:
                if client.sock is not None:
                    client.sock.close()
            except Exception:  # noqa: BLE001
                pass
            client.sock = None
            client._connect_sock()  # noqa: SLF001
            logger.info("harmony serial={} L1 HmClient socket 已重连", self.serial)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony serial={} L1 socket 重连失败：{}（下一步 L2 重建 Driver）",
                self.serial, exc,
            )
            raise

    # ----- L2：客户端级重建 ------------------------------------------

    def _rebuild_raw(self) -> None:
        """清 singleton + 重新 new HmDriver。daemon 仍健在的话这一步就够。"""
        self._release_hmclient_quietly()
        self._invalidate_hmdriver_singleton(self.serial)
        self._raw = HmDriver(self.serial)  # type: ignore[misc]
        self._sync_managed_fport()
        logger.info("harmony serial={} L2 Driver 已重建", self.serial)

    # ----- L3：设备端 daemon 级重拉 ----------------------------------

    def _respawn_daemon(self) -> None:
        """设备侧 uitest daemon 僵尸 / 不响应时的最后兜底：

        1. ``hdc shell pkill -9 -f "uitest start-daemon"`` —— 杀设备端 uitest 进程
           （hmdriver2 起名固定为 ``uitest start-daemon singleness``；多起几个只会
           相互抢占 port 8012，一起杀光最干净）
        2. 关 HmClient socket + ``hdc fport rm`` 清本地转发
        3. 清 hmdriver2.Driver 单例缓存
        4. 给 daemon 一点退出时间，避免 new HmDriver 抢在 port 还占着时 connect
        5. ``HmDriver(serial)`` 新建实例时，hmdriver2 内部 ``_UITestService.init()``
           会自动：再次 pkill 保险 → 推 agent.so → ``uitest start-daemon singleness``
           → rebuild fport → 重连 socket

        执行完成不保证一定能用——如果真机进了"hdc shell 能执行但 uitest 起不来"
        的坏状态，只能物理重启手机。但 L3 能 cover 我们 90% 的"daemon 僵尸"场景。
        """
        logger.warning("harmony serial={} L3 启动设备侧 daemon 重拉（僵尸恢复）", self.serial)
        try:
            hdc_shell(
                self.serial,
                'pkill -9 -f "uitest start-daemon"',
                timeout=5.0,
                check=False,
            )
            logger.info("harmony serial={} L3.1 设备侧 uitest daemon 已 pkill", self.serial)
        except HdcError as exc:
            # pkill 失败不是硬错误：进程不存在时 exit!=0 也会被归到 HdcError；
            # 不存在本身就是我们想要的状态
            logger.warning(
                "harmony serial={} L3.1 pkill 非零退出（可能本来就没跑）：{}",
                self.serial, exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony serial={} L3.1 pkill 异常（继续走）：{}", self.serial, exc,
            )

        self._release_hmclient_quietly()
        self._invalidate_hmdriver_singleton(self.serial)

        # 给 daemon 一点彻底退出时间；这里等 1s 经验值（pkill -9 秒级生效，
        # port 释放靠内核 TIME_WAIT 跳过——daemon 的 socket 是 listen 端，
        # 进程死了 listen fd 即释放，一般不需要等完整 2MSL）
        time.sleep(1.0)

        # 重建实例：hmdriver2 构造函数 → HmClient.start() → _UITestService.init()
        # 会把 daemon 整个重拉起来，包括 agent.so 重推（md5 一致时跳过）
        self._raw = HmDriver(self.serial)  # type: ignore[misc]
        self._sync_managed_fport()
        logger.warning(
            "harmony serial={} L3 daemon 重拉 + Driver 重建完成（如仍不通请物理重启手机）",
            self.serial,
        )

    # ----- 调度：三级自愈 --------------------------------------------

    def _call_with_reconnect(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """跑 ``fn(*args, **kwargs)``；按 L1 → L2 → L3 顺序升级自愈。

        **并发串行化（根因修复）**：整个 ``send→recv`` 都在 ``_heal_lock``
        （按 serial 全局共享）之下。这是本次修复的核心——此前 L0「首次尝试」
        在锁外走乐观路径，rescan_loop 的临时 driver 与 vlm_loop 的缓存 driver
        会同时 ``sendall`` / ``recv`` 同一条 uitest socket，导致响应粘包被误判成
        socket 故障、进而并发自愈重建、端口漂移。现在锁覆盖 L0，就从源头杜绝了
        并发使用 socket；uitest 本就「同时只服务一个客户端」，这里只是把代码
        行为和设备真实约束对齐（控制通道串行；MJPEG 视频是独立 socket，不受此锁影响）。

        调用方需传 lambda / 顶层函数（内部引用 ``self._raw``），这样 L2/L3 重建后
        lambda 里的 ``self._raw`` 能自动绑到新对象。典型用法：

        .. code-block:: python

            return self._call_with_reconnect(lambda: self._raw.click(x, y))

        失败路径分叉（全程持锁）：
        - L0 首次 fn 成功 → 返回（绝大多数路径）
        - L0 抛 socket 异常 → L1 重连 socket 并重试
        - L1 失败 → L2 重建 Driver 并重试
        - L2 失败 → L3 杀 daemon + 重建 并重试
        - L3 仍失败 → 把最后这次异常原样抛给调用方，supervisor 据此下线设备

        **L0 非 socket 异常的轻量重试**（hypium daemon 抖动兜底）：
        L0 抛非 socket 异常时（如 ``display_rotation get failed`` /
        ``display_size get failed``，常见于 hypium daemon 在 hdc shell 多端
        并发下的瞬态卡顿），等 50ms 重试一次再决定是否抛回——这类异常 90%
        是一过性的，但又不属于 socket 级故障（不该走 L1/L2/L3 重连重建，那
        会拖几秒），轻量重试是性价比最高的修法。仍失败才把原异常抛给调用
        方，避免吞掉真正的业务错误。
        """
        # 全程持锁：L0 起就串行化，从源头杜绝并发使用同一条 uitest socket
        with self._heal_lock:
            # 别的 wrapper 可能刚完成 L2/L3；先跟随最新 singleton，避免继续调用
            # 已 release（sock=None）的旧 raw 再触发一轮冗余自愈。
            self._refresh_raw_from_singleton()
            # L0：首次尝试
            try:
                return fn(*args, **kwargs)
            except self._SOCKET_DEAD_ERRORS as exc_l0:
                first_exc: BaseException = exc_l0
            except Exception as exc_l0_business:  # noqa: BLE001
                # hypium daemon 偶发的非 socket 异常（如 display_rotation/size get
                # failed）：50ms 退避重试 1 次，仍异常照原样抛出（保持业务语义）
                logger.debug(
                    "harmony serial={} L0 非 socket 异常 ({}: {})，50ms 后重试 1 次",
                    self.serial, exc_l0_business.__class__.__name__, exc_l0_business,
                )
                time.sleep(0.05)
                return fn(*args, **kwargs)

            logger.warning(
                "harmony serial={} L0 socket 异常 ({})，L1 重连后重试",
                self.serial, first_exc,
            )

            # L1：重连 socket 后重试
            try:
                self._reconnect_hmclient()
                return fn(*args, **kwargs)
            except self._SOCKET_DEAD_ERRORS as exc_l1:
                logger.warning(
                    "harmony serial={} L1 socket 重连后仍异常 ({})，升 L2 重建 Driver",
                    self.serial, exc_l1,
                )
            except Exception as exc_l1_other:  # noqa: BLE001
                logger.warning(
                    "harmony serial={} L1 重连路径抛非 socket 异常 ({})，升级 L2",
                    self.serial, exc_l1_other,
                )

            # L2：重建 Driver 后重试
            try:
                self._rebuild_raw()
                return fn(*args, **kwargs)
            except self._SOCKET_DEAD_ERRORS as exc_l3:
                logger.warning(
                    "harmony serial={} L2 Driver 重建后仍异常 ({})，升 L3 杀 daemon",
                    self.serial, exc_l3,
                )
            except Exception as exc_l2_other:  # noqa: BLE001
                logger.warning(
                    "harmony serial={} L2 重建路径抛非 socket 异常 ({})，升级 L3",
                    self.serial, exc_l2_other,
                )

            # L3：设备侧 daemon 重拉 + Driver 重建
            self._respawn_daemon()
            return fn(*args, **kwargs)

    # ------------------------------------------------------------------
    # 对外暴露的原生 driver（高级脚本用控件树 / XPath 的入口）
    # ------------------------------------------------------------------
    def get_raw_driver(self) -> Any:
        """返回原生 ``hmdriver2.Driver``。

        测试团队可以这么用（不走 VLM 视觉）：

        .. code-block:: python

            from ai_phone.agent.drivers import open_driver
            d = open_driver(serial, "harmony")
            raw = d.get_raw_driver()
            raw.xpath('//*[@text="登录"]').click()
            raw(text="密码").input_text("123456")

        ai-phone 不包装任何 hmdriver2 功能，保留生态完整性。
        """
        with self._heal_lock:
            self._refresh_raw_from_singleton()
            return self._raw

    # ------------------------------------------------------------------
    # 屏幕信息
    # ------------------------------------------------------------------
    def _invalidate_display_cache(self) -> None:
        """清掉 hmdriver2.Driver 的 ``display_size`` / ``display_rotation``
        ``@cached_property``——**只清跟物理屏幕形态强相关的**两个字段，
        **不碰** ``device_info``。

        **为什么只清这两个**：VLM runner 每 step 都调 ``window_size()`` 把归一
        化坐标（0-1000 域）换算成设备像素，横竖屏切换 / 折叠屏展开都会改尺寸，
        cache 陈旧就会点击错位。hypium 一次 invoke ~10ms，每 step 调一次 pop
        + 重查代价可忽略。

        **为什么不碰 device_info**：``device_info`` 对应的是 6 个 ``hdc shell
        param get`` + ``ifconfig``（model / brand / osVersion / apiLevel
        / abi / ipAddr），这些字段跟屏幕形态无关，机型决定后永不变化。rescan
        _loop 每 5s 会调用 ``device_info`` 探活，如果把它一起 pop，每 5s 就
        多出一整轮 hdc shell 打扰，设备侧 uitest daemon 刚起的瞬态更容易被戳到
        返回空、进而走 hdc-only 降级（表现为前端卡片 model/brand 显示 "-"
        且不再刷新）。保留 ``device_info`` 的 cached_property 可以让 rescan 只
        在首次成功后轻量命中缓存。
        """
        d = getattr(self._raw, "__dict__", None)
        if not isinstance(d, dict):
            return
        # 有意不包含 "device_info"——见上方 docstring
        for attr in ("display_size", "display_rotation"):
            d.pop(attr, None)

    def window_size(self) -> Tuple[int, int]:
        # VLM 每 step 都会按这里返回的 (w, h) 把归一化坐标换算成像素，
        # 横竖屏 / 折叠屏展开瞬间必须返回实时值，cache 旧了就点击错位。
        # 只清 display_size / display_rotation，device_info 留着给 rescan 命中。
        def _do() -> Tuple[int, int]:
            self._invalidate_display_cache()
            w, h = self._raw.display_size
            return int(w), int(h)
        return self._call_with_reconnect(_do)

    def rotation(self) -> int:
        """DisplayRotation 的 value 约定：0/1/2/3 对应 0°/90°/180°/270°。"""
        def _do() -> int:
            self._invalidate_display_cache()
            rot = self._raw.display_rotation
            try:
                return int(getattr(rot, "value", rot))
            except Exception:  # noqa: BLE001
                return 0
        return self._call_with_reconnect(_do)

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    def _raw_screenshot_bytes(self) -> bytes:
        """落盘到临时文件再读回。hmdriver2.screenshot 默认方法是 snapshot_display，
        内部就是 ``hdc shell snapshot_display -f /data/local/tmp/*.jpeg + hdc file recv``，
        约 200-400ms / 张。文件扩展名决定编码，这里统一 JPEG 省一次转码。

        注意：snapshot_display 路径全走 ``hdc shell``，**不经过 hmdriver2 socket**，
        所以本方法不需要 ``_call_with_reconnect`` 包；但前置的 driver 状态校验
        （hmdriver2 内部可能走 invoke）会走 socket，所以稳妥起见还是包一层。
        """
        def _do() -> bytes:
            fd, path = tempfile.mkstemp(prefix="hm-shot-", suffix=".jpeg")
            os.close(fd)
            try:
                self._raw.screenshot(path)
                with open(path, "rb") as f:
                    return f.read()
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return self._call_with_reconnect(_do)

    def screenshot_png(self) -> bytes:
        # VLM / 上传链路少数地方坚持要 PNG；鸿蒙 snapshot_display 直出 JPEG，
        # 这里转一次。PNG 在 VLM 侧会再被压回 JPEG，有点浪费但接口契约是这样。
        jpeg = self._raw_screenshot_bytes()
        try:
            img = Image.open(io.BytesIO(jpeg))
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning("harmony screenshot_png 转码失败，返原始 JPEG 字节: {}", exc)
            return jpeg

    def screenshot_jpeg(self, quality: int = 25, max_side: Optional[int] = None) -> bytes:
        # 鸿蒙直出的 JPEG 质量是设备端默认（较高，~80），对 VLM 来说超标。
        # 重新用 Pillow 压到指定 quality 降低带宽。
        raw = self._raw_screenshot_bytes()
        try:
            img = Image.open(io.BytesIO(raw))
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max_side and max(img.size) > max_side:
                ratio = max_side / float(max(img.size))
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning("harmony screenshot_jpeg 重压失败，返原始 JPEG: {}", exc)
            return raw

    def save_screenshot_to_album(self) -> AlbumSaveResult:
        """HarmonyOS：aa start 触发系统截图服务（等同控制中心截图按钮），系统自动存入图库。

        为什么走这条（已真机逐一实测，见方案 §11 Step 5）：
        - ``snapshot_display`` 是开发者截图落文件，不进图库；``mediatool`` 只有
          recv/query/delete（**不能**写入图库）；HarmonyOS NEXT 沙箱下 shell 也
          访问不到媒体目录。丢文件进不了图库（同 iOS）。
        - 系统截图服务 ``com.huawei.hmos.screenshot`` 持 ``CAPTURE_SCREEN`` +
          ``WRITE_IMAGEVIDEO`` 权限；用其 ``ServiceExtAbility`` + TOGGLE 动作、
          ``trigger_type=control-panel`` 触发（等价点控制中心的截图按钮），由系统
          完成截图并自动入库。与 iOS 借 Siri、Android 用 screencap 同属"借系统能力"。

        成功判据与局限：以 ``aa start`` 返回 "start ability successfully" 为成功；
        能否真正落盘依赖系统截图服务（HarmonyOS 侧无法回读图库做二次确认）。
        """
        try:
            out = (
                hdc_shell(
                    self.serial, _HM_SCREENSHOT_CMD, timeout=10.0, check=False
                )
                or ""
            ).strip()
        except Exception as exc:  # noqa: BLE001
            return AlbumSaveResult(
                ok=False, platform=self.platform, supported=True,
                method="aa_start_screenshot",
                error=f"触发系统截图失败：{exc}",
            )
        if "successfully" not in out.lower():
            return AlbumSaveResult(
                ok=False, platform=self.platform, supported=True,
                method="aa_start_screenshot",
                error=f"系统截图服务未成功启动：{out or '(空输出)'}",
            )
        # aa start 异步，留时间让系统完成截图+入库
        time.sleep(_HM_SCREENSHOT_SETTLE_SEC)
        return AlbumSaveResult(
            ok=True, platform=self.platform,
            method="aa_start(control-panel)",
        )

    # ------------------------------------------------------------------
    # 触控（全部经 _call_with_reconnect，BrokenPipe 时自动重连重试）
    # ------------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        self._call_with_reconnect(lambda: self._raw.click(int(x), int(y)))

    def double_click(self, x: int, y: int, interval_ms: int = 100) -> None:
        # 覆盖 BaseDriver 默认（两次 click + sleep）：hmdriver2 有原生 doubleClick
        # 更接近真人手势；interval_ms 忽略（由设备系统决定 tap 双击判定间隔）
        self._call_with_reconnect(lambda: self._raw.double_click(int(x), int(y)))

    def long_press(self, x: int, y: int, duration_ms: int = 1000) -> None:
        # hmdriver2.long_click 时长固定（约 1000ms），短按正合适；
        # 需要更长的用 swipe 原地滞留模拟（duration_ms / 1000 = 秒级停留）
        if duration_ms <= 1200:
            self._call_with_reconnect(lambda: self._raw.long_click(int(x), int(y)))
            return
        # 用 swipe 同起止点 + 反推 speed 做长按；speed 打到最低（200 px/s）
        # 一个身位都不动又要走完 distance=0，hmdriver2 会 clamp 到最小 speed，
        # 实测效果等价于"按住不放 duration 秒"。
        self._call_with_reconnect(
            lambda: self._raw.swipe(int(x), int(y), int(x), int(y), speed=200)
        )

    def swipe(
        self, sx: int, sy: int, ex: int, ey: int, duration_ms: int = 500
    ) -> None:
        # hmdriver2 接受 speed（px/s）而非 duration。距离 / 秒 = 像素/秒
        dx = ex - sx
        dy = ey - sy
        distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
        seconds = max(0.05, duration_ms / 1000.0)
        speed = int(distance / seconds)
        # hmdriver2 内部会 clamp 到 [200, 40000]，超出打 warning；这里先夹一次安静
        speed = max(200, min(40000, speed))
        self._call_with_reconnect(
            lambda: self._raw.swipe(int(sx), int(sy), int(ex), int(ey), speed=speed)
        )

    # ------------------------------------------------------------------
    # 输入 & 按键
    # ------------------------------------------------------------------
    def type_text(self, text: str) -> None:
        if not text:
            return
        # hmdriver2.input_text 走 "Driver.inputText" api → 设备端 uitest inputText
        # 原生支持 Unicode，不需要像 Android 那样预装 ADBKeyBoard
        try:
            self._call_with_reconnect(lambda: self._raw.input_text(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony input_text 失败 serial={} text={!r}: {}；"
                "请确认输入框已聚焦（hmdriver2 要求先 focus）",
                self.serial, text, exc,
            )

    def press_home(self) -> None:
        self._call_with_reconnect(lambda: self._raw.go_home())

    def press_back(self) -> None:
        self._call_with_reconnect(lambda: self._raw.go_back())

    def press_keycode(self, code: int) -> None:
        """穿透 int 到 hmdriver2.press_key。鸿蒙 keycode 表和 Android **不一样**，
        上层 action 层要做平台分叉映射，不要把 Android keycode 直接丢进来。
        """
        self._call_with_reconnect(lambda: self._raw.press_key(int(code)))

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------
    def list_third_party_packages(self) -> List[str]:
        return self._list_apps(include_system=False)

    def list_all_packages(self) -> List[str]:
        # include_system_apps=True 会把系统自带的"设置/图库/浏览器/应用市场"等也
        # 返回，给 open_app 二次 VLM 包名匹配更宽的候选，避免命中系统应用时被过滤
        return self._list_apps(include_system=True)

    def _list_apps(self, *, include_system: bool) -> List[str]:
        try:
            return list(self._raw.list_apps(include_system_apps=include_system))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony list_apps 失败 serial={} include_system={}: {}",
                self.serial,
                include_system,
                exc,
            )
            return []

    def activate_app(self, package_name: str) -> None:
        # hmdriver2.start_app 会自动推断 main ability；传空 page_name 它会先 get_app_main_ability
        try:
            self._raw.start_app(package_name)
        except Exception as exc:  # noqa: BLE001
            # 兜底：直接走 aa start，最简路径
            logger.debug(
                "harmony start_app({}) 失败 {}；尝试 aa start 兜底",
                package_name, exc,
            )
            try:
                hdc_shell(
                    self.serial, f"aa start -b {package_name}", timeout=10.0,
                )
            except HdcError as exc2:
                raise RuntimeError(
                    f"无法启动鸿蒙应用 {package_name}：hmdriver2 和 aa start 都失败 ({exc2})"
                ) from exc2

    def terminate_app(self, package_name: str) -> None:
        try:
            self._raw.stop_app(package_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("harmony stop_app({}) 失败 {}，尝试 aa force-stop 兜底", package_name, exc)
            try:
                hdc_shell(self.serial, f"aa force-stop {package_name}", timeout=5.0)
            except HdcError:
                pass

    def current_app(self) -> str:
        # hmdriver2.current_app 返 (pkg, page)；我们只拿 pkg
        try:
            pkg, _page = self._raw.current_app()
            return pkg or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------
    def device_info(self) -> DeviceInfo:
        """融合 hmdriver2.device_info 和 hdc 拿到的信息。

        hmdriver2.device_info 是 @cached_property，首次访问触发一批 hdc shell：
        param get / ifconfig / ohos.buildinfo 等。之后不再更新。
        """
        width, height = self.window_size()
        brand = ""
        model = ""
        os_version = ""
        try:
            info = self._raw.device_info
            model = str(getattr(info, "model", "") or "")
            os_version = str(getattr(info, "sysVersion", "") or "")
            # hmdriver2.DeviceInfo 没有单独 brand 字段（productName 含厂商）；
            # 从 productName 切一下，或再查 param。
            product_name = str(getattr(info, "productName", "") or "")
            if product_name and not brand:
                brand = product_name
        except Exception as exc:  # noqa: BLE001
            logger.debug("harmony device_info 查询失败 serial={}: {}", self.serial, exc)

        # brand 兜底：hdc shell param get const.product.brand
        # 注意 hdc 错误码（[Fail][E001005] Device not found...）会被原样写到 stdout，
        # 必须用 _is_hdc_error_text 过滤，否则错误文本会被显示成"设备品牌"
        if not brand:
            try:
                raw = hdc_shell(
                    self.serial, "param get const.product.brand", timeout=3.0, check=False,
                ).replace("\r", "").strip()
                brand = "" if _is_hdc_error_text(raw) else raw
            except HdcError:
                brand = ""

        # 三个字段任何一个被 hdc 错误污染都置空（防御性双保险）
        if _is_hdc_error_text(brand):
            brand = ""
        if _is_hdc_error_text(model):
            model = ""
        if _is_hdc_error_text(os_version):
            os_version = ""

        return DeviceInfo(
            serial=self.serial,
            platform=self.platform,
            brand=brand,
            model=model,
            os_version=os_version,
            screen_width=width,
            screen_height=height,
            status="online",
        )

    def close(self) -> None:
        """供 ``_invalidate_dead_*_driver`` 风格的自愈逻辑调用。

        hmdriver2.Driver.__del__ 会自己 release HmClient；我们这里主动 release
        一次避免 TCP 挂很久，并清 singleton，下次 ``open_harmony_driver`` 才能
        真正 new 一个 fresh 实例（缓存字段是单数 ``_instance``，见 ``_respawn_daemon`` 注释）。

        持 serial 锁：close 会把 sock 置 None + 清 singleton，必须与其它线程
        在飞的 invoke / 自愈互斥，否则重演「sock=None 被别人先看到」的 NPE。
        """
        with self._heal_lock:
            self._refresh_raw_from_singleton()
            self._release_hmclient_quietly()
            self._invalidate_hmdriver_singleton(self.serial)
            self._raw = None


def prepare_harmony_for_run(
    serial: str,
    *,
    screen_size: Optional[Tuple[int, int]] = None,
    force: bool = False,
    swipe: Optional[bool] = None,
) -> None:
    """不依赖 hmdriver2 的 HarmonyOS Run 前唤醒。

    这条路径故意只走 hdc：黑屏态下 hmdriver2/uitest socket 可能还没恢复，
    而 Server 大脑的 prepare_for_run 又必须发生在首次截图和缓存回放前。
    """
    settings = get_settings()
    if not force and not settings.harmony_wake_before_run:
        return

    do_swipe = bool(settings.harmony_wake_swipe_enabled) and bool(swipe)
    was_lit = _harmony_screen_is_lit(serial) if do_swipe else None

    try:
        out = hdc_shell(
            serial,
            "power-shell wakeup",
            timeout=5.0,
            check=False,
        )
        if out:
            logger.debug("设备 {} HarmonyOS wakeup 输出：{}", serial, out[:160])
        logger.info("设备 {} Run 前唤醒 HarmonyOS：power-shell wakeup", serial)
    except Exception as exc:  # noqa: BLE001
        logger.warning("设备 {} Run 前唤醒 HarmonyOS 失败：{}", serial, exc)
        raise

    settle_s = max(0, int(settings.harmony_wake_settle_ms)) / 1000.0
    if settle_s > 0:
        time.sleep(settle_s)

    if not do_swipe:
        return
    if was_lit is True:
        logger.info("设备 {} HarmonyOS Run 前已亮屏，跳过兜底上滑", serial)
        return

    width, height = _normalize_preflight_screen_size(screen_size)
    x = round(width * 0.5)
    from_y = round(height * 0.82)
    to_y = round(height * 0.35)
    velocity = 1500
    try:
        out = hdc_shell(
            serial,
            f"uitest uiInput swipe {x} {from_y} {x} {to_y} {velocity}",
            timeout=5.0,
            check=False,
        )
        if out:
            logger.debug("设备 {} HarmonyOS wake 上滑输出：{}", serial, out[:160])
        logger.info(
            "设备 {} HarmonyOS wake 后上滑进入可操作态：({}, {}) -> ({}, {})",
            serial,
            x,
            from_y,
            x,
            to_y,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("设备 {} HarmonyOS wake 后上滑失败：{}", serial, exc)
        raise

    swipe_settle_s = max(0, int(settings.harmony_wake_swipe_settle_ms)) / 1000.0
    if swipe_settle_s > 0:
        time.sleep(swipe_settle_s)


def _normalize_preflight_screen_size(
    screen_size: Optional[Tuple[int, int]],
) -> Tuple[int, int]:
    if screen_size is not None:
        try:
            width = int(screen_size[0])
            height = int(screen_size[1])
            if width > 0 and height > 0:
                return width, height
        except Exception:  # noqa: BLE001
            pass
    # 只在拿不到设备注册尺寸时兜底；当前主力鸿蒙机为 1080x2504，1080x2400 的
    # 比例上滑仍能从锁屏壁纸回到可操作态。
    return 1080, 2400


def _harmony_screen_is_lit(serial: str) -> Optional[bool]:
    try:
        out = hdc_shell(
            serial,
            "hidumper -s PowerManagerService -a -s",
            timeout=3.0,
            check=False,
        ) or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("设备 {} HarmonyOS wake 前读取屏幕状态失败：{}", serial, exc)
        return None
    if _harmony_screen_is_off(out):
        return False
    if _harmony_screen_is_on(out):
        return True
    return None


def _harmony_screen_is_off(hidumper_out: str) -> bool:
    text = hidumper_out or ""
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


def _harmony_screen_is_on(hidumper_out: str) -> bool:
    text = hidumper_out or ""
    for needle in (
        "Current State: AWAKE",
        "Screen On: true",
        "screenOn: true",
    ):
        if needle in text:
            return True
    return False


# ----------------------------------------------------------------------
# 设备发现
# ----------------------------------------------------------------------
def list_harmony_devices(include_offline: bool = False) -> List[DeviceInfo]:
    """扫描 hdc 已识别的鸿蒙设备。

    - hdc 不在 PATH / hdc server 未起 → 返空（不抛，不要让整机扫描爆炸）
    - 已 Connected 的设备尝试构造 HarmonyDriver 拿完整信息（model / 屏幕尺寸）；
      失败（hmdriver2 未装 / uitest 未起）只返基本信息
    - Unauthorized / Offline 只返 serial + status，不构造 driver 避免阻塞
    """
    if not hdc_available():
        return []

    infos: List[DeviceInfo] = []
    try:
        targets = hdc_list_targets()
    except Exception as exc:  # noqa: BLE001
        logger.debug("hdc list_targets 失败，跳过鸿蒙扫描: {}", exc)
        return []

    for t in targets:
        status_lower = t.status.lower()
        if status_lower != "connected":
            if include_offline:
                infos.append(
                    DeviceInfo(
                        serial=t.serial,
                        platform="harmony",
                        status=status_lower if status_lower else "offline",
                    )
                )
            continue

        # 尝试完整探测
        if not _HMDRIVER2_AVAILABLE:
            # 没装 hmdriver2：只能靠 hdc shell 拿最少信息
            infos.append(_fallback_info_via_hdc(t.serial))
            continue
        try:
            # 扫描路径也会打一次息屏设置——靠 _STAY_AWAKE_DONE 做 serial 粒度
            # 幂等，只有插上第一次才真跑 hdc shell，后续 rescan 命中缓存直接跳过
            drv = HarmonyDriver(
                t.serial,
                setup_power=bool(get_settings().harmony_setup_stay_awake),
            )
            infos.append(drv.device_info())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony 设备 {} 完整探测失败，退到 hdc-only 信息：{}",
                t.serial, exc,
            )
            infos.append(_fallback_info_via_hdc(t.serial))
    return infos


def _is_hdc_error_text(s: Optional[str]) -> bool:
    """判断字符串是不是 hdc 的错误码输出（而不是真实 param 值）。

    hdc 在设备临时不可达 / param 不存在 / uitest 未起时，会把错误文本原样写到
    stdout，**exit code 还是 0**——这就是为什么我们要用内容判定：

    - ``[Fail][E001005] Device not found or connected`` —— 设备不可达
    - ``[Empty]`` —— param 不存在
    - ``Get parameter "const.ohos.version.release" fail! errNum is:106!`` ——
      OpenHarmony 原生 ``param get`` 在 key 不存在时这样喊（HUAWEI ALT-AL10 /
      EMUI 鸿蒙壳子常见）。106 是 "param not exist" 的 errCode
    - ``error: ...`` —— 其它 hdc 一般错误

    真实的 ``const.*`` 值不会出现这些模式。模块级提供，供 ``device_info`` 与
    ``_fallback_info_via_hdc`` 共用，避免错误文本被当成"设备品牌"显示到 Web 卡片上。
    """
    if not s:
        return False
    lower = s.strip().lower()
    if not lower:
        return False
    if lower.startswith("[fail]") or lower.startswith("[empty]"):
        return True
    if lower.startswith("error:") or lower.startswith("[e0"):
        return True
    # ``[Fail][E00xxxx] Device not found`` 形态再多兜一层
    if "[e0" in lower and ("device" in lower or "not found" in lower):
        return True
    # OpenHarmony param get 失败格式：``get parameter "xxx" fail! errnum is:106!``
    # 不做 startswith 匹配：有的机型会在前面多一段设备前缀 / 空行
    if "get parameter" in lower and ("fail!" in lower or "errnum" in lower):
        return True
    # 兜底：任意一行同时出现 fail! 和 errnum —— 鸿蒙家族通用错误打印风格
    if "errnum" in lower and "fail" in lower:
        return True
    return False


def _fallback_info_via_hdc(serial: str) -> DeviceInfo:
    """hmdriver2 不可用时的设备信息兜底：只用 hdc shell param get。

    每次调用都会起 3 次 hdc subprocess（~300ms），只在设备发现扫描阶段调一次，
    不在热路径上。
    """
    def _param(name: str) -> str:
        try:
            raw = hdc_shell(
                serial, f"param get {name}", timeout=3.0, check=False,
            ).replace("\r", "").strip()
            return "" if _is_hdc_error_text(raw) else raw
        except HdcError:
            return ""

    brand = _param("const.product.brand")
    model = _param("const.product.model")
    os_version = _param("const.ohos.version.release") or _param("const.product.software.version")
    return DeviceInfo(
        serial=serial,
        platform="harmony",
        brand=brand,
        model=model,
        os_version=os_version,
        screen_width=0,
        screen_height=0,
        status="online",
        extra={"hmdriver2": "unavailable"},
    )


def open_harmony_driver(serial: str, **_kwargs: Any) -> HarmonyDriver:
    """按 serial 打开一个 HarmonyDriver；找不到 / 未授权会抛 RuntimeError。

    ``**_kwargs`` 当前没用，只是和 iOS 的签名对齐（``on_status`` 未来可用于
    上报 hmdriver2 首次连接进度 / hypium-agent.hap 下发进度到 web 提示条）。
    """
    return HarmonyDriver(
        serial,
        setup_power=bool(get_settings().harmony_setup_stay_awake),
    )


__all__ = [
    "HarmonyDriver",
    "prepare_harmony_for_run",
    "list_harmony_devices",
    "open_harmony_driver",
]
