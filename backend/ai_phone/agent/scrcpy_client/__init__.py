"""Vendored 精简版 scrcpy client。

来源：https://github.com/leng-yue/py-scrcpy-client (v0.5.0, MIT license)

为什么自己 vendor 而不是直接 ``pip install scrcpy-client``：

1. 上游 pin 了 ``adbutils<2.0.0``，但项目里别处已经在用 ``adbutils>=2.6``，强行同
   时安装会冲突。实际 scrcpy client 用到的 adbutils API（``AdbConnection`` /
   ``AdbDevice`` / ``Network`` / ``adb`` + ``device.shell`` / ``device.sync.push``
   / ``device.create_connection``）在 1.x 和 2.x 里都稳定，vendor 后能直接配
   2.x 跑，无需降级。

2. 上游打包了 ``scrcpy-server.jar`` 在 wheel 内部；我们改放 ``backend/assets/``，
   走和 ADBKeyBoard.apk 同样的"asset 兜底"模式，便于版本管理与替换。

3. 上游可选依赖 ``opencv-python`` / ``PySide6``（UI 用），我们只要 H.264 解码（依
   靠 PyAV）+ 控制通道，去掉无用依赖。

只保留实际用到的部分；其余（如 demo UI、剪贴板交互等）按需要再补。
"""

from .const import (  # noqa: F401
    ACTION_DOWN,
    ACTION_MOVE,
    ACTION_UP,
    EVENT_DISCONNECT,
    EVENT_FRAME,
    EVENT_INIT,
    EVENT_RAW_BYTES,
    LOCK_SCREEN_ORIENTATION_UNLOCKED,
    POWER_MODE_NORMAL,
    POWER_MODE_OFF,
)
from .core import Client  # noqa: F401
