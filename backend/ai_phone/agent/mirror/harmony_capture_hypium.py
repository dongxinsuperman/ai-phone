"""HarmonyOS 镜像 —— P3-B：hypium Captures MJPEG 流。

**核心发现**：HOScrcpy 客户端反查协议 + hmdriver2 ``_screenrecord.py`` 源码交叉验证，
HarmonyOS 官方 ``com.ohos.devicetest.hypiumApiHelper`` 的 ``Captures`` 服务
（``startCaptureScreen`` / ``stopCaptureScreen``）**不是** H.264 流，而是设备侧
硬件编码出来的 **MJPEG 流**：socket 长连一次后，设备主动 push
``\\xff\\xd8 ... \\xff\\xd9`` 完整 JPEG 帧序列。

**与 P3-A 的本质差异**：

- P3-A 截图轮询：每帧 ``hdc shell snapshot_display -f`` + ``hdc file recv``，
  10fps 是物理上限，CPU/USB 都在做无用功
- P3-B hypium MJPEG：socket 长连一次，设备 push，**省掉每帧 hdc 往返**，
  实测 30fps（设备侧硬编码后直接走 uitest socket）

**与 iOS ``mjpeg_passthrough`` 的关系**：

- 上层数据契约完全一致：``on_jpeg(jpeg_bytes, w, h)`` → ``MSG_MIRROR_JPEG``
  → 前端 ``useJpegMirror`` 渲染
- 折叠屏 / 异形屏 / 横竖屏切换：每帧自带尺寸，前端 ``<img>`` 天然自适应
- 上层 ``_HarmonyMirrorSession`` 不感知 backend 差异，靠 ``build_harmony_streamer``
  工厂分发

**socket 隔离策略**：

hmdriver2 的 ``HmClient`` 是**每实例一把 socket**（不复用），并且 control 通道
（click/swipe）已经被 ``HarmonyDriver`` 持有的 client 占用。本 streamer
**新建独立 ``HmClient`` 实例**，专门读 MJPEG，避免 control 与 video 互相阻塞。
两把 socket 共享同一个 ``hdc fport`` 端口（uitest daemon 自身支持多客户端）。
"""
from __future__ import annotations

import io
import json
import socket
import threading
import time
from datetime import datetime
from typing import Callable, Optional, Tuple

from PIL import Image
from loguru import logger


_SOCKET_RECV_TIMEOUT = 8.0  # MJPEG 流稳态下每个 recv 应该 <100ms 拿到数据
_RECV_CHUNK = 4 * 1024 * 1024  # 4MiB；和 hmdriver2 RecordClient 一致

# JPEG 帧分隔符
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


