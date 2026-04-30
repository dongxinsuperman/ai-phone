"""/api/runs：运行记录。

.. deprecated:: v1-第4梯队
    ``/api/runs`` **即将被标为 deprecated**。v1 正式对外契约请走 ``/api/submissions``。
    ``/api/runs`` 保留是为了：

    - 调度器内部（``SubmissionScheduler._try_dispatch``）仍复用 Run/RunStep/RunLog
      数据模型与 WS ``start_run`` 协议；
    - 前端"只读 Run 抽屉"（Queue 页日志 + 步骤预览）仍走 ``GET /api/runs/{id}``；
    - 老的手工调试路径（``POST /api/runs``）短期内仍允许操作，但不推荐。

    **新接入方**一律使用 ``/api/submissions``。老接口不再接受新字段、不扩展功能；
    v2 会移除 ``POST /api/runs`` / ``POST /api/runs/{id}/stop``，只保留 GET 只读
    查询用于前端日志展示。

M1.6a 历史说明（保留方便溯源）：
    提供 CRUD 能力，真正的"派发到 Agent"要等 M1.6b WS hub 完成之后才接通；
    ``POST /api/runs`` 只负责落库 + 占自动锁，状态留在 ``pending``，由后续 WS
    流程推进。

接口（全部标记为 deprecated，前端保留使用 GET）：
- GET /api/runs                       列出
- GET /api/runs/{id}                  详情
- GET /api/runs/{id}/steps            步骤列表
- GET /api/runs/{id}/logs             日志列表（?since_id=N 增量）
- POST /api/runs                      创建（deprecated，v2 将移除）
- POST /api/runs/{id}/stop            请求停止（deprecated，v2 将移除）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings
from ai_phone.shared import protocol as P

from ..hub import Hub
from ..lockstore import DeviceLockStore, LockConflict
from ..models import Case, Device, Run, RunLog, RunStep
from ._deps import DBSession, HubDep, LockStoreDep

# 白名单：API 接受的 engine 取值。新增引擎时同步本表 + factory.py。
_KNOWN_ENGINES = ("vlm", "midscene")

router = APIRouter(
    prefix="/api/runs",
    tags=["runs (deprecated, use /api/submissions)"],
    deprecated=True,
)


class RunCreate(BaseModel):
    device_serial: str
    goal: Optional[str] = None
    case_id: Optional[str] = Field(None, max_length=32)
    # 新锁模型：调用方（浏览器 / job / webhook）已持有设备锁 token，Run 沿用这把锁。
    # 不传表示"由调度端代抢一把 job 锁跑"，跑完自动释放。
    lock_token: Optional[str] = None
    # 执行引擎选择：'vlm'（默认 / ai-phone 主链路）或 'midscene'（外接寄居）。
    # 'midscene' 仅在 settings.midscene_enabled=True 时被接受；详见
    # `Midscene执行器接入方案.md`。批次投递（/api/submissions）不接受此字段。
    engine: Optional[str] = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _require_goal_or_case(self) -> "RunCreate":
        if not self.goal and not self.case_id:
            raise ValueError("goal 和 case_id 至少要有一个")
        if self.engine is not None and self.engine not in _KNOWN_ENGINES:
            raise ValueError(
                f"engine 必须是 {_KNOWN_ENGINES} 之一，收到 {self.engine!r}"
            )
        return self


@router.get("")
async def list_runs(
    session: AsyncSession = DBSession,
    device_serial: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if device_serial:
        stmt = stmt.where(Run.device_serial == device_serial)
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    res = await session.execute(stmt)
    return [r.to_dict() for r in res.scalars().all()]


@router.get("/{run_id}")
async def get_run(run_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run.to_dict()


@router.get("/{run_id}/steps")
async def get_run_steps(run_id: str, session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    res = await session.execute(
        select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.step)
    )
    return [s.to_dict() for s in res.scalars().all()]


@router.get("/{run_id}/logs")
async def get_run_logs(
    run_id: str,
    session: AsyncSession = DBSession,
    since_id: int = Query(0, ge=0, description="增量拉取游标；仅返回 id > since_id 的日志"),
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    stmt = (
        select(RunLog)
        .where(RunLog.run_id == run_id, RunLog.id > since_id)
        .order_by(RunLog.id)
        .limit(limit)
    )
    res = await session.execute(stmt)
    logs = res.scalars().all()
    return {
        "run_id": run_id,
        "next_since_id": logs[-1].id if logs else since_id,
        "items": [lg.to_dict() for lg in logs],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(
    body: RunCreate,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    dev = await session.get(Device, body.device_serial)
    if dev is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    if dev.status != "online":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"device status={dev.status}, 必须 online 才能下发任务",
        )

    agent_id = hub.agent_id_for_serial(body.device_serial)
    # agent_id 为 None 表示没有 Agent 在线管辖此设备；允许创建但派发跳过（测试场景）
    # 生产里严格的话可以 503，这里容忍让 REST 单测不依赖 WS 链路
    goal = body.goal or ""
    case_id: Optional[str] = body.case_id
    if case_id:
        case = await session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
        pre_goal = ""
        if case.prerequisite_case_id:
            pre = await session.get(Case, case.prerequisite_case_id)
            if pre is not None:
                pre_goal = pre.goal
        combined = f"{pre_goal}\n\n{case.goal}".strip() if pre_goal else case.goal
        goal = body.goal or combined

    # 锁的来源二选一：
    # (1) 调用方已持锁 → lock_token 校验通过就沿用
    # (2) 没 token → 当场代抢一把 job 锁（自动化场景：定时任务 / webhook）
    existing_lock = store.peek(body.device_serial)
    job_lock_token: Optional[str] = None  # 如果是 (2) 代抢的才有值；Run 结束时释放
    if body.lock_token:
        if existing_lock is None or existing_lock.token != body.lock_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="lock_token 无效或已过期，请刷新后重试",
            )
    else:
        # 代抢：用一个 run-级 holder，失败直接 409
        try:
            info = await store.acquire(
                body.device_serial,
                holder=f"run-{body.device_serial}",
                holder_type="job",
                meta={"auto_acquired": True},
            )
            job_lock_token = info.token
        except LockConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"设备已被占用，无法启动自动任务：{exc}",
            )

    # 引擎选择：缺省走 vlm（与历史行为一致）。midscene 仅在 settings.midscene_enabled
    # 显式启用时被接受，避免未配置 bridge 的环境意外路由到外接通道。
    engine = (body.engine or "vlm").strip().lower()
    if engine == "midscene" and not get_settings().midscene_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="midscene 引擎未启用：请在 .env 中设置 AI_PHONE_MIDSCENE_ENABLED=true",
        )

    run = Run(
        device_serial=body.device_serial,
        agent_id=agent_id,
        case_id=case_id,
        goal=goal,
        status="pending",
        engine=engine,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    dispatched = False
    if agent_id is not None:
        await hub.bind_run(run.id, agent_id)
        dispatched = await hub.send_to_agent(
            agent_id,
            {
                "type": P.MSG_START_RUN,
                "run_id": run.id,
                "device_serial": body.device_serial,
                "goal": goal,
                "engine": engine,
            },
        )
        if not dispatched:
            await hub.unbind_run(run.id)

    payload = run.to_dict()
    payload["dispatched"] = dispatched
    payload["agent_id"] = agent_id
    # 只有 "代抢" 的 job 锁 token 才回吐，让调度端保存用于 Run 结束后释放；
    # 浏览器路径下 token 归 useDeviceLock 管，这里不回吐避免泄露
    if job_lock_token is not None:
        payload["job_lock_token"] = job_lock_token
    return payload


@router.post("/{run_id}/stop")
async def stop_run(
    run_id: str,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if run.status in ("success", "failed", "stopped"):
        return run.to_dict()

    # 先通知 Agent 软停；具体兑现由 Agent 触发 run_done(cancelled) 回来
    await hub.send_to_run(run_id, {"type": P.MSG_STOP_RUN, "run_id": run_id})

    run.status = "stopped"
    run.reason = run.reason or "stopped_by_user"
    await session.commit()
    # 新锁模型：锁不归属 run，由发起方（浏览器 tab / 调度端）自己管释放；这里不碰。
    await hub.unbind_run(run_id)
    await session.refresh(run)
    return run.to_dict()
