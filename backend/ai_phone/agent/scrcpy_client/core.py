"""scrcpy client 主体：连 scrcpy-server v2.4，解码 H.264，向上派发 frame / control。

Vendored from leng-yue/py-scrcpy-client（MIT），主要改动：
- adbutils API 适配 2.x（实际只用 ``Network`` / ``adb`` / ``device.shell`` / ``device.create_connection``，1.x 与 2.x 兼容）
- ``scrcpy-server.jar`` 默认从 ``backend/assets/`` 读取，可由 ``server_jar`` 参数覆盖
- 用 loguru 替代裸 print，警告走标准日志
- ``last_frame`` 为 ``av.VideoFrame``（而不是 numpy ndarray）；上层用 PyAV 自带的
  ``to_image()`` / ``reformat()`` 转 PIL Image，避免到处依赖 numpy
"""
from __future__ import annotations

import os
import socket
import struct
import threading
from pathlib import Path
from time import sleep
from typing import Any, Callable, List, Optional, Tuple, Union

from adbutils import AdbConnection, AdbDevice, AdbError, Network, adb
from av.codec import CodecContext
from av.error import InvalidDataError
from loguru import logger

from .const import (
    EVENT_DISCONNECT,
    EVENT_FRAME,
    EVENT_INIT,
    EVENT_RAW_BYTES,
    LOCK_SCREEN_ORIENTATION_UNLOCKED,
)
from .control import ControlSender


# 默认 jar 路径：backend/assets/scrcpy-server.jar（与 ADBKeyBoard.apk 同目录约定）
_DEFAULT_JAR_PATH = (
    Path(__file__).resolve().parents[3] / "assets" / "scrcpy-server.jar"
)

# 必须与 jar 文件版本对齐；scrcpy server 协议每个大版本会变
_SCRCPY_SERVER_VERSION = "2.4"


