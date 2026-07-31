"""Read-only ABI inspection for uploaded Harmony HAP archives."""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterable

from .models import HarmonyAppPackageMeta


_ABI_ALIASES = {
    "arm64-v8a": "arm64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86_64": "x86_64",
    "x64": "x86_64",
}


def _abis_from_names(names: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for name in names:
        normalized = str(name or "").replace("\\", "/").lower()
        if not normalized.endswith(".so"):
            continue
        parts = [part for part in normalized.split("/") if part]
        for part in parts:
            abi = _ABI_ALIASES.get(part)
            if abi:
                found.add(abi)
    return found


def parse_hap_abi(package_id: str, path: Path) -> HarmonyAppPackageMeta:
    """Inspect archive entry names only; never extract untrusted package data."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            native_entries = [
                name for name in names if str(name).lower().endswith(".so")
            ]
            abis = _abis_from_names(native_entries)
            if native_entries and not abis:
                return HarmonyAppPackageMeta(
                    package_id=package_id,
                    abi_set="unknown",
                    abi_state="unknown_native_layout",
                    last_error=(
                        "HAP contains native .so files but no recognized "
                        "arm64/x86_64 ABI directory"
                    ),
                )
            return HarmonyAppPackageMeta(
                package_id=package_id,
                abi_set=",".join(sorted(abis)) if abis else "none",
                abi_state="resolved",
                last_error="",
            )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return HarmonyAppPackageMeta(
            package_id=package_id,
            abi_set="unknown",
            abi_state="parse_failed",
            last_error=f"{type(exc).__name__}: {exc}"[:4000],
        )


def abi_matches(abi_set: str, target_abi: str) -> bool:
    package_abis = {
        _ABI_ALIASES.get(value.strip().lower(), value.strip().lower())
        for value in re.split(r"[,;]", abi_set or "")
        if value.strip()
    }
    if not package_abis or package_abis == {"none"}:
        return True
    return _ABI_ALIASES.get(target_abi, target_abi) in package_abis


__all__ = ["abi_matches", "parse_hap_abi"]
