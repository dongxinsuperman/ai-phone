from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.agent.device_activity import device_activity
from ai_phone.agent.native_operation import NativeOperation
from ai_phone.shared import protocol as P

from .android import install_apk
from .harmony import install_hap
from .ios import install_ipa
from .ios_sim import install_sim_app

_TASKS: Set[asyncio.Task] = set()


async def handle_app_install_start(client: AgentWSClient, msg: Dict[str, Any]) -> None:
    """收到安装任务后立即后台执行，避免阻塞 Agent WS 接收循环。"""
    serial = str(msg.get("serial") or "")
    if not device_activity.acquire(serial):
        await client.send({"type": P.MSG_APP_INSTALL_RESULT, "serial": serial,
            "task_id": msg.get("task_id", ""), "item_id": msg.get("item_id", ""),
            "request_id": msg.get("request_id", ""), "platform": msg.get("platform", ""),
            "package_name": msg.get("package_name", ""), "success": False,
            "reason": "device_under_maintenance", "message": "虚拟机正在启停或清理，请稍后重试"})
        return
    task = asyncio.create_task(
        _run_app_install(client, dict(msg)),
        name=f"app-install-{msg.get('item_id') or msg.get('serial') or 'unknown'}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(lambda _: device_activity.release(serial))


async def _run_app_install(client: AgentWSClient, msg: Dict[str, Any]) -> None:
    task_id = str(msg.get("task_id") or "")
    item_id = str(msg.get("item_id") or "")
    serial = str(msg.get("serial") or "").strip()
    platform = str(msg.get("platform") or "").strip().lower()
    filename = _safe_filename(str(msg.get("filename") or "app-package"))
    timeout_sec = _safe_timeout(msg.get("timeout_sec"))

    success = False
    reason = "install_failed"
    message = ""
    try:
        if not item_id or not serial or not platform:
            reason = "bad_request"
            message = "安装任务参数缺失"
        else:
            tmp = tempfile.TemporaryDirectory(prefix="ai-phone-app-install-")
            native_owns_tmp = False
            try:
                local_path = Path(tmp.name) / filename
                try:
                    await _download_package(client, str(msg.get("package_url") or ""), local_path)
                except Exception as exc:  # noqa: BLE001
                    reason = "download_failed"
                    message = f"{type(exc).__name__}: {exc}"
                else:
                    # _install_by_platform owns cleanup from entry onwards,
                    # including timeout/cancel before its worker can start.
                    native_owns_tmp = True
                    success, reason, message = await _install_by_platform(
                        platform,
                        serial,
                        local_path,
                        timeout_sec,
                        cleanup=tmp.cleanup,
                    )
            finally:
                if not native_owns_tmp:
                    tmp.cleanup()
    except asyncio.TimeoutError:
        success = False
        reason = "timeout"
        message = f"安装超过 {timeout_sec}s 未完成"
    except Exception as exc:  # noqa: BLE001
        success = False
        reason = "install_failed"
        message = f"{type(exc).__name__}: {exc}"

    payload = {
        "type": P.MSG_APP_INSTALL_RESULT,
        "task_id": task_id,
        "item_id": item_id,
        "serial": serial,
        "success": success,
        "reason": "" if success else reason,
        "message": message or ("安装成功" if success else "安装失败"),
    }
    sent = await client.send(payload)
    logger.info(
        "app_install result sent={} task_id={} item_id={} serial={} success={} reason={}",
        sent,
        task_id,
        item_id,
        serial,
        success,
        payload["reason"],
    )


async def _download_package(client: AgentWSClient, package_url: str, local_path: Path) -> None:
    url = _absolute_url(client.server_http_base, package_url)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        async with http.stream("GET", url) as resp:
            resp.raise_for_status()
            total = 0
            with local_path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    fh.write(chunk)
    if total <= 0:
        raise RuntimeError("Server 返回空包文件")
    logger.info("app_install package downloaded url={} path={} bytes={}", url, local_path, total)


async def _install_by_platform(
    platform: str,
    serial: str,
    local_path: Path,
    timeout_sec: int,
    *, cleanup: Optional[Callable[[], None]] = None,
) -> tuple[bool, str, str]:
    # 按**内部通道**分发，不是对外平台：iOS 真机与虚拟机的安装方式毫无共同点
    # （USB lockdown vs 宿主 simctl，.ipa vs .app），正是要分开的地方。
    installer = {
        "android": install_apk,
        "harmony": install_hap,
        "ios": install_ipa,
        "ios_sim": install_sim_app,
    }.get(platform)
    if installer is None:
        if cleanup is not None:
            cleanup()
        return False, "platform_unsupported", f"不支持的平台: {platform}"

    try:
        operation = NativeOperation(
            serial, device_activity, lambda: installer(serial, local_path, timeout_sec),
            cleanup=cleanup,
        )
    except BaseException:
        if cleanup is not None:
            cleanup()
        raise
    try:
        return await asyncio.wait_for(operation.run(), timeout=max(30, timeout_sec + 30))
    finally:
        operation.abandon_if_pending()


def _absolute_url(server_http_base: str, package_url: str) -> str:
    if not package_url:
        raise RuntimeError("package_url empty")
    parsed = urlparse(package_url)
    if parsed.scheme in {"http", "https"}:
        return package_url
    if not server_http_base:
        raise RuntimeError("server_http_base empty")
    return urljoin(server_http_base.rstrip("/") + "/", package_url.lstrip("/"))


def _safe_filename(filename: str) -> str:
    name = Path(filename or "app-package").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "app-package"


def _safe_timeout(value: Any) -> int:
    try:
        return max(30, int(value or 600))
    except Exception:  # noqa: BLE001
        return 600
