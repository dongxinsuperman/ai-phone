"""Agent 侧 iOS 虚拟机生命周期管理。

对标 :class:`ai_phone.agent.android_vm.manager.AndroidVmManager`——方法名、锁模型、
状态上报、重连认领、消失巡检全部对齐（方案 §0.3：一等对照物是 Android 虚拟机）。

身份锚点：**虚拟机名字** ``aiphone_sim_<vm_id>``，与 Android 用 AVD 名
``aiphone_vm_<vm_id>`` 同构。名字持久落在宿主磁盘，Agent 重启后据此反解 vm_id
认领回来。UDID 由 ``simctl create`` 生成，创建后固定不变，作为设备 serial 上报。

与 Android / 鸿蒙的三处实质差异（方案 §6.5.5）：

1. **不需要 Server 端口租约。** 虚拟机 serial 是 UDID、天然全局唯一，WDA 端口纯
   本机事务（``ios_simulator_wda.allocate_ports``），两台 Mac 各用 8300 互不影响。
2. **不需要「改配置 → 删了重建」这条产品路径。** 机型与系统版本创建后锁定，
   检测到不一致就报告错误，不能用一次启动指令暗中删除已有实例和数据。
3. **就绪判据是 boot + WDA**。``simctl bootstatus`` 只证明系统起来了；能不能截图
   点击要看 WDA。对齐鸿蒙「HDC Connected 之后还要等 hmdriver2 握手」的做法，
   不把「开机完成」当「设备可用」。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.drivers.ios_simulator import mark_managed, unmark_managed
from ai_phone.agent.drivers.ios_simulator_wda import (
    IosSimulatorWdaLauncher,
    allocate_ports,
    drop_port_reservation,
    release_ports,
)
from ai_phone.agent.drivers.simctl import SimctlError, simctl_run
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.config import get_settings
from ai_phone.shared import protocol as P

from .capability import find_ios_sim_tools, probe_ios_sim_capability


# 受管虚拟机的名字前缀。与 Android 的 ``aiphone_vm_`` 同构：名字是持久身份锚点，
# Agent 重启后靠它把仍在运行的实例认领回来。
_MANAGED_NAME_PREFIX = "aiphone_sim_"


def _start_deadline(msg: Dict[str, Any]) -> Optional[float]:
    raw = msg.get("operation_timeout_sec")
    if raw in (None, ""):
        return None
    budget = float(raw)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("operation_timeout_sec must be a positive finite number")
    deadline = time.monotonic() + budget
    queued_deadline = msg.get("_operation_deadline")
    if queued_deadline is not None:
        queued_deadline = float(queued_deadline)
        if not math.isfinite(queued_deadline):
            raise ValueError("operation deadline must be finite")
        deadline = min(deadline, queued_deadline)
    return deadline


def _startup_timeout(deadline: Optional[float], maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("iOS Simulator startup deadline exceeded")
    return min(maximum, remaining)


def _register_wda_endpoint(udid: str, wda_port: int, mjpeg_port: int) -> None:
    """把已就绪的 WDA 登记进端点表，供 readiness 探针与镜像使用。

    **必须在这里登记，不能只靠 ``open_ios_simulator_driver``**：本类是自己起
    launcher 的，不走开驱动那条路；而 readiness 探针只认端点表——不登记的话，
    实例明明跑着，探针却一直报 ``wda_not_ready``，设备永远进不了派单池。

    这里放的 ``WdaClient`` 是**不带 session 的**（构造函数只建 HTTP 连接池，不发
    请求）。探针只用它的 ``base_url``。等驱动真被打开时会用带 session 的那把覆盖
    本条目，镜像下发 appium settings 靠的是那一把。
    """
    try:
        from ai_phone.agent.drivers.ios_simulator_driver import (  # noqa: PLC0415
            SimWdaEndpoint,
            register_sim_endpoint,
        )
        from ai_phone.agent.drivers.wda_client import WdaClient  # noqa: PLC0415

        register_sim_endpoint(
            udid,
            SimWdaEndpoint(
                wda_port=int(wda_port),
                mjpeg_port=int(mjpeg_port),
                client=WdaClient(base_url=f"http://127.0.0.1:{int(wda_port)}"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # 登记失败只影响 readiness 与镜像，实例本身已经跑起来了，不该因此判失败
        logger.warning("登记 WDA 端点失败 udid={}（不影响实例运行）：{}", udid, exc)


def _unregister_wda_endpoint(udid: str) -> None:
    try:
        from ai_phone.agent.drivers.ios_simulator_driver import (  # noqa: PLC0415
            unregister_sim_endpoint,
        )

        unregister_sim_endpoint(udid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("摘除 WDA 端点失败 udid={}：{}", udid, exc)


def managed_sim_name(vm_id: str) -> str:
    """VM ID 原样进入持久名字；不能截断或替换字符造成身份碰撞。"""
    if not re.fullmatch(r"[A-Za-z0-9_]+", vm_id or ""):
        raise ValueError("vm_id must contain only letters, digits and underscores")
    return f"{_MANAGED_NAME_PREFIX}{vm_id}"


def vmid_from_sim_name(name: str) -> str:
    """从受管虚拟机名反解 vm_id；非受管返回空串。与 :func:`managed_sim_name` 互逆。"""
    n = (name or "").strip()
    if n.startswith(_MANAGED_NAME_PREFIX):
        suffix = n[len(_MANAGED_NAME_PREFIX):]
        if re.fullmatch(r"[A-Za-z0-9_]+", suffix):
            return suffix
    return ""


class SimctlListFailed(RuntimeError):
    """``simctl list`` 本身失败了——**不等于**「实例不存在」。

    区分这两者对删除路径是必须的：把「没问到」当成「没有」，会让删除返回成功
    而实例还在。
    """


class InstanceIdentityError(RuntimeError):
    """VM ID、实例名或 UDID 对不上；禁止猜测目标后操作。"""


class SimVmStartFailed(RuntimeError):
    """启动失败仍携带已经创建的实例身份，Server 不能因此以为从未创建过。"""

    def __init__(self, message: str, udid: str, details: Dict[str, Any]) -> None:
        super().__init__(message)
        self.udid = udid
        self.details = details


@dataclass
class SimVmRuntime:
    """一台受管虚拟机的进程内运行态。字段与 Android ``VmRuntime`` 一一对应。"""

    vm_id: str
    name: str
    udid: str
    sim_name: str
    device_type: str
    runtime_id: str
    started_at: float
    wda_port: int = 0
    mjpeg_port: int = 0
    launcher: Optional[IosSimulatorWdaLauncher] = None
    # ready 只表示已确认 boot + WDA；缺席两轮仅触发严格状态核实，不等于消失。
    ready: bool = False
    missing_ticks: int = 0
    start_request_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class IosSimVmManager:
    """一个 Agent 进程内所有受管 iOS 虚拟机的所有者。"""

    def __init__(
        self,
        *,
        runtime_dir: Optional[Path] = None,
        max_instances: Optional[int] = None,
        drop_driver_cache: Optional[Callable[[str], None]] = None,
    ) -> None:
        settings = get_settings()
        self.runtime_dir = runtime_dir or (Path(settings.storage_dir) / "ios_sim_runtime")
        self._max_instances_override = max_instances
        # 与鸿蒙同形：manager 不认识 agent 的 driver 缓存，由 main 注入摘除动作
        self._drop_driver_cache = drop_driver_cache
        self._runtimes: Dict[str, SimVmRuntime] = {}
        self._last_reclaimed_ids: set[str] = set()
        # 两层锁，语义与 Android 完全一致：
        # ① _start_lock（全局）：保护「查重 + 创建 + 占位注册」临界区
        # ② _vm_lock(vm_id)（按 VM，RLock）：串行化同一台的 start/stop/delete，
        #    杜绝「边启动边停止」把占位 pop 掉留下幽灵 running
        self._start_lock = threading.Lock()
        self._vm_locks: Dict[str, threading.RLock] = {}
        self._vm_locks_guard = threading.Lock()
        # 只用于拒绝认领/巡检开始后发生启停的旧快照，不改变对外状态或协议。
        self._vm_generations: Dict[str, int] = {}

    def _vm_lock(self, vm_id: str) -> "threading.RLock":
        with self._vm_locks_guard:
            lk = self._vm_locks.get(vm_id)
            if lk is None:
                lk = threading.RLock()
                self._vm_locks[vm_id] = lk
            return lk

    # 这些旋钮属于 Server 下发集，每次读 get_settings() 保证下发后立即生效
    @property
    def max_instances(self) -> int:
        return self._max_instances_override or get_settings().ios_sim_max_instances

    @property
    def boot_timeout_sec(self) -> int:
        return int(get_settings().ios_sim_boot_timeout_sec)

    # ------------------------------------------------------------------
    # 设备池装饰（对齐 android_vm decorate_devices）
    # ------------------------------------------------------------------
    def decorate_devices(self, infos: List[DeviceInfo]) -> List[DeviceInfo]:
        """给受管虚拟机补上纳管身份字段。

        与 Android 的差异：**这里不需要过滤未纳管实例**——过滤已经在更前面完成了
        （``list_ios_simulators`` 默认 ``managed_only=True``，只上报纳管表里的
        UDID）。所以本方法只做「补字段」，不做「丢弃」。
        """
        udid_to_runtime = {
            rt.udid: rt for rt in list(self._runtimes.values()) if rt.udid
        }
        for info in infos:
            rt = udid_to_runtime.get(info.serial)
            if rt is None:
                continue
            extra = dict(info.extra or {})
            extra.update(
                {
                    "device_kind": "virtual",
                    "is_virtual": True,
                    "vm_platform": "ios",
                    "vm_instance_id": rt.vm_id,
                    "vm_name": rt.name,
                }
            )
            info.extra = extra
        return infos

    # ------------------------------------------------------------------
    # 异步 WS handler（签名与 Android 一致）
    # ------------------------------------------------------------------
    async def handle_capability_probe(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        result = await asyncio.to_thread(self.probe, msg)
        await client.send(
            {
                "type": P.MSG_IOS_SIM_VM_CAPABILITY,
                "request_id": msg.get("request_id") or "",
                "agent_id": client.agent_id,
                **result,
            }
        )

    async def handle_start(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        await client.send(self._status_payload(msg, state="starting", ok=True, reason="starting"))
        # WS 分发层按 VM 串行调度；这里必须等到操作真正结束，不能再另起脱队任务。
        await self._start_and_report(client, msg)

    async def handle_stop(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        await client.send(self._status_payload(msg, state="stopping", ok=True, reason="stopping"))
        result = await asyncio.to_thread(
            self.stop_sync, str(msg.get("vm_id") or ""), str(msg.get("udid") or "")
        )
        await client.send(
            self._status_payload(
                msg,
                state="stopped" if result.get("ok") else "error",
                ok=bool(result.get("ok")),
                reason=str(result.get("reason") or ""),
                error=str(result.get("error") or ""),
                udid=str(result.get("udid") or ""),
                details=dict(result.get("details") or {}),
            )
        )
        await self._refresh_devices_safe(client)

    async def handle_delete(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        """删除某台受管虚拟机（Server 删配置 / 换绑新 Agent 时下发）。

        名字按 vm_id 独占（``aiphone_sim_<vmid>``），只命中这一台，不影响其它实例、
        也不影响用户自己在 Xcode 里建的虚拟机。
        """
        result = await asyncio.to_thread(
            self.delete_sync,
            str(msg.get("vm_id") or ""),
            str(msg.get("udid") or ""),
        )
        await client.send(
            self._status_payload(
                msg,
                state="stopped" if result.get("ok") else "error",
                ok=bool(result.get("ok")),
                reason=str(result.get("reason") or ""),
                error=str(result.get("error") or ""),
                details={
                    **dict(result.get("details") or {}),
                    "sim_name": str(result.get("sim_name") or ""),
                    "deleted": bool(result.get("ok")),
                    "cleanup_confirmed": bool(result.get("ok")),
                },
            )
        )
        await self._refresh_devices_safe(client)

    async def _start_and_report(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        try:
            # 排入共享 executor 之前固定本机时限。网络消息中的同名内部字段必须
            # 丢弃，不能把另一进程的 monotonic 值当成本机时钟或延长排队后的预算。
            operation_msg = {key: value for key, value in msg.items() if key != "_operation_deadline"}
            operation_msg["_operation_deadline"] = _start_deadline(operation_msg)
            result = await asyncio.to_thread(self.start_sync, operation_msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("iOS 虚拟机启动失败 vm_id={}: {}", msg.get("vm_id"), exc)
            runtime = self._runtimes.get(str(msg.get("vm_id") or ""))
            await client.send(
                self._status_payload(
                    msg, state="error", ok=False, reason="start_failed", error=str(exc),
                    udid=runtime.udid if runtime else str(getattr(exc, "udid", "")),
                    details={**(dict(runtime.details) if runtime else {}),
                             **dict(getattr(exc, "details", {}))},
                )
            )
            await self._refresh_devices_safe(client)
            return
        await client.send(
            self._status_payload(
                msg,
                state="running",
                ok=True,
                reason="running",
                udid=str(result.get("udid") or ""),
                details=dict(result.get("details") or {}),
            )
        )
        await self._refresh_devices_safe(client)

    async def _refresh_devices_safe(self, client: AgentWSClient) -> None:
        try:
            await client.refresh_devices()
        except Exception as exc:  # noqa: BLE001
            logger.warning("iOS 虚拟机刷新设备快照失败（忽略）：{}", exc)

    # ------------------------------------------------------------------
    # 探查
    # ------------------------------------------------------------------
    def probe(self, requirement: Dict[str, Any]) -> Dict[str, Any]:
        return probe_ios_sim_capability(
            requirement,
            current_instances=len(self._runtimes),
            max_instances=self.max_instances,
        )

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start_sync(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        deadline = _start_deadline(msg)
        vm_id = str(msg.get("vm_id") or "").strip()
        if not vm_id:
            raise ValueError("vm_id is required")
        with self._vm_lock(vm_id):
            self._vm_generations[vm_id] = self._vm_generations.get(vm_id, 0) + 1
            attempt: Dict[str, Any] = {}
            try:
                return self._start_sync_locked(vm_id, msg, attempt, deadline)
            except Exception as exc:
                if not attempt.get("instance_selected") and not attempt.get("create_attempted"):
                    if isinstance(exc, TimeoutError) and not attempt.get("operation_started"):
                        raise SimVmStartFailed(str(exc), "", {
                            "operation_not_started": True, "created_in_attempt": False,
                            "instance_deleted": False, "cleanup_confirmed": True,
                        }) from exc
                    raise
                raise self._finish_failed_start(vm_id, attempt, exc) from exc

    def _start_sync_locked(
        self, vm_id: str, msg: Dict[str, Any], attempt: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        attempt = attempt if attempt is not None else {}
        _startup_timeout(deadline, 30.0)
        existing = self._runtimes.get(vm_id)
        # Server 仅首次绑定下发 rollback；有已绑定身份或当前可用实例时仍保护原数据。
        attempt["rollback_allowed"] = bool(
            msg.get("rollback_new_instance") and not msg.get("require_existing_instance")
            and not msg.get("udid") and not (existing and existing.ready)
        )
        if existing is not None and existing.details.get("cleanup_pending"):
            attempt["operation_started"] = True
            stopped = self._stop_sync_locked(vm_id, existing.udid)
            if not stopped.get("ok"):
                raise RuntimeError(f"上次操作仍待清理，拒绝再次启动：{stopped.get('error')}")

        capability = self.probe(dict(msg))
        _startup_timeout(deadline, 30.0)
        if not capability.get("ok"):
            raise RuntimeError(str(capability.get("reason") or "ios simulator capability unavailable"))

        device_type = str(msg.get("device_type") or "").strip()
        runtime_id = str(msg.get("runtime") or "").strip()
        if not device_type:
            raise ValueError("device_type is required")
        # 探查已把人类可读版本（如 "26.0"）解析成确切 runtime；启动必须用 identifier，
        # 避免 simctl 在多个同前缀 runtime 间自行挑选。
        matched = (capability.get("details") or {}).get("matched_runtime") or {}
        resolved_runtime = str(matched.get("identifier") or runtime_id)
        if not resolved_runtime:
            raise ValueError("runtime is required")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        sim_name = managed_sim_name(vm_id)
        alias = str(msg.get("alias") or msg.get("name") or vm_id)

        with self._start_lock:
            udid = self._ensure_instance(
                sim_name=sim_name,
                device_type=device_type,
                runtime_id=resolved_runtime,
                expected_udid=str(msg.get("udid") or (existing.udid if existing else "")),
                require_existing=bool(msg.get("require_existing_instance") or existing),
                attempt=attempt,
                deadline=deadline,
            )
            attempt.update({"instance_selected": True, "udid": udid})
            runtime = existing if existing is not None and existing.udid == udid else SimVmRuntime(
                vm_id=vm_id, name=alias, udid=udid, sim_name=sim_name,
                device_type=device_type, runtime_id=resolved_runtime, started_at=time.time(),
            )
            runtime.ready = False
            runtime.start_request_id = str(msg.get("request_id") or "")
            self._runtimes[vm_id] = runtime
            try:
                ports = allocate_ports(udid)
            except Exception as exc:
                # simctl create 已成功，即使端口预留失败也必须把这台身份报告回去。
                runtime.details.update({"instance_created": True, "sim_name": sim_name})
                raise SimVmStartFailed(str(exc), udid, dict(runtime.details)) from exc
            runtime.wda_port, runtime.mjpeg_port = ports.wda, ports.mjpeg

        self._boot(udid, deadline=deadline)
        _startup_timeout(deadline, float(get_settings().wda_startup_timeout_s))
        # 已核实的持久实例名直接用于 WDA 身份校验，不让查询失败削弱校验。
        launcher = runtime.launcher or IosSimulatorWdaLauncher(
            udid, expect_sim_name=sim_name, device_type=device_type
        )
        # start 抛错也必须持有 launcher，才能回收已拉起的 iPad xcodebuild。
        runtime.launcher = launcher
        if deadline is None:
            launcher.start()
        else:
            launcher.start(
                wait_ready_s=_startup_timeout(deadline, float(get_settings().wda_startup_timeout_s)),
                deadline=deadline,
            )
        _startup_timeout(deadline, 1.0)
        runtime.ready = True
        runtime.missing_ticks = 0
        mark_managed(udid)
        _register_wda_endpoint(udid, ports.wda, ports.mjpeg)

        details = {
            "sim_name": sim_name,
            "device_type": device_type,
            "runtime": resolved_runtime,
            "wda_port": ports.wda,
            "mjpeg_port": ports.mjpeg,
            "reused": existing is runtime,
            "created_in_attempt": bool(attempt.get("created_in_attempt")),
        }
        runtime.details = dict(details)
        logger.info(
            "iOS 虚拟机已就绪 vm_id={} udid={} name={} wda={} ",
            vm_id, udid, sim_name, ports.wda,
        )
        return {"udid": udid, "details": details}

    def _finish_failed_start(
        self, vm_id: str, attempt: Dict[str, Any], error: Exception,
    ) -> SimVmStartFailed:
        """一次启动失败只做一次停止；首次绑定另做一次精确删除，不安排后台重试。"""
        created_here = bool(attempt.get("created_in_attempt"))
        rollback = bool(attempt.get("rollback_allowed") and created_here)
        udid = str(attempt.get("udid") or "")
        details: Dict[str, Any] = {
            "sim_name": managed_sim_name(vm_id), "rollback_new_instance": rollback,
            "instance_created": bool(udid), "cleanup_confirmed": False, "instance_deleted": False,
            "created_in_attempt": created_here,
        }
        # create 命令失败/超时也可能已落盘。只能核实唯一的受管名字，不靠错误文本猜。
        if not udid:
            try:
                udid = self._owned_target(vm_id, self._list_all_instances_strict())
            except (SimctlListFailed, InstanceIdentityError) as exc:
                details["cleanup_error"] = str(exc)
                return SimVmStartFailed(f"{error}；无法确认创建结果：{exc}", "", details)
            if not udid:
                details.update({"cleanup_confirmed": True, "instance_deleted": False})
                return SimVmStartFailed(str(error), "", details)
            # 创建前严格清单确认没有同名实例，本次 create 后才出现唯一受管实例。
            created_here = bool(attempt.get("create_attempted"))
            rollback = bool(attempt.get("rollback_allowed") and created_here)
            details.update({"created_in_attempt": created_here, "rollback_new_instance": rollback})
            details["instance_created"] = True
        runtime = self._runtimes.get(vm_id)
        if runtime is not None:
            runtime.ready = False
        cleanup = self._stop_sync_locked(vm_id, udid)
        if cleanup.get("ok") and rollback:
            # 再核对身份和 Shutdown 后仅删除，不再发第二次 stop。
            cleanup = self._delete_sync_locked(vm_id, udid, stop_first=False)
        confirmed = bool(cleanup.get("ok"))
        details.update({
            "cleanup_confirmed": confirmed, "instance_deleted": rollback and confirmed,
            "instance_created": not (rollback and confirmed),
        })
        if not confirmed:
            details.update({"cleanup_pending": True, "cleanup_error": str(cleanup.get("error") or cleanup.get("reason"))})
            runtime = self._runtimes.get(vm_id)
            if runtime is not None:
                runtime.details.update(details)
            return SimVmStartFailed(
                f"{error}；启动失败后的关机尚未完成或实例清理失败：{details['cleanup_error']}", udid, details
            )
        return SimVmStartFailed(str(error), udid, details)

    def _ensure_instance(
        self, *, sim_name: str, device_type: str, runtime_id: str,
        expected_udid: str = "", require_existing: bool = False,
        attempt: Optional[Dict[str, Any]] = None,
        deadline: Optional[float] = None,
    ) -> str:
        """确保存在一台名为 ``sim_name`` 的虚拟机，返回其 UDID。

        实例是**常驻**的（方案 §6.5.1）：同一个 vm_id 永远对应同一台虚拟机，已装的
        App 与数据跨启停留存。所以这里优先复用同名实例，而不是每次新建。

        机型或系统版本不一致时拒绝启动，已有实例的数据不能被隐式删除重建。
        """
        if attempt is not None:
            attempt["created_in_attempt"] = False
        _startup_timeout(deadline, 30.0)
        found = self._find_by_name(sim_name, deadline=deadline)
        _startup_timeout(deadline, 30.0)
        if found is not None:
            udid, cur_type, cur_runtime = found
            if expected_udid and udid != expected_udid:
                raise InstanceIdentityError(
                    f"已有实例 {sim_name} 的 UDID={udid} 与记录 {expected_udid} 不一致，未执行启动"
                )
            if cur_type == device_type and cur_runtime == runtime_id:
                logger.info("复用已有受管虚拟机 name={} udid={}", sim_name, udid)
                return udid
            raise RuntimeError(
                f"已有虚拟机 {sim_name} 的配置不一致（机型 {cur_type}→{device_type}，"
                f"系统 {cur_runtime}→{runtime_id}）；未修改已有实例，请明确删除后新建"
            )
        if require_existing or expected_udid:
            raise RuntimeError(f"原虚拟机实例 {sim_name} 已缺失，未自动创建空白替代实例")

        if attempt is not None:
            attempt["create_attempted"] = True
        try:
            udid = simctl_run(
                "create", sim_name, device_type, runtime_id, timeout=_startup_timeout(deadline, 120.0)
            ).strip()
        except SimctlError as exc:
            raise RuntimeError(
                f"创建虚拟机失败 name={sim_name} device_type={device_type} "
                f"runtime={runtime_id}：{exc}"
            ) from exc
        if not udid:
            raise RuntimeError(f"创建虚拟机成功但未返回 UDID：name={sim_name}")
        if attempt is not None:
            attempt.update({"created_in_attempt": True, "udid": udid})
        _startup_timeout(deadline, 1.0)
        logger.info("已创建受管虚拟机 name={} udid={}", sim_name, udid)
        return udid

    def _find_by_name(self, sim_name: str, *, deadline: Optional[float] = None) -> Optional[tuple]:
        """按名字找已存在的实例，返回 ``(udid, device_type, runtime_id)``。"""
        instances = (self._list_all_instances_strict() if deadline is None else
                     self._list_all_instances_strict(timeout=_startup_timeout(deadline, 30.0)))
        matches = [
            (udid, meta) for udid, meta in instances.items()
            if meta.get("name") == sim_name
        ]
        if len(matches) > 1:
            raise InstanceIdentityError(f"存在多个同名实例 {sim_name}，拒绝选择或创建")
        if matches:
            udid, meta = matches[0]
            return udid, meta.get("device_type", ""), meta.get("runtime", "")
        return None

    def _list_all_instances(self) -> Dict[str, Dict[str, Any]]:
        """``simctl list devices -j`` 全量（含未启动），返回 ``{udid: meta}``。

        不复用 ``drivers.simctl.list_simulators``：那个只面向「可用设备发现」，
        会滤掉 ``isAvailable=False`` 的实例；而生命周期管理必须看到全部，否则
        「runtime 被卸载导致实例不可用」这种情况会被误判成「实例不存在」而重复创建。
        """
        return self._list_all_instances_strict()

    def _list_all_instances_strict(self, *, timeout: float = 30.0) -> Dict[str, Dict[str, Any]]:
        """同上，但**列举失败抛错**而不是返回空表。

        生命周期中的创建、停止、删除和对账都必须区分「查不到」与「没有」。
        """
        try:
            raw = simctl_run("list", "devices", "-j", timeout=timeout)
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("devices"), dict):
                raise ValueError("simctl list devices 返回的清单结构无效")
            for entries in payload["devices"].values():
                if not isinstance(entries, list) or any(
                    not isinstance(entry, dict) or not entry.get("udid") or not entry.get("name")
                    for entry in entries
                ):
                    raise ValueError("simctl list devices 返回的实例记录不完整")
        except Exception as exc:  # noqa: BLE001
            raise SimctlListFailed(str(exc)) from exc
        out: Dict[str, Dict[str, Any]] = {}
        for runtime_id, entries in (payload.get("devices") or {}).items():
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                udid = str(entry.get("udid") or "")
                if not udid:
                    continue
                out[udid] = {
                    "name": str(entry.get("name") or ""),
                    "state": str(entry.get("state") or ""),
                    "runtime": str(runtime_id),
                    "device_type": str(entry.get("deviceTypeIdentifier") or ""),
                    "is_available": bool(entry.get("isAvailable", False)),
                }
        return out

    def _boot(self, udid: str, *, deadline: Optional[float] = None) -> None:
        """开机并等待完成。已经开着的视为成功（幂等）。"""
        try:
            simctl_run("boot", udid, timeout=_startup_timeout(deadline, 120.0))
        except SimctlError as exc:
            text = f"{exc.stdout} {exc.stderr}".lower()
            # 已经 Booted 时 simctl 会报 "Unable to boot device in current state: Booted"
            if "current state: booted" in text or "already booted" in text:
                logger.debug("虚拟机 {} 已在运行，跳过 boot", udid)
            else:
                raise RuntimeError(f"虚拟机开机失败 udid={udid}：{exc}") from exc
        # bootstatus 是官方的开机完成判据（等价鸿蒙的 bootevent.boot.completed）
        try:
            simctl_run("bootstatus", udid, timeout=_startup_timeout(deadline, float(self.boot_timeout_sec)))
        except SimctlError as exc:
            raise RuntimeError(
                f"等待虚拟机开机完成超时 udid={udid}（上限 {self.boot_timeout_sec}s）：{exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 停止 / 删除
    # ------------------------------------------------------------------
    def stop_sync(self, vm_id: str, udid: str = "") -> Dict[str, Any]:
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {"ok": False, "reason": "bad_request", "error": "vm_id required", "udid": ""}
        with self._vm_lock(vm_id):
            self._vm_generations[vm_id] = self._vm_generations.get(vm_id, 0) + 1
            return self._stop_sync_locked(vm_id, udid)

    @staticmethod
    def _owned_target(vm_id: str, instances: Dict[str, Dict[str, Any]], udid: str = "") -> str:
        """校验持久身份后选目标，外部 UDID 只作校验，不能覆盖 VM ID 所有权。"""
        sim_name = managed_sim_name(vm_id)
        if udid in instances and instances[udid].get("name") != sim_name:
            raise InstanceIdentityError(
                f"udid={udid} 的实例名是「{instances[udid].get('name')}」，"
                f"与本配置的「{sim_name}」不符，已拒绝操作"
            )
        matches = [u for u, meta in instances.items() if meta.get("name") == sim_name]
        if len(matches) > 1:
            raise InstanceIdentityError(f"存在多个同名实例 {sim_name}，拒绝选择目标")
        return matches[0] if matches else ""

    def _stop_sync_locked(self, vm_id: str, udid: str = "") -> Dict[str, Any]:
        runtime = self._runtimes.get(vm_id)
        try:
            instances = self._list_all_instances_strict()
            target = self._owned_target(vm_id, instances, udid)
            if runtime is not None:
                self._owned_target(vm_id, instances, runtime.udid)
        except SimctlListFailed as exc:
            return {"ok": False, "reason": "instance_list_failed", "error": str(exc), "udid": udid}
        except (InstanceIdentityError, ValueError) as exc:
            return {"ok": False, "reason": "udid_name_mismatch", "error": str(exc), "udid": udid}
        if not target:
            if runtime is not None:
                if runtime.launcher is not None:
                    try:
                        runtime.launcher.stop()
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "reason": "wda_stop_failed", "error": str(exc),
                                "udid": runtime.udid}
                self._forget(vm_id, runtime.udid)
            return {"ok": True, "reason": "not_running", "udid": "",
                    "details": {"cleanup_confirmed": True}}
        udid = target
        meta = instances[target]
        if runtime is None or runtime.udid != target:
            runtime = SimVmRuntime(
                vm_id=vm_id, name=vm_id, udid=udid, sim_name=managed_sim_name(vm_id),
                device_type=str(meta.get("device_type") or ""),
                runtime_id=str(meta.get("runtime") or ""), started_at=time.time(),
            )
            self._runtimes[vm_id] = runtime
        if str(meta.get("state") or "").lower() == "shutdown":
            if runtime.launcher is not None:
                try:
                    runtime.launcher.stop()
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "reason": "wda_stop_failed", "error": str(exc), "udid": udid}
            self._forget(vm_id, udid)
            return {"ok": True, "reason": "stopped", "udid": udid,
                    "details": {"cleanup_confirmed": True}}

        runtime.ready = False
        # 先停 WDA 再关机：反过来会让 launcher 的 terminate 打在一台已经没了的设备上
        wda_error = ""
        if runtime.launcher is not None:
            try:
                runtime.launcher.stop()
            except Exception as exc:  # noqa: BLE001
                wda_error = str(exc)
                logger.warning("停止 WDA 失败，继续确认 guest 关机 udid={}：{}", udid, exc)
        shutdown_error = ""
        try:
            simctl_run("shutdown", udid, timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            shutdown_error = str(exc)
        # 命令成功、already-shutdown、超时都读回同一份事实；不靠宽泛错误文本猜成功。
        try:
            after = self._list_all_instances_strict()
            self._owned_target(vm_id, after, udid)
            stopped = udid not in after or str(after[udid].get("state") or "").lower() == "shutdown"
        except (SimctlListFailed, InstanceIdentityError) as exc:
            stopped = False
            shutdown_error = f"{shutdown_error}; {exc}".strip("; ")
        if not stopped or wda_error:
            error = shutdown_error or wda_error or "simctl shutdown 后实例仍未确认关机"
            runtime.details.update({"cleanup_pending": True, "cleanup_error": error})
            return {"ok": False, "reason": "stop_failed", "error": error, "udid": udid,
                    "details": dict(runtime.details)}
        self._forget(vm_id, udid)
        return {"ok": True, "reason": "stopped", "udid": udid,
                "details": {"cleanup_confirmed": True}}

    def restart_wda_sync(self, udid: str) -> bool:
        """只重启这台虚拟机的 WDA，**不关机、不删实例**。

        用于 WDA 卡死自愈：虚拟机本身是好的（照样能开机、装着 App、留着数据），
        坏的只是里面那个负责接受指令的 WDA。所以停掉它再拉起来就行，没必要
        动虚拟机——关机重开要几十秒，而且会丢掉当前打开的页面。

        调用方（``main._readiness_self_heal``）负责确认这台设备当下没人用；
        这里只管做，不重复判断占用。
        """
        udid = (udid or "").strip()
        if not udid:
            return False
        vm_id = ""
        runtime = None
        for candidate_id, rt in list(self._runtimes.items()):
            if rt.udid == udid:
                vm_id, runtime = candidate_id, rt
                break
        if runtime is None:
            logger.debug("udid={} 不在纳管表里，跳过 WDA 重启", udid)
            return False

        with self._vm_lock(vm_id):
            # 再确认一次：拿锁期间这台可能已经被停掉了
            if self._runtimes.get(vm_id) is not runtime:
                return False
            self._vm_generations[vm_id] = self._vm_generations.get(vm_id, 0) + 1
            launcher = runtime.launcher
            if launcher is None:
                return False
            try:
                launcher.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("重启前停 WDA 失败（继续）udid={}：{}", udid, exc)
            try:
                launcher.start()
            except Exception as exc:  # noqa: BLE001
                runtime.ready = False
                logger.warning("重启 WDA 失败 udid={}：{}", udid, exc)
                return False
            runtime.ready = True
            logger.info("udid={} 的 WDA 已重启", udid)
            return True

    def stop_all(self) -> int:
        vm_ids = list(self._runtimes.keys())
        for vm_id in vm_ids:
            self.stop_sync(vm_id)
        return len(vm_ids)

    def delete_sync(self, vm_id: str, udid: str = "") -> Dict[str, Any]:
        """先停再删该 vm_id 独占的虚拟机实例。实例不存在视为成功（幂等）。"""
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {"ok": False, "reason": "bad_request", "error": "vm_id required", "sim_name": ""}
        with self._vm_lock(vm_id):
            self._vm_generations[vm_id] = self._vm_generations.get(vm_id, 0) + 1
            return self._delete_sync_locked(vm_id, udid)

    def _delete_sync_locked(self, vm_id: str, udid: str, *, stop_first: bool = True) -> Dict[str, Any]:
        sim_name = managed_sim_name(vm_id)

        # 只删名字确实是 aiphone_sim_<vm_id> 的那一台。
        #
        # ``udid`` 是 Server 传来的外部输入，而 ``simctl delete`` 连同实例、
        # 已装应用和用户数据一起抹掉，没有回收站。安卓那边删的是
        # ``avdmanager delete avd -n <name>``，名字由 vm_id 算出、不接受外部
        # 标识，结构上就删不错人；我们必须按 UDID 删（simctl 只认 UDID），
        # 那就得自己补这道校验，才能拿到同等的安全性。
        # 删除必须用严格列举：拿不到清单就不能继续，否则「没问到」会被当成
        # 「没有」，回一个成功而实例还在。
        try:
            instances = self._list_all_instances_strict()
        except SimctlListFailed as exc:
            return {
                "ok": False,
                "reason": "instance_list_failed",
                "error": f"无法列举虚拟机实例，未执行删除：{exc}",
                "sim_name": sim_name,
            }
        try:
            target = self._owned_target(vm_id, instances, udid)
        except InstanceIdentityError as exc:
            return {"ok": False, "reason": "udid_name_mismatch", "error": str(exc), "sim_name": sim_name}
        if not target:
            # 列举成功且确实找不到 → 实例本来就不在，幂等成功
            runtime = self._runtimes.get(vm_id)
            if runtime is not None:
                stopped = self._stop_sync_locked(vm_id, runtime.udid)
                if not stopped.get("ok"):
                    return {**stopped, "sim_name": sim_name}
            try:
                for absent_udid in {udid, runtime.udid if runtime else ""} - {""}:
                    drop_port_reservation(absent_udid)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": "port_release_failed", "error": str(exc),
                        "sim_name": sim_name}
            return {"ok": True, "reason": "not_found", "error": "", "sim_name": sim_name,
                    "details": {"cleanup_confirmed": True}}
        if stop_first:
            stopped = self._stop_sync_locked(vm_id, target)
            if not stopped.get("ok"):
                return {**stopped, "sim_name": sim_name}
        elif str(instances[target].get("state") or "").lower() != "shutdown":
            return {"ok": False, "reason": "stop_unconfirmed", "error": "删除前实例不再处于 Shutdown，未重试停止",
                    "sim_name": sim_name}
        ok = self._delete_instance(target)
        return {
            "ok": ok,
            "reason": "deleted" if ok else "delete_failed",
            "error": "" if ok else "simctl delete failed",
            "sim_name": sim_name,
            "details": {"cleanup_confirmed": ok},
        }

    def _delete_instance(self, udid: str) -> bool:
        # 调用方已按 VM ID 验证所有权并确认停止。删除后必须确认实例确实消失，
        # 才能归还端口；未知错误文本不能作为释放依据。
        try:
            simctl_run("delete", udid, timeout=120.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除虚拟机失败 udid={}：{}", udid, exc)
        try:
            if udid in self._list_all_instances_strict():
                return False
        except SimctlListFailed as exc:
            logger.warning("无法确认虚拟机删除结果 udid={}：{}", udid, exc)
            return False
        unmark_managed(udid)
        try:
            drop_port_reservation(udid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("实例已删除但端口预留未释放 udid={}：{}", udid, exc)
            return False
        return True

    def _forget(self, vm_id: str, udid: str) -> None:
        """确认停止后摘掉运行登记；仅释放端口缓存，保留磁盘预留。"""
        self._runtimes.pop(vm_id, None)
        self._last_reclaimed_ids.discard(vm_id)
        if udid:
            unmark_managed(udid)
            release_ports(udid)
            # 端点也要摘：已停止实例不能继续贡献 readiness；持久端口只在删除后回池。
            _unregister_wda_endpoint(udid)
            # driver 缓存必须一起摘。只摘端点不摘 driver 会留下一个更隐蔽的坑：
            # 重启后进工作台，_get_or_open_driver 命中缓存直接返回，
            # open_ios_simulator_driver 不会被调到，端点永远补不回来，镜像必挂。
            if self._drop_driver_cache is not None:
                try:
                    self._drop_driver_cache(udid)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("摘除 driver 缓存失败 udid={}（忽略）：{}", udid, exc)

    # ------------------------------------------------------------------
    # 重连认领 / 对账（对齐 Android）
    # ------------------------------------------------------------------
    def reconcile_running_vms_sync(self) -> List[SimVmRuntime]:
        """扫本机虚拟机，把仍在运行的受管实例认领回来。

        身份锚点是名字 ``aiphone_sim_<vmid>``（持久落盘，Agent 重启后还在）。
        WDA 是虚拟机内部的进程，只要虚拟机没关机它通常还活着——因此认领时用一次
        轻量 HTTP 探活决定 ready，而不是重新走完整启动链，保证 pre-hello 阶段够快。
        """
        tools, missing = find_ios_sim_tools()
        if tools is None:
            logger.debug("iOS 虚拟机工具链不可用，跳过认领：{}", missing)
            return []

        adopted: List[SimVmRuntime] = []
        seen: set[str] = set()
        generations = dict(self._vm_generations)
        instances = self._list_all_instances_strict()
        for udid, meta in instances.items():
            if str(meta.get("state") or "").lower() != "booted":
                continue
            vm_id = vmid_from_sim_name(str(meta.get("name") or ""))
            if not vm_id:
                # 用户自己在 Xcode 里开的实例，不属于本平台，一概不碰
                continue
            lock = self._vm_lock(vm_id)
            # 正在启动/停止的实例由该操作持有，重连不得抢走 launcher 或提前报就绪。
            if not lock.acquire(blocking=False):
                continue
            try:
                if self._vm_generations.get(vm_id, 0) != generations.get(vm_id, 0):
                    continue
                try:
                    self._owned_target(vm_id, instances, udid)
                except InstanceIdentityError as exc:
                    logger.error("拒绝认领身份冲突的 iOS 虚拟机：{}", exc)
                    continue
                rt = self._runtimes.get(vm_id)
                if rt is not None:
                    # 同一 Agent 仅重连时沿用原 launcher（iPad 持有 xcodebuild 句柄）。
                    if rt.udid == udid:
                        rt.ready = bool(
                            not rt.details.get("cleanup_pending") and rt.launcher is not None
                            and rt.launcher.is_alive(expect_sim_name=rt.sim_name)
                        )
                        rt.details.update({"wda_alive": rt.ready, "guest_state": "Booted"})
                        if rt.ready:
                            mark_managed(udid)
                            _register_wda_endpoint(udid, rt.wda_port, rt.mjpeg_port)
                        else:
                            unmark_managed(udid)
                            _unregister_wda_endpoint(udid)
                        seen.add(vm_id)
                        adopted.append(rt)
                    continue
                ports = allocate_ports(udid)
                adopted_name = str(meta.get("name") or "")
                launcher = IosSimulatorWdaLauncher(
                    udid, ports=ports, expect_sim_name=adopted_name,
                    device_type=str(meta.get("device_type") or ""),
                )
                wda_alive = launcher.is_alive(expect_sim_name=adopted_name)
                rt = SimVmRuntime(
                    vm_id=vm_id, name=vm_id, udid=udid, sim_name=adopted_name,
                    device_type=str(meta.get("device_type") or ""),
                    runtime_id=str(meta.get("runtime") or ""), started_at=time.time(),
                    wda_port=ports.wda, mjpeg_port=ports.mjpeg,
                    launcher=launcher, ready=wda_alive,
                    details={"reclaimed": True, "sim_name": adopted_name,
                             "wda_port": ports.wda, "wda_alive": wda_alive, "guest_state": "Booted"},
                )
                seen.add(vm_id)
                self._runtimes[vm_id] = rt
                if wda_alive:
                    mark_managed(udid)
                    _register_wda_endpoint(udid, ports.wda, ports.mjpeg)
                else:
                    unmark_managed(udid)
                    logger.info("受管虚拟机 {} 仍在运行但 WDA 未就绪", rt.sim_name)
                adopted.append(rt)
            finally:
                lock.release()

        self._last_reclaimed_ids = set(seen)
        if adopted:
            logger.info("已认领 {} 台仍在运行的受管 iOS 虚拟机", len(adopted))
        return adopted

    async def report_reclaimed_vms(self, client: AgentWSClient, *, rescan: bool = True) -> int:
        if rescan:
            runtimes = await asyncio.to_thread(self.reconcile_running_vms_sync)
        else:
            runtimes = [
                self._runtimes[vm_id]
                for vm_id in sorted(self._last_reclaimed_ids)
                if vm_id in self._runtimes
            ]
        for rt in runtimes:
            # Booted + WDA 不可用是自动化故障，不是关机；使用既有 error 状态，
            # 可显式重试启动 WDA 或停止，不制造一个无人推进的 starting。
            await client.send(
                {
                    "type": P.MSG_IOS_SIM_VM_STATUS,
                    "vm_id": rt.vm_id,
                    "state": "running" if rt.ready else "error",
                    "ok": rt.ready,
                    "reason": "reclaimed" if rt.ready else "wda_not_ready",
                    "error": "" if rt.ready else "虚拟机仍在运行，但 WDA 尚未就绪",
                    "udid": rt.udid,
                    "details": dict(rt.details),
                    **({"observed_request_id": rt.start_request_id} if rt.start_request_id else {}),
                }
            )
        if runtimes:
            await self._refresh_devices_safe(client)
        return len(runtimes)

    def list_managed_vmids(self) -> List[str]:
        """本机全部受管实例的 vm_id（含未启动的）。用于孤儿对账。"""
        out: set[str] = set()
        for meta in self._list_all_instances_strict().values():
            vm_id = vmid_from_sim_name(str(meta.get("name") or ""))
            if vm_id:
                out.add(vm_id)
        out.update(self._runtimes.keys())
        return sorted(out)

    async def report_orphan_reconcile(self, client: AgentWSClient) -> int:
        """(重)连后上报严格实例清单，由 Server 按总账核对归属并清理孤儿。

        即使一台都没有也要发空清单——让 Server 对「归本 Agent 但已不在本机」的实例
        做差集收敛（置 agent_offline），否则 Server 重启后会滞留假 running。
        """
        try:
            inventory = await asyncio.to_thread(self._reconcile_inventory_sync)
        except SimctlListFailed as exc:
            # 空清单会让 Server 做差集收敛，失败时不可伪造「本机没有实例」。
            logger.warning("iOS 对账清单查询失败，本轮不上报：{}", exc)
            return 0
        vm_ids = inventory["vm_ids"]
        await client.send(
            {
                "type": P.MSG_IOS_SIM_VM_RECONCILE,
                "agent_id": client.agent_id,
                **inventory,
            }
        )
        logger.info(
            "已上报 iOS 虚拟机对账清单：在跑 {} / 没跑 {}（共 {}）",
            len(inventory["running_vm_ids"]), len(inventory["stopped_vm_ids"]), len(vm_ids),
        )
        return len(vm_ids)

    def _reconcile_inventory_sync(self) -> Dict[str, Any]:
        generations = dict(self._vm_generations)
        instances = self._list_all_instances_strict()
        vm_ids: set[str] = set()
        running: set[str] = set()
        stopped: set[str] = set()
        unknown: set[str] = set()
        proofs: Dict[str, Dict[str, str]] = {}
        for udid, meta in instances.items():
            vm_id = vmid_from_sim_name(str(meta.get("name") or ""))
            if not vm_id:
                continue
            if vm_id in vm_ids:
                unknown.add(vm_id)  # 同名重复实例，不能随便挑一台解释事实
            vm_ids.add(vm_id)
            if str(meta.get("name") or "") == managed_sim_name(vm_id):
                proofs[vm_id] = {"udid": udid, "name": str(meta["name"]),
                                 "state": str(meta.get("state") or "")}
            state = str(meta.get("state") or "").lower()
            if state == "booted":
                running.add(vm_id)
            elif state == "shutdown":
                stopped.add(vm_id)
            else:
                unknown.add(vm_id)
        for vm_id in set(self._runtimes) | set(self._vm_generations) | vm_ids:
            lock = self._vm_lock(vm_id)
            if not lock.acquire(blocking=False):
                unknown.add(vm_id)
                continue
            try:
                if (self._vm_generations.get(vm_id, 0) != generations.get(vm_id, 0)
                        or (vm_id in self._runtimes and vm_id not in vm_ids)):
                    unknown.add(vm_id)
            finally:
                lock.release()
        vm_ids.update(unknown)
        return {
            "vm_ids": sorted(vm_ids), "running_vm_ids": sorted(running - unknown),
            "stopped_vm_ids": sorted(stopped - unknown), "unknown_vm_ids": sorted(unknown),
            "inventory_complete": True,
            "instance_proofs": {vm_id: proof for vm_id, proof in proofs.items() if vm_id not in unknown},
        }

    async def sweep_vanished_vms(self, client: AgentWSClient, present_serials: set) -> int:
        """缺席只触发核实；只有严格清单确认 Shutdown/不存在才报告停止。"""
        if not self._runtimes:
            return 0
        payloads = await asyncio.to_thread(self._sweep_vanished_sync, present_serials)
        for payload in payloads:
            await client.send(payload)
        return len(payloads)

    def _sweep_vanished_sync(self, present_serials: set) -> List[Dict[str, Any]]:
        payloads: List[Dict[str, Any]] = []
        candidates = []
        for vm_id, rt in list(self._runtimes.items()):
            lock = self._vm_lock(vm_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                if self._runtimes.get(vm_id) is not rt or not rt.udid:
                    continue
                if rt.udid in present_serials:
                    rt.missing_ticks = 0
                    if rt.details.pop("discovery_uncertain", False):
                        rt.details.pop("discovery_reason", None)
                        if rt.ready:
                            payloads.append({**self._status_payload(
                                {"vm_id": vm_id}, state="running", ok=True,
                                reason="discovery_recovered", udid=rt.udid, details=dict(rt.details),
                            ), **({"observed_request_id": rt.start_request_id} if rt.start_request_id else {})})
                    continue
                rt.missing_ticks += 1
                if rt.missing_ticks >= 2:
                    candidates.append((vm_id, rt, self._vm_generations.get(vm_id, 0), rt.start_request_id))
            finally:
                lock.release()
        if not candidates:
            return payloads
        # 一轮异常最多补查一次，不为每个设备重复扫描；不占用 asyncio 事件循环。
        list_error = ""
        try:
            instances = self._list_all_instances_strict()
        except SimctlListFailed as exc:
            instances = {}
            list_error = str(exc)
        for vm_id, rt, generation, observed_request_id in candidates:
            lock = self._vm_lock(vm_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                if (self._runtimes.get(vm_id) is not rt
                        or self._vm_generations.get(vm_id, 0) != generation):
                    continue
                error = list_error
                reason = "instance_list_failed" if error else "device_not_discovered"
                meta = instances.get(rt.udid)
                if not error:
                    try:
                        target = self._owned_target(vm_id, instances, rt.udid)
                        if target and target != rt.udid:
                            raise InstanceIdentityError("受管实例的 UDID 已变化，未自动接管")
                    except InstanceIdentityError as exc:
                        error, reason = str(exc), "udid_name_mismatch"
                        rt.ready = False
                        unmark_managed(rt.udid)
                        _unregister_wda_endpoint(rt.udid)
                    if not error and (meta is None or str(meta.get("state") or "").lower() == "shutdown"):
                        if rt.details.get("cleanup_pending"):
                            error = str(rt.details.get("cleanup_error") or "上次清理失败，等待显式操作")
                            reason = "cleanup_pending"
                        elif rt.launcher is not None:
                            try:
                                rt.launcher.stop()
                            except Exception as exc:  # noqa: BLE001
                                error, reason = str(exc), "wda_stop_failed"
                        if not error:
                            self._forget(vm_id, rt.udid)
                            payloads.append({**self._status_payload(
                                {"vm_id": vm_id}, state="stopped", ok=True, reason="vanished",
                                udid=rt.udid, details={"sim_name": rt.sim_name},
                            ), **({"observed_request_id": observed_request_id} if observed_request_id else {})})
                            continue
                previous = rt.details.get("discovery_reason")
                rt.details.update({"discovery_uncertain": True, "discovery_reason": reason})
                # 保留纳管登记，下一轮发现成功后才有机会回到设备池；不因暂时缺席
                # 关闭 guest、不释放端口、不丢掉后续停止/删除需要的所有权。
                if previous != reason:
                    payloads.append({**self._status_payload(
                        {"vm_id": vm_id}, state="error", ok=False, reason=reason,
                        error=error or "虚拟机未出现在设备扫描中，尚未确认停止", udid=rt.udid,
                        details={**rt.details, "guest_state": meta.get("state", "") if meta else "unknown"},
                    ), **({"observed_request_id": observed_request_id} if observed_request_id else {})})
            finally:
                lock.release()
        return payloads

    # ------------------------------------------------------------------
    @staticmethod
    def _status_payload(
        msg: Dict[str, Any],
        *,
        state: str,
        ok: bool,
        reason: str,
        error: str = "",
        udid: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "type": P.MSG_IOS_SIM_VM_STATUS,
            "request_id": msg.get("request_id") or "",
            "vm_id": msg.get("vm_id") or "",
            "state": state,
            "ok": ok,
            "reason": reason,
            "error": error,
            "udid": udid,
            "details": details or {},
        }
