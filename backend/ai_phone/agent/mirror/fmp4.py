"""H.264 annex-B → fragmented MP4 streaming muxer，基于 ffmpeg 子进程。

为什么用子进程而不是 PyAV：
- ffmpeg ``-c:v copy -f mp4 -movflags +empty_moov+default_base_moof+...``
  是经过工业打磨的 fmp4 muxer，输出可被浏览器 MSE 直接 ``appendBuffer``
- PyAV 没有把"产出 init segment"和"产出每个 media segment"清晰分开的 API；
  自己写 fmp4 muxer 又太多 bit-twiddling 代码（avcC/moov/moof/trun 等）
- ffmpeg 8.x 在 Mac 上 brew 默认装；其它平台容器/脚本里也是一行 apt
- ``-c:v copy`` 不重编码，CPU < 5% 即可处理 6Mbps 流

工作流：
  scrcpy raw bytes (annex-B H.264) ──> ffmpeg stdin
                                           │
                                           ▼
                                    ffmpeg fmp4 muxer
                                           │
                                           ▼
                       stdout: ftyp + moov + (moof + mdat)*
                                           │
                                           ▼
                          _BoxScanner 切成完整顶层 box
                                           │
                                           ▼
                  on_init(ftyp+moov)  /  on_segment(moof+mdat)

线程模型：
- ``feed()`` 由 scrcpy decode 线程同步调用，只做 ``stdin.write``
- 一个 daemon 读线程跑 ``stdout.read``，攒满 box 就回调 on_init / on_segment
- 一个 daemon 读线程跑 ``stderr.readline``，把 ffmpeg 警告/错误转 loguru

回调可能在读线程里触发；调用方负责线程安全（agent 这边用 ``asyncio.run_coroutine_threadsafe``）。
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Callable, Iterable, List, Optional, Tuple

from loguru import logger


_FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"


# ---------------------------------------------------------------------------
# ISO BMFF 顶层 box 扫描
# ---------------------------------------------------------------------------
def _scan_top_level_boxes(buf: bytes) -> Iterable[Tuple[str, bytes, int]]:
    """从 ``buf`` 头部依次切出完整顶层 box，遇到不完整就停下。

    yield ``(box_type, box_bytes, box_end_pos)``；调用方按最后一次 yield 的
    end_pos 截断 buf 即可（没 yield 时 end_pos 视为 0）。
    """
    pos = 0
    n = len(buf)
    while pos + 8 <= n:
        size = int.from_bytes(buf[pos : pos + 4], "big")
        box_type = bytes(buf[pos + 4 : pos + 8]).decode("ascii", errors="replace")
        if size == 1:
            # 64-bit largesize
            if pos + 16 > n:
                return
            size = int.from_bytes(buf[pos + 8 : pos + 16], "big")
        elif size == 0:
            # box 延伸到文件尾。在流式场景里我们看不到尾，视为不完整等下一轮
            return
        if size < 8:
            # 损坏：跳过这一字节，宁可错过也不死循环
            pos += 1
            continue
        if pos + size > n:
            return
        yield box_type, bytes(buf[pos : pos + size]), pos + size
        pos += size


def extract_resolution_from_moov(moov_bytes: bytes) -> Optional[Tuple[int, int]]:
    """从 fmp4 init segment（含 moov）里捞出视频分辨率 ``(w, h)``。

    依据：``avc1`` VisualSampleEntry 的固定布局，width/height 在 type 字段
    后 28/30 字节（uint16 big-endian）。
    """
    p = moov_bytes.find(b"avc1")
    if p < 0:
        return None
    if p + 32 > len(moov_bytes):
        return None
    w = int.from_bytes(moov_bytes[p + 28 : p + 30], "big")
    h = int.from_bytes(moov_bytes[p + 30 : p + 32], "big")
    if 0 < w <= 16384 and 0 < h <= 16384:
        return (w, h)
    return None


def extract_sps_nal(raw: bytes) -> Optional[bytes]:
    """从 H.264 annex-B 字节串里找出第一个 SPS NAL，返回其 raw bytes（不含 start code）。

    annex-B 格式：每个 NAL 由 ``0x00000001``（4 字节）或 ``0x000001``（3 字节）
    start code 引导。NAL header 第一字节的低 5 bit 是 ``nal_unit_type``，``7`` = SPS。

    用途：scrcpy v2 在 H.264 流自身里携带 SPS/PPS（``send_frame_meta=false`` 没
    额外旋转事件），设备旋转后会重发 SPS（分辨率字段变了 → 字节也变）。上层只要
    比较 SPS bytes 是否变化，就能判定 "需要重启 fmp4 流水线"。比手写 Exp-Golomb
    解析整个 SPS 拿 width/height 简单几十倍，且足够准确。
    """
    n = len(raw)
    if n < 5:
        return None
    i = 0
    while i + 3 < n:
        if raw[i] == 0 and raw[i + 1] == 0:
            sc_len = 0
            if i + 3 < n and raw[i + 2] == 0 and raw[i + 3] == 1:
                sc_len = 4
            elif raw[i + 2] == 1:
                sc_len = 3
            if sc_len:
                start = i + sc_len
                if start < n and (raw[start] & 0x1F) == 7:
                    j = start + 1
                    while j + 2 < n:
                        if raw[j] == 0 and raw[j + 1] == 0 and (
                            raw[j + 2] == 1
                            or (j + 3 < n and raw[j + 2] == 0 and raw[j + 3] == 1)
                        ):
                            return bytes(raw[start:j])
                        j += 1
                    return bytes(raw[start:])
                i = start
                continue
        i += 1
    return None


def extract_codec_string_from_moov(moov_bytes: bytes) -> Optional[str]:
    """读出 ``avcC`` 里的 profile/compat/level，拼成 MSE 期望的 codec 字符串。

    ISO/IEC 14496-15 AVCDecoderConfigurationRecord 头 4 字节：
        configurationVersion(1) | AVCProfileIndication(1)
        | profile_compatibility(1) | AVCLevelIndication(1)
    在 box 里：[size:4][type='avcC':4][配置头4...]，所以从 type 起 +4..+8 是
    那 4 字节，+5..+7 即 profile/compat/level。

    返回形如 ``"avc1.42E01E"``；找不到 / 不合法时返回 ``None``，调用方可回退到
    通用 ``avc1.42E01E``（Constrained Baseline 3.0）。
    """
    p = moov_bytes.find(b"avcC")
    if p < 0:
        return None
    # avcC 的 type 在 p..p+4，payload 从 p+4 开始
    if p + 8 > len(moov_bytes):
        return None
    profile = moov_bytes[p + 5]
    compat = moov_bytes[p + 6]
    level = moov_bytes[p + 7]
    return f"avc1.{profile:02X}{compat:02X}{level:02X}"


# ---------------------------------------------------------------------------
# Streamer
# ---------------------------------------------------------------------------
class FMp4Streamer:
    """单设备 scrcpy → fmp4 流转换器。

    生命周期由 ``_MirrorSession`` 持有：scrcpy 起来时 ``feed()``，scrcpy 停
    或者 mirror 关闭时 ``stop()``。
    """

    def __init__(
        self,
        on_init: Callable[[bytes], None],
        on_segment: Callable[[bytes], None],
        framerate: int = 30,
        frag_ms: int = 50,
        gop_sec: int = 1,
        ffmpeg_bin: Optional[str] = None,
        log_tag: str = "fmp4",
        input_args: Optional[List[str]] = None,
        video_filter: Optional[str] = None,
    ) -> None:
        """``input_args``：ffmpeg 输入侧参数（包含 ``-f`` / ``-framerate`` / ``-i pipe:0``）。

        默认 None → 走 Android scrcpy 的 raw H.264 annex-B 输入（``-f h264``）。
        iOS 会传 ``['-f','image2pipe','-framerate', '8','-i','pipe:0']`` 之类，
        把 PNG/JPEG 序列喂给 ffmpeg，由 libx264 编码成 H.264 → fmp4，
        浏览器侧 MSE 完全无感（前端不需要按平台分支）。

        ``video_filter``：``-vf`` 的值。最常见用途是把输入尺寸对齐到偶数
        （``yuv420p + libx264`` 硬要求），典型值
        ``scale=trunc(iw/2)*2:trunc(ih/2)*2``。不填就不插 -vf。
        （iOS wda_mjpeg 路径必用：WDA mjpegScalingFactor 是 1-100 百分比，
         经常算出奇数宽/高，没这个 filter 直接让 ffmpeg "width not divisible
         by 2" 退出、一个 segment 都吐不出来、前端黑屏。）
        """
        self._on_init = on_init
        self._on_segment = on_segment
        self._framerate = max(1, int(framerate))
        # 16ms 是 ffmpeg muxer 能接受的下限；上限 1000ms（1s）已经太离谱了
        self._frag_ms = max(16, min(1000, int(frag_ms)))
        # 0 = 每帧都 IDR（极端低延迟，码率最高）；上限 10 秒
        self._gop_sec = max(0, min(10, int(gop_sec)))
        self._bin = ffmpeg_bin or _FFMPEG_BIN
        self._log_tag = log_tag
        self._input_args: List[str] = list(input_args) if input_args else []
        self._video_filter: Optional[str] = (video_filter or "").strip() or None

        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stopped = False
        self._spawn_lock = threading.Lock()

        # box 扫描状态
        self._scan_buf = bytearray()
        self._init_emitted = False
        self._pending_init: List[bytes] = []  # 累积 ftyp，遇到 moov 就一起送
        self._pending_seg: List[bytes] = []  # 累积 moof，遇到 mdat 就一起送

    # ------------------------------------------------------------------ life
    def _spawn(self) -> bool:
        """启动 ffmpeg 子进程；线程安全。"""
        with self._spawn_lock:
            if self._proc is not None:
                return True
            # 默认输入侧（Android raw H.264 annex-B）
            default_input = [
                "-fflags",
                "+flush_packets+genpts",
                "-flags",
                "low_delay",
                "-analyzeduration",
                "0",
                "-probesize",
                "32",
                "-f",
                "h264",
                "-framerate",
                str(self._framerate),
                "-i",
                "pipe:0",
            ]
            input_section = self._input_args if self._input_args else default_input
            cmd = [
                self._bin,
                "-hide_banner",
                # 临时调到 info；调通后改回 warning。verbose 太吵
                "-loglevel",
                "info",
                # 输入侧低延迟：不缓冲、自动生成 PTS
                # 注意：早期版本曾加 +nobuffer，但与 -f h264 raw 输入组合时
                # 会让 demuxer 直接丢光 NAL（实测 0 frame），所以这里只保留
                # flush_packets/genpts/low_delay
                # 注意 2：曾尝试 -use_wallclock_as_timestamps 1 想消掉 mp4 muxer
                # 的 "Timestamps are unset" 警告，但实测在 -c:v copy 路径下会
                # 让 ffmpeg 50 秒后悄悄退出（stderr 无任何输出），所以放弃该
                # 优化，时间戳警告先留着，后面有需要再用 bsf 给 PTS。
                *input_section,
                # 可选 -vf（主要给 iOS wda_mjpeg 路径做尺寸偶数对齐）
                *(["-vf", self._video_filter] if self._video_filter else []),
                # 重编码：scrcpy 推过来的 raw H.264 没有 PTS/DTS，``-c:v copy``
                # + mp4 muxer 在新版 ffmpeg 上会因为 "Timestamps are unset"
                # 在 ~26 帧后悄悄退出（time=N/A），导致 MSE 几秒就断流。
                # 改成 libx264 ultrafast + zerolatency 重编码：
                #   - 由 ffmpeg 自己根据 -framerate 30 生成单调 PTS/DTS
                #   - ultrafast preset 在 M1 上 720p@30 约 5~8% CPU，可接受
                #   - zerolatency 关掉 B 帧 + 1 frame lookahead，端到端 ~50ms
                #   - 走 baseline profile，与原 scrcpy 输出一致，浏览器都能解
                #   - keyint=fps 保证每秒一个 IDR，配合 frag_keyframe 切片
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-profile:v",
                "baseline",
                "-pix_fmt",
                "yuv420p",
                # GOP：gop_sec=0 表示每帧都 IDR；正常 gop_sec=1 → fps 帧 1 IDR
                "-g",
                str(max(1, self._gop_sec * self._framerate)),
                "-keyint_min",
                str(max(1, self._gop_sec * self._framerate)),
                "-x264-params",
                "scenecut=0",
                "-r",
                str(self._framerate),
                # fragmented MP4，按 frag_duration（微秒）切片
                #   empty_moov         : moov 只放 codec 描述，不放样本表
                #   default_base_moof  : ISO BMFF 推荐（MSE 期望 default-base-is-moof）
                #   separate_moof      : 强制每个分片单独的 moof+mdat（MSE 必需）
                #   frag_keyframe      : 关键帧也强制切片
                # frag_duration=100000us (=100ms) 在延迟和开销之间取平衡
                "-f",
                "mp4",
                "-movflags",
                "+empty_moov+default_base_moof+separate_moof+frag_keyframe",
                "-frag_duration",
                str(self._frag_ms * 1000),  # ffmpeg 这里要微秒
                "-flush_packets",
                "1",
                "pipe:1",
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except FileNotFoundError:
                logger.error(
                    "[{}] ffmpeg 不在 PATH，MSE 镜像不可用：{}", self._log_tag, self._bin
                )
                return False
            except Exception as exc:  # noqa: BLE001
                logger.exception("[{}] spawn ffmpeg 失败: {}", self._log_tag, exc)
                return False

            self._stdout_thread = threading.Thread(
                target=self._read_stdout_loop,
                daemon=True,
                name=f"fmp4-stdout-{self._log_tag}",
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr_loop,
                daemon=True,
                name=f"fmp4-stderr-{self._log_tag}",
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            logger.info(
                "[{}] ffmpeg 启动 pid={} fps≤{} frag={}ms gop={}s",
                self._log_tag,
                self._proc.pid,
                self._framerate,
                self._frag_ms,
                self._gop_sec,
            )
            return True

    def feed(self, raw: bytes) -> None:
        """喂 H.264 annex-B 字节给 ffmpeg。同步、非阻塞（Linux pipe 默认 64KB 缓冲）。"""
        if self._stopped or not raw:
            return
        if self._proc is None:
            if not self._spawn():
                self._stopped = True
                return
        try:
            self._proc.stdin.write(raw)
        except (BrokenPipeError, ValueError, OSError) as exc:
            logger.warning("[{}] ffmpeg stdin 写失败，停止: {}", self._log_tag, exc)
            self._stopped = True

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._kill_proc()
        logger.info("[{}] ffmpeg 已停止", self._log_tag)

    def set_video_filter(self, new_filter: Optional[str]) -> None:
        """动态调整 ``-vf``。值变化会立刻 ``restart`` 子进程，老 ffmpeg 不支持
        运行时改 filter。没变就 no-op。

        典型场景：iOS 设备旋转时切 transpose（``transpose=1,scale=...``）。
        """
        norm = (new_filter or "").strip() or None
        if norm == self._video_filter:
            return
        logger.info(
            "[{}] video_filter 变更 {!r} → {!r}（将重启 ffmpeg）",
            self._log_tag, self._video_filter, norm,
        )
        self._video_filter = norm
        self.restart()

    def restart(self) -> None:
        """杀掉当前 ffmpeg 子进程并重启。

        典型触发场景：设备旋转或分辨率变更，``mp4`` muxer + ``libx264`` 都
        不支持中途改输入分辨率，所以必须重开一遍。新进程会在收到下一组
        SPS+PPS+IDR（旋转后 scrcpy 自动重发）后产出新的 init segment，
        ``_on_init`` 把它再次广播给浏览器，浏览器据此重建 MediaSource。

        线程模型：``feed()`` 与 ``restart()`` 都从 scrcpy decode 线程串行调用，
        不会并发；老的 stdout reader 线程会在老进程 stdout 关闭后自动结束。
        """
        if self._stopped:
            return
        self._kill_proc()
        # 等老 reader 自然退出，避免它把残余字节写进我们马上要清空的 _scan_buf
        old_reader = self._stdout_thread
        if old_reader is not None and old_reader.is_alive():
            old_reader.join(timeout=0.5)
        # 清空所有 box 扫描状态：旧 fragment 残片 / 半截 init 不能混进新流
        self._scan_buf = bytearray()
        self._pending_init = []
        self._pending_seg = []
        self._init_emitted = False
        ok = self._spawn()
        if ok:
            logger.info("[{}] ffmpeg 已重启（旋转/分辨率变更）", self._log_tag)

    def _kill_proc(self) -> None:
        """优雅终止当前 ffmpeg 进程；不动 ``_stopped`` 标志，方便 restart 复用。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ readers
    def _read_stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        first_logged = False
        total_out = 0
        try:
            while not self._stopped:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                total_out += len(chunk)
                if not first_logged:
                    logger.info(
                        "[{}] ffmpeg stdout 首次出 bytes len={} (前 16 字节 hex={})",
                        self._log_tag,
                        len(chunk),
                        chunk[:16].hex(),
                    )
                    first_logged = True
                self._scan_buf.extend(chunk)
                self._drain_boxes()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[{}] stdout reader exit: {}", self._log_tag, exc)
        logger.info(
            "[{}] stdout reader thread end (total out={} bytes)",
            self._log_tag,
            total_out,
        )

    def _drain_boxes(self) -> None:
        """从 ``_scan_buf`` 切出已完整的顶层 box，分组成 init / media 段后回调。"""
        last_end = 0
        for box_type, box_bytes, end_pos in _scan_top_level_boxes(self._scan_buf):
            last_end = end_pos
            if not self._init_emitted:
                # init segment：累 ftyp，等 moov 一起送
                self._pending_init.append(box_bytes)
                if box_type == "moov":
                    init = b"".join(self._pending_init)
                    self._pending_init = []
                    self._init_emitted = True
                    try:
                        self._on_init(init)
                    except Exception:  # noqa: BLE001
                        logger.exception("[{}] on_init callback raised", self._log_tag)
            else:
                # media segment：累 moof，等 mdat 一起送
                # 罕见场景下 ffmpeg 会插一些 'sidx' / 'free' 之类的 box，
                # 也跟当前 segment 一起打包，让浏览器整段 appendBuffer
                self._pending_seg.append(box_bytes)
                if box_type == "mdat":
                    seg = b"".join(self._pending_seg)
                    self._pending_seg = []
                    try:
                        self._on_segment(seg)
                    except Exception:  # noqa: BLE001
                        logger.exception("[{}] on_segment callback raised", self._log_tag)
        if last_end:
            del self._scan_buf[:last_end]

    def _read_stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while not self._stopped:
                line = proc.stderr.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    # ffmpeg loglevel=info 时大部分行只是状态/分析，转成 info 级
                    # 别；带 'error' / 'fatal' 的提级 warning，避免漏告警
                    low = text.lower()
                    if "error" in low or "fatal" in low or "invalid" in low:
                        logger.warning("[{}] ffmpeg: {}", self._log_tag, text)
                    else:
                        logger.info("[{}] ffmpeg: {}", self._log_tag, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[{}] stderr reader exit: {}", self._log_tag, exc)
