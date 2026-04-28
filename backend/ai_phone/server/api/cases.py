"""/api/cases：case 轻量 CRUD。

遵循"微分离"约定：case 只是模板，运行时会拷贝 goal 到 Run 表；修改 case 不会
影响已经在跑的 Run。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Case
from ._deps import DBSession

router = APIRouter(prefix="/api/cases", tags=["cases"])


class CaseUpsert(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    goal: str = Field(..., min_length=1)
    prerequisite_case_id: Optional[str] = Field(None, max_length=32)


@router.get("")
async def list_cases(session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    res = await session.execute(select(Case).order_by(Case.updated_at.desc()))
    return [c.to_dict() for c in res.scalars().all()]


@router.get("/{case_id}")
async def get_case(case_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    case = await session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    return case.to_dict()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_case(body: CaseUpsert, session: AsyncSession = DBSession) -> Dict[str, Any]:
    if body.prerequisite_case_id:
        pre = await session.get(Case, body.prerequisite_case_id)
        if pre is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"prerequisite_case_id={body.prerequisite_case_id} 不存在",
            )
    case = Case(
        title=body.title,
        goal=body.goal,
        prerequisite_case_id=body.prerequisite_case_id,
    )
    session.add(case)
    await session.commit()
    await session.refresh(case)
    return case.to_dict()


@router.put("/{case_id}")
async def update_case(
    case_id: str, body: CaseUpsert, session: AsyncSession = DBSession
) -> Dict[str, Any]:
    case = await session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    if body.prerequisite_case_id == case_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="前置 case 不能指向自己",
        )
    if body.prerequisite_case_id:
        pre = await session.get(Case, body.prerequisite_case_id)
        if pre is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"prerequisite_case_id={body.prerequisite_case_id} 不存在",
            )
    case.title = body.title
    case.goal = body.goal
    case.prerequisite_case_id = body.prerequisite_case_id
    await session.commit()
    await session.refresh(case)
    return case.to_dict()


@router.delete("/{case_id}")
async def delete_case(case_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    case = await session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
    await session.delete(case)
    await session.commit()
    return {"deleted": case_id}


@router.get("/{case_id}/effective-goal")
async def effective_goal(case_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    """返回拼好前置的最终 goal，一次给 Runner 消费。

    前置 case 只展开一层（和 Groovy 的"拼接不嵌套"约定一致）。
    """
    case = await session.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")

    pre_goal = ""
    if case.prerequisite_case_id:
        pre = await session.get(Case, case.prerequisite_case_id)
        if pre is not None:
            pre_goal = pre.goal
    combined = f"{pre_goal}\n\n{case.goal}".strip() if pre_goal else case.goal
    return {
        "case_id": case.id,
        "title": case.title,
        "goal": combined,
        "prerequisite_case_id": case.prerequisite_case_id,
    }
