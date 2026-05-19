"""iOS WDA 生命周期策略（auto / stable 双模式）。

公开口径见 ``docs/ios-setup（iOS接入指南）.md``；本模块落地 auto / stable 双模式约束。

为什么独立成模块：
- 调试期"频繁热拔插自愈"是已经跑稳的生产能力（auto 模式），不能被部署期
  "成功后尽量不碰"的诉求（stable 模式）改动污染。
- WDA 生命周期决策（preload / respawn / invalidate close / spawn）必须收敛到
  本模块；调用方只问 ``policy.allow_xxx(...)``，绝不在多个文件里散落
  ``if mode == "stable":``——一旦扩散后续就有人偷偷在某条路径里再做一次
  自愈，stable 的"少扰动"承诺会被悄悄打穿。
- launcher / open_ios_driver / device_provider 都不感知 ``auto/stable`` 业务
  语义，只读布尔参数或调 ``policy.xxx``，模式判断收敛到 ``get_policy()``
  这一处。

policy 默认值（auto 模式）必须与本模块落地前的"无 policy"隐含默认完全等价：
preload=True / runtime_drop_respawn=True / preflight_deadlock_respawn=True /
driver_invalidation_close=True / spawn=True。auto 路径走过来一行也不应感
知 policy 的存在。

**配置来源**（修 Codex P2）：从 ``ai_phone.config.get_settings()`` 读，与项目
其他模块一致。pydantic Settings 在 ``model_config`` 里声明了
``env_file=(".env", ".env.local")``——意味着部署把
``AI_PHONE_IOS_WDA_LIFECYCLE_MODE=stable`` 放在 ``.env.local`` 也能被识别。
**禁止**直接 ``os.environ.get("AI_PHONE_IOS_WDA_LIFECYCLE_MODE")``：
``agent/main.py`` 顶部的 ``_load_dotenv()`` 只加载 ``.env``，绕过 Settings 会
让 ``.env.local`` 里的 lifecycle 配置静默失效，stable 名存实亡却不报错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Optional, Set


__all__ = [
    "IosWdaLifecycleMode",
    "IosWdaLifecyclePolicy",
    "StableWdaUnavailable",
    "get_ios_wda_lifecycle_policy",
    "reset_ios_wda_lifecycle_policy",
]


class IosWdaLifecycleMode(str, Enum):
    """iOS WDA 生命周期模式。

    - ``AUTO``：调试期。允许自动预热 / 自动 respawn / 失效后 close 重建，
      与本方案落地前的现有行为完全等价。
    - ``STABLE``：部署期。人工准备一次后 agent 只 attach/reuse，不主动
      respawn/relaunch WDA；失效后抛 ``StableWdaUnavailable``，由调用方
      显式上报浏览器。
    """

    AUTO = "auto"
    STABLE = "stable"

    @classmethod
    def parse(cls, raw: Optional[str]) -> "IosWdaLifecycleMode":
        """env 解析容错：空值 / 大小写 / 未知值都回落到 AUTO，保证不影响调试期。"""
        if raw is None:
            return cls.AUTO
        value = raw.strip().lower()
        if not value:
            return cls.AUTO
        if value == cls.STABLE.value:
            return cls.STABLE
        return cls.AUTO


class StableWdaUnavailable(RuntimeError):
    """stable 模式专用：WDA 不可达且 policy 禁止自动重启。

    用途：
    - stable 下 ``_handle_ios_driver_unhealthy`` 返回 False 后由调用方抛出，
      或调用方直接捕获后改派 ``device_status``，告诉用户走"拔出 USB →
      人工准备 → 重新插入"那一套。
    - stable 下首次插入会话已 spawn 过、第二次 open 又找不到 WDA 时，
      ``open_ios_driver`` 抛出。
    """


@dataclass
class IosWdaLifecyclePolicy:
    """生命周期策略对象。一个 agent 进程一份，per-serial 状态机内嵌。"""

    mode: IosWdaLifecycleMode
    allow_initial_spawn_in_stable: bool = True
    _spawned_serials: Set[str] = field(default_factory=set)
    _lock: RLock = field(default_factory=RLock)

    # ------------------------------------------------------------------
    # 基本属性
    # ------------------------------------------------------------------
    @property
    def is_stable(self) -> bool:
        return self.mode == IosWdaLifecycleMode.STABLE

    @property
    def is_auto(self) -> bool:
        return self.mode == IosWdaLifecycleMode.AUTO

    # ------------------------------------------------------------------
    # 生命周期行为开关（auto 全部 True，与方案落地前的隐含默认等价）
    # ------------------------------------------------------------------
    def allow_preload(self) -> bool:
        """auto: 允许插线预热；stable: 不主动预热，避免空闲设备突然"WDA 编译中"。"""
        return self.is_auto

    def allow_runtime_drop_respawn(self) -> bool:
        """auto: 运行中 xcodebuild 退出后自动 respawn；stable: 不自动，等人工。"""
        return self.is_auto

    def allow_preflight_deadlock_respawn(self) -> bool:
        """auto: preflight 死锁 60s 后自动 respawn；stable: 不自动，提示人工解锁。"""
        return self.is_auto

    def allow_driver_invalidation_close(self) -> bool:
        """auto: ``/status`` 不通时 close + 摘 cache；stable: 不 close、不摘，
        交给上层 catch 后报错并显式 device_status 通知浏览器。"""
        return self.is_auto

    # ------------------------------------------------------------------
    # spawn 状态机（§7.5.1）
    # ------------------------------------------------------------------
    def allow_spawn(self, serial: str, *, reason: str = "open_driver") -> bool:
        """是否允许 launcher 本次执行 spawn 分支。

        auto:    永远允许（与现状一致）。
        stable:  按 ``allow_initial_spawn_in_stable`` 区分——
                 - True（默认 B 子方案）：本次"USB 插入会话"内最多一次。
                   ``record_spawned`` 之后再问就 False，直到
                   ``on_device_disconnected`` 把 serial 清出去。
                 - False（A 子方案）：恒为 False，要求外部已起 WDA。
        """
        if self.is_auto:
            return True
        if not self.allow_initial_spawn_in_stable:
            return False
        with self._lock:
            return serial not in self._spawned_serials

    def record_spawned(self, serial: str) -> None:
        """launcher.start() 真正走了 spawn 分支（不是 attach / disabled）后调用。

        注意：spawn 失败（xcodebuild rc!=0 / wait_ready 超时）**不要**调，
        否则用户根据错误提示走完准备链路后下次 open 会被状态机误拦。详见
        §7.5.1.4 第 5 条边界承诺。
        """
        if self.is_auto:
            # auto 下不维护集合，避免后续模式切换时残留脏状态。
            return
        with self._lock:
            self._spawned_serials.add(serial)

    def on_device_disconnected(self, serial: str) -> None:
        """USB 拔出 / udid 从 usbmux 列表消失。

        语义：宣告"本次插入会话结束"。下次 udid 重新出现时会被视为新一次
        插入，``allow_spawn`` 又返回 True。

        判定离线必须用"udid 完全从 usbmux 列表消失"这一信号；不要用
        ``status != "online"``（unauthorized / passcode_locked 仍属于"在场
        但需人工解锁"，清状态会让用户解锁后又被允许 spawn 一次）。
        """
        if self.is_auto:
            return
        with self._lock:
            self._spawned_serials.discard(serial)

    def spawned_serial_count(self) -> int:
        """debug 日志用，不影响行为。"""
        with self._lock:
            return len(self._spawned_serials)

    # ------------------------------------------------------------------
    # 错误文案
    # ------------------------------------------------------------------
    def stable_unavailable_message(self, reason: str) -> str:
        """统一的人类可读文案：让用户一眼知道下一步是"拔插重试 + 人工准备"。"""
        return (
            "iOS WDA stable mode: WDA 不可达，已跳过自动重启。"
            f"原因：{reason}。"
            "请人工确认 iPhone 是否解锁、WDA 是否仍运行、USB 是否稳定，"
            "恢复后请拔出并重新插入设备。"
        )


# ----------------------------------------------------------------------
# 全局单例（一个 agent 进程一份）
# ----------------------------------------------------------------------
_POLICY: Optional[IosWdaLifecyclePolicy] = None
_POLICY_LOCK = RLock()


def _build_policy_from_settings() -> IosWdaLifecyclePolicy:
    """从 pydantic Settings 构造 policy。

    走 ``get_settings()`` 让 ``.env`` 和 ``.env.local`` 两份文件都被尊重——
    部署机房通常把 ``stable`` 写在 ``.env.local``。

    Settings 不可用时（极少数纯测试场景下 import 失败）回落到全默认 auto，
    保留"不影响调试期"的承诺：宁可不开 stable，也不让 policy 模块本身把
    agent 启动顶崩。
    """
    try:
        from ai_phone.config import get_settings  # noqa: PLC0415
        settings = get_settings()
        mode_raw = getattr(settings, "ios_wda_lifecycle_mode", "auto")
        allow_initial_spawn = bool(
            getattr(settings, "ios_wda_stable_allow_initial_spawn", True)
        )
    except Exception:  # noqa: BLE001
        # Settings 拿不到时回落 auto + allow_initial_spawn=True，等同于"无策略"
        mode_raw = "auto"
        allow_initial_spawn = True
    return IosWdaLifecyclePolicy(
        mode=IosWdaLifecycleMode.parse(mode_raw),
        allow_initial_spawn_in_stable=allow_initial_spawn,
    )


def get_ios_wda_lifecycle_policy() -> IosWdaLifecyclePolicy:
    """返回进程级单例 policy。

    第一次调用时从 Settings 解析；之后复用，避免 spawn 状态机的内存集合每次
    new 一个新对象。
    """
    global _POLICY
    if _POLICY is not None:
        return _POLICY
    with _POLICY_LOCK:
        if _POLICY is None:
            _POLICY = _build_policy_from_settings()
    return _POLICY


def reset_ios_wda_lifecycle_policy() -> None:
    """测试钩子：单测里在改 env 后调用，强制下一次 ``get_*`` 重新解析。

    同时清掉 ``get_settings()`` 的 ``lru_cache``——否则单测改完 env 之后
    policy 拿到的还是旧 Settings，下次调用拿到的是缓存的 auto。

    生产代码不应调用——agent 运行期切换 mode 不在本方案承诺范围内。
    """
    global _POLICY
    with _POLICY_LOCK:
        _POLICY = None
    try:
        from ai_phone.config import get_settings  # noqa: PLC0415
        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
