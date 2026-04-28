"""iOS 镜像通道（方案 C）：WDA MJPEG server → ffmpeg image2pipe → fmp4 → MSE。

为什么不用 dvt_screenshot：
- pmd3 DVT ``Screenshot.get_screenshot()`` 单张 ~350ms（实测 iPhone 16 Pro Max +
  iOS 26），实际帧率被锁死在 ~2-3fps，画面不连贯
- 持续高频调 DVT 会让 iPhone 明显发烫（GPU 一直在压缩 RGB48 PNG）
- 依赖 ``tunneld`` + DDI 挂载，链路长，任一环节挂掉都黑屏

为什么 wda_mjpeg：
- WDA 工程内置 ``GCDWebServer`` 跑 MJPEG，端口默认 9100，长连接 multipart 输出
  ``image/jpeg``。这是 Appium / Sonic / tidevice 在 iOS 17+ 实际跑的方案
- 帧率由 WDA 自己用 ``XCUIScreen.mainScreen().screenshot()`` 控制，可以
  跑到 20-30fps，单帧 ~50KB
- 依赖只剩 "WDA 已经在跑"——和我们做控制用的 8100 是同一个 WDA 进程，
  零额外服务、零额外终端
- 不依赖 ``tunneld`` / DDI / DVT 任意一项

链路：
    iPhone WDA (device:9100, MJPEG)
        ↓ usbmux 转发（_UsbmuxPortForwarder，复用 ios.py 那个）
    127.0.0.1:<local>
        ↓ httpx GET stream，按 multipart boundary 切 JPEG
    JPEG 帧
        ↓ ffmpeg image2pipe -c:v libx264 -f mp4 fragmented
    init segment + media segment
        ↓ 浏览器 MSE / <video>

设备旋转：每帧解析 JPEG 头里 SOF0 拿 W/H，发现 W/H 互换就 ``FMp4Streamer.restart()``，
和 dvt_screenshot 路径等价（前端 useMseMirror 一样能识别 init segment 尺寸变化）。

为什么和 dvt_screenshot 完全独立：
- 用户偏好低耦合（"复制文件"），两个后端不共享 Streamer 类，互不影响
- 出问题时 env 一切就能回退到 dvt_screenshot
"""
from __future__ import annotations

import io
import socket
import threading
import time
from typing import Callable, Optional, Tuple

import httpx
from loguru import logger

from .fmp4 import FMp4Streamer

# WDA mjpeg server multipart 流的最小切帧粒度
_HTTP_READ_CHUNK = 16 * 1024
# 给单帧 JPEG 的最大字节数（iPhone 16 Pro Max 1290x2796 quality=80 ~600KB；留 8MB 兜底）
_MAX_FRAME_BYTES = 8 * 1024 * 1024
# multipart boundary 一般是 "--BoundaryString"，我们直接扫 JPEG SOI（FF D8）/ EOI（FF D9）
# 这样不依赖 boundary 名，不同 WDA 版本都兼容
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"

# yuv420p + libx264 硬要求偶数宽高。
#
# **横屏处理**：不在 agent 侧做 transpose。WDA mjpeg server
# （``XCUIScreen.mainScreen().screenshot``）返回的就是**当前 UI 坐标系**
# 正向画面——竖屏 app 返回 W<H、横屏 app 返回 W>H，方向都是正的。
# 前端按 ``video.videoWidth vs videoHeight`` 识别方向翻 wrap 即可（和
# Android scrcpy / Sonic 同一套路径）。
#
# 踩过的坑：曾尝试"WDA 返回物理 canvas 永远竖屏，需要 agent transpose"
# 的假设，实测是错的 —— 手机横屏时 ffmpeg 再 transpose 反而把正向内容
# 转躺倒。保留这段注释给以后别再重复掉坑。
_EVEN_SCALE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"


