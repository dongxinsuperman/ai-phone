"""Agent-side lifecycle manager for DevEco HarmonyOS virtual machines."""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from ai_phone.agent.drivers import open_harmony_driver
from ai_phone.agent.drivers.base import BaseDriver, DeviceInfo
from ai_phone.agent.drivers.hdc import hdc_list_targets, hdc_run
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.agent.android_vm.capability import host_abi
from ai_phone.config import get_settings
from ai_phone.shared import protocol as P
from ai_phone.shared.harmony_identity import normalize_harmony_instance_uuid

from .capability import (
    HDC_PORT_MAX,
    HDC_PORT_MIN,
    discover_harmony_image_roots,
    discover_harmony_instance_root,
    find_harmony_tools,
    format_os_version,
    probe_harmony_vm_capability,
    scan_downloaded_images,
)
from .registry import (
    register_managed_serial,
    set_managed_fport,
    unregister_managed_serial,
)


_INSTANCE_PREFIX = "aiphone_harmony_"

# DisplayManagerService 的 dump 开关。官方 create/start CLI 没有公开初始折叠状态
# 参数，这是实测可用的唯一通道：-m 折叠（外屏）、-f 展开（内屏）。
FOLD_DUMP_ARG_FOLDED = "-m"
FOLD_DUMP_ARG_UNFOLDED = "-f"
FOLD_VERIFY_ATTEMPTS = 6
FOLD_VERIFY_INTERVAL_SEC = 1.0


@dataclass
class HarmonyVmRuntime:
    vm_id: str
    name: str
    instance_name: str
    instance_path: str
    image_root: str
    hdc_port: int
    hdc_serial: str
    lease_token: str
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    driver: Optional[BaseDriver] = field(default=None, repr=False)
    log_file: Optional[Any] = field(default=None, repr=False)
    driver_fport_port: Optional[int] = None
    started_at: float = 0.0
    ready: bool = False
    missing_ticks: int = 0

    def persistent_dict(self) -> Dict[str, Any]:
        return {
            "vm_id": self.vm_id,
            "name": self.name,
            "instance_name": self.instance_name,
            "instance_path": self.instance_path,
            "image_root": self.image_root,
            "hdc_port": self.hdc_port,
            "hdc_serial": self.hdc_serial,
            "lease_token": self.lease_token,
            "driver_fport_port": self.driver_fport_port,
            "started_at": self.started_at,
            "ready": self.ready,
            "missing_ticks": self.missing_ticks,
        }


