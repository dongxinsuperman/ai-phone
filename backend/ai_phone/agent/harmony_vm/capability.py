"""Capability probing for DevEco HarmonyOS Emulator hosts."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Set, Tuple

from ai_phone.agent.android_vm.capability import available_memory_mb, host_abi
from ai_phone.agent.drivers.hdc import hdc_available, hdc_list_targets, hdc_run
from ai_phone.agent.vm_ports import loopback_port_available
from ai_phone.config import get_settings

from .fport import (
    FPORT_MAX,
    FPORT_MIN,
    install_harmony_fport_patch,
    patch_state,
)


HDC_PORT_MIN = 10000
HDC_PORT_MAX = 16555

# 下发前 dry run 用的实例名。固定名字便于识别残留，且它建在临时目录里，
# 不会与受管实例重名。
_DRY_RUN_NAME = "aiphone_capability_dryrun"

# Cache only installed-image/create compatibility, never port occupancy,
# current instances or memory. A host/tool/image change invalidates it early.
_STATIC_PROBE_TTL_SEC = 60.0
_STATIC_PROBE_CACHE_SIZE = 64
_STATIC_PROBE_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}
_STATIC_PROBE_LOCK = threading.Lock()


def startup_timeout(deadline: Optional[float], maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Harmony VM startup deadline exceeded")
    return min(maximum, remaining)


@dataclass(frozen=True)
class HarmonyVmTools:
    emulator: str


DEVICE_TYPES = (
    "Phone",
    "Foldable",
    "WideFold",
    "TripleFold",
    "Tablet",
    "2in1",
    "2in1 Foldable",
    "Wearable",
    "TV",
)

_CLI_ERROR_MARKERS = (
    "can not get image data",
    "cannot get image data",
    "can not open file",
    "download failed",
    "jsondataopt failed",
    "update cloud screen profiles failed",
    "network host not found",
)


def normalize_abi(value: str) -> str:
    raw = (value or "auto").strip().lower()
    if raw in {"", "auto"}:
        return host_abi()
    if raw in {"arm", "arm64", "arm64-v8a", "aarch64"}:
        return "arm64"
    if raw in {"x86", "x64", "amd64"}:
        return "x86_64"
    return raw


def _emulator_candidates() -> Iterator[str]:
    """Yield standard DevEco locations before PATH."""
    system = platform.system()
    if system == "Darwin":
        yield "/Applications/DevEco-Studio.app/Contents/tools/emulator/Emulator"
        yield os.path.expanduser(
            "~/Applications/DevEco-Studio.app/Contents/tools/emulator/Emulator"
        )
        for root in (Path("/Applications"), Path.home() / "Applications"):
            if root.is_dir():
                for path in root.glob(
                    "DevEco*.app/Contents/tools/emulator/Emulator"
                ):
                    yield str(path)
    elif system == "Windows":
        for root_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(root_name, "")
            if root:
                yield os.path.join(
                    root,
                    "Huawei",
                    "DevEco Studio",
                    "tools",
                    "emulator",
                    "Emulator.exe",
                )
                for path in Path(root).glob(
                    "Huawei/DevEco Studio*/tools/emulator/Emulator.exe"
                ):
                    yield str(path)
    else:
        yield os.path.expanduser("~/DevEco-Studio/tools/emulator/Emulator")
        yield "/opt/DevEco-Studio/tools/emulator/Emulator"
    path_candidate = shutil.which("Emulator") or shutil.which("Emulator.exe") or ""
    if path_candidate:
        yield path_candidate


def _is_deveco_emulator(candidate: str, *, deadline: Optional[float] = None) -> bool:
    startup_timeout(deadline, 1.0)
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        return False
    try:
        stat = Path(candidate).stat()
    except OSError:
        return False
    return _emulator_help_supported(
        candidate, stat.st_mtime_ns, stat.st_size, int(time.monotonic() // 60),
        startup_timeout(deadline, 3.0),
    )


@lru_cache(maxsize=32)
def _emulator_help_supported(
    candidate: str, modified_ns: int, size: int, ttl_bucket: int, timeout: float
) -> bool:
    del modified_ns, size, ttl_bucket
    rc, raw = _run([candidate, "-help"], timeout=timeout)
    if rc != 0:
        return False
    lower = raw.lower()
    return (
        "harmonyos" in lower
        and "-imagelist" in lower
        and "-screenprofilelist" in lower
        and "-hdcport" in lower
    )


def supports_no_window(emulator: str, *, deadline: Optional[float] = None) -> bool:
    """Valid old help means GUI; failed or malformed help is an error."""
    rc, raw = _run([emulator, "-help"], timeout=startup_timeout(deadline, 5.0))
    if rc != 0 or not re.search(r"(?i)-(?:start|hvd)\b", raw):
        raise RuntimeError(f"harmony_vm_headless_probe_failed: Emulator -help 探查失败 (rc={rc}): {raw[-500:]}")
    return bool(re.search(r"(?i)(?<![\w-])-noWindow\b", raw))


def find_harmony_tools(*, deadline: Optional[float] = None) -> Tuple[Optional[HarmonyVmTools], list[str]]:
    # Android SDK 也可能在 PATH 暴露同名 Emulator，因此必须先找 DevEco
    # 官方安装位置，并对命令面做校验，不能只看文件名。
    seen: set[str] = set()
    for candidate in _emulator_candidates():
        startup_timeout(deadline, 1.0)
        resolved = str(Path(candidate).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        supported = (
            _is_deveco_emulator(resolved) if deadline is None
            else _is_deveco_emulator(resolved, deadline=deadline)
        )
        startup_timeout(deadline, 1.0)
        if supported:
            return HarmonyVmTools(emulator=resolved), []
    missing = ["DevEco Emulator"]
    if not hdc_available():
        missing.append("hdc")
    return None, missing


def format_os_version(os_version: str, api_version: str) -> str:
    os_value = re.sub(r"^HarmonyOS\s*", "", (os_version or "").strip(), flags=re.IGNORECASE)
    api_value = (api_version or "").strip()
    match = re.fullmatch(
        r"(\d+(?:\.\d+)*)(?:\s*\(\s*(\d+)\s*\))?(?:\s*Beta\s*(\d+))?",
        os_value,
        re.IGNORECASE,
    )
    if not match:
        return ""
    embedded_api = match.group(2) or ""
    if embedded_api and api_value and embedded_api != api_value:
        return ""
    api = embedded_api or api_value
    if not api.isdigit():
        return ""
    beta = f" Beta{match.group(3)}" if match.group(3) else ""
    return f"HarmonyOS {match.group(1)}({api}){beta}"


def _run(
    args: list[str],
    *,
    timeout: float = 15.0,
    input_text: Optional[str] = None,
) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return -1, f"timeout after {timeout}s: {exc}"
    except OSError as exc:
        return -2, f"{type(exc).__name__}: {exc}"
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    return int(proc.returncode), output


def emulator_args_with_roots(
    tools: HarmonyVmTools,
    command: Iterable[str],
    requirement: Dict[str, Any],
) -> list[str]:
    del requirement
    # DevEco 自己维护本机 imageRoot。这里不接受 Server/用户提供绝对路径，
    # 也不把一个 Agent 的路径跨 Agent 携带。
    return [tools.emulator, *command]


def _emulator_config_files() -> Iterator[Path]:
    """Yield DevEco Emulator's own local configuration files.

    DevEco persists ``Emulator -config -imageRoot ...`` as ``imagePath`` in
    ``.emu_config``.  The directory contains the Emulator version, so discover
    it with a glob instead of pinning one DevEco release.
    """
    home = Path.home()
    patterns = [
        home / "Library" / "Caches" / "Huawei" / "Emulator*" / ".emu_config",
        home / ".cache" / "Huawei" / "Emulator*" / ".emu_config",
        home / ".Huawei" / "Emulator*" / ".emu_config",
    ]
    for pattern in patterns:
        yield from pattern.parent.parent.glob(f"{pattern.parent.name}/{pattern.name}")
    for env_name in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env_name)
        if base:
            yield from (Path(base) / "Huawei").glob("Emulator*/.emu_config")


def _roots_from_emulator_config() -> Iterator[str]:
    for config_path in _emulator_config_files():
        try:
            lines = config_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, separator, value = line.partition(":")
            if (
                separator
                and key.strip().lower() in {"imagepath", "imageroot"}
                and value.strip()
            ):
                yield value.strip()


def discover_harmony_instance_root() -> str:
    """Return DevEco's configured instance root or its official default.

    Unlike Android, DevEco 6.1 crashes in native cache-image creation when an
    instance path contains non-ASCII characters. The Agent must therefore not
    inherit the repository/storage path. `Emulator -config -instancePath`
    remains authoritative; when it is absent, DevEco itself uses
    `~/.Huawei/Emulator/deployed`.
    """
    for config_path in _emulator_config_files():
        try:
            lines = config_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, separator, value = line.partition(":")
            if (
                separator
                and key.strip().lower() in {"instancepath", "instance_root"}
                and value.strip()
            ):
                return str(Path(value.strip()).expanduser().resolve())
    return str(
        (Path.home() / ".Huawei" / "Emulator" / "deployed").resolve()
    )


def _roots_from_running_emulators() -> Iterator[str]:
    """Read explicit ``-imageRoot`` values from live DevEco Emulator processes."""
    try:
        import psutil  # noqa: PLC0415

        processes = list(psutil.process_iter(["name", "cmdline"]))
    except Exception:  # noqa: BLE001
        return
    for process in processes:
        try:
            command = [str(part) for part in (process.info.get("cmdline") or [])]
        except Exception:  # noqa: BLE001
            continue
        if not command:
            continue
        executable = Path(command[0]).name.lower()
        if executable not in {"emulator", "emulator.exe"}:
            continue
        for index, part in enumerate(command[:-1]):
            if part.lower() == "-imageroot" and command[index + 1].strip():
                yield command[index + 1].strip()


def discover_harmony_image_roots(
    extra_roots: Iterable[Path | str] = (),
) -> list[str]:
    """Discover the host's real DevEco image roots, following Android's model.

    Sources are local and read-only:

    1. roots already recorded by locally-created managed instances;
    2. DevEco's official ``.emu_config``;
    3. explicit ``-imageRoot`` arguments of running DevEco Emulator processes;
    4. official user-relative default locations.

    Only directories that actually contain ``system-image`` are returned.
    No ai-phone-owned image directory is created or silently substituted.
    """
    candidates: list[str] = [str(root) for root in extra_roots if str(root).strip()]
    candidates.extend(_roots_from_emulator_config())
    candidates.extend(_roots_from_running_emulators())

    harmony_hvd_home = os.environ.get("HarmonyOS_HVD_HOME", "").strip()
    if harmony_hvd_home:
        candidates.append(harmony_hvd_home)

    home = Path.home()
    candidates.extend(
        [
            str(home / ".Huawei" / "Emulator" / "deployed"),
            str(home / "Library" / "Huawei" / "Sdk"),
        ]
    )

    found: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            root = Path(candidate).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        value = str(root)
        if value in seen or not (root / "system-image").is_dir():
            continue
        seen.add(value)
        found.append(value)
    return found


def _license_status(tools: HarmonyVmTools) -> Tuple[bool, str]:
    rc, raw = _run([tools.emulator, "-license"], timeout=10.0, input_text="n\n")
    lower = raw.lower()
    if "need to be reviewed" in lower or "license check aborted" in lower:
        return False, raw
    if rc == 0:
        return True, raw
    return False, raw or f"license command exit {rc}"


def list_downloaded_images(
    tools: HarmonyVmTools,
    requirement: Dict[str, Any],
    *,
    image_roots: Iterable[Path | str] = (),
) -> Tuple[bool, str]:
    """Read installed candidates locally, then ask DevEco for unknown layouts.

    ``phone_all`` covers the phone/folding families on both architectures.
    Metadata establishes the candidate, not create compatibility: the actual
    CLI create probe remains mandatory before a candidate is accepted.
    """
    # The local metadata is enough to identify an installed candidate; the
    # bounded create probe below remains the compatibility authority. Avoid a
    # network-backed catalog request on every dispatch when it is available.
    deadline = requirement.get("_operation_deadline")
    startup_timeout(deadline, 1.0)
    local_images = scan_downloaded_images(image_roots)
    startup_timeout(deadline, 1.0)
    version_spec = format_os_version(
        str(requirement.get("os_version") or ""), str(requirement.get("api_version") or "")
    )
    device_type = str(requirement.get("device_type") or "Phone").strip()
    requested_abi = normalize_abi(str(requirement.get("abi") or "auto"))
    if any(
        row["device_type"] == device_type and row["os_version"] == version_spec
        and row["abi"] == requested_abi for row in local_images
    ):
        return True, json.dumps(local_images, ensure_ascii=False)
    rc, raw = _run(
        [tools.emulator, "-imageList", "-downloaded", "true"],
        timeout=startup_timeout(deadline, 8.0),
    )
    startup_timeout(deadline, 1.0)
    lowered = (raw or "").lower()
    cli_failed = rc != 0 or any(marker in lowered for marker in _CLI_ERROR_MARKERS)
    if not cli_failed:
        images = _normalize_images(raw, downloaded=True)
        if images:
            return True, json.dumps(images, ensure_ascii=False)
    return bool(local_images), json.dumps(local_images, ensure_ascii=False)


def dry_run_create(
    tools: HarmonyVmTools,
    requirement: Dict[str, Any],
    *,
    image_roots: Iterable[Path | str] = (),
) -> tuple[bool, str]:
    """真跑一次 ``-create`` 再删掉，确认这台机器真能建出这个组合。

    只看 ``-imageList -downloaded true`` 是不够的：它按目录存在与否判断，会把
    旧目录布局的镜像报成已下载。本机的 ``HarmonyOS-5.0.1/foldable_arm`` 就是这种
    情况——磁盘上有 6 GB 内容，``-downloaded`` 说有，但当前 CLI 统一去找
    ``phone_all_arm``，真创建时报 ``Cannot find image``。半截下载的镜像同理。

    结果只在工具、镜像和配置未变化时短暂复用；命令有明确上限，不把正常
    环境下的一次耗时当作故障时延保证。
    """
    deadline = requirement.get("_operation_deadline")
    startup_timeout(deadline, 1.0)
    version_spec = format_os_version(
        str(requirement.get("os_version") or ""),
        str(requirement.get("api_version") or ""),
    )
    device_type = str(requirement.get("device_type") or "Phone").strip()
    screen_profile = str(requirement.get("screen_profile") or "").strip()
    display = requirement.get("display")
    if not isinstance(display, dict):
        config = requirement.get("config_json")
        display = config.get("display") if isinstance(config, dict) else None
    # 机型不可选的组合走官方默认机型，此时绝不能传 -screenProfile，否则必然被拒。
    if isinstance(display, dict) and str(display.get("mode") or "") == "official_default":
        screen_profile = ""
    del image_roots  # DevEco 自己维护本机 imageRoot，与真实创建保持同一套解析。
    with tempfile.TemporaryDirectory(prefix="aiphone_harmony_dryrun_") as tmp:
        args = [
            tools.emulator,
            "-create",
            _DRY_RUN_NAME,
            "-deviceType",
            device_type,
            "-osVersion",
            version_spec,
            "-instancePath",
            tmp,
            "-memory",
            "2",
        ]
        if screen_profile:
            args.extend(["-screenProfile", screen_profile])
        rc, raw = _run(args, timeout=startup_timeout(deadline, 15.0))
        if rc != 0 or "create success" not in (raw or "").lower():
            return False, _first_cli_message(raw) or f"exit {rc}"
        # 删不掉残留实例会让下一次同名 dry run 被误判成不可创建。
        delete_rc, delete_raw = _run(
            [
                tools.emulator,
                "-delete",
                _DRY_RUN_NAME,
                "-instancePath",
                tmp,
            ],
            timeout=5.0,
            input_text="y\n",
        )
        if delete_rc != 0 or (Path(tmp) / _DRY_RUN_NAME).exists():
            return False, "临时实例清理未确认：" + (_first_cli_message(delete_raw) or str(delete_rc))
        startup_timeout(deadline, 1.0)
    return True, ""


def _create_failure_reason(
    cli_message: str, device_type: str, version_spec: str
) -> str:
    """把 CLI 原文翻译成可操作的提示。

    ``Cannot find image`` 最常见的成因不是“没下载”，而是磁盘上那份镜像当前 CLI
    用不了——旧目录布局或半截下载都会让 ``-imageList -downloaded true`` 报成已装。
    直接把这句英文抛给用户，看到的人只会以为 SDK 装坏了。
    """
    lowered = (cli_message or "").lower()
    if "find image" in lowered:
        return (
            f"该 Agent 没有可用的 {device_type} / {version_spec} 镜像"
            f"（可能未安装，或已下载的那份目录结构过旧、内容不完整）。"
            f"请先在该 Agent 上安装该镜像再重新探查。"
        )
    return f"该镜像无法创建实例：{cli_message}"


def _first_cli_message(raw: str) -> str:
    for line in (raw or "").splitlines():
        text = line.strip()
        if text and "create fail" not in text.lower():
            return text[:300]
    return (raw or "").strip()[:300]


def scan_downloaded_images(
    image_roots: Iterable[Path | str],
) -> list[Dict[str, Any]]:
    """Directly inspect discovered DevEco image roots without network access.

    ``Emulator -imageList -downloaded true`` still contacts Huawei's catalog
    service.  That makes a local capability probe fail offline and can block
    behind an ongoing download.  Android already solves the equivalent problem
    by scanning ``<sdk>/system-images``; Harmony must follow the same rule.
    """
    found: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for raw_root in image_roots:
        root = Path(raw_root).expanduser()
        system_images = root / "system-image"
        if not system_images.is_dir():
            continue
        try:
            version_dirs = list(system_images.iterdir())
        except OSError:
            continue
        for version_dir in version_dirs:
            if not version_dir.is_dir() or not version_dir.name.startswith("HarmonyOS-"):
                continue
            version_name = version_dir.name.removeprefix("HarmonyOS-")
            try:
                image_dirs = list(version_dir.iterdir())
            except OSError:
                continue
            for image_dir in image_dirs:
                if not image_dir.is_dir():
                    continue
                info_path = image_dir / "info.json"
                # A partially downloaded archive must never count as installed.
                if not info_path.is_file() or not (image_dir / "system.img").is_file():
                    continue
                try:
                    info = json.loads(info_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                api_version = str(info.get("apiVersion") or "").strip()
                device_types = _device_types_from_image_dir(image_dir.name)
                if not api_version or not device_types:
                    continue
                os_version = format_os_version(version_name, api_version)
                raw_abi = str(_pick(info, "abi", "cpuArch", "architecture") or "")
                if not raw_abi:
                    match = re.search(r"_(arm64|arm|x86_64|x86|x64)$", image_dir.name, re.IGNORECASE)
                    raw_abi = match.group(1) if match else ""
                if not os_version or not raw_abi:
                    continue
                abi = normalize_abi(raw_abi)
                for device_type in device_types:
                    key = (device_type, os_version, abi)
                    # Discovery order is authoritative. phone_all is one
                    # package shared by the phone/folding display families;
                    # actual create compatibility is still checked below.
                    found.setdefault(key, {
                        "id": f"{device_type}|{os_version}|{abi}",
                        "device_type": device_type,
                        "os_version": os_version,
                        "api_version": api_version,
                        "software_version": str(info.get("version") or ""),
                        "abi": abi,
                        "downloaded": True,
                        "image_root": str(root.resolve()),
                        "image_path": str(image_dir.resolve()),
                    })
    return sorted(
        found.values(),
        key=lambda row: (
            str(row["device_type"]),
            str(row["os_version"]),
            str(row["abi"]),
        ),
    )


def _device_type_from_image_dir(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    prefixes = (
        ("2in1foldable", "2in1 Foldable"),
        ("triplefold", "TripleFold"),
        ("widefold", "WideFold"),
        ("wearablekid", "WearableKid"),
        ("foldable", "Foldable"),
        ("wearable", "Wearable"),
        ("tablet", "Tablet"),
        ("phone", "Phone"),
        ("2in1", "2in1"),
        ("tv", "TV"),
    )
    for prefix, device_type in prefixes:
        if normalized.startswith(prefix):
            return device_type
    return ""


def _device_types_from_image_dir(name: str) -> tuple[str, ...]:
    normalized = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if normalized.startswith("phoneall"):
        return ("Phone", "Foldable", "WideFold", "TripleFold")
    single = _device_type_from_image_dir(name)
    return (single,) if single else ()


def _cli_output_failed(raw: str) -> bool:
    lower = (raw or "").lower()
    return any(marker in lower for marker in _CLI_ERROR_MARKERS)


def _json_value(raw: str) -> Any:
    text = (raw or "").strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    return None


def _objects(
    value: Any, inherited: Optional[Dict[str, Any]] = None
) -> Iterator[Dict[str, Any]]:
    context = dict(inherited or {})
    if isinstance(value, dict):
        context.update(
            {
                key: child
                for key, child in value.items()
                if not isinstance(child, (dict, list))
            }
        )
        yield {**context, **value}
        for child in value.values():
            yield from _objects(child, context)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child, context)


def _pick(row: Dict[str, Any], *names: str) -> Any:
    lowered = {
        str(key).lower().replace("_", ""): value for key, value in row.items()
    }
    for name in names:
        value = lowered.get(name.lower().replace("_", ""))
        if value not in (None, ""):
            return value
    return ""


def _version_spec_from_row(row: Dict[str, Any]) -> str:
    direct = str(
        _pick(
            row,
            "osVersion",
            "os_version",
            "versionName",
            "softwareVersion",
            "version",
        )
        or ""
    ).strip()
    api = str(_pick(row, "apiVersion", "api", "apiLevel") or "").strip()
    return format_os_version(direct, api)


def _device_type_from_row(row: Dict[str, Any], raw: str = "") -> str:
    value = str(
        _pick(row, "deviceType", "device_type", "deviceCategory", "type") or ""
    ).strip()
    for device_type in DEVICE_TYPES:
        if value.lower() == device_type.lower():
            return device_type
    for device_type in sorted(DEVICE_TYPES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(device_type)}\b", raw, re.IGNORECASE):
            return device_type
    return ""


def _normalize_images(raw: str, *, downloaded: bool) -> list[Dict[str, Any]]:
    found: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    parsed = _json_value(raw)
    if parsed is not None:
        for row in _objects(parsed):
            version_spec = _version_spec_from_row(row)
            if not version_spec:
                continue
            row_text = json.dumps(row, ensure_ascii=False)
            device_type = _device_type_from_row(row, row_text)
            if not device_type:
                continue
            api_match = re.search(r"\((\d+)\)", version_spec)
            abi = str(_pick(row, "abi", "cpuArch", "architecture") or "").strip()
            resolved_abi = normalize_abi(abi) if abi else "auto"
            key = (device_type, version_spec, resolved_abi)
            found[key] = {
                "id": f"{device_type}|{version_spec}",
                "device_type": device_type,
                "os_version": version_spec,
                "api_version": api_match.group(1) if api_match else "",
                "abi": resolved_abi,
                "downloaded": downloaded
                or str(
                    _pick(row, "downloaded", "isDownloaded", "installed") or ""
                ).lower()
                in {"true", "1", "yes"},
            }
    if not found:
        current_type = ""
        for line in raw.splitlines():
            detected = _device_type_from_row({}, line)
            if detected:
                current_type = detected
            for version_spec in re.findall(
                r"HarmonyOS\s+\d+(?:\.\d+)*(?:\(\d+\))(?:\s+Beta\d+)?",
                line,
            ):
                device_type = detected or current_type
                if not device_type:
                    continue
                api_match = re.search(r"\((\d+)\)", version_spec)
                version_spec = format_os_version(version_spec, "")
                key = (device_type, version_spec, "auto")
                found[key] = {
                    "id": f"{device_type}|{version_spec}",
                    "device_type": device_type,
                    "os_version": version_spec,
                    "api_version": api_match.group(1) if api_match else "",
                    "abi": "auto",
                    "downloaded": downloaded,
                }
    return sorted(
        found.values(), key=lambda row: (row["device_type"], row["os_version"])
    )


def _forwarded_ports(
    targets: Optional[Iterable[Any]] = None, *, deadline: Optional[float] = None
) -> Set[int]:
    ports: Set[int] = set()
    local_deadline = time.monotonic() + 5.0
    deadline = min(local_deadline, deadline) if deadline is not None else local_deadline
    for target in targets if targets is not None else hdc_list_targets():
        if str(target.status).strip().lower() != "connected":
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("harmony_fport_probe_timeout")
        try:
            raw = hdc_run(
                "fport", "ls", serial=target.serial, timeout=min(1.0, remaining), check=False
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"harmony_fport_probe_failed:{target.serial}") from exc
        for local in re.findall(r"tcp:(\d+)\s+tcp:\d+", raw or ""):
            try:
                ports.add(int(local))
            except ValueError:
                pass
    return ports


def _listener_ports(*, deadline: Optional[float] = None) -> Set[int]:
    """Return occupied HDC ports using the same bind test as Android.

    ``psutil.net_connections`` can be denied by macOS even for the current
    user.  Returning an empty set in that case caused Server to lease port
    10000 while another desktop process already owned it.  Bind-testing the
    dedicated Harmony HDC pool is local, deterministic, and permission-free.
    """
    startup_timeout(deadline, 1.0)
    result: Set[int] = set()
    try:
        import psutil  # noqa: PLC0415

        for conn in psutil.net_connections(kind="tcp"):
            if str(conn.status).upper() != "LISTEN" or not conn.laddr:
                continue
            port = int(conn.laddr.port)
            if HDC_PORT_MIN <= port <= HDC_PORT_MAX:
                result.add(port)
    except Exception:  # noqa: BLE001
        pass

    # macOS can deny psutil's global socket table while the system lsof command
    # still exposes current-user listeners.  This is evidence collection, not
    # a dependency: failure falls through to per-port bind checks.
    if platform.system() == "Darwin":
        lsof = shutil.which("lsof")
        if lsof:
            rc, raw = _run(
                [lsof, "-nP", "-iTCP", "-sTCP:LISTEN"],
                timeout=startup_timeout(deadline, 2.0),
            )
            if rc == 0:
                for value in re.findall(r":(\d+)\s+\(LISTEN\)", raw):
                    port = int(value)
                    if HDC_PORT_MIN <= port <= HDC_PORT_MAX:
                        result.add(port)

    for port in range(HDC_PORT_MIN, HDC_PORT_MAX + 1):
        startup_timeout(deadline, 1.0)
        if port in result:
            continue
        try:
            available = loopback_port_available(port, deadline=deadline)
        except TimeoutError:
            raise
        except OSError:
            # EPERM means this process is sandboxed and proves nothing about
            # occupancy. Only the helper's confirmed bind conflict is included.
            continue
        if not available:
            result.add(port)
    return result


def _static_probe_key(
    tools: HarmonyVmTools, requirement: Dict[str, Any], roots: list[str]
) -> str:
    paths = {Path(tools.emulator), *_emulator_config_files()}
    for root in roots:
        images = Path(root) / "system-image"
        paths.add(images)
        for info in images.glob("*/*/info.json"):
            paths.update((info.parent, info, info.parent / "system.img"))
    stamps = []
    for path in sorted(paths, key=str):
        try:
            stat = path.stat()
            stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append((str(path), None, None))
    fields = (
        "device_type", "os_version", "api_version", "abi",
        "screen_profile", "display", "config_json",
    )
    return json.dumps(
        {
            "files": stamps, "roots": roots, "host_abi": host_abi(),
            "config": {key: requirement.get(key) for key in fields},
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _probe_image_compatibility(
    tools: HarmonyVmTools,
    requirement: Dict[str, Any],
    roots: list[str],
) -> Dict[str, Any]:
    """Reuse only successful static checks; every caller still probes live ports."""
    deadline = requirement.get("_operation_deadline")
    startup_timeout(deadline, 1.0)
    key = _static_probe_key(tools, requirement, roots)
    # Contending probes must not build an unbounded backlog of create/delete
    # subprocesses. The caller can explicitly probe again after this check.
    if not _STATIC_PROBE_LOCK.acquire(timeout=startup_timeout(deadline, 1.0)):
        return {"ok": False, "reason": "该 Agent 正在检查鸿蒙镜像，请稍后重新探查", "details": {}}
    try:
        startup_timeout(deadline, 1.0)
        now = time.monotonic()
        cached = _STATIC_PROBE_CACHE.get(key)
        if cached is not None and cached[0] > now:
            result = deepcopy(cached[1])
            result["details"]["static_probe_cached"] = True
            return result
        for expired in [entry for entry, (expires, _) in _STATIC_PROBE_CACHE.items() if expires <= now]:
            _STATIC_PROBE_CACHE.pop(expired, None)

        details: Dict[str, Any] = {"image_roots": roots, "static_probe_cached": False}
        images_ok, image_raw = list_downloaded_images(tools, requirement, image_roots=roots)
        details["downloaded_images_raw"] = image_raw[-8000:]
        if not images_ok:
            return {
                "ok": False,
                "reason": "无法确认 DevEco 已下载镜像；不会用联网镜像或未知目录兜底",
                "details": details,
            }
        version_spec = format_os_version(
            str(requirement.get("os_version") or ""), str(requirement.get("api_version") or "")
        )
        device_type = str(requirement.get("device_type") or "Phone").strip()
        requested_abi = normalize_abi(str(requirement.get("abi") or "auto"))
        images = _normalize_images(image_raw, downloaded=True)
        if not any(
            row["device_type"] == device_type and row["os_version"] == version_spec
            and row["abi"] in {"auto", requested_abi}
            for row in images
        ):
            return {
                "ok": False,
                "reason": f"缺少指定的鸿蒙虚拟镜像：{device_type} / {version_spec} / {requested_abi}",
                "details": details,
            }
        create_ok, create_reason = dry_run_create(tools, requirement, image_roots=roots)
        startup_timeout(deadline, 1.0)
        details["dry_run_create"] = create_reason or "ok"
        if not create_ok:
            return {
                "ok": False,
                "reason": _create_failure_reason(create_reason, device_type, version_spec),
                "details": details,
            }
        result = {"ok": True, "reason": "可用", "details": details}
        while len(_STATIC_PROBE_CACHE) >= _STATIC_PROBE_CACHE_SIZE:
            _STATIC_PROBE_CACHE.pop(next(iter(_STATIC_PROBE_CACHE)))
        _STATIC_PROBE_CACHE[key] = (time.monotonic() + _STATIC_PROBE_TTL_SEC, deepcopy(result))
        return result
    finally:
        _STATIC_PROBE_LOCK.release()


def probe_harmony_vm_capability(
    requirement: Dict[str, Any],
    *,
    current_instances: int,
    max_instances: int,
    image_roots: Iterable[Path | str] = (),
) -> Dict[str, Any]:
    deadline = requirement.get("_operation_deadline")
    startup_timeout(deadline, 1.0)
    requested_abi = normalize_abi(str(requirement.get("abi") or "auto"))
    version_spec = format_os_version(
        str(requirement.get("os_version") or ""),
        str(requirement.get("api_version") or ""),
    )
    details: Dict[str, Any] = {
        "host_os": platform.system(),
        "host_machine": platform.machine(),
        "host_abi": host_abi(),
        "requested_abi": requested_abi,
        "os_version_spec": version_spec,
        "current_instances": current_instances,
        "max_instances": max_instances,
        "hdc_port_range": [HDC_PORT_MIN, HDC_PORT_MAX],
        "fport_range": [FPORT_MIN, FPORT_MAX],
        "requested_acceleration": "auto",
        "acceleration_selectable": False,
        "detected_cpu_acceleration": "unknown",
        "detected_gpu_renderer": "unknown",
    }

    tools, missing = (
        find_harmony_tools() if deadline is None else find_harmony_tools(deadline=deadline)
    )
    startup_timeout(deadline, 1.0)
    if tools is None:
        details["missing_tools"] = missing
        return {
            "ok": False,
            "reason": f"缺少鸿蒙虚拟机工具：{', '.join(missing)}",
            "details": details,
        }
    details["tools"] = {"emulator": tools.emulator, "hdc": hdc_available()}
    try:
        details["no_window_supported"] = supports_no_window(tools.emulator, deadline=deadline)
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), "details": details}
    if not hdc_available():
        return {"ok": False, "reason": "缺少 hdc", "details": details}

    patched, patch_reason = install_harmony_fport_patch()
    details["hmdriver2_fport_patch"] = patch_state()
    if not patched:
        return {
            "ok": False,
            "reason": f"hmdriver2 FPort 隔离不可用：{patch_reason}",
            "details": details,
        }
    if requested_abi not in {"arm64", "x86_64"}:
        return {
            "ok": False,
            "reason": f"不支持的目标 ABI：{requested_abi}",
            "details": details,
        }
    if requested_abi != host_abi():
        return {
            "ok": False,
            "reason": f"宿主架构 {host_abi()} 与目标 ABI {requested_abi} 不匹配",
            "details": details,
        }
    if not version_spec:
        return {
            "ok": False,
            "reason": "必须填写 DevEco 镜像的 HarmonyOS 版本和 API 版本",
            "details": details,
        }

    # Do not pre-emptively require every DevEco agreement.  This host has
    # already downloaded, created and booted Emulator instances while
    # ``-license`` still reports unrelated agreements.  Android capability
    # probing likewise treats the installed image as the hard condition.
    # If a concrete install/create command needs another agreement, preserve
    # that CLI failure verbatim; never accept it automatically.
    details["license_policy"] = "deferred_to_install_or_create_cli"

    roots = discover_harmony_image_roots(image_roots)
    startup_timeout(deadline, 1.0)
    compatibility = _probe_image_compatibility(tools, requirement, roots)
    startup_timeout(deadline, 1.0)
    details.update(compatibility["details"])
    if not compatibility["ok"]:
        return {"ok": False, "reason": compatibility["reason"], "details": details}

    try:
        startup_timeout(deadline, 1.0)
        targets = hdc_list_targets()
        startup_timeout(deadline, 1.0)
        listener_ports = _listener_ports() if deadline is None else _listener_ports(deadline=deadline)
        startup_timeout(deadline, 1.0)
        fport_ports = _forwarded_ports(targets) if deadline is None else _forwarded_ports(targets, deadline=deadline)
        startup_timeout(deadline, 1.0)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": f"无法确认鸿蒙端口占用：{exc}",
            "details": details,
        }
    # 只统计 Connected 的 target。hdc 会长期保留已消失实例的 Offline target
    # （官方 tconn -remove 也删不掉），但实测这些端口并没有任何进程监听，
    # 属于 hdc 的陈旧记账而非真实占用。若把它们计入已占端口，可用池会随实例
    # 增删被持续蚕食——本机曾因此白白排除 6 个空闲端口。
    target_ports = {
        int(match.group(1))
        for target in targets
        if target.status.strip().lower() == "connected"
        for match in [re.fullmatch(r"(?:127\.0\.0\.1|localhost):(\d+)", target.serial)]
        if match
    }
    details["hdc_targets"] = [
        {"serial": target.serial, "status": target.status} for target in targets
    ]
    details["hdc_target_ports"] = sorted(
        port for port in target_ports if HDC_PORT_MIN <= port <= HDC_PORT_MAX
    )
    details["fport_ports"] = sorted(
        port for port in fport_ports if FPORT_MIN <= port <= FPORT_MAX
    )
    details["tcp_listener_ports"] = sorted(
        port for port in listener_ports if HDC_PORT_MIN <= port <= FPORT_MAX
    )
    details["local_excluded_ports"] = sorted(
        (target_ports | fport_ports | listener_ports)
    )

    vm_ram_mb = int(requirement.get("memory_gb") or 4) * 1024
    avail_mb = available_memory_mb()
    details["available_memory_mb"] = avail_mb
    warning = ""
    reserve_mb = get_settings().harmony_vm_min_free_mb
    if avail_mb is not None and avail_mb < vm_ram_mb + reserve_mb:
        warning = (
            f"当前 Agent 已运行 {current_instances} 台鸿蒙虚拟机，可用内存约 "
            f"{avail_mb}MB 偏低，继续下发可能不稳定（建议预留约 "
            f"{vm_ram_mb + reserve_mb}MB）。"
        )
    details["warning"] = warning
    startup_timeout(deadline, 1.0)
    return {
        "ok": True,
        "reason": warning or "可用",
        "warning": warning,
        "details": details,
    }


__all__ = [
    "DEVICE_TYPES",
    "HDC_PORT_MAX",
    "HDC_PORT_MIN",
    "HarmonyVmTools",
    "dry_run_create",
    "emulator_args_with_roots",
    "find_harmony_tools",
    "format_os_version",
    "list_downloaded_images",
    "normalize_abi",
    "probe_harmony_vm_capability",
    "scan_downloaded_images",
]
