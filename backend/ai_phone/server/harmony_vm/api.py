"""Independent REST API for managed HarmonyOS virtual machines."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..api._deps import DBSession, HubDep
from ..api.submissions import RequireBearer
from ..hub import Hub
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
    now_utc,
    point_alias_to_runtime,
    quarantine_current_lease,
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
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
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
            if alias and vm.hdc_serial:
                await point_alias_to_runtime(session, vm, vm.hdc_serial)
            elif alias:
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
    for key in ("memory_gb", "storage_gb", "boot_mode", "config_json"):
        if key in patch and patch[key] is not None:
            setattr(vm, key, patch[key])
    await session.commit()
    await session.refresh(vm)
    return vm.to_dict()


@router.delete("/{vm_id}")
async def delete_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    # 删除的准入条件与 Android 完全一致：只拦"活动中"，不拦租约。
    # （Android: `if vm.state in ACTIVE_STATES` 之后即可删，见 android_vm/api.py）
    if vm.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="active vm cannot be deleted")
    old_agent = vm.assigned_agent_id or ""
    quarantined_port: Optional[int] = None
    if vm.lease_token:
        # 非活动态却仍持租约，只可能是 agent_offline / error / unavailable ——
        # 此时停止命令送不到 Agent，若按租约拦住删除就会死锁（配置永远删不掉）。
        # 处置：放行删除，但把端口转入 quarantined 而不是直接释放。这样既拿到了
        # 与 Android 相同的删除自由度，又不会在"端口是否真的空了"未确认时就把它
        # 重新分配出去。隔离端口需要显式 force-release 才回到可用池。
        quarantined_port = vm.hdc_port
        await quarantine_current_lease(
            session,
            vm,
            reason="vm_deleted_while_lease_held",
            error=f"配置在 {vm.state} 状态下被删除，端口保留隔离待人工确认",
            details={"deleted_vm_id": vm_id, "agent_id": old_agent},
        )
    await delete_owned_alias(session, vm)
    await session.delete(vm)
    await session.commit()
    cleanup_sent = False
    if old_agent:
        cleanup_sent = await hub.send_to_agent(old_agent, {
            "type": P.MSG_HARMONY_VM_DELETE,
            "request_id": _request_id(),
            "vm_id": vm_id,
        })
    return {
        "id": vm_id,
        "deleted": True,
        "cleanup_sent": cleanup_sent,
        "quarantined_port": quarantined_port,
    }


@router.post("/{vm_id}/force-release")
async def force_release_instance_lease(
    body: HarmonyVmForceReleaseReq,
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    """Explicit operator escape hatch; never called automatically."""
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    if not body.confirmed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="confirmed=true is required for force release",
        )
    if vm.state in ACTIVE_STATES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="active harmony vm cannot be force released",
        )
    if vm.lease_token:
        await reserve_placeholder_alias(session, vm)
        await release_port_lease(
            session,
            vm,
            reason=f"force_release:{body.reason.strip()}",
        )
    vm.assigned_agent_id = None
    vm.state = "stopped"
    vm.error_code = "lease_force_released"
    vm.error_message = body.reason.strip()
    vm.stopped_at = now_utc()
    await session.commit()
    await session.refresh(vm)
    return {
        "released": True,
        "warning": (
            "operator confirmed external cleanup; a still-running old Agent "
            "will be rejected by VM identity checks"
        ),
        "instance": vm.to_dict(),
    }


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
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    agent_id = (vm.assigned_agent_id or "").strip()
    if not agent_id or not hub.has_agent(agent_id):
        vm.assigned_agent_id = None
        vm.state = "stopped"
        vm.error_code = "redispatch_required"
        vm.error_message = REDISPATCH_REQUIRED
        await session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED)
    return await _send_start(vm_id, agent_id, session, hub)


@router.post("/{vm_id}/stop")
async def stop_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    if not vm.assigned_agent_id or not vm.lease_token:
        vm.state = "stopped"
        vm.stopped_at = now_utc()
        await reserve_placeholder_alias(session, vm)
        if vm.lease_token:
            await release_port_lease(session, vm, reason="stopped_without_agent")
        await session.commit()
        await session.refresh(vm)
        return {"sent": False, "instance": vm.to_dict()}
    sent = await hub.send_to_agent(vm.assigned_agent_id, {
        "type": P.MSG_HARMONY_VM_STOP,
        "request_id": _request_id(),
        "vm_id": vm.id,
        "hdc_serial": vm.hdc_serial or "",
        "lease_token": vm.lease_token,
    })
    vm.state = "stopping" if sent else "agent_offline"
    if not sent:
        vm.error_code = "agent_offline"
        vm.error_message = "assigned agent offline"
    await session.commit()
    await session.refresh(vm)
    return {"sent": sent, "instance": vm.to_dict()}


async def _send_start(
    vm_id: str,
    agent_id: str,
    session: AsyncSession,
    hub: Hub,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="harmony vm not found")
    if vm.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="active vm cannot be dispatched")
    old_agent = (vm.assigned_agent_id or "").strip()
    if old_agent and old_agent != agent_id:
        return await _switch_agent(
            vm=vm,
            new_agent_id=agent_id,
            session=session,
            hub=hub,
        )
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
    excluded = capability_waiter.excluded_ports_for(agent_id)
    try:
        lease = await allocate_port_lease(
            session, vm, agent_id=agent_id, excluded_ports=excluded
        )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    payload = {
        "type": P.MSG_HARMONY_VM_START,
        **vm_payload(vm, request_id=_request_id()),
        "assigned_port": lease.port,
        "lease_token": lease.lease_token,
        # 全局固定 UUID 与退役历史：非空时统一身份；清空后，Agent 只重置
        # 仍带历史共享值的旧实例，未启用过共享身份的实例保持 DevEco 默认值。
        **(await _harmony_identity_payload(session)),
    }
    # 先提交租约与 starting 状态，再下发。反序会有丢状态窗口：Agent 可能在
    # 几十毫秒内就回失败，而租约尚未落库，handle_vm_status 因查不到租约而丢弃
    # 该消息，界面永远卡在“启动中”。
    vm.assigned_agent_id = agent_id
    vm.state = "starting"
    vm.error_code = ""
    vm.error_message = ""
    vm.stopped_at = None
    await session.commit()
    await session.refresh(vm)

    sent = await hub.send_to_agent(agent_id, payload)
    if not sent:
        await release_port_lease(session, vm, reason="send_failed")
        vm.assigned_agent_id = None
        vm.state = "stopped"
        vm.error_code = "redispatch_required"
        vm.error_message = REDISPATCH_REQUIRED
        await session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED)
    return {"sent": True, "instance": vm.to_dict()}


async def _switch_agent(
    *,
    vm: HarmonyVmInstance,
    new_agent_id: str,
    session: AsyncSession,
    hub: Hub,
) -> Dict[str, Any]:
    """Copy Android's switch contract: retire old vm_id and create a new one."""
    old_agent = (vm.assigned_agent_id or "").strip()
    old_vm_id = vm.id
    old_serial = (vm.hdc_serial or "").strip()
    if vm.lease_token:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "old harmony vm cleanup is not confirmed; stop it before "
                "switching Agent"
            ),
        )
    capability_waiter = get_capability_waiter()
    live_capability = await capability_waiter.probe_agent(
        hub=hub,
        vm=vm,
        agent_id=new_agent_id,
    )
    if not bool(live_capability.get("ok")):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "harmony_agent_capability_unavailable",
                "message": str(
                    live_capability.get("reason")
                    or "新 Agent 鸿蒙虚拟机能力不可用"
                ),
                "details": (
                    live_capability.get("details")
                    if isinstance(live_capability.get("details"), dict)
                    else {}
                ),
            },
        )
    inherited = {
        "name": vm.name,
        "alias": vm.alias,
        "device_type": vm.device_type,
        "os_version": vm.os_version,
        "api_version": vm.api_version,
        "abi": vm.abi,
        "image_id": vm.image_id,
        "screen_profile": vm.screen_profile,
        "screen_width": vm.screen_width,
        "screen_height": vm.screen_height,
        "density": vm.density,
        "screen_size_in": vm.screen_size_in,
        "memory_gb": vm.memory_gb,
        "storage_gb": vm.storage_gb,
        "boot_mode": vm.boot_mode,
        "config_json": dict(vm.config_json or {}),
    }
    await delete_owned_alias(session, vm)
    await session.delete(vm)
    await session.flush()

    new_vm = HarmonyVmInstance(state="draft", **inherited)
    session.add(new_vm)
    await session.flush()
    await reserve_placeholder_alias(session, new_vm)
    excluded = capability_waiter.excluded_ports_for(new_agent_id)
    lease = await allocate_port_lease(
        session,
        new_vm,
        agent_id=new_agent_id,
        excluded_ports=excluded,
    )
    payload = {
        "type": P.MSG_HARMONY_VM_START,
        **vm_payload(new_vm, request_id=_request_id()),
        "assigned_port": lease.port,
        "lease_token": lease.lease_token,
        **(await _harmony_identity_payload(session)),
    }
    # 必须先提交租约与 starting 状态，再下发。
    # 反过来的话（先下发、后提交）存在丢状态的窗口：Agent 可能在几十毫秒内就返回
    # 失败，而此时 Server 这边租约和 lease_token 还没落库，handle_vm_status 查不到
    # 对应租约会直接丢弃该消息，界面就永远卡在“启动中”。
    new_vm.assigned_agent_id = new_agent_id
    new_vm.state = "starting"
    new_vm.error_code = ""
    new_vm.error_message = ""
    new_vm.stopped_at = None
    await session.commit()
    await session.refresh(new_vm)

    sent = await hub.send_to_agent(new_agent_id, payload)
    if not sent:
        # 已提交，不能再 rollback：显式回退到可重新下发的状态并释放租约。
        await reserve_placeholder_alias(session, new_vm)
        await release_port_lease(session, new_vm, reason="dispatch_send_failed")
        new_vm.state = "stopped"
        new_vm.error_code = "dispatch_send_failed"
        new_vm.error_message = "下发失败：Agent 连接不可用"
        await session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED)

    if old_agent:
        try:
            await hub.send_to_agent(
                old_agent,
                {
                    "type": P.MSG_HARMONY_VM_DELETE,
                    "request_id": _request_id(),
                    "vm_id": old_vm_id,
                    "hdc_serial": old_serial,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return {
        "sent": True,
        "switched": True,
        "old_vm_id": old_vm_id,
        "instance": new_vm.to_dict(),
    }


__all__ = ["router"]
