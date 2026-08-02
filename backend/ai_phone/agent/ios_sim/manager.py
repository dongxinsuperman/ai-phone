"""Agent 侧 iOS 虚拟机生命周期管理。

对标 :class:`ai_phone.agent.android_vm.manager.AndroidVmManager`——方法名、锁模型、
状态上报、重连认领、消失巡检全部对齐（方案 §0.3：一等对照物是 Android 虚拟机）。

身份锚点：**虚拟机名字** ``aiphone_sim_<vm_id>``，与 Android 用 AVD 名
``aiphone_vm_<vm_id>`` 同构。名字持久落在宿主磁盘，Agent 重启后据此反解 vm_id
认领回来。UDID 由 ``simctl create`` 生成，创建后固定不变，作为设备 serial 上报。

与 Android / 鸿蒙的三处实质差异（方案 §6.5.5）：

1. **不需要 Server 端口租约。** 虚拟机 serial 是 UDID、天然全局唯一，WDA 端口纯
   本机事务（``ios_simulator_wda.allocate_ports``），两台 Mac 各用 8300 互不影响。
2. **不需要「改配置 → 删了重建」这条产品路径。** 机型与系统版本创建后即由官方目录
   锁定、不可修改（Server 侧照抄鸿蒙 ``CATALOG_LOCKED_FIELDS``），要换配置就新建
   一台。本文件里仍保留「检测到不一致就删除重建」的兜底，但那只防御异常情形
   （手工改过实例、目录快照升级导致 identifier 变化等），不是正常业务路径。
3. **就绪判据是 boot + WDA**。``simctl bootstatus`` 只证明系统起来了；能不能截图
   点击要看 WDA。对齐鸿蒙「HDC Connected 之后还要等 hmdriver2 握手」的做法，
   不把「开机完成」当「设备可用」。
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
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
    """``vm_id`` → 受管虚拟机名。vm_id 恒为短 hex，截断不会丢信息。"""
    suffix = re.sub(r"[^A-Za-z0-9_]+", "_", vm_id or "")[:24]
    return f"{_MANAGED_NAME_PREFIX}{suffix}"


def vmid_from_sim_name(name: str) -> str:
    """从受管虚拟机名反解 vm_id；非受管返回空串。与 :func:`managed_sim_name` 互逆。"""
    n = (name or "").strip()
    if n.startswith(_MANAGED_NAME_PREFIX):
        return n[len(_MANAGED_NAME_PREFIX):]
    return ""


class SimctlListFailed(RuntimeError):
    """``simctl list`` 本身失败了——**不等于**「实例不存在」。

    区分这两者对删除路径是必须的：把「没问到」当成「没有」，会让删除返回成功
    而实例还在。
    """


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
    # 运行期巡检：ready=已确认开机且 WDA 就绪（未就绪不判"消失"，避免启动中被误清）；
    # missing_ticks=连续在设备快照里缺席的轮数（连续 2 轮才判消失，防单次抖动）。
    ready: bool = False
    missing_ticks: int = 0
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
            rt.udid: rt for rt in self._runtimes.values() if rt.udid
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
        task = asyncio.create_task(
            self._start_and_report(client, msg),
            name=f"ios-sim-vm-start-{msg.get('vm_id')}",
        )
        task.add_done_callback(_log_task_error)

    async def handle_stop(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        await client.send(self._status_payload(msg, state="stopping", ok=True, reason="stopping"))
        result = await asyncio.to_thread(self.stop_sync, str(msg.get("vm_id") or ""))
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
                    "sim_name": str(result.get("sim_name") or ""),
                    "deleted": bool(result.get("ok")),
                },
            )
        )
        await self._refresh_devices_safe(client)

    async def _start_and_report(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        try:
            result = await asyncio.to_thread(self.start_sync, msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("iOS 虚拟机启动失败 vm_id={}: {}", msg.get("vm_id"), exc)
            await client.send(
                self._status_payload(
                    msg, state="error", ok=False, reason="start_failed", error=str(exc)
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
        vm_id = str(msg.get("vm_id") or "").strip()
        if not vm_id:
            raise ValueError("vm_id is required")
        with self._vm_lock(vm_id):
            return self._start_sync_locked(vm_id, msg)

    def _start_sync_locked(self, vm_id: str, msg: Dict[str, Any]) -> Dict[str, Any]:
        existing = self._runtimes.get(vm_id)
        if existing is not None and existing.ready:
            return {"udid": existing.udid, "details": {"reused": True}}

        capability = self.probe(dict(msg))
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
            )
            ports = allocate_ports(udid)
            runtime = SimVmRuntime(
                vm_id=vm_id,
                name=alias,
                udid=udid,
                sim_name=sim_name,
                device_type=device_type,
                runtime_id=resolved_runtime,
                started_at=time.time(),
                wda_port=ports.wda,
                mjpeg_port=ports.mjpeg,
            )
            self._runtimes[vm_id] = runtime

        try:
            self._boot(udid)
            # 实例名由 manager 算出（aiphone_sim_<vm_id>），必然正确——直接传给
            # launcher 做身份校验，别让它回头去 simctl 查：查询会失败，而失败的
            # 时刻恰恰最需要这道校验。
            launcher = IosSimulatorWdaLauncher(udid, expect_sim_name=sim_name)
            # 就绪判据第二段：光开机不算可用，WDA 起来才算（见模块 docstring 第 3 点）
            launcher.start()
            runtime.launcher = launcher
            runtime.ready = True
            runtime.missing_ticks = 0
            # 纳管登记：只有登记过的 UDID 才会被 list_ios_simulators 上报进设备池
            mark_managed(udid)
            _register_wda_endpoint(udid, ports.wda, ports.mjpeg)
        except Exception:
            # 启动链任一步失败都要把占位清干净，绝不留下幽灵 running
            self._forget(vm_id, udid)
            raise

        details = {
            "sim_name": sim_name,
            "device_type": device_type,
            "runtime": resolved_runtime,
            "wda_port": ports.wda,
            "mjpeg_port": ports.mjpeg,
        }
        runtime.details = dict(details)
        logger.info(
            "iOS 虚拟机已就绪 vm_id={} udid={} name={} wda={} ",
            vm_id, udid, sim_name, ports.wda,
        )
        return {"udid": udid, "details": details}

    def _ensure_instance(self, *, sim_name: str, device_type: str, runtime_id: str) -> str:
        """确保存在一台名为 ``sim_name`` 的虚拟机，返回其 UDID。

        实例是**常驻**的（方案 §6.5.1）：同一个 vm_id 永远对应同一台虚拟机，已装的
        App 与数据跨启停留存。所以这里优先复用同名实例，而不是每次新建。

        机型或系统版本与记录不一致时**删除重建**——这两项是虚拟机唯一可配的东西，
        改任一项等价于换一台机器；重建成本实测仅 0.19 秒 / 17 MB。这与鸿蒙
        「DevEco 没有 update 命令，只能删了重建」的处理同构。
        """
        found = self._find_by_name(sim_name)
        if found is not None:
            udid, cur_type, cur_runtime = found
            if cur_type == device_type and cur_runtime == runtime_id:
                logger.info("复用已有受管虚拟机 name={} udid={}", sim_name, udid)
                return udid
            logger.info(
                "受管虚拟机 {} 配置已变（机型 {}→{}，系统 {}→{}），删除重建",
                sim_name, cur_type, device_type, cur_runtime, runtime_id,
            )
            self._delete_instance(udid)

        try:
            udid = simctl_run(
                "create", sim_name, device_type, runtime_id, timeout=120.0
            ).strip()
        except SimctlError as exc:
            raise RuntimeError(
                f"创建虚拟机失败 name={sim_name} device_type={device_type} "
                f"runtime={runtime_id}：{exc}"
            ) from exc
        if not udid:
            raise RuntimeError(f"创建虚拟机成功但未返回 UDID：name={sim_name}")
        logger.info("已创建受管虚拟机 name={} udid={}", sim_name, udid)
        return udid

    def _find_by_name(self, sim_name: str) -> Optional[tuple]:
        """按名字找已存在的实例，返回 ``(udid, device_type, runtime_id)``。"""
        for udid, meta in self._list_all_instances().items():
            if meta.get("name") == sim_name:
                return udid, meta.get("device_type", ""), meta.get("runtime", "")
        return None

    def _list_all_instances(self) -> Dict[str, Dict[str, Any]]:
        """``simctl list devices -j`` 全量（含未启动），返回 ``{udid: meta}``。

        不复用 ``drivers.simctl.list_simulators``：那个只面向「可用设备发现」，
        会滤掉 ``isAvailable=False`` 的实例；而生命周期管理必须看到全部，否则
        「runtime 被卸载导致实例不可用」这种情况会被误判成「实例不存在」而重复创建。
        """
        try:
            return self._list_all_instances_strict()
        except SimctlListFailed as exc:
            logger.warning("列举虚拟机实例失败：{}", exc)
            return {}

    def _list_all_instances_strict(self) -> Dict[str, Dict[str, Any]]:
        """同上，但**列举失败抛错**而不是返回空表。

        扫描类调用（发现、认领、查重）拿空表继续是合理的：最坏是这一轮少看见
        几台，下一轮补上。但**删除不行**——空表会被读成「这个 UDID 不存在」，
        于是回一个 ok=True/not_found，而实例其实还在。删除报告成功、东西还在，
        只能等后续孤儿对账兜底，那不是即时的、必然的。
        """
        try:
            raw = simctl_run("list", "devices", "-j", timeout=30.0)
            payload = json.loads(raw) if raw else {}
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

    def _boot(self, udid: str) -> None:
        """开机并等待完成。已经开着的视为成功（幂等）。"""
        try:
            simctl_run("boot", udid, timeout=120.0)
        except SimctlError as exc:
            text = f"{exc.stdout} {exc.stderr}".lower()
            # 已经 Booted 时 simctl 会报 "Unable to boot device in current state: Booted"
            if "current state: booted" in text or "already booted" in text:
                logger.debug("虚拟机 {} 已在运行，跳过 boot", udid)
            else:
                raise RuntimeError(f"虚拟机开机失败 udid={udid}：{exc}") from exc
        # bootstatus 是官方的开机完成判据（等价鸿蒙的 bootevent.boot.completed）
        try:
            simctl_run("bootstatus", udid, timeout=float(self.boot_timeout_sec))
        except SimctlError as exc:
            raise RuntimeError(
                f"等待虚拟机开机完成超时 udid={udid}（上限 {self.boot_timeout_sec}s）：{exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 停止 / 删除
    # ------------------------------------------------------------------
    def stop_sync(self, vm_id: str) -> Dict[str, Any]:
        with self._vm_lock(vm_id):
            return self._stop_sync_locked(vm_id)

    def _stop_sync_locked(self, vm_id: str) -> Dict[str, Any]:
        runtime = self._runtimes.get(vm_id)
        if runtime is None:
            return {"ok": True, "reason": "not_running", "udid": ""}
        udid = runtime.udid
        # 先停 WDA 再关机：反过来会让 launcher 的 terminate 打在一台已经没了的设备上
        if runtime.launcher is not None:
            try:
                runtime.launcher.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("停止 WDA 失败（忽略）udid={}：{}", udid, exc)
        try:
            simctl_run("shutdown", udid, timeout=60.0)
        except SimctlError as exc:
            text = f"{exc.stdout} {exc.stderr}".lower()
            if "current state: shutdown" in text or "unable to shutdown" in text:
                logger.debug("虚拟机 {} 已关机", udid)
            else:
                logger.warning("虚拟机关机失败 udid={}：{}", udid, exc)
        self._forget(vm_id, udid)
        return {"ok": True, "reason": "stopped", "udid": udid}

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
            return self._delete_sync_locked(vm_id, udid)

    def _delete_sync_locked(self, vm_id: str, udid: str) -> Dict[str, Any]:
        sim_name = managed_sim_name(vm_id)
        if vm_id in self._runtimes:
            self.stop_sync(vm_id)

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
        target = udid
        if target:
            meta = instances.get(target)
            if meta is None:
                # 这个 udid 已经不存在了：退回按名字找，处理「实例还在但
                # 记录里的 udid 过期了」的情况
                logger.debug("udid={} 已不存在，退回按名字 {} 查找", target, sim_name)
                target = ""
            elif meta.get("name") != sim_name:
                logger.error(
                    "拒绝删除：udid={} 的实例名是「{}」，不是本配置的「{}」——"
                    "疑似记录错乱，删错会永久抹掉别人的虚拟机和数据",
                    target, meta.get("name"), sim_name,
                )
                return {
                    "ok": False,
                    "reason": "udid_name_mismatch",
                    "error": (
                        f"udid={target} 的实例名是「{meta.get('name')}」，"
                        f"与本配置的「{sim_name}」不符，已拒绝删除"
                    ),
                    "sim_name": sim_name,
                }
        if not target:
            # 用上面那份严格清单按名字找，不再调 _find_by_name——它内部会重新
            # 列举一次（宽松版），失败时又退回空表，等于把刚堵上的盲区放回来。
            for cand_udid, meta in instances.items():
                if meta.get("name") == sim_name:
                    target = cand_udid
                    break
        if not target:
            # 列举成功且确实找不到 → 实例本来就不在，幂等成功
            return {"ok": True, "reason": "not_found", "error": "", "sim_name": sim_name}
        ok = self._delete_instance(target)
        return {
            "ok": ok,
            "reason": "deleted" if ok else "delete_failed",
            "error": "" if ok else "simctl delete failed",
            "sim_name": sim_name,
        }

    def _delete_instance(self, udid: str) -> bool:
        # 删除前先确保关机，否则 simctl 会拒绝
        try:
            simctl_run("shutdown", udid, timeout=60.0, check=False)
        except Exception:  # noqa: BLE001
            pass
        unmark_managed(udid)
        try:
            simctl_run("delete", udid, timeout=120.0)
        except SimctlError as exc:
            text = f"{exc.stdout} {exc.stderr}".lower()
            if "invalid device" in text or "not found" in text:
                # 已经没了，幂等成功——号可以还
                drop_port_reservation(udid)
                return True
            logger.warning("删除虚拟机失败 udid={}：{}", udid, exc)
            # **删除失败就不还号**。实例还在，它下次还要用同一个端口；提前还了
            # 等于把这台的稳定身份弄丢，下次启动可能拿到别人的号。
            return False
        # 删除确认成功后才归还预留。停止只解进程内绑定（见 _forget）——实例和
        # 数据都还在，下次启动还是这台设备，端口必须还是同一个。
        drop_port_reservation(udid)
        return True

    def _forget(self, vm_id: str, udid: str) -> None:
        """从进程内状态与纳管表里摘掉，并释放端口。"""
        self._runtimes.pop(vm_id, None)
        self._last_reclaimed_ids.discard(vm_id)
        if udid:
            unmark_managed(udid)
            release_ports(udid)
            # 端点也要摘：留着的话实例都停了，readiness 探针还会去连那个端口，
            # 而端口已经回池、可能被下一台实例拿走，探到的就是别人的 WDA。
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
        for udid, meta in self._list_all_instances().items():
            if str(meta.get("state") or "").lower() != "booted":
                continue
            vm_id = vmid_from_sim_name(str(meta.get("name") or ""))
            if not vm_id:
                # 用户自己在 Xcode 里开的实例，不属于本平台，一概不碰
                continue
            seen.add(vm_id)
            ports = allocate_ports(udid)
            adopted_name = str(meta.get("name") or "")
            launcher = IosSimulatorWdaLauncher(
                udid, ports=ports, expect_sim_name=adopted_name
            )
            # 必须带实例名校验：端口上应答的未必是这台设备的 WDA。认错的后果是
            # 两台设备互换身份（操作 A 实际驱动 B），而且界面上完全看不出来。
            wda_alive = launcher.is_alive(expect_sim_name=adopted_name)

            rt = self._runtimes.get(vm_id)
            if rt is None:
                rt = SimVmRuntime(
                    vm_id=vm_id,
                    name=vm_id,
                    udid=udid,
                    sim_name=str(meta.get("name") or ""),
                    device_type=str(meta.get("device_type") or ""),
                    runtime_id=str(meta.get("runtime") or ""),
                    started_at=time.time(),
                )
                self._runtimes[vm_id] = rt
            rt.udid = udid
            rt.wda_port = ports.wda
            rt.mjpeg_port = ports.mjpeg
            rt.launcher = launcher
            rt.ready = wda_alive
            rt.missing_ticks = 0
            rt.details = {
                "reclaimed": True,
                "sim_name": rt.sim_name,
                "wda_port": ports.wda,
                "wda_alive": wda_alive,
            }
            # 只有 WDA 也活着才算真正可用，才登记进设备池；否则留待显式 start 拉起
            if wda_alive:
                mark_managed(udid)
                # 与首次启动同理：不登记端点，readiness 探针就一直看不到这台，
                # Agent 重启后认领回来的实例会永远停在「未就绪」。
                _register_wda_endpoint(udid, ports.wda, ports.mjpeg)
            else:
                unmark_managed(udid)
                logger.info(
                    "受管虚拟机 {} 仍在运行但 WDA 未就绪，暂不进设备池（等一次 start）",
                    rt.sim_name,
                )
            adopted.append(rt)

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
            # WDA 没活着就报 stopped，**不能报 starting**——没有任何东西正在启动它，
            # 而 starting 在前端不显示「启动」按钮（只显示「停止」），用户会卡在
            # 永远的「启动中」里出不来。报 stopped 才给出正确的下一步动作。
            #
            # 这条对 iPad 尤其要紧：它走 xcodebuild，Agent 一重启 WDA 就跟着没了
            # （与 iOS 真机同族的固有性质），于是「实例还在、WDA 已死」从 iPhone 上
            # 的罕见边界变成了 iPad 上的常态。
            await client.send(
                {
                    "type": P.MSG_IOS_SIM_VM_STATUS,
                    "vm_id": rt.vm_id,
                    "state": "running" if rt.ready else "stopped",
                    "ok": True,
                    "reason": "reclaimed",
                    "udid": rt.udid,
                    "details": dict(rt.details),
                }
            )
        if runtimes:
            await self._refresh_devices_safe(client)
        return len(runtimes)

    def list_managed_vmids(self) -> List[str]:
        """本机全部受管实例的 vm_id（含未启动的）。用于孤儿对账。"""
        out: set[str] = set()
        for meta in self._list_all_instances().values():
            vm_id = vmid_from_sim_name(str(meta.get("name") or ""))
            if vm_id:
                out.add(vm_id)
        out.update(self._runtimes.keys())
        return sorted(out)

    async def report_orphan_reconcile(self, client: AgentWSClient) -> int:
        """(重)连后上报本机受管实例清单，区分在跑/没跑，由 Server 认领归属并清孤儿。

        即使一台都没有也要发空清单——让 Server 对「归本 Agent 但已不在本机」的实例
        做差集收敛（置 agent_offline），否则 Server 重启后会滞留假 running。
        """
        vm_ids = await asyncio.to_thread(self.list_managed_vmids)
        running = sorted(set(self._last_reclaimed_ids) & set(vm_ids))
        stopped = sorted(set(vm_ids) - set(running))
        await client.send(
            {
                "type": P.MSG_IOS_SIM_VM_RECONCILE,
                "agent_id": client.agent_id,
                "vm_ids": vm_ids,
                "running_vm_ids": running,
                "stopped_vm_ids": stopped,
            }
        )
        logger.info(
            "已上报 iOS 虚拟机对账清单：在跑 {} / 没跑 {}（共 {}）",
            len(running), len(stopped), len(vm_ids),
        )
        return len(vm_ids)

    async def sweep_vanished_vms(self, client: AgentWSClient, present_serials: set) -> int:
        """运行中存活巡检（蹭 rescan 节拍，零额外扫描）。

        本机以为在跑、但 UDID 已从设备快照消失的实例（被外部关机 / 被删）→ 上报
        stopped 并清理。语义与 Android ``sweep_vanished_vms`` 完全一致，包括
        「连续缺席 2 轮才判消失」这条防抖规则。
        """
        reported = 0
        for vm_id, rt in list(self._runtimes.items()):
            # 只巡检已就绪的：启动中的 UDID 还没进设备快照，绝不能判消失
            if not rt.ready or not rt.udid:
                continue
            if rt.udid in present_serials:
                rt.missing_ticks = 0
                continue
            rt.missing_ticks += 1
            if rt.missing_ticks < 2:
                logger.debug(
                    "iOS 虚拟机 {} ({}) 本轮缺席（第 {} 次），暂不判消失",
                    vm_id, rt.udid, rt.missing_ticks,
                )
                continue
            udid = rt.udid
            self._forget(vm_id, udid)
            await client.send(
                {
                    "type": P.MSG_IOS_SIM_VM_STATUS,
                    "vm_id": vm_id,
                    "state": "stopped",
                    "ok": True,
                    "reason": "vanished",
                    "udid": "",
                    "details": {"sim_name": rt.sim_name},
                }
            )
            logger.info("iOS 虚拟机 {} ({}) 连续缺席，已消失，上报 stopped", vm_id, udid)
            reported += 1
        return reported

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


def _log_task_error(task: "asyncio.Task") -> None:
    try:
        exc = task.exception()
    except Exception:  # noqa: BLE001
        return
    if exc is not None:
        logger.error("iOS 虚拟机后台任务异常 name={}：{}", task.get_name(), exc)