class HarmonyHypiumStreamer:
    """鸿蒙 hypium MJPEG 流。接口与 ``HarmonyScreenshotStreamer`` 严格对齐
    （``start`` / ``stop`` / ``restart`` / ``is_alive``），让上层不感知后端差异。

    Args:
        serial: 设备 udid，用于日志
        local_port: 当前 ``HarmonyDriver`` 已建立的精确本地 fport（首次连接用）
        on_jpeg: 每帧回调，签名 ``(jpeg_bytes, width, height) -> None``
        port_provider: 可选。每次（重）连接前回调，取控制通道**当前** fport。
            控制侧自愈（L2/L3）重建 Driver 会换 fport（16556→16557），旧端口
            随即失效；若镜像仍死连构造时的旧端口，会连续失败直至线程退出、黑屏。
            传入 ``driver.current_fport`` 后，每次重连都刷成最新端口，实现镜像
            跟随控制通道自愈。为 None 时退化为固定端口（老行为）。
        log_tag: 日志前缀
    """

    def __init__(
        self,
        serial: str,
        local_port: int,
        on_jpeg: Callable[[bytes, int, int], None],
        *,
        port_provider: Optional[Callable[[], Optional[int]]] = None,
        log_tag: str = "hm-hypium",
    ) -> None:
        self._serial = serial
        self._on_jpeg = on_jpeg
        self._log_tag = log_tag
        self._local_port = int(local_port)
        self._port_provider = port_provider
        if not 1 <= self._local_port <= 65535:
            raise ValueError(f"invalid Harmony mirror local_port: {local_port}")

        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._last_size: Optional[Tuple[int, int]] = None
        # 每次连接是否已经产出过有效帧。只有“连上但一帧都没有”的失败
        # 才应连续累计；一条已经恢复出帧的链路日后再断，应从第 1 次
        # 重新计数，不能把整个 streamer 生命周期的断流累加起来。
        self._attempt_had_frame = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run_with_retry,
            name=f"hm-hypium-{self._serial[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info("[{}] 启动 hypium MJPEG 流", self._log_tag)

    def stop(self) -> None:
        self._stopped = True
        self._close_sock()
        # 不 join 主线程；和 P3-A 同款保守策略
        self._thread = None
        logger.info("[{}] 停止 hypium MJPEG 流", self._log_tag)

    def restart(self) -> None:
        """断流自愈在 ``_run_with_retry`` 内部已闭环，无需外部 restart。"""

    @property
    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stopped
        )

    # ------------------------------------------------------------------
    # 主循环 + 自愈
    # ------------------------------------------------------------------
    def _run_with_retry(self) -> None:
        """连接 → 读流，断了 sleep 后重连。最多连续失败 5 次后退出，让上层 supervisor
        看到 ``is_alive=False`` 走整体重启（和 iOS mjpeg_passthrough 同款策略）。
        """
        consecutive_fail = 0
        while not self._stopped:
            self._attempt_had_frame = False
            try:
                self._connect_and_pump()
                consecutive_fail = 0  # 主循环正常退出（被 stop）才会到这
            except Exception as exc:  # noqa: BLE001
                if self._stopped:
                    return
                # 上一条连接已收到过有效 JPEG，证明它确实恢复过。
                # 这次断流是新的第 1 次，不与历史已恢复故障累加。
                if self._attempt_had_frame:
                    consecutive_fail = 0
                consecutive_fail += 1
                logger.warning(
                    "[{}] 流中断（第 {} 次）: {}",
                    self._log_tag, consecutive_fail, exc,
                )
                self._close_sock()
                if consecutive_fail >= 5:
                    logger.error(
                        "[{}] 连续 5 次失败，退出 streamer 让 supervisor 接管",
                        self._log_tag,
                    )
                    return
                # 退避：1s / 2s / 4s / 8s / 16s
                time.sleep(min(16.0, 2 ** (consecutive_fail - 1)))

    def _connect_and_pump(self) -> None:
        """连接精确 Driver fport → startCaptureScreen → 读流，直到出错或 stop。"""

        # 每次（重）连接前都从 driver 的锁内对账入口取端口。对账失败时本轮重连
        # 必须 fail-closed，不能沿用旧端口；否则旧 fport 被其它映射复用时可能串流。
        if self._port_provider is not None:
            try:
                fresh = self._port_provider()
                if fresh is None or not 1 <= int(fresh) <= 65535:
                    raise ValueError(f"invalid fport: {fresh}")
                if int(fresh) != self._local_port:
                    logger.info(
                        "[{}] 控制通道 fport 变化，镜像跟随 {} → {}",
                        self._log_tag, self._local_port, int(fresh),
                    )
                    self._local_port = int(fresh)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"harmony_mirror_fport_reconcile_failed:serial={self._serial}:{exc}"
                ) from exc

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(_SOCKET_RECV_TIMEOUT)
        self._sock.connect(("127.0.0.1", self._local_port))
        logger.info(
            "[{}] socket 已连 127.0.0.1:{}（独立通道，与控制通道分离）",
            self._log_tag, self._local_port,
        )

        self._send_captures("startCaptureScreen", [])
        # 协议响应：``{"result":"true",...}`` 或 ``{"result":"false","exception":...}``
        # 第一包通常 <1KB，单独 recv 一次拿响应；之后才是 MJPEG 流
        reply = self._recv_first_reply()
        if "true" not in reply:
            raise RuntimeError(f"startCaptureScreen 响应非 true: {reply!r}")
        logger.info("[{}] startCaptureScreen 已确认，开始读 MJPEG 流", self._log_tag)

        try:
            self._pump_jpeg_loop()
        finally:
            # 优雅停（best-effort，stop 命令失败也无所谓 —— socket close 后设备自动停推）
            try:
                self._send_captures("stopCaptureScreen", [])
            except Exception:  # noqa: BLE001
                pass

    def _pump_jpeg_loop(self) -> None:
        """从 socket 切 JPEG 帧，每帧调 ``on_jpeg``。和 hmdriver2 RecordClient
        ``_record_worker`` 同款 SOI/EOI 切分。
        """
        assert self._sock is not None
        buf = bytearray()

        # 统计窗口
        stat_n = 30
        stat_count = 0
        stat_total_ms = 0.0
        stat_window_start = time.monotonic()

        while not self._stopped:
            frame_start = time.monotonic()
            try:
                chunk = self._sock.recv(_RECV_CHUNK)
            except socket.timeout:
                # 超时就 continue 一下，stop 标志位是 while 头检查的
                # 真断流会被下一次 recv 抛 ConnectionError
                continue
            if not chunk:
                raise ConnectionError("socket EOF（设备主动断开）")
            buf += chunk

            # 一次 recv 可能包含 0~N 个完整 JPEG，循环切到不能切为止
            while True:
                s_idx = buf.find(_SOI)
                if s_idx < 0:
                    # 没有 SOI 了，buffer 里全是垃圾，丢掉
                    if buf:
                        buf.clear()
                    break
                e_idx = buf.find(_EOI, s_idx + 2)
                if e_idx < 0:
                    # SOI 找到了但 EOI 还没来；保留 [s_idx:] 等下一轮
                    if s_idx > 0:
                        del buf[:s_idx]
                    break
                jpeg = bytes(buf[s_idx : e_idx + 2])
                del buf[: e_idx + 2]

                w, h = self._peek_size(jpeg)
                # 解析到完整 JPEG 才算本次连接健康；只完成 TCP/协议握手
                # 但一帧都拿不到，仍属于连续失败，不能被偷偷清零。
                self._attempt_had_frame = True
                if (w, h) != self._last_size and self._last_size is not None:
                    logger.info(
                        "[{}] 画面尺寸变化 {} → {}（旋转/分辨率切换）",
                        self._log_tag, self._last_size, (w, h),
                    )
                self._last_size = (w, h)

                try:
                    self._on_jpeg(jpeg, w, h)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[{}] on_jpeg 回调异常: {}", self._log_tag, exc)

                stat_count += 1
                stat_total_ms += (time.monotonic() - frame_start) * 1000.0
                if stat_count >= stat_n:
                    window = time.monotonic() - stat_window_start
                    actual_fps = stat_count / max(0.001, window)
                    avg_ms = stat_total_ms / stat_count
                    logger.info(
                        "[{}] mirror stat: 实际 {:.1f}fps 单帧平均 {:.1f}ms 输出 {} 张",
                        self._log_tag, actual_fps, avg_ms, stat_count,
                    )
                    stat_count = 0
                    stat_total_ms = 0.0
                    stat_window_start = time.monotonic()
                # 切下一帧时刷新 frame_start，让统计反映 push 节拍
                frame_start = time.monotonic()

    # ------------------------------------------------------------------
    # hypium 协议封装（不依赖 hmdriver2 私有 API，避免版本耦合）
    # ------------------------------------------------------------------
    def _send_captures(self, api: str, args: list) -> None:
        assert self._sock is not None
        msg = {
            "module": "com.ohos.devicetest.hypiumApiHelper",
            "method": "Captures",
            "params": {"api": api, "args": args},
            "request_id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        }
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self._sock.sendall(payload)

    def _recv_first_reply(self) -> str:
        """读 startCaptureScreen 的首响应；用 1KB buf 拿到字符串就够。

        如果首包恰好和首帧 JPEG 粘包了（边界情况），这里只解前面的 JSON 段，
        剩余的 JPEG 字节会被 ``_pump_jpeg_loop`` 第一轮 recv 重新读取。
        实际抓包看 uitest 是 ``\\n`` 分包，JSON 单独一行，几乎不会粘包。
        """
        assert self._sock is not None
        try:
            data = self._sock.recv(1024)
        except socket.timeout:
            raise RuntimeError("startCaptureScreen 等首响应超时（uitest 没回）")
        # 设备返回的是 utf-8 JSON，截断到第一个 \n 即可
        try:
            line = data.split(b"\n", 1)[0]
            return line.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return repr(data[:128])

    # ------------------------------------------------------------------
    # socket 清理
    # ------------------------------------------------------------------
    def _close_sock(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _peek_size(jpeg: bytes) -> Tuple[int, int]:
        """JPEG 尺寸快速解析（Pillow 惰性读 SOF）。"""
        try:
            with Image.open(io.BytesIO(jpeg)) as img:
                return int(img.size[0]), int(img.size[1])
        except Exception:  # noqa: BLE001
            return 0, 0


__all__ = ["HarmonyHypiumStreamer"]
