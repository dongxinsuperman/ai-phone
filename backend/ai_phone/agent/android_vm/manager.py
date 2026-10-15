"""Agent-side Android VM manager."""
from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from loguru import logger

from ai_phone.agent.drivers.base import DeviceInfo
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.config import get_settings
from ai_phone.shared import protocol as P

from .capability import (
    AndroidVmTools,
    default_system_image,
    find_android_tools,
    list_installed_system_images,
    normalize_abi,
    probe_android_vm_capability,
)


@dataclass
class VmRuntime:
    vm_id: str
    name: str
    adb_serial: str
    port: int
    process: Optional[subprocess.Popen]
    started_at: float
    log_file: Optional[Any] = None
    avd_name: str = ""
    # 缺席只触发本机进程核验，不能作为 stopped 或释放资源的证据。
    ready: bool = False
    missing_ticks: int = 0
    transport_unavailable: bool = False
    observed_request_id: str = ""
    operation_deadline: Optional[float] = None
    exit_evidence: Optional[Dict[str, Any]] = None


class VmStartFailure(RuntimeError):
    """保留跨线程的清理结论；asyncio 会重建 TimeoutError 并丢掉其自定义属性。"""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cleanup_confirmed = bool(getattr(cause, "cleanup_confirmed", False))
        self.cleanup_error = str(getattr(cause, "cleanup_error", ""))
        self.created_in_attempt = getattr(cause, "created_in_attempt", None)
        self.instance_preexisting = bool(getattr(cause, "instance_preexisting", False))


