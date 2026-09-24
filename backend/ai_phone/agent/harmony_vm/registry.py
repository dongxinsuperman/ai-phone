"""Process-local registry shared by Harmony VM lifecycle and mirror code."""
from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple


_LOCK = threading.RLock()
_MANAGED_FPORTS: Dict[str, Optional[int]] = {}
_VM_CONNECTIONS: Dict[str, Tuple[str, str]] = {}
_HDC_OWNERS: Dict[str, str] = {}
_FOLDED_SCREEN_WIDTHS: Dict[str, Tuple[str, int]] = {}


def vm_device_identity(vm_id: str) -> str:
    """Stable public device key for one Server-managed Harmony VM."""
    return f"harmony-vm:{vm_id}"


def register_managed_vm(
    vm_id: str,
    hdc_serial: str,
    lease_token: str,
    *,
    folded_screen_width: int = 0,
) -> None:
    """Bind one active VM lease to its current HDC address.

    HDC ports can be reused. Rebinding a port invalidates the previous VM's
    logical address before any new command can be resolved through it.
    """
    if not vm_id or not hdc_serial or not lease_token:
        raise ValueError("managed Harmony VM requires vm_id, HDC and lease")
    identity = vm_device_identity(vm_id)
    with _LOCK:
        previous = _VM_CONNECTIONS.pop(identity, None)
        _FOLDED_SCREEN_WIDTHS.pop(identity, None)
        if previous and _HDC_OWNERS.get(previous[0]) == identity:
            _HDC_OWNERS.pop(previous[0], None)
        old_owner = _HDC_OWNERS.get(hdc_serial)
        if old_owner and old_owner != identity:
            _VM_CONNECTIONS.pop(old_owner, None)
            _FOLDED_SCREEN_WIDTHS.pop(old_owner, None)
        _VM_CONNECTIONS[identity] = (hdc_serial, lease_token)
        _HDC_OWNERS[hdc_serial] = identity
        if folded_screen_width > 0:
            _FOLDED_SCREEN_WIDTHS[identity] = (
                lease_token,
                int(folded_screen_width),
            )


def unregister_managed_vm(vm_id: str, lease_token: str) -> None:
    """Forget only this VM's matching lease; never remove a newer port owner."""
    if not lease_token:
        return
    identity = vm_device_identity(vm_id)
    with _LOCK:
        connection = _VM_CONNECTIONS.get(identity)
        if connection is None or connection[1] != lease_token:
            return
        _VM_CONNECTIONS.pop(identity, None)
        _FOLDED_SCREEN_WIDTHS.pop(identity, None)
        if _HDC_OWNERS.get(connection[0]) == identity:
            _HDC_OWNERS.pop(connection[0], None)


def managed_vm_owner(hdc_serial: str) -> Optional[Tuple[str, str]]:
    """Return (vm_id, lease_token) for a currently bound HDC address."""
    with _LOCK:
        identity = _HDC_OWNERS.get(hdc_serial)
        connection = _VM_CONNECTIONS.get(identity or "")
        if identity and connection and connection[0] == hdc_serial:
            return identity.removeprefix("harmony-vm:"), connection[1]
    return None


def is_managed_vm_identity_active(identity: str) -> bool:
    with _LOCK:
        connection = _VM_CONNECTIONS.get(identity)
        return bool(connection and _HDC_OWNERS.get(connection[0]) == identity)


def resolve_harmony_serial(serial: str) -> str:
    """Resolve a public VM key to its active HDC address, failing closed."""
    if not serial.startswith("harmony-vm:"):
        return serial
    with _LOCK:
        connection = _VM_CONNECTIONS.get(serial)
        if connection and _HDC_OWNERS.get(connection[0]) == serial:
            return connection[0]
    raise RuntimeError(f"managed_harmony_vm_not_current:{serial}")


def managed_folded_screen_width(identity: str, hdc_serial: str) -> Optional[int]:
    """Return this active managed Foldable VM lease's desired outer width.

    An HDC address alone is never enough to identify a VM.  A stale driver or
    a port rebound to another VM must not carry the previous fold setting.
    """
    if not identity.startswith("harmony-vm:") or not hdc_serial:
        return None
    with _LOCK:
        connection = _VM_CONNECTIONS.get(identity)
        target = _FOLDED_SCREEN_WIDTHS.get(identity)
        if (
            connection is not None
            and connection[0] == hdc_serial
            and _HDC_OWNERS.get(hdc_serial) == identity
            and target is not None
            and target[0] == connection[1]
        ):
            return target[1]
    return None


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
    "is_managed_vm_identity_active",
    "managed_folded_screen_width",
    "managed_fport",
    "managed_vm_owner",
    "register_managed_serial",
    "register_managed_vm",
    "resolve_harmony_serial",
    "set_managed_fport",
    "unregister_managed_serial",
    "unregister_managed_vm",
    "vm_device_identity",
]
