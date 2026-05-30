from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..api._deps import DBSession, HubDep, LockStoreDep
from ..hub import Hub
from ..lockstore import DeviceLockStore
from ..models import AppPackage
from .schemas import CreateTaskRequest
from .service import (
    create_task,
    eligible_devices,
    list_packages,
    package_file_path,
    retry_unsuccessful,
    task_to_dict,
)
from .storage import save_upload

router = APIRouter(prefix="/api/app-install", tags=["app-install"])


@router.post("/packages", status_code=status.HTTP_201_CREATED)
async def upload_package(
    file: UploadFile = File(...),
    session: AsyncSession = DBSession,
) -> Dict[str, Any]:
    filename, platform, storage_path = await save_upload(file)
    pkg = AppPackage(filename=filename, platform=platform, storage_path=storage_path)
    session.add(pkg)
    await session.commit()
    await session.refresh(pkg)
    return pkg.to_dict()


@router.get("/packages")
async def packages(session: AsyncSession = DBSession) -> List[Dict[str, Any]]:
    return await list_packages(session)


@router.get("/packages/{package_id}/download")
async def download_package(package_id: str, session: AsyncSession = DBSession) -> FileResponse:
    pkg = await session.get(AppPackage, package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="package not found")
    path = package_file_path(pkg)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="package file not found")
    return FileResponse(
        path=str(path),
        filename=pkg.filename,
        media_type="application/octet-stream",
    )


@router.get("/packages/{package_id}/eligible-devices")
async def get_eligible_devices(
    package_id: str,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> List[Dict[str, Any]]:
    return await eligible_devices(session, store, hub, package_id)


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_install_task(
    body: CreateTaskRequest,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    return await create_task(session, store, hub, body.package_id, body.serials)


@router.get("/tasks/{task_id}")
async def get_install_task(task_id: str, session: AsyncSession = DBSession) -> Dict[str, Any]:
    return await task_to_dict(session, task_id)


@router.post("/tasks/{task_id}/retry-unsuccessful")
async def retry_install_task(
    task_id: str,
    session: AsyncSession = DBSession,
    store: DeviceLockStore = LockStoreDep,
    hub: Hub = HubDep,
) -> Dict[str, Any]:
    return await retry_unsuccessful(session, store, hub, task_id)