class AndroidVmManager:
    """Owns Android Emulator runtimes for one Agent process."""

    def __init__(self, *, runtime_dir: Optional[Path] = None, max_instances: Optional[int] = None):
        # 这些旋钮已收归 Settings 的「下发集」，由 Server 集中下发控制。下发在 WS 连接后
        # 才到达（set_runtime_override 覆盖），所以这里**不在 __init__ 固化**，而用 @property
        # 每次读 get_settings()，保证 Server 下发后立即生效。
        settings = get_settings()
        self.runtime_dir = runtime_dir or (Path(settings.storage_dir) / "vm_runtime")
        self._max_instances_override = max_instances  # 构造参数显式覆盖（测试/特殊用途）
        self._runtimes: Dict[str, VmRuntime] = {}
        self._last_reclaimed_ids: set[str] = set()
        self._running_inventory_complete = False
        self._avd_inventory_proofs: Dict[str, Dict[str, str]] = {}
        # 只有成功创建/复用 AVD 或实际枚举到 AVD 才记入；运行占位和推算名称不是创建证据。
        self._created_vm_ids: set[str] = set()
        # 两层锁：
        # ① _start_lock（全局）：保护"选端口 + 占位注册"这段临界区（不同 VM 间互斥，防抢端口）。
        # ② _vm_lock(vm_id)（按 VM）：串行化【同一台 VM】的 start/stop/delete，杜绝"边启动边停止"
        #    把占位 pop 掉、留下幽灵 running。用 RLock 允许同线程重入（start 失败时内部会调 stop_sync）。
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

    @property
    def max_instances(self) -> int:
        # 沿用现有容量软提示；实际指令并发由 WS 队列限制。
        return self._max_instances_override or get_settings().android_vm_max_instances

    @property
    def no_window(self) -> bool:
        return get_settings().android_vm_no_window

    @property
    def boot_timeout_sec(self) -> int:
        return get_settings().android_vm_boot_timeout_sec

    @property
    def density(self) -> int:
        return get_settings().android_vm_density

    @property
    def kill_foreign(self) -> bool:
        return get_settings().android_vm_kill_foreign

    @property
    def orphan_cleanup(self) -> bool:
        return get_settings().android_vm_orphan_cleanup

    def decorate_devices(self, infos: List[DeviceInfo]) -> List[DeviceInfo]:
        serial_to_runtime = {rt.adb_serial: rt for rt in list(self._runtimes.values()) if _emulator_port(rt.adb_serial)}
        out: List[DeviceInfo] = []
        for info in infos:
            rt = serial_to_runtime.get(info.serial)
            if rt is not None:
                extra = dict(info.extra or {})
                extra.update({
                    "device_kind": "virtual",
                    "is_virtual": True,
                    "vm_instance_id": rt.vm_id,
                    "vm_name": rt.name,
                    "vm_transport_unavailable": rt.transport_unavailable,
                })
                info.extra = extra
            elif info.serial.startswith("emulator-"):
                continue
            out.append(info)
        return out

    async def handle_capability_probe(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        result = await asyncio.to_thread(
            self.probe,
            msg,
        )
        await client.send({
            "type": P.MSG_VM_CAPABILITY,
            "request_id": msg.get("request_id") or "",
            "agent_id": client.agent_id,
            **result,
        })

    async def handle_start(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        await client.send(self._status_payload(msg, state="starting", ok=True, reason="starting"))
        # WS 层按 VM 排队；必须等本次完成，不能另起 task 绕过顺序与并发上限。
        await self._start_and_report(client, msg)

    async def handle_stop(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        await client.send(self._status_payload(msg, state="stopping", ok=True, reason="stopping"))
        result = await asyncio.to_thread(
            self.stop_sync, str(msg.get("vm_id") or ""), str(msg.get("adb_serial") or ""),
            str(msg.get("request_id") or ""),
        )
        await client.send(self._status_payload(
            msg,
            state="stopped" if result.get("ok") else "error",
            ok=bool(result.get("ok")),
            reason=str(result.get("reason") or ""),
            error=str(result.get("error") or ""),
            adb_serial=str(result.get("adb_serial") or ""),
            details=dict(result.get("details") or {}),
        ))
        await self._refresh_devices_safe(client)

    async def handle_delete(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        """删除某 VM 的远端 AVD（Server 删除配置 / 换绑到新 Agent 时下发）。

        先停 emulator 再删 AVD；AVD 名按 vm_id 独占（``aiphone_vm_<vmid>``），只命中
        这一台，不影响其它 VM / 真机链路；只有资源清理确认完成后才回报 deleted。
        """
        result = await asyncio.to_thread(
            self.delete_sync,
            str(msg.get("vm_id") or ""),
            str(msg.get("adb_serial") or ""),
        )
        await client.send(self._status_payload(
            msg,
            state="stopped" if result.get("ok") else "error",
            ok=bool(result.get("ok")),
            reason=str(result.get("reason") or ""),
            error=str(result.get("error") or ""),
            details={
                **dict(result.get("details") or {}),
                "avd_name": str(result.get("avd_name") or ""),
                "deleted_avd": bool(result.get("ok")),
                "cleanup_confirmed": bool(result.get("ok")),
            },
        ))
        await self._refresh_devices_safe(client)

    def probe(self, requirement: Dict[str, Any]) -> Dict[str, Any]:
        return probe_android_vm_capability(
            requirement,
            current_instances=len(self._runtimes),
            max_instances=self.max_instances,
        )

    async def _start_and_report(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        try:
            # 单调时钟只属于本 Agent：覆盖消息里可能携带的同名值，再在入线程池前计时。
            operation_msg = {**msg, "_operation_deadline": None}
            operation_msg["_operation_deadline"] = _start_deadline(operation_msg)
            result = await asyncio.to_thread(self.start_sync, operation_msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Android VM 启动失败 vm_id={}: {}", msg.get("vm_id"), exc)
            error = str(exc)
            if getattr(exc, "cleanup_error", ""):
                error += f"；清理未完成：{exc.cleanup_error}"
            details = {
                "cleanup_confirmed": bool(getattr(exc, "cleanup_confirmed", False)),
                "instance_created": str(msg.get("vm_id") or "") in self._created_vm_ids,
            }
            if getattr(exc, "created_in_attempt", None) is not None:
                details["created_in_attempt"] = bool(exc.created_in_attempt)
            if getattr(exc, "instance_preexisting", False):
                details["instance_preexisting"] = True
            await client.send(self._status_payload(
                msg,
                state="error",
                ok=False,
                reason="start_failed",
                error=error,
                details=details,
            ))
            await self._refresh_devices_safe(client)
            return
        await client.send(self._status_payload(
            msg,
            state="running",
            ok=True,
            reason="running",
            adb_serial=str(result.get("adb_serial") or ""),
            details=dict(result.get("details") or {}),
        ))
        await self._refresh_devices_safe(client)

    async def _refresh_devices_safe(self, client: AgentWSClient) -> None:
        try:
            await client.refresh_devices()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Android VM 刷新设备快照失败（忽略）：{}", exc)

    def start_sync(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        vm_id = str(msg.get("vm_id") or "").strip()
        if not vm_id:
            raise ValueError("vm_id is required")
        # 按 vm_id 串行化：同一台 VM 的 start/stop/delete 排队执行，互不打架。
        # 重复 start 会被挡在前一个之后，前一个完成后这里 existing 已 ready → reused 上报 running 才正确。
        had_instance = vm_id in self._runtimes or vm_id in self._created_vm_ids
        try:
            deadline = _start_deadline(msg)
            operation_msg = {**msg, "_operation_deadline": deadline}
            with _startup_lock(self._vm_lock(vm_id), deadline):
                had_instance = vm_id in self._runtimes or vm_id in self._created_vm_ids
                return self._start_sync_locked(vm_id, operation_msg)
        except Exception as exc:
            first_bind = _msg_bool(msg, "rollback_new_instance", False) and not _msg_bool(
                msg, "require_existing_instance", False
            )
            if first_bind and not hasattr(exc, "created_in_attempt") and not getattr(exc, "creation_attempted", False):
                exc.created_in_attempt = False
            if (
                first_bind
                and not had_instance
                and vm_id not in self._runtimes
                and vm_id not in self._created_vm_ids
                and not hasattr(exc, "cleanup_confirmed")
            ):
                # 首次排队/探查即失败，尚未创建资源，不为确认空账而执行原生命令。
                exc.cleanup_confirmed = True
            raise VmStartFailure(exc) from exc

    def _start_sync_locked(self, vm_id: str, msg: Dict[str, Any]) -> Dict[str, Any]:
        operation_deadline = _start_deadline(msg)
        _startup_timeout(operation_deadline, 1)
        rollback_new = _msg_bool(msg, "rollback_new_instance", False) and not _msg_bool(
            msg, "require_existing_instance", False
        )
        existing = self._runtimes.get(vm_id)
        if existing is not None:
            if rollback_new:
                error = RuntimeError("首次绑定发现已有受管实例，未修改或启动")
                error.created_in_attempt = False
                error.instance_preexisting = True
                raise error
            if not existing.ready or existing.transport_unavailable:
                raise RuntimeError("已有实例尚未就绪或清理未完成，请先确认状态")
            tools, _missing = find_android_tools()
            if tools is None or self._emulator_avd_name(tools, existing.adb_serial) != (
                existing.avd_name or _safe_avd_name(vm_id)
            ):
                raise RuntimeError("无法确认已有实例身份，请先停止或恢复通道")
            existing.operation_deadline = operation_deadline
            self._wait_boot_completed(tools, existing.adb_serial)
            existing.observed_request_id = str(msg.get("request_id") or "")
            return {"adb_serial": existing.adb_serial, "details": {
                "reused": True, "instance_created": vm_id in self._created_vm_ids,
                "created_in_attempt": False,
            }}

        requirement = dict(msg)
        capability = self.probe(requirement)
        if not capability.get("ok"):
            raise RuntimeError(str(capability.get("reason") or "vm capability unavailable"))
        tools, missing = find_android_tools()
        if tools is None:
            raise RuntimeError(f"missing android tools: {', '.join(missing)}")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        abi = normalize_abi(str(msg.get("abi") or "auto"))
        width = _msg_int(msg, "screen_width", 1080)
        height = _msg_int(msg, "screen_height", 2400)
        density = _density_int(msg, "density", self.density)
        orientation = _orientation(str(msg.get("orientation") or ""), width, height)
        ram_mb = _optional_int(msg, "ram_mb", 0, 256, 65536)
        cpu_cores = _optional_int(msg, "cpu_cores", 0, 1, 16)
        vm_heap_mb = _optional_int(msg, "vm_heap_mb", 0, 16, 4096)
        internal_storage_mb = _optional_int(msg, "internal_storage_mb", 0, 512, 262144)
        sdcard_mb = _optional_int(msg, "sdcard_mb", 0, 0, 262144)
        gpu_mode = _choice(str(msg.get("gpu_mode") or "auto"), {"auto", "host", "swiftshader_indirect", "angle_indirect", "guest"}, "auto")
        network_speed = _choice(str(msg.get("network_speed") or "full"), {"full", "gsm", "hscsd", "gprs", "edge", "umts", "hsdpa", "lte", "evdo"}, "full")
        network_delay = _choice(str(msg.get("network_delay") or "none"), {"none", "gsm", "hscsd", "gprs", "edge", "umts", "lte", "evdo"}, "none")
        snapshot_policy = _choice(str(msg.get("snapshot_policy") or "discard_changes"), {"save", "discard_changes", "cold_boot", "no_snapshot"}, "discard_changes")
        system_type = str(msg.get("system_type") or "google_apis").strip() or "google_apis"
        system_image = str(msg.get("system_image") or "").strip() or default_system_image(
            int(msg.get("api_level") or 35),
            abi,
            system_type,
        )
        avd_name = _safe_avd_name(vm_id)
        # Server 全局分配的端口 + 全局已占端口（兜底避让），保证跨机器 serial 不撞。
        assigned_port = _opt_port(msg.get("assigned_port"))
        if msg.get("assigned_port") not in (None, "") and assigned_port is None:
            raise ValueError("assigned_port must be an even emulator port between 5554 and 5682")
        exclude_ports = {
            p for p in (_opt_port(x) for x in (msg.get("exclude_ports") or [])) if p is not None
        }
        # VM 锁已完成同实例去重；全局锁只保护选端口和占位注册。
        with _startup_lock(self._start_lock, operation_deadline):
            port = self._choose_port(prefer=assigned_port, exclude=exclude_ports)
            adb_serial = f"emulator-{port}"
            runtime = VmRuntime(
                vm_id=vm_id,
                name=str(msg.get("name") or vm_id),
                adb_serial=adb_serial,
                port=port,
                process=None,
                started_at=time.time(),
                avd_name=avd_name,
                ready=False,
                observed_request_id=str(msg.get("request_id") or ""),
                operation_deadline=operation_deadline,
            )
            self._runtimes[vm_id] = runtime
        # 占位之后失败先尝试回收；未确认回收时保留记录，不能把资源当成已经释放。
        created_here = False
        try:
            created_here = self._ensure_avd(
                tools,
                avd_name=avd_name,
                system_image=system_image,
                width=width,
                height=height,
                density=density,
                orientation=orientation,
                ram_mb=ram_mb,
                vm_heap_mb=vm_heap_mb,
                internal_storage_mb=internal_storage_mb,
                sdcard_mb=sdcard_mb,
                hardware=_hardware_config(msg),
                require_existing=bool(msg.get("require_existing_instance")),
                create_only=rollback_new,
            )
            self._created_vm_ids.add(vm_id)
            args = [
                tools.emulator,
                "-avd", avd_name,
                "-port", str(port),
                "-skin", f"{width}x{height}",
                "-prop", f"qemu.sf.lcd_density={density}",
                "-prop", f"debug.aiphone.vmid={vm_id}",
                "-prop", f"debug.aiphone.alias={str(msg.get('alias') or msg.get('name') or vm_id)}",
            ]
            # 时区：启动 -prop 即可生效（已真机验证）；locale 启动注入无效，走开机后预置。
            _tz = (get_settings().android_vm_timezone or "").strip()
            if _tz:
                args.extend(["-prop", f"persist.sys.timezone={_tz}"])
            if _msg_bool(msg, "no_audio", True):
                args.append("-no-audio")
            if _msg_bool(msg, "no_boot_anim", True):
                args.append("-no-boot-anim")
            if snapshot_policy == "discard_changes":
                args.append("-no-snapshot-save")
            elif snapshot_policy == "cold_boot":
                args.append("-no-snapshot-load")
            elif snapshot_policy == "no_snapshot":
                args.append("-no-snapshot")
            if _msg_bool(msg, "wipe_data", False):
                args.append("-wipe-data")
            if _msg_bool(msg, "writable_system", False):
                args.append("-writable-system")
            if _msg_bool(msg, "no_window", self.no_window):
                args.append("-no-window")
            if ram_mb:
                args.extend(["-memory", str(ram_mb)])
            if cpu_cores:
                args.extend(["-cores", str(cpu_cores)])
            if gpu_mode != "auto":
                args.extend(["-gpu", gpu_mode])
            if network_speed != "full":
                args.extend(["-netspeed", network_speed])
            if network_delay != "none":
                args.extend(["-netdelay", network_delay])
            dns_server = str(msg.get("dns_server") or "").strip()
            if dns_server:
                args.extend(["-dns-server", dns_server])
            http_proxy = str(msg.get("http_proxy") or "").strip()
            if http_proxy:
                args.extend(["-http-proxy", http_proxy])
            back_camera = _camera(str(msg.get("back_camera") or "emulated"))
            front_camera = _camera(str(msg.get("front_camera") or "none"))
            if back_camera:
                args.extend(["-camera-back", back_camera])
            if front_camera:
                args.extend(["-camera-front", front_camera])
            log_dir = self.runtime_dir / vm_id
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(log_dir / "emulator.log", "ab")  # noqa: SIM115
            runtime.log_file = log_file
            _startup_timeout(operation_deadline, 1)
            proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT)
            runtime.process = proc
            self._wait_boot_completed(tools, adb_serial)
            if self._emulator_avd_name(tools, adb_serial) != avd_name:
                raise RuntimeError("启动后设备身份不匹配，拒绝配置或上报其他实例")
            # 开机后预置：系统语言 / 时区 / 自动化友好项（best-effort，失败不阻断启动）。
            # 注意里面会 stop/start 重启 framework，那一下 adb 设备可能短暂抖动。
            self._provision_device(tools, adb_serial)
            _startup_timeout(operation_deadline, 1)
            if self._emulator_avd_name(tools, adb_serial) != avd_name:
                raise RuntimeError("开机预置后无法确认实例身份")
            # 预置全部做完、临返回前才标 ready——此后才纳入运行期"消失"巡检。
            # 放在这里（而非 _wait_boot_completed 之后）：避免 provision 期间的 framework 重启抖动
            # 被巡检盯上（虽有 missing_ticks 防抖，但放到全部就绪后语义更严谨、零误判窗口）。
            runtime.ready = True
            runtime.missing_ticks = 0
            return {
                "adb_serial": adb_serial,
                "details": {
                    "avd_name": avd_name,
                    "instance_created": True,
                    "created_in_attempt": bool(created_here),
                    "port": port,
                    "system_image": system_image,
                    "no_window": _msg_bool(msg, "no_window", self.no_window),
                    "screen_width": width,
                    "screen_height": height,
                    "density": density,
                    "orientation": orientation,
                    "ram_mb": ram_mb,
                    "cpu_cores": cpu_cores,
                    "vm_heap_mb": vm_heap_mb,
                    "internal_storage_mb": internal_storage_mb,
                    "sdcard_mb": sdcard_mb,
                    "gpu_mode": gpu_mode,
                    "network_speed": network_speed,
                    "network_delay": network_delay,
                    "snapshot_policy": snapshot_policy,
                },
            }
        except Exception as exc:
            # 首次绑定失败回收本次实例；已有绑定的重启失败只能停机，不能删除用户数据。
            created_here = bool(created_here or getattr(exc, "created_here", False))
            if rollback_new and created_here:
                exc.created_in_attempt = True
            try:
                if rollback_new and not created_here and runtime.process is None:
                    # 同名旧实例或创建结果未知，不按 Server 的首次绑定标志猜测本机数据归属。
                    self._forget_runtime(runtime)
                    cleanup = {
                        "ok": vm_id not in self._created_vm_ids and not getattr(exc, "creation_attempted", False),
                        "error": "未取得本次创建成功证据，保留现有文件等待对账",
                    }
                else:
                    cleanup = self.delete_sync(vm_id, adb_serial) if rollback_new and created_here else self.stop_sync(vm_id)
            except Exception as cleanup_exc:  # noqa: BLE001
                cleanup = {"ok": False, "error": str(cleanup_exc)}
            if not cleanup.get("ok"):
                logger.error("Android VM 启动失败后清理未完成 vm_id={}: {}", vm_id, cleanup)
                exc.cleanup_error = str(cleanup.get("error") or cleanup.get("reason") or "cleanup failed")
            exc.cleanup_confirmed = bool(cleanup.get("ok"))
            raise

    def stop_sync(self, vm_id: str, adb_serial: str = "", request_id: str = "") -> Dict[str, Any]:
        if not vm_id:
            return {"ok": False, "reason": "bad_request", "error": "vm_id required"}
        with self._vm_lock(vm_id):
            return self._stop_sync_locked(vm_id, adb_serial, request_id)

    def _stop_sync_locked(self, vm_id: str, adb_serial: str = "", request_id: str = "") -> Dict[str, Any]:
        runtime = self._runtimes.get(vm_id)
        was_tracked = runtime is not None
        avd_name = _safe_avd_name(vm_id)
        matches, _complete = _find_avd_processes(avd_name)
        if runtime is None:
            # Agent 重启或 ADB 失联后仍能按持久 AVD 身份收回实例，绝不按旧 serial 盲杀。
            port = next((port for _proc, port in matches if port), _emulator_port(adb_serial))
            runtime = VmRuntime(
                vm_id=vm_id, name=vm_id, adb_serial=f"emulator-{port}" if port else "",
                port=port, process=None, started_at=time.time(), avd_name=avd_name,
            )
        self._runtimes[vm_id] = runtime
        if request_id:
            runtime.observed_request_id = request_id
        runtime.transport_unavailable = True
        serials = {runtime.adb_serial} | {f"emulator-{port}" for _proc, port in matches if port}
        tools, _missing = find_android_tools()
        graceful = False
        if tools is not None:
            for serial in serials:
                if _emulator_port(serial) and self._emulator_avd_name(tools, serial) == avd_name:
                    graceful = self._kill_emulator(tools, serial) or graceful
        owned_processes = ([runtime.process] if runtime.process is not None else [])
        owned_processes.extend(proc for proc, _port in matches)
        stopped_pids: set[int] = set()
        for proc in owned_processes:
            pid = getattr(proc, "pid", id(proc))
            if pid in stopped_pids:
                continue
            stopped_pids.add(pid)
            try:
                _stop_owned_process(proc, graceful=graceful)
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止 Android VM 进程失败 vm_id={}: {}", vm_id, exc)
        deadline = time.monotonic() + _STOP_VERIFY_TIMEOUT_SEC
        while not self._runtime_exit_confirmed(runtime):
            if time.monotonic() >= deadline:
                return {
                    "ok": False, "reason": "stop_unconfirmed",
                    "error": "未确认实例进程退出及端口释放，保留管理记录", "adb_serial": runtime.adb_serial,
                    "details": {"avd_name": avd_name, "port": runtime.port,
                                "cleanup_pending": True, "cleanup_confirmed": False,
                                "instance_created": vm_id in self._created_vm_ids,
                                "exit_evidence": runtime.exit_evidence or {}},
                }
            time.sleep(0.1)
        self._forget_runtime(runtime)
        return {
            "ok": True, "reason": "stopped" if was_tracked or matches or graceful else "not_running",
            "adb_serial": runtime.adb_serial,
            "details": {"avd_name": avd_name, "port": runtime.port, "cleanup_confirmed": True,
                        "instance_created": vm_id in self._created_vm_ids,
                        "exit_evidence": runtime.exit_evidence or {}},
        }

    def _runtime_exit_confirmed(self, runtime: VmRuntime) -> bool:
        evidence: Dict[str, Any] = {"confirmed": False, "owned_process_exit_code": None}
        runtime.exit_evidence = evidence
        if runtime.process is not None:
            try:
                exit_code = runtime.process.poll()
                evidence["owned_process_exit_code"] = exit_code
                if exit_code is None:
                    evidence["reason"] = "owned_process_running"
                    return False
            except Exception as exc:  # noqa: BLE001
                evidence.update(reason="owned_process_unknown", error=str(exc))
                return False
        matches, complete = _find_avd_processes(runtime.avd_name or _safe_avd_name(runtime.vm_id))
        evidence.update(
            process_inventory_complete=complete,
            matching_process_ids=[getattr(proc, "pid", None) for proc, _port in matches],
        )
        if matches or not complete:
            evidence["reason"] = "matching_process_running" if matches else "process_inventory_incomplete"
            return False
        ports = (runtime.port, runtime.port + 1) if runtime.port else ()
        blocked_ports = [port for port in ports if not _is_port_free(port)]
        evidence.update(
            blocked_ports=blocked_ports,
            reason="port_unavailable" if blocked_ports else "exit_confirmed",
            confirmed=not blocked_ports,
        )
        return not blocked_ports

    def _forget_runtime(self, runtime: VmRuntime) -> None:
        if self._runtimes.get(runtime.vm_id) is not runtime:
            return
        self._runtimes.pop(runtime.vm_id, None)
        self._last_reclaimed_ids.discard(runtime.vm_id)
        if runtime.log_file is not None:
            try:
                runtime.log_file.close()
            except Exception:
                pass

    def stop_all(self) -> int:
        vm_ids = list(self._runtimes.keys())
        return sum(bool(self.stop_sync(vm_id).get("ok")) for vm_id in vm_ids)

    def delete_sync(self, vm_id: str, adb_serial: str = "") -> Dict[str, Any]:
        """先停 emulator，再删该 VM 独占的 AVD。AVD 不存在视为成功（幂等）。"""
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {"ok": False, "reason": "bad_request", "error": "vm_id required", "avd_name": ""}
        # 与 start/stop 同一把 per-VM 锁串行：删除不会和正在进行的启动/停止交叉
        with self._vm_lock(vm_id):
            return self._delete_sync_locked(vm_id, adb_serial)

    def _delete_sync_locked(self, vm_id: str, adb_serial: str) -> Dict[str, Any]:
        avd_name = _safe_avd_name(vm_id)
        # 1) 必须确认停妥；旧 serial 即使被别人复用也不能关闭另一台实例。
        stopped = self.stop_sync(vm_id, adb_serial)
        if not stopped.get("ok"):
            return {**stopped, "avd_name": avd_name}
        tools, _missing = find_android_tools()
        if tools is None:
            return {"ok": False, "reason": "tools_missing", "error": "android tools missing", "avd_name": avd_name,
                    "details": {"instance_created": vm_id in self._created_vm_ids, "cleanup_confirmed": False}}
        # 2) 删除 AVD 一次；失败返回本次结果，不在后台继续或重试。
        ok = self._delete_avd(tools, avd_name)
        if ok:
            self._created_vm_ids.discard(vm_id)
            self._avd_inventory_proofs.pop(vm_id, None)
        return {
            "ok": ok,
            "reason": "deleted" if ok else "delete_failed",
            "error": "" if ok else "avdmanager delete failed",
            "avd_name": avd_name,
            "details": {"cleanup_confirmed": ok, "instance_created": vm_id in self._created_vm_ids},
        }

    def _delete_avd(self, tools: AndroidVmTools, avd_name: str) -> bool:
        try:
            proc = subprocess.run(
                [tools.avdmanager, "delete", "avd", "-n", avd_name],
                check=False, capture_output=True, text=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("avdmanager delete 失败 avd={}: {}", avd_name, exc)
            return False
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            return self._remove_avd_dir(tools, avd_name)
        # AVD 本就不存在 → 幂等成功。
        if "no Android Virtual Device" in out or "not found" in out.lower():
            return self._remove_avd_dir(tools, avd_name)
        logger.error("avdmanager delete 失败 avd={}: {}", avd_name, out or "(无输出)")
        return False

    def _remove_avd_dir(self, tools: AndroidVmTools, avd_name: str) -> bool:
        """兜底清理 avdmanager 没删干净的残留目录 / .ini。"""
        cfg = _avd_config_path(tools, avd_name)
        if cfg is None:
            return False
        avd_dir = cfg.parent
        ini = avd_dir.parent / f"{avd_name}.ini"
        try:
            if avd_dir.exists():
                shutil.rmtree(avd_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理 AVD 目录失败 {}: {}", avd_dir, exc)
            return False  # 保留 .ini 索引，供下次明确清理/重连对账发现残留。
        try:
            if ini.exists():
                ini.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理 AVD .ini 失败 {}: {}", avd_name, exc)
        return not avd_dir.exists() and not ini.exists()

    def reconcile_running_vms_sync(self) -> List[VmRuntime]:
        self._running_inventory_complete = False
        self._last_reclaimed_ids.clear()
        tools, _missing = find_android_tools()
        if tools is None:
            return []
        try:
            proc = subprocess.run(
                [tools.adb, "devices"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except Exception:
            return []
        if proc.returncode != 0:
            return []
        self._running_inventory_complete = True
        adopted: List[VmRuntime] = []
        seen_vm_ids: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) < 2 or not _emulator_port(parts[0]):
                continue
            if parts[1] != "device":
                self._running_inventory_complete = False
                continue
            serial = parts[0]
            # 身份锚点改为 AVD 名（aiphone_vm_<vmid>，持久、重启还在）。
            # 不再读 debug.aiphone.vmid——Emulator 会丢弃这种自定义属性（实测 getprop 为空）。
            avd_name = self._emulator_avd_name(tools, serial)
            vm_id = _vmid_from_avd_name(avd_name)
            if not vm_id:
                # 不碰外部实例；身份查询失败也不能推导出受管实例已停止。
                if not avd_name:
                    self._running_inventory_complete = False
                continue
            if vm_id in seen_vm_ids:
                self._running_inventory_complete = False
                adopted = [rt for rt in adopted if rt.vm_id != vm_id]
                if vm_id in self._runtimes:
                    self._runtimes[vm_id].transport_unavailable = True
                    self._runtimes[vm_id].ready = False
                logger.error("同一 AVD 存在多个运行实例，拒绝认领 vm_id={}", vm_id)
                continue
            seen_vm_ids.add(vm_id)
            alias = self._getprop(tools, serial, "debug.aiphone.alias")  # best-effort，可能为空
            booted = self._getprop(tools, serial, "sys.boot_completed") == "1"
            lock = self._vm_lock(vm_id)
            if not lock.acquire(blocking=False):
                self._running_inventory_complete = False
                continue
            try:
                runtime = self._runtimes.get(vm_id)
                if runtime is None:
                    runtime = VmRuntime(
                        vm_id=vm_id, name=alias or vm_id, adb_serial=serial,
                        port=_emulator_port(serial), process=None, started_at=time.time(), avd_name=avd_name,
                    )
                    self._runtimes[vm_id] = runtime
                runtime.name = alias or runtime.name or vm_id
                runtime.adb_serial = serial
                runtime.port = _emulator_port(serial)
                runtime.avd_name = avd_name
                # 已认领的系统进程需要巡检，即使 guest 正在开机；未完成 boot 不报 running。
                runtime.ready = True
                runtime.transport_unavailable = not booted
                runtime.missing_ticks = 0
                if booted:
                    adopted.append(runtime)
            finally:
                lock.release()
        self._last_reclaimed_ids = {rt.vm_id for rt in adopted}
        return adopted

    async def report_reclaimed_vms(self, client: AgentWSClient, *, rescan: bool = True) -> int:
        if rescan:
            runtimes = await asyncio.to_thread(self.reconcile_running_vms_sync)
        else:
            runtimes = [
                self._runtimes[vm_id]
                for vm_id in sorted(self._last_reclaimed_ids)
                if vm_id in self._runtimes and self._runtimes[vm_id].ready
            ]
        for rt in runtimes:
            await client.send({
                "type": P.MSG_VM_STATUS,
                "vm_id": rt.vm_id,
                "state": "running",
                "ok": True,
                "reason": "reclaimed",
                "adb_serial": rt.adb_serial,
                "details": {
                    "reclaimed": True,
                    "avd_name": rt.avd_name,
                    "port": rt.port,
                    "instance_created": rt.vm_id in self._created_vm_ids,
                },
            })
        if runtimes:
            await self._refresh_devices_safe(client)
        return len(runtimes)

    async def sweep_vanished_vms(self, client: AgentWSClient, present_serials: set) -> int:
        """ADB 缺席只说明通道不可用；本机确认进程退出、端口释放后才上报停止。"""
        reported = 0
        for vm_id, rt in list(self._runtimes.items()):
            status = await asyncio.to_thread(self._sweep_runtime, rt, present_serials)
            if status is None:
                continue
            await client.send({
                "type": P.MSG_VM_STATUS, "vm_id": vm_id,
                "details": {"avd_name": rt.avd_name or _safe_avd_name(vm_id), "port": rt.port,
                            "cleanup_confirmed": status["state"] == "stopped",
                            "instance_created": vm_id in self._created_vm_ids},
                **status,
            })
            reported += 1
        return reported

    def _sweep_runtime(self, runtime: VmRuntime, present_serials: set) -> Optional[Dict[str, Any]]:
        # 不等待启动/停止持有的锁，也不能让旧快照把刚启动的新 runtime 清掉。
        lock = self._vm_lock(runtime.vm_id)
        if not lock.acquire(blocking=False):
            return None
        try:
            if self._runtimes.get(runtime.vm_id) is not runtime or not runtime.ready or not runtime.adb_serial:
                return None
            # 在本机观察前、仍持 VM 锁时复制；后续 stop/start 不得给旧观察换上新操作号。
            observed = {"observed_request_id": runtime.observed_request_id}
            if runtime.adb_serial in present_serials:
                runtime.missing_ticks = 0
                if runtime.transport_unavailable:
                    tools, _missing = find_android_tools()
                    if tools is None or self._emulator_avd_name(tools, runtime.adb_serial) != (
                        runtime.avd_name or _safe_avd_name(runtime.vm_id)
                    ):
                        return None
                    if self._getprop(tools, runtime.adb_serial, "sys.boot_completed") != "1":
                        return None
                    runtime.transport_unavailable = False
                    return {**observed, "state": "running", "ok": True, "reason": "transport_restored",
                            "adb_serial": runtime.adb_serial}
                return None
            runtime.missing_ticks += 1
            if runtime.missing_ticks < 2:
                return None
            if self._runtime_exit_confirmed(runtime):
                self._forget_runtime(runtime)
                return {**observed, "state": "stopped", "ok": True, "reason": "vanished", "adb_serial": ""}
            if runtime.transport_unavailable:
                return None
            runtime.transport_unavailable = True
            return {
                **observed, "state": "error", "ok": False, "reason": "transport_unavailable",
                "error": "设备通道不可用，尚未确认实例退出", "adb_serial": runtime.adb_serial,
            }
        finally:
            lock.release()

    def warm_capability_cache(self) -> None:
        """预热镜像列举缓存：首次 ``sdkmanager --list_installed`` 很慢，连接后先跑一次，
        让随后的第一次探查直接命中缓存、秒回，避免 Server 端探查超时（第一次显示不可用）。
        """
        tools, _missing = find_android_tools()
        if tools is not None:
            try:
                list_installed_system_images(tools)
            except Exception as exc:  # noqa: BLE001
                logger.debug("预热镜像缓存失败（忽略）：{}", exc)

    def list_managed_avd_vmids(self) -> List[str]:
        """列出本机所有受管 AVD（aiphone_vm_*）反解出的 vm_id，并入正在运行的。

        vm_id 为旧版 12 位或新版 32 位 hex，AVD 名 ``aiphone_vm_<vmid>`` 可逆反解。
        """
        self._avd_inventory_proofs = {}
        vmids: set[str] = set(self._runtimes.keys())
        tools, _missing = find_android_tools()
        if tools is None:
            raise RuntimeError("Android tools missing; AVD inventory unavailable")
        # avdmanager 可能已删 .ini，却未删净目录；残留私有目录同样必须进入孤儿对账。
        disk_vmids = _managed_avd_files(tools)
        vmids.update(disk_vmids)
        self._created_vm_ids.update(disk_vmids)
        proc = subprocess.run(
            [tools.avdmanager, "list", "avd"], check=False, capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            raise RuntimeError("avdmanager list failed; AVD inventory unavailable")
        self._avd_inventory_proofs = {
            vmid: {"avd_name": name}
            for name in _avd_names(proc.stdout or "") if (vmid := _vmid_from_avd_name(name))
        }
        self._created_vm_ids.update(self._avd_inventory_proofs)
        vmids.update(self._avd_inventory_proofs)
        processes, complete = _emulator_process_inventory()
        # 即使 AVD 定义被外部删掉，仍在跑的受管进程也必须进入对账，不能变成隐形孤儿。
        vmids.update(vmid for _proc, name, _port in processes if (vmid := _vmid_from_avd_name(name)))
        if not complete:
            self._running_inventory_complete = False
        return sorted(vmids)

    async def report_orphan_reconcile(self, client: AgentWSClient) -> int:
        """(重)连后上报本机受管 AVD 清单（区分在跑/没跑），由 Server 认领归属、置态、清孤儿。

        - running_vm_ids：本机正在跑的（_last_reclaimed_ids，由前一步 reconcile 扫 adb 得到）
        - stopped_vm_ids：确认进程已退出的实例；unknown_vm_ids：存在但运行情况待确认。
        清单或运行探测失败时 inventory_complete=False，不能做缺失差集推断。
        """
        if not self.orphan_cleanup:
            return 0
        inventory_complete = True
        try:
            vmids = await asyncio.to_thread(self.list_managed_avd_vmids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AVD 清单不完整，拒绝推断缺失或已停止：{}", exc)
            inventory_complete = False
            vmids = sorted(set(self._runtimes) | self._created_vm_ids)
        # 即使本机一台受管 AVD 都没有，也发空清单：让 Server 对"归本 Agent 但已不在本机"的
        # VM 做差集收敛（置 agent_offline），否则 Server 重启后这些会滞留假 running。
        running = sorted(set(self._last_reclaimed_ids) & set(vmids))
        stopped = []
        unknown = []
        for vm_id in sorted(set(vmids) - set(running)):
            runtime = self._runtimes.get(vm_id)
            if runtime is None:
                runtime = VmRuntime(vm_id, vm_id, "", 0, None, 0, avd_name=_safe_avd_name(vm_id))
            if inventory_complete and self._running_inventory_complete and await asyncio.to_thread(
                self._runtime_exit_confirmed, runtime
            ):
                stopped.append(vm_id)
            else:
                unknown.append(vm_id)
        await client.send({
            "type": P.MSG_VM_RECONCILE,
            "agent_id": client.agent_id,
            "vm_ids": vmids,
            "running_vm_ids": running,
            "stopped_vm_ids": stopped,
            "unknown_vm_ids": unknown,
            "inventory_complete": inventory_complete and self._running_inventory_complete,
            "instance_proofs": dict(self._avd_inventory_proofs),
        })
        logger.info(
            "已上报 Android VM 对账清单：在跑 {} / 没跑 {}（共 {}），等待 Server 认领与清理",
            len(running), len(stopped), len(vmids),
        )
        return len(vmids)

    def _ensure_avd(
        self,
        tools: AndroidVmTools,
        *,
        avd_name: str,
        system_image: str,
        width: int,
        height: int,
        density: int,
        orientation: str,
        ram_mb: int,
        vm_heap_mb: int,
        internal_storage_mb: int,
        sdcard_mb: int,
        hardware: Dict[str, Any],
        require_existing: bool = False,
        create_only: bool = False,
    ) -> bool:
        proc = subprocess.run(
            [tools.avdmanager, "list", "avd"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            raise RuntimeError("无法读取已有 AVD 清单，拒绝继续创建或修改实例")
        names = _avd_names(proc.stdout or "")
        if avd_name in names:
            if vm_id := _vmid_from_avd_name(avd_name):
                self._created_vm_ids.add(vm_id)
            if create_only:
                error = RuntimeError(f"首次绑定发现已有同名 AVD，未修改或启动：{avd_name}")
                error.created_in_attempt = False
                error.instance_preexisting = True
                raise error
            self._write_avd_screen_config(
                tools,
                avd_name=avd_name,
                width=width,
                height=height,
                density=density,
                orientation=orientation,
                ram_mb=ram_mb,
                vm_heap_mb=vm_heap_mb,
                internal_storage_mb=internal_storage_mb,
                sdcard_mb=sdcard_mb,
                hardware=hardware,
            )
            return False
        if require_existing:
            raise RuntimeError(f"原有实例缺失，未自动重建：{avd_name}")
        create_args = [tools.avdmanager, "create", "avd", "-n", avd_name, "-k", system_image]
        if not create_only:
            create_args.append("--force")
        try:
            create = subprocess.run(
                create_args, input="no\n", check=False, capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            exc.creation_attempted = True
            raise
        if create.returncode != 0:
            error = RuntimeError(
                "avdmanager create failed: "
                + ((create.stdout or "") + (create.stderr or ""))[-1200:]
            )
            error.creation_attempted = True
            raise error
        if vm_id := _vmid_from_avd_name(avd_name):
            self._created_vm_ids.add(vm_id)
        try:
            self._write_avd_screen_config(
                tools, avd_name=avd_name, width=width, height=height, density=density,
                orientation=orientation, ram_mb=ram_mb, vm_heap_mb=vm_heap_mb,
                internal_storage_mb=internal_storage_mb, sdcard_mb=sdcard_mb, hardware=hardware,
            )
        except Exception as exc:  # noqa: BLE001
            exc.created_here = True
            raise
        return True

    def _write_avd_screen_config(
        self,
        tools: AndroidVmTools,
        *,
        avd_name: str,
        width: int,
        height: int,
        density: int,
        orientation: str = "",
        ram_mb: int = 0,
        vm_heap_mb: int = 0,
        internal_storage_mb: int = 0,
        sdcard_mb: int = 0,
        hardware: Optional[Dict[str, Any]] = None,
    ) -> None:
        config_path = _avd_config_path(tools, avd_name)
        if config_path is None:
            logger.warning("未找到 AVD config，跳过屏幕尺寸写入 avd={}", avd_name)
            return
        updates = {
            "hw.lcd.width": str(width),
            "hw.lcd.height": str(height),
            "hw.lcd.density": str(density),
            "hw.initialOrientation": _orientation(orientation, width, height),
        }
        if ram_mb:
            updates["hw.ramSize"] = str(ram_mb)
        if vm_heap_mb:
            updates["vm.heapSize"] = str(vm_heap_mb)
        if internal_storage_mb:
            updates["disk.dataPartition.size"] = f"{internal_storage_mb}M"
        if sdcard_mb:
            updates["sdcard.size"] = f"{sdcard_mb}M"
        for key, value in (hardware or {}).items():
            updates[key] = value
        existing: Dict[str, str] = {}
        if config_path.exists():
            for line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip()
        existing.update(updates)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "\n".join(f"{key}={value}" for key, value in sorted(existing.items())) + "\n",
            encoding="utf-8",
        )

    def _emulator_avd_name(self, tools: AndroidVmTools, serial: str) -> str:
        if not _emulator_port(serial):
            return ""
        try:
            proc = subprocess.run(
                [tools.adb, "-s", serial, "emu", "avd", "name"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return ""
        if proc.returncode != 0:
            return ""
        for line in (proc.stdout or "").splitlines():
            value = line.strip()
            if value and value.upper() != "OK" and not value.startswith(("KO:", "error:")):
                return value
        return ""

    def _getprop(self, tools: AndroidVmTools, serial: str, prop: str) -> str:
        try:
            proc = subprocess.run(
                [tools.adb, "-s", serial, "shell", "getprop", prop],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return ""
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""

    def _kill_emulator(self, tools: AndroidVmTools, serial: str) -> bool:
        try:
            proc = subprocess.run(
                [tools.adb, "-s", serial, "emu", "kill"],
                check=False,
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _wait_boot_completed(self, tools: AndroidVmTools, adb_serial: str) -> None:
        deadline = time.monotonic() + self.boot_timeout_sec
        runtime = next((rt for rt in list(self._runtimes.values()) if rt.adb_serial == adb_serial), None)
        if runtime is not None and runtime.operation_deadline is not None:
            deadline = min(deadline, runtime.operation_deadline)
        subprocess.run(
            [tools.adb, "-s", adb_serial, "wait-for-device"],
            check=False,
            capture_output=True,
            timeout=_startup_timeout(deadline, 60),
        )
        while time.monotonic() < deadline:
            proc = subprocess.run(
                [tools.adb, "-s", adb_serial, "shell", "getprop", "sys.boot_completed"],
                check=False,
                capture_output=True,
                text=True,
                timeout=_startup_timeout(deadline, 5),
            )
            if proc.returncode == 0 and (proc.stdout or "").strip() == "1":
                # 开机成功只认 sys.boot_completed=1。不再发 keyevent 82 唤醒——那是早年"按 MENU
                # 解锁到桌面"的老土办法，新版安卓已失效、模拟器默认即亮屏，且解锁由每次用设备前的
                # driver.prepare_for_run（WAKEUP + wm dismiss-keyguard）兜底。删掉它，避免刚开机
                # input 服务未就绪时 keyevent 卡 5 秒 → 误判 start_failed。
                return
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"Android VM boot timeout: {adb_serial}")

    def _provision_device(self, tools: AndroidVmTools, adb_serial: str) -> None:
        """开机后预置：系统语言 / 时区 / 自动化友好项。best-effort，失败只告警不阻断启动。

        locale 必须 ``setprop persist.sys.locale`` 后**重启 framework（stop/start）**才生效
        ——启动 ``-prop`` 注入对 locale 无效（已真机验证）；时区启动注入即可，这里再设一次兜底。
        """
        settings = get_settings()
        locale = (settings.android_vm_locale or "").strip()
        tz = (settings.android_vm_timezone or "").strip()
        optimize = settings.android_vm_optimize_for_automation
        if not (locale or tz or optimize):
            return

        def _sh(*cmd: str, timeout: int = 15) -> int:
            proc = subprocess.run(
                [tools.adb, "-s", adb_serial, *cmd],
                check=False, capture_output=True, timeout=timeout,
            )
            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", "ignore").strip()
                logger.warning(
                    "VM {} 预置命令失败 cmd='{}' rc={} err={}",
                    adb_serial, " ".join(cmd), proc.returncode, err[:200],
                )
            return proc.returncode

        try:
            # setprop persist.* 需要 root（google_apis / default 镜像可 root）。
            subprocess.run([tools.adb, "-s", adb_serial, "root"], check=False, capture_output=True, timeout=15)
            subprocess.run([tools.adb, "-s", adb_serial, "wait-for-device"], check=False, capture_output=True, timeout=30)
            # 1) persist.* 属性（能扛过 framework 重启）：locale 需重启才生效，时区即时。
            need_restart = False
            if locale:
                _sh("shell", "setprop", "persist.sys.locale", locale)
                need_restart = True
            if tz:
                _sh("shell", "setprop", "persist.sys.timezone", tz)
            # 2) 重启 framework 让 locale 生效，再等一次开机完成。
            if need_restart:
                _sh("shell", "stop")
                _sh("shell", "start")
                self._wait_boot_completed(tools, adb_serial)
            # 3) settings put 必须放在 framework 重启**之后**——否则会被 stop/start 覆盖/恢复。
            #    但 sys.boot_completed=1 早于 settings 服务就绪，重启后直接 put 会 rc=224 /
            #    "Can't find service"（真机验证）。所以先等 settings 服务可用，再 put + 失败重试。
            opt_failed = 0
            if optimize:
                if not self._wait_settings_ready(tools, adb_serial):
                    logger.warning("VM {} settings 服务迟迟未就绪，自动化优化可能不全", adb_serial)
                opt_cmds = [
                    ("global", "window_animation_scale", "0"),
                    ("global", "transition_animation_scale", "0"),
                    ("global", "animator_duration_scale", "0"),
                    ("system", "time_12_24", "24"),
                ]
                for namespace, key, value in opt_cmds:
                    ok = False
                    for _attempt in range(4):
                        if _sh("shell", "settings", "put", namespace, key, value) == 0:
                            ok = True
                            break
                        time.sleep(1)
                    if not ok:
                        opt_failed += 1
            if opt_failed:
                logger.warning(
                    "VM {} 开机预置部分失败：自动化优化 {}/{} 条未生效 locale={} tz={}",
                    adb_serial, opt_failed, 4, locale or "-", tz or "-",
                )
            else:
                logger.info(
                    "VM {} 开机预置完成 locale={} tz={} optimize={}",
                    adb_serial, locale or "-", tz or "-", optimize,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("VM {} 开机预置失败（忽略，不阻断启动）：{}", adb_serial, exc)

    def _wait_settings_ready(self, tools: AndroidVmTools, adb_serial: str, timeout_sec: int = 30) -> bool:
        """等 settings 服务真正可用（framework 重启后 sys.boot_completed=1 仍可能早于它）。

        以 ``settings get`` 能正常返回（rc=0 且非 "Can't find service"）为就绪信号。
        """
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                proc = subprocess.run(
                    [tools.adb, "-s", adb_serial, "shell", "settings", "get", "global", "window_animation_scale"],
                    check=False, capture_output=True, text=True, timeout=10,
                )
            except Exception:  # noqa: BLE001
                time.sleep(1)
                continue
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0 and "Can't find service" not in out and "Exception" not in out:
                return True
            time.sleep(1)
        return False

    def _choose_port(
        self,
        *,
        prefer: Optional[int] = None,
        exclude: Optional[set[int]] = None,
    ) -> int:
        """挑一个本机空闲的 emulator 端口。

        ``prefer``：Server 全局分配的端口——必须使用此端口，冲突时拒绝启动。
        ``exclude``：Server 下发的全局已占端口，并入本机已用一起避让（兜底防跨机撞号）。
        无 Server 预留（旧客户端/本地调用）时，才保留本机分配兼容路径。
        """
        used = {runtime.port for runtime in list(self._runtimes.values())}
        if exclude:
            used |= exclude
        if prefer is not None:
            if prefer in used or not _is_port_free(prefer) or not _is_port_free(prefer + 1):
                raise RuntimeError(f"已分配的 emulator 端口 {prefer} 被占用，未自动换端口")
            return prefer
        for port in range(5554, 5684, 2):
            if port in used or port == prefer:
                continue
            if _is_port_free(port) and _is_port_free(port + 1):
                return port
        raise RuntimeError("no free emulator port")

    @staticmethod
    def _status_payload(
        msg: Dict[str, Any],
        *,
        state: str,
        ok: bool,
        reason: str,
        error: str = "",
        adb_serial: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "type": P.MSG_VM_STATUS,
            "request_id": msg.get("request_id") or "",
            "vm_id": msg.get("vm_id") or "",
            "state": state,
            "ok": ok,
            "reason": reason,
            "error": error,
            "adb_serial": adb_serial,
            "details": details or {},
        }


_AVD_NAME_PREFIX = "aiphone_vm_"
_STOP_VERIFY_TIMEOUT_SEC = 5.0


def _start_deadline(msg: Dict[str, Any]) -> Optional[float]:
    raw = msg.get("operation_timeout_sec")
    if raw in (None, ""):
        return None
    timeout = float(raw)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("operation_timeout_sec must be a positive finite number")
    deadline = time.monotonic() + timeout
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
        raise TimeoutError("Android VM startup deadline exceeded")
    return min(maximum, remaining)


@contextmanager
def _startup_lock(lock: Any, deadline: Optional[float]):
    acquired = lock.acquire() if deadline is None else lock.acquire(
        timeout=_startup_timeout(deadline, threading.TIMEOUT_MAX)
    )
    if not acquired:
        raise TimeoutError("Android VM startup deadline exceeded waiting for lifecycle lock")
    try:
        _startup_timeout(deadline, 1)
        yield
    finally:
        lock.release()


def _avd_names(output: str) -> set[str]:
    return {
        line.strip().split(":", 1)[1].strip()
        for line in output.splitlines() if line.strip().startswith("Name:")
    }


def _find_avd_processes(avd_name: str) -> tuple[list[tuple[Any, int]], bool]:
    """只认 emulator/qemu 命令行中的精确 AVD 名；端口从不作为杀进程的身份。"""
    processes, complete = _emulator_process_inventory()
    return [(proc, port) for proc, name, port in processes if name == avd_name], complete


def _emulator_process_inventory() -> tuple[list[tuple[Any, str, int]], bool]:
    processes: list[tuple[Any, str, int]] = []
    complete = True
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if not name:
                try:
                    if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                        continue
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except (psutil.AccessDenied, OSError):
                    pass
                complete = False
                continue
            if name not in {"emulator", "emulator.exe"} and not name.startswith("qemu-system-"):
                continue
            try:
                args = proc.cmdline()
                if not args:
                    complete = False
                    continue
                actual_name = ""
                port = 0
                for i, arg in enumerate(args):
                    if arg == "-avd" and i + 1 < len(args):
                        actual_name = args[i + 1]
                    elif arg.startswith("@"):
                        actual_name = arg[1:]
                    if arg in {"-port", "-ports"} and i + 1 < len(args):
                        port = _opt_port(args[i + 1].split(",", 1)[0]) or 0
                if actual_name:
                    processes.append((proc, actual_name, port))
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.AccessDenied, OSError):
                complete = False
    except (psutil.Error, OSError):
        complete = False
    return processes, complete


def _stop_owned_process(proc: Any, *, graceful: bool) -> None:
    """Popen 或按精确 AVD 名找到的 psutil.Process；后者在发信号时还校验 PID 代次。"""
    try:
        if graceful:
            try:
                proc.wait(timeout=5)
                return
            except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                pass
        if hasattr(proc, "poll"):
            if proc.poll() is not None:
                return
        elif not proc.is_running():
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=5)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return


def _safe_avd_name(vm_id: str) -> str:
    # VM 表 ID 最长 32 位：完整保留新版 UUID hex，旧版 12 位名称不变。
    suffix = re.sub(r"[^A-Za-z0-9_]+", "_", vm_id)[:32]
    return f"{_AVD_NAME_PREFIX}{suffix}"


def _vmid_from_avd_name(name: str) -> str:
    """从受管 AVD 名 ``aiphone_vm_<vmid>`` 反解出 vm_id；非受管返回空串。

    旧版 12 位及新版 32 位 hex ID 都与 `_safe_avd_name` 严格互逆。
    """
    n = (name or "").strip()
    if n.startswith(_AVD_NAME_PREFIX):
        vm_id = n[len(_AVD_NAME_PREFIX):]
        # 不把会被替换/截断的外部名称反解为另一台受管实例的身份。
        if vm_id and _safe_avd_name(vm_id) == n:
            return vm_id
    return ""


def _msg_int(msg: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(msg.get(key) or default)
    except Exception:
        return default
    return max(320, min(7680, value))


def _density_int(msg: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(msg.get(key) or default)
    except Exception:
        return default
    return max(120, min(800, value))


def _optional_int(
    msg: Dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        raw = msg.get(key)
        value = int(raw if raw not in (None, "") else default)
    except Exception:
        return default
    if value <= 0:
        return 0
    return max(minimum, min(maximum, value))


def _msg_bool(msg: Dict[str, Any], key: str, default: bool) -> bool:
    raw = msg.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _choice(value: str, allowed: set[str], default: str) -> str:
    raw = (value or "").strip().lower()
    return raw if raw in allowed else default


def _camera(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in {"none", "emulated", "webcam0"}:
        return raw
    return ""


def _hardware_config(msg: Dict[str, Any]) -> Dict[str, str]:
    return {
        "hw.camera.back": _camera(str(msg.get("back_camera") or "emulated")) or "emulated",
        "hw.camera.front": _camera(str(msg.get("front_camera") or "none")) or "none",
        "hw.gps": "yes" if _msg_bool(msg, "gps", True) else "no",
        "hw.accelerometer": "yes" if _msg_bool(msg, "accelerometer", True) else "no",
        "hw.gyroscope": "yes" if _msg_bool(msg, "gyroscope", True) else "no",
        "hw.sensors.proximity": "yes" if _msg_bool(msg, "proximity", False) else "no",
        "hw.keyboard": "yes" if _msg_bool(msg, "hardware_keyboard", False) else "no",
        "hw.dPad": "yes" if str(msg.get("navigation_style") or "none").lower() == "dpad" else "no",
    }


def _orientation(value: str, width: int, height: int) -> str:
    raw = (value or "").strip().lower()
    if raw in {"portrait", "landscape"}:
        return raw
    return "portrait" if height >= width else "landscape"


def _emulator_port(serial: str) -> int:
    try:
        if re.fullmatch(r"emulator-\d+", serial):
            return _opt_port(serial.rsplit("-", 1)[1]) or 0
    except Exception:
        pass
    return 0


def _opt_port(value: Any) -> Optional[int]:
    """把 Server 下发的端口值（assigned_port / exclude_ports 元素）解析成 int；非法返回 None。"""
    if value is None:
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 5554 <= port <= 5682 and port % 2 == 0 else None


def _avd_config_path(tools: AndroidVmTools, avd_name: str) -> Optional[Path]:
    roots: List[Path] = []
    avd_home = os.environ.get("ANDROID_AVD_HOME")
    if avd_home:
        return Path(avd_home) / f"{avd_name}.avd" / "config.ini"
    # Path.home() 跨平台：macOS→/Users/x、Linux→/home/x、Windows→C:\Users\x（即 USERPROFILE）。
    try:
        home = Path.home()
    except Exception:  # noqa: BLE001
        home = None
    if home is not None:
        roots.append(home / ".android" / "avd")
    if tools.sdk_root:
        roots.append(Path(tools.sdk_root) / ".android" / "avd")
    for root in roots:
        path = root / f"{avd_name}.avd" / "config.ini"
        if path.exists() or root.exists():
            return path
    return roots[0] / f"{avd_name}.avd" / "config.ini" if roots else None


def _managed_avd_files(tools: AndroidVmTools) -> set[str]:
    """仅枚举当前 AVD 根目录下本平台的私有目录/索引，不据此生成绑定证明。"""
    sample = _avd_config_path(tools, f"{_AVD_NAME_PREFIX}inventory")
    if sample is None or not sample.parent.parent.exists():
        return set()
    vmids = set()
    for path in sample.parent.parent.iterdir():
        if (path.suffix == ".avd" and path.is_dir()) or (path.suffix == ".ini" and path.is_file()):
            vm_id = _vmid_from_avd_name(path.stem)
            if vm_id:
                vmids.add(vm_id)
    return vmids


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # 与 Emulator socketSetXReuseAddr 一致：POSIX 允许重用已关闭连接的 TIME_WAIT，
        # Windows 使用独占地址而非 SO_REUSEADDR；两者都不共享活跃监听端口。
        try:
            option = getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR)
            sock.setsockopt(socket.SOL_SOCKET, option, 1)
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True
