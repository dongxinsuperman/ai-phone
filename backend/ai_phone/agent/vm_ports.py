"""Local TCP availability checks for native VM listeners."""
from __future__ import annotations

import errno
import socket
import time
from typing import Optional


_CONNECT_TIMEOUT_SEC = 0.1


def _connect_timeout(deadline: Optional[float]) -> float:
    if deadline is None:
        return _CONNECT_TIMEOUT_SEC
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("VM port probe deadline exceeded")
    return min(_CONNECT_TIMEOUT_SEC, remaining)


def loopback_port_available(port: int, *, deadline: Optional[float] = None) -> bool:
    """Check whether a new native IPv4 loopback listener can use this port.

    POSIX native emulators use SO_REUSEADDR, so TIME_WAIT alone must not block
    reuse. A reuse bind can also coexist with a live wildcard listener on
    macOS: close that probe before connecting, and accept only a refused
    connection. This does not prove that a previous VM process has exited;
    lifecycle callers must check their exact process and device separately.

    Windows keeps its exclusive-address semantics. An initial bind error other
    than EADDRINUSE is raised so capability reports need not mislabel permission
    failures as occupied ports. Lifecycle callers can conservatively reject it.
    """
    _connect_timeout(deadline)
    address = ("127.0.0.1", int(port))
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if exclusive is not None:
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            probe.bind(address)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
    else:
        _connect_timeout(deadline)
        return True
    _connect_timeout(deadline)
    if exclusive is not None:
        return False

    # Only a plain EADDRINUSE takes this slower path; free port-pool scans keep
    # one bind per port. Never leave the reuse probe bound during the connect.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
    except OSError:
        _connect_timeout(deadline)
        return False

    timeout = _connect_timeout(deadline)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            result = connection.connect_ex(address)
    except OSError:
        result = None
    _connect_timeout(deadline)
    return result == errno.ECONNREFUSED
