"""Agent-side lifecycle manager for DevEco HarmonyOS virtual machines."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from ai_phone.agent.drivers import open_harmony_driver
from ai_phone.agent.drivers.base import BaseDriver, DeviceInfo
from ai_phone.agent.drivers.hdc import hdc_list_targets, hdc_run
from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.agent.vm_ports import loopback_port_available
from ai_phone.agent.android_vm.capability import host_abi
from ai_phone.config import get_settings
from ai_phone.shared import protocol as P
from ai_phone.shared.harmony_identity import normalize_harmony_instance_uuid

from .capability import (
    HDC_PORT_MAX,
    HDC_PORT_MIN,
    HarmonyVmTools,
    discover_harmony_image_roots,
    discover_harmony_instance_root,
    find_harmony_tools,
    format_os_version,
    probe_harmony_vm_capability,
    scan_downloaded_images,
    startup_timeout,
)
from .registry import (
    managed_vm_owner,
    register_managed_serial,
    register_managed_vm,
    set_managed_fport,
    unregister_managed_serial,
    unregister_managed_vm,
    vm_device_identity,
)


_INSTANCE_PREFIX = "aiphone_harmony_"

# DisplayManagerService 的 dump 开关。官方 create/start CLI 没有公开初始折叠状态
# 参数，这是实测可用的唯一通道：-m 折叠（外屏）、-f 展开（内屏）。
FOLD_DUMP_ARG_FOLDED = "-m"
FOLD_DUMP_ARG_UNFOLDED = "-f"
FOLD_VERIFY_ATTEMPTS = 6
FOLD_VERIFY_INTERVAL_SEC = 1.0


def _spawn_harmony_emulator(
    args: List[str],
    log_file: Any,
    *,
    auto_accept_agreements: bool,
    vm_id: str,
) -> subprocess.Popen:
    """Start DevEco and optionally answer its agreement prompt once."""
    if not auto_accept_agreements:
        return subprocess.Popen(
            args,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    stream = process.stdin
    if stream is None:
        logger.warning(
            "Harmony VM 协议自动确认已开启，但 Emulator stdin 不可用 "
            "vm_id={}",
            vm_id,
        )
        return process

    sent = False
    try:
        stream.write(b"y\n")
        stream.flush()
        sent = True
    except (OSError, ValueError) as exc:
        # BrokenPipeError is an OSError. A version that does not prompt may
        # close stdin immediately and still boot normally, so keep tracking
        # the real process and let the existing process/HDC/driver gates decide.
        logger.warning(
            "Harmony VM 向 Emulator 写入协议确认失败 "
            "vm_id={} error={}:{}",
            vm_id,
            type(exc).__name__,
            exc,
        )
    finally:
        try:
            stream.close()
        except (OSError, ValueError) as exc:
            logger.warning(
                "Harmony VM 关闭 Emulator stdin 失败 "
                "vm_id={} error={}:{}",
                vm_id,
                type(exc).__name__,
                exc,
            )
    if sent:
        logger.info(
            "Harmony VM 已向 Emulator stdin 发送一次协议确认 vm_id={}",
            vm_id,
        )
    return process


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
    observed_request_id: str = ""
    created_in_attempt: Optional[bool] = None
    operation_deadline: Optional[float] = field(default=None, repr=False)
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    driver: Optional[BaseDriver] = field(default=None, repr=False)
    log_file: Optional[Any] = field(default=None, repr=False)
    log_start_offset: int = 0
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
            "observed_request_id": self.observed_request_id,
            **({"created_in_attempt": self.created_in_attempt} if isinstance(self.created_in_attempt, bool) else {}),
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
        self._registry_lock = threading.RLock()
        self._registry_readable = True
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

    @staticmethod
    def _instance_observation(
        vm_id: str, known: Dict[str, Any], *, state: str = "unknown",
        resources_released: bool = False, reason: str = "instance_unverified",
    ) -> Dict[str, Any]:
        return {
            "vm_id": vm_id,
            "state": state,
            "identity": {
                key: str(known.get(key) or "")
                for key in ("instance_name", "instance_path", "lease_token", "hdc_serial")
            },
            "resources_released": resources_released,
            "reason": reason,
        }

    def observe_instance(
        self, vm_id: str, deadline: Optional[float] = None
    ) -> Dict[str, Any]:
        """Observe the exact local instance without changing a VM or registry."""
        vm_id = str(vm_id or "").strip()
        known = dict(self._known.get(vm_id) or {})
        runtime = self._runtimes.get(vm_id)
        if runtime is not None:
            known.update(runtime.persistent_dict())
        observation = self._instance_observation(vm_id, known)
        lock = self._vm_lock(vm_id)
        if not lock.acquire(blocking=False):
            observation["reason"] = "operation_in_progress"
            return observation
        try:
            startup_timeout(deadline, 1.0)
            known = dict(self._known.get(vm_id) or {})
            runtime = self._runtimes.get(vm_id)
            if runtime is not None:
                known.update(runtime.persistent_dict())
            observation = self._instance_observation(vm_id, known)
            if not self._registry_readable:
                observation["reason"] = "registry_unreadable"
                return observation
            if not known:
                observation["reason"] = "instance_not_recorded"
                return observation
            name = str(known.get("instance_name") or "")
            path = str(known.get("instance_path") or "")
            if name != _safe_instance_name(vm_id) or not path:
                observation["reason"] = "instance_identity_unverified"
                return observation
            active = (
                runtime.hdc_serial
                if runtime is not None and runtime.process is not None and runtime.process.poll() is None
                else self._active_instance_serial(vm_id, known)
            )
            startup_timeout(deadline, 1.0)
            if active is None:
                observation["reason"] = "instance_process_unknown"
                return observation
            if active:
                observation.update(state="running", reason="instance_process_running")
                return observation
            instance_dir = Path(path) / name
            exists = instance_dir.exists()
            observation["state"] = "stopped" if exists else "missing"
            if exists and not (instance_dir / "config.ini").is_file():
                observation["reason"] = "instance_definition_unverified"
                return observation
            serial = str(known.get("hdc_serial") or "")
            token = str(known.get("lease_token") or "")
            port = _serial_port(serial)
            if not port or port != _int_value(known.get("hdc_port"), 0):
                observation["reason"] = "transport_identity_unverified"
                return observation
            owner = managed_vm_owner(serial)
            if owner is not None and owner != (vm_id, token):
                observation["reason"] = "transport_owned_by_another_vm"
                return observation
            startup_timeout(deadline, 1.0)
            try:
                targets = hdc_list_targets()
            except Exception:  # noqa: BLE001
                startup_timeout(deadline, 1.0)
                observation["reason"] = "hdc_query_failed"
                return observation
            startup_timeout(deadline, 1.0)
            statuses = {target.status.strip().lower() for target in targets if target.serial == serial}
            if statuses - {"offline"}:
                observation["reason"] = "hdc_transport_present"
                return observation
            ports = {port}
            fport = _int_value(known.get("driver_fport_port"), 0)
            if fport:
                ports.add(fport)
            for current_port in ports:
                startup_timeout(deadline, 1.0)
                free = self._port_is_free(current_port)
                startup_timeout(deadline, 1.0)
                if not free:
                    observation["reason"] = "transport_port_unavailable"
                    return observation
            owner = managed_vm_owner(serial)
            if owner is not None and owner != (vm_id, token):
                observation["reason"] = "transport_owned_by_another_vm"
                return observation
            observation.update(resources_released=True, reason="instance_process_absent_resources_released")
            return observation
        except TimeoutError:
            observation.update(state="unknown", resources_released=False, reason="observation_deadline")
            return observation
        except Exception:  # noqa: BLE001
            observation.update(state="unknown", resources_released=False, reason="observation_failed")
            return observation
        finally:
            lock.release()

    def _observation_from_exit(
        self, vm_id: str, known: Dict[str, Any], evidence: Dict[str, Any],
        *, resources_released: bool, reason: str,
    ) -> Dict[str, Any]:
        """Convert this call's exit sample, never a saved registry sample."""
        state = evidence.get("instance_process_status")
        if evidence.get("process_handle_present"):
            state = "running" if evidence.get("process_return_code") is None else "absent"
        if state == "absent":
            name = str(known.get("instance_name") or "")
            path = str(known.get("instance_path") or "")
            state = (
                "stopped" if (Path(path) / name).exists() else "missing"
            ) if name == _safe_instance_name(vm_id) and path else "unknown"
            if state == "stopped" and not (Path(path) / name / "config.ini").is_file():
                resources_released, reason = False, "instance_definition_unverified"
        if state not in {"running", "stopped", "missing"}:
            state = "unknown"
        return self._instance_observation(
            vm_id, known, state=state,
            resources_released=resources_released and state in {"stopped", "missing"}, reason=reason,
        )

    async def _failure_details(self, vm_id: str, details: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(details)
        if "instance_observation" not in result:
            result["instance_observation"] = await asyncio.to_thread(
                self.observe_instance, vm_id, time.monotonic() + 5.0
            )
        return result

    def decorate_devices(self, infos: List[DeviceInfo]) -> List[DeviceInfo]:
        serial_to_runtime = {
            runtime.hdc_serial: runtime
            for runtime in self._runtimes.copy().values()
            if runtime.ready
            and runtime.hdc_serial
            and managed_vm_owner(runtime.hdc_serial)
            == (runtime.vm_id, runtime.lease_token)
        }
        known_managed_serials = {
            str(row.get("hdc_serial") or "")
            for row in self._known.copy().values()
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
                    "hdc_serial": runtime.hdc_serial,
                    "lease_token": runtime.lease_token,
                }
            )
            info.serial = vm_device_identity(runtime.vm_id)
            info.extra = extra
            out.append(info)
        return out

    def probe(
        self, requirement: Dict[str, Any], *, inspection_deadline: Optional[float] = None
    ) -> Dict[str, Any]:
        if requirement.get("inspect_instance") is True:
            # Monotonic timestamps are local to this process, never trusted
            # from an incoming capability message.
            observation = self.observe_instance(
                str(requirement.get("vm_id") or ""),
                _inspection_deadline(requirement) if inspection_deadline is None else inspection_deadline,
            )
            return {
                "ok": observation["state"] != "unknown",
                "reason": observation["reason"],
                "details": {"instance_observation": observation},
            }
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
            for row in self._known.copy().values()
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
        if msg.get("inspect_instance") is True:
            result = await asyncio.to_thread(
                self.probe, msg, inspection_deadline=_inspection_deadline(msg)
            )
        else:
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
        # The WebSocket dispatcher queues lifecycle commands per VM. Keep the
        # operation in that queue until its terminal result has been sent.
        await self._start_and_report(client, msg)

    async def _start_and_report(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        try:
            # Include executor queue time as well as native startup time.
            # start_sync retains this earlier deadline instead of resetting
            # the budget when a worker thread finally becomes available.
            operation_msg = {**msg, "_operation_deadline": _start_deadline(msg)}
            result = await asyncio.to_thread(self.start_sync, operation_msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Harmony VM 启动失败 vm_id={}: {}", msg.get("vm_id"), exc)
            cleanup = getattr(exc, "cleanup_details", {})
            details = await self._failure_details(str(msg.get("vm_id") or ""), dict(cleanup or {}))
            await client.send(
                self._status_payload(
                    msg,
                    state="error",
                    ok=False,
                    reason="start_failed",
                    error=str(exc),
                    details=details,
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
        vm_id = str(msg.get("vm_id") or "")
        serial = str(msg.get("hdc_serial") or "")
        deadline: Optional[float] = None
        try:
            # WS already deducted its queue time. Progress, executor and locks
            # all consume this same local deadline, never a network timestamp.
            deadline = _stop_deadline(msg)
            await asyncio.wait_for(
                client.send(self._status_payload(msg, state="stopping", ok=True, reason="stopping")),
                timeout=_stop_timeout(deadline, 120.0),
            )
            result = await asyncio.to_thread(
                self.stop_sync, vm_id, serial, str(msg.get("lease_token") or ""), deadline=deadline,
            )
        except (TimeoutError, ValueError) as exc:
            result = self._stop_budget_failure(vm_id, serial, exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Harmony VM 停止收口异常 vm_id={}", vm_id)
            result = {
                "ok": False, "reason": "stop_failed", "error": str(exc),
                "details": {"cleanup_confirmed": False},
            }
        if not result.get("ok") and result.get("reason") not in {"operation_timeout", "bad_request"}:
            # A failed operation may be observed only within its original
            # budget. Expiry never opens another five-second inspection.
            if deadline is not None and time.monotonic() >= deadline:
                result = self._stop_budget_failure(vm_id, serial, TimeoutError("stop deadline exceeded"))
            else:
                details = dict(result.get("details") or {})
                if "instance_observation" not in details:
                    details["instance_observation"] = await asyncio.to_thread(self.observe_instance, vm_id, deadline)
                result["details"] = details
                if deadline is not None and time.monotonic() >= deadline:
                    result = self._stop_budget_failure(vm_id, serial, TimeoutError("stop deadline exceeded"))
        await client.send(self._status_payload(
            msg, state="stopped" if result.get("ok") else "error",
            ok=bool(result.get("ok")), reason=str(result.get("reason") or ""),
            error=str(result.get("error") or ""), hdc_serial=str(result.get("hdc_serial") or ""),
            details=dict(result.get("details") or {}),
        ))
        if deadline is None or time.monotonic() < deadline:
            await self._refresh_devices_safe(client)

    async def handle_delete(
        self, client: AgentWSClient, msg: Dict[str, Any]
    ) -> None:
        try:
            result = await asyncio.to_thread(
                self.delete_sync,
                str(msg.get("vm_id") or ""),
                str(msg.get("hdc_serial") or ""),
                str(msg.get("lease_token") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Harmony VM 删除收口异常 vm_id={}", msg.get("vm_id"))
            result = {
                "ok": False,
                "reason": "delete_failed",
                "error": str(exc),
                "details": {"cleanup_confirmed": False},
            }
        if not result.get("ok"):
            result["details"] = await self._failure_details(
                str(msg.get("vm_id") or ""), dict(result.get("details") or {})
            )
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
        deadline = _start_deadline(msg)
        msg = {**msg, "_operation_deadline": deadline}
        vm_id = str(msg.get("vm_id") or "").strip()
        if not vm_id:
            raise ValueError("vm_id is required")
        if not self._registry_readable:
            raise RuntimeError("harmony_vm_registry_unreadable")
        try:
            with _startup_lock(self._vm_lock(vm_id), deadline):
                startup_timeout(deadline, 1.0)
                return self._start_sync_locked(vm_id, msg)
        except Exception as exc:
            if not hasattr(exc, "cleanup_details"):
                # Capability/port validation or queue expiry can fail before
                # this attempt owns resources. A computed instance name is
                # not proof that an instance was created.
                known = self._known.get(vm_id) or {}
                instance_path = Path(str(known.get("instance_path") or self.instance_path))
                instance_dir = instance_path / _safe_instance_name(vm_id)
                exc.cleanup_details = {  # type: ignore[attr-defined]
                    "created_in_attempt": False,
                    "instance_created": (instance_dir / "config.ini").is_file(),
                    "cleanup_confirmed": (
                        vm_id not in self._runtimes and not known and not instance_dir.exists()
                    ),
                }
            if isinstance(exc, TimeoutError):
                # asyncio/concurrent.futures may normalize TimeoutError and
                # discard custom attributes across to_thread. Preserve the
                # terminal cleanup evidence in a regular operation error.
                wrapped = RuntimeError(str(exc))
                wrapped.cleanup_details = exc.cleanup_details  # type: ignore[attr-defined]
                raise wrapped from exc
            raise

    def _start_sync_locked(
        self, vm_id: str, msg: Dict[str, Any]
    ) -> Dict[str, Any]:
        deadline = msg.get("_operation_deadline")
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
        known = self._known.get(vm_id) or {}
        if known.get("rollback_pending"):
            raise RuntimeError("harmony_vm_cleanup_required")
        stale_runtime = existing is not None and (
            existing.lease_token != lease_token or not existing.ready or existing.missing_ticks >= 2
        )
        if msg.get("require_existing_instance") or known.get("cleanup_pending") or stale_runtime:
            with _startup_lock(self._start_lock, deadline):
                observation = self.observe_instance(vm_id, deadline)
                first_bind = msg.get("rollback_new_instance") is True and not msg.get("require_existing_instance")
                if first_bind or observation["state"] != "stopped" or not observation["resources_released"]:
                    reason = (
                        "harmony_vm_transport_unavailable" if existing is not None and observation["state"] == "running"
                        else "harmony_vm_instance_missing" if observation["state"] == "missing"
                        else "harmony_vm_instance_owner_unverified" if observation["reason"] in {"instance_not_recorded", "instance_identity_unverified"}
                        else "harmony_vm_cleanup_required"
                    )
                    failure = RuntimeError(f"{reason}:{observation['reason']}")
                    failure.cleanup_details = {  # type: ignore[attr-defined]
                        "cleanup_confirmed": False, "created_in_attempt": False,
                        "instance_created": (
                            Path(str(known.get("instance_path") or self.instance_path))
                            / _safe_instance_name(vm_id) / "config.ini"
                        ).is_file(),
                        "instance_observation": observation,
                    }
                    raise failure
                # This is an explicit start, after current process/transport
                # absence was established. Only expired local bookkeeping is
                # removed; no native stop/delete or definition rewrite occurs.
                prior = {**known, **(existing.persistent_dict() if existing is not None else {})}
                unregister_managed_vm(vm_id, str(prior.get("lease_token") or ""))
                prior_serial = str(prior.get("hdc_serial") or "")
                if managed_vm_owner(prior_serial) is None:
                    unregister_managed_serial(prior_serial)
                self._runtimes.pop(vm_id, None)
                self._last_reclaimed_ids.discard(vm_id)
                if self._drop_driver_cache is not None:
                    self._drop_driver_cache(vm_device_identity(vm_id))
                self._known[vm_id] = {
                    **prior, "ready": False, "cleanup_pending": False,
                    "cleanup_confirmed": True, "driver_fport_port": None,
                }
                self._save_registry()
                existing = None
        if existing is not None:
            return self._reuse_started_runtime(existing, msg)

        startup_timeout(deadline, 1.0)
        capability = self.probe(msg)
        startup_timeout(deadline, 1.0)
        if not capability.get("ok"):
            raise RuntimeError(
                str(capability.get("reason") or "harmony_vm_capability_unavailable")
            )
        probe_details = capability.get("details") or {}
        probed_tool = str((probe_details.get("tools") or {}).get("emulator") or "")
        tools, missing = (HarmonyVmTools(probed_tool), []) if probed_tool else find_harmony_tools()
        startup_timeout(deadline, 1.0)
        if tools is None:
            raise RuntimeError(f"missing Harmony VM tools: {', '.join(missing)}")
        # 空串表示"不传 -imageRoot，交给 DevEco 用它自己配置的镜像目录"。
        # 与 Android 同理：Android 不向 emulator 传 SDK 路径，由工具链自己解析。
        # 目录扫描只能作为定位辅助，扫不到不代表镜像不存在——官方的
        # phone_all_arm 一份镜像覆盖多种形态，目录名反推不出来。镜像是否可用
        # 已由能力探查用 DevEco 的已下载清单把过一道关（见 capability.py）。
        image_root = self._resolve_image_root(msg)
        startup_timeout(deadline, 1.0)

        with _startup_lock(self._start_lock, deadline):
            existing = self._runtimes.get(vm_id)
            if existing is not None:
                return self._reuse_started_runtime(existing, msg)
            conflict = self._port_conflict(port, serial)
            startup_timeout(deadline, 1.0)
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
                observed_request_id=str(msg.get("request_id") or ""),
                created_in_attempt=False,
                operation_deadline=deadline,
                started_at=time.time(),
            )
            self._runtimes[vm_id] = runtime
            register_managed_vm(
                vm_id,
                serial,
                lease_token,
                folded_screen_width=_managed_folded_screen_width(msg),
            )
            register_managed_serial(serial)

        rollback_new_definition = False
        try:
            known = self._known.get(vm_id) or {}
            recorded_instance_path = str(
                known.get("instance_path") or ""
            ).strip()
            if (
                not known.get("created")
                and known.get("instance_name") == runtime.instance_name
                and recorded_instance_path
                and (Path(recorded_instance_path) / runtime.instance_name / "config.ini").is_file()
            ):
                # Older registries may lack the created bit. Exact recorded
                # ownership plus the real definition preserves upgrade
                # compatibility without guessing from the generated name.
                known = {**known, "created": True}
                self._known[vm_id] = known
            if bool(known.get("created")) and recorded_instance_path:
                # A changed host default must not move/recreate a user's
                # existing instance or discard its private data on restart.
                runtime.instance_path = recorded_instance_path
                runtime.image_root = str(known.get("image_root") or image_root)
                if not recorded_instance_path.isascii():
                    raise RuntimeError("harmony_vm_instance_path_unsupported:non_ascii")
            instance_config = (
                Path(runtime.instance_path)
                / runtime.instance_name
                / "config.ini"
            )
            rollback_new_definition = (
                bool(msg.get("rollback_new_instance"))
                and not bool(msg.get("require_existing_instance"))
                and not bool(known.get("created"))
                and not instance_config.parent.exists()
            )
            if bool(msg.get("require_existing_instance")) and not bool(known.get("created")):
                raise RuntimeError(f"harmony_vm_instance_owner_unverified:{instance_config}")
            # Android checks the AVD on disk before launching.  DevEco may
            # return exit code 0 together with "Device create fail."; registry
            # state alone is therefore not creation evidence.
            if bool(known.get("created")) and not instance_config.is_file():
                raise RuntimeError(f"harmony_vm_instance_missing:{instance_config}")
            current_creation_config = _persistent_message_config(msg)
            previous_creation_config = known.get("last_config")
            if (
                bool(known.get("created"))
                and instance_config.is_file()
                and isinstance(previous_creation_config, dict)
                and previous_creation_config != current_creation_config
            ):
                # DevEco has no in-place update for create-time fields. A
                # restart must never silently delete apps/data to apply one.
                raise RuntimeError("harmony_vm_config_changed:create_new_instance")
            if not bool(known.get("created")):
                startup_timeout(deadline, 1.0)
                definition_absent_before_create = not instance_config.parent.exists()
                try:
                    self._create_instance(tools.emulator, runtime, msg)
                finally:
                    if definition_absent_before_create:
                        try:
                            runtime.created_in_attempt = instance_config.is_file()
                        except OSError:
                            # An unknown post-create state is not positive
                            # evidence authorizing deletion of private data.
                            runtime.created_in_attempt = None
                startup_timeout(deadline, 1.0)
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
            startup_timeout(deadline, 1.0)

            log_dir = self.runtime_dir / "logs" / vm_id
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = open(log_dir / "emulator.log", "ab")  # noqa: SIM115
            runtime.log_file = log_file
            runtime.log_start_offset = log_file.tell()
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
            startup_timeout(deadline, 1.0)
            runtime.process = _spawn_harmony_emulator(
                args,
                log_file,
                auto_accept_agreements=bool(
                    get_settings().harmony_auto_accept_agreements
                ),
                vm_id=runtime.vm_id,
            )
            startup_timeout(deadline, 1.0)
            boot_deadline = time.monotonic() + self.boot_timeout_sec
            if deadline is not None:
                boot_deadline = min(boot_deadline, deadline)
            self._wait_hdc(runtime, deadline=boot_deadline)
            startup_timeout(deadline, 1.0)
            runtime.driver = self._wait_driver(
                runtime,
                deadline=boot_deadline,
            )
            startup_timeout(deadline, 1.0)
            raw = runtime.driver.get_raw_driver()  # type: ignore[attr-defined]
            startup_timeout(deadline, 1.0)
            runtime.driver_fport_port = int(raw._client.local_port)  # noqa: SLF001
            set_managed_fport(runtime.hdc_serial, runtime.driver_fport_port)
            # 折叠形态必须在设备进入可调度状态之前落定，否则调度到的设备形态
            # 与用户所选不符；失败按硬失败处理，不静默回落到展开态。
            self._apply_fold_state(runtime, msg)
            startup_timeout(deadline, 1.0)
            runtime.ready = True
            runtime.missing_ticks = 0
            self._known[vm_id] = {
                **runtime.persistent_dict(),
                "created": True,
                "last_config": _persistent_message_config(msg),
            }
            self._save_registry()
            details = self._runtime_details(runtime)
            startup_timeout(deadline, 1.0)
            return {
                "hdc_serial": runtime.hdc_serial,
                "details": details,
            }
        except Exception as exc:
            cleanup = self._cleanup_failed_start(
                runtime, rollback_new_definition=rollback_new_definition
            )
            wrapped = RuntimeError(str(exc))
            wrapped.cleanup_details = cleanup  # type: ignore[attr-defined]
            raise wrapped from exc

    def _cleanup_failed_start(
        self, runtime: HarmonyVmRuntime, *, rollback_new_definition: bool
    ) -> Dict[str, Any]:
        # One cleanup transaction, not an automatic retry. Existing instances
        # keep their definition/data when a restart fails. Only a definition
        # created by this first-bind attempt may be deleted here.
        if rollback_new_definition:
            self._known[runtime.vm_id] = {
                **self._known.get(runtime.vm_id, {}),
                **runtime.persistent_dict(),
                "rollback_pending": True,
            }
        try:
            if rollback_new_definition:
                result = self.delete_sync(runtime.vm_id, runtime.hdc_serial, runtime.lease_token)
            else:
                result = self._stop_runtime(runtime, keep_instance=True)
            details = dict(result.get("details") or {})
            if not result.get("ok"):
                details["cleanup_confirmed"] = False
                details["cleanup_error"] = str(result.get("error") or result.get("reason") or "cleanup_failed")
        except Exception as cleanup_exc:  # noqa: BLE001
            details = {"cleanup_confirmed": False, "cleanup_error": str(cleanup_exc)}
        created = self._instance_definition_present(runtime)
        details["instance_created"] = created
        if isinstance(runtime.created_in_attempt, bool):
            details["created_in_attempt"] = runtime.created_in_attempt
        if created:
            details["instance_name"] = runtime.instance_name
            details["instance_path"] = runtime.instance_path
        details["rollback_new_instance"] = rollback_new_definition
        if rollback_new_definition:
            complete = bool(details.get("cleanup_confirmed")) and not created
            details["rollback_completed"] = complete
            details["cleanup_confirmed"] = complete
            if not complete:
                self._known[runtime.vm_id] = {
                    **self._known.get(runtime.vm_id, {}),
                    **runtime.persistent_dict(),
                    "ready": False,
                    "rollback_pending": True,
                    "cleanup_pending": True,
                    "cleanup_confirmed": False,
                    "created": created,
                }
                try:
                    self._save_registry()
                except Exception as save_exc:  # noqa: BLE001
                    details["registry_error"] = str(save_exc)
        return details

    @staticmethod
    def _instance_definition_present(runtime: HarmonyVmRuntime) -> bool:
        return (
            runtime.instance_name == _safe_instance_name(runtime.vm_id)
            and (Path(runtime.instance_path) / runtime.instance_name / "config.ini").is_file()
        )

    def _reuse_started_runtime(
        self, runtime: HarmonyVmRuntime, msg: Dict[str, Any]
    ) -> Dict[str, Any]:
        deadline = msg.get("_operation_deadline")
        startup_timeout(deadline, 1.0)
        if runtime.lease_token != str(msg.get("lease_token") or "") or runtime.hdc_port != _int_value(msg.get("assigned_port"), 0):
            raise RuntimeError("running VM lease does not match start request")
        if not runtime.ready or runtime.missing_ticks >= 2:
            raise RuntimeError("harmony_vm_transport_unavailable:stop_before_retry")
        observed_request_id = str(msg.get("request_id") or runtime.observed_request_id)
        if observed_request_id != runtime.observed_request_id or runtime.created_in_attempt is not False:
            runtime.observed_request_id = observed_request_id
            runtime.created_in_attempt = False
            self._known[runtime.vm_id] = {
                **self._known.get(runtime.vm_id, {}),
                "observed_request_id": observed_request_id,
                "created_in_attempt": False,
            }
            self._save_registry()
        details = self._runtime_details(runtime, reused=True)
        startup_timeout(deadline, 1.0)
        return {
            "hdc_serial": runtime.hdc_serial,
            "details": details,
        }

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
            timeout=startup_timeout(runtime.operation_deadline, 15.0),
            check=False,
        )
        startup_timeout(runtime.operation_deadline, 1.0)
        # dump 输出和 DMS 的历史尺寸都不能证明当前活动屏幕的形态。
        # window_size 每次清掉 hmdriver2 的 display_size 缓存，连续两次回读
        # 当前可操作屏宽，避免把一次短暂切换误报为已稳定就绪。
        if runtime.driver is None:
            raise RuntimeError("harmony_fold_state_driver_unavailable")
        confirmed = 0
        for _ in range(FOLD_VERIFY_ATTEMPTS):
            time.sleep(startup_timeout(runtime.operation_deadline, FOLD_VERIFY_INTERVAL_SEC))
            startup_timeout(runtime.operation_deadline, 1.0)
            try:
                reported = runtime.driver.window_size()
            except Exception as exc:  # noqa: BLE001
                # 形态切换时 uitest 可短暂重连；留在有限确认窗口内重试。
                confirmed = 0
                logger.debug(
                    "harmony vm {} 折叠形态实时尺寸暂不可读：{}",
                    runtime.vm_id,
                    exc,
                )
                continue
            startup_timeout(runtime.operation_deadline, 1.0)
            if (
                reported[0] > 0
                and reported[1] > 0
                and (reported[0] == folded_width) == folded
            ):
                confirmed += 1
            else:
                confirmed = 0
            if confirmed >= 2:
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
            timeout=startup_timeout(runtime.operation_deadline, 120.0),
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
                if self._emulator_agreement_required(runtime):
                    raise RuntimeError(
                        "harmony_vm_agreement_required: DevEco Emulator "
                        "requires local agreement confirmation"
                    )
                raise RuntimeError(
                    f"emulator exited during boot: {runtime.process.returncode}"
                )
            try:
                hdc_run("tconn", runtime.hdc_serial, timeout=startup_timeout(deadline, 3.0), check=False)
                startup_timeout(deadline, 1.0)
                targets = {
                    target.serial: target.status.lower()
                    for target in hdc_list_targets()
                }
                startup_timeout(deadline, 1.0)
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
                        timeout=startup_timeout(deadline, 3.0),
                        check=False,
                    )
                    startup_timeout(deadline, 1.0)
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
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))
        raise TimeoutError(
            f"harmony_vm_boot_timeout:{runtime.hdc_serial}:{last_error}"
        )

    def _emulator_agreement_required(self, runtime: HarmonyVmRuntime) -> bool:
        path = self.runtime_dir / "logs" / runtime.vm_id / "emulator.log"
        try:
            if runtime.log_file is not None:
                runtime.log_file.flush()
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(runtime.log_start_offset, stream.tell() - 16 * 1024))
                tail = stream.read().decode("utf-8", errors="replace").lower()
        except OSError:
            return False
        return "please agree to the agreement first" in tail

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
                startup_timeout(deadline, 1.0)
                driver = open_harmony_driver(runtime.hdc_serial)
                startup_timeout(deadline, 1.0)
                # This read-only call goes through hmdriver2/uitest and is the
                # authoritative hand-off point to the automation layer.
                driver.window_size()
                startup_timeout(deadline, 1.0)
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

    def stop_sync(
        self, vm_id: str, hdc_serial: str = "", lease_token: str = "",
        *, deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {
                "ok": False,
                "reason": "bad_request",
                "error": "vm_id required",
                "details": {"cleanup_confirmed": False},
            }
        if not self._registry_readable:
            return self._stop_rejected("registry_unreadable", hdc_serial)
        try:
            with _stop_lock(self._vm_lock(vm_id), deadline):
                runtime = self._runtimes.get(vm_id)
                if runtime is None:
                    known = self._known.get(vm_id) or {}
                    known_token = str(known.get("lease_token") or "")
                    known_serial = str(known.get("hdc_serial") or "")
                    if lease_token and lease_token != known_token:
                        return self._stop_rejected("lease_mismatch", known_serial)
                    if hdc_serial and known_serial and hdc_serial != known_serial:
                        return self._stop_rejected("hdc_mismatch", known_serial)
                    # A stored confirmation describes an earlier observation. An
                    # explicit stop must verify current identity/transport facts,
                    # including registries written by older Agent versions.
                    inspection_started = time.monotonic()
                    inspection_unix = time.time()
                    _stop_timeout(deadline, 1.0)
                    serial = (self._active_instance_serial(vm_id, known) if deadline is None else
                              self._active_instance_serial(vm_id, known, deadline=deadline))
                    _stop_timeout(deadline, 1.0)
                    if serial is None:
                        return self._stop_rejected("instance_owner_unverified", known_serial)
                    if not serial:
                        owner = managed_vm_owner(known_serial)
                        port = _serial_port(known_serial)
                        exit_evidence: Dict[str, Any] = {
                            "source": "registry_instance_absence",
                            "hdc_serial": known_serial,
                            "hdc_port": port,
                            "started_unix": inspection_unix,
                            "instance_process_status": "absent",
                            "process_handle_present": False,
                            "sample_count": 1,
                            "last_sample": {"target_status": "not_checked_owned_transport", "port_bindable": None},
                        }
                        if owner is None:
                            _stop_timeout(deadline, 1.0)
                            target_rows = (hdc_list_targets() if deadline is None else
                                           hdc_list_targets(deadline=deadline, strict=True))
                            _stop_timeout(deadline, 1.0)
                            targets = {
                                target.serial: target.status.lower() for target in target_rows
                            }
                            connected = any(
                                target.serial == known_serial and target.status.lower() == "connected"
                                for target in target_rows
                            )
                            target_status = "connected" if connected else targets.get(known_serial)
                            port_bindable = (
                                self._port_is_free(port) if port and not connected else None
                            )
                            _stop_timeout(deadline, 1.0)
                            exit_evidence["last_sample"] = {
                                "target_status": target_status if target_status is not None else "absent",
                                "port_bindable": port_bindable,
                                "duration_sec": round(time.monotonic() - inspection_started, 3),
                            }
                            exit_evidence["elapsed_sec"] = round(time.monotonic() - inspection_started, 3)
                            if connected or (port and not port_bindable):
                                rejected = self._stop_rejected(
                                    "instance_absence_unconfirmed", known_serial
                                )
                                rejected["details"]["exit_evidence"] = exit_evidence
                                rejected["details"]["instance_observation"] = self._observation_from_exit(
                                    vm_id, known, exit_evidence, resources_released=False,
                                    reason="instance_absence_unconfirmed",
                                )
                                return rejected
                        exit_evidence["elapsed_sec"] = round(time.monotonic() - inspection_started, 3)
                        _stop_timeout(deadline, 1.0)
                        crash_clean, crash_errors = (
                            self._cleanup_crash_service(vm_id, known) if deadline is None else
                            self._cleanup_crash_service(vm_id, known, deadline=deadline)
                        )
                        _stop_timeout(deadline, 1.0)
                        if not crash_clean:
                            logger.warning(
                                "Harmony VM crash-service 清理未确认 vm_id={} errors={}",
                                vm_id,
                                crash_errors,
                            )
                        self._known[vm_id] = {
                            **known,
                            "ready": False,
                            "cleanup_pending": False,
                            "cleanup_confirmed": True,
                            "exit_evidence": exit_evidence,
                        }
                        self._save_registry()
                        _stop_timeout(deadline, 1.0)
                        unregister_managed_vm(vm_id, known_token)
                        return {
                            "ok": True,
                            "reason": "not_running",
                            "hdc_serial": known_serial,
                            "details": {
                                "cleanup_confirmed": True, "exit_evidence": exit_evidence,
                                "instance_observation": self._observation_from_exit(
                                    vm_id, known, exit_evidence,
                                    resources_released=owner is None, reason="not_running",
                                ),
                            },
                        }
                    if serial != known_serial:
                        return self._stop_rejected("instance_hdc_mismatch", known_serial)
                    runtime = self._runtime_from_known(vm_id, known, serial)
                elif lease_token and lease_token != runtime.lease_token:
                    return self._stop_rejected("lease_mismatch", runtime.hdc_serial)
                elif hdc_serial and hdc_serial != runtime.hdc_serial:
                    return self._stop_rejected("hdc_mismatch", runtime.hdc_serial)
                return self._stop_runtime(runtime, keep_instance=True, deadline=deadline)
        except TimeoutError as exc:
            if deadline is None:
                raise
            return self._stop_budget_failure(vm_id, hdc_serial, exc)

    def _stop_budget_failure(self, vm_id: str, serial: str, exc: Exception) -> Dict[str, Any]:
        runtime = self._runtimes.get(vm_id)
        known = dict(self._known.get(vm_id) or {})
        if runtime is not None:
            known.update(runtime.persistent_dict())
        reason = "operation_timeout" if isinstance(exc, TimeoutError) else "bad_request"
        return {
            "ok": False, "reason": reason, "error": str(exc),
            "hdc_serial": str(known.get("hdc_serial") or serial),
            "details": {
                "cleanup_confirmed": False, "cleanup_pending": True, "exit_evidence": {},
                "instance_observation": self._instance_observation(vm_id, known, reason=reason),
            },
        }

    def _stop_runtime(
        self, runtime: HarmonyVmRuntime, *, keep_instance: bool, deadline: Optional[float] = None
    ) -> Dict[str, Any]:
        with _stop_lock(self._start_lock, deadline):
            return self._stop_runtime_locked(runtime, keep_instance=keep_instance, deadline=deadline)

    @staticmethod
    def _stop_rejected(reason: str, hdc_serial: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "reason": reason,
            "error": reason,
            "hdc_serial": hdc_serial,
            "details": {"cleanup_confirmed": False},
        }

    def _stop_runtime_locked(
        self, runtime: HarmonyVmRuntime, *, keep_instance: bool, deadline: Optional[float] = None
    ) -> Dict[str, Any]:
        _stop_timeout(deadline, 1.0)
        owner = managed_vm_owner(runtime.hdc_serial)
        if owner is not None and owner != (runtime.vm_id, runtime.lease_token):
            return self._stop_rejected("hdc_owned_by_another_vm", runtime.hdc_serial)
        # Keep the exact runtime/lease identity until exit is confirmed.
        self._runtimes[runtime.vm_id] = runtime
        runtime.ready = False
        self._last_reclaimed_ids.discard(runtime.vm_id)
        if self._drop_driver_cache is not None:
            try:
                _stop_timeout(deadline, 1.0)
                self._drop_driver_cache(vm_device_identity(runtime.vm_id))
                _stop_timeout(deadline, 1.0)
            except Exception as exc:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                logger.warning(
                    "清理 Harmony VM 通用 driver cache 失败 serial={}: {}",
                    runtime.hdc_serial,
                    exc,
                )
        if runtime.driver is not None:
            try:
                _stop_timeout(deadline, 1.0)
                runtime.driver.close()
                _stop_timeout(deadline, 1.0)
            except Exception as exc:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                logger.debug("关闭 Harmony VM driver 失败：{}", exc)
        _stop_timeout(deadline, 1.0)
        runtime.driver = None
        if deadline is None:
            self._remove_fport(runtime)
        else:
            self._remove_fport(runtime, deadline=deadline)
        _stop_timeout(deadline, 1.0)
        tools, _missing = find_harmony_tools() if deadline is None else find_harmony_tools(deadline=deadline)
        _stop_timeout(deadline, 1.0)
        stop_errors: list[str] = []
        if tools is not None:
            try:
                proc = subprocess.run(
                    [tools.emulator, "-stop", runtime.instance_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=_stop_timeout(deadline, 20.0),
                )
                _stop_timeout(deadline, 1.0)
                if proc.returncode != 0:
                    stop_errors.append(
                        f"Emulator -stop rc={proc.returncode}:"
                        f"{(proc.stderr or proc.stdout or '').strip()[-500:]}"
                    )
            except Exception as exc:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                stop_errors.append(f"Emulator -stop: {exc}")
        else:
            stop_errors.append("DevEco Emulator missing during stop")
        _stop_timeout(deadline, 1.0)
        if runtime.process is not None:
            try:
                if runtime.process.poll() is None:
                    _stop_timeout(deadline, 1.0)
                    runtime.process.terminate()
                    try:
                        runtime.process.wait(timeout=_stop_timeout(deadline, 5.0))
                    except Exception:  # noqa: BLE001
                        _stop_timeout(deadline, 1.0)
                        _stop_timeout(deadline, 1.0)
                        runtime.process.kill()
                        runtime.process.wait(timeout=_stop_timeout(deadline, 5.0))
            except Exception as exc:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                stop_errors.append(f"emulator process cleanup: {exc}")
        _stop_timeout(deadline, 1.0)
        if runtime.log_file is not None:
            try:
                runtime.log_file.close()
            except Exception:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                pass
        try:
            remove_output = hdc_run(
                "tconn",
                runtime.hdc_serial,
                "-remove",
                timeout=_stop_timeout(deadline, 5.0),
                check=False,
            )
            _stop_timeout(deadline, 1.0)
            if _looks_like_cli_error(remove_output):
                stop_errors.append(
                    f"hdc tconn -remove: {remove_output[-500:]}"
                )
        except Exception as exc:  # noqa: BLE001
            _stop_timeout(deadline, 1.0)
            stop_errors.append(f"hdc tconn -remove: {exc}")
        _stop_timeout(deadline, 1.0)
        self._last_removal_evidence: Dict[str, Any] = {}
        cleanup_confirmed, cleanup_mode = (
            self._wait_removed(runtime.hdc_serial, runtime.hdc_port) if deadline is None else
            self._wait_removed(runtime.hdc_serial, runtime.hdc_port, deadline=deadline)
        )
        _stop_timeout(deadline, 1.0)
        process_return_code = runtime.process.poll() if runtime.process is not None else None
        exit_evidence = {
            **self._last_removal_evidence,
            "process_handle_present": runtime.process is not None,
            "process_return_code": process_return_code,
            "process_sampled_unix": time.time(),
        }
        if runtime.process is not None and process_return_code is None:
            cleanup_confirmed = False
            cleanup_mode = "process_still_running"
            stop_errors.append("emulator process is still running")
        elif runtime.process is None:
            # A reconnected Agent has no Popen handle. Transport removal alone
            # cannot prove that its exact native instance stopped successfully.
            active_serial = (
                self._active_instance_serial(runtime.vm_id, runtime.persistent_dict()) if deadline is None else
                self._active_instance_serial(runtime.vm_id, runtime.persistent_dict(), deadline=deadline)
            )
            _stop_timeout(deadline, 1.0)
            exit_evidence["instance_process_status"] = (
                "unknown" if active_serial is None
                else "running" if active_serial else "absent"
            )
            exit_evidence["instance_process_sampled_unix"] = time.time()
            if active_serial != "":
                cleanup_confirmed = False
                cleanup_mode = (
                    "process_exit_unconfirmed" if active_serial is None
                    else "process_still_running"
                )
                stop_errors.append(
                    "exact emulator process exit could not be confirmed"
                    if active_serial is None else "exact emulator process is still running"
                )
        _stop_timeout(deadline, 1.0)
        crash_clean, crash_errors = (
            self._cleanup_crash_service(runtime.vm_id, runtime.persistent_dict()) if deadline is None else
            self._cleanup_crash_service(runtime.vm_id, runtime.persistent_dict(), deadline=deadline)
        )
        _stop_timeout(deadline, 1.0)
        stop_errors.extend(crash_errors)
        if not crash_clean:
            logger.warning(
                "Harmony VM crash-service 清理未确认 vm_id={} errors={}",
                runtime.vm_id,
                crash_errors,
            )
        if keep_instance:
            # A failed stop has already dropped the live driver and VM route,
            # but the old process/HDC address may still be present. Persist a
            # separate pending bit even when startup failed before this VM was
            # first written to the registry. A retry must recheck ownership
            # and port absence instead of treating ready=False as proof that
            # cleanup finished.
            self._known[runtime.vm_id] = {
                **(self._known.get(runtime.vm_id) or {}),
                **runtime.persistent_dict(),
                "ready": False,
                "driver_fport_port": None if cleanup_confirmed else runtime.driver_fport_port,
                "cleanup_pending": not cleanup_confirmed,
                "cleanup_confirmed": cleanup_confirmed,
                "exit_evidence": exit_evidence,
            }
            self._save_registry()
        _stop_timeout(deadline, 1.0)
        if cleanup_confirmed:
            self._runtimes.pop(runtime.vm_id, None)
            unregister_managed_vm(runtime.vm_id, runtime.lease_token)
            if managed_vm_owner(runtime.hdc_serial) is None:
                unregister_managed_serial(runtime.hdc_serial)
        # 清理后的事实是停止成功的判据。HDC 若保留已离线 TCP target，
        # 残留 serial 只作为诊断上报；后续分配仍按租约和实时端口检查判断。
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
                "crash_service_cleanup_confirmed": crash_clean,
                "hdc_port": runtime.hdc_port,
                "exit_evidence": exit_evidence,
                "instance_observation": self._observation_from_exit(
                    runtime.vm_id, runtime.persistent_dict(), exit_evidence,
                    resources_released=cleanup_confirmed, reason=cleanup_mode,
                ),
            },
        }



    def delete_sync(
        self, vm_id: str, hdc_serial: str = "", lease_token: str = ""
    ) -> Dict[str, Any]:
        vm_id = (vm_id or "").strip()
        if not vm_id:
            return {
                "ok": False,
                "reason": "bad_request",
                "error": "vm_id required",
                "details": {"cleanup_confirmed": False},
            }
        if not self._registry_readable:
            return self._stop_rejected("registry_unreadable", hdc_serial)
        with self._vm_lock(vm_id):
            known = self._known.get(vm_id) or {}
            stop = self.stop_sync(vm_id, hdc_serial, lease_token)
            if not stop.get("ok"):
                return stop
            stopped_exit = {key: stop["details"][key]
                            for key in ("exit_evidence", "instance_observation")
                            if key in (stop.get("details") or {})}
            tools, missing = find_harmony_tools()
            if tools is None:
                return {
                    "ok": False,
                    "reason": "missing_tools",
                    "error": ", ".join(missing),
                    "details": {"cleanup_confirmed": False, **stopped_exit},
                }
            instance_name = str(known.get("instance_name") or _safe_instance_name(vm_id))
            instance_path = str(known.get("instance_path") or self.instance_path)
            if instance_name != _safe_instance_name(vm_id):
                return {
                    "ok": False,
                    "reason": "instance_owner_mismatch",
                    "error": f"registry instance does not belong to {vm_id}",
                    "details": {"cleanup_confirmed": False, **stopped_exit},
                }
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
                    "details": {"cleanup_confirmed": False, **stopped_exit},
                }
            instance_dir = Path(instance_path) / instance_name
            if instance_dir.exists():
                return {
                    "ok": False,
                    "reason": "delete_unconfirmed",
                    "error": f"instance directory still exists: {instance_dir}",
                    "details": {"cleanup_confirmed": False, **stopped_exit},
                }
            crash_clean, crash_errors = self._cleanup_crash_service(vm_id, known)
            if not crash_clean:
                logger.warning(
                    "Harmony VM 删除后 crash-service 清理未确认 vm_id={} errors={}",
                    vm_id,
                    crash_errors,
                )
            self._known.pop(vm_id, None)
            self._save_registry()
            return {
                "ok": True,
                "reason": "deleted",
                "details": {
                    **stopped_exit,
                    "instance_observation": self._instance_observation(
                        vm_id, known, state="missing", resources_released=True, reason="deleted"
                    ),
                    "cleanup_confirmed": True,
                    "crash_service_cleanup_confirmed": crash_clean,
                    "crash_service_cleanup_errors": crash_errors,
                    "instance_name": instance_name,
                    "instance_directory_absent": True,
                },
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
        with self._start_lock:
            return self._reconcile_running_vms_sync_locked()

    def _reconcile_running_vms_sync_locked(self) -> List[HarmonyVmRuntime]:
        connected = {
            target.serial
            for target in hdc_list_targets()
            if target.status.lower() == "connected"
        }
        adopted: List[HarmonyVmRuntime] = []
        for vm_id, known in list(self._known.items()):
            # A failed first-bind rollback remains protected. Other historical
            # ready/cleanup flags do not supersede the exact process + HDC
            # identity and successful driver handshake checked below.
            if known.get("rollback_pending"):
                continue
            observation = self.observe_instance(vm_id, time.monotonic() + 5.0)
            if observation["state"] != "running":
                continue
            serial = str(known.get("hdc_serial") or "")
            token = str(known.get("lease_token") or "")
            if not serial or not token or serial not in connected:
                continue
            if observation["identity"]["hdc_serial"] != serial or observation["identity"]["lease_token"] != token:
                logger.warning(
                    "Harmony VM 不凭复用 HDC 端口认领：实例进程未核实 "
                    "vm_id={} serial={}", vm_id, serial,
                )
                continue
            owner = managed_vm_owner(serial)
            if owner is not None and owner != (vm_id, token):
                continue
            runtime = self._runtime_from_known(vm_id, known, serial)
            register_managed_serial(serial)
            try:
                runtime.driver = open_harmony_driver(serial)
                raw = runtime.driver.get_raw_driver()  # type: ignore[attr-defined]
                runtime.driver_fport_port = int(raw._client.local_port)  # noqa: SLF001
                set_managed_fport(serial, runtime.driver_fport_port)
                runtime.driver.window_size()
                fold_config = known.get("last_config")
                folded_width = _managed_folded_screen_width(fold_config)
                if folded_width:
                    # 只对有可信旧配置的受管 Foldable 外屏重验。Agent 重启期间
                    # 屏幕可能已被系统电源状态改回内屏，不能凭旧 ready 标志认领。
                    self._apply_fold_state(runtime, fold_config)
                runtime.ready = True
                register_managed_vm(
                    vm_id,
                    serial,
                    token,
                    folded_screen_width=folded_width,
                )
                self._runtimes[vm_id] = runtime
                self._known[vm_id] = {
                    **known,
                    **runtime.persistent_dict(),
                }
                adopted.append(runtime)
            except Exception as exc:  # noqa: BLE001
                if managed_vm_owner(serial) is None:
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
        unknown_vm_ids: set[str] = set()
        for vm_id in list(self._known):
            observation = await asyncio.to_thread(
                self.observe_instance, vm_id, time.monotonic() + 5.0
            )
            observed = observation["state"]
            identity = observation["identity"]
            name, path = identity["instance_name"], identity["instance_path"]
            if observed == "unknown":
                unknown_vm_ids.add(vm_id)
            if name != _safe_instance_name(vm_id) or not path:
                unknown_vm_ids.add(vm_id)
                continue
            if observed == "missing":
                # The complete inventory's omission reports a proven absent
                # definition; do not present a missing directory as an entity.
                continue
            exists = (Path(path) / name / "config.ini").is_file()
            if observed == "unknown" and not exists:
                continue
            # Old ready/cleanup bits are historical observations. They cannot
            # overwrite current process facts or certify a missing definition.
            details = {
                "instance_name": name,
                "instance_path": path,
                "orphan_reconcile": True,
                "managed_instance_present": exists,
                "instance_created": exists,
                "cleanup_confirmed": bool(observation["resources_released"]),
                "instance_observation": observation,
            }
            known = self._known.get(vm_id) or {}
            for key in ("cleanup_pending", "rollback_pending"):
                if known.get(key):
                    details[key] = True
            rows.append({
                "vm_id": vm_id,
                "lease_token": identity["lease_token"],
                "hdc_serial": identity["hdc_serial"],
                "state": observed if observed in {"running", "stopped"} else "error",
                "error": observation["reason"] if observed in {"missing", "unknown"} else "",
                "details": details,
            })
        complete, untracked = self._inventory_completeness()
        await client.send(
            {
                "type": P.MSG_HARMONY_VM_RECONCILE,
                "agent_id": client.agent_id,
                "instances": rows,
                "inventory_complete": complete,
                "unknown_vm_ids": sorted(unknown_vm_ids),
                "untracked_instance_names": untracked,
            }
        )
        return len(rows)

    def _stopped_cleanup_confirmed(self, vm_id: str, known: Dict[str, Any]) -> bool:
        observation = self.observe_instance(vm_id, time.monotonic() + 5.0)
        return observation["state"] == "stopped" and bool(observation["resources_released"])

    def _inventory_completeness(self) -> tuple[bool, list[str]]:
        known = self._known.copy()
        roots = {self.instance_path, *(Path(str(row["instance_path"])) for row in known.values() if row.get("instance_path"))}
        tracked = {
            str((Path(str(row.get("instance_path") or self.instance_path)) / _safe_instance_name(vm_id)).resolve())
            for vm_id, row in known.items()
        }
        untracked: list[str] = []
        complete = self._registry_readable
        for root in roots:
            try:
                for path in root.iterdir():
                    if path.name.startswith(_INSTANCE_PREFIX) and path.is_dir() and str(path.resolve()) not in tracked:
                        untracked.append(str(path))
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
        # A platform-created directory without its owner record must not be
        # inferred as another VM from its lossy/truncated name.
        return complete and not untracked, sorted(untracked)

    async def sweep_vanished_vms(
        self, client: AgentWSClient, present_serials: set[str]
    ) -> int:
        reported = 0
        for vm_id, runtime in list(self._runtimes.items()):
            status = self._presence_status(
                runtime, present=vm_device_identity(vm_id) in present_serials
            )
            if status is not None:
                await client.send(status)
                reported += 1
        return reported

    def _presence_status(
        self, runtime: HarmonyVmRuntime, *, present: bool
    ) -> Optional[Dict[str, Any]]:
        # A scan miss says nothing about process/data ownership. Keep the
        # runtime and lease so an explicit stop can still clean it up. Do not
        # wait behind an ongoing start/stop from the event loop.
        lock = self._vm_lock(runtime.vm_id)
        if not lock.acquire(blocking=False):
            return None
        try:
            if self._runtimes.get(runtime.vm_id) is not runtime or not runtime.ready:
                return None
            was_missing = runtime.missing_ticks >= 2
            runtime.missing_ticks = 0 if present else runtime.missing_ticks + 1
            if (present and not was_missing) or (not present and runtime.missing_ticks != 2):
                return None
            return self._status_payload(
                {
                    "vm_id": runtime.vm_id,
                    "lease_token": runtime.lease_token,
                    "observed_request_id": runtime.observed_request_id,
                },
                state="running" if present else "error",
                ok=present,
                reason="transport_recovered" if present else "transport_unavailable",
                error="" if present else "设备通道暂不可用，实例归属保留，未执行关机",
                hdc_serial=runtime.hdc_serial,
                details={
                    "cleanup_confirmed": False,
                    "instance_preserved": True,
                    "hdc_port": runtime.hdc_port,
                    "missing_ticks": runtime.missing_ticks,
                },
            )
        finally:
            lock.release()

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
            "instance_created": self._instance_definition_present(runtime),
            **({"created_in_attempt": runtime.created_in_attempt} if isinstance(runtime.created_in_attempt, bool) else {}),
            "hdc_port": runtime.hdc_port,
            "hdc_serial": runtime.hdc_serial,
            "driver_fport_port": runtime.driver_fport_port,
            "resolved_abi": host_abi(),
            "reused": reused,
            "reclaimed": reclaimed,
            "cleanup_confirmed": False,
            "instance_observation": self._instance_observation(
                runtime.vm_id, runtime.persistent_dict(), state="running", reason="instance_running"
            ),
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
            observed_request_id=str(known.get("observed_request_id") or ""),
            created_in_attempt=known.get("created_in_attempt") if isinstance(known.get("created_in_attempt"), bool) else None,
            driver_fport_port=(
                _int_value(known.get("driver_fport_port"), 0) or None
            ),
            started_at=float(known.get("started_at") or time.time()),
            ready=bool(known.get("ready")),
        )

    def _active_instance_serial(
        self, vm_id: str, known: Dict[str, Any], *, deadline: Optional[float] = None
    ) -> Optional[str]:
        """Read the live DevEco process; never infer ownership from HDC alone.

        Empty string means the exact instance is absent. None means process
        inspection was unavailable and callers must leave the lease isolated.
        """
        _stop_timeout(deadline, 1.0)
        try:
            import psutil  # noqa: PLC0415

            processes = psutil.process_iter(["name", "cmdline"])
        except Exception as exc:  # noqa: BLE001
            _stop_timeout(deadline, 1.0)
            logger.warning("Harmony VM 进程归属不可查 vm_id={}: {}", vm_id, exc)
            return None
        expected_name = _safe_instance_name(vm_id)
        expected_path = Path(str(known.get("instance_path") or self.instance_path))
        expected_port = _int_value(known.get("hdc_port"), 0)
        uncertain = False
        try:
            for process in processes:
                _stop_timeout(deadline, 1.0)
                try:
                    args = [str(part) for part in (process.info.get("cmdline") or [])]
                except Exception:  # noqa: BLE001
                    _stop_timeout(deadline, 1.0)
                    uncertain = True
                    continue
                if not args and str(process.info.get("name") or "").lower() in {"emulator", "emulator.exe"}:
                    # A visible Emulator with unreadable argv might be this
                    # exact instance; absence is not established.
                    uncertain = True
                    continue
                if not args or Path(args[0]).name.lower() not in {"emulator", "emulator.exe"}:
                    continue
                lower_args = [part.lower() for part in args]

                def value_after(flag: str) -> str:
                    try:
                        index = lower_args.index(flag.lower())
                        return args[index + 1]
                    except (ValueError, IndexError):
                        return ""

                if value_after("-start") != expected_name:
                    continue
                path = value_after("-instancePath")
                port = _int_value(value_after("-hdcPort"), 0)
                if not path or not port:
                    return None
                if Path(path).resolve() != expected_path.resolve() or port != expected_port:
                    return None
                return f"127.0.0.1:{port}"
        except Exception as exc:  # noqa: BLE001
            _stop_timeout(deadline, 1.0)
            logger.warning("Harmony VM 进程归属不可查 vm_id={}: {}", vm_id, exc)
            return None
        _stop_timeout(deadline, 1.0)
        return None if uncertain else ""

    def _cleanup_crash_service(
        self, vm_id: str, known: Dict[str, Any], *, deadline: Optional[float] = None
    ) -> tuple[bool, list[str]]:
        """Terminate only this VM's DevEco crash helper after stop/delete.

        The helper may outlive Emulator and be reparented. A process basename
        alone is not ownership evidence: both its socket and data directory
        must exactly identify this managed instance.
        """
        _stop_timeout(deadline, 1.0)
        instance_name = str(known.get("instance_name") or "")
        instance_path = str(known.get("instance_path") or "")
        if not instance_name and not instance_path:
            # Unknown/idempotent stop: there is no instance to identify.
            return True, []
        if instance_name != _safe_instance_name(vm_id) or not instance_path:
            return False, ["crash service instance identity is incomplete"]
        expected_socket = f"{instance_name}_crash"
        expected_dir = (Path(instance_path) / instance_name).resolve()

        def flag_value(args: list[str], flag: str) -> str:
            values: list[str] = []
            for index, arg in enumerate(args[1:], start=1):
                if arg == flag and index + 1 < len(args):
                    values.append(args[index + 1])
                elif arg.startswith(f"{flag}="):
                    values.append(arg[len(flag) + 1 :])
            return values[0] if len(values) == 1 else ""

        try:
            import psutil  # noqa: PLC0415

            processes = psutil.process_iter(["name", "cmdline"])
        except Exception as exc:  # noqa: BLE001
            _stop_timeout(deadline, 1.0)
            return False, [f"crash service process scan unavailable: {exc}"]

        errors: list[str] = []
        try:
            for process in processes:
                _stop_timeout(deadline, 1.0)
                try:
                    info = process.info
                    args = [str(part) for part in (info.get("cmdline") or [])]
                except Exception as exc:  # noqa: BLE001
                    _stop_timeout(deadline, 1.0)
                    # Without argv, ownership cannot be proved. Never kill it.
                    if str(getattr(process, "info", {}).get("name") or "") == "emulator-crash-service":
                        errors.append(f"crash service pid={process.pid} argv unavailable: {exc}")
                    continue
                if not args and str(info.get("name") or "") == "emulator-crash-service":
                    errors.append(f"crash service pid={process.pid} argv unavailable")
                if not args or Path(args[0]).name != "emulator-crash-service":
                    continue
                if flag_value(args, "-socket") != expected_socket:
                    continue
                data_dir = flag_value(args, "-data-dir")
                if not data_dir or Path(data_dir).resolve() != expected_dir:
                    continue
                try:
                    _stop_timeout(deadline, 1.0)
                    process.terminate()
                    process.wait(timeout=_stop_timeout(deadline, 3.0))
                except psutil.NoSuchProcess:
                    continue
                except psutil.TimeoutExpired:
                    try:
                        _stop_timeout(deadline, 1.0)
                        process.kill()
                        process.wait(timeout=_stop_timeout(deadline, 2.0))
                    except psutil.NoSuchProcess:
                        continue
                    except Exception as exc:  # noqa: BLE001
                        _stop_timeout(deadline, 1.0)
                        errors.append(f"crash service pid={process.pid}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    _stop_timeout(deadline, 1.0)
                    errors.append(f"crash service pid={process.pid}: {exc}")
        except Exception as exc:  # noqa: BLE001
            _stop_timeout(deadline, 1.0)
            errors.append(f"crash service process scan failed: {exc}")
        _stop_timeout(deadline, 1.0)
        return not errors, errors


    def _remove_fport(self, runtime: HarmonyVmRuntime, *, deadline: Optional[float] = None) -> None:
        _stop_timeout(deadline, 1.0)
        ports: set[int] = set()
        if runtime.driver_fport_port:
            ports.add(runtime.driver_fport_port)
        try:
            raw = hdc_run(
                "fport", "ls", serial=runtime.hdc_serial, timeout=_stop_timeout(deadline, 3.0), check=False
            )
            _stop_timeout(deadline, 1.0)
            ports.update(
                int(local)
                for local in re.findall(r"tcp:(\d+)\s+tcp:8012", raw or "")
            )
        except Exception:  # noqa: BLE001
            _stop_timeout(deadline, 1.0)
            pass
        _stop_timeout(deadline, 1.0)
        for port in ports:
            try:
                hdc_run(
                    "fport",
                    "rm",
                    f"tcp:{port}",
                    "tcp:8012",
                    serial=runtime.hdc_serial,
                    timeout=_stop_timeout(deadline, 3.0),
                    check=False,
                )
                _stop_timeout(deadline, 1.0)
            except Exception as exc:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                logger.debug("清理 fport {} 失败：{}", port, exc)


    def _wait_removed(self, serial: str, port: int, *, deadline: Optional[float] = None) -> tuple[bool, str]:
        _stop_timeout(deadline, 1.0)
        started = time.monotonic()
        verify_deadline = min(started + 15.0, deadline) if deadline is not None else started + 15.0
        evidence: Dict[str, Any] = {
            "hdc_serial": serial,
            "hdc_port": port,
            "started_unix": time.time(),
            "budget_sec": round(verify_deadline - started, 3),
            "sample_count": 0,
            "max_sample_duration_sec": 0.0,
            "max_offline_free_samples": 0,
            "last_sample": {},
        }
        # _stop_runtime holds _start_lock, so this bounded summary belongs to
        # exactly one stop. Preserve the existing tuple return contract.
        self._last_removal_evidence = evidence

        def finish(confirmed: bool, mode: str) -> tuple[bool, str]:
            evidence["elapsed_sec"] = round(time.monotonic() - started, 3)
            evidence["result"] = mode
            return confirmed, mode

        offline_ticks = 0
        while time.monotonic() < verify_deadline:
            sample_started = time.monotonic()
            evidence["sample_count"] += 1
            try:
                _stop_timeout(deadline, 1.0)
                target_rows = (hdc_list_targets() if deadline is None else
                               hdc_list_targets(deadline=deadline, strict=True))
                _stop_timeout(deadline, 1.0)
                targets = {
                    target.serial: target.status.lower()
                    for target in target_rows
                }
                port_free = self._port_is_free(port)
                _stop_timeout(deadline, 1.0)
                status = targets.get(serial)
                duration = round(time.monotonic() - sample_started, 3)
                evidence["max_sample_duration_sec"] = max(evidence["max_sample_duration_sec"], duration)
                evidence["last_sample"] = {
                    "target_status": status if status is not None else "absent",
                    "port_bindable": port_free,
                    "duration_sec": duration,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                }
                if status is None and port_free:
                    return finish(True, "removed")
                if status == "offline" and port_free:
                    offline_ticks += 1
                    evidence["max_offline_free_samples"] = max(evidence["max_offline_free_samples"], offline_ticks)
                    # DevEco HDC 6.1 keeps dead TCP targets and rejects
                    # `tconn <serial> -remove` with "No target available".
                    # Four stable observations plus a reusable port confirm
                    # transport removal. The caller verifies the process
                    # separately; the stale target is diagnostic, not a lease.
                    if offline_ticks >= 4:
                        return finish(True, "offline_hdc_target")
                else:
                    offline_ticks = 0
            except Exception as exc:  # noqa: BLE001
                _stop_timeout(deadline, 1.0)
                duration = round(time.monotonic() - sample_started, 3)
                evidence["max_sample_duration_sec"] = max(evidence["max_sample_duration_sec"], duration)
                evidence["last_sample"] = {
                    "target_status": "unknown",
                    "port_bindable": None,
                    "duration_sec": duration,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}:{exc}"[:300],
                }
                logger.debug(
                    "等待 Harmony VM 清理确认失败 serial={}: {}",
                    serial,
                    exc,
                )
            _stop_timeout(deadline, 1.0)
            time.sleep(min(0.5, max(0.0, verify_deadline - time.monotonic())))
        _stop_timeout(deadline, 1.0)
        return finish(False, "unconfirmed")


    def _port_conflict(self, port: int, expected_serial: str) -> str:
        owner = managed_vm_owner(expected_serial)
        if owner is not None:
            return f"managed VM already owns HDC target:{owner[0]}"
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
        try:
            return loopback_port_available(port)
        except OSError:
            return False

    def _load_registry(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._registry_readable = all(isinstance(value, dict) for value in raw.values())
                return {
                    str(key): value
                    for key, value in raw.items()
                    if isinstance(value, dict)
                }
            self._registry_readable = False
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            self._registry_readable = False
            logger.error(
                "Harmony VM registry 无法读取，不会猜测认领实例 path={}: {}",
                self.registry_path,
                exc,
            )
        return {}

    def _save_registry(self) -> None:
        # Different VM locks may publish concurrently. Serialize snapshots as
        # well as replacements, otherwise an older snapshot can win last or
        # one writer can rename another writer's shared .tmp file.
        with self._registry_lock:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            snapshot = self._known.copy()
            temporary: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.registry_path.parent,
                    prefix="registry.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(snapshot, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.registry_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

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
            **(
                {"observed_request_id": msg["observed_request_id"]}
                if msg.get("observed_request_id") else {}
            ),
        }


