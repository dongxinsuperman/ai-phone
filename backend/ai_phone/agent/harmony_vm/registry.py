"""Process-local registry shared by Harmony VM lifecycle and mirror code."""
from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple


_LOCK = threading.RLock()
_MANAGED_FPORTS: Dict[str, Optional[int]] = {}


def register_managed_serial(serial: str, fport_port: Optional[int] = None) -> None:
    with _LOCK:
        _MANAGED_FPORTS[serial] = fport_port


def set_managed_fport(serial: str, fport_port: int) -> None:
    with _LOCK:
        if serial in _MANAGED_FPORTS:
            _MANAGED_FPORTS[serial] = int(fport_port)


def unregister_managed_serial(serial: str) -> None:
    with _LOCK:
        _MANAGED_FPORTS.pop(serial, None)


def managed_fport(serial: str) -> Tuple[bool, Optional[int]]:
    with _LOCK:
        if serial not in _MANAGED_FPORTS:
            return False, None
        return True, _MANAGED_FPORTS[serial]


__all__ = [
    "managed_fport",
    "register_managed_serial",
    "set_managed_fport",
    "unregister_managed_serial",
]