class Client:
    """单设备 scrcpy 会话：拉视频流 + 控制信道。

    使用方式（threaded 模式，最常见）::

        c = Client(device="R3CR70STPCK")
        c.add_listener(EVENT_FRAME, lambda frame: ...)
        c.start(threaded=True)
        ...
        c.control.touch(100, 200, ACTION_DOWN)
        c.control.touch(100, 200, ACTION_UP)
        ...
        c.stop()
    """

    def __init__(
        self,
        device: Optional[Union[AdbDevice, str]] = None,
        max_width: int = 0,
        bitrate: int = 4_000_000,
        max_fps: int = 0,
        flip: bool = False,
        block_frame: bool = False,
        stay_awake: bool = False,
        lock_screen_orientation: int = LOCK_SCREEN_ORIENTATION_UNLOCKED,
        connection_timeout: int = 3000,
        encoder_name: Optional[str] = None,
        codec_name: Optional[str] = None,
        server_jar: Optional[Union[str, Path]] = None,
    ) -> None:
        assert max_width >= 0
        assert bitrate >= 0
        assert max_fps >= 0
        assert -1 <= lock_screen_orientation <= 3
        assert connection_timeout >= 0
        assert encoder_name in (
            None,
            "OMX.google.h264.encoder",
            "OMX.qcom.video.encoder.avc",
            "c2.qti.avc.encoder",
            "c2.android.avc.encoder",
        )
        assert codec_name in (None, "h264", "h265", "av1")

        self.flip = flip
        self.max_width = max_width
        self.bitrate = bitrate
        self.max_fps = max_fps
        self.block_frame = block_frame
        self.stay_awake = stay_awake
        self.lock_screen_orientation = lock_screen_orientation
        self.connection_timeout = connection_timeout
        self.encoder_name = encoder_name
        self.codec_name = codec_name
        self._server_jar = Path(server_jar) if server_jar else _DEFAULT_JAR_PATH

        # 解析 device
        if device is None:
            devices = adb.device_list()
            if not devices:
                raise RuntimeError("no adb devices online")
            device = devices[0]
        elif isinstance(device, str):
            device = adb.device(serial=device)
        self.device: AdbDevice = device

        self.listeners: dict = dict(frame=[], init=[], disconnect=[], raw_bytes=[])

        self.last_frame: Optional[Any] = None  # av.VideoFrame
        self.resolution: Optional[Tuple[int, int]] = None
        self.device_name: Optional[str] = None
        self.control = ControlSender(self)

        self.alive = False
        self.__server_stream: Optional[AdbConnection] = None
        self.__video_socket: Optional[socket.socket] = None
        self.control_socket: Optional[socket.socket] = None
        self.control_socket_lock = threading.Lock()
        self.stream_loop_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ deploy

    def __deploy_server(self) -> None:
        jar = self._server_jar
        if not jar.is_file():
            raise FileNotFoundError(f"scrcpy-server.jar 不存在：{jar}")
        # push 到设备 /data/local/tmp/，与上游路径一致
        self.device.sync.push(str(jar), "/data/local/tmp/scrcpy-server.jar")

        commands: List[str] = [
            "CLASSPATH=/data/local/tmp/scrcpy-server.jar",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            _SCRCPY_SERVER_VERSION,
            "log_level=info",
            f"max_size={self.max_width}",
            f"max_fps={self.max_fps}",
            f"video_bit_rate={self.bitrate}",
            "video_encoder=" + (self.encoder_name or "OMX.google.h264.encoder"),
            "video_codec=" + (self.codec_name or "h264"),
            "tunnel_forward=true",
            "send_frame_meta=false",
            "control=true",
            "audio=false",
            "show_touches=false",
            "stay_awake=" + ("true" if self.stay_awake else "false"),
            "power_off_on_close=false",
            "clipboard_autosync=false",
        ]
        self.__server_stream = self.device.shell(commands, stream=True)
        # 等 server 起来；它会先打日志到 stdout
        try:
            self.__server_stream.read(10)
        except Exception:
            pass

    def __init_server_connection(self) -> None:
        # 视频通道：scrcpy server 会监听 abstract socket "scrcpy"
        for _ in range(self.connection_timeout // 100):
            try:
                self.__video_socket = self.device.create_connection(
                    Network.LOCAL_ABSTRACT, "scrcpy"
                )
                break
            except AdbError:
                sleep(0.1)
        else:
            raise ConnectionError("scrcpy-server 启动后未能在 3s 内建立视频通道")

        dummy = self.__video_socket.recv(1)
        if not len(dummy) or dummy != b"\x00":
            raise ConnectionError("scrcpy 视频通道首字节异常")

        # 控制通道
        self.control_socket = self.device.create_connection(
            Network.LOCAL_ABSTRACT, "scrcpy"
        )
        # 设备名（64 byte 定长）
        name = self.__video_socket.recv(64).decode("utf-8", errors="ignore").rstrip("\x00")
        if not name:
            raise ConnectionError("scrcpy server 未返回 device_name")
        self.device_name = name

        # 注意：scrcpy server 1.20 在 device_name 后跟 4 byte ``WWHH`` 分辨率；
        # 2.x（我们用的 v2.4）已经把它移到 H.264 流自己的 SPS 里，不再额外发送。
        # 读那 4 byte 会把视频流前 4 byte 吞掉，导致首个 IDR 解码失败。
        # 这里直接跳过 → ``resolution`` 在收到首个解码帧时由 ``__stream_loop`` 写入。
        # 切非阻塞，loop 里靠 BlockingIOError 节流
        self.__video_socket.setblocking(False)

    # ------------------------------------------------------------------ run

    def start(self, threaded: bool = False, daemon_threaded: bool = False) -> None:
        assert not self.alive, "client already started"
        self.__deploy_server()
        self.__init_server_connection()
        self.alive = True
        self.__send_to_listeners(EVENT_INIT)
        if threaded or daemon_threaded:
            self.stream_loop_thread = threading.Thread(
                target=self.__stream_loop,
                daemon=daemon_threaded,
                name=f"scrcpy-stream-{self.device.serial if self.device else '?'}",
            )
            self.stream_loop_thread.start()
        else:
            self.__stream_loop()

    def stop(self) -> None:
        self.alive = False
        for sock in (self.control_socket, self.__video_socket):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        if self.__server_stream is not None:
            try:
                self.__server_stream.close()
            except Exception:
                pass
        self.control_socket = None
        self.__video_socket = None
        self.__server_stream = None

    def __stream_loop(self) -> None:
        codec = CodecContext.create("h264", "r")
        while self.alive:
            try:
                raw = self.__video_socket.recv(0x10000)
                if raw == b"":
                    raise ConnectionError("scrcpy 视频流被关闭")
                # MSE 路径：把 annex-B 字节直接喂给 fmp4 streamer，
                # 不需要再走 PyAV decode（CPU 友好）
                if self.listeners.get(EVENT_RAW_BYTES):
                    self.__send_to_listeners(EVENT_RAW_BYTES, raw)
                # 仅当确实有人需要 PIL frame（如旧 JPEG 路径 / OCR 调试）才
                # 解码；否则跳过 codec.parse + codec.decode，省 ~30% CPU
                if self.listeners.get(EVENT_FRAME):
                    packets = codec.parse(raw)
                    for packet in packets:
                        frames = codec.decode(packet)
                        for frame in frames:
                            # 不再立刻 to_ndarray；上层按需 to_image() 转 PIL，避免本
                            # 进程在没人订阅时白白消耗 CPU
                            self.last_frame = frame
                            self.resolution = (frame.width, frame.height)
                            self.__send_to_listeners(EVENT_FRAME, frame)
            except (BlockingIOError, InvalidDataError):
                # 没数据或解码瞬时坏：sleep 一个解码 tick
                sleep(0.005)
                if self.listeners.get(EVENT_FRAME) and not self.block_frame:
                    self.__send_to_listeners(EVENT_FRAME, None)
            except (ConnectionError, OSError) as exc:
                if self.alive:
                    logger.warning("scrcpy 视频流断开 device={}: {}", self.device.serial, exc)
                    self.__send_to_listeners(EVENT_DISCONNECT)
                self.stop()
                return

    # ------------------------------------------------------------------ listeners

    def add_listener(self, cls: str, listener: Callable[..., Any]) -> None:
        self.listeners.setdefault(cls, []).append(listener)

    def remove_listener(self, cls: str, listener: Callable[..., Any]) -> None:
        try:
            self.listeners.get(cls, []).remove(listener)
        except ValueError:
            pass

    def __send_to_listeners(self, cls: str, *args: Any, **kwargs: Any) -> None:
        for fn in list(self.listeners.get(cls, [])):
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # 单个 listener 不能拖死整个 loop
                logger.debug("scrcpy listener 抛异常 cls={} err={}", cls, exc)


__all__ = ["Client"]
