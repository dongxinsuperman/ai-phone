"""Agent-side Android VM manager."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    # 运行期巡检用：ready=已确认开机完成/在跑（未就绪不判"消失"，避免启动中被误清）；
    # missing_ticks=连续在设备快照里缺席的轮数（连续 2 轮才判消失，防 adb 单次抖动）。
    ready: bool = False
    missing_ticks: int = 0


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
        # 仅防极端失控的硬兜底；真正能起几台由 capability.probe 按实时可用内存决定。
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
        serial_to_runtime = {rt.adb_serial: rt for rt in self._runtimes.values() if rt.adb_serial}
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
        task = asyncio.create_task(self._start_and_report(client, msg), name=f"android-vm-start-{msg.get('vm_id')}")
        task.add_done_callback(_log_task_error)

    async def handle_stop(self, client: AgentWSClient, msg: Dict[str, Any]) -> None:
        await client.send(self._status_payload(msg, state="stopping", ok=True, reason="stopping"))
        result = await asyncio.to_thread(self.stop_sync, str(msg.get("vm_id") or ""))
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
        这一台，不影响其它 VM / 真机链路。Server 已删 DB 记录、不等 ack，此处尽力清理。
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
                "avd_name": str(result.get("avd_name") or ""),
                "deleted_avd": bool(result.get("ok")),
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
            result = await asyncio.to_thread(self.start_sync, msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Android VM 启动失败 vm_id={}: {}", msg.get("vm_id"), exc)
            await client.send(self._status_payload(
                msg,
                state="error",
                ok=False,
                reason="start_failed",
                error=str(exc),
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
        with self._vm_lock(vm_id):
            return self._start_sync_locked(vm_id, msg)

    def _start_sync_locked(self, vm_id: str, msg: Dict[str, Any]) -> Dict[str, Any]:
        existing = self._runtimes.get(vm_id)
        if existing is not None:
            return {"adb_serial": existing.adb_serial, "details": {"reused": True}}

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
        exclude_ports = {
            p for p in (_opt_port(x) for x in (msg.get("exclude_ports") or [])) if p is not None
        }
        # 锁内：幂等去重 + 选端口 + 占位注册（原子）——杜绝并发选到同一端口 / 同 vm_id 重复启动。
        with self._start_lock:
            existing = self._runtimes.get(vm_id)
            if existing is not None:
                return {"adb_serial": existing.adb_serial, "details": {"reused": True}}
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
            )
            self._runtimes[vm_id] = runtime
        # 占位之后任何失败都清掉占位（释放 vm_id/端口、关 log fd、杀残进程），避免泄漏。
        try:
            self._ensure_avd(
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
            )
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
            proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT)
            runtime.process = proc
            self._wait_boot_completed(tools, adb_serial)
            # 开机后预置：系统语言 / 时区 / 自动化友好项（best-effort，失败不阻断启动）。
            # 注意里面会 stop/start 重启 framework，那一下 adb 设备可能短暂抖动。
            self._provision_device(tools, adb_serial)
            # 预置全部做完、临返回前才标 ready——此后才纳入运行期"消失"巡检。
            # 放在这里（而非 _wait_boot_completed 之后）：避免 provision 期间的 framework 重启抖动
            # 被巡检盯上（虽有 missing_ticks 防抖，但放到全部就绪后语义更严谨、零误判窗口）。
            runtime.ready = True
            runtime.missing_ticks = 0
            return {
                "adb_serial": adb_serial,
                "details": {
                    "avd_name": avd_name,
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
        except Exception:
            self.stop_sync(vm_id)
            raise

    def stop_sync(self, vm_id: str) -> Dict[str, Any]:
        with self._vm_lock(vm_id):
            return self._stop_sync_locked(vm_id)

    def _stop_sync_locked(self, vm_id: str) -> Dict[str, Any]:
        runtime = self._runtimes.pop(vm_id, None)
        if runtime is None:
            return {"ok": True, "reason": "not_running", "adb_serial": ""}
        adb_serial = runtime.adb_serial
        tools, _missing = find_android_tools()
        if tools is not None:
            try:
                subprocess.run(
                    [tools.adb, "-s", adb_serial, "emu", "kill"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
        if runtime.process is not None and runtime.process.poll() is None:
            runtime.process.terminate()
            try:
                runtime.process.wait(timeout=5)
            except Exception:
                runtime.process.kill()
        if runtime.log_file is not None:
            try:
                runtime.log_file.close()
            except Exception:
                pass
        return {"ok": True, "reason": "stopped", "adb_serial": adb_serial}

    def stop_all(self) -> int:
        vm_ids = list(self._runtimes.keys())
        for vm_id in vm_ids:
            self.stop_sync(vm_id)
        return len(vm_ids)

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
        tools, _missing = find_android_tools()
        # 1) 先停 emulator（本进程启动的走 stop_sync；否则按 serial 发 emu kill），避免文件占用
        if vm_id in self._runtimes:
            self.stop_sync(vm_id)
        elif adb_serial and tools is not None:
            self._kill_emulator(tools, adb_serial)
        if tools is None:
            return {"ok": False, "reason": "tools_missing", "error": "android tools missing", "avd_name": avd_name}
        # 2) 删除 AVD（emulator 释放文件可能有延迟，重试几次）
        ok = self._delete_avd(tools, avd_name)
        return {
            "ok": ok,
            "reason": "deleted" if ok else "delete_failed",
            "error": "" if ok else "avdmanager delete failed",
            "avd_name": avd_name,
        }

    def _delete_avd(self, tools: AndroidVmTools, avd_name: str) -> bool:
        last = ""
        for _attempt in range(3):
            try:
                proc = subprocess.run(
                    [tools.avdmanager, "delete", "avd", "-n", avd_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(1.0)
                continue
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                self._remove_avd_dir(tools, avd_name)
                return True
            # AVD 本就不存在 → 幂等成功
            if "no Android Virtual Device" in out or "not found" in out.lower():
                self._remove_avd_dir(tools, avd_name)
                return True
            last = out
            time.sleep(1.0)  # emulator 可能还没释放 AVD 文件
        logger.error("avdmanager delete 失败 avd={}: {}", avd_name, last or "(无输出)")
        return False

    def _remove_avd_dir(self, tools: AndroidVmTools, avd_name: str) -> None:
        """兜底清理 avdmanager 没删干净的残留目录 / .ini。"""
        cfg = _avd_config_path(tools, avd_name)
        if cfg is None:
            return
        avd_dir = cfg.parent
        try:
            if avd_dir.exists():
                shutil.rmtree(avd_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("清理 AVD 目录失败 {}: {}", avd_dir, exc)
        try:
            ini = avd_dir.parent / f"{avd_name}.ini"
            if ini.exists():
                ini.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.debug("清理 AVD .ini 失败 {}: {}", avd_name, exc)

    def reconcile_running_vms_sync(self) -> List[VmRuntime]:
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
        adopted: List[VmRuntime] = []
        seen_vm_ids: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) < 2 or not parts[0].startswith("emulator-") or parts[1] != "device":
                continue
            serial = parts[0]
            # 身份锚点改为 AVD 名（aiphone_vm_<vmid>，持久、重启还在）。
            # 不再读 debug.aiphone.vmid——Emulator 会丢弃这种自定义属性（实测 getprop 为空）。
            avd_name = self._emulator_avd_name(tools, serial)
            vm_id = _vmid_from_avd_name(avd_name)
            if not vm_id:
                # 非受管 AVD（野生模拟器 / 名字对不上）
                if self.kill_foreign:
                    self._kill_emulator(tools, serial)
                continue
            seen_vm_ids.add(vm_id)
            alias = self._getprop(tools, serial, "debug.aiphone.alias")  # best-effort，可能为空
            existing = self._runtimes.get(vm_id)
            if existing is not None:
                existing.name = alias or existing.name or vm_id
                existing.adb_serial = serial
                existing.port = _emulator_port(serial)
                existing.avd_name = avd_name
                existing.ready = True  # 从 adb 扫到=确在跑
                existing.missing_ticks = 0
                adopted.append(existing)
                continue
            runtime = VmRuntime(
                vm_id=vm_id,
                name=alias or vm_id,
                adb_serial=serial,
                port=_emulator_port(serial),
                process=None,
                started_at=time.time(),
                avd_name=avd_name,
                ready=True,  # 认领=确在跑，纳入巡检
            )
            self._runtimes[vm_id] = runtime
            adopted.append(runtime)
        self._last_reclaimed_ids = {rt.vm_id for rt in adopted}
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
                },
            })
        if runtimes:
            await self._refresh_devices_safe(client)
        return len(runtimes)

    async def sweep_vanished_vms(self, client: AgentWSClient, present_serials: set) -> int:
        """运行中存活巡检（蹭 rescan 节拍，零额外扫描）：本机以为在跑、但 serial 已从设备
        快照消失的 VM（emulator 崩溃 / 被外部关闭）→ 立刻上报 stopped 并从 _runtimes 清掉。

        这样"运行中途掉链子"也能在一个扫描周期内把 Server 的 state 收敛成 stopped，
        不再滞留假 running（与"重连认领""换 Agent"互补，覆盖运行期）。
        """
        reported = 0
        for vm_id, rt in list(self._runtimes.items()):
            # 只巡检"已就绪/在跑过"的：启动中(ready=False)的 serial 还没进设备列表，绝不能判消失
            if not rt.ready or not rt.adb_serial:
                continue
            if rt.adb_serial in present_serials:
                rt.missing_ticks = 0
                continue
            # 缺席一次先记账，连续 2 轮(~10s)都不在才判消失——防 adb 单次抖动误清
            rt.missing_ticks += 1
            if rt.missing_ticks < 2:
                logger.debug("VM {} serial {} 本轮缺席（第 {} 次），暂不判消失",
                             vm_id, rt.adb_serial, rt.missing_ticks)
                continue
            self._runtimes.pop(vm_id, None)
            self._last_reclaimed_ids.discard(vm_id)
            await client.send({
                "type": P.MSG_VM_STATUS,
                "vm_id": vm_id,
                "state": "stopped",
                "ok": True,
                "reason": "vanished",
                "adb_serial": "",
                "details": {"avd_name": rt.avd_name, "port": rt.port},
            })
            logger.info("VM {} 的 emulator({}) 连续缺席，已消失，上报 stopped", vm_id, rt.adb_serial)
            reported += 1
        return reported

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

        vm_id 是纯 hex 短 id，AVD 名 ``aiphone_vm_<vmid>`` 可逆反解。
        """
        vmids: set[str] = set(self._runtimes.keys())
        tools, _missing = find_android_tools()
        if tools is not None:
            try:
                proc = subprocess.run(
                    [tools.avdmanager, "list", "avd"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                for line in (proc.stdout or "").splitlines():
                    s = line.strip()
                    if s.startswith("Name:"):
                        vmid = _vmid_from_avd_name(s.split(":", 1)[1].strip())
                        if vmid:
                            vmids.add(vmid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("列出 AVD 失败（跳过孤儿对账）：{}", exc)
        return sorted(vmids)

    async def report_orphan_reconcile(self, client: AgentWSClient) -> int:
        """(重)连后上报本机受管 AVD 清单（区分在跑/没跑），由 Server 认领归属、置态、清孤儿。

        - running_vm_ids：本机正在跑的（_last_reclaimed_ids，由前一步 reconcile 扫 adb 得到）
        - stopped_vm_ids：本机只有 AVD、没在跑的（全集 - 在跑）
        Server 据此：在跑的留 running（由 vm_status reclaimed 置态）、没跑的明确归 stopped。
        """
        if not self.orphan_cleanup:
            return 0
        vmids = await asyncio.to_thread(self.list_managed_avd_vmids)
        # 即使本机一台受管 AVD 都没有，也发空清单：让 Server 对"归本 Agent 但已不在本机"的
        # VM 做差集收敛（置 agent_offline），否则 Server 重启后这些会滞留假 running。
        running = sorted(set(self._last_reclaimed_ids) & set(vmids))
        stopped = sorted(set(vmids) - set(running))
        await client.send({
            "type": P.MSG_VM_RECONCILE,
            "agent_id": client.agent_id,
            "vm_ids": vmids,
            "running_vm_ids": running,
            "stopped_vm_ids": stopped,
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
    ) -> None:
        proc = subprocess.run(
            [tools.avdmanager, "list", "avd"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if f"Name: {avd_name}" in (proc.stdout or ""):
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
            return
        create = subprocess.run(
            [
                tools.avdmanager,
                "create",
                "avd",
                "-n", avd_name,
                "-k", system_image,
                "--force",
            ],
            input="no\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if create.returncode != 0:
            raise RuntimeError(
                "avdmanager create failed: "
                + ((create.stdout or "") + (create.stderr or ""))[-1200:]
            )
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
        for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
            value = line.strip()
            if value and value.upper() != "OK":
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
        return (proc.stdout or "").strip()

    def _kill_emulator(self, tools: AndroidVmTools, serial: str) -> None:
        try:
            subprocess.run(
                [tools.adb, "-s", serial, "emu", "kill"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    def _wait_boot_completed(self, tools: AndroidVmTools, adb_serial: str) -> None:
        subprocess.run(
            [tools.adb, "-s", adb_serial, "wait-for-device"],
            check=False,
            capture_output=True,
            timeout=min(60, self.boot_timeout_sec),
        )
        deadline = time.monotonic() + self.boot_timeout_sec
        while time.monotonic() < deadline:
            proc = subprocess.run(
                [tools.adb, "-s", adb_serial, "shell", "getprop", "sys.boot_completed"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if (proc.stdout or "").strip() == "1":
                # 开机成功只认 sys.boot_completed=1。不再发 keyevent 82 唤醒——那是早年"按 MENU
                # 解锁到桌面"的老土办法，新版安卓已失效、模拟器默认即亮屏，且解锁由每次用设备前的
                # driver.prepare_for_run（WAKEUP + wm dismiss-keyguard）兜底。删掉它，避免刚开机
                # input 服务未就绪时 keyevent 卡 5 秒 → 误判 start_failed。
                return
            time.sleep(2)
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

        ``prefer``：Server 全局分配的端口——本机也空闲时优先用它，保证 serial 全网唯一。
        ``exclude``：Server 下发的全局已占端口，并入本机已用一起避让（兜底防跨机撞号）。
        本机该端口被其它进程占用时，落回区间扫描另选一个（serial 会经 vm_status 回填）。
        """
        used = {runtime.port for runtime in self._runtimes.values()}
        if exclude:
            used |= exclude
        if (
            prefer is not None
            and prefer not in used
            and _is_port_free(prefer)
            and _is_port_free(prefer + 1)
        ):
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


def _safe_avd_name(vm_id: str) -> str:
    # vm_id 恒为短 hex（server `_short_id` 12 位），[:24] 不会截断；可逆反解见 _vmid_from_avd_name。
    suffix = re.sub(r"[^A-Za-z0-9_]+", "_", vm_id)[:24]
    return f"{_AVD_NAME_PREFIX}{suffix}"


def _vmid_from_avd_name(name: str) -> str:
    """从受管 AVD 名 ``aiphone_vm_<vmid>`` 反解出 vm_id；非受管返回空串。

    vm_id 是纯 hex 短 id，`_safe_avd_name` 与本函数严格互为逆（round-trip 一致）。
    """
    n = (name or "").strip()
    if n.startswith(_AVD_NAME_PREFIX):
        return n[len(_AVD_NAME_PREFIX):]
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
        if serial.startswith("emulator-"):
            return int(serial.rsplit("-", 1)[1])
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
    return port if port > 0 else None


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


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _log_task_error(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("Android VM background task failed: {}", exc)
