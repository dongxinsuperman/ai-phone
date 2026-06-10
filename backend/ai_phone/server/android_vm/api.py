"""Internal Android VM management API."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Path, status
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..models import (
    AndroidDeviceProfile,
    AndroidVmCoverageProfile,
    AndroidVmInstance,
    DeviceAlias,
)
from ..api._deps import DBSession, HubDep
from ..api.submissions import RequireBearer
from ..hub import Hub
from .catalog import (
    builtin_coverage_profiles,
    cap_profile_fields,
    clean_and_dedupe_profiles,
    parse_google_supported_devices_csv,
    parse_play_device_catalog_csv,
    resolution_bucket,
)

# 官方目录层 source_type：导入时全量覆盖这一层；其它 source_type（人工/精选）保留。
PLAY_CATALOG_SOURCE = "google_play_device_catalog"
from .schemas import (
    AndroidDeviceProfileImportReq,
    AndroidVmCreateReq,
    AndroidVmDispatchReq,
    AndroidVmPatchReq,
)
from .service import (
    apply_vm_patch,
    delete_vm_alias,
    get_capability_waiter,
    get_vm_or_404,
    now_utc,
    sync_vm_alias,
    vm_payload,
)

ACTIVE_STATES = {"starting", "running", "stopping"}
REDISPATCH_REQUIRED_DETAIL = "assigned agent offline; please probe and dispatch again"

router = APIRouter(
    prefix="/api/internal/vm/instances",
    tags=["internal-android-vm"],
    dependencies=[RequireBearer],
)

catalog_router = APIRouter(
    prefix="/api/internal/vm",
    tags=["internal-android-vm-catalog"],
    dependencies=[RequireBearer],
)


@router.get("")
async def list_instances(session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    res = await session.execute(select(AndroidVmInstance).order_by(AndroidVmInstance.created_at.desc()))
    return [row.to_dict() for row in res.scalars().all()]


def _profile_matches_request(prof: AndroidDeviceProfile, body: AndroidVmCreateReq) -> bool:
    """real_device 一致性：请求的画像参数必须与档案吻合，否则视为被篡改。

    屏幕宽高（档案有值时）必须相等；api_level 必须落在档案支持的 SDK 版本内。
    档案缺该字段时跳过对应校验（不过度拒绝）。
    """
    if prof.screen_width and int(prof.screen_width) != int(body.screen_width):
        return False
    if prof.screen_height and int(prof.screen_height) != int(body.screen_height):
        return False
    apis = set()
    for item in (prof.sdk_versions or []):
        m = re.search(r"\d+", str(item))
        if m:
            apis.add(int(m.group()))
    if apis and int(body.api_level) not in apis:
        return False
    return True


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_instance(
    body: AndroidVmCreateReq,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    # 单一身份口径：别名是设备身份，name 永远镜像 alias（schema 的 name 仅作兼容字段，
    # 即便有人绕过前端传 name != alias 也会被强制对齐，杜绝双名）。
    # 创建时别名必填 + 唯一：强制第一次就给一个能分辨的名字（创建后可随意改、甚至改空）。
    alias = _normalize_alias(body.alias, fallback=body.name)
    if not alias:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"reason": "alias_required", "message": "创建时设备别名必填"},
        )
    name = alias
    await _ensure_alias_available(session, alias=alias)
    config_json = _normalize_vm_config(body)
    # 声明为真机（real_device）必须指向一条 verified 设备档案，否则降级为 custom，
    # 防止前端（含空库默认表单）绕过「真实设备库」口径凭空造一台“真机”。
    profile_ref_type = body.profile_ref_type.strip() or "custom"
    profile_ref_id = body.profile_ref_id.strip()
    profile_id = body.profile_id.strip()
    profile_name = body.profile_name.strip()
    capability_marks = body.capability_marks
    if profile_ref_type == "real_device":
        prof = await session.get(AndroidDeviceProfile, profile_ref_id) if profile_ref_id else None
        ok = prof is not None and (prof.verification_status or "") == "verified"
        if ok:
            ok = _profile_matches_request(prof, body)
        if not ok:
            # 不合法（无档案 / 未验证 / 画像参数与档案不符）→ 降级为自定义，
            # 连真机身份字段一并清空，避免“显示像真机、身份却是 custom 或被改过参数”。
            profile_ref_type = "custom"
            profile_ref_id = ""
            profile_id = ""
            profile_name = ""
            capability_marks = {}
            config_json["identity"] = {}
    vm = AndroidVmInstance(
        name=name,
        alias=alias,
        profile_ref_type=profile_ref_type,
        profile_ref_id=profile_ref_id,
        profile_id=profile_id,
        profile_name=profile_name,
        config_version=body.config_version,
        config_json=config_json,
        capability_marks=capability_marks,
        api_level=body.api_level,
        abi=_normalize_abi(body.abi),
        system_type=body.system_type.strip() or "google_apis",
        system_image=body.system_image.strip(),
        screen_width=body.screen_width,
        screen_height=body.screen_height,
        density=body.density,
        orientation=body.orientation,
        state="draft",
    )
    session.add(vm)
    await session.commit()
    await session.refresh(vm)
    return vm.to_dict()


@router.get("/{vm_id}")
async def get_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    return vm.to_dict()


@router.patch("/{vm_id}")
async def patch_instance(
    body: AndroidVmPatchReq,
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    patch = body.model_dump(exclude_unset=True)
    # 别名是设备身份，像真机一样随时可改（含运行态）。其余物理/运行参数仍要求非运行态。
    # name 不再单独可改：永远镜像 alias，这里直接丢弃外部传入的 name。
    patch.pop("name", None)
    alias_only = set(patch.keys()).issubset({"alias"})
    if not alias_only and vm.state not in (
        "draft",
        "stopped",
        "error",
        "unavailable",
        "agent_offline",
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="running vm cannot be edited")
    if "abi" in patch and patch["abi"] is not None:
        patch["abi"] = _normalize_abi(str(patch["abi"]))
    for key in (
        "profile_ref_type",
        "profile_ref_id",
        "profile_id",
        "profile_name",
        "orientation",
        "system_type",
        "system_image",
    ):
        if key in patch and patch[key] is not None:
            patch[key] = str(patch[key]).strip()
    alias_changed = False
    if "alias" in patch:
        # 创建后别名可自由改（与真机一致），含改空：唯一身份由 vm_id 锚定。
        # 注意这里不做 fallback——显式传入才是用户意图，传空字符串即"清空别名"。
        raw_alias = patch.get("alias")
        requested_alias = ("" if raw_alias is None else str(raw_alias)).strip()[:128]
        if requested_alias != (vm.alias or ""):
            if requested_alias:  # 只有非空别名才查重（空别名允许多台并存）
                await _ensure_alias_available(
                    session, alias=requested_alias, vm_id=vm.id, current_serial=vm.adb_serial or "",
                )
            # 先按旧别名删 DeviceAlias 映射（必须在改 vm.alias 之前）
            await delete_vm_alias(session, vm)
            patch["alias"] = requested_alias
            patch["name"] = requested_alias  # name 镜像 alias，杜绝双名残留
            alias_changed = True
        else:
            patch.pop("alias", None)
    if "config_json" in patch and patch["config_json"] is not None:
        patch["config_json"] = _normalize_vm_config_dict(
            patch["config_json"],
            fallback={
                "api_level": patch.get("api_level", vm.api_level),
                "system_type": patch.get("system_type", vm.system_type),
                "abi": patch.get("abi", vm.abi),
                "screen_width": patch.get("screen_width", vm.screen_width),
                "screen_height": patch.get("screen_height", vm.screen_height),
                "density": patch.get("density", vm.density),
                "orientation": patch.get("orientation", vm.orientation),
            },
        )
    apply_vm_patch(vm, patch)
    # 运行态改名：把新别名映射指到当前 emulator serial（旧映射已在上面按旧名删除）
    if alias_changed and vm.adb_serial and vm.state in ACTIVE_STATES:
        await sync_vm_alias(session, vm, vm.adb_serial)
    await session.commit()
    await session.refresh(vm)
    return vm.to_dict()


# RAM 档位（key, 下界含, 上界不含 MB）。前后端共用这套定义，保证筛选与 facet 一致。
RAM_BUCKETS: List[tuple] = [
    ("<2G", 0, 2048),
    ("2-4G", 2048, 4096),
    ("4-6G", 4096, 6144),
    ("6-8G", 6144, 8192),
    ("8-12G", 8192, 12288),
    ("12G+", 12288, None),
]
_RAM_BUCKET_MAP = {key: (lo, hi) for key, lo, hi in RAM_BUCKETS}


def _csv_values(raw: str) -> List[str]:
    """逗号分隔多选参数 → 去空去重的有序列表。"""
    seen: List[str] = []
    for part in (raw or "").split(","):
        v = part.strip()
        if v and v not in seen:
            seen.append(v)
    return seen


# Android Emulator 最低支持 API 21（Android 5）。只有含 ≥21 版本的机型才能真正创建；
# 低于 21 的老机型展示了也会创建失败（422），所以列表/facet/计数统一过滤掉。
_BUILDABLE_SDKS = tuple(range(21, 61))


def _buildable_clause():
    """机型支持的 SDK 版本里至少有一个 ≥21（可在 Emulator 创建）。"""
    return or_(*[AndroidDeviceProfile.sdk_index.like(f"%;{n};%") for n in _BUILDABLE_SDKS])


def _ram_clause(keys: List[str]):
    """把若干 RAM 档位 key 合成一个 OR 条件（命中任一档即可）。"""
    ranges = []
    for key in keys:
        bound = _RAM_BUCKET_MAP.get(key)
        if not bound:
            continue
        lo, hi = bound
        conds = [AndroidDeviceProfile.ram_mb.isnot(None), AndroidDeviceProfile.ram_mb >= lo]
        if hi is not None:
            conds.append(AndroidDeviceProfile.ram_mb < hi)
        ranges.append(and_(*conds))
    return or_(*ranges) if ranges else None


# 可参与「联动 facet」的筛选维度名；计算某维度的 facet 时会排除它自身的筛选。
_FILTER_DIMS = ("q", "form_factor", "brand", "sdk", "resolution", "ram")

# 首屏列表排序：国内主流品牌优先（与前端品牌榜一致，全小写）。
# 不选品牌时让华为/小米/OPPO 等排在 10.or / 2E 等长尾前面。
PREFERRED_BRANDS = [
    "huawei", "vivo", "oppo", "honor", "xiaomi", "redmi", "realme", "oneplus",
    "iqoo", "meizu", "nubia", "poco", "zte", "samsung", "motorola", "hisense",
    "coolpad", "gionee", "blackshark", "nothing",
]


def _profile_filter_clauses(
    *,
    q: str = "",
    form_factor: str = "",
    brand: str = "",
    sdk: str = "",
    resolution: str = "",
    ram: str = "",
    exclude: str = "",
) -> list:
    """根据多维筛选参数生成 where 子句列表（只查已验证数据）。

    exclude 指定的维度会被跳过，用于 facet 联动计数（算某维度选项时排除它自己）。
    多词搜索：空格分词后逐词 AND，每个词在多个身份字段间 OR；不依赖精确大小写。
    """
    clauses = [
        AndroidDeviceProfile.verification_status == "verified",
        _buildable_clause(),  # 只保留可在 Emulator 创建（≥API21）的机型
    ]
    if exclude != "q":
        for token in (q or "").split():
            tok = token.strip()
            if not tok:
                continue
            like = f"%{tok}%"
            clauses.append(
                or_(
                    AndroidDeviceProfile.manufacturer.ilike(like),
                    AndroidDeviceProfile.brand.ilike(like),
                    AndroidDeviceProfile.series.ilike(like),
                    AndroidDeviceProfile.device.ilike(like),
                    AndroidDeviceProfile.model_code.ilike(like),
                    AndroidDeviceProfile.marketing_name.ilike(like),
                )
            )
    if exclude != "form_factor":
        ff = (form_factor or "").strip()
        if ff:
            clauses.append(func.lower(AndroidDeviceProfile.form_factor) == ff.lower())
    if exclude != "brand":
        brands = [b.lower() for b in _csv_values(brand)]
        if brands:
            clauses.append(func.lower(AndroidDeviceProfile.brand).in_(brands))
    if exclude != "resolution":
        resos = _csv_values(resolution)
        if resos:
            clauses.append(AndroidDeviceProfile.resolution_bucket.in_(resos))
    if exclude != "sdk":
        sdks = _csv_values(sdk)
        if sdks:
            clauses.append(
                or_(*[AndroidDeviceProfile.sdk_index.like(f"%;{s};%") for s in sdks])
            )
    if exclude != "ram":
        ram_clause = _ram_clause(_csv_values(ram))
        if ram_clause is not None:
            clauses.append(ram_clause)
    return clauses


@catalog_router.get("/device-profiles")
async def list_device_profiles(
    q: str = "",
    form_factor: str = "",
    brand: str = "",
    sdk: str = "",
    resolution: str = "",
    ram: str = "",
    offset: int = 0,
    limit: int = 60,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    page_size = max(1, min(int(limit or 60), 200))
    page_offset = max(0, int(offset or 0))
    stats = await _device_profile_stats(session)
    # 全部服务端筛选 + 分页：只查已验证（CSV 导入）数据，不兜底假数据。
    clauses = _profile_filter_clauses(
        q=q, form_factor=form_factor, brand=brand, sdk=sdk,
        resolution=resolution, ram=ram,
    )
    base = select(AndroidDeviceProfile).where(*clauses)
    matched_total = int(
        (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        or 0
    )
    # 精准匹配优先：搜索词与型号/代号/品牌完全相等的排最前，其次子串靠前命中。
    order_cols = []
    query = (q or "").strip()
    if query:
        ql = query.lower()
        exact_rank = case(
            (func.lower(AndroidDeviceProfile.model_code) == ql, 0),
            (func.lower(AndroidDeviceProfile.marketing_name) == ql, 0),
            (func.lower(AndroidDeviceProfile.device) == ql, 0),
            (func.lower(AndroidDeviceProfile.brand) == ql, 1),
            (func.lower(AndroidDeviceProfile.marketing_name).like(f"{ql}%"), 2),
            else_=3,
        )
        order_cols.append(exact_rank.asc())
    # 主流品牌优先（首屏不再被 10.or / 2E 等长尾占满）
    brand_rank = case(
        *[(func.lower(AndroidDeviceProfile.brand) == b, i) for i, b in enumerate(PREFERRED_BRANDS)],
        else_=len(PREFERRED_BRANDS),
    )
    order_cols.append(brand_rank.asc())
    order_cols.extend([
        AndroidDeviceProfile.brand.asc(),
        AndroidDeviceProfile.marketing_name.asc(),
        AndroidDeviceProfile.id.asc(),
    ])
    page_stmt = base.order_by(*order_cols).offset(page_offset).limit(page_size)
    res = await session.execute(page_stmt)
    rows = [row.to_dict() for row in res.scalars().all()]
    dispatchable_template_total = stats["db_verified_total"]
    return {
        "items": rows,
        "page": {
            "offset": page_offset,
            "limit": page_size,
            "returned": len(rows),
            "total": matched_total,
            "has_more": (page_offset + len(rows)) < matched_total,
        },
        "stats": {
            **stats,
            "visible_total": dispatchable_template_total,
            "dispatchable_template_total": dispatchable_template_total,
            "matched_total": matched_total,
            "fallback_total": 0,
            "displayed_total": len(rows),
            "returned": len(rows),
            "limit": page_size,
            "using_builtin_fallback": False,
        },
        "source_policy": {
            "real_devices_require_source": True,
            "primary_source": "google_play_device_catalog",
            "builtin_presets_are_source_backed": False,
            "builtin_presets_are_fallback_only": False,
            "only_verified_profiles_are_selectable": True,
            "empty_means_no_match_or_not_imported": True,
            "candidate_pending_is_hidden": True,
            "no_artificial_catalog_limit": True,
            "facets": ["form_factor(默认手机+平板)", "brand", "sdk", "resolution"],
            "dropped_dimensions": ["popularity(install_base 无数据)", "screen_cutout(目录无字段)"],
        },
    }


@catalog_router.post("/device-profiles/import-play-catalog")
async def import_play_device_catalog(
    body: AndroidDeviceProfileImportReq,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    """前台导入官方目录 CSV（无密码）：解析 → 预清洗 → 覆盖式替换官方层。

    覆盖语义：只删除 source_type == 官方目录 的「官方层」，再灌入清洗后的新数据；
    其它 source_type（人工精选 / 手工补充层）不受影响。
    """
    collected_at = _parse_optional_datetime(body.collected_at)
    raw_rows = parse_play_device_catalog_csv(
        body.csv_text,
        source_url=body.source_url.strip(),
        collected_at=collected_at,
    )
    rows = clean_and_dedupe_profiles(raw_rows)
    # 覆盖式：清掉官方层旧数据（人工补充层 source_type 不同，不动）
    deleted = await session.execute(
        delete(AndroidDeviceProfile).where(
            AndroidDeviceProfile.source_type == PLAY_CATALOG_SOURCE
        )
    )
    for data in rows:
        session.add(AndroidDeviceProfile(**data))
    await session.commit()
    return {
        "mode": "replace",
        "raw_total": len(raw_rows),
        "imported": len(rows),
        "removed_old": int(deleted.rowcount or 0),
        "dropped_by_clean": len(raw_rows) - len(rows),
    }


@catalog_router.post("/device-profiles/import-google-supported-devices")
async def import_google_supported_devices(
    body: AndroidDeviceProfileImportReq,
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    collected_at = _parse_optional_datetime(body.collected_at)
    rows = parse_google_supported_devices_csv(
        body.csv_text,
        source_url=body.source_url.strip() or "https://storage.googleapis.com/play_public/supported_devices.csv",
        collected_at=collected_at,
    )
    imported = 0
    updated = 0
    for data in rows:
        cap_profile_fields(data)
        existing = await _find_existing_profile(session, data)
        if existing is None:
            session.add(AndroidDeviceProfile(**data))
            imported += 1
        else:
            for key, value in data.items():
                setattr(existing, key, value)
            updated += 1
    if rows:
        await session.commit()
    return {
        "imported": imported,
        "updated": updated,
        "total": len(rows),
        "verification_status": "candidate_pending",
        "selectable": False,
    }


@catalog_router.get("/device-brands")
async def list_device_brands(
    form_factor: str = "",
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    """品牌选项（带计数），供前端"按机型"全量品牌筛选用。只统计已验证设备。"""
    stmt = select(
        AndroidDeviceProfile.brand, func.count(AndroidDeviceProfile.id)
    ).where(AndroidDeviceProfile.verification_status == "verified")
    ff = (form_factor or "").strip()
    if ff:
        stmt = stmt.where(func.lower(AndroidDeviceProfile.form_factor) == ff.lower())
    stmt = stmt.group_by(AndroidDeviceProfile.brand).order_by(
        func.count(AndroidDeviceProfile.id).desc(),
        AndroidDeviceProfile.brand.asc(),
    )
    res = await session.execute(stmt)
    items = [
        {"brand": (b or "").strip(), "count": int(c or 0)}
        for b, c in res.all()
        if (b or "").strip()
    ]
    return {"items": items, "total": len(items)}


@catalog_router.get("/device-facets")
async def list_device_facets(
    q: str = "",
    form_factor: str = "",
    brand: str = "",
    sdk: str = "",
    resolution: str = "",
    ram: str = "",
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    """多维联动 facet：返回各维度可选值及其计数。

    计算某一维度的选项时，会应用「除该维度以外」的全部筛选条件，
    这样勾选品牌后系统/分辨率/内存的计数会随之更新，避免点出 0 结果。
    """
    filters = dict(
        q=q, form_factor=form_factor, brand=brand,
        sdk=sdk, resolution=resolution, ram=ram,
    )

    async def grouped(col, exclude: str) -> Dict[Any, int]:
        clauses = _profile_filter_clauses(**filters, exclude=exclude)
        stmt = select(col, func.count()).where(*clauses).group_by(col)
        res = await session.execute(stmt)
        return {row[0]: int(row[1] or 0) for row in res.all()}

    async def scalar_count(clauses) -> int:
        sub = select(AndroidDeviceProfile.id).where(*clauses).subquery()
        return int(
            (await session.execute(select(func.count()).select_from(sub))).scalar_one() or 0
        )

    ff_map = await grouped(AndroidDeviceProfile.form_factor, "form_factor")
    brand_map = await grouped(AndroidDeviceProfile.brand, "brand")
    reso_map = await grouped(AndroidDeviceProfile.resolution_bucket, "resolution")

    # SDK：按候选 api 逐个统计（sdk_index 形如 ;31;33;）。
    sdk_base = _profile_filter_clauses(**filters, exclude="sdk")
    sdk_items = []
    for api in range(36, 20, -1):
        cnt = await scalar_count(sdk_base + [AndroidDeviceProfile.sdk_index.like(f"%;{api};%")])
        if cnt:
            sdk_items.append({"id": api, "count": cnt})

    # RAM：按档位逐个统计，保持档位固定顺序。
    ram_base = _profile_filter_clauses(**filters, exclude="ram")
    ram_items = []
    for key, _lo, _hi in RAM_BUCKETS:
        clause = _ram_clause([key])
        cnt = await scalar_count(ram_base + [clause]) if clause is not None else 0
        ram_items.append({"id": key, "count": cnt})

    matched_total = await scalar_count(_profile_filter_clauses(**filters))

    return {
        "form_factor": [
            {"id": k, "count": v}
            for k, v in sorted(ff_map.items(), key=lambda kv: -kv[1])
            if k
        ],
        "brand": [
            {"id": (k or "").strip(), "count": v}
            for k, v in sorted(brand_map.items(), key=lambda kv: (-kv[1], (kv[0] or "")))
            if (k or "").strip()
        ],
        "sdk": sdk_items,
        "resolution": [{"id": k, "count": v} for k, v in reso_map.items() if k],
        "ram": ram_items,
        "matched_total": matched_total,
    }


@catalog_router.get("/coverage-profiles")
async def list_coverage_profiles(session: AsyncSession = DBSession) -> Dict[str, Any]:
    res = await session.execute(
        select(AndroidVmCoverageProfile).order_by(AndroidVmCoverageProfile.name.asc())
    )
    rows = [row.to_dict() for row in res.scalars().all()]
    if not rows:
        rows = builtin_coverage_profiles()
    return {
        "items": rows,
        "source_policy": {
            "source_type": "internal_strategy",
            "not_real_device": True,
        },
    }


@router.delete("/{vm_id}")
async def delete_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    if vm.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="active vm cannot be deleted")
    # 顺序：先删 DB（提交成功）→ 再 best-effort 通知 Agent 清远端 AVD。
    # 反过来"先通知后删 DB"若 commit 失败，会变成"AVD 已删、配置还在"的不一致；
    # 漏通知（Agent 离线）由 reconcile「库里没有就删」兜底。与 _switch_agent 口径一致。
    old_agent = (vm.assigned_agent_id or "").strip()
    old_serial = (vm.adb_serial or "").strip()
    alias_deleted = await delete_vm_alias(session, vm)
    await session.delete(vm)
    await session.commit()
    cleanup_sent = False
    if old_agent:
        try:
            cleanup_sent = await hub.send_to_agent(old_agent, {
                "type": P.MSG_VM_DELETE,
                "request_id": _request_id(),
                "vm_id": vm_id,
                "adb_serial": old_serial,
            })
        except Exception:  # noqa: BLE001
            cleanup_sent = False
    return {
        "id": vm_id,
        "deleted": True,
        "alias_deleted": alias_deleted,
        "avd_cleanup_sent": cleanup_sent,
    }


async def _notify_agent_cleanup_avd(hub: Hub, agent_id: str | None, vm: AndroidVmInstance) -> bool:
    """通知指定 Agent 清理某 VM 的远端 AVD（删除/换绑时调用）。Agent 侧执行 avdmanager delete。"""
    target = (agent_id or "").strip()
    if not target:
        return False
    try:
        return await hub.send_to_agent(
            target,
            {
                "type": P.MSG_VM_DELETE,
                "request_id": _request_id(),
                "vm_id": vm.id,
                "adb_serial": vm.adb_serial or "",
            },
        )
    except Exception:  # noqa: BLE001
        return False


@router.post("/{vm_id}/dispatch-candidates")
async def dispatch_candidates(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    return await get_capability_waiter().probe(hub=hub, vm=vm)


@router.post("/{vm_id}/dispatch")
async def dispatch_instance(
    body: AndroidVmDispatchReq,
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    agent_id = body.agent_id.strip()
    if not hub.has_agent(agent_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED_DETAIL)
    return await _send_start(
        vm_id=vm_id,
        agent_id=agent_id,
        session=session,
        hub=hub,
        clear_assignment_on_send_failure=True,
    )


@router.post("/{vm_id}/start")
async def start_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    assigned_agent_id = (vm.assigned_agent_id or "").strip()
    if not assigned_agent_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="vm has no assigned agent")
    if not hub.has_agent(assigned_agent_id):
        await _clear_vm_assignment_for_redispatch(vm, session)
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED_DETAIL)
    return await _send_start(
        vm_id=vm_id,
        agent_id=assigned_agent_id,
        session=session,
        hub=hub,
        clear_assignment_on_send_failure=True,
    )


@router.post("/{vm_id}/stop")
async def stop_instance(
    vm_id: str = Path(..., min_length=1, max_length=64),
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    if not vm.assigned_agent_id:
        vm.state = "stopped"
        vm.adb_serial = None
        vm.stopped_at = now_utc()
        await session.commit()
        await session.refresh(vm)
        return {"sent": False, "instance": vm.to_dict()}
    sent = await hub.send_to_agent(
        vm.assigned_agent_id,
        {
            "type": P.MSG_VM_STOP,
            "request_id": _request_id(),
            "vm_id": vm.id,
            "adb_serial": vm.adb_serial or "",
        },
    )
    vm.state = "stopping" if sent else "unavailable"
    if not sent:
        vm.error_message = "assigned agent offline"
        vm.adb_serial = None
        vm.stopped_at = now_utc()
    await session.commit()
    await session.refresh(vm)
    return {"sent": sent, "instance": vm.to_dict()}


async def _send_start(
    *,
    vm_id: str,
    agent_id: str,
    session: AsyncSession,
    hub: Hub,
    clear_assignment_on_send_failure: bool = False,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="android vm not found")
    if vm.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="active vm cannot be dispatched")
    # 换 Agent（下发到与原来不同的、且已绑定过的 Agent）：删旧 vm_id + 新建 vm_id（继承别名/配置）。
    # 这样旧 Agent 即便离线漏了清理，回来报旧 vmid 也会因"库里没有"被 reconcile 清掉，
    # 不会出现"旧 Agent 抢回 / 两台同款同时在跑"。详见 android-vm-plan §21.3。
    old_agent = (vm.assigned_agent_id or "").strip()
    if old_agent and old_agent != agent_id:
        return await _switch_agent(vm=vm, new_agent_id=agent_id, session=session, hub=hub)
    payload = {
        "type": P.MSG_VM_START,
        **vm_payload(vm, request_id=_request_id()),
    }
    sent = await hub.send_to_agent(agent_id, payload)
    if not sent and clear_assignment_on_send_failure:
        await _clear_vm_assignment_for_redispatch(vm, session)
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED_DETAIL)
    vm.assigned_agent_id = agent_id
    vm.state = "starting" if sent else "unavailable"
    vm.error_message = "" if sent else "selected agent offline"
    if sent:
        vm.stopped_at = None
    await session.commit()
    await session.refresh(vm)
    return {"sent": sent, "instance": vm.to_dict()}


async def _clear_vm_assignment_for_redispatch(
    vm: AndroidVmInstance,
    session: AsyncSession,
) -> None:
    vm.assigned_agent_id = None
    vm.state = "stopped"
    vm.error_message = REDISPATCH_REQUIRED_DETAIL
    vm.adb_serial = None
    vm.stopped_at = now_utc()
    await session.commit()


async def _switch_agent(
    *, vm: AndroidVmInstance, new_agent_id: str, session: AsyncSession, hub: Hub
) -> Dict[str, Any]:
    """换 Agent：删旧 vm_id（通知旧 Agent 清 AVD + 清旧别名映射）+ 新建 vm_id（继承别名/配置）绑新 Agent。"""
    old_agent = (vm.assigned_agent_id or "").strip()
    old_vm_id = vm.id
    old_serial = (vm.adb_serial or "").strip()
    # 1) 快照旧机全部可继承字段（别名 / 画像 / 运行参数），新机原样复制
    inherited = dict(
        name=vm.name,
        alias=vm.alias,
        profile_ref_type=vm.profile_ref_type,
        profile_ref_id=vm.profile_ref_id,
        profile_id=vm.profile_id,
        profile_name=vm.profile_name,
        config_version=vm.config_version,
        config_json=dict(vm.config_json or {}),
        capability_marks=dict(vm.capability_marks or {}),
        api_level=vm.api_level,
        abi=vm.abi,
        system_type=vm.system_type,
        system_image=vm.system_image,
        screen_width=vm.screen_width,
        screen_height=vm.screen_height,
        density=vm.density,
        orientation=vm.orientation,
    )
    # 2) 删旧 vm（含别名映射），flush 释放别名供新 vm 复用
    await delete_vm_alias(session, vm)
    await session.delete(vm)
    await session.flush()
    # 3) 新建 vm（新 vm_id 自动生成；别名/配置全继承），绑新 Agent
    new_vm = AndroidVmInstance(state="draft", **inherited)
    session.add(new_vm)
    await session.flush()
    # 4) 发 start 给新 Agent
    payload = {"type": P.MSG_VM_START, **vm_payload(new_vm, request_id=_request_id())}
    sent = await hub.send_to_agent(new_agent_id, payload)
    if not sent:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED_DETAIL)
    new_vm.assigned_agent_id = new_agent_id
    new_vm.state = "starting"
    new_vm.error_message = ""
    new_vm.stopped_at = None
    await session.commit()
    await session.refresh(new_vm)
    # 5) DB 提交成功后，才通知旧 Agent 清理旧 AVD（在线即删；离线靠 reconcile「库里没有就删」兜底）。
    #    放到 commit 之后：避免上面任何步骤异常回滚后，旧 AVD 已被删而 DB 仍是旧 vm 的不一致。
    if old_agent:
        try:
            await hub.send_to_agent(old_agent, {
                "type": P.MSG_VM_DELETE,
                "request_id": _request_id(),
                "vm_id": old_vm_id,
                "adb_serial": old_serial,
            })
        except Exception:  # noqa: BLE001
            pass
    return {
        "sent": sent,
        "switched": True,
        "old_vm_id": old_vm_id,
        "instance": new_vm.to_dict(),
    }


def _normalize_abi(value: str) -> str:
    raw = (value or "auto").strip().lower()
    if raw == "arm64-v8a":
        return "arm64"
    if raw not in {"auto", "arm64", "x86_64"}:
        return "auto"
    return raw


def _normalize_alias(value: str, *, fallback: str) -> str:
    alias = (value or "").strip()
    if not alias:
        alias = (fallback or "").strip()
    return alias[:128]


def _normalize_vm_config(body: AndroidVmCreateReq) -> Dict[str, Any]:
    return _normalize_vm_config_dict(
        body.config_json,
        fallback={
            "api_level": body.api_level,
            "system_type": body.system_type,
            "abi": _normalize_abi(body.abi),
            "screen_width": body.screen_width,
            "screen_height": body.screen_height,
            "density": body.density,
            "orientation": body.orientation,
        },
    )


def _normalize_vm_config_dict(
    config: Dict[str, Any],
    *,
    fallback: Dict[str, Any],
) -> Dict[str, Any]:
    cfg = config if isinstance(config, dict) else {}
    return {
        "system": {
            "api_level": int(_nested(cfg, "system", "api_level", fallback.get("api_level", 35))),
            "system_type": str(_nested(cfg, "system", "system_type", fallback.get("system_type", "google_apis")) or "google_apis"),
            "abi": _normalize_abi(str(_nested(cfg, "system", "abi", fallback.get("abi", "auto")))),
        },
        "display": {
            "screen_width": int(_nested(cfg, "display", "screen_width", fallback.get("screen_width", 1080))),
            "screen_height": int(_nested(cfg, "display", "screen_height", fallback.get("screen_height", 2400))),
            "density": int(_nested(cfg, "display", "density", fallback.get("density", 420))),
            "orientation": str(_nested(cfg, "display", "orientation", fallback.get("orientation", "portrait")) or "portrait"),
            "screen_size_in": str(_nested(cfg, "display", "screen_size_in", "")),
            "cutout": str(_nested(cfg, "display", "cutout", "")),
            "shape_note": str(_nested(cfg, "display", "shape_note", "")),
        },
        "performance": {
            "ram_mb": int(_nested(cfg, "performance", "ram_mb", 4096)),
            "cpu_cores": int(_nested(cfg, "performance", "cpu_cores", 4)),
            "vm_heap_mb": int(_nested(cfg, "performance", "vm_heap_mb", 384)),
            "gpu_mode": str(_nested(cfg, "performance", "gpu_mode", "auto") or "auto"),
        },
        "storage": {
            "internal_storage_mb": int(_nested(cfg, "storage", "internal_storage_mb", 8192)),
            "sdcard_mb": int(_nested(cfg, "storage", "sdcard_mb", 0)),
            "wipe_data": bool(_nested(cfg, "storage", "wipe_data", False)),
            "snapshot_policy": str(_nested(cfg, "storage", "snapshot_policy", "discard_changes") or "discard_changes"),
        },
        "network": {
            "speed": str(_nested(cfg, "network", "speed", "full") or "full"),
            "delay": str(_nested(cfg, "network", "delay", "none") or "none"),
            "dns_server": str(_nested(cfg, "network", "dns_server", "")),
            "http_proxy": str(_nested(cfg, "network", "http_proxy", "")),
        },
        "hardware": {
            "back_camera": str(_nested(cfg, "hardware", "back_camera", "emulated") or "emulated"),
            "front_camera": str(_nested(cfg, "hardware", "front_camera", "none") or "none"),
            "gps": bool(_nested(cfg, "hardware", "gps", True)),
            "accelerometer": bool(_nested(cfg, "hardware", "accelerometer", True)),
            "gyroscope": bool(_nested(cfg, "hardware", "gyroscope", True)),
            "proximity": bool(_nested(cfg, "hardware", "proximity", False)),
            "hardware_keyboard": bool(_nested(cfg, "hardware", "hardware_keyboard", False)),
            "navigation_style": str(_nested(cfg, "hardware", "navigation_style", "none") or "none"),
        },
        "startup": {
            "no_window": bool(_nested(cfg, "startup", "no_window", True)),
            "no_audio": bool(_nested(cfg, "startup", "no_audio", True)),
            "no_boot_anim": bool(_nested(cfg, "startup", "no_boot_anim", True)),
            "writable_system": bool(_nested(cfg, "startup", "writable_system", False)),
        },
        "identity": dict(cfg.get("identity") or {}),
    }


def _nested(config: Dict[str, Any], group: str, key: str, default: Any) -> Any:
    value = config.get(group)
    if not isinstance(value, dict):
        return default
    return value.get(key, default)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _profile_passes_facets(row: Dict[str, Any], facets: Dict[str, str]) -> bool:
    """维度精筛：设备类型 / 品牌 / Android 版本(SDK) / 分辨率档。"""
    ff = facets.get("form_factor") or ""
    if ff and str(row.get("form_factor") or "").strip().lower() != ff.lower():
        return False
    brand = facets.get("brand") or ""
    if brand and str(row.get("brand") or "").strip().lower() != brand.lower():
        return False
    sdk = facets.get("sdk") or ""
    if sdk:
        sdks = [str(x) for x in (row.get("sdk_versions") or [])]
        if str(sdk) not in sdks:
            return False
    reso = facets.get("resolution") or ""
    if reso:
        bucket = (row.get("raw") or {}).get("resolution_bucket") or resolution_bucket(
            row.get("screen_width"), row.get("screen_height")
        )
        if bucket != reso:
            return False
    return True


def _device_profile_matches_query(row: Dict[str, Any], query: str) -> bool:
    raw = (query or "").strip().lower()
    if not raw:
        return True
    haystack = " ".join(
        str(value or "")
        for value in (
            row.get("manufacturer"),
            row.get("brand"),
            row.get("series"),
            row.get("device"),
            row.get("model_code"),
            row.get("marketing_name"),
            row.get("source_type"),
            row.get("screen_shape"),
            row.get("market_region"),
            row.get("popularity_source"),
            " ".join(row.get("abis") or []),
            " ".join(row.get("sdk_versions") or []),
            " ".join(row.get("market_tags") or []),
            " ".join((row.get("raw") or {}).get("tags") or []),
        )
    ).lower()
    return raw in haystack


async def _device_profile_stats(session: AsyncSession) -> Dict[str, Any]:
    verification_rows = await session.execute(
        select(AndroidDeviceProfile.verification_status, func.count(AndroidDeviceProfile.id))
        .group_by(AndroidDeviceProfile.verification_status)
    )
    verification_counts = {
        str(status or "verified"): int(count or 0)
        for status, count in verification_rows.all()
    }
    source_rows = await session.execute(
        select(AndroidDeviceProfile.source_type, func.count(AndroidDeviceProfile.id))
        .group_by(AndroidDeviceProfile.source_type)
    )
    source_counts = {
        str(source_type or ""): int(count or 0)
        for source_type, count in source_rows.all()
    }
    total = sum(verification_counts.values())
    verified = verification_counts.get("verified", 0)
    # 可创建总数 = verified 且含 ≥API21 版本（与列表口径一致，避免“总数虚高”）
    buildable = int(
        (await session.execute(
            select(func.count()).select_from(
                select(AndroidDeviceProfile.id).where(
                    AndroidDeviceProfile.verification_status == "verified",
                    _buildable_clause(),
                ).subquery()
            )
        )).scalar_one()
        or 0
    )
    return {
        "total": total,
        "db_verified_total": buildable,
        "verified_total": verified,
        "buildable_total": buildable,
        "candidate_pending_total": verification_counts.get("candidate_pending", 0),
        "rejected_total": verification_counts.get("rejected", 0),
        "verification_counts": verification_counts,
        "source_counts": source_counts,
    }


async def _find_existing_profile(
    session: AsyncSession, data: Dict[str, Any]
) -> AndroidDeviceProfile | None:
    res = await session.execute(
        select(AndroidDeviceProfile).where(
            AndroidDeviceProfile.source_type == data.get("source_type", ""),
            AndroidDeviceProfile.brand == data.get("brand", ""),
            AndroidDeviceProfile.device == data.get("device", ""),
            AndroidDeviceProfile.model_code == data.get("model_code", ""),
            AndroidDeviceProfile.variant_key == data.get("variant_key", ""),
        )
    )
    return res.scalar_one_or_none()


async def _ensure_alias_available(
    session: AsyncSession,
    *,
    alias: str,
    vm_id: str = "",
    current_serial: str = "",
) -> None:
    if not alias:
        return
    res = await session.execute(
        select(AndroidVmInstance).where(AndroidVmInstance.alias == alias)
    )
    for row in res.scalars().all():
        if row.id != vm_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason": "vm_alias_conflict",
                    "alias": alias,
                    "conflictVmId": row.id,
                },
            )
    res_alias = await session.execute(select(DeviceAlias).where(DeviceAlias.alias == alias))
    existing = res_alias.scalar_one_or_none()
    if existing is not None and existing.serial != current_serial:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "alias_conflict",
                "alias": alias,
                "conflictSerial": existing.serial,
            },
        )


def _request_id() -> str:
    import uuid

    return uuid.uuid4().hex[:16]
