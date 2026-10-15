"""Keep package-operation resources until their synchronous work has exited."""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Generic, Optional, TypeVar

from loguru import logger

from .device_activity import DeviceActivity


T = TypeVar("T")


class NativeOperation(Generic[T]):
    """One native call, with a lease acquired before executor submission.

    Cancelling an asyncio waiter cannot stop a running thread. If work has not
    started, abandon it and release immediately; otherwise the native wrapper
    owns both the device lease and optional temporary-resource cleanup until
    its finally block. No retry or second result is produced here.
    """

    def __init__(
        self, serial: str, activity: DeviceActivity, call: Callable[[], T],
        *, cleanup: Optional[Callable[[], None]] = None,
    ) -> None:
        if not activity.acquire(serial):
            raise RuntimeError("device_under_maintenance")
        self._serial = serial
        self._activity = activity
        self._call = call
        self._cleanup = cleanup
        self._lock = threading.Lock()
        self._started = False
        self._abandoned = False
        self._released = False

    async def run(self) -> T:
        try:
            return await asyncio.to_thread(self._invoke)
        finally:
            self.abandon_if_pending()

    def abandon_if_pending(self) -> None:
        """Also safe if wait_for cancelled run() before its coroutine started."""
        with self._lock:
            if self._started:
                return
            self._abandoned = True
        self._finish()

    def _invoke(self) -> T:
        with self._lock:
            if self._abandoned:
                # A queued worker can be picked up just as its waiter times
                # out. The abandoned marker prevents a late native start.
                return None  # type: ignore[return-value]
            if self._started:
                raise RuntimeError("native operation may only run once")
            self._started = True
        try:
            return self._call()
        finally:
            self._finish()

    def _finish(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            if self._cleanup is not None:
                self._cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.warning("原生包操作临时资源清理失败 serial={}: {}", self._serial, exc)
        finally:
            self._activity.release(self._serial)