class IosMjpegStreamer:
    """单台 iOS 设备的 WDA MJPEG → fmp4 推流器。

    对外 API 与 ``IosScreenStreamer`` 完全等价（``start`` / ``stop`` / ``restart``
    / ``is_alive``），让 ``_IosMirrorSession`` 可以用同一套生命周期管理代码。

    线程模型：
        - daemon ``_pump_loop`` 线程持续 GET ``/`` 拿 multipart MJPEG
        - 每拿到一帧 JPEG 立刻 ``FMp4Streamer.feed(jpeg_bytes)``
        - ffmpeg 自己的 stdout reader 线程产 init / media segment 回调
    """

    def __init__(
        self,
        serial: str,
        on_init: Callable[[bytes], None],
        on_segment: Callable[[bytes], None],
        target_fps: int = 20,
        jpeg_quality: int = 60,
        long_edge: int = 720,
        device_mjpeg_port: int = 9100,
        wda_local_port_for_settings: Optional[int] = None,
        frag_ms: int = 50,
        gop_sec: int = 1,
        log_tag: str = "ios-mjpeg",
    ) -> None:
        """
        Args:
            serial: udid
            on_init / on_segment: fmp4 init segment / media segment 回调
            target_fps: 请 WDA 输出的帧率，最终通过 appium settings 推下去
            jpeg_quality: 请 WDA 输出的 JPEG 质量（1-100）
            long_edge: 让 WDA 内部缩放到这个最长边像素（0 = 不缩放，输出原分辨率）
            device_mjpeg_port: WDA 在设备侧监听 mjpeg 的端口
            wda_local_port_for_settings: 已就绪 WDA 的本地端口（用于推 appium settings）。
                可选，None 时跳过 settings 下发，使用 WDA 默认参数。
            frag_ms / gop_sec: 透传给 FMp4Streamer
        """
        self._serial = serial
        self._on_init = on_init
        self._on_segment = on_segment
        self._target_fps = max(1, min(60, int(target_fps)))
        self._jpeg_quality = max(1, min(100, int(jpeg_quality)))
        self._long_edge = max(0, int(long_edge))
        self._device_mjpeg_port = int(device_mjpeg_port)
        self._wda_local_port = wda_local_port_for_settings
        self._log_tag = log_tag

        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._forwarder = None  # type: ignore[assignment]
        self._mjpeg_local_port: Optional[int] = None
        # 方向监听：WDA mjpeg canvas 尺寸**首次连接时定死**，不会随设备物理
        # 旋转自动改。监听 WDA orientation 变化 → set 这个 Event → pump_loop
        # 主动断开重连 → WDA 新连接按当前 orientation 重建 canvas → JPEG 尺寸
        # 变 → ``_last_size`` 检测到 → ffmpeg restart → 前端 video 'resize'
        # 事件触发 → wrap 翻框。整链打通。
        self._need_reconnect = threading.Event()
        self._last_orientation: Optional[str] = None
        self._rot_thread: Optional[threading.Thread] = None

        self._fmp4 = FMp4Streamer(
            on_init=on_init,
            on_segment=on_segment,
            framerate=self._target_fps,
            frag_ms=frag_ms,
            gop_sec=gop_sec,
            log_tag=log_tag,
            input_args=[
                # 同 ios_capture.py 的 dvt_screenshot 路径：用 wallclock 标 PTS，
                # 避免实际帧率 < target 时 buffered.end 推进慢于 wall-clock 而堆延迟
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                "image2pipe",
                "-framerate",
                str(self._target_fps),
                "-i",
                "pipe:0",
            ],
            # **关键**：WDA mjpegScalingFactor 是 1-100 整数百分比，经常算出奇数
            # 宽/高（比如 1290×2796 × 26% = 335×727，335 是奇数）。
            # libx264 + yuv420p 硬要求偶数宽高，遇到奇数会立即退出、一个 fmp4
            # segment 都吐不出来 → 前端 MSE 拿不到 init → 永远黑屏。
            # 这个 scale filter 把输入切到最近的偶数（最多切 1 个像素），
            # 在 libx264 之前就把格式敲平。几乎零开销。
            video_filter=_EVEN_SCALE,
        )

        self._last_size: Optional[Tuple[int, int]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self._setup_port_forward()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[{}] mjpeg 端口转发启动失败 udid={} device:{}: {}",
                self._log_tag, self._serial, self._device_mjpeg_port, exc,
            )
            self._stopped = True
            return

        # settings 下发挪到 pump loop 里"driver 就绪后第一次进 loop"时做，
        # 这里不做：start() 是同步路径，过早推 settings 会和 driver 的 session
        # 建立过程赛跑

        self._thread = threading.Thread(
            target=self._pump_loop,
            daemon=True,
            name=f"ios-mjpeg-{self._log_tag}",
        )
        self._thread.start()

        # orientation watcher：检测到 WDA orientation 变化 → set event
        # 让 pump_loop 主动断开重连，迫使 WDA 按新方向重建 mjpeg canvas
        self._rot_thread = threading.Thread(
            target=self._orientation_watch_loop,
            daemon=True,
            name=f"ios-mjpeg-rot-{self._log_tag}",
        )
        self._rot_thread.start()

        logger.info(
            "[{}] iOS MJPEG 推流已启动 fps≤{} quality={} long_edge={} device_port={}",
            self._log_tag, self._target_fps, self._jpeg_quality,
            self._long_edge, self._device_mjpeg_port,
        )

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self._fmp4.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._forwarder is not None:
            try:
                self._forwarder.stop()
            except Exception:  # noqa: BLE001
                pass
            self._forwarder = None
        # daemon 线程不显式 join

    def restart(self) -> None:
        try:
            self._fmp4.restart()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] fmp4 restart 失败: {}", self._log_tag, exc)
        self._last_size = None

    @property
    def is_alive(self) -> bool:
        return not self._stopped and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # 端口转发：复用 ios.py 的 _UsbmuxPortForwarder（同一进程内重用同一套 usbmux
    # 桥）。本地端口动态拿一个空闲的，避免和 wda 8100 / 多设备打架
    # ------------------------------------------------------------------
    def _setup_port_forward(self) -> None:
        from ai_phone.agent.drivers.ios import _UsbmuxPortForwarder  # noqa: PLC0415

        local_port = _grab_free_local_port()
        fwd = _UsbmuxPortForwarder(
            udid=self._serial,
            local_port=local_port,
            device_port=self._device_mjpeg_port,
        )
        fwd.start()
        self._forwarder = fwd
        self._mjpeg_local_port = local_port

    def _push_wda_mjpeg_settings(self) -> None:
        """把帧率 / 质量 / 缩放推给 WDA。**必须复用 driver 的 WdaClient**，
        绝对不能自己 new 一个再 ``create_session`` —— WDA 是单 session 模型，
        后来的 POST /session 会把 driver 的 session 顶掉。顶掉后：

        - mjpeg server 没有 active screen，直接 HTTP 502（这个 bug 当场中过一次）
        - 手动 tap / swipe 等所有控制全部 404 fail

        所以从 ``ios.py::_WDA_CLIENT_MAP`` 取 driver 常驻的那把 client，
        调 ``update_appium_settings``，**不 close**（不碰 session 生命周期）。
        取不到就跳过（WDA 会用 cap/工程默认参数跑 mjpeg）。
        """
        try:
            from ai_phone.agent.drivers.ios import _WDA_CLIENT_MAP  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return

        wda = _WDA_CLIENT_MAP.get(self._serial)
        if wda is None:
            logger.info(
                "[{}] 跳过 mjpeg settings 下发：driver 还没就绪 / 未放入 _WDA_CLIENT_MAP，"
                "WDA 会用工程默认参数跑 mjpeg（通常 10fps + quality 25）",
                self._log_tag,
            )
            return

        settings: dict = {
            "mjpegServerScreenshotQuality": int(self._jpeg_quality),
            "mjpegServerFramerate": int(self._target_fps),
        }
        if self._long_edge > 0:
            # WDA 是按"百分比"缩放，不能直接传像素；取 iPhone 系列最长边 2796
            # 做保守上限估算缩放百分比。WDA 接受的是 ``mjpegScalingFactor``：
            # 1-100 整数。target=720 → ~26%，target=1080 → ~39%
            pct = max(10, min(100, int(round(self._long_edge / 2796 * 100))))
            settings["mjpegScalingFactor"] = pct
        # **不 close 这个 client** —— 它是 driver 的公共引用
        wda.update_appium_settings(settings)
        logger.info("[{}] WDA mjpeg settings 已下发: {}", self._log_tag, settings)

    # ------------------------------------------------------------------
    # MJPEG 抓取主循环
    # ------------------------------------------------------------------
    def _pump_loop(self) -> None:
        """长连接 GET WDA mjpeg server，按 SOI/EOI 切 JPEG 帧 → ffmpeg。

        **常见故障码**（跟 Appium WDA ``FBMjpegServer.m`` 实现对应）：

        - ``502 Bad Gateway``：WDA 没 active session（没 ``POST /session``），
          ``XCUIScreen.mainScreen().screenshot`` 抛异常。解决：确保 driver
          已就绪（``_WDA_CLIENT_MAP`` 已填，我们在 ``_IosMirrorSession.start``
          的 wda_mjpeg 分支里已经主动 ``_get_or_open_driver``）。遇到 502
          会轮询等 driver，不盲目 5s 重连
        - ``Connection refused``：WDA 工程没启用 mjpegServer（fork 可能把它关了）
          或 ``USE_PORT`` 被改。解决：Xcode 里检查 WebDriverAgent 工程启动
          log 有没有 "MJPEG server listening at :9100"
        """
        url = f"http://127.0.0.1:{self._mjpeg_local_port}/"
        consecutive_errors = 0
        settings_pushed = False

        # 帧率 / 耗时统计，每 N 帧打一行
        _stat_window = 30
        _stat_count = 0
        _stat_pil_ms = 0.0
        _stat_total_start = time.monotonic()

        while not self._stopped:
            # 惰性推 settings：第一次进 loop + driver 的 WdaClient 就绪后，才推一次。
            # 这样就能处理"mirror 比 driver 先 start"的时序（虽然我们在 main.py
            # 里已经让 _IosMirrorSession.start 同步等 _get_or_open_driver，但
            # 那边也有可能因为 WDA 未配置失败；这里就进不去分支，保持 WDA 默认参数）
            if not settings_pushed:
                try:
                    from ai_phone.agent.drivers.ios import _WDA_CLIENT_MAP  # noqa: PLC0415
                    if self._wda_local_port and _WDA_CLIENT_MAP.get(self._serial):
                        self._push_wda_mjpeg_settings()
                        settings_pushed = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[{}] 推 settings 延迟到下轮: {}", self._log_tag, exc,
                    )

            try:
                with httpx.stream(
                    "GET",
                    url,
                    timeout=httpx.Timeout(10.0, read=None, connect=5.0),
                    headers={"Accept": "multipart/x-mixed-replace, image/jpeg"},
                ) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"WDA mjpeg HTTP {resp.status_code}: "
                            f"{resp.read()[:200]!r}"
                        )
                    consecutive_errors = 0
                    buf = bytearray()
                    for chunk in resp.iter_bytes(_HTTP_READ_CHUNK):
                        if self._stopped:
                            break
                        if self._need_reconnect.is_set():
                            logger.info(
                                "[{}] 检测到 WDA orientation 变化，断开重连",
                                self._log_tag,
                            )
                            self._need_reconnect.clear()
                            break  # 跳出 iter_bytes → 外层 with 退出 → while 重连
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        # 切完所有完整帧再继续读
                        while True:
                            if self._stopped:
                                break
                            soi = buf.find(_JPEG_SOI)
                            if soi < 0:
                                buf.clear()
                                break
                            eoi = buf.find(_JPEG_EOI, soi + 2)
                            if eoi < 0:
                                # 还没拿够这一帧的字节，等下一波
                                if soi > 0:
                                    del buf[:soi]
                                if len(buf) > _MAX_FRAME_BYTES:
                                    logger.warning(
                                        "[{}] 单帧 > {}MB 仍未见 EOI，丢弃缓冲重置",
                                        self._log_tag, _MAX_FRAME_BYTES // (1024 * 1024),
                                    )
                                    buf.clear()
                                break
                            jpeg = bytes(buf[soi : eoi + 2])
                            del buf[: eoi + 2]

                            _t0 = time.monotonic()
                            size = self._peek_jpeg_size(jpeg)
                            _stat_pil_ms += (time.monotonic() - _t0) * 1000

                            self._consume_jpeg(jpeg, size)
                            _stat_count += 1
                            if _stat_count >= _stat_window:
                                elapsed = time.monotonic() - _stat_total_start
                                actual_fps = _stat_count / elapsed if elapsed > 0 else 0
                                logger.info(
                                    "[{}] mjpeg stat: 实际 {:.1f}fps (target≤{}fps) "
                                    "JPEG 头解析平均 {:.1f}ms 输出 {} 张",
                                    self._log_tag, actual_fps, self._target_fps,
                                    _stat_pil_ms / _stat_count, _stat_count,
                                )
                                _stat_count = 0
                                _stat_pil_ms = 0.0
                                _stat_total_start = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                if self._stopped:
                    break
                consecutive_errors += 1
                if consecutive_errors <= 3 or consecutive_errors % 30 == 0:
                    logger.warning(
                        "[{}] mjpeg 拉流异常 #{}: {}（5s 后重连）",
                        self._log_tag, consecutive_errors, exc,
                    )
                if consecutive_errors >= 60:
                    logger.error(
                        "[{}] 连续 60 次 mjpeg 拉流失败，停止 iOS 镜像",
                        self._log_tag,
                    )
                    self._stopped = True
                    break
                time.sleep(5.0)
                continue

        logger.info("[{}] iOS MJPEG 推流线程已退出", self._log_tag)

    # ------------------------------------------------------------------
    # 单帧消费：默认走 ffmpeg → fmp4 → MSE。子类（passthrough）可 override
    # 改成 "直接发 JPEG 给 server" 的路径，复用全部拉流 / 重连 / settings 代码。
    # ------------------------------------------------------------------
    def _consume_jpeg(
        self, jpeg: bytes, size: Optional[Tuple[int, int]]
    ) -> None:
        if (
            size is not None
            and self._last_size is not None
            and size != self._last_size
        ):
            logger.info(
                "[{}] 检测到 mjpeg 尺寸变化 {}→{}，重启 ffmpeg",
                self._log_tag, self._last_size, size,
            )
            self.restart()
        if size is not None:
            self._last_size = size
        self._fmp4.feed(jpeg)

    # ------------------------------------------------------------------
    # 方向监听：WDA mjpeg canvas 首次连接时定死尺寸，不会跟物理旋转变。
    # 我们周期查 WDA ``/orientation``，变化就 set reconnect event 让 pump_loop
    # 主动断开 mjpeg 流；WDA 新连接按当前方向重建 canvas → JPEG 尺寸变 →
    # 已有"尺寸变 → restart ffmpeg"链路自动接上 → 前端 video 'resize' 事件
    # 触发 → wrap 翻框。
    # ------------------------------------------------------------------
    def _orientation_watch_loop(self) -> None:
        time.sleep(2.0)  # 等 driver session + _WDA_CLIENT_MAP 就绪
        poll_interval = 1.5
        while not self._stopped:
            try:
                from ai_phone.agent.drivers.ios import _WDA_CLIENT_MAP  # noqa: PLC0415
                wda = _WDA_CLIENT_MAP.get(self._serial)
                if wda is not None:
                    orient = (wda.orientation() or "").upper()
                    if self._last_orientation is None:
                        self._last_orientation = orient
                    elif orient and orient != self._last_orientation:
                        logger.info(
                            "[{}] WDA orientation {} → {}，触发 mjpeg 重连",
                            self._log_tag, self._last_orientation, orient,
                        )
                        self._last_orientation = orient
                        self._need_reconnect.set()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[{}] orientation 查询失败: {}", self._log_tag, exc)
            for _ in range(int(poll_interval * 10)):
                if self._stopped:
                    break
                time.sleep(0.1)
        logger.debug("[{}] 方向监听线程已退出", self._log_tag)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _peek_jpeg_size(jpeg_bytes: bytes) -> Optional[Tuple[int, int]]:
        """快速读 JPEG 尺寸，不解码全图。"""
        try:
            from PIL import Image  # noqa: PLC0415
            with Image.open(io.BytesIO(jpeg_bytes)) as im:
                return int(im.size[0]), int(im.size[1])
        except Exception:  # noqa: BLE001
            return None


def _grab_free_local_port() -> int:
    """让 OS 分配一个空闲端口；bind→getsockname→close→返回。

    短暂的 TIME_WAIT 风险接受：用完立刻被 _UsbmuxPortForwarder 重新 bind，
    macOS 下连续抢同一端口几乎不会失败（SO_REUSEADDR 也开了）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    finally:
        s.close()
    return port


__all__ = ["IosMjpegStreamer"]
