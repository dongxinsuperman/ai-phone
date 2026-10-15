"""Local exclusion between device use and VM maintenance (all platforms)."""
from __future__ import annotations

import threading
from contextlib import contextmanager


class DeviceActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[str, int] = {}
        self._maintenance: set[str] = set()

    def acquire(self, serial: str, *, maintenance: bool = False) -> bool:
        with self._lock:
            if serial in self._maintenance or (maintenance and self._users.get(serial)):
                return False
            if maintenance:
                self._maintenance.add(serial)
            else:
                self._users[serial] = self._users.get(serial, 0) + 1
            return True

    def release(self, serial: str, *, maintenance: bool = False) -> None:
        with self._lock:
            if maintenance:
                self._maintenance.discard(serial)
            elif self._users.get(serial, 0) > 1:
                self._users[serial] -= 1
            else:
                self._users.pop(serial, None)

    @contextmanager
    def hold(self, serial: str, *, maintenance: bool = False):
        acquired = self.acquire(serial, maintenance=maintenance)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(serial, maintenance=maintenance)


device_activity = DeviceActivity()
