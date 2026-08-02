"""iOS 虚拟机的内部 REST 接口。

路由前缀独立（``/api/internal/ios-sim``），与 Android 的 ``/api/internal/vm``、
鸿蒙的 ``/api/internal/harmony-vm`` 并列，互不复用（方案 §0.3 的隔离原则）。

暴露的操作严格对齐三端右侧卡片那六个，不多不少（方案 §6.5.1）：

```text
探查   POST /instances/{id}/dispatch-candidates
下发   POST /instances/{id}/dispatch
启动   POST /instances/{id}/start
停止   POST /instances/{id}/stop
复制   POST /instances/{id}/copy
删除   DELETE /instances/{id}
```

**没有** erase / wipe 端点——三端统一，不为 iOS 搞特殊化。
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.shared import protocol as P

from ..api._deps import DBSession, HubDep
from ..api.submissions import RequireBearer
from ..hub import Hub
from . import catalog as cat
from .models import IosSimCatalogSnapshot, IosSimVmInstance
from .schemas import IosSimVmCreateReq, IosSimVmDispatchReq, IosSimVmPatchReq
from .service import (
    ACTIVE_STATES,
    delete_owned_alias,
    ensure_alias_available,
    get_capability_waiter,
    get_vm_or_404,
    now_utc,
    point_alias_to_runtime,
    reserve_placeholder_alias,
    vm_payload,
)


def _require_ios_sim_db(request: Request) -> None:
    """DDL 失败时明确 503，而不是抛一堆 SQL 错误。

    与鸿蒙同构：这项能力的表是隔离创建的，失败只影响自己。
    """
    if getattr(request.app.state, "ios_sim_db_ready", True) is False:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="iOS 虚拟机数据表初始化失败；现有设备与 Android / 鸿蒙链路不受影响",
        )


router = APIRouter(
    prefix="/api/internal/ios-sim/instances",
    tags=["internal-ios-sim"],
    dependencies=[RequireBearer, Depends(_require_ios_sim_db)],
)
catalog_router = APIRouter(
    prefix="/api/internal/ios-sim",
    tags=["internal-ios-sim"],
    dependencies=[RequireBearer, Depends(_require_ios_sim_db)],
)

REDISPATCH_REQUIRED = "所属 Agent 已离线，请重新探查并下发"


def _request_id() -> str:
    return uuid.uuid4().hex[:16]


async def _resolve_names(
    session: AsyncSession, device_type: str, runtime: str
) -> Dict[str, str]:
    """把 identifier 补成可读名，并做 Server 侧的组合预校验。

    **这只是预校验**，用于在下发前挡掉明显不可能的组合、省一次无效往返。
    最终判据是 Agent 探查——那边用 runtime 自带的 ``supportedDeviceTypes``，
    是苹果直给的权威表（方案 §6.5.4）。两者冲突时以 Agent 为准。
    """
    row = await session.get(IosSimCatalogSnapshot, "official")
    payload = row.payload if row is not None else None
    dt = cat.find_device_type(device_type, payload)
    if dt is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"官方目录里没有该机型：{device_type}",
        )
    # runtime identifier 形如 ...SimRuntime.iOS-26-0 → 版本串 26.0
    tail = runtime.rsplit(".", 1)[-1]
    parts = tail.split("-")
    version = ".".join(parts[1:]) if len(parts) >= 2 else ""
    runtime_name = f"{parts[0]} {version}" if version else tail
    if version and not cat.supports_version(dt, version):
        # 苹果用 65535.255.255 表示「没有上限」，不是空值——直接拼进提示会变成
        # 一串没人看得懂的数字。
        max_str = str(dt.get("max_runtime_version_string") or "")
        max_label = "无上限" if not max_str or max_str.startswith("65535.") else max_str
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                f"机型「{dt.get('name')}」不支持 {runtime_name}"
                f"（官方支持区间 {dt.get('min_runtime_version_string')} ~ {max_label}）"
            ),
        )
    return {
        "device_type_name": str(dt.get("name") or ""),
        "runtime_name": runtime_name,
        "os_version": version,
    }


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
@catalog_router.get("/catalog")
async def get_catalog(session: AsyncSession = DBSession) -> Dict[str, Any]:
    """官方机型目录。前端左侧选择器用。

    只回答「有哪些机型、各自支持哪些系统版本」。**「这台 Agent 装了哪些 runtime」
    不在这里**——那要靠探查（方案 §6.5.4.1），因为每台 Agent 装的 Xcode 与 runtime
    都不同，Server 存快照反而会失真。
    """
    row = await session.get(IosSimCatalogSnapshot, "official")
    if row is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="官方机型目录尚未导入；请重启 Server 或检查 official_catalog.json",
        )
    return row.to_dict()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@catalog_router.get("/health")
async def health() -> Dict[str, Any]:
    summary = cat.catalog_summary()
    return {"ok": True, "catalog": summary}


@router.get("")
async def list_instances(session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    rows = (
        await session.execute(
            select(IosSimVmInstance).order_by(IosSimVmInstance.created_at.desc())
        )
    ).scalars().all()
    return [row.to_dict() for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_instance(
    body: IosSimVmCreateReq, session: AsyncSession = DBSession
) -> Dict[str, Any]:
    alias = body.alias.strip()
    try:
        await ensure_alias_available(session, alias)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    names = await _resolve_names(session, body.device_type, body.runtime)
    vm = IosSimVmInstance(
        name=(body.name or alias).strip(),
        alias=alias,
        device_type=body.device_type,
        runtime=body.runtime,
        config_json=body.config_json or {},
        state="draft",
        **names,
    )
    session.add(vm)
    await session.flush()
    # 草稿态还没有 UDID，先用占位 serial 占住别名，避免被别人抢走
    await reserve_placeholder_alias(session, vm)
    await session.commit()
    await session.refresh(vm)
    logger.info(
        "已创建 iOS 虚拟机配置 vm_id={} alias={} 机型={} 系统={}",
        vm.id, vm.alias, vm.device_type_name, vm.runtime_name,
    )
    return vm.to_dict()


@router.get("/{vm_id}")
async def get_instance(vm_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    return vm.to_dict()


@router.patch("/{vm_id}")
async def patch_instance(
    vm_id: str, body: IosSimVmPatchReq, session: AsyncSession = DBSession
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    if vm.state in ACTIVE_STATES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"实例处于 {vm.state} 状态，请先停止再修改配置",
        )

    # 机型与系统版本由官方目录锁定，不能改。与鸿蒙 CATALOG_LOCKED_FIELDS 一致：
    # 允许在这里改等于绕过整套兼容校验（机型 × 版本区间），能改出一台起不来的配置，
    # 且直到下发才暴露。要换配置就新建一台，不在 PATCH 里放暗道。
    conflicting = [
        key
        for key, incoming in (
            ("device_type", body.device_type),
            ("runtime", body.runtime),
        )
        if incoming is not None and incoming != getattr(vm, key)
    ]
    if conflicting:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason": "catalog_locked_fields_immutable",
                "message": (
                    "机型与系统版本由官方目录锁定，不能修改："
                    + "、".join(conflicting)
                    + "。请新建一台配置。"
                ),
            },
        )

    if body.alias is not None:
        alias = body.alias.strip()
        try:
            await ensure_alias_available(session, alias, exclude_vm_id=vm_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        # 先摘旧别名行再改字段：别名表按 serial 存，旧行认的是旧别名，
        # 顺序反过来会摘不掉，留下一条孤儿映射把新别名卡住。
        await delete_owned_alias(session, vm)
        vm.alias = alias
        vm.name = alias
        try:
            if vm.udid:
                await point_alias_to_runtime(session, vm, vm.udid)
            else:
                await reserve_placeholder_alias(session, vm)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if body.name is not None:
        vm.name = body.name.strip() or vm.alias
    if body.config_json is not None:
        vm.config_json = body.config_json

    await session.commit()
    await session.refresh(vm)
    return vm.to_dict()


@router.delete("/{vm_id}")
async def delete_instance(
    vm_id: str,
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    agent_id = str(vm.assigned_agent_id or "")
    udid = str(vm.udid or "")
    await delete_owned_alias(session, vm)
    await session.delete(vm)
    await session.commit()

    cleanup_sent = False
    if agent_id:
        # 尽力清理远端实例；Agent 离线时该指令丢失，留待重连后的孤儿对账兜底
        cleanup_sent = await hub.send_to_agent(
            agent_id,
            {
                "type": P.MSG_IOS_SIM_VM_DELETE,
                "request_id": _request_id(),
                "vm_id": vm_id,
                "udid": udid,
            },
        )
    logger.info("已删除 iOS 虚拟机配置 vm_id={} 远端清理下发={}", vm_id, cleanup_sent)
    return {"id": vm_id, "deleted": True, "cleanup_sent": cleanup_sent}


@router.post("/{vm_id}/copy", status_code=status.HTTP_201_CREATED)
async def copy_instance(
    vm_id: str, body: IosSimVmCreateReq, session: AsyncSession = DBSession
) -> Dict[str, Any]:
    """按现有实例的配置新建一台（对齐三端卡片的「复制配置」）。

    只复制配置，**不复制数据**——新实例是一台全新的虚拟机，UDID 与数据目录都是新的。
    """
    try:
        source = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="源实例不存在") from exc
    alias = body.alias.strip()
    try:
        await ensure_alias_available(session, alias)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    vm = IosSimVmInstance(
        name=(body.name or alias).strip(),
        alias=alias,
        device_type=source.device_type,
        device_type_name=source.device_type_name,
        runtime=source.runtime,
        runtime_name=source.runtime_name,
        os_version=source.os_version,
        config_json=dict(source.config_json or {}),
        state="draft",
    )
    session.add(vm)
    await session.flush()
    await reserve_placeholder_alias(session, vm)
    await session.commit()
    await session.refresh(vm)
    logger.info("已复制 iOS 虚拟机配置 {} → {}", vm_id, vm.id)
    return vm.to_dict()


# ---------------------------------------------------------------------------
# 探查 / 下发 / 启停
# ---------------------------------------------------------------------------
@router.post("/{vm_id}/dispatch-candidates")
async def dispatch_candidates(
    vm_id: str,
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    """向所有在线 Agent 广播探查，返回各自能否承接。

    与 Android / 鸿蒙一致：**Server 不自动选 Agent**，把候选列表给用户选。
    """
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    return await get_capability_waiter().probe(hub=hub, vm=vm)


@router.post("/{vm_id}/dispatch")
async def dispatch(
    vm_id: str,
    body: IosSimVmDispatchReq,
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    """把实例下发给指定 Agent 并启动。"""
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    agent_id = body.agent_id.strip()
    if vm.state in ACTIVE_STATES:
        # 与 Android 一致：在跑的实例不能直接改派，必须先停。否则同一份配置会在
        # 两台机器上各跑一台。
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"实例处于 {vm.state} 状态，请先停止再下发到其他 Agent",
        )
    if not hub.has_agent(agent_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Agent 不在线：{agent_id}")

    # 下发前最后确认一次：探查结果可能已经过期（runtime 被卸载、内存被吃满等）
    fresh = await get_capability_waiter().probe_agent(hub=hub, vm=vm, agent_id=agent_id)
    if not fresh.get("ok"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"该 Agent 当前无法承接：{fresh.get('reason') or '不可用'}",
        )

    old_agent = (vm.assigned_agent_id or "").strip()
    if old_agent and old_agent != agent_id:
        return await _switch_agent(
            vm=vm, new_agent_id=agent_id, session=session, hub=hub
        )

    vm.assigned_agent_id = agent_id
    vm.state = "starting"
    vm.error_code = ""
    vm.error_message = ""
    await session.commit()

    sent = await hub.send_to_agent(
        agent_id,
        {"type": P.MSG_IOS_SIM_VM_START, **vm_payload(vm, request_id=_request_id())},
    )
    if not sent:
        vm.state = "unavailable"
        vm.error_code = "dispatch_send_failed"
        vm.error_message = "下发失败：Agent 连接不可用"
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="下发失败")
    await session.refresh(vm)
    return {"sent": True, "instance": vm.to_dict()}


async def _switch_agent(
    *,
    vm: IosSimVmInstance,
    new_agent_id: str,
    session: AsyncSession,
    hub: Hub,
) -> Dict[str, Any]:
    """换 Agent：删旧 vm_id + 新建 vm_id（继承别名与配置）绑到新 Agent。

    **为什么要换 vm_id，而不是改一下归属字段就完事**：对账的规则是「谁报谁绑」，
    旧 Agent 若当时离线、没收到删除指令，回来后照样会报这个 vm_id，把实例抢回去。
    换掉 id 之后，新旧是两条记录，旧的那条已经删了，旧 Agent 再报就必然被判成
    孤儿清掉——没有抢回的可能。这套做法直接沿用 Android ``_switch_agent``
    （见 android-vm-plan §21.3）。

    顺序也照抄 Android：**先把新记录落库、发出启动，成功之后才通知旧 Agent 删**。
    反过来的话，中间任何一步回滚都会留下「旧实例已被删、库里还是旧记录」的空洞。
    旧 Agent 离线收不到删除指令也没关系，它回来对账时会因为「库里没有这个 id」
    被回发删除。
    """
    old_agent = (vm.assigned_agent_id or "").strip()
    old_vm_id = vm.id
    old_udid = (vm.udid or "").strip()

    inherited = dict(
        name=vm.name,
        alias=vm.alias,
        device_type=vm.device_type,
        device_type_name=vm.device_type_name,
        runtime=vm.runtime,
        runtime_name=vm.runtime_name,
        os_version=vm.os_version,
        config_json=dict(vm.config_json or {}),
    )

    # 先释放旧别名，新记录才能复用同一个别名（别名全局唯一）
    await delete_owned_alias(session, vm)
    await session.delete(vm)
    await session.flush()

    new_vm = IosSimVmInstance(state="draft", **inherited)
    session.add(new_vm)
    await session.flush()
    await reserve_placeholder_alias(session, new_vm)

    # 必须先提交 starting，再下发。反过来（先下发、后提交）有丢状态的窗口：
    # Agent 可能在几十毫秒内就回报 starting / error，而此时新 vm_id 还没落库，
    # handle_vm_status 用的是另一个会话、查不到这条记录会把消息直接丢弃，
    # 界面就永远停在「启动中」。时序照搬鸿蒙 _switch_agent。
    new_vm.assigned_agent_id = new_agent_id
    new_vm.state = "starting"
    new_vm.error_code = ""
    new_vm.error_message = ""
    new_vm.stopped_at = None
    await session.commit()
    await session.refresh(new_vm)

    sent = await hub.send_to_agent(
        new_agent_id,
        {
            "type": P.MSG_IOS_SIM_VM_START,
            **vm_payload(new_vm, request_id=_request_id()),
        },
    )
    if not sent:
        # 已提交，不能再 rollback：显式回退到可重新下发的状态。
        new_vm.state = "stopped"
        new_vm.error_code = "dispatch_send_failed"
        new_vm.error_message = "下发失败：Agent 连接不可用"
        await session.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="下发失败：请重新探查并下发"
        )

    # 落库成功后才通知旧 Agent 清理。
    #
    # 这一步是**尽力而为**，与 Android / 鸿蒙同口径：兜底是旧 Agent 下次重连时的
    # 孤儿对账（它报的旧 vm_id 在库里已不存在 → 回发删除）。已知局限：旧 Agent
    # 一直在线、但 simctl delete 失败时，要等到它下次重连才会重试，期间旧实例会
    # 留在那台机器上。把发送结果原样返回，调用方至少能知道「清理还没确认」。
    cleanup_sent = False
    if old_agent:
        try:
            cleanup_sent = await hub.send_to_agent(
                old_agent,
                {
                    "type": P.MSG_IOS_SIM_VM_DELETE,
                    "request_id": _request_id(),
                    "vm_id": old_vm_id,
                    "udid": old_udid,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "通知旧 Agent {} 清理 iOS 虚拟机 {} 抛错：{}", old_agent, old_vm_id, exc
            )
        if not cleanup_sent:
            logger.warning(
                "旧 Agent {} 未收到清理指令，iOS 虚拟机 {}（udid={}）仍留在那台机器上，"
                "等它重连时由孤儿对账清除",
                old_agent, old_vm_id, old_udid or "(未知)",
            )

    logger.info(
        "iOS 虚拟机换 Agent：{} → {}，vm_id {} → {}（别名 {} 已继承）",
        old_agent or "(无)", new_agent_id, old_vm_id, new_vm.id, new_vm.alias,
    )
    return {
        "sent": True,
        "switched": True,
        "old_vm_id": old_vm_id,
        "old_cleanup_sent": cleanup_sent,
        "instance": new_vm.to_dict(),
    }


@router.post("/{vm_id}/start")
async def start(
    vm_id: str,
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    agent_id = str(vm.assigned_agent_id or "").strip()
    if not agent_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="实例尚未下发给任何 Agent，请先探查并下发"
        )
    if not hub.has_agent(agent_id):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=REDISPATCH_REQUIRED)

    vm.state = "starting"
    vm.error_code = ""
    vm.error_message = ""
    await session.commit()
    sent = await hub.send_to_agent(
        agent_id,
        {"type": P.MSG_IOS_SIM_VM_START, **vm_payload(vm, request_id=_request_id())},
    )
    if not sent:
        vm.state = "unavailable"
        vm.error_code = "start_send_failed"
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="下发失败")
    await session.refresh(vm)
    return {"sent": True, "instance": vm.to_dict()}


@router.post("/{vm_id}/stop")
async def stop(
    vm_id: str,
    session: AsyncSession = DBSession,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    try:
        vm = await get_vm_or_404(session, vm_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="实例不存在") from exc
    agent_id = str(vm.assigned_agent_id or "").strip()
    if not agent_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="实例尚未下发给任何 Agent")
    if not hub.has_agent(agent_id):
        # Agent 已离线，实例事实上已经不在跑；直接置终态，不必等 ack
        vm.state = "agent_offline"
        vm.error_code = "agent_offline"
        await session.commit()
        await session.refresh(vm)
        return {"sent": False, "instance": vm.to_dict()}

    vm.state = "stopping"
    await session.commit()
    sent = await hub.send_to_agent(
        agent_id,
        {
            "type": P.MSG_IOS_SIM_VM_STOP,
            "request_id": _request_id(),
            "vm_id": vm.id,
            "udid": str(vm.udid or ""),
        },
    )
    if not sent:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="下发失败")
    await session.refresh(vm)
    return {"sent": True, "instance": vm.to_dict()}