def _stop_deadline(msg: Dict[str, Any]) -> float:
    try:
        seconds = float(msg.get("operation_timeout_sec", 120.0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("operation_timeout_sec must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("operation_timeout_sec must be a positive finite number")
    return time.monotonic() + seconds


def _stop_timeout(deadline: Optional[float], maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Harmony VM stop deadline exceeded")
    return min(maximum, remaining)


@contextmanager
def _stop_lock(lock: Any, deadline: Optional[float]):
    acquired = lock.acquire() if deadline is None else lock.acquire(
        timeout=_stop_timeout(deadline, threading.TIMEOUT_MAX)
    )
    if not acquired:
        raise TimeoutError("Harmony VM stop deadline exceeded waiting for lifecycle lock")
    try:
        _stop_timeout(deadline, 1.0)
        yield
    finally:
        lock.release()


def _inspection_deadline(msg: Dict[str, Any]) -> float:
    try:
        raw = msg.get("operation_timeout_sec")
        budget = float(raw) if raw is not None else 5.0
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("invalid inspection budget")
    except (TypeError, ValueError):
        budget = 0.0
    return time.monotonic() + budget


def _start_deadline(msg: Dict[str, Any]) -> Optional[float]:
    raw = msg.get("operation_timeout_sec")
    if raw is None:
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


@contextmanager
def _startup_lock(lock: Any, deadline: Optional[float]):
    acquired = (
        lock.acquire() if deadline is None
        else lock.acquire(timeout=startup_timeout(deadline, threading.TIMEOUT_MAX))
    )
    if not acquired:
        raise TimeoutError("Harmony VM startup deadline exceeded waiting for lifecycle lock")
    try:
        yield
    finally:
        lock.release()


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


def _managed_folded_screen_width(msg: Any) -> int:
    """Return the outer width only for a managed Foldable's folded choice."""
    if not isinstance(msg, dict) or str(msg.get("device_type") or "") != "Foldable":
        return 0
    config = msg.get("config_json")
    if not isinstance(config, dict):
        return 0
    fold = config.get("fold")
    if not isinstance(fold, dict) or fold.get("initial_state") != "folded":
        return 0
    folded_screen = config.get("folded_screen")
    if not isinstance(folded_screen, dict):
        return 0
    return max(0, _int_value(folded_screen.get("width"), 0))


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
