from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..hub import Hub
from ..lockstore import DeviceLockStore
from ..models import AppInstallTask, AppInstallTaskItem, AppPackage, Device, DeviceAlias
from ..harmony_vm.models import HarmonyAppPackageMeta, HarmonyVmInstance
from ..harmony_vm.package_meta import abi_matches

DEFAULT_TIMEOUT_SEC = 600
TERMINAL_STATES = {"success", "failed", "timeout", "unknown"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _alias_map(session: AsyncSession) -> Dict[str, str]:
    res = await session.execute(select(DeviceAlias.serial, DeviceAlias.alias))
    return {row.serial: row.alias for row in res.all()}


def package_to_dict(pkg: AppPackage) -> Dict[str, Any]:
    return pkg.to_dict()


async def list_packages(session: AsyncSession) -> List[Dict[str, Any]]:
    res = await session.execute(
        select(AppPackage).order_by(AppPackage.created_at.desc(), AppPackage.id.desc())
    )
    packages = list(res.scalars().all())
    out = [package_to_dict(pkg) for pkg in packages]
    harmony_ids = [pkg.id for pkg in packages if pkg.platform == "harmony"]
    meta_by_id: Dict[str, HarmonyAppPackageMeta] = {}
    metadata_error = ""
    if harmony_ids:
        try:
            meta_rows = (
                await session.execute(
                    select(HarmonyAppPackageMeta).where(
                        HarmonyAppPackageMeta.package_id.in_(harmony_ids)
                    )
                )
            ).scalars().all()
            meta_by_id = {meta.package_id: meta for meta in meta_rows}
        except SQLAlchemyError as exc:
            await session.rollback()
            metadata_error = f"{type(exc).__name__}: {exc}"[:4000]
    for row in out:
        if row["platform"] == "harmony":
            package_id = str(row["id"])
            meta = meta_by_id.get(package_id)
            row["harmony_abi"] = meta.to_dict() if meta else {
                "package_id": package_id,
                "abi_set": "unknown",
                "abi_state": (
                    "storage_unavailable" if metadata_error else "missing"
                ),
                "last_error": (
                    metadata_error or "Harmony package ABI metadata missing"
                ),
            }
    return out


async def task_to_dict(session: AsyncSession, task_id: str) -> Dict[str, Any]:
    task = await session.get(AppInstallTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    pkg = await session.get(AppPackage, task.package_id)
    res = await session.execute(
        select(AppInstallTaskItem)
        .where(AppInstallTaskItem.task_id == task_id)
        .order_by(AppInstallTaskItem.id)
    )
    items = [item.to_dict() for item in res.scalars().all()]
    return {
        **task.to_dict(),
        "package": package_to_dict(pkg) if pkg else None,
        "items": items,
        "summary": _summary(items),
    }


def _summary(items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out = {
        "total": 0,
        "pending": 0,
        "running": 0,
        "success": 0,
        "failed": 0,
        "timeout": 0,
        "unknown": 0,
    }
    for item in items:
        out["total"] += 1
        state = str(item.get("state") or "")
        if state in out:
            out[state] += 1
    return out


async def eligible_devices(
    session: AsyncSession,
    store: DeviceLockStore,
    hub: Hub,
    package_id: str,
) -> List[Dict[str, Any]]:
    pkg = await session.get(AppPackage, package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="package not found")
    aliases = await _alias_map(session)
    res = await session.execute(
        select(Device)
        .where(Device.status == "online", Device.platform == pkg.platform)
        .order_by(Device.serial)
    )
    out: List[Dict[str, Any]] = []
    for dev in res.scalars().all():
        ok, _reason, _message, _agent = _check_device(dev, store, hub, pkg.platform)
        if not ok:
            continue
        compatible, _reason, _message = await _check_package_device_compatibility(
            session, hub, pkg, dev
        )
        if not compatible:
            continue
        out.append(
            {
                "serial": dev.serial,
                "alias": aliases.get(dev.serial, ""),
                "platform": dev.platform,
                "brand": dev.brand or "",
                "model": dev.model or "",
                "osVersion": dev.os_version or "",
            }
        )
    return out


async def create_task(
    session: AsyncSession,
    store: DeviceLockStore,
    hub: Hub,
    package_id: str,
    serials: List[str],
) -> Dict[str, Any]:
    pkg = await session.get(AppPackage, package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="package not found")
    unique_serials = _unique_serials(serials)
    if not unique_serials:
        raise HTTPException(status_code=400, detail="serials empty")

    task = AppInstallTask(package_id=pkg.id, state="running")
    session.add(task)
    await session.flush()

    dispatches: List[Tuple[AppInstallTaskItem, str]] = []
    start_time = now_utc()
    for serial in unique_serials:
        dev = await session.get(Device, serial)
        ok, reason, message, agent_id = _check_device(dev, store, hub, pkg.platform)
        if ok:
            ok, reason, message = await _check_package_device_compatibility(
                session, hub, pkg, dev
            )
        if not ok:
            item = AppInstallTaskItem(
                task_id=task.id,
                serial=serial,
                platform=(dev.platform if dev is not None else pkg.platform),
                state="failed",
                reason=reason,
                message=message,
                finished_at=start_time,
                timeout_sec=DEFAULT_TIMEOUT_SEC,
            )
        else:
            item = AppInstallTaskItem(
                task_id=task.id,
                serial=serial,
                platform=dev.platform,  # type: ignore[union-attr]
                state="running",
                started_at=start_time,
                timeout_sec=DEFAULT_TIMEOUT_SEC,
            )
            dispatches.append((item, str(agent_id)))
        session.add(item)

    await session.flush()
    await _refresh_task_state(session, task.id)
    await session.commit()

    await _dispatch_items(session, hub, pkg, dispatches)
    return await task_to_dict(session, task.id)


async def retry_unsuccessful(
    session: AsyncSession,
    store: DeviceLockStore,
    hub: Hub,
    task_id: str,
) -> Dict[str, Any]:
    task = await session.get(AppInstallTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    pkg = await session.get(AppPackage, task.package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="package not found")

    res = await session.execute(
        select(AppInstallTaskItem).where(
            AppInstallTaskItem.task_id == task_id,
            AppInstallTaskItem.state.in_(["failed", "timeout", "unknown"]),
        )
    )
    items = list(res.scalars().all())
    dispatches: List[Tuple[AppInstallTaskItem, str]] = []
    start_time = now_utc()
    for item in items:
        dev = await session.get(Device, item.serial)
        ok, reason, message, agent_id = _check_device(dev, store, hub, pkg.platform)
        if ok:
            ok, reason, message = await _check_package_device_compatibility(
                session, hub, pkg, dev
            )
        item.reason = ""
        item.message = ""
        item.finished_at = None
        item.started_at = start_time
        item.timeout_sec = item.timeout_sec or DEFAULT_TIMEOUT_SEC
        if not ok:
            item.state = "failed"
            item.reason = reason
            item.message = message
            item.finished_at = start_time
        else:
            item.state = "running"
            item.platform = dev.platform  # type: ignore[union-attr]
            dispatches.append((item, str(agent_id)))

    await _refresh_task_state(session, task_id)
    await session.commit()

    await _dispatch_items(session, hub, pkg, dispatches)
    return await task_to_dict(session, task_id)


async def handle_result(session: AsyncSession, msg: Dict[str, Any]) -> None:
    item_id = str(msg.get("item_id") or "")
    if not item_id:
        return
    item = await session.get(AppInstallTaskItem, item_id)
    if item is None or item.state != "running":
        return
    success = bool(msg.get("success"))
    item.finished_at = now_utc()
    item.message = str(msg.get("message") or "")
    if success:
        item.state = "success"
        item.reason = ""
        if not item.message:
            item.message = "安装成功"
    else:
        reason = str(msg.get("reason") or "install_failed")
        item.state = "timeout" if reason == "timeout" else "failed"
        item.reason = reason
    await _refresh_task_state(session, item.task_id)
    await session.commit()


async def mark_startup_unknown(session: AsyncSession) -> int:
    res = await session.execute(
        select(AppInstallTaskItem).where(AppInstallTaskItem.state.in_(["pending", "running"]))
    )
    items = list(res.scalars().all())
    ts = now_utc()
    task_ids = set()
    for item in items:
        item.state = "unknown"
        item.reason = "unknown_server_restarted"
        item.message = "Server 重启，安装状态已重置"
        item.finished_at = ts
        task_ids.add(item.task_id)
    for task_id in task_ids:
        await _refresh_task_state(session, task_id)
    await session.commit()
    return len(items)


async def mark_timeouts(session: AsyncSession) -> int:
    res = await session.execute(
        select(AppInstallTaskItem).where(AppInstallTaskItem.state == "running")
    )
    items = list(res.scalars().all())
    ts = now_utc()
    changed = 0
    task_ids = set()
    for item in items:
        if item.started_at is None:
            continue
        elapsed = (ts - item.started_at).total_seconds()
        if elapsed < (item.timeout_sec or DEFAULT_TIMEOUT_SEC) + 60:
            continue
        item.state = "timeout"
        item.reason = "timeout"
        item.message = "安装超时，可重试"
        item.finished_at = ts
        task_ids.add(item.task_id)
        changed += 1
    for task_id in task_ids:
        await _refresh_task_state(session, task_id)
    if changed:
        await session.commit()
    return changed


async def _dispatch_items(
    session: AsyncSession,
    hub: Hub,
    pkg: AppPackage,
    dispatches: List[Tuple[AppInstallTaskItem, str]],
) -> None:
    if not dispatches:
        return
    changed = False
    for item, agent_id in dispatches:
        payload = {
            "type": P.MSG_APP_INSTALL_START,
            "task_id": item.task_id,
            "item_id": item.id,
            "serial": item.serial,
            "platform": item.platform,
            "package_url": f"/api/app-install/packages/{pkg.id}/download",
            "filename": pkg.filename,
            "timeout_sec": item.timeout_sec or DEFAULT_TIMEOUT_SEC,
        }
        ok = await hub.send_to_agent(agent_id, payload)
        if not ok and item.state == "running":
            item.state = "failed"
            item.reason = "dispatch_failed"
            item.message = "发送给 Agent 失败"
            item.finished_at = now_utc()
            changed = True
    if changed:
        for item, _ in dispatches:
            await _refresh_task_state(session, item.task_id)
        await session.commit()


async def _refresh_task_state(session: AsyncSession, task_id: str) -> None:
    task = await session.get(AppInstallTask, task_id)
    if task is None:
        return
    res = await session.execute(
        select(AppInstallTaskItem.state).where(AppInstallTaskItem.task_id == task_id)
    )
    states = [str(row[0]) for row in res.all()]
    if states and all(state in TERMINAL_STATES for state in states):
        task.state = "done"
        task.finished_at = now_utc()
    else:
        task.state = "running"
        task.finished_at = None


def _check_device(
    dev: Optional[Device],
    store: DeviceLockStore,
    hub: Hub,
    expected_platform: str,
) -> Tuple[bool, str, str, Optional[str]]:
    if dev is None:
        return False, "offline", "设备不存在或不在线", None
    if dev.platform != expected_platform:
        return False, "platform_mismatch", f"设备平台 {dev.platform} 与包平台 {expected_platform} 不一致", None
    if dev.status != "online":
        return False, "offline", f"设备状态为 {dev.status}", None
    extra = hub.get_device_extra(dev.serial) or {}
    readiness = extra.get("readiness") or {}
    if readiness.get("ready") is not True:
        return False, "not_ready", str(readiness.get("hint") or "设备当前不可 ready"), None
    if store.peek(dev.serial) is not None:
        return False, "locked", "设备当前已被占用", None
    agent_id = hub.agent_id_for_serial(dev.serial)
    if agent_id is None:
        return False, "agent_offline", "找不到在线 Agent", None
    return True, "", "", agent_id


async def _check_package_device_compatibility(
    session: AsyncSession,
    hub: Hub,
    pkg: AppPackage,
    dev: Optional[Device],
) -> Tuple[bool, str, str]:
    """Apply ABI gating only to managed Harmony VMs; real devices stay unchanged."""
    if pkg.platform != "harmony" or dev is None:
        return True, "", ""
    extra = hub.get_device_extra(dev.serial)
    vm_id = str(extra.get("vm_instance_id") or "").strip()
    if not vm_id:
        return True, "", ""
    vm = await session.get(HarmonyVmInstance, vm_id)
    if vm is None:
        return False, "harmony_vm_identity_missing", "受管鸿蒙虚拟机身份已失效"
    meta = await session.get(HarmonyAppPackageMeta, pkg.id)
    if meta is None or meta.abi_state != "resolved":
        state = meta.abi_state if meta is not None else "missing"
        return (
            False,
            "hap_abi_unknown",
            f"HAP ABI 无法确认（{state}），不向虚拟机盲目下发",
        )
    target_abi = vm.abi
    if target_abi == "auto":
        last_status = (
            (vm.runtime or {}).get("last_status")
            if isinstance((vm.runtime or {}).get("last_status"), dict)
            else {}
        )
        details = (
            last_status.get("details")
            if isinstance(last_status.get("details"), dict)
            else {}
        )
        target_abi = str(details.get("resolved_abi") or "")
    if target_abi not in {"arm64", "x86_64"}:
        return (
            False,
            "harmony_vm_abi_unknown",
            "运行中鸿蒙虚拟机的实际 ABI 未回传，拒绝盲目安装",
        )
    if not abi_matches(meta.abi_set, target_abi):
        return (
            False,
            "hap_abi_mismatch",
            f"HAP ABI {meta.abi_set} 与虚拟机 {target_abi} 不匹配",
        )
    return True, "", ""


def _unique_serials(serials: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in serials:
        serial = str(raw or "").strip()
        if not serial or serial in seen:
            continue
        seen.add(serial)
        out.append(serial)
    return out


def package_file_path(pkg: AppPackage) -> Path:
    return Path(pkg.storage_path)
