from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Set
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from ai_phone.agent.ws_client import AgentWSClient
from ai_phone.shared import protocol as P

from .android import install_apk
from .harmony import install_hap
from .ios import install_ipa

_TASKS: Set[asyncio.Task] = set()


async def handle_app_install_start(client: AgentWSClient, msg: Dict[str, Any]) -> None:
    """收到安装任务后立即后台执行，避免阻塞 Agent WS 接收循环。"""
    task = asyncio.create_task(
        _run_app_install(client, dict(msg)),
        name=f"app-install-{msg.get('item_id') or msg.get('serial') or 'unknown'}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


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
            with tempfile.TemporaryDirectory(prefix="ai-phone-app-install-") as tmp:
                local_path = Path(tmp) / filename
                try:
                    await _download_package(client, str(msg.get("package_url") or ""), local_path)
                except Exception as exc:  # noqa: BLE001
                    reason = "download_failed"
                    message = f"{type(exc).__name__}: {exc}"
                else:
                    success, reason, message = await _install_by_platform(
                        platform,
                        serial,
                        local_path,
                        timeout_sec,
                    )
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
) -> tuple[bool, str, str]:
    installer = {
        "android": install_apk,
        "harmony": install_hap,
        "ios": install_ipa,
    }.get(platform)
    if installer is None:
        return False, "platform_unsupported", f"不支持的平台: {platform}"

    return await asyncio.wait_for(
        asyncio.to_thread(installer, serial, local_path, timeout_sec),
        timeout=max(30, timeout_sec + 30),
    )


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
