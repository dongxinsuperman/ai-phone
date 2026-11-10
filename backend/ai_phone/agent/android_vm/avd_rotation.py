"""Rotation through the Android Emulator controller used by Android Studio."""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import grpc
import psutil


_ANGLE_BY_QUADRANT = (0.0, 90.0, -180.0, -90.0)
_SET_PHYSICAL_MODEL = "/android.emulation.control.EmulatorController/setPhysicalModel"


def _discovery_directories() -> list[Path]:
    # Emulator discovery uses a platform runtime directory, then the Android user directory.
    roots = [Path.home() / ".android"]
    roots.extend(Path(os.environ[key]) for key in (
        "ANDROID_EMULATOR_HOME", "ANDROID_USER_HOME", "ANDROID_AVD_HOME",
    ) if os.environ.get(key))
    if os.environ.get("ANDROID_SDK_HOME"):
        roots.append(Path(os.environ["ANDROID_SDK_HOME"]) / ".android")
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "TemporaryItems")
    elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Temp")
    elif sys.platform == "linux":
        roots.append(Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"))
    return [root / "avd" / "running" for root in dict.fromkeys(roots)]


def rotation_endpoint(avd_name: str, serial: str) -> tuple[int, str] | None:
    for directory in _discovery_directories():
        for registration in directory.glob("pid_*.ini"):
            pid = registration.stem.removeprefix("pid_")
            if not pid.isdecimal() or not psutil.pid_exists(int(pid)):
                continue
            try:
                props = dict(line.split("=", 1) for line in registration.read_text(encoding="utf-8").splitlines()
                             if "=" in line)
                port = int(props.get("grpc.port") or 0)
            except (OSError, ValueError):
                continue
            if (props.get("avd.id") == avd_name and props.get("port.serial") == serial.removeprefix("emulator-")
                    and 0 < port < 65536 and props.get("grpc.token")):
                return port, props["grpc.token"]
    return None


def set_rotation(endpoint: tuple[int, str], quadrant: int, timeout: float) -> None:
    port, token = endpoint
    # SDK emulator_controller.proto: ROTATION is enum 1 in field 1.
    # Field 3 is a ParameterValue with field 1 containing three packed floats.
    angles = struct.pack("<3f", 0.0, 0.0, _ANGLE_BY_QUADRANT[quadrant])
    parameters = b"\x0a" + bytes([len(angles)]) + angles
    request = b"\x08\x01\x1a" + bytes([len(parameters)]) + parameters
    with grpc.secure_channel(f"localhost:{port}", grpc.local_channel_credentials(),
                             options=(("grpc.enable_http_proxy", 0),)) as channel:
        try:
            channel.unary_unary(_SET_PHYSICAL_MODEL)(
                request, timeout=timeout, metadata=(("authorization", f"Bearer {token}"),),
            )
        except grpc.RpcError as exc:
            raise RuntimeError(f"AVD 机身旋转请求失败 ({exc.code().name})") from exc