class HarmonyVmManager:
    """Own all managed DevEco emulator instances for one Agent process."""

    def __init__(
        self,
        *,
        runtime_dir: Optional[Path] = None,
        drop_driver_cache: Optional[Callable[[str], None]] = None,
    ) -> None:
        settings = get_settings()
        self.runtime_dir = runtime_dir or (
            Path(settings.storage_dir) / "harmony_vm_runtime"
        )
        self.instance_path = (
            runtime_dir / "instances"
            if runtime_dir is not None
            else Path(discover_harmony_instance_root())
        )
        self.registry_path = self.runtime_dir / "registry.json"
        self._drop_driver_cache = drop_driver_cache
        self._runtimes: Dict[str, HarmonyVmRuntime] = {}
        self._known: Dict[str, Dict[str, Any]] = self._load_registry()
        self._start_lock = threading.RLock()
        self._locks_guard = threading.Lock()
        self._vm_locks: Dict[str, threading.RLock] = {}
        self._last_reclaimed_ids: set[str] = set()

    @property
    def max_instances(self) -> int:
        return int(get_settings().harmony_vm_max_instances)

    @property
    def boot_timeout_sec(self) -> int:
        return int(get_settings().harmony_vm_boot_timeout_sec)

    @property
    def orphan_cleanup(self) -> bool:
        return bool(get_settings().harmony_vm_orphan_cleanup)

    def _vm_lock(self, vm_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._vm_locks.get(vm_id)
            if lock is None:
                lock = threading.RLock()
                self._vm_locks[vm_id] = lock
            return lock

    def decorate_devices(self, infos: List[DeviceInfo]) -> List[DeviceInfo]:
        serial_to_runtime = {
            runtime.hdc_serial: runtime
            for runtime in self._runtimes.values()
            if runtime.ready and runtime.hdc_serial
        }
        known_managed_serials = {
            str(row.get("hdc_serial") or "")
            for row in self._known.values()
            if str(row.get("hdc_serial") or "")
        }
        out: List[DeviceInfo] = []
        for info in infos:
            runtime = serial_to_runtime.get(info.serial)
            if runtime is None:
                # A registry-known VM that failed lease/driver reclaim must not
                # leak into the generic route as if it were a physical device.
                # Unknown manually-created emulators are not filtered.
                if info.serial in known_managed_serials:
                    continue
                out.append(info)
                continue
            extra = dict(info.extra or {})
            extra.update(
                {
                    "device_kind": "virtual",
                    "is_virtual": True,
                    "vm_platform": "harmony",
                    "vm_instance_id": runtime.vm_id,
                    "vm_name": runtime.name,
                }
            )
            info.extra = extra
            out.append(info)
        return out

    def probe(self, requirement: Dict[str, Any]) -> Dict[str, Any]:
        result = probe_harmony_vm_capability(
            requirement,
            current_instances=len(self._runtimes),
            max_instances=self.max_instances,
            image_roots=self._discovered_image_roots(),
        )
        details = (
            result.get("details")
            if isinstance(result.get("details"), dict)
            else {}
        )
        details["instance_root"] = str(self.instance_path)
        details["instance_root_source"] = "deveco_config_or_official_default"
        result["details"] = details
        if not str(self.instance_path).isascii():
            result["ok"] = False
            result["reason"] = (
                "DevEco Emulator 不支持含非 ASCII 字符的实例目录；"
                "请用 Emulator -config -instancePath 配置纯英文目录"
            )
        return result

    def _discovered_image_roots(self) -> list[str]:
        # Existing local instances retain the root they were created against;
        # new instances use DevEco's current official configuration.  These are
        # local facts only and are never supplied by Server.
        recorded = [
            str(row.get("image_root") or "")
            for row in self._known.values()
            if str(row.get("image_root") or "").strip()
        ]
        return discover_harmony_image_roots(recorded)

    def _resolve_image_root(self, requirement: Dict[str, Any]) -> str:
        version_spec = format_os_version(
            str(requirement.get("os_version") or ""),
            str(requirement.get("api_version") or ""),
        )
        device_type = str(requirement.get("device_type") or "Phone").strip()
        requested_abi = str(requirement.get("abi") or "auto").strip().lower()
        if requested_abi in {"", "auto"}:
            requested_abi = host_abi()
        elif requested_abi in {"arm", "arm64-v8a", "aarch64"}:
            requested_abi = "arm64"
        elif requested_abi in {"x86", "x64", "amd64"}:
            requested_abi = "x86_64"
        for image in scan_downloaded_images(self._discovered_image_roots()):
            if (
                str(image.get("device_type") or "") == device_type
                and str(image.get("os_version") or "") == version_spec
                and str(image.get("abi") or "") == requested_abi
            ):
                return str(image.get("image_root") or "")
        return ""

    async def handle_capability_probe(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        result = await asyncio.to_thread(self.probe, msg)
        await client.send(
            {
                "type": P.MSG_HARMONY_VM_CAPABILITY,
                "request_id": msg.get("request_id") or "",
                "agent_id": client.agent_id,
                **result,
            }
        )

    async def handle_start(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        await client.send(
            self._status_payload(msg, state="starting", ok=True, reason="starting")
        )
        task = asyncio.create_task(
            self._start_and_report(client, msg),
            name=f"harmony-vm-start-{msg.get('vm_id')}",
        )
        task.add_done_callback(_log_task_error)

    async def _start_and_report(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        try:
            result = await asyncio.to_thread(self.start_sync, msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Harmony VM 启动失败 vm_id={}: {}", msg.get("vm_id"), exc)
            cleanup = getattr(exc, "cleanup_details", {})
            await client.send(
                self._status_payload(
                    msg,
                    state="error",
                    ok=False,
                    reason="start_failed",
                    error=str(exc),
                    details=dict(cleanup or {}),
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
                hdc_serial=str(result.get("hdc_serial") or ""),
                details=dict(result.get("details") or {}),
            )
        )
        await self._refresh_devices_safe(client)

    async def handle_stop(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        await client.send(
            self._status_payload(msg, state="stopping", ok=True, reason="stopping")
        )
        try:
            result = await asyncio.to_thread(
                self.stop_sync,
                str(msg.get("vm_id") or ""),
                str(msg.get("hdc_serial") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Harmony VM 停止收口异常 vm_id={}", msg.get("vm_id"))
            result = {
                "ok": False,
                "reason": "stop_failed",
                "error": str(exc),
                "details": {"cleanup_confirmed": False},
            }
        await client.send(
            self._status_payload(
                msg,
                state="stopped" if result.get("ok") else "error",
                ok=bool(result.get("ok")),
                reason=str(result.get("reason") or ""),
                error=str(result.get("error") or ""),
                hdc_serial=str(result.get("hdc_serial") or ""),
                details=dict(result.get("details") or {}),
            )
        )
        await self._refresh_devices_safe(client)

    async def handle_delete(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        try:
            result = await asyncio.to_thread(
                self.delete_sync,
                str(msg.get("vm_id") or ""),
                str(msg.get("hdc_serial") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Harmony VM 删除收口异常 vm_id={}", msg.get("vm_id"))
            result = {
                "ok": False,
                "reason": "delete_failed",
                "error": str(exc),
                "details": {"cleanup_confirmed": False},
            }
        await client.send(
            self._status_payload(
                msg,
                state="stopped" if result.get("ok") else "error",
                ok=bool(result.get("ok")),
                reason=str(result.get("reason") or ""),
                error=str(result.get("error") or ""),
                details=dict(result.get("details") or {}),
            )
        )
        await self._refresh_devices_safe(client)

    def start_sync(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        vm_id = str(msg.get("vm_id") or "").strip()
        if not vm_id:
            raise ValueError("vm_id is required")
        with self._vm_lock(vm_id):
            return self._start_sync_locked(vm_id, msg)

    def _start_sync_locked(
        self, vm_id: str, msg: Dict[str, Any]
    ) -> Dict[str, Any]:
        lease_token = str(msg.get("lease_token") or "").strip()
        port = _int_value(msg.get("assigned_port"), 0)
        if not lease_token:
            raise ValueError("lease_token is required")
        if not HDC_PORT_MIN <= port <= HDC_PORT_MAX:
            raise ValueError(
                f"assigned_port must be in {HDC_PORT_MIN}..{HDC_PORT_MAX}"
            )
        serial = f"127.0.0.1:{port}"
        existing = self._runtimes.get(vm_id)
        if existing is not None:
            if existing.lease_token != lease_token or existing.hdc_port != port:
                raise RuntimeError("running VM lease does not match start request")
            return {
                "hdc_serial": existing.hdc_serial,
                "details": self._runtime_details(existing, reused=True),
            }

        capability = self.probe(msg)
        if not capability.get("ok"):
            raise RuntimeError(
                str(capability.get("reason") or "harmony_vm_capability_unavailable")
            )
        tools, missing = find_harmony_tools()
        if tools is None:
            raise RuntimeError(f"missing Harmony VM tools: {', '.join(missing)}")
        # 空串表示"不传 -imageRoot，交给 DevEco 用它自己配置的镜像目录"。
        # 与 Android 同理：Android 不向 emulator 传 SDK 路径，由工具链自己解析。
        # 目录扫描只能作为定位辅助，扫不到不代表镜像不存在——官方的
        # phone_all_arm 一份镜像覆盖多种形态，目录名反推不出来。镜像是否可用
        # 已由能力探查用 DevEco 的已下载清单把过一道关（见 capability.py）。
        image_root = self._resolve_image_root(msg)

        with self._start_lock:
            existing = self._runtimes.get(vm_id)
            if existing is not None:
                return {
                    "hdc_serial": existing.hdc_serial,
                    "details": self._runtime_details(existing, reused=True),
                }
            conflict = self._port_conflict(port, serial)
            if conflict:
                raise RuntimeError(f"harmony_hdc_port_conflict:{port}:{conflict}")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.instance_path.mkdir(parents=True, exist_ok=True)
            instance_name = _safe_instance_name(vm_id)
            runtime = HarmonyVmRuntime(
                vm_id=vm_id,
                name=str(msg.get("alias") or msg.get("name") or vm_id),
                instance_name=instance_name,
                instance_path=str(self.instance_path.resolve()),
                image_root=image_root,
                hdc_port=port,
                hdc_serial=serial,
                lease_token=lease_token,
                started_at=time.time(),
            )
            self._runtimes[vm_id] = runtime
            register_managed_serial(serial)

        try:
            known = self._known.get(vm_id) or {}
            recorded_instance_path = str(
                known.get("instance_path") or ""
            ).strip()
            if (
                bool(known.get("created"))
                and recorded_instance_path
                and Path(recorded_instance_path) != Path(runtime.instance_path)
                and (
                    Path(recorded_instance_path)
                    / runtime.instance_name
                    / "config.ini"
                ).is_file()
            ):
                # Migrate definitions created by older ai-phone builds under
                # the repository path. DevEco can create/delete there but its
                # native boot path crashes on non-ASCII components.
                self._delete_instance_definition(
                    tools.emulator,
                    instance_name=runtime.instance_name,
                    instance_path=recorded_instance_path,
                )
                known = {**known, "created": False}
                self._known[vm_id] = known
                self._save_registry()
            instance_config = (
                Path(runtime.instance_path)
                / runtime.instance_name
                / "config.ini"
            )
            # Android checks the AVD on disk before launching.  DevEco may
            # return exit code 0 together with "Device create fail."; registry
            # state alone is therefore not creation evidence.
            if bool(known.get("created")) and not instance_config.is_file():
                logger.warning(
                    "Harmony VM registry says created but config.ini is absent; "
                    "recreating vm_id={} path={}",
                    vm_id,
                    instance_config,
                )
                known = {**known, "created": False}
                self._known[vm_id] = known
                self._save_registry()
            current_creation_config = _persistent_message_config(msg)
            previous_creation_config = known.get("last_config")
            if (
                bool(known.get("created"))
                and instance_config.is_file()
                and isinstance(previous_creation_config, dict)
                and previous_creation_config != current_creation_config
            ):
                # Android updates an existing AVD's config.ini before launch.
                # DevEco has no update command for create-time fields, so the
                # same managed instance must be explicitly recreated. Keeping
                # it would make Server show the new config while Emulator
                # silently continues with the old one.
                self._delete_instance_definition(
                    tools.emulator,
                    instance_name=runtime.instance_name,
                    instance_path=runtime.instance_path,
                )
                known = {**known, "created": False}
                self._known[vm_id] = known
                self._save_registry()
            if not bool(known.get("created")):
                self._create_instance(tools.emulator, runtime, msg)
                if not instance_config.is_file():
                    raise RuntimeError(
                        "harmony_vm_create_failed:"
                        f"instance_config_missing:{instance_config}"
                    )
                known = {
                    **runtime.persistent_dict(),
                    "created": True,
                    "last_config": _persistent_message_config(msg),
                }
                self._known[vm_id] = known
                self._save_registry()

            # 每次启动都写，而不是只在创建时写：这样 Server 改了全局 UUID 之后，
            # 已存在的实例下次启动就跟着生效，不用删掉重建。
            _apply_instance_uuid(
                instance_config,
                str(msg.get("instance_uuid") or ""),
                msg.get("retired_instance_uuids"),
            )

            log_dir = self.runtime_dir / "logs" / vm_id
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(log_dir / "emulator.log", "ab")  # noqa: SIM115
            runtime.log_file = log_file
            args = [
                tools.emulator,
                "-start",
                runtime.instance_name,
                "-instancePath",
                runtime.instance_path,
            ]
            if runtime.image_root:
                args.extend(["-imageRoot", runtime.image_root])
            args.extend(
                [
                    "-hdcPort",
                    str(runtime.hdc_port),
                    "-bootmode",
                    _boot_mode(str(msg.get("boot_mode") or "cold")),
                ]
            )
            runtime.process = subprocess.Popen(
                args, stdout=log_file, stderr=subprocess.STDOUT
            )
            boot_deadline = time.monotonic() + self.boot_timeout_sec
            self._wait_hdc(runtime, deadline=boot_deadline)
            runtime.driver = self._wait_driver(
                runtime,
                deadline=boot_deadline,
            )
            raw = runtime.driver.get_raw_driver()  # type: ignore[attr-defined]
            runtime.driver_fport_port = int(raw._client.local_port)  # noqa: SLF001
            set_managed_fport(runtime.hdc_serial, runtime.driver_fport_port)
            # 折叠形态必须在设备进入可调度状态之前落定，否则调度到的设备形态
            # 与用户所选不符；失败按硬失败处理，不静默回落到展开态。
            self._apply_fold_state(runtime, msg)
            runtime.ready = True
            runtime.missing_ticks = 0
            self._known[vm_id] = {
                **runtime.persistent_dict(),
                "created": True,
                "last_config": _persistent_message_config(msg),
            }
            self._save_registry()
            return {
                "hdc_serial": runtime.hdc_serial,
                "details": self._runtime_details(runtime),
            }
        except Exception as exc:
            cleanup = self._stop_runtime(runtime, keep_instance=True)
            wrapped = RuntimeError(str(exc))
            wrapped.cleanup_details = cleanup  # type: ignore[attr-defined]
            raise wrapped from exc

    def _apply_fold_state(
        self, runtime: HarmonyVmRuntime, msg: Dict[str, Any]
    ) -> None:
        """把折叠屏切到用户选定的初始形态。

        公开的 ``create``/``start`` CLI 没有初始折叠状态参数，实例配置里的
        ``hw.snap.fold.status`` 也只在快照恢复链路读取，完整冷启动会跳过。可用的
        官方通道是 DisplayManagerService 的 dump 开关：``-f`` 展开、``-m`` 折叠。
        本机实测（Mate X5 + HarmonyOS 6.0.31）来回切换四次，应用侧拿到的显示尺寸
        在内屏 2224×2496 与外屏 1080×2504 之间如实变化。

        因此这是启动后切换，不是启动参数——冷启动最初的一小段仍是展开态，
        但在设备对平台 ready 之前就会切到位。
        """
        config = (
            msg.get("config_json")
            if isinstance(msg.get("config_json"), dict)
            else {}
        )
        if str(msg.get("device_type") or "") != "Foldable":
            return
        folded_width = _int_value(
            (config.get("folded_screen") or {}).get("width"), 0
        )
        if not folded_width:
            # 没有外屏宽度就无法回读判定形态。宁可显式失败，也不要交付一台
            # 形态未经确认的设备。
            raise RuntimeError("harmony_fold_state_missing_folded_screen")
        fold = config.get("fold") if isinstance(config.get("fold"), dict) else {}
        state = str(fold.get("initial_state") or "unfolded").strip().lower()
        if state not in {"folded", "unfolded"}:
            raise RuntimeError(f"unsupported_harmony_fold_state:{state}")
        from ai_phone.agent.drivers.hdc import hdc_shell  # noqa: PLC0415

        folded = state == "folded"
        # 展开态也要走这条路。冷启动确实是展开的，但"确实"不等于"已确认"——
        # 不回读就无法保证交付的形态与用户所选一致。
        hdc_shell(
            runtime.hdc_serial,
            "hidumper -s DisplayManagerService -a "
            + (FOLD_DUMP_ARG_FOLDED if folded else FOLD_DUMP_ARG_UNFOLDED),
            timeout=15.0,
            check=False,
        )
        # dump 开关不返回结果，只能回读显示尺寸确认：折叠后宽度等于外屏宽度，
        # 展开后不等于。对不上说明这一版镜像不支持该开关，必须报错。
        for _ in range(FOLD_VERIFY_ATTEMPTS):
            time.sleep(FOLD_VERIFY_INTERVAL_SEC)
            reported = _last_display_size(runtime.hdc_serial)
            if reported and (reported[0] == folded_width) == folded:
                logger.info(
                    "harmony vm {} 折叠形态已确认为 {}（{}x{}）",
                    runtime.vm_id,
                    state,
                    reported[0],
                    reported[1],
                )
                return
        raise RuntimeError(f"harmony_fold_state_not_applied:{state}")

    def _create_instance(
        self, emulator: str, runtime: HarmonyVmRuntime, msg: Dict[str, Any]
    ) -> None:
        version_spec = format_os_version(
            str(msg.get("os_version") or ""),
            str(msg.get("api_version") or ""),
        )
        if not version_spec:
            raise RuntimeError("invalid_harmony_os_version")
        args = [
            emulator,
            "-create",
            runtime.instance_name,
            "-deviceType",
            str(msg.get("device_type") or "Phone"),
            "-osVersion",
            version_spec,
            "-instancePath",
            runtime.instance_path,
        ]
        if runtime.image_root:
            args.extend(["-imageRoot", runtime.image_root])
        config = (
            msg.get("config_json")
            if isinstance(msg.get("config_json"), dict)
            else {}
        )
        display_config = (
            config.get("display")
            if isinstance(config.get("display"), dict)
            else {}
        )
        screen_mode = str(display_config.get("mode") or "custom").strip().lower()
        screen_profile = str(msg.get("screen_profile") or "").strip()
        if screen_mode == "official_default":
            # DevEco 官方约定：-screenProfile 与 -screen 都不传时，根据
            # deviceType 自动选择默认机型。HarmonyOS 5.x 只能走这条路——机型名
            # 仍要留在实例记录里备查，但传给 CLI 会被直接拒绝。
            pass
        elif screen_profile:
            args.extend(["-screenProfile", screen_profile])
        else:
            if screen_mode not in {"", "custom"}:
                raise RuntimeError(
                    f"unsupported_harmony_screen_mode:{screen_mode}"
                )
            unfolded = " ".join(
                [
                    str(_int_value(msg.get("screen_width"), 1080)),
                    str(_int_value(msg.get("screen_height"), 2340)),
                    str(_int_value(msg.get("density"), 420)),
                    str(msg.get("screen_size_in") or "6.5"),
                ]
            )
            args.extend(["-screen", unfolded])
            if str(msg.get("device_type") or "") in {
                "Foldable",
                "WideFold",
                "TripleFold",
                "2in1 Foldable",
            }:
                folded = (
                    config.get("folded_screen")
                    if isinstance(config.get("folded_screen"), dict)
                    else {}
                )
                if not folded:
                    raise RuntimeError(
                        "foldable_custom_screen_requires_folded_screen"
                    )
                args.append(
                    " ".join(
                        [
                            str(_int_value(folded.get("width"), 1080)),
                            str(_int_value(folded.get("height"), 2480)),
                            str(_int_value(folded.get("density"), 480)),
                            str(folded.get("size_in") or "6.4"),
                        ]
                    )
                )
        args.extend(
            [
                "-storage",
                str(_int_value(msg.get("storage_gb"), 8)),
                "-memory",
                str(_int_value(msg.get("memory_gb"), 4)),
            ]
        )
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        output = "\n".join(
            part for part in (proc.stdout, proc.stderr) if part
        ).strip()
        if proc.returncode != 0 or _looks_like_cli_error(output):
            raise RuntimeError(
                f"harmony_vm_create_failed:rc={proc.returncode}:{output[-2000:]}"
            )

    def _wait_hdc(
        self,
        runtime: HarmonyVmRuntime,
        *,
        deadline: Optional[float] = None,
    ) -> None:
        deadline = deadline or (time.monotonic() + self.boot_timeout_sec)
        last_error = ""
        while time.monotonic() < deadline:
            if runtime.process is not None and runtime.process.poll() is not None:
                raise RuntimeError(
                    f"emulator exited during boot: {runtime.process.returncode}"
                )
            try:
                hdc_run("tconn", runtime.hdc_serial, timeout=3.0, check=False)
                targets = {
                    target.serial: target.status.lower()
                    for target in hdc_list_targets()
                }
                if targets.get(runtime.hdc_serial) == "connected":
                    # HDC appears several seconds before HarmonyOS finishes
                    # starting appfwk/launcher/uitest.  Treating Connected as
                    # "device ready" races hmdriver2.Driver.create: its socket
                    # accepts the connection but returns an empty payload, and
                    # the old startup path then shut down an otherwise healthy
                    # Emulator.  OpenHarmony publishes this parameter as true
                    # only after the system boot sequence is complete.
                    boot_state = hdc_run(
                        "shell",
                        "param get bootevent.boot.completed",
                        serial=runtime.hdc_serial,
                        timeout=3.0,
                        check=False,
                    )
                    if boot_state.strip().lower() == "true":
                        return
                    last_error = (
                        "hdc connected; "
                        f"bootevent.boot.completed={boot_state.strip() or 'empty'}"
                    )
                else:
                    last_error = (
                        f"target status={targets.get(runtime.hdc_serial, 'missing')}"
                    )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            time.sleep(1.0)
        raise TimeoutError(
            f"harmony_vm_boot_timeout:{runtime.hdc_serial}:{last_error}"
        )

    def _wait_driver(
        self,
        runtime: HarmonyVmRuntime,
        *,
        deadline: Optional[float] = None,
    ) -> BaseDriver:
        """Wait for the managed VM's real automation channel to become ready."""
        deadline = deadline or (time.monotonic() + self.boot_timeout_sec)
        last_error = ""
        attempts = 0
        while time.monotonic() < deadline:
            if runtime.process is not None and runtime.process.poll() is not None:
                raise RuntimeError(
                    f"emulator exited before driver ready: {runtime.process.returncode}"
                )
            driver: Optional[BaseDriver] = None
            try:
                attempts += 1
                driver = open_harmony_driver(runtime.hdc_serial)
                # This read-only call goes through hmdriver2/uitest and is the
                # authoritative hand-off point to the automation layer.
                driver.window_size()
                return driver
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}:{exc}"
                if driver is not None:
                    try:
                        driver.close()
                    except Exception:  # noqa: BLE001
                        pass
                if self._drop_driver_cache is not None:
                    try:
                        self._drop_driver_cache(runtime.hdc_serial)
                    except Exception:  # noqa: BLE001
                        pass
                # A failed hmdriver2 constructor may already have installed an
                # 8012 fport. Remove it before the bounded retry so each attempt
                # starts from one unambiguous automation channel.
                self._remove_fport(runtime)
                if attempts == 1 or attempts % 5 == 0:
                    logger.warning(
                        "Harmony VM 系统已启动但 Driver 尚未就绪，继续等待 "
                        "vm_id={} serial={} attempt={} error={}",
                        runtime.vm_id,
                        runtime.hdc_serial,
                        attempts,
                        last_error,
                    )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))
        raise TimeoutError(
            "harmony_vm_driver_timeout:"
            f"{runtime.hdc_serial}:attempts={attempts}:{last_error}"
        )

    def stop_sync(self, vm_id: str, hdc_serial: str = "") -> Dict[str, Any]:
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {
                "ok": False,
                "reason": "bad_request",
                "error": "vm_id required",
                "details": {"cleanup_confirmed": False},
            }
        with self._vm_lock(vm_id):
            runtime = self._runtimes.get(vm_id)
            if runtime is None:
                known = self._known.get(vm_id) or {}
                serial = hdc_serial or str(known.get("hdc_serial") or "")
                if not serial:
                    return {
                        "ok": True,
                        "reason": "not_running",
                        "hdc_serial": "",
                        "details": {"cleanup_confirmed": True},
                    }
                runtime = self._runtime_from_known(vm_id, known, serial)
            return self._stop_runtime(runtime, keep_instance=True)

    def _stop_runtime(
        self, runtime: HarmonyVmRuntime, *, keep_instance: bool
    ) -> Dict[str, Any]:
        self._runtimes.pop(runtime.vm_id, None)
        self._last_reclaimed_ids.discard(runtime.vm_id)
        if self._drop_driver_cache is not None:
            try:
                self._drop_driver_cache(runtime.hdc_serial)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "清理 Harmony VM 通用 driver cache 失败 serial={}: {}",
                    runtime.hdc_serial,
                    exc,
                )
        if runtime.driver is not None:
            try:
                runtime.driver.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 Harmony VM driver 失败：{}", exc)
        runtime.driver = None
        self._remove_fport(runtime)
        tools, _missing = find_harmony_tools()
        stop_errors: list[str] = []
        if tools is not None:
            try:
                proc = subprocess.run(
                    [tools.emulator, "-stop", runtime.instance_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if proc.returncode != 0:
                    stop_errors.append(
                        f"Emulator -stop rc={proc.returncode}:"
                        f"{(proc.stderr or proc.stdout or '').strip()[-500:]}"
                    )
            except Exception as exc:  # noqa: BLE001
                stop_errors.append(f"Emulator -stop: {exc}")
        else:
            stop_errors.append("DevEco Emulator missing during stop")
        if runtime.process is not None:
            try:
                if runtime.process.poll() is None:
                    runtime.process.terminate()
                    try:
                        runtime.process.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        runtime.process.kill()
            except Exception as exc:  # noqa: BLE001
                stop_errors.append(f"emulator process cleanup: {exc}")
        if runtime.log_file is not None:
            try:
                runtime.log_file.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            remove_output = hdc_run(
                "tconn",
                runtime.hdc_serial,
                "-remove",
                timeout=5.0,
                check=False,
            )
            if _looks_like_cli_error(remove_output):
                stop_errors.append(
                    f"hdc tconn -remove: {remove_output[-500:]}"
                )
        except Exception as exc:  # noqa: BLE001
            stop_errors.append(f"hdc tconn -remove: {exc}")
        cleanup_confirmed, cleanup_mode = self._wait_removed(
            runtime.hdc_serial,
            runtime.hdc_port,
        )
        unregister_managed_serial(runtime.hdc_serial)
        if keep_instance and runtime.vm_id in self._known:
            self._known[runtime.vm_id] = {
                **self._known[runtime.vm_id],
                "ready": False,
                "driver_fport_port": None,
            }
            self._save_registry()
        # 清理后的事实是停止成功的判据。HDC 若保留已离线 TCP target，
        # 仍会把残留 serial 明确上报给 Server 并从后续端口分配中排除。
        # CLI 的“本来就没在跑”等输出仍原样放在 stop_errors 里供排障，但不把
        # 已经确认干净的实例误送入租约隔离区。
        ok = cleanup_confirmed
        return {
            "ok": ok,
            "reason": "stopped" if ok else "cleanup_unconfirmed",
            "error": "; ".join(stop_errors),
            "hdc_serial": runtime.hdc_serial,
            "details": {
                "cleanup_confirmed": cleanup_confirmed,
                "cleanup_mode": cleanup_mode,
                "stale_hdc_target": (
                    runtime.hdc_serial
                    if cleanup_mode == "offline_hdc_target"
                    else ""
                ),
                "stop_errors": stop_errors,
                "hdc_port": runtime.hdc_port,
            },
        }

    def delete_sync(self, vm_id: str, hdc_serial: str = "") -> Dict[str, Any]:
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {
                "ok": False,
                "reason": "bad_request",
                "error": "vm_id required",
                "details": {"cleanup_confirmed": False},
            }
        with self._vm_lock(vm_id):
            known = self._known.get(vm_id) or {}
            stop = self.stop_sync(vm_id, hdc_serial)
            if not stop.get("ok"):
                return stop
            tools, missing = find_harmony_tools()
            if tools is None:
                return {
                    "ok": False,
                    "reason": "missing_tools",
                    "error": ", ".join(missing),
                    "details": {"cleanup_confirmed": False},
                }
            instance_name = str(known.get("instance_name") or _safe_instance_name(vm_id))
            instance_path = str(known.get("instance_path") or self.instance_path)
            # -delete 会交互询问 "do you really want to delete this device folder?"，
            # 非交互环境下不处理就会挂住或删不掉。这里用两道保险：
            #   -force  实测有效（跳过询问、目录确实清空），但官方 -help 未列出该
            #           参数，属未文档化行为，不能作为唯一依赖；
            #   stdin   同时喂入 "y"，即使某个版本移除了 -force 也能正常完成。
            proc = subprocess.run(
                [
                    tools.emulator,
                    "-delete",
                    instance_name,
                    "-instancePath",
                    instance_path,
                    "-force",
                ],
                check=False,
                capture_output=True,
                text=True,
                input="y\n",
                timeout=30,
            )
            output = "\n".join(
                part for part in (proc.stdout, proc.stderr) if part
            ).strip()
            # DevEco 的“实例不存在”属于幂等成功；其他错误不吞。
            not_found = any(
                marker in output.lower()
                for marker in ("not exist", "not found", "does not exist")
            )
            if proc.returncode != 0 and not not_found:
                return {
                    "ok": False,
                    "reason": "delete_failed",
                    "error": output[-2000:],
                    "details": {"cleanup_confirmed": True},
                }
            self._known.pop(vm_id, None)
            self._save_registry()
            return {
                "ok": True,
                "reason": "deleted",
                "details": {"cleanup_confirmed": True, "instance_name": instance_name},
            }

    def _delete_instance_definition(
        self,
        emulator: str,
        *,
        instance_name: str,
        instance_path: str | Path,
    ) -> None:
        instance_config = (
            Path(instance_path)
            / instance_name
            / "config.ini"
        )
        proc = subprocess.run(
            [
                emulator,
                "-delete",
                instance_name,
                "-instancePath",
                str(instance_path),
                "-force",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        output = "\n".join(
            part for part in (proc.stdout, proc.stderr) if part
        ).strip()
        not_found = any(
            marker in output.lower()
            for marker in ("not exist", "not found", "does not exist")
        )
        if (
            (proc.returncode != 0 and not not_found)
            or _looks_like_cli_error(output)
            or instance_config.exists()
        ):
            raise RuntimeError(
                "harmony_vm_reconfigure_delete_failed:"
                f"rc={proc.returncode}:{output[-2000:]}"
            )

    def reconcile_running_vms_sync(self) -> List[HarmonyVmRuntime]:
        connected = {
            target.serial
            for target in hdc_list_targets()
            if target.status.lower() == "connected"
        }
        adopted: List[HarmonyVmRuntime] = []
        for vm_id, known in list(self._known.items()):
            # stop 会保留实例定义以便下次启动，但会把 ready 持久化为 false。
            # 旧实例的 HDC 端口之后可能分配给另一台 VM；如果只凭
            # ``serial in connected`` 认领，就会把多条旧配置同时绑定到同一台
            # 新 VM。运行态在每次成功握手后都会持久化 ready=true，因此这里
            # 必须先用它区分“可恢复的运行实例”和“仅保留的停止配置”。
            if not bool(known.get("ready")):
                continue
            serial = str(known.get("hdc_serial") or "")
            token = str(known.get("lease_token") or "")
            if not serial or not token or serial not in connected:
                continue
            runtime = self._runtime_from_known(vm_id, known, serial)
            register_managed_serial(serial)
            try:
                runtime.driver = open_harmony_driver(serial)
                raw = runtime.driver.get_raw_driver()  # type: ignore[attr-defined]
                runtime.driver_fport_port = int(raw._client.local_port)  # noqa: SLF001
                set_managed_fport(serial, runtime.driver_fport_port)
                runtime.driver.window_size()
                runtime.ready = True
                self._runtimes[vm_id] = runtime
                self._known[vm_id] = {
                    **known,
                    **runtime.persistent_dict(),
                }
                adopted.append(runtime)
            except Exception as exc:  # noqa: BLE001
                unregister_managed_serial(serial)
                logger.error(
                    "Harmony VM 重连握手失败，不认领也不作为真机上报 vm_id={} serial={}: {}",
                    vm_id,
                    serial,
                    exc,
                )
        if adopted:
            self._save_registry()
        self._last_reclaimed_ids = {runtime.vm_id for runtime in adopted}
        return adopted

    async def report_reclaimed_vms(
        self, client: AgentWSClient, *, rescan: bool = True
    ) -> int:
        runtimes = (
            await asyncio.to_thread(self.reconcile_running_vms_sync)
            if rescan
            else [
                self._runtimes[vm_id]
                for vm_id in sorted(self._last_reclaimed_ids)
                if vm_id in self._runtimes
            ]
        )
        if runtimes:
            await client.send(
                {
                    "type": P.MSG_HARMONY_VM_RECONCILE,
                    "agent_id": client.agent_id,
                    "instances": [
                        {
                            "vm_id": runtime.vm_id,
                            "lease_token": runtime.lease_token,
                            "state": "running",
                            "hdc_serial": runtime.hdc_serial,
                            "details": self._runtime_details(
                                runtime, reclaimed=True
                            ),
                        }
                        for runtime in runtimes
                    ],
                }
            )
            await self._refresh_devices_safe(client)
        return len(runtimes)

    async def report_orphan_reconcile(self, client: AgentWSClient) -> int:
        if not self.orphan_cleanup:
            return 0
        rows = []
        for vm_id, known in self._known.items():
            instance_name = str(known.get("instance_name") or "")
            instance_path = str(known.get("instance_path") or "")
            instance_config = (
                Path(instance_path) / instance_name / "config.ini"
                if instance_name and instance_path
                else None
            )
            # 与 Android 的 avdmanager 实体清单一致：本地注册表只负责定位，
            # 不能单独作为所有权证据。实例配置已经不存在的旧记录不上报。
            if (
                instance_name != _safe_instance_name(vm_id)
                or instance_config is None
                or not instance_config.is_file()
            ):
                logger.warning(
                    "跳过不存在的 Harmony 受管实例对账 vm_id={} path={}",
                    vm_id,
                    str(instance_config or ""),
                )
                continue
            rows.append(
                {
                    "vm_id": vm_id,
                    "lease_token": str(known.get("lease_token") or ""),
                    "state": "running" if vm_id in self._runtimes else "stopped",
                    "hdc_serial": str(known.get("hdc_serial") or ""),
                    "details": {
                        "instance_name": instance_name,
                        "orphan_reconcile": True,
                        "managed_instance_present": True,
                    },
                }
            )
        await client.send(
            {
                "type": P.MSG_HARMONY_VM_RECONCILE,
                "agent_id": client.agent_id,
                "instances": rows,
            }
        )
        return len(rows)

    async def sweep_vanished_vms(
        self, client: AgentWSClient, present_serials: set[str]
    ) -> int:
        reported = 0
        for vm_id, runtime in list(self._runtimes.items()):
            if not runtime.ready:
                continue
            if runtime.hdc_serial in present_serials:
                runtime.missing_ticks = 0
                continue
            runtime.missing_ticks += 1
            if runtime.missing_ticks < 2:
                continue
            # “设备快照里消失”不等于“端口和进程已经干净”。走同一套 stop
            # 收口并以 target 消失 + 端口可 bind 为事实判据；确认不了就上报
            # error，让 Server 隔离租约，绝不直接当 stopped 释放。
            result = await asyncio.to_thread(
                self.stop_sync,
                vm_id,
                runtime.hdc_serial,
            )
            ok = bool(result.get("ok"))
            await client.send(
                self._status_payload(
                    {
                        "vm_id": vm_id,
                        "lease_token": runtime.lease_token,
                    },
                    state="stopped" if ok else "error",
                    ok=ok,
                    reason="vanished" if ok else str(
                        result.get("reason") or "cleanup_unconfirmed"
                    ),
                    error=str(result.get("error") or ""),
                    hdc_serial=str(result.get("hdc_serial") or ""),
                    details=dict(result.get("details") or {}),
                )
            )
            reported += 1
        return reported

    def stop_all(self) -> int:
        ids = list(self._runtimes)
        for vm_id in ids:
            self.stop_sync(vm_id)
        return len(ids)

    async def _refresh_devices_safe(self, client: AgentWSClient) -> None:
        try:
            await client.refresh_devices()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Harmony VM 刷新设备快照失败：{}", exc)

    def _runtime_details(
        self,
        runtime: HarmonyVmRuntime,
        *,
        reused: bool = False,
        reclaimed: bool = False,
    ) -> Dict[str, Any]:
        return {
            "instance_name": runtime.instance_name,
            "instance_path": runtime.instance_path,
            "hdc_port": runtime.hdc_port,
            "hdc_serial": runtime.hdc_serial,
            "driver_fport_port": runtime.driver_fport_port,
            "resolved_abi": host_abi(),
            "reused": reused,
            "reclaimed": reclaimed,
            "cleanup_confirmed": True,
            **self._acceleration_details(runtime),
        }

    def _acceleration_details(self, runtime: HarmonyVmRuntime) -> Dict[str, Any]:
        """Read a bounded log tail; unknown stays unknown instead of being guessed."""
        path = self.runtime_dir / "logs" / runtime.vm_id / "emulator.log"
        try:
            if runtime.log_file is not None:
                runtime.log_file.flush()
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 256 * 1024))
                raw = stream.read().decode("utf-8", errors="replace").lower()
        except OSError:
            raw = ""

        evidence: list[str] = []
        cpu = "unknown"
        if any(marker in raw for marker in ("hypervisor.framework", " hvf", "applehv")):
            cpu = "hardware"
            evidence.append("emulator_log:hypervisor")
        elif re.search(r"\btcg\b", raw):
            cpu = "software"
            evidence.append("emulator_log:tcg")

        gpu = "unknown"
        for marker, label in (
            ("swiftshader", "SwiftShader"),
            ("angle", "ANGLE"),
            ("metal", "Metal"),
            ("opengl", "OpenGL"),
        ):
            if marker in raw:
                gpu = label
                evidence.append(f"emulator_log:{marker}")
                break
        return {
            "requested_acceleration": "auto",
            "acceleration_selectable": False,
            "detected_cpu_acceleration": cpu,
            "detected_gpu_renderer": gpu,
            "acceleration_evidence": evidence,
        }

    def _runtime_from_known(
        self, vm_id: str, known: Dict[str, Any], serial: str
    ) -> HarmonyVmRuntime:
        port = _serial_port(serial) or _int_value(known.get("hdc_port"), 0)
        return HarmonyVmRuntime(
            vm_id=vm_id,
            name=str(known.get("name") or vm_id),
            instance_name=str(
                known.get("instance_name") or _safe_instance_name(vm_id)
            ),
            instance_path=str(known.get("instance_path") or self.instance_path),
            image_root=str(known.get("image_root") or ""),
            hdc_port=port,
            hdc_serial=serial,
            lease_token=str(known.get("lease_token") or ""),
            driver_fport_port=(
                _int_value(known.get("driver_fport_port"), 0) or None
            ),
            started_at=float(known.get("started_at") or time.time()),
            ready=bool(known.get("ready")),
        )

    def _remove_fport(self, runtime: HarmonyVmRuntime) -> None:
        ports: set[int] = set()
        if runtime.driver_fport_port:
            ports.add(runtime.driver_fport_port)
        try:
            raw = hdc_run(
                "fport", "ls", serial=runtime.hdc_serial, timeout=3.0, check=False
            )
            ports.update(
                int(local)
                for local in re.findall(r"tcp:(\d+)\s+tcp:8012", raw or "")
            )
        except Exception:  # noqa: BLE001
            pass
        for port in ports:
            try:
                hdc_run(
                    "fport",
                    "rm",
                    f"tcp:{port}",
                    "tcp:8012",
                    serial=runtime.hdc_serial,
                    timeout=3.0,
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("清理 fport {} 失败：{}", port, exc)

    def _wait_removed(self, serial: str, port: int) -> tuple[bool, str]:
        deadline = time.monotonic() + 15.0
        offline_ticks = 0
        while time.monotonic() < deadline:
            try:
                targets = {
                    target.serial: target.status.lower()
                    for target in hdc_list_targets()
                }
                port_free = self._port_is_free(port)
                status = targets.get(serial)
                if status is None and port_free:
                    return True, "removed"
                if status == "offline" and port_free:
                    offline_ticks += 1
                    # DevEco HDC 6.1 keeps dead TCP targets and rejects
                    # `tconn <serial> -remove` with "No target available".
                    # Four stable observations plus a bindable port prove no
                    # Emulator/listener remains. The stale target is surfaced
                    # and its port remains excluded from future Server leases.
                    if offline_ticks >= 4:
                        return True, "offline_hdc_target"
                else:
                    offline_ticks = 0
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "等待 Harmony VM 清理确认失败 serial={}: {}",
                    serial,
                    exc,
                )
            time.sleep(0.5)
        return False, "unconfirmed"

    def _port_conflict(self, port: int, expected_serial: str) -> str:
        # 判据必须与能力探查完全一致：只认 Connected。
        # hdc 会长期保留已消失实例的 Offline target（官方 tconn -remove 删不掉），
        # 但实测这些端口没有任何进程监听，是陈旧记账而非真实占用。若这里把 Offline
        # 也算冲突，就会形成闭环：探查报"空闲" -> Server 分配 -> 启动又拒绝 ->
        # 该端口从此永久不可用。真实占用由下面的 bind 检测负责，那才是硬证据。
        targets = {
            target.serial
            for target in hdc_list_targets()
            if target.status.strip().lower() == "connected"
        }
        if expected_serial in targets:
            return f"hdc target already exists:{expected_serial}"
        if not self._port_is_free(port):
            return "tcp listener already exists"
        return ""

    @staticmethod
    def _port_is_free(port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    str(key): value
                    for key, value in raw.items()
                    if isinstance(value, dict)
                }
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Harmony VM registry 无法读取，不会猜测认领实例 path={}: {}",
                self.registry_path,
                exc,
            )
        return {}

    def _save_registry(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._known, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.registry_path)

    @staticmethod
    def _status_payload(
        msg: Dict[str, Any],
        *,
        state: str,
        ok: bool,
        reason: str,
        error: str = "",
        hdc_serial: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "type": P.MSG_HARMONY_VM_STATUS,
            "request_id": msg.get("request_id") or "",
            "vm_id": msg.get("vm_id") or "",
            "lease_token": msg.get("lease_token") or "",
            "state": state,
            "ok": ok,
            "reason": reason,
            "error": error,
            "hdc_serial": hdc_serial,
            "details": details or {},
        }


def _persistent_message_config(msg: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "device_type",
        "os_version",
        "api_version",
        "abi",
        "image_id",
        "screen_profile",
        "screen_width",
        "screen_height",
        "density",
        "screen_size_in",
        "memory_gb",
        "storage_gb",
        "config_json",
    )
    return {key: msg.get(key) for key in keys}


def _safe_instance_name(vm_id: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_]+", "_", vm_id)[:32]
    return f"{_INSTANCE_PREFIX}{suffix}"


def _boot_mode(value: str) -> str:
    mapping = {
        # 产品只暴露“完整冷启动”。映射到已完成双实例实测的
        # coldboot_no_save：不复用/写入快速启动快照，但 userdata（已装 APP 与数据）
        # 仍属于实例磁盘，不会被 reset。
        "cold": "coldboot_no_save",
        "coldboot": "coldboot",
        "cold_no_save": "coldboot_no_save",
        "coldboot_no_save": "coldboot_no_save",
        "reset": "reset",
        "reset_no_save": "reset_no_save",
        "snapshot": "snapshot",
    }
    normalized = (value or "cold").strip().lower()
    if normalized not in mapping:
        raise ValueError(f"unsupported Harmony boot mode: {value}")
    return mapping[normalized]


def _serial_port(serial: str) -> int:
    match = re.fullmatch(r"(?:127\.0\.0\.1|localhost):(\d+)", serial or "")
    return int(match.group(1)) if match else 0


def _apply_instance_uuid(
    config_path: Path,
    instance_uuid: str,
    retired_instance_uuids: object = None,
) -> None:
    """把 Server 下发的固定 UUID 写进实例 config.ini。

    DevEco 的设备 UDID 就是这个字段拼出来的（固定前缀 + uuid 去横杠大写 + 补零），
    而 ``-create`` 每次随机生成它，导致每台虚拟机 UDID 都不同。内测分发的应用要
    按 UDID 报备设备，逐台报备在批量起虚拟机的场景下没法用；固定住之后报备一次
    即可。

    留空时，仅当实例当前 UUID 命中过去的共享 UUID，才生成一份新的实例独立 UUID；
    从未启用共享身份的实例保持 DevEco 默认值。写入失败按硬失败处理：静默跳过会
    让页面显示已恢复默认、实际仍沿用共享身份。
    """
    try:
        value = normalize_harmony_instance_uuid(instance_uuid)
    except ValueError as exc:
        raise RuntimeError(f"invalid_harmony_instance_uuid:{instance_uuid}") from exc
    retired_values: set[str] = set()
    if isinstance(retired_instance_uuids, (list, tuple, set)):
        for candidate in retired_instance_uuids:
            try:
                normalized = normalize_harmony_instance_uuid(candidate)
            except ValueError as exc:
                raise RuntimeError(
                    f"invalid_retired_harmony_instance_uuid:{candidate}"
                ) from exc
            if normalized:
                retired_values.add(normalized)
    if not value and not retired_values:
        return
    if not config_path.is_file():
        raise RuntimeError(f"harmony_instance_config_missing:{config_path}")
    text = config_path.read_text(encoding="utf-8", errors="replace")
    current_match = re.search(r"(?m)^uuid\s*=\s*([^\s#;]+)", text)
    if not value:
        current_value = (
            current_match.group(1).strip().lower() if current_match else ""
        )
        if current_value not in retired_values:
            return
        value = str(uuid.uuid4())
    patched, count = re.subn(
        r"^uuid=.*$", f"uuid={value}", text, count=1, flags=re.MULTILINE
    )
    if count != 1:
        raise RuntimeError("harmony_instance_config_has_no_uuid_field")
    if patched != text:
        temporary = config_path.with_name(
            f".{config_path.name}.{os.getpid()}.uuid.tmp"
        )
        try:
            temporary.write_text(patched, encoding="utf-8")
            os.chmod(temporary, config_path.stat().st_mode)
            os.replace(temporary, config_path)
        except OSError as exc:
            raise RuntimeError(
                f"harmony_instance_uuid_write_failed:{config_path}"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    verified = config_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?m)^uuid\s*=\s*([^\s#;]+)", verified)
    if not match or match.group(1).strip().lower() != value:
        raise RuntimeError(f"harmony_instance_uuid_verify_failed:{config_path}")
    logger.info("harmony 实例 UUID 已设置为 {}", value)


def _last_display_size(serial: str) -> Optional[tuple[int, int]]:
    """读回 DisplayManagerService 最近一次上报的显示尺寸。

    折叠开关是 dump 命令，没有返回值，只能从 DMS 自己的事件记录里回读确认。
    """
    from ai_phone.agent.drivers.hdc import hdc_shell  # noqa: PLC0415

    try:
        out = hdc_shell(
            serial,
            "hidumper -s DisplayManagerService -a -a",
            timeout=10.0,
            check=False,
        ) or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取鸿蒙显示尺寸失败 {}", exc)
        return None
    found = re.findall(r"width:\s*(\d+)\s+height:\s*(\d+)", out)
    return (int(found[-1][0]), int(found[-1][1])) if found else None


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _looks_like_cli_error(output: str) -> bool:
    lower = (output or "").lower()
    return any(
        marker in lower
        for marker in (
            "error:",
            "[fail]",
            "failed to",
            " create fail",
            "unknown screenprofile",
            "can not ",
            "cannot ",
            "license agreement",
        )
    )


def _log_task_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Harmony VM background task failed: {}", exc)


__all__ = ["HarmonyVmManager", "HarmonyVmRuntime"]
