"""Android Emulator capability probing for Agent-side VM hosting."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ai_phone.config import get_settings


@dataclass
class AndroidVmTools:
    adb: str
    emulator: str
    avdmanager: str
    sdkmanager: str = ""
    sdk_root: str = ""


_IMAGE_CACHE: dict[str, tuple[float, list[str]]] = {}


def available_memory_mb() -> Optional[int]:
    """宿主当前可用物理内存（MB）。探测不到返回 None（不阻断，退化为仅看硬上限）。"""
    try:
        import psutil  # noqa: PLC0415
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        return None


def host_abi() -> str:
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return "arm64"
    return "x86_64"


def normalize_abi(value: str) -> str:
    raw = (value or "auto").strip().lower()
    if raw in {"", "auto"}:
        return host_abi()
    if raw == "arm64-v8a":
        return "arm64"
    if raw not in {"arm64", "x86_64"}:
        return host_abi()
    return raw


def android_image_abi(abi: str) -> str:
    return "arm64-v8a" if normalize_abi(abi) == "arm64" else "x86_64"


def default_system_image(api_level: int, abi: str, system_type: str = "google_apis") -> str:
    image_type = system_type if system_type in {"google_apis", "default"} else "google_apis"
    return f"system-images;android-{int(api_level)};{image_type};{android_image_abi(abi)}"


def find_android_tools() -> tuple[Optional[AndroidVmTools], list[str]]:
    sdk_roots = _sdk_roots()
    adb = _find_tool("adb", sdk_roots, ["platform-tools/adb"])
    emulator = _find_tool("emulator", sdk_roots, ["emulator/emulator"])
    avdmanager = _find_tool(
        "avdmanager",
        sdk_roots,
        ["cmdline-tools/latest/bin/avdmanager", "tools/bin/avdmanager"],
    )
    sdkmanager = _find_tool(
        "sdkmanager",
        sdk_roots,
        ["cmdline-tools/latest/bin/sdkmanager", "tools/bin/sdkmanager"],
    )
    missing = []
    if not adb:
        missing.append("adb")
    if not emulator:
        missing.append("emulator")
    if not avdmanager:
        missing.append("avdmanager")
    if missing:
        return None, missing
    return AndroidVmTools(
        adb=adb or "",
        emulator=emulator or "",
        avdmanager=avdmanager or "",
        sdkmanager=sdkmanager or "",
        sdk_root=sdk_roots[0] if sdk_roots else "",
    ), []


def probe_android_vm_capability(
    requirement: Dict[str, Any],
    *,
    current_instances: int,
    max_instances: int,
) -> Dict[str, Any]:
    tools, missing = find_android_tools()
    requested_abi = normalize_abi(str(requirement.get("abi") or "auto"))
    api_level = int(requirement.get("api_level") or 35)
    system_type = str(requirement.get("system_type") or "google_apis").strip() or "google_apis"
    system_image = str(requirement.get("system_image") or "").strip()
    if not system_image:
        system_image = default_system_image(api_level, requested_abi, system_type)

    details: Dict[str, Any] = {
        "host_os": platform.system(),
        "host_machine": platform.machine(),
        "host_abi": host_abi(),
        "requested_abi": requested_abi,
        "api_level": api_level,
        "system_type": system_type,
        "system_image": system_image,
        "current_instances": current_instances,
        "max_instances": max_instances,
    }
    if missing:
        details["missing_tools"] = missing
        return {"ok": False, "reason": f"缺少 Android SDK 工具：{', '.join(missing)}", "details": details}
    assert tools is not None
    details["tools"] = {
        "adb": tools.adb,
        "emulator": tools.emulator,
        "avdmanager": tools.avdmanager,
        "sdkmanager": tools.sdkmanager,
        "sdk_root": tools.sdk_root,
    }
    if requested_abi != host_abi():
        return {
            "ok": False,
            "reason": f"宿主架构 {host_abi()} 与目标 ABI {requested_abi} 不匹配",
            "details": details,
        }
    # 镜像是硬条件（真起不来才拦）：缺指定镜像 / 无法确认任何镜像 → 不可用。
    installed = list_installed_system_images(tools)
    details["installed_system_images"] = installed
    if installed and system_image not in installed:
        return {
            "ok": False,
            "reason": f"缺少 system image：{system_image}",
            "details": details,
        }
    if not installed:
        reason = (
            "未发现已安装 Android system image"
            if tools.sdkmanager
            else "缺少 sdkmanager，无法确认 system image 是否就绪"
        )
        return {"ok": False, "reason": reason, "details": details}
    # 数量 / 内存都「不拦截」——只做软提示。原因：macOS 的 available 偏保守
    # （compressed / cached 内存可回收却不计入），硬挡会误杀其实能起的机器。
    # 内存偏低时仅在 reason/warning 里提醒，是否下发由用户决定（不弹二级确认）。
    vm_ram_mb = int(requirement.get("ram_mb") or 4096)
    min_free_mb = get_settings().android_vm_min_free_mb
    avail_mb = available_memory_mb()
    details["available_memory_mb"] = avail_mb
    warning = ""
    if avail_mb is not None and avail_mb < vm_ram_mb + min_free_mb:
        warning = (
            f"当前 Agent 已运行 {current_instances} 台虚拟机，可用内存约 {avail_mb}MB 偏低，"
            f"继续下发可能不稳定（建议预留约 {vm_ram_mb + min_free_mb}MB）。"
        )
    details["warning"] = warning
    return {"ok": True, "reason": warning or "可用", "warning": warning, "details": details}


def _scan_system_images(sdk_root: str) -> list[str]:
    """直接扫描 ``<sdk>/system-images/android-*/<type>/<abi>/`` 目录列出真实已装镜像。

    这是"看这台 Agent 真有没有"的最可靠方式——不依赖任何 sdkmanager（避免它指向另一个
    空 SDK 时误报无镜像）。以目录里有 ``system.img`` 视为有效镜像。
    """
    if not sdk_root:
        return []
    base = Path(sdk_root) / "system-images"
    if not base.is_dir():
        return []
    images: list[str] = []
    try:
        for api_dir in base.iterdir():
            if not api_dir.is_dir() or not api_dir.name.startswith("android-"):
                continue
            for type_dir in api_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for abi_dir in type_dir.iterdir():
                    if not abi_dir.is_dir():
                        continue
                    if (abi_dir / "system.img").exists() or (abi_dir / "build.prop").exists():
                        images.append(f"system-images;{api_dir.name};{type_dir.name};{abi_dir.name}")
    except Exception:  # noqa: BLE001
        return []
    return sorted(set(images))


def list_installed_system_images(tools: AndroidVmTools) -> list[str]:
    # 首选：直接扫所有候选 SDK 的 system-images 目录（实地、与 sdkmanager 无关）。
    scanned: set[str] = set()
    for root in _sdk_roots():
        scanned.update(_scan_system_images(root))
    if tools.sdk_root:
        scanned.update(_scan_system_images(tools.sdk_root))
    if scanned:
        return sorted(scanned)
    # 兜底：扫不到才退回 sdkmanager --list_installed（带缓存）。
    if not tools.sdkmanager:
        return []
    cache_sec = get_settings().android_vm_image_cache_sec
    cache_key = tools.sdkmanager
    cached = _IMAGE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] <= cache_sec:
        return list(cached[1])
    try:
        proc = subprocess.run(
            [tools.sdkmanager, "--list_installed"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return []
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    images: list[str] = []
    for line in out.splitlines():
        value = line.strip().split("|", 1)[0].strip()
        if value.startswith("system-images;"):
            images.append(value)
    result = sorted(set(images))
    _IMAGE_CACHE[cache_key] = (now, result)
    return result


def _sdk_root_from_tool(tool_path: str, tool: str) -> Optional[str]:
    """从工具的真实路径反推它所属的 Android SDK 根目录。

    只认标准 SDK 结构，否则返回 None——这样 PATH 里的 Homebrew sdkmanager
    （``/opt/homebrew/bin/sdkmanager``，不在 SDK 结构内）会被自动排除。
    """
    # 不做 resolve()：按 which 给出的路径结构判断即可。resolve() 会跟随软链，可能把
    # /opt/homebrew/bin/sdkmanager 链到 caskroom 的标准结构而被误认成 SDK。
    p = Path(tool_path)
    if tool == "emulator" and p.parent.name == "emulator":
        return str(p.parent.parent)               # <sdk>/emulator/emulator → <sdk>
    if tool == "adb" and p.parent.name == "platform-tools":
        return str(p.parent.parent)               # <sdk>/platform-tools/adb → <sdk>
    if p.parent.name == "bin":                    # sdkmanager / avdmanager
        gp = p.parent.parent
        if gp.name == "latest" and gp.parent.name == "cmdline-tools":
            return str(gp.parent.parent)          # <sdk>/cmdline-tools/latest/bin → <sdk>
        if gp.name == "tools":
            return str(gp.parent)                 # <sdk>/tools/bin → <sdk>
    return None


def _sdk_roots() -> list[str]:
    """动态确定本机所有候选 SDK 根目录（不硬编码具体目录）：

    1) Agent 自己声明的 ``ANDROID_SDK_ROOT`` / ``ANDROID_HOME``（最权威）；
    2) 从 PATH 里实际能用的 emulator/avdmanager/sdkmanager/adb **反推**其所属 SDK
       （"你实际在用哪个 SDK 就看哪个"，Homebrew 等非 SDK 结构会被排除）；
    3) Android Studio 默认安装路径兜底。
    """
    roots: list[str] = []
    for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        value = os.environ.get(key)
        if value:
            roots.append(value)
    for tool in ("emulator", "avdmanager", "sdkmanager", "adb"):
        found = shutil.which(tool)
        if found:
            inferred = _sdk_root_from_tool(found, tool)
            if inferred:
                roots.append(inferred)
    # 兜底：相对【当前用户 home】的常见默认安装位置（跨平台，不写死具体盘符/用户名）。
    # Path.home() 跨平台：macOS→/Users/x、Linux→/home/x、Windows→C:\Users\x。
    try:
        home = Path.home()
    except Exception:  # noqa: BLE001
        home = None
    if home is not None:
        roots.extend([
            str(home / "Library" / "Android" / "sdk"),            # macOS 默认
            str(home / "Android" / "Sdk"),                         # Linux / 通用
            str(home / "AppData" / "Local" / "Android" / "Sdk"),  # Windows 默认
        ])
    out: list[str] = []
    seen = set()
    for root in roots:
        if root and root not in seen and Path(root).exists():
            seen.add(root)
            out.append(root)
    return out


def _is_windows() -> bool:
    # 抽成函数做测试接缝：不可 monkeypatch os.name（pathlib 靠它决定 Posix/Windows Path 会崩）。
    return os.name == "nt"


def _exe_candidates(path: Path) -> list[Path]:
    """跨平台可执行名：无后缀优先（macOS/Linux 行为不变）；Windows 再补 .exe/.bat/.cmd。

    adb/emulator 是 .exe，avdmanager/sdkmanager 是 .bat——一律都试，命中即用。
    """
    if _is_windows():
        return [path, path.with_suffix(".exe"), path.with_suffix(".bat"), path.with_suffix(".cmd")]
    return [path]


def _find_tool(name: str, sdk_roots: list[str], rels: list[str]) -> str:
    # 优先从 Android SDK 根目录取（保证 adb/emulator/avdmanager/sdkmanager 同属一个 SDK，
    # 镜像才查得到）；PATH/which 只作兜底——避免误用 Homebrew 等装在 PATH 里、却指向另一个
    # 空 SDK 的 sdkmanager（真机踩过：列不出镜像）。
    for root in sdk_roots:
        for rel in rels:
            for cand in _exe_candidates(Path(root) / rel):
                if cand.exists():
                    return str(cand)
    # which 兜底：跨平台通用（Windows 的 shutil.which 会按 PATHEXT 自动匹配 .exe/.bat）。
    found = shutil.which(name)
    if found:
        return found
    return ""
