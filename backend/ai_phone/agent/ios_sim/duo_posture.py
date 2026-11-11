"""Native controls for an iPhone Duo simulator."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ai_phone.agent.drivers.simctl import simctl_run

_SOURCE = Path(__file__).parent
# Use the same counterclockwise body indexes as the other VM workbenches.
DUO_ORIENTATIONS = ("portrait", "landscape_left", "portrait_upside_down", "landscape_right")
_BODY_READBACK = ("portrait", "landscapeLeft", "portraitUpsideDown", "landscapeRight")
_ORIENTATION_EVENT = ("portrait", "landscape-left", "pud", "landscape-right")
_FOLD_ANGLES = {"folded": "0", "unfolded": "180"}


def _remaining(deadline: float, limit: float) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError("等待 iPhone Duo 确认原生姿态超时")
    return min(budget, limit)


@dataclass(frozen=True)
class DuoDisplay:
    id: int
    width: int
    height: int
    uuid: str
    rotation: str
    primary: bool

    @property
    def pixel_size(self) -> tuple[int, int]:
        if self.rotation in ("rot90", "rot270"):
            return self.height, self.width
        return self.width, self.height

    def native_point(self, x: int, y: int) -> tuple[float, float]:
        width, height = self.pixel_size
        u, v = min(1.0, max(0.0, x / width)), min(1.0, max(0.0, y / height))
        return {
            "rot0": (u, v), "rot90": (v, 1 - u),
            "rot180": (1 - u, 1 - v), "rot270": (1 - v, u),
        }[self.rotation]


class DuoControls:
    def __init__(self, udid: str, xcrun: str = "/usr/bin/xcrun") -> None:
        self.udid = udid
        self.xcrun = xcrun
        self.capture_id: int | None = None
        self._build_dir: tempfile.TemporaryDirectory | None = None
        self._build_lock = threading.Lock()

    def _helper(self, deadline: float) -> str:
        with self._build_lock:
            if self._build_dir is None:
                directory = tempfile.TemporaryDirectory(prefix="aiphone-duo-")
                binary = str(Path(directory.name) / "duo_native")
                try:
                    subprocess.run(
                        [self.xcrun, "--sdk", "iphonesimulator", "clang", "-arch", "arm64",
                         "-mios-simulator-version-min=27.1", "-O2", "-fobjc-arc",
                         "-Wl,-adhoc_codesign", "-o", binary, str(_SOURCE / "duo_hinge.m"),
                         str(_SOURCE / "duo_touch.m"), "-framework", "IOKit",
                         "-framework", "Foundation", "-framework", "CoreMotion"],
                        capture_output=True, text=True, check=True, timeout=_remaining(deadline, 10.0),
                    )
                except subprocess.CalledProcessError as exc:
                    directory.cleanup()
                    raise RuntimeError(f"无法构建 iPhone Duo 原生控制程序：{exc.stderr.strip()}") from exc
                self._build_dir = directory
            return str(Path(self._build_dir.name) / "duo_native")

    def _send(self, deadline: float, *args: str) -> str:
        helper = self._helper(deadline)
        return simctl_run("spawn", self.udid, helper, *args, timeout=_remaining(deadline, 60.0))

    def _read(self, deadline: float, *command: str) -> dict:
        try:
            result = subprocess.run(
                [self.xcrun, "devicectl", "device", *command, "--device", self.udid,
                 "--timeout", "5", "--json-output", "-"],
                capture_output=True, text=True, check=True, timeout=_remaining(deadline, 6.0),
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"无法读取 iPhone Duo 原生状态：{exc.stderr.strip()}") from exc
        return json.loads(result.stdout)["result"]

    def display(self, deadline: float, *, cover: bool | None = None) -> DuoDisplay:
        while True:
            active = [
                panel for panel in self._read(deadline, "info", "displays")["displays"]
                if "integrated" in panel.get("type", {}) and panel.get("active") is True
            ]
            # A single active panel can still report an old backlight value after a fold.
            lit = [panel for panel in active if panel.get("backlightState") in ("activeOn", "inactiveOn")]
            candidates = lit or active
            if len(candidates) == 1:
                panel = candidates[0]
                if cover is None or panel.get("primary") is cover:
                    width, height = panel["nativeSize"]
                    return DuoDisplay(int(panel["displayId"]), int(width), int(height),
                                      panel["uniqueId"], panel["currentOrientation"], bool(panel["primary"]))
            time.sleep(_remaining(deadline, 0.2))

    def angle(self, deadline: float) -> float:
        return float(self._send(deadline, "angle"))

    def set_fold(self, state: str, deadline: float) -> DuoDisplay:
        target = _FOLD_ANGLES[state]
        cover = state == "folded"
        self._send(deadline, target)
        try:
            return self.display(min(deadline, time.monotonic() + 2.0), cover=cover)
        except (TimeoutError, subprocess.TimeoutExpired):
            # The hinge sensor can settle before iOS starts switching panels.
            self._send(deadline, "replay", target)
            return self.display(deadline, cover=cover)

    def body_rotation(self, deadline: float) -> int:
        actual = self._read(deadline, "orientation", "get").get("deviceOrientationNonFlat")
        if actual not in _BODY_READBACK:
            raise RuntimeError(f"无法确认 iPhone Duo 机身方向：{actual}")
        return _BODY_READBACK.index(actual)

    def set_orientation(self, orientation: str, deadline: float) -> int:
        target = DUO_ORIENTATIONS.index(orientation)
        if self.body_rotation(deadline) == target:
            return target
        self._send(deadline, "orientation", _ORIENTATION_EVENT[target])
        while self.body_rotation(deadline) != target:
            time.sleep(_remaining(deadline, 0.2))
        return target

    def touch(self, start: tuple[int, int], end: tuple[int, int], duration_ms: int,
              *, count: int = 1, interval_ms: int = 100) -> None:
        deadline = time.monotonic() + max(8.0, (duration_ms * count + interval_ms) / 1000 + 6.0)
        panel = self.display(deadline)
        if panel.id != self.capture_id:
            raise RuntimeError("活动屏已切换，请等镜像切到当前屏幕再点击")
        coords = (*panel.native_point(*start), *panel.native_point(*end))
        self._send(deadline, "touch", panel.uuid, *(str(v) for v in coords),
                   str(duration_ms), str(count), str(interval_ms))
