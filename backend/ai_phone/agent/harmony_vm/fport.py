"""Deterministic Harmony hmdriver2 FPort allocation.

DevEco Emulator reserves TCP 10000..16555 for HDC.  hmdriver2 1.4.4 normally
asks the OS for any free local port, which can land inside that range and race
with a later VM allocation.  The managed Agent installs one process-wide patch
that allocates only from 16556..20000 and holds a lock through the real HDC
``fport`` command.

There is intentionally no compatibility fallback: an unknown hmdriver2 version
is reported as unavailable instead of silently returning to random ports.
"""
from __future__ import annotations

import socket
import threading
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Iterable, Optional, Set, Tuple


FPORT_MIN = 16556
FPORT_MAX = 20000
SUPPORTED_HMDRIVER2_VERSION = "1.4.4"

_ALLOC_LOCK = threading.RLock()
_PATCH_STATE: Dict[str, Any] = {
    "installed": False,
    "reason": "not_installed",
    "version": "",
}


def _list_forwarded_ports() -> Set[int]:
    from ai_phone.agent.drivers.hdc import hdc_list_targets, hdc_run

    ports: Set[int] = set()
    for target in hdc_list_targets():
        try:
            raw = hdc_run(
                "fport", "ls", serial=target.serial, timeout=3.0, check=False
            )
        except Exception:  # noqa: BLE001
            continue
        for chunk in (raw or "").replace("\r", " ").replace("\n", " ").split():
            if not chunk.startswith("tcp:"):
                continue
            try:
                value = int(chunk[4:])
            except ValueError:
                continue
            if FPORT_MIN <= value <= FPORT_MAX:
                ports.add(value)
    return ports


def _listener_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.03)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def _candidate_ports(excluded: Iterable[int]) -> Iterable[int]:
    blocked = {int(p) for p in excluded}
    for port in range(FPORT_MIN, FPORT_MAX + 1):
        if port not in blocked and not _listener_in_use(port):
            yield port


def install_harmony_fport_patch() -> Tuple[bool, str]:
    """Install the exact-version hmdriver2 patch once."""
    if bool(_PATCH_STATE["installed"]):
        return True, str(_PATCH_STATE["reason"])
    try:
        installed_version = version("hmdriver2")
    except PackageNotFoundError:
        _PATCH_STATE.update(reason="hmdriver2_not_installed", version="")
        return False, "hmdriver2_not_installed"
    _PATCH_STATE["version"] = installed_version
    if installed_version != SUPPORTED_HMDRIVER2_VERSION:
        reason = (
            "unsupported_hmdriver2_version:"
            f"{installed_version};required:{SUPPORTED_HMDRIVER2_VERSION}"
        )
        _PATCH_STATE["reason"] = reason
        return False, reason

    try:
        import hmdriver2.hdc as hm_hdc
        from ai_phone.agent.drivers.hdc import hdc_run
    except Exception as exc:  # noqa: BLE001
        reason = f"hmdriver2_import_failed:{type(exc).__name__}:{exc}"
        _PATCH_STATE["reason"] = reason
        return False, reason

    current = hm_hdc.HdcWrapper.forward_port
    if getattr(current, "__aiphone_harmony_fport_patch__", False):
        _PATCH_STATE.update(installed=True, reason="installed")
        return True, "installed"

    def _forward_port(self: Any, rport: int) -> int:
        with _ALLOC_LOCK:
            occupied = _list_forwarded_ports()
            last_error: Optional[Exception] = None
            for local_port in _candidate_ports(occupied):
                try:
                    hdc_run(
                        "fport",
                        f"tcp:{local_port}",
                        f"tcp:{int(rport)}",
                        serial=str(self.serial),
                        timeout=5.0,
                    )
                    return local_port
                except Exception as exc:  # noqa: BLE001
                    # Another process may have won between the socket probe and
                    # HDC registration.  Record the port and continue inside the
                    # same process-wide critical section.
                    last_error = exc
                    occupied.add(local_port)
            detail = f":{last_error}" if last_error is not None else ""
            raise RuntimeError(f"harmony_fport_pool_exhausted{detail}")

    _forward_port.__aiphone_harmony_fport_patch__ = True  # type: ignore[attr-defined]
    _forward_port.__aiphone_original__ = current  # type: ignore[attr-defined]
    hm_hdc.HdcWrapper.forward_port = _forward_port
    _PATCH_STATE.update(installed=True, reason="installed")
    return True, "installed"


def patch_state() -> Dict[str, Any]:
    return dict(_PATCH_STATE)


__all__ = [
    "FPORT_MAX",
    "FPORT_MIN",
    "SUPPORTED_HMDRIVER2_VERSION",
    "install_harmony_fport_patch",
    "patch_state",
]
