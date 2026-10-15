"""Independent REST API for managed HarmonyOS virtual machines."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..api._deps import DBSession, HubDep
from ..api.submissions import RequireBearer
from ..hub import Hub
from ..vm_lifecycle import (
    is_untouched_draft,
    require_retirement_allowed, send_operation_once,
    begin_operation, maintenance, require_idle, require_startable,
    require_device_idle, require_owner_host, runtime as lifecycle_runtime,
    transaction_lock, record_send_failure, delete_device_record, forget_device_route,
    cleanup_command, send_cleanup_once, cancel_operation_timeout, arm_operation_timeout,
    host_id as agent_host_id, update_runtime,
)
from .catalog import normalize_manifest
from .models import (
    HarmonyVmCatalogSnapshot,
    HarmonyVmInstance,
    HarmonyVmSetting,
)
from .schemas import (
    HarmonyVmCatalogImportReq,
    HarmonyVmCreateReq,
    HarmonyVmDispatchReq,
    HarmonyVmForceReleaseReq,
    HarmonyVmPatchReq,
    HarmonyVmSettingReq,
)
from .service import (
    ACTIVE_STATES,
    allocate_port_lease,
    delete_owned_alias,
    ensure_alias_available,
    get_capability_waiter,
    get_vm_or_404,
    normalize_abi,
    release_port_lease,
    reserve_placeholder_alias,
    vm_payload,
)


def _require_harmony_vm_db(request: Request) -> None:
    if getattr(request.app.state, "harmony_vm_db_ready", True) is False:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Harmony VM 数据表初始化失败；现有设备与 Android 链路不受影响",
        )




router = APIRouter(
    prefix="/api/internal/harmony-vm/instances",
    tags=["internal-harmony-vm"],
    dependencies=[RequireBearer, Depends(_require_harmony_vm_db)],
)
catalog_router = APIRouter(
    prefix="/api/internal/harmony-vm",
    tags=["internal-harmony-vm"],
    dependencies=[RequireBearer, Depends(_require_harmony_vm_db)],
)

REDISPATCH_REQUIRED = "assigned agent offline; please probe and dispatch again"
LEASE_CLEANUP_REQUIRED = (
    "旧鸿蒙虚拟机尚未确认停止；请先重试停止，或核实旧宿主机实例已关闭后确认释放占用"
)

# 创建时按官方目录校验并写死的字段，之后一律不可改。
CATALOG_LOCKED_FIELDS = (
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
)


def _request_id() -> str:
    return uuid.uuid4().hex[:16]


def _alias(body_alias: str, body_name: str) -> str:
    return (body_alias or body_name or "").strip()


def _version_spec(os_version: str, api_version: str) -> str:
    value = (os_version or "").strip()
    if value.startswith("HarmonyOS ") and "(" in value:
        return value
    api = (api_version or "").strip()
    return f"HarmonyOS {value}({api})" if value and api else ""


def _official_screen_config(
    requested_config: Dict[str, Any], profile: Dict[str, Any], image_id: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """按官方机型锁定屏幕参数与创建方式。

    屏幕规格不采信请求体：机型是从官方目录里选的，尺寸就必须跟着官方目录走，
    否则会出现“选了 Mate 60、却建出别的分辨率”的实例。

    创建方式同样由目录决定，不由调用方指定——``screen_profile`` 把机型名交给
    ``-screenProfile``；``default`` 是这一版 Emulator 不接受该机型名，但它本来
    就是该形态该版本的默认机型，两个屏幕参数都不传就会建出它。
    """
    methods = profile.get("create_methods")
    method = (
        str(methods.get(image_id) or "") if isinstance(methods, dict) else ""
    )
    if method not in {"screen_profile", "default"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "catalog_compatibility_missing",
                "message": "Server 鸿蒙目录缺少该机型的创建方式，不能绕过校验创建",
            },
        )
    config = dict(requested_config or {})
    display = dict(config.get("display") or {})
    display["mode"] = "profile" if method == "screen_profile" else "official_default"
    config["display"] = display
    screen = {
        "screen_width": int(profile.get("width") or 0) or 1080,
        "screen_height": int(profile.get("height") or 0) or 2340,
        "density": int(profile.get("density") or 0) or 420,
        "screen_size_in": str(profile.get("size_in") or ""),
    }
    if profile.get("outer_width") and profile.get("outer_height"):
        config["folded_screen"] = {
            "width": int(profile["outer_width"]),
            "height": int(profile["outer_height"]),
            "density": screen["density"],
            "size_in": str(
                profile.get("outer_size_in") or screen["screen_size_in"]
            ),
        }
    else:
        config.pop("folded_screen", None)
    return config, screen


def _fold_config(
    config: Dict[str, Any], device_type: str, fold_state: str
) -> Dict[str, Any]:
    """记录折叠屏初始形态，并挡掉在其它形态上误传的请求。

    只有 ``Foldable`` 的两态模型经过实测（DisplayManagerService 的 -m/-f 开关）。
    WideFold、TripleFold、2in1 Foldable 各有独立的状态模型且都没验证过，宁可
    显式拒绝，也不接一个实际不会生效的字段。
    """
    updated = dict(config or {})
    if fold_state == "unfolded":
        updated.pop("fold", None)
        return updated
    if device_type != "Foldable":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "fold_state_unsupported_device_type",
                "message": "仅折叠屏（Foldable）支持设置初始折叠形态",
            },
        )
    updated["fold"] = {"initial_state": fold_state}
    return updated


async def _harmony_setting(session: AsyncSession) -> HarmonyVmSetting:
    row = await session.get(HarmonyVmSetting, "global")
    if row is None:
        row = HarmonyVmSetting(id="global", instance_uuid="")
        session.add(row)
        await session.flush()
    return row


async def _retired_harmony_instance_uuids(session: AsyncSession) -> List[str]:
    """返回曾经启用、后来被替换或清空的共享 UUID。

    不给 ``harmony_vm_settings`` 增加新列，避免给已部署 Server 制造一次表结构
    升级；同一张独立设置表用 ``retired_*`` 行保留历史值。Agent 只在实例当前
    UUID 命中这里时恢复独立身份，因此不会误改从未启用共享身份的实例。
    """
    result = await session.execute(
        select(HarmonyVmSetting.instance_uuid).where(
            HarmonyVmSetting.id.like("retired_%")
        )
    )
    return sorted(
        {
            str(value or "").strip().lower()
            for value in result.scalars()
            if str(value or "").strip()
        }
    )


async def _harmony_identity_payload(session: AsyncSession) -> Dict[str, Any]:
    row = await _harmony_setting(session)
    return {
        "instance_uuid": row.instance_uuid or "",
        "retired_instance_uuids": await _retired_harmony_instance_uuids(session),
    }


@catalog_router.get("/settings")
async def get_settings(session: AsyncSession = DBSession) -> Dict[str, Any]:
    row = await _harmony_setting(session)
    await session.commit()
    return row.to_dict()


@catalog_router.put("/settings")
async def put_settings(
    body: HarmonyVmSettingReq,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    row = await _harmony_setting(session)
    old_value = (row.instance_uuid or "").strip().lower()
    new_value = body.instance_uuid.strip().lower()
    if old_value and old_value != new_value:
        # 记录所有退役过的共享 UUID。恢复默认时，Agent 据此只重置仍带旧共享值
        # 的实例；已经是独立 UUID 的实例保持不动。
        retired_id = f"retired_{old_value.replace('-', '')[:24]}"
        retired = await session.get(HarmonyVmSetting, retired_id)
        if retired is None:
            session.add(
                HarmonyVmSetting(id=retired_id, instance_uuid=old_value)
            )
        else:
            retired.instance_uuid = old_value
    row.instance_uuid = new_value
    await session.commit()
    await session.refresh(row)
    logger.info(
        "harmony_vm_setting.instance_uuid={} udid={}",
        row.instance_uuid or "(未设置)",
        row.to_dict()["device_udid"] or "(未设置)",
    )
    return row.to_dict()


@catalog_router.get("/catalog")
async def get_catalog(session: AsyncSession = DBSession) -> Dict[str, Any]:
    snapshot = await session.get(HarmonyVmCatalogSnapshot, "official")
    if snapshot is not None:
        return snapshot.to_dict()
    return {
        "ok": False,
        "reason": "Server 尚未导入 DevEco 官方目录",
        "source": None,
        "device_types": [],
        "images": [],
        "screen_profiles": [],
        "stats": {
            "images": 0,
            "creatable_images": 0,
            "unavailable_images": 0,
            "screen_profiles": 0,
        },
        "source_policy": {
            "owner": "server",
            "mode": "replace_official_snapshot",
            "agent_catalog_aggregation": False,
            "bundled_preset_on_empty_db": True,
            "empty_means_initialization_failed": True,
            "builtin_fallback": False,
        },
    }


@catalog_router.post("/catalog/import")
async def import_catalog(
    body: HarmonyVmCatalogImportReq,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    try:
        normalized = normalize_manifest(body.manifest)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": str(exc),
                "message": "导入文件必须同时包含可解析的官方 images 与 screen_profiles",
            },
        ) from exc
    collected_at = None
    if body.collected_at:
        try:
            collected_at = datetime.fromisoformat(
                body.collected_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="collected_at must be ISO-8601",
            ) from exc
    snapshot = await session.get(HarmonyVmCatalogSnapshot, "official")
    created = snapshot is None
    if snapshot is None:
        snapshot = HarmonyVmCatalogSnapshot(id="official")
        session.add(snapshot)
    snapshot.source_type = "deveco_emulator_official"
    snapshot.source_url = body.source_url.strip()
    snapshot.collected_at = collected_at
    snapshot.emulator_version = normalized["emulator_version"]
    snapshot.device_types_json = normalized["device_types"]
    snapshot.images_json = normalized["images"]
    snapshot.screen_profiles_json = normalized["screen_profiles"]
    await session.commit()
    await session.refresh(snapshot)
    return {
        "mode": "replace",
        "created": created,
        "images": len(normalized["images"]),
        "screen_profiles": len(normalized["screen_profiles"]),
        "catalog": snapshot.to_dict(),
    }


@router.get("")
async def list_instances(session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    rows = (
        await session.execute(
            select(HarmonyVmInstance).order_by(HarmonyVmInstance.created_at.desc())
        )
    ).scalars().all()
    return [row.to_dict() for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_instance(
    body: HarmonyVmCreateReq,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    alias = _alias(body.alias, body.name)
    if not alias:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"reason": "alias_required", "message": "创建时设备别名必填"},
        )
    try:
        await ensure_alias_available(session, alias=alias)
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="alias already exists")
    catalog = await session.get(HarmonyVmCatalogSnapshot, "official")
    if catalog is None or not catalog.images_json:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "harmony_catalog_not_imported",
                "message": "Server 尚未导入 DevEco 官方目录，不能创建鸿蒙虚拟机配置",
            },
        )
    requested_spec = _version_spec(body.os_version, body.api_version)
    requested_abi = normalize_abi(body.abi)
    images = [
        item
        for item in catalog.images_json
        if isinstance(item, dict)
    ]
    selected = next(
        (
            item
            for item in images
            if body.image_id.strip()
            and str(item.get("id") or "") == body.image_id.strip()
            and item.get("creatable", True) is not False
        ),
        None,
    )
    if selected is None and not body.image_id.strip():
        selected = next(
            (
                item
                for item in images
                if str(item.get("device_type") or "") == body.device_type
                and str(item.get("os_version") or "") == requested_spec
                and item.get("creatable", True) is not False
                and (
                    requested_abi == "auto"
                    or str(item.get("abi") or "auto") in {"auto", requested_abi}
                )
            ),
            None,
        )
    if selected is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "official_image_required",
                "message": "镜像必须来自 Server 的 DevEco 官方目录",
            },
        )
    selected_type = str(selected.get("device_type") or "")
    selected_version = str(selected.get("os_version") or "")
    selected_api = str(selected.get("api_version") or "")
    selected_abi = normalize_abi(str(selected.get("abi") or "auto"))
    if requested_abi != "auto" and selected_abi not in {"auto", requested_abi}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "reason": "catalog_abi_mismatch",
                "message": "请求架构与 Server 官方目录镜像不一致",
            },
        )
    screen_profile = body.screen_profile.strip()
    profiles = [
        item
        for item in (catalog.screen_profiles_json or [])
        if isinstance(item, dict)
        and str(item.get("device_type") or "") == selected_type
    ]
    if not screen_profile:
        # 不传机型不等于放开自定义屏幕。前端已取消自定义模式（方案 3.3），API 也
        # 不能留暗道：这里解析成该镜像的官方默认机型，屏幕参数照样锁定在目录里。
        selected_profile = next(
            (
                item
                for item in profiles
                if isinstance(item.get("create_methods"), dict)
                and item["create_methods"].get(str(selected.get("id") or ""))
                == "default"
            ),
            None,
        )
        if selected_profile is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "official_screen_profile_required",
                    "message": "该系统版本没有官方默认机型，必须显式选择设备机型",
                },
            )
        screen_profile = str(selected_profile.get("name") or "")
    else:
        selected_profile = next(
            (
                item
                for item in profiles
                if str(item.get("name") or "") == screen_profile
            ),
            None,
        )
        if selected_profile is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "official_screen_profile_required",
                    "message": "设备机型必须来自 Server 的 DevEco 官方目录，且与系统版本属于同一设备形态",
                },
            )
        supported_image_ids = selected_profile.get("supported_image_ids")
        if not isinstance(supported_image_ids, list):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "catalog_compatibility_missing",
                    "message": "Server 鸿蒙目录缺少机型与系统版本兼容关系，不能绕过校验创建",
                },
            )
        if str(selected.get("id") or "") not in {
            str(value) for value in supported_image_ids
        }:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "device_model_image_incompatible",
                    "message": "所选设备机型不支持该系统版本",
                },
            )
    config_json, screen = _official_screen_config(
        body.config_json, selected_profile, str(selected.get("id") or "")
    )
    config_json = _fold_config(config_json, selected_type, body.fold_state)
    vm = HarmonyVmInstance(
        name=alias,
        alias=alias,
        device_type=selected_type,
        os_version=selected_version,
        api_version=selected_api,
        abi=selected_abi if requested_abi == "auto" else requested_abi,
        image_id=str(selected.get("id") or ""),
        screen_profile=screen_profile,
        **screen,
        memory_gb=body.memory_gb,
        storage_gb=body.storage_gb,
        boot_mode="cold",
        config_json=config_json,
        state="draft",
    )
    session.add(vm)
    await session.flush()
    await reserve_placeholder_alias(session, vm)
    await session.commit()
    await session.refresh(vm)
    return vm.to_dict()


@router.get("/{vm_id}")
async def get_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    try:
        return (await get_vm_or_404(session, vm_id)).to_dict()
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")


@router.patch("/{vm_id}")
async def patch_instance(
    body: HarmonyVmPatchReq,
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    await transaction_lock(session, f"harmony-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    require_idle(vm, allow_running=True)
    patch = body.model_dump(exclude_unset=True)
    patch.pop("name", None)
    alias_only = set(patch).issubset({"alias"})
    if not alias_only and vm.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="active vm configuration is immutable")
    if "alias" in patch:
        alias = str(patch.pop("alias") or "").strip()[:128]
        if alias != vm.alias:
            if alias:
                try:
                    await ensure_alias_available(session, alias=alias, exclude_vm_id=vm.id)
                except ValueError:
                    raise HTTPException(status.HTTP_409_CONFLICT, detail="alias already exists")
            # 与 Android VM 相同：先按旧身份删除映射，再更新 alias/name；
            # 显式空字符串表示清空，vm_id 仍是唯一身份，不做名称兜底。
            await delete_owned_alias(session, vm)
            vm.alias = alias
            vm.name = alias
            if alias:
                await reserve_placeholder_alias(session, vm)
    # 机型、镜像和屏幕是创建时按官方目录校验并锁定的。允许在这里改等于绕过整套
    # 兼容校验，能改出一台 CLI 拒绝创建的配置，且直到下发才暴露。要换配置就重新
    # 建一台，不在 PATCH 里放暗道。
    conflicting = [
        key
        for key in CATALOG_LOCKED_FIELDS
        if key in patch
        and patch[key] is not None
        and patch[key] != getattr(vm, key)
    ]
    if conflicting:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "catalog_locked_fields_immutable",
                "message": (
                    "机型、系统版本与屏幕参数由官方目录锁定，不能修改："
                    + "、".join(conflicting)
                    + "。请新建一台配置。"
                ),
            },
        )
    if vm.assigned_agent_id or lifecycle_runtime(vm).get("instance_created"):
        immutable_changes = [key for key in ("memory_gb", "storage_gb", "config_json")
                             if key in patch and patch[key] is not None and patch[key] != getattr(vm, key)]
        if immutable_changes:
            raise HTTPException(409, {"reason": "bound_configuration_immutable",
                "message": "已绑定实例的硬件与镜像配置不能原位修改，请新建配置：" + "、".join(immutable_changes)})
    for key in ("memory_gb", "storage_gb", "boot_mode", "config_json"):
        if key in patch and patch[key] is not None:
            setattr(vm, key, patch[key])
    await session.commit()
    await session.refresh(vm)
    return vm.to_dict()


@router.delete("/{vm_id}")
async def delete_instance(vm_id: str, session: AsyncSession = DBSession, hub: Hub = HubDep) -> Dict[str, Any]:
    await transaction_lock(session, f"harmony-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "实例不存在")
    require_retirement_allowed(hub, vm)
    target, command, alias_deleted = await _retire_configuration(session, vm, hub)
    await session.commit()
    await forget_device_route(hub, vm)
    sent = await send_cleanup_once(hub, vm, target, command)
    return {"id": vm_id, "deleted": True, "alias_deleted": alias_deleted,
            "cleanup_sent": sent, "avd_cleanup_sent": sent}


async def _retire_configuration(session: AsyncSession, vm: HarmonyVmInstance, hub: Hub):
    """Remove only ledger state; the caller commits before any network send."""
    target, command = cleanup_command(vm, P.MSG_HARMONY_VM_DELETE)
    alias_deleted = await delete_owned_alias(session, vm)
    cancel_operation_timeout(vm)
    maintenance(hub, vm, True)
    await release_port_lease(session, vm, reason="configuration_deleted")
    await delete_device_record(session, vm)
    await session.delete(vm)
    await session.flush()
    return target, command, alias_deleted

@router.post("/{vm_id}/force-release")
async def force_release_instance_lease(
    body: HarmonyVmForceReleaseReq,
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    """Compatibility endpoint: a confirmation string is not cleanup evidence."""
    await transaction_lock(session, f"harmony-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "harmony vm not found")
    require_idle(vm)
    if vm.assigned_agent_id or vm.lease_token or vm.hdc_port is not None or vm.hdc_serial:
        raise HTTPException(409, "请先停止或删除实例，等待宿主确认清理；人工确认不能替代清理结果")
    return {"released": True, "instance": vm.to_dict()}


@router.post("/{vm_id}/dispatch-candidates")
async def dispatch_candidates(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    return await get_capability_waiter().probe(hub=hub, vm=vm)


@router.post("/{vm_id}/dispatch")
async def dispatch_instance(
    body: HarmonyVmDispatchReq,
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    agent_id = body.agent_id.strip()
    if not hub.has_agent(agent_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED)
    return await _send_start(vm_id, agent_id, session, hub)


@router.post("/{vm_id}/start")
async def start_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession, hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "harmony vm not found")
    agent_id = str(vm.assigned_agent_id or "")
    if not agent_id:
        raise HTTPException(409, "实例尚未绑定 Agent，请先探查并下发")
    return await _send_start(vm_id, agent_id, session, hub)


@router.post("/{vm_id}/stop")
async def stop_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession, hub: Hub = HubDep,
) -> Dict[str, Any]:
    await transaction_lock(session, f"harmony-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "harmony vm not found")
    require_idle(vm, allow_running=True)
    if not vm.assigned_agent_id:
        if vm.lease_token or vm.hdc_port is not None or vm.hdc_serial:
            raise HTTPException(409, LEASE_CLEANUP_REQUIRED)
        return {"sent": False, "instance": vm.to_dict()}
    require_owner_host(vm, hub)
    require_device_idle(hub, vm)
    request_id = begin_operation(vm, "stop", target_agent_id=vm.assigned_agent_id,
                                 target_host_id=agent_host_id(hub, vm.assigned_agent_id))
    vm.state = "stopping"
    maintenance(hub, vm, True)
    await session.commit()
    arm_operation_timeout(vm, hub)
    sent = await send_operation_once(hub, vm.assigned_agent_id, {
        "type": P.MSG_HARMONY_VM_STOP, "request_id": request_id, "vm_id": vm.id,
        "hdc_serial": vm.hdc_serial or "", "lease_token": vm.lease_token or "",
    })
    if not sent:
        await record_send_failure(session, vm, request_id, "所属 Agent 离线，尚未确认停止")
    await session.refresh(vm)
    return {"sent": sent, "instance": vm.to_dict()}


async def _send_start(
    vm_id: str,
    agent_id: str,
    session: AsyncSession,
    hub: Hub,
    *, expected_draft: bool = False,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
        await session.refresh(vm)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    if expected_draft and not is_untouched_draft(vm):
        raise HTTPException(409, "新配置已被另一操作接管，本次换机不会重复启动")
    require_idle(vm)
    old_agent = str(vm.assigned_agent_id or "")
    if old_agent and old_agent != agent_id:
        return await _switch_agent(vm=vm, new_agent_id=agent_id, session=session, hub=hub)
    require_startable(vm)
    if not hub.has_agent(agent_id):
        raise HTTPException(409, "所属 Agent 离线，请等待连接")
    probed_payload = vm_payload(vm)
    capability_waiter = get_capability_waiter()
    live_capability = await capability_waiter.probe_agent(
        hub=hub,
        vm=vm,
        agent_id=agent_id,
    )
    if not bool(live_capability.get("ok")):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "harmony_agent_capability_unavailable",
                "message": str(
                    live_capability.get("reason")
                    or "Agent 鸿蒙虚拟机能力不可用"
                ),
                "details": (
                    live_capability.get("details")
                    if isinstance(live_capability.get("details"), dict)
                    else {}
                ),
            },
        )
    # Do not block status/reconcile on this VM while waiting for the same
    # Agent to answer the capability probe.
    await transaction_lock(session, f"harmony-vm:{vm_id}")
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(404, "实例已删除")
    if expected_draft and not is_untouched_draft(vm):
        raise HTTPException(409, "新配置已被另一操作接管，本次换机不会重复启动")
    require_startable(vm)
    if vm_payload(vm) != probed_payload:
        raise HTTPException(409, "探查期间配置已修改，请重新探查")
    if vm.lease_token or vm.hdc_port is not None or vm.hdc_serial:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=LEASE_CLEANUP_REQUIRED)
    old_agent = (vm.assigned_agent_id or "").strip()
    if old_agent and old_agent != agent_id:
        return await _switch_agent(
            vm=vm,
            new_agent_id=agent_id,
            session=session,
            hub=hub,
        )
    excluded = capability_waiter.excluded_ports_for(agent_id)
    try:
        lease = await allocate_port_lease(
            session, vm, agent_id=agent_id, excluded_ports=excluded
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    require_owner_host(vm, hub)
    require_device_idle(hub, vm)
    first_bind = not bool(vm.assigned_agent_id)
    request_id = begin_operation(vm, "start", target_agent_id=agent_id,
        target_host_id=agent_host_id(hub, agent_id), first_bind=first_bind)
    payload = {
        "type": P.MSG_HARMONY_VM_START,
        **vm_payload(vm, request_id=request_id),
        "require_existing_instance": bool(lifecycle_runtime(vm).get("instance_created") or vm.started_at),
        "rollback_new_instance": first_bind,
        "operation_timeout_sec": lifecycle_runtime(vm)["operation"]["timeout_sec"],
        "assigned_port": lease.port,
        "lease_token": lease.lease_token,
        # 全局固定 UUID 与退役历史：非空时统一身份；清空后，Agent 只重置
        # 仍带历史共享值的旧实例，未启用过共享身份的实例保持 DevEco 默认值。
        **(await _harmony_identity_payload(session)),
    }
    # 先提交租约与 starting 状态，再下发。反序会有丢状态窗口：Agent 可能在
    # 几十毫秒内就回失败，而租约尚未落库，handle_vm_status 因查不到租约而丢弃
    # 该消息，界面永远卡在“启动中”。
    vm.state = "starting"
    vm.error_code = ""
    vm.error_message = ""
    vm.stopped_at = None
    maintenance(hub, vm, True)
    await session.commit()
    arm_operation_timeout(vm, hub)
    await session.refresh(vm)

    sent = await send_operation_once(hub, agent_id, payload)
    if not sent:
        await record_send_failure(session, vm, request_id, "启动下发失败，保留绑定与端口等待对账")
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED)
    return {"sent": True, "instance": vm.to_dict()}


async def _switch_agent(*, vm: HarmonyVmInstance, new_agent_id: str, session: AsyncSession, hub: Hub) -> Dict[str, Any]:
    await transaction_lock(session, f"harmony-vm:{vm.id}")
    try:
        vm = await get_vm_or_404(session, vm.id)
    except LookupError:
        raise HTTPException(404, "实例已删除")
    require_retirement_allowed(hub, vm)
    inherited = {key: getattr(vm, key) for key in ('name', 'alias', 'device_type', 'os_version', 'api_version', 'abi', 'image_id', 'screen_profile', 'screen_width', 'screen_height', 'density', 'screen_size_in', 'memory_gb', 'storage_gb', 'boot_mode', 'config_json')}
    old_vm_id = vm.id
    old_target, old_command, _ = await _retire_configuration(session, vm, hub)
    new_vm = HarmonyVmInstance(state="draft", **inherited)
    session.add(new_vm)
    await session.flush()
    await reserve_placeholder_alias(session, new_vm)
    # Retire old ID and establish the replacement configuration atomically.
    # An unreachable old WebSocket cannot leave the ledger with neither row.
    new_vm_id = new_vm.id
    await session.commit()
    await forget_device_route(hub, vm)
    cleanup_sent = await send_cleanup_once(hub, vm, old_target, old_command)
    try:
        result = await _send_start(new_vm_id, new_agent_id, session, hub, expected_draft=True)
        sent = bool(result.get("sent"))
        instance = result.get("instance") or new_vm.to_dict()
    except HTTPException as exc:
        await transaction_lock(session, f"harmony-vm:{new_vm_id}")
        current = await session.get(HarmonyVmInstance, new_vm_id, populate_existing=True)
        if current is None:
            await session.commit()
            raise HTTPException(409, "新配置已被其他操作删除") from exc
        # A different caller may have started/stopped/deleted the visible new
        # configuration while old-host cleanup was in flight. Never overwrite
        # its operation or result with this switch call's stale exception.
        if is_untouched_draft(current):
            current.state = "error"
            current.error_message = f"新配置启动失败：{exc.detail}"
            update_runtime(current, last_status={"state": "error", "ok": False,
                           "details": {"cleanup_confirmed": True, "instance_created": False}},
                           details={"cleanup_confirmed": True, "instance_created": False})
        instance = current.to_dict()
        await session.commit()
        sent = False
    return {"sent": sent, "switched": True, "old_vm_id": old_vm_id,
            "old_cleanup_sent": cleanup_sent, "instance": instance}

__all__ = ["router"]
