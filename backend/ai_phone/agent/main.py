"""Agent 主进程：扫描本机设备、连 Server WS、响应派发的 run / 镜像。

运行方式：``python -m ai_phone agent --server <ws://...> --token <t> --name <n>``
"""
from __future__ import annotations

# 必须最早调用：macOS 上历史遗留的 numpy 启动 segfault 兜底补丁
#   - 起源：Apple 自带 Python 3.9 + numpy 1.x 在 agent 冷启动时偶发 segfault
#   - 现状：基线已升 Python 3.11，未再复现；补丁改动极小且幂等，稳妥起见保留
#   - 触发未复测前**不要摘**，摘除方案见 _numpy_macos_fix.py 顶部说明
from ai_phone.agent._numpy_macos_fix import ensure_patched_pre_import as _np_fix

_np_fix()

import asyncio
import base64
import os
import platform
import socket
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Set, Tuple, get_args

# 模块顶层的 MIRROR_* 常量从 os.environ 读，需要先把 backend/.env 加载进来。
# pydantic Settings 自己解析 .env 不会注入到 os.environ，所以这里显式 load_dotenv。
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(override=False)
except Exception:  # noqa: BLE001
    pass

from loguru import logger

from ai_phone.agent.drivers import (
    list_all_devices,
    open_driver as _open_driver_by_platform,
)
from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.agent.mirror import (
    FMp4Streamer,
    extract_codec_string_from_moov,
    extract_resolution_from_moov,
    extract_sps_nal as _extract_sps_nal,
)
from ai_phone.agent.runner import build_runner
from ai_phone.agent.runner_bridge import RunnerBridge
from ai_phone.agent.scrcpy_client import (
    EVENT_DISCONNECT as _SCRCPY_EVENT_DISCONNECT,
    EVENT_INIT as _SCRCPY_EVENT_INIT,
    EVENT_RAW_BYTES as _SCRCPY_EVENT_RAW_BYTES,
    Client as ScrcpyClient,
)
from ai_phone.agent.ws_client import AgentWSClient, normalize_server_address, stable_agent_id
from ai_phone.config import get_settings
from ai_phone.shared import protocol as P


_DRIVER_METHODS = set(get_args(P.DriverMethod))


# scrcpy mirror 参数：长边像素 / 帧率上限 / 比特率
# 全部支持环境变量覆盖（AI_PHONE_MIRROR_*），改完 agent 重启生效。
#
# MSE 直传方案：scrcpy → fmp4 (ffmpeg -c:v copy) → 浏览器 <video>，全程 H.264
# 不再二次编码成 JPEG。所以画质完全由 scrcpy 这一段的 MAX_WIDTH/BITRATE 决定，
# JPEG_QUALITY / 静止抑制等老参数已废弃（仍然能在 .env 里见到，是为了向后兼容）。

def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MIRROR_MAX_WIDTH = _env_int("AI_PHONE_MIRROR_MAX_WIDTH", 1280)
"""scrcpy server 缩放后的长边像素。720 模糊，1280 锐利，1920 接近原生。
MSE 路径下没有再编码损失，1280 已经能看清细文字。"""

MIRROR_MAX_FPS = _env_int("AI_PHONE_MIRROR_MAX_FPS", 30)
"""H.264 帧率上限。MSE 不需要二次抽帧，30fps 流畅且 CPU < 5%。"""

MIRROR_BITRATE = _env_int("AI_PHONE_MIRROR_BITRATE", 6_000_000)
"""H.264 编码码率（bit/s）。1280→6M 是甜点；调高到 10M 可以更锐但带宽吃紧。"""

MIRROR_FRAG_MS = _env_int("AI_PHONE_MIRROR_FRAG_MS", 50)
"""fmp4 媒体分片时长（毫秒）。

直接决定 ffmpeg 出货粒度，是端到端延迟最大的可调项之一：
- 50  : 默认。延迟 ~50~120ms，CPU 略升（每段都走一次 muxer + 一次 WS 帧）
- 100 : 老默认。延迟 ~100~250ms，CPU 更省
- 33  : 接近 1 帧 1 段（30fps 下），延迟最低，但 WS 频率会很高 ~30msg/s

不要小于 16ms，否则 ffmpeg muxer 会拒绝。"""

MIRROR_GOP_SEC = _env_int("AI_PHONE_MIRROR_GOP_SEC", 1)
"""IDR 关键帧间隔（秒）。

影响：
- 首帧时间：浏览器需要等到一个 IDR 才能开始解码
- 拖动播放点 / 第二窗口订阅时的恢复延迟
- 码率（IDR 比 P 帧大很多）
推荐：1（默认）；想首帧快可调 0；要省码率调 2。"""


# 进程内驱动缓存：同一设备的 VLM Run 和手动 input 共享一个 driver 实例，
# 避免反复构造连接，也让 ADBKeyBoard / WDA session 等缓存状态在 Run 内 / 间复用。
_driver_cache: Dict[str, BaseDriver] = {}

# serial → platform 映射。设备发现时填，open_driver / mirror 创建时按它路由。
# 没记录的 serial 默认按 Android 处理（adbutils），保留旧行为兼容。
_serial_platform: Dict[str, str] = {}


def _record_serial_platform(infos: List[Any]) -> None:
    """把 device_provider 拿到的设备列表里的 serial → platform 记进缓存，
    给后续 ``_get_or_open_driver`` / mirror 路由复用。
    """
    for info in infos:
        # info 既可能是 DeviceInfo dataclass 也可能是 dict（device_provider 转过）
        try:
            serial = getattr(info, "serial", None) or info["serial"]
            platform = getattr(info, "platform", None) or info["platform"]
        except Exception:  # noqa: BLE001
            continue
        if serial and platform:
            _serial_platform[str(serial)] = str(platform)


def _get_or_open_driver(
    serial: str,
    on_status: Optional[Any] = None,
) -> BaseDriver:
    """打开/取缓存的 driver。

    ``on_status``：仅首次打开 iOS 设备时有意义——会把 WDA 启动过程中的阶段
    （compiling / need_unlock / ready / error）主动推到 web 提示条。
    Android 会静默忽略。
    """
    cached = _driver_cache.get(serial)
    if cached is not None:
        # 已经开过；通知上层"已就绪"，让 web 提示条直接闭合
        if callable(on_status):
            try:
                on_status("ready", "设备就绪", "设备已建立连接，可直接使用。", 0)
            except Exception:  # noqa: BLE001
                pass
        return cached
    platform = _serial_platform.get(serial, "android")
    kwargs: Dict[str, Any] = {}
    if on_status is not None and platform == "ios":
        kwargs["on_status"] = on_status
    drv = _open_driver_by_platform(serial, platform, **kwargs)
    _driver_cache[serial] = drv
    return drv


def _invalidate_dead_ios_driver(serial: str) -> bool:
    """热拔插自愈：若缓存里的 iOS driver 对应的 WDA 已经不可达，
    就把它关掉 + 从 ``_driver_cache`` 摘掉，让下一次 ``_get_or_open_driver``
    重新拉起 xcodebuild + 建 session。

    背景：iOS 拔线时设备端的 WDA 会跟着死，但老 driver 实例还在 cache 里；
    此时走 mirror 重开会复用到这把"死 driver"，所有 WDA HTTP 立刻 502。

    返回 True 表示真的清理了缓存（调用方知道接下来是一次"冷启动"），False
    表示缓存健康或本来就没有缓存。
    """
    drv = _driver_cache.get(serial)
    if drv is None or getattr(drv, "platform", None) != "ios":
        return False
    try:
        from .drivers.ios import _WDA_CLIENT_MAP  # noqa: PLC0415
        cli = _WDA_CLIENT_MAP.get(serial)
        if cli is None:
            raise RuntimeError("WdaClient 未登记在全局 map")
        # /status 不需要 session，通不通最能反映 WDA 进程本身是否还活
        st = cli.status() or {}
        if not st:
            raise RuntimeError("WDA /status 返回空")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "iOS driver 缓存已失效 serial={} reason={} — 丢弃并在下次 open 时重建",
            serial, exc,
        )
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass
        _driver_cache.pop(serial, None)
        return True


def _make_device_status_reporter(
    ws_client: "AgentWSClient",
    loop: asyncio.AbstractEventLoop,
    serial: str,
) -> Any:
    """返回一个线程安全的 callable(stage, title, hint, elapsed_ms)，
    把 WDA 启动进度等设备状态封装成 ``MSG_DEVICE_STATUS`` 发给 Server。

    因为 launcher 的 on_status 是在子线程（_drain_output / locked_watcher）里
    触发的，这里用 ``asyncio.run_coroutine_threadsafe`` 把 ``ws_client.send``
    转回 event loop 执行。发送失败只打 debug，不影响主路径。
    """
    def _reporter(stage: str, title: str, hint: str, elapsed_ms: int) -> None:
        msg = {
            "type": P.MSG_DEVICE_STATUS,
            "serial": serial,
            "stage": stage,
            "title": title or "",
            "hint": hint or "",
            "elapsed_ms": int(elapsed_ms or 0),
            "ts": time.time(),
        }
        try:
            asyncio.run_coroutine_threadsafe(ws_client.send(msg), loop)
        except Exception as exc:  # noqa: BLE001
            logger.debug("device_status 上报失败 serial={}: {}", serial, exc)

    return _reporter


class _RunSupervisor:
    """跟踪当前 Agent 正在跑的所有 run（run_id → task / bridge）。"""

    def __init__(self) -> None:
        self._runs: Dict[str, Dict[str, Any]] = {}

    def is_busy(self, serial: str) -> bool:
        self._drop_done()
        return any(r["serial"] == serial for r in self._runs.values())

    def register(self, run_id: str, serial: str, task: asyncio.Task, bridge: RunnerBridge) -> None:
        self._runs[run_id] = {"serial": serial, "task": task, "bridge": bridge}

        def _cleanup(done: asyncio.Task) -> None:
            self._runs.pop(run_id, None)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except Exception:  # noqa: BLE001
                return
            if exc is not None:
                logger.error("runner task 提前异常 run_id={}: {}", run_id, exc)

        task.add_done_callback(_cleanup)

    def pop(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._runs.pop(run_id, None)

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        self._drop_done()
        return self._runs.get(run_id)

    def all_tasks(self) -> List[asyncio.Task]:
        self._drop_done()
        return [r["task"] for r in self._runs.values()]

    def _drop_done(self) -> None:
        for run_id, entry in list(self._runs.items()):
            task = entry.get("task")
            if isinstance(task, asyncio.Task) and task.done():
                self._runs.pop(run_id, None)


# ------------------------------------------------------------------
# iOS 预热（即插即用模式）
# ------------------------------------------------------------------
# rescan 发现新的 online iOS 设备时，如果 AI_PHONE_IOS_WDA_PRELOAD=true，
# 就起一个 daemon 线程在后台把 WDA 拉起来，不等浏览器点"进入工作台"。
# 启动进度通过 MSG_DEVICE_STATUS 推回 server，首页/工作台都能看到。
#
# 这两个 ref 由 ``run()`` 在 ws 客户端就绪后注入；在它们被设置前所有 preload
# 操作都会 silently no-op，避免模块加载期触发。
_ws_client_ref: Optional["AgentWSClient"] = None
_event_loop_ref: Optional[asyncio.AbstractEventLoop] = None

# 记录当前 rescan 里看到的、已启过预热线程的 iOS serial 集合。
# 设备从 usbmux 列表消失（真拔线）时被摘掉；这样下次重插能再次触发一轮预热。
# 注意：**不**按 status 过滤——lockdown 抽风时设备会被误标 unauthorized，如果
# 摘掉会导致 "agent 重启 + 首次 rescan 碰上 lockdown 抽风 → 永远不 preload →
# WDA 永远起不来" 的死循环。preload worker 失败时会写 fail_ts 做 cooldown，
# 比按 status 过滤更稳。
_preloaded_ios_serials: Set[str] = set()
_preload_lock = threading.Lock()

# serial → 最近一次 preload 失败的 monotonic 时间。cooldown 内不再派发，避免
# 真未授权 / 未信任 的设备每 5s 起一次 xcodebuild。
_preload_fail_ts: Dict[str, float] = {}
_PRELOAD_RETRY_COOLDOWN_SEC = 45.0


def _try_wake_ios(serial: str) -> None:
    """尝试把 iPhone 屏幕点亮。WDA 已就绪才有效。

    策略：先 ``unlock``（WDA 内部会判断锁屏状态，没锁就 no-op），失败降级
    ``press_button home``。两步都失败只打 warning，不抛——唤醒是锦上添花，
    不应该阻塞主流程。
    """
    try:
        from .drivers.ios import _WDA_CLIENT_MAP  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    cli = _WDA_CLIENT_MAP.get(serial)
    if cli is None:
        return
    try:
        cli.unlock()
        logger.info("iOS 唤醒屏幕成功 serial={} via wda.unlock", serial)
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug("wda.unlock 失败 serial={} : {}，降级 press home", serial, exc)
    try:
        cli.press_button("home")
        logger.info("iOS 唤醒屏幕成功 serial={} via press home", serial)
    except Exception as exc:  # noqa: BLE001
        logger.warning("iOS 唤醒屏幕失败 serial={} : {}", serial, exc)


def _preload_ios_worker(serial: str) -> None:
    """后台线程：启动 WDA 并（可选）唤醒屏幕。"""
    settings = get_settings()
    reporter = None
    if _ws_client_ref is not None and _event_loop_ref is not None:
        try:
            reporter = _make_device_status_reporter(
                _ws_client_ref, _event_loop_ref, serial
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("构造 device_status reporter 失败 serial={}: {}", serial, exc)

    # 热拔插：上次的 driver 缓存可能对应已死的 WDA，先健康检查一次
    try:
        _invalidate_dead_ios_driver(serial)
    except Exception:  # noqa: BLE001
        pass

    try:
        if reporter is not None:
            try:
                reporter("initializing", "WDA 预热中",
                         "插入即启动：正在后台建立 WDA 会话，稍等…", 0)
            except Exception:  # noqa: BLE001
                pass
        _get_or_open_driver(serial, on_status=reporter)
        logger.info("iOS 预热成功 serial={}", serial)
        if settings.ios_wake_on_enter:
            _try_wake_ios(serial)
    except Exception as exc:  # noqa: BLE001
        logger.warning("iOS 预热失败 serial={} reason={}", serial, exc)
        with _preload_lock:
            _preloaded_ios_serials.discard(serial)
            _preload_fail_ts[serial] = time.monotonic()
        if reporter is not None:
            try:
                reporter(
                    "error",
                    "WDA 预热失败",
                    f"{int(_PRELOAD_RETRY_COOLDOWN_SEC)}s 后自动重试；"
                    f"若持续失败请看 agent 终端日志: {exc}",
                    0,
                )
            except Exception:  # noqa: BLE001
                pass


def _maybe_preload_ios(infos: List[Any]) -> None:
    """扫到任意 iOS 设备就起线程预热（受 cooldown 保护）。

    为什么不按 status 过滤：
      lockdown StartSession 在 iOS 18/26 + 锁屏 / session 老化时会 PasswordProtected，
      使得 ``list_ios_devices`` 只能把该设备标 unauthorized。如果只认 online 才 preload，
      就会陷入 "rescan 见 unauthorized → 不 preload → WDA 不起 → 下次还是 unauthorized"
      的死循环。正解是**只要 usbmux 还能看到这个 udid**（说明 USB 物理通路没问题），
      就派一次 preload 去拉 WDA——xcodebuild 走的是 instruments，不依赖 lockdown。
      失败则走 cooldown。
    """
    settings = get_settings()
    if not settings.ios_wda_preload:
        return
    if _ws_client_ref is None or _event_loop_ref is None:
        return
    present_ios: Set[str] = set()
    for info in infos:
        try:
            plat = getattr(info, "platform", None)
            status = getattr(info, "status", None)
            serial = getattr(info, "serial", None)
        except Exception:  # noqa: BLE001
            continue
        # offline 就别试了，usbmux 都连不上 xcodebuild 也没戏
        if plat == "ios" and serial and status != "offline":
            present_ios.add(str(serial))
    now = time.monotonic()
    with _preload_lock:
        stale = _preloaded_ios_serials - present_ios
        for s in stale:
            _preloaded_ios_serials.discard(s)
            _preload_fail_ts.pop(s, None)
            logger.debug("iOS 预热记录摘除 serial={}（设备已拔出）", s)
        new_serials = present_ios - _preloaded_ios_serials
        # 已有活 driver：标记一下就够，不用再起线程
        for s in list(new_serials):
            if s in _driver_cache:
                _preloaded_ios_serials.add(s)
                new_serials.discard(s)
        # cooldown 内的 serial 不再派发
        for s in list(new_serials):
            last_fail = _preload_fail_ts.get(s)
            if last_fail is not None and (now - last_fail) < _PRELOAD_RETRY_COOLDOWN_SEC:
                new_serials.discard(s)
        for s in new_serials:
            _preloaded_ios_serials.add(s)
            t = threading.Thread(
                target=_preload_ios_worker,
                args=(s,),
                name=f"ios-preload-{s[-6:]}",
                daemon=True,
            )
            t.start()
            logger.info("iOS 预热线程已派发 serial={}（AI_PHONE_IOS_WDA_PRELOAD=true）", s)


def _device_provider() -> List[Dict[str, Any]]:
    infos = list_all_devices()
    _record_serial_platform(infos)
    _maybe_preload_ios(infos)
    return [d.to_dict() for d in infos]


async def _handle_start_run(
    client: AgentWSClient,
    supervisor: _RunSupervisor,
    msg: Dict[str, Any],
) -> None:
    run_id = str(msg.get("run_id") or "").strip()
    serial = str(msg.get("device_serial") or "").strip()
    goal = str(msg.get("goal") or "").strip()
    try:
        attempt = max(1, int(msg.get("attempt") or 1))
    except Exception:  # noqa: BLE001
        attempt = 1
    # 引擎选择：缺省 'vlm'（与历史行为完全等价）。'midscene' 等外接引擎走不同路径，
    # 比如不开 ai-phone driver、不走 vlm_loop。详见 `Midscene执行器接入方案.md`。
    engine = str(msg.get("engine") or "vlm").strip().lower() or "vlm"
    if not (run_id and serial and goal):
        logger.warning("start_run 参数不全 | run_id={} serial={} goal_len={}", run_id, serial, len(goal))
        return

    if supervisor.is_busy(serial):
        await client.send(
            {
                "type": P.MSG_RUN_DONE,
                "run_id": run_id,
                "serial": serial,
                "attempt": attempt,
                "result": "error",
                "message": "device busy on another run",
                "steps": 0,
                "elapsed_ms": 0,
                "token_stats": {},
            }
        )
        return

    bridge = RunnerBridge(
        run_id=run_id,
        serial=serial,
        ws_send=client.send,
        server_http_base=client.server_http_base or get_settings().server_http_base,
        attempt=attempt,
    )

    async def _run_task() -> None:
        # 外接引擎（如 midscene）不需要 ai-phone 自己的 driver 缓存：它们自带 ADB
        # 客户端，由 bridge 子进程操作设备。这里跳过 _get_or_open_driver 既省时间，
        # 也避免在不需要时把 iOS WDA / scrcpy 这类副作用带起来。
        driver = None
        if engine == "vlm":
            try:
                driver = await asyncio.to_thread(_get_or_open_driver, serial)
            except Exception as exc:  # noqa: BLE001
                logger.exception("打开设备失败 serial={}", serial)
                await client.send(
                    {
                        "type": P.MSG_RUN_DONE,
                        "run_id": run_id,
                        "serial": serial,
                        "attempt": attempt,
                        "result": "error",
                        "message": f"open_driver_failed: {exc}",
                        "steps": 0,
                        "elapsed_ms": 0,
                        "token_stats": {},
                    }
                )
                await bridge.aclose()
                supervisor.pop(run_id)
                return

        try:
            runner = build_runner(
                engine=engine,
                run_id=run_id,
                serial=serial,
                driver=driver,
                goal=goal,
                emit=bridge.emit,
                settings=get_settings(),
            )
        except Exception as exc:  # noqa: BLE001
            await client.send(
                {
                    "type": P.MSG_RUN_DONE,
                    "run_id": run_id,
                    "serial": serial,
                    "attempt": attempt,
                    "result": "error",
                    "message": f"init_runner_failed: {exc}",
                    "steps": 0,
                    "elapsed_ms": 0,
                    "token_stats": {},
                }
            )
            await bridge.aclose()
            supervisor.pop(run_id)
            return

        try:
            await runner.run()
        except asyncio.CancelledError:
            await client.send(
                {
                    "type": P.MSG_RUN_DONE,
                    "run_id": run_id,
                    "serial": serial,
                    "attempt": attempt,
                    "result": "cancelled",
                    "message": "stopped_by_server",
                    "steps": 0,
                    "elapsed_ms": 0,
                    "token_stats": {},
                }
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Runner 异常 run_id={}", run_id)
            await client.send(
                {
                    "type": P.MSG_RUN_DONE,
                    "run_id": run_id,
                    "serial": serial,
                    "attempt": attempt,
                    "result": "error",
                    "message": f"runner_crash: {exc}",
                    "steps": 0,
                    "elapsed_ms": 0,
                    "token_stats": {},
                }
            )
        finally:
            await bridge.aclose()
            supervisor.pop(run_id)

    task = asyncio.create_task(_run_task(), name=f"runner-{run_id}")
    supervisor.register(run_id, serial, task, bridge)
    logger.info("run 已启动 | run_id={} serial={}", run_id, serial)


async def _handle_stop_run(
    client: AgentWSClient,
    supervisor: _RunSupervisor,
    msg: Dict[str, Any],
) -> None:
    run_id = str(msg.get("run_id") or "").strip()
    entry = supervisor.get(run_id)
    if entry is None:
        logger.info("stop_run 未找到 | run_id={}", run_id)
        return
    task: asyncio.Task = entry["task"]
    logger.info("收到 stop_run，取消任务 | run_id={}", run_id)
    task.cancel()


async def _handle_input(
    client: AgentWSClient,
    msg: Dict[str, Any],
    mirror_sup: Optional["_MirrorSupervisor"] = None,
) -> None:
    """浏览器 / 定时任务发来的手动输入，路由到合适的下行通道。

    协议：msg = {type:"input", serial, kind, params}
    kind ∈ {tap, swipe, long_press, type, press_home, press_back}

    路由策略（与 sonic 一致的双通道思路）：
    - **手动操作**（tap / swipe / long_press）：当该 serial 已经有活跃 scrcpy 镜
      像会话时，走 scrcpy 控制信道，端到端延迟 ~30ms（vs adb input 的 200ms+），
      "指哪打哪"的体验。
    - **type / press_home / press_back**：走 driver（adb），因为 type 还要靠
      ADBKeyBoard 处理中文，按键也是 adb keyevent 最稳。
    - 没有镜像会话（如 webhook / 定时任务直接打 input 但没人在看画面）：全部回
      退到 driver（adb），保证功能可用。

    注意 VLM run 自己**不**走这条；runner 直接拿 driver 调 click/swipe（adb），
    与 sonic 脚本行为完全一致。
    """
    serial = str(msg.get("serial") or "").strip()
    kind = str(msg.get("kind") or "").strip()
    params: Dict[str, Any] = msg.get("params") or {}
    if not serial or not kind:
        logger.warning("input 参数不全 | serial={} kind={}", serial, kind)
        return
    try:
        driver = await asyncio.to_thread(_get_or_open_driver, serial)
    except Exception as exc:  # noqa: BLE001
        logger.warning("input 无法打开设备 {}: {}", serial, exc)
        return

    session = mirror_sup.get_session(serial) if mirror_sup is not None else None
    use_scrcpy = (
        kind in ("tap", "swipe", "long_press")
        and session is not None
        and session.is_alive
        and session.control is not None
    )
    logger.info(
        "input | serial={} kind={} params={} route={} (mirror={} alive={} ctrl={})",
        serial,
        kind,
        params,
        "scrcpy" if use_scrcpy else "adb",
        session is not None,
        bool(session and session.is_alive),
        bool(session and session.control),
    )

    try:
        if use_scrcpy:
            # scrcpy fast-path：直接走控制 socket，几乎零延迟
            await _dispatch_input_via_scrcpy(driver, session, kind, params)
            return

        # adb fallback
        if kind == "tap":
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
            await asyncio.to_thread(driver.click, x, y)
        elif kind == "swipe":
            x1 = int(params.get("x1", 0))
            y1 = int(params.get("y1", 0))
            x2 = int(params.get("x2", 0))
            y2 = int(params.get("y2", 0))
            dur = int(params.get("duration_ms", 300))
            await asyncio.to_thread(driver.swipe, x1, y1, x2, y2, dur)
        elif kind == "long_press":
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
            dur = int(params.get("duration_ms", 1000))
            await asyncio.to_thread(driver.long_press, x, y, dur)
        elif kind == "type":
            text = str(params.get("text", ""))
            await asyncio.to_thread(driver.type_text, text)
        elif kind == "press_home":
            await asyncio.to_thread(driver.press_home)
        elif kind == "press_back":
            await asyncio.to_thread(driver.press_back)
        elif kind == "keycode":
            code = int(params.get("code", 0))
            if code <= 0:
                logger.warning("keycode 缺 code 参数 | serial={} params={}", serial, params)
                return
            # scrcpy fast-path：DOWN+UP 一对发出，延迟 ~30ms
            if (
                session is not None
                and session.is_alive
                and session.control is not None
            ):
                try:
                    from .scrcpy_client.const import ACTION_DOWN, ACTION_UP

                    def _send_kc() -> None:
                        session.control.keycode(code, ACTION_DOWN)
                        session.control.keycode(code, ACTION_UP)

                    await asyncio.to_thread(_send_kc)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "scrcpy keycode 失败回退 adb | serial={} code={} err={}",
                        serial,
                        code,
                        exc,
                    )
            await asyncio.to_thread(driver.press_keycode, code)
        else:
            logger.warning("input 未知 kind={} serial={}", kind, serial)
    except Exception as exc:  # noqa: BLE001
        logger.exception("input 执行失败 serial={} kind={}: {}", serial, kind, exc)


async def _handle_driver_command(client: AgentWSClient, msg: Dict[str, Any]) -> None:
    """执行 Server 大脑下发的单次 BaseDriver RPC，并回传 driver_result。"""
    message_id = str(msg.get("message_id") or "")
    run_id = str(msg.get("run_id") or "")
    serial = str(msg.get("serial") or "").strip()
    method = str(msg.get("method") or "").strip()
    params = dict(msg.get("params") or {})
    started = time.monotonic()

    async def _send_result(
        *,
        ok: bool,
        result: Any = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "type": P.MSG_DRIVER_RESULT,
            "message_id": message_id,
            "run_id": run_id,
            "serial": serial,
            "method": method,
            "ok": ok,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if ok:
            payload["result"] = result
        else:
            payload["error"] = error or {
                "category": "device",
                "error_class": "DriverCommandError",
                "message": "driver_command failed",
            }
        await client.send(payload)

    if not message_id or not serial or not method:
        await _send_result(
            ok=False,
            error={
                "category": "device",
                "error_class": "MalformedDriverCommand",
                "message": "driver_command 缺少 message_id / serial / method",
            },
        )
        return

    if method not in _DRIVER_METHODS:
        await _send_result(
            ok=False,
            error={
                "category": "device",
                "error_class": "UnknownDriverMethod",
                "message": f"未知 driver method: {method}",
            },
        )
        return

    try:
        driver = await asyncio.to_thread(_get_or_open_driver, serial)
        fn = getattr(driver, method)
        raw_result = await asyncio.to_thread(
            fn, **_normalize_driver_params(method, params)
        )
        await _send_result(ok=True, result=_serialize_driver_result(method, raw_result))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "driver_command 执行失败 | trace_id={} run_id={} serial={} method={}",
            message_id,
            run_id,
            serial,
            method,
        )
        await _send_result(ok=False, error=_driver_error_payload(exc))


def _normalize_driver_params(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(params)
    if method == "scroll" and isinstance(out.get("center"), list):
        center = out["center"]
        if len(center) == 2:
            out["center"] = (int(center[0]), int(center[1]))
    return out


def _serialize_driver_result(method: str, result: Any) -> Any:
    if isinstance(result, bytes):
        mime = "image/png" if method == "screenshot_png" else "image/jpeg"
        return {
            "encoding": "base64",
            "mime": mime,
            "data": base64.b64encode(result).decode("ascii"),
        }
    if hasattr(result, "to_dict") and callable(result.to_dict):
        return result.to_dict()
    if isinstance(result, tuple):
        return list(result)
    return result


def _driver_error_payload(exc: Exception) -> Dict[str, Any]:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {
        "category": "device",
        "error_class": exc.__class__.__name__,
        "message": str(exc),
        "traceback": "\n".join(tb.strip().splitlines()[-12:]),
    }


async def _dispatch_input_via_scrcpy(
    driver: BaseDriver,
    session: "_MirrorSession",
    kind: str,
    params: Dict[str, Any],
) -> None:
    """tap / swipe / long_press 经 scrcpy 控制信道下发。

    **坐标系坑**：scrcpy server 2.x 严格校验 touch event 携带的 (screen_w,
    screen_h) 必须等于它自己当前 surface 的尺寸（也就是编码后的 H.264 帧尺寸，
    例如 max_size=720 时是 328×720），不匹配就直接 ``Ignore touch event, it
    was generated for a different device size`` 丢弃，**完全没有反应**。

    所以上层传进来的"设备物理像素 (x, y)"（与 sonic / VLM / driver.click 一致
    的坐标系）这里要先按 ``frame / device`` 比例缩放到 frame 坐标系再发包；
    scrcpy server 拿到后自然能映射回真实物理坐标。
    """
    from .scrcpy_client.const import ACTION_DOWN, ACTION_UP

    dw, dh = session.get_device_size(driver)
    if not dw or not dh:
        raise RuntimeError("device size unknown, fallback to adb")
    fres = session.resolution
    if not fres or not fres[0] or not fres[1]:
        # 帧还没出来，回退 adb；不抛异常以免把外层 try 走漏
        raise RuntimeError("scrcpy frame resolution unknown, fallback to adb")
    fw, fh = fres
    sx_scale = fw / dw
    sy_scale = fh / dh
    ctrl = session.control

    def _f(x: int, y: int) -> tuple:
        return (max(0, min(fw - 1, round(x * sx_scale))),
                max(0, min(fh - 1, round(y * sy_scale))))

    logger.info(
        "scrcpy.input | serial={} kind={} dev={}x{} frame={}x{} params={}",
        session.serial, kind, dw, dh, fw, fh, params,
    )

    def _do_tap(x: int, y: int) -> None:
        fx, fy = _f(x, y)
        ctrl.touch_at(fx, fy, fw, fh, action=ACTION_DOWN)
        # 极短的 down→up 间隔；不 sleep 太久避免被识别为长按
        time.sleep(0.02)
        ctrl.touch_at(fx, fy, fw, fh, action=ACTION_UP)

    def _do_long_press(x: int, y: int, dur_ms: int) -> None:
        fx, fy = _f(x, y)
        ctrl.touch_at(fx, fy, fw, fh, action=ACTION_DOWN)
        time.sleep(max(0.0, dur_ms / 1000.0))
        ctrl.touch_at(fx, fy, fw, fh, action=ACTION_UP)

    def _do_swipe(sx: int, sy: int, ex: int, ey: int, dur_ms: int) -> None:
        fsx, fsy = _f(sx, sy)
        fex, fey = _f(ex, ey)
        ctrl.swipe_at(fsx, fsy, fex, fey, fw, fh, duration_ms=dur_ms)

    if kind == "tap":
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        await asyncio.to_thread(_do_tap, x, y)
    elif kind == "long_press":
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        dur = int(params.get("duration_ms", 800))
        await asyncio.to_thread(_do_long_press, x, y, dur)
    elif kind == "swipe":
        x1 = int(params.get("x1", 0))
        y1 = int(params.get("y1", 0))
        x2 = int(params.get("x2", 0))
        y2 = int(params.get("y2", 0))
        dur = int(params.get("duration_ms", 300))
        await asyncio.to_thread(_do_swipe, x1, y1, x2, y2, dur)


class _MirrorSession:
    """单设备 scrcpy → fmp4 → MSE 视频流会话。

    流水线：
      scrcpy 解码线程 ──recv socket──▶ raw H.264 annex-B
                                          │
                                          ▼
                                   FMp4Streamer (ffmpeg -c:v copy)
                                          │
                              ┌───────────┴───────────┐
                              ▼                       ▼
                         on_init(ftyp+moov)      on_segment(moof+mdat)
                              │                       │
                              ▼                       ▼
                       MSG_VIDEO_INIT          MSG_VIDEO_SEGMENT
                              │                       │
                              └─────────┬─────────────┘
                                        ▼
                            asyncio.run_coroutine_threadsafe
                                        │
                                        ▼
                                  ws.send(payload)

    与旧 JPEG 路径相比：
      - 不再 PyAV decode → PIL → JPEG 二次编码（CPU 减半，画质无损）
      - 不再静止帧抑制 / 限频 / 心跳（H.264 流自身在静止时码率自然下降）
      - 浏览器拿到的就是 H.264，渲染由硬件解码完成，无 <img> src 切换闪烁
    """

    def __init__(
        self,
        serial: str,
        ws_client: "AgentWSClient",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.serial = serial
        self._ws = ws_client
        self._loop = loop
        self._scrcpy: Optional[ScrcpyClient] = None
        self._stopped = False
        self._segment_count = 0

        # 设备物理像素尺寸，懒加载（首次手动 tap 时拉一次）。scrcpy 自身的
        # ``resolution`` 是缩放后的帧尺寸（如 328×720），不能拿来当 tap 坐标系。
        # 历史遗留字段，已不再用作缓存（每次 get_device_size 都直接读 driver）；
        # 保留只是为了不破坏 dataclass-like 的属性访问语义。
        self._device_size: Optional[Tuple[int, int]] = None

        # MSE 路径相关
        self._fmp4: Optional[FMp4Streamer] = None
        # mirror 帧分辨率（与 scrcpy 编码后输出一致，例如 720p 设备物理 1080p
        # 时这里是 328×720）。从 fmp4 init segment 的 avc1 box 提取，
        # 用于 tap 坐标系缩放。
        self._mirror_resolution: Optional[Tuple[int, int]] = None
        # 缓存最近一次 init segment payload，方便订阅方刷新页面时让上层重发
        self._last_init_payload: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        try:
            self._scrcpy = ScrcpyClient(
                device=self.serial,
                max_width=MIRROR_MAX_WIDTH,
                max_fps=MIRROR_MAX_FPS,
                bitrate=MIRROR_BITRATE,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrcpy 初始化失败 serial={}: {}", self.serial, exc)
            return

        self._fmp4 = FMp4Streamer(
            on_init=self._on_fmp4_init,
            on_segment=self._on_fmp4_segment,
            framerate=MIRROR_MAX_FPS,
            frag_ms=MIRROR_FRAG_MS,
            gop_sec=MIRROR_GOP_SEC,
            log_tag=f"fmp4:{self.serial}",
        )

        self._scrcpy.add_listener(_SCRCPY_EVENT_INIT, self._on_init)
        self._scrcpy.add_listener(_SCRCPY_EVENT_RAW_BYTES, self._on_raw_bytes)
        self._scrcpy.add_listener(_SCRCPY_EVENT_DISCONNECT, self._on_disconnect)

        try:
            # daemon=True：进程退出时不会卡住
            self._scrcpy.start(threaded=True, daemon_threaded=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("scrcpy 启动失败 serial={}: {}", self.serial, exc)
            self._scrcpy = None
            return

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._fmp4 is not None:
            try:
                self._fmp4.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("fmp4 stop 抛异常 serial={}: {}", self.serial, exc)
            self._fmp4 = None
        if self._scrcpy is not None:
            try:
                self._scrcpy.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("scrcpy stop 抛异常 serial={}: {}", self.serial, exc)
            self._scrcpy = None

    @property
    def control(self):
        """暴露 scrcpy 的控制信道，给手动 input fast-path 用。"""
        return self._scrcpy.control if self._scrcpy is not None else None

    @property
    def resolution(self) -> Optional[Tuple[int, int]]:
        """mirror 帧分辨率，用于手动 tap 坐标系缩放。"""
        if self._mirror_resolution is not None:
            return self._mirror_resolution
        # 兜底：scrcpy 自己 decode 出来的 frame 尺寸（仅在还有人订阅 EVENT_FRAME
        # 时才有值；MSE 路径下我们不订阅，所以基本走不到这条）
        return self._scrcpy.resolution if self._scrcpy is not None else None

    @property
    def is_alive(self) -> bool:
        """scrcpy 视频流是否还能用。stop() 之后或断流后为 False。"""
        return (
            not self._stopped
            and self._scrcpy is not None
            and self._scrcpy.alive
            and self._scrcpy.control_socket is not None
        )

    def get_device_size(self, driver: BaseDriver) -> Tuple[int, int]:
        """返回设备 ``(w, h)`` 逻辑尺寸（adbutils 会随旋转返回当前方向的宽高）。

        **每次都直接 driver.window_size()，不缓存**。
        曾经的实现是懒加载缓存一次，结果设备旋转后缓存值还是旧方向的，下游
        ``_dispatch_input_via_scrcpy`` 用 ``sx_scale = fw / dw_stale`` 算缩放，
        前端按 1080×2400 发坐标、agent 按错误的轴向缩放，手势直接打飞到屏幕
        另一半。adbutils 的 ``window_size`` 走一次 ``wm size``，<10ms，对手动
        tap 完全无感，VLM 几秒一次更不在意。简单粗暴最稳。"""
        try:
            return driver.window_size()
        except Exception as exc:  # noqa: BLE001
            logger.warning("拿不到设备 {} 尺寸：{}", self.serial, exc)
            return (0, 0)

    def invalidate_device_size(self) -> None:
        """保留以兼容外部调用；现在 ``get_device_size`` 已不缓存，此方法是 no-op。"""
        return

    # -------------------------------------------------------------- listeners
    def _on_init(self) -> None:
        logger.info(
            "scrcpy 已就绪 serial={} device_name={}",
            self.serial,
            getattr(self._scrcpy, "device_name", "?"),
        )

    def _on_disconnect(self) -> None:
        logger.info("scrcpy 视频流断开 serial={}", self.serial)
        self._stopped = True
        if self._fmp4 is not None:
            try:
                self._fmp4.stop()
            except Exception:  # noqa: BLE001
                pass
            self._fmp4 = None

    def _on_raw_bytes(self, raw: bytes) -> None:
        """scrcpy 解码线程拿到 socket 数据 → 喂给 ffmpeg fmp4。"""
        if self._stopped or self._fmp4 is None:
            return
        # 设备旋转检测：SPS 里编码了分辨率，旋转后 SPS 字节会变。
        # 一旦检测到新 SPS：重启 ffmpeg（mp4 muxer + libx264 都不支持中途
        # 改分辨率），新的 ffmpeg 会自然产出新的 init segment（含新 moov），
        # 浏览器看到带新 width/height 的 init 后会重建 MediaSource。
        sps = _extract_sps_nal(raw)
        if sps is not None:
            prev = getattr(self, "_last_sps_bytes", None)
            if prev is not None and sps != prev:
                logger.info(
                    "scrcpy 检测到新 SPS（设备旋转/分辨率变更），重启 ffmpeg serial={}",
                    self.serial,
                )
                try:
                    self._fmp4.restart()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fmp4 restart 失败 serial={}: {}", self.serial, exc)
                # 重要：device_size 也要让下一次 input 重新读，否则手动 tap 会按
                # 旋转前的 (w, h) 缩放，整个手势打到屏幕另一半
                self.invalidate_device_size()
            self._last_sps_bytes = sps
        self._fmp4.feed(raw)
        # 首次喂数据 / 每 100KB 累计：
        # - 没有 raw_bytes 进来 → scrcpy 端断流
        # - 有 raw_bytes 进来但 ffmpeg 不出 init → muxer 配置问题
        self._raw_bytes_total = getattr(self, "_raw_bytes_total", 0) + len(raw)
        prev_bucket = getattr(self, "_raw_bytes_bucket", -1)
        cur_bucket = self._raw_bytes_total // (200 * 1024)  # 每 200KB 报一次
        if cur_bucket != prev_bucket:
            self._raw_bytes_bucket = cur_bucket
            logger.info(
                "scrcpy raw_bytes 累计 serial={} total={}KB",
                self.serial,
                self._raw_bytes_total // 1024,
            )

    def _on_fmp4_init(self, init: bytes) -> None:
        """ffmpeg 产出的 init segment（ftyp + moov）。整段送一次，浏览器据此构 SourceBuffer。"""
        if self._stopped:
            return
        res = extract_resolution_from_moov(init)
        codec = extract_codec_string_from_moov(init) or "avc1.42E01E"
        if res:
            self._mirror_resolution = res
        w, h = res or (0, 0)
        payload: Dict[str, Any] = {
            "type": P.MSG_VIDEO_INIT,
            "serial": self.serial,
            "data": base64.b64encode(init).decode("ascii"),
            "mime": f'video/mp4; codecs="{codec}"',
            "width": w,
            "height": h,
            "ts": time.time(),
        }
        self._last_init_payload = payload
        logger.info(
            "MSE init segment 就绪 serial={} mime={} size={}x{} bytes={}",
            self.serial,
            payload["mime"],
            w,
            h,
            len(init),
        )
        self._dispatch(payload)

    def replay_init(self) -> None:
        """重发上一次缓存的 init segment。

        触发场景：浏览器刷新 / 第二个 tab 订阅同一台设备 → 服务端再次给 agent
        发 ``MSG_START_MIRROR``。会话已经在跑，但新订阅者错过了之前那条 init
        消息，<video> 永远拿不到 codec，只能丢后续的 media segment。
        这里把缓存的 init payload 再扔一次，新订阅者就能正常构 SourceBuffer。
        """
        if self._stopped:
            return
        payload = self._last_init_payload
        if payload is None:
            return
        # 时间戳更新一下，避免下游按 ts 去重
        payload = dict(payload)
        payload["ts"] = time.time()
        logger.info("MSE init segment 重广播 serial={}", self.serial)
        self._dispatch(payload)

    def _on_fmp4_segment(self, seg: bytes) -> None:
        """ffmpeg 产出的一个 media segment（moof + mdat）。"""
        if self._stopped:
            return
        self._segment_count += 1
        payload = {
            "type": P.MSG_VIDEO_SEGMENT,
            "serial": self.serial,
            "data": base64.b64encode(seg).decode("ascii"),
            "ts": time.time(),
        }
        self._dispatch(payload)
        # 每 30 段（≈3s @ 10fps 或 ≈1s @ 30fps）打一条节流日志，方便诊断"画面
        # 不动"时是 ffmpeg 没出货 还是 链路下游断了
        if self._segment_count == 1 or self._segment_count % 30 == 0:
            logger.info(
                "MSE segment 累计 serial={} count={} 本段bytes={}",
                self.serial,
                self._segment_count,
                len(seg),
            )

    def _dispatch(self, payload: Dict[str, Any]) -> None:
        """把要发送的 WS 消息从 ffmpeg 读线程调度回 asyncio 主循环。"""
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send(payload), self._loop)
        except Exception as exc:  # noqa: BLE001
            logger.debug("调度 mirror payload 失败 serial={}: {}", self.serial, exc)


class _IosMirrorSession:
    """单设备 iOS 镜像会话：pmd3 截图轮询 → ffmpeg image2pipe → fmp4 → MSE。

    与 Android 的 ``_MirrorSession`` 接口对齐（``start`` / ``stop`` /
    ``replay_init`` / ``is_alive`` / ``control`` / ``resolution`` /
    ``get_device_size``），让 ``_MirrorSupervisor`` 不感知平台、让
    ``_handle_input`` 的 fast-path 判定（``session.control is not None``）
    自然走 fallback（iOS 没有 scrcpy 控制信道，所有 input 走 driver→WDA）。
    """

    def __init__(
        self,
        serial: str,
        ws_client: "AgentWSClient",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.serial = serial
        self._ws = ws_client
        self._loop = loop
        self._stopped = False
        self._segment_count = 0

        self._streamer = None  # type: ignore[assignment]
        self._mirror_resolution: Optional[Tuple[int, int]] = None
        self._last_init_payload: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        # iOS mirror 后端按 settings.ios_mirror_backend 三选一：
        # - mjpeg_passthrough（默认）：WDA mjpeg server → JPEG 直推浏览器
        #   每帧独立，旋转/分辨率天然自适应；浏览器走 <img>/canvas 不用 MSE
        # - wda_mjpeg：WDA mjpeg → ffmpeg H.264 → fmp4 → MSE（保留做兜底）
        # - dvt_screenshot（最老）：pmd3 DVT 截图轮询 → fmp4 → MSE
        try:
            from .mirror import build_ios_streamer  # noqa: PLC0415
            from ai_phone.config import get_settings as _get_s  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.error("iOS mirror 启动失败 serial={}: build_ios_streamer 导入失败 {}", self.serial, exc)
            self._stopped = True
            return

        backend = (_get_s().ios_mirror_backend or "mjpeg_passthrough").strip().lower()

        # mjpeg_passthrough / wda_mjpeg 两种 WDA mjpeg 后端都必须先把 driver 起来：
        # - WDA xcodebuild test 已在跑 → 有 server listen 9100
        # - driver 初始化会 ``create_session`` → WDA mjpeg server 才能 screenshot
        #   否则 `XCUIScreen.mainScreen` 拿不到 active app，mjpeg 直接返 HTTP 502
        # - driver 创建后会把 WdaClient 放入 _WDA_CLIENT_MAP，供 streamer 复用
        #   （绝对不能让 streamer 自己 new WdaClient 建新 session，会顶掉这把）
        if backend in ("wda_mjpeg", "mjpeg_passthrough"):
            # 把 WDA 启动进度（compiling / need_unlock / preflight_deadlock /
            # ready / error）通过 WS 推给浏览器，让用户不用看 agent 终端。
            status_reporter = _make_device_status_reporter(self._ws, self._loop, self.serial)
            # 拔线重插场景：缓存里的 driver 对应的 WDA 很可能已经跟着设备死了，
            # 先做一次健康检查，不通就丢弃缓存，避免后面 mjpeg 全 502
            _invalidate_dead_ios_driver(self.serial)
            try:
                _get_or_open_driver(self.serial, on_status=status_reporter)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "iOS mjpeg mirror 启动前 open_driver 失败 serial={}: {} "
                    "（WDA 没起来 mjpeg 就会 502；建议检查 .env 的 AI_PHONE_WDA_PROJECT_DIR / Xcode 是否起好）",
                    self.serial, exc,
                )
            else:
                # 进入工作台自动唤醒屏幕：Face ID 机型长时间不操作会息屏，
                # 再走 lockdown / mjpeg 都会很别扭；亮一下比让用户手动按电源键省心
                try:
                    if _get_s().ios_wake_on_enter:
                        _try_wake_ios(self.serial)
                except Exception:  # noqa: BLE001
                    pass

        # 拿 WDA 本地端口，用于给 streamer 推 appium settings
        wda_local_port: Optional[int] = None
        try:
            from .drivers.ios import _PORT_ALLOC_MAP  # noqa: PLC0415
            wda_local_port = _PORT_ALLOC_MAP.get(self.serial)
        except Exception:  # noqa: BLE001
            pass

        try:
            self._streamer = build_ios_streamer(
                serial=self.serial,
                on_init=self._on_fmp4_init,
                on_segment=self._on_fmp4_segment,
                on_jpeg=self._on_mirror_jpeg,
                # dvt_screenshot 路径：iOS DVT 单帧 ~350ms，5fps 是 target 上限
                # （再高 ffmpeg 按虚假帧率标 PTS → MSE 延迟堆积）
                # wda_mjpeg / mjpeg_passthrough 路径：build_ios_streamer 会改用
                # settings.wda_mjpeg_fps
                target_fps=min(MIRROR_MAX_FPS, 5),
                frag_ms=MIRROR_FRAG_MS,
                gop_sec=MIRROR_GOP_SEC,
                log_tag=f"ios-fmp4:{self.serial}",
                wda_local_port=wda_local_port,
            )
            self._streamer.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "iOS mirror streamer 启动失败 serial={}：{} "
                "（dvt_screenshot 后端需要 tunneld + DDI；"
                "wda_mjpeg / mjpeg_passthrough 后端需要 WDA 已就绪）",
                self.serial, exc,
            )
            self._stopped = True
            self._streamer = None
            return

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._streamer is not None:
            try:
                self._streamer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._streamer = None

    @property
    def control(self):
        # iOS 没有 scrcpy 控制信道；返回 None 让 _handle_input 走 driver fallback
        return None

    @property
    def resolution(self) -> Optional[Tuple[int, int]]:
        return self._mirror_resolution

    @property
    def is_alive(self) -> bool:
        return (
            not self._stopped
            and self._streamer is not None
            and self._streamer.is_alive
        )

    def get_device_size(self, driver: BaseDriver) -> Tuple[int, int]:
        try:
            return driver.window_size()
        except Exception as exc:  # noqa: BLE001
            logger.warning("拿不到 iOS 设备 {} 尺寸：{}", self.serial, exc)
            return (0, 0)

    def _on_fmp4_init(self, init: bytes) -> None:
        if self._stopped:
            return
        res = extract_resolution_from_moov(init)
        codec = extract_codec_string_from_moov(init) or "avc1.42E01E"
        if res:
            self._mirror_resolution = res
        w, h = res or (0, 0)
        payload: Dict[str, Any] = {
            "type": P.MSG_VIDEO_INIT,
            "serial": self.serial,
            "data": base64.b64encode(init).decode("ascii"),
            "mime": f'video/mp4; codecs="{codec}"',
            "width": w,
            "height": h,
            "ts": time.time(),
        }
        self._last_init_payload = payload
        logger.info(
            "iOS MSE init segment 就绪 serial={} mime={} size={}x{} bytes={}",
            self.serial, payload["mime"], w, h, len(init),
        )
        self._dispatch(payload)

    def _on_fmp4_segment(self, seg: bytes) -> None:
        if self._stopped:
            return
        self._segment_count += 1
        payload = {
            "type": P.MSG_VIDEO_SEGMENT,
            "serial": self.serial,
            "data": base64.b64encode(seg).decode("ascii"),
            "ts": time.time(),
        }
        self._dispatch(payload)
        if self._segment_count == 1 or self._segment_count % 30 == 0:
            logger.info(
                "iOS MSE segment 累计 serial={} count={} 本段bytes={}",
                self.serial, self._segment_count, len(seg),
            )

    def _on_mirror_jpeg(self, jpeg: bytes, w: int, h: int) -> None:
        """mjpeg_passthrough 后端的单帧回调。把 JPEG base64 后走 MSG_MIRROR_JPEG
        广播给浏览器。浏览器 ``<img>`` 每帧独立绘制，方向/分辨率天然自适应。

        尺寸信息（w/h）非严格必要，前端 ``<img>`` 的 ``naturalWidth/Height``
        会在 load 后自己刷新；这里传一份是为了让 server 也能看到日志里画面
        分辨率，方便排障。
        """
        if self._stopped:
            return
        self._segment_count += 1
        if (w, h) != (0, 0):
            # 只在尺寸变化时记 _mirror_resolution，避免每帧都写
            if self._mirror_resolution != (w, h):
                self._mirror_resolution = (w, h)
        payload = {
            "type": P.MSG_MIRROR_JPEG,
            "serial": self.serial,
            "data": base64.b64encode(jpeg).decode("ascii"),
            "width": int(w),
            "height": int(h),
            "ts": time.time(),
        }
        self._dispatch(payload)
        if self._segment_count == 1 or self._segment_count % 60 == 0:
            logger.info(
                "iOS mjpeg passthrough 累计 serial={} count={} "
                "最近一帧 {}×{} bytes={}",
                self.serial, self._segment_count, w, h, len(jpeg),
            )

    def replay_init(self) -> None:
        if self._stopped:
            return
        payload = self._last_init_payload
        if payload is None:
            return
        payload = dict(payload)
        payload["ts"] = time.time()
        logger.info("iOS MSE init segment 重广播 serial={}", self.serial)
        self._dispatch(payload)

    def _dispatch(self, payload: Dict[str, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send(payload), self._loop)
        except Exception as exc:  # noqa: BLE001
            logger.debug("调度 iOS mirror payload 失败 serial={}: {}", self.serial, exc)


class _HarmonyMirrorSession:
    """单设备 HarmonyOS 镜像会话：hmdriver2 截图轮询 → JPEG 直推浏览器。

    架构上和 ``_IosMirrorSession`` 的 ``mjpeg_passthrough`` 分支**完全同形**：
    - 走 ``MSG_MIRROR_JPEG`` 协议，前端 ``useJpegMirror`` 组件复用
    - ``replay_init`` no-op（JPEG 没有 MSE init segment 概念）
    - ``control`` 永远 None → ``_handle_input`` 走 driver.click / swipe fallback
      （鸿蒙上 driver 就是 hmdriver2，HmClient socket 本身就是低延时控制通道）

    与 iOS 的差别：
    - 不涉及 WDA / xcodebuild，无预热阶段；driver 首次创建会下发 uitest agent
      hap（秒级），但不需要 GUI 干预，不用像 iOS 那样推 WDA 启动进度条
    - 无 ``_on_fmp4_*`` 回调（没有 H.264 路径，P3-A 阶段）
    """

    def __init__(
        self,
        serial: str,
        ws_client: "AgentWSClient",
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.serial = serial
        self._ws = ws_client
        self._loop = loop
        self._stopped = False
        self._frame_count = 0

        self._streamer = None  # type: ignore[assignment]
        self._mirror_resolution: Optional[Tuple[int, int]] = None

    def start(self) -> None:
        try:
            from .mirror import build_harmony_streamer  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "harmony mirror 启动失败 serial={}: build_harmony_streamer 导入失败 {} "
                "（通常是没装 harmony extras：pip install -e \"backend[harmony]\"）",
                self.serial, exc,
            )
            self._stopped = True
            return

        # 和 iOS 一样，先确保 driver 就绪——streamer 会复用同一个 hmdriver2.Driver
        # singleton，避免双方抢 hdc 端口转发。driver 创建失败（设备未授权 / uitest
        # 起不来）直接放弃镜像，让浏览器看到 device_status=error 而不是黑屏卡死。
        try:
            driver = _get_or_open_driver(self.serial)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony mirror 启动前 open_driver 失败 serial={}: {}（"
                "请 hdc list targets 看设备是否 Connected；"
                "必要时 hdc kill-server && hdc start-server 重启 hdc daemon）",
                self.serial, exc,
            )
            self._stopped = True
            return

        try:
            self._streamer = build_harmony_streamer(
                serial=self.serial,
                driver=driver,
                on_jpeg=self._on_mirror_jpeg,
                log_tag=f"hm-mirror:{self.serial}",
            )
            self._streamer.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "harmony mirror streamer 启动失败 serial={}: {}",
                self.serial, exc,
            )
            self._stopped = True
            self._streamer = None
            return

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._streamer is not None:
            try:
                self._streamer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._streamer = None

    @property
    def control(self):
        # 鸿蒙没有独立的 scrcpy 控制信道；所有 input 走 driver（hmdriver2）fallback
        return None

    @property
    def resolution(self) -> Optional[Tuple[int, int]]:
        return self._mirror_resolution

    @property
    def is_alive(self) -> bool:
        return (
            not self._stopped
            and self._streamer is not None
            and self._streamer.is_alive
        )

    def get_device_size(self, driver: BaseDriver) -> Tuple[int, int]:
        try:
            return driver.window_size()
        except Exception as exc:  # noqa: BLE001
            logger.warning("拿不到 harmony 设备 {} 尺寸：{}", self.serial, exc)
            return (0, 0)

    def _on_mirror_jpeg(self, jpeg: bytes, w: int, h: int) -> None:
        """JPEG passthrough 单帧回调。格式和 iOS ``_IosMirrorSession._on_mirror_jpeg``
        完全相同，前端 ``useJpegMirror`` 不用区分平台。
        """
        if self._stopped:
            return
        self._frame_count += 1
        if (w, h) != (0, 0) and self._mirror_resolution != (w, h):
            self._mirror_resolution = (w, h)
        payload = {
            "type": P.MSG_MIRROR_JPEG,
            "serial": self.serial,
            "data": base64.b64encode(jpeg).decode("ascii"),
            "width": int(w),
            "height": int(h),
            "ts": time.time(),
        }
        self._dispatch(payload)
        if self._frame_count == 1 or self._frame_count % 60 == 0:
            logger.info(
                "harmony mjpeg 累计 serial={} count={} 最近一帧 {}×{} bytes={}",
                self.serial, self._frame_count, w, h, len(jpeg),
            )

    def replay_init(self) -> None:
        """JPEG 路径无 init segment；每帧独立。本方法是 _MirrorSupervisor 幂等
        start 的一部分，保留空实现和 iOS / Android 会话签名一致。
        """
        return

    def _dispatch(self, payload: Dict[str, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send(payload), self._loop)
        except Exception as exc:  # noqa: BLE001
            logger.debug("调度 harmony mirror payload 失败 serial={}: {}", self.serial, exc)


class _MirrorSupervisor:
    """管理每个 serial 的 mirror 会话；同一 serial 只允许一个会话。

    ``start_mirror`` 幂等；重复 start 会复用现有会话。``stop_mirror`` 立刻关。
    按 ``_serial_platform`` 决定创建 Android 还是 iOS 会话。
    """

    def __init__(self, ws_client: "AgentWSClient") -> None:
        self._ws = ws_client
        self._sessions: Dict[str, Any] = {}  # _MirrorSession | _IosMirrorSession
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        """在 event loop 线程上调一次，绑定主循环；之后 ``_get_loop`` 在任何
        线程里都能安全返回。必须在 ``start()`` 走到线程池前调用。
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            # 退化路径：只有在没人先调 ensure_loop 时才走，会 raise 如果不在
            # event loop 线程里——属于编程错误，尽早暴露
            self._loop = asyncio.get_running_loop()
        return self._loop

    def start(self, serial: str) -> None:
        if not serial:
            return
        existing = self._sessions.get(serial)
        if existing is not None and not getattr(existing, "_stopped", False):
            # 幂等：会话还在跑，但要把缓存的 init segment 重广播一次。
            existing.replay_init()
            return
        platform = _serial_platform.get(serial, "android")
        if platform == "ios":
            session: Any = _IosMirrorSession(serial, self._ws, self._get_loop())
        elif platform == "harmony":
            session = _HarmonyMirrorSession(serial, self._ws, self._get_loop())
        else:
            session = _MirrorSession(serial, self._ws, self._get_loop())
        session.start()
        # 启动失败的 session 直接丢弃，下次 start 会重试，不在 _sessions 里留坏会话
        if getattr(session, "_stopped", False):
            logger.warning(
                "mirror 会话启动失败 serial={} platform={}（详见上文 warning）",
                serial, platform,
            )
            return
        self._sessions[serial] = session
        logger.info("mirror 会话已建立 serial={} platform={}", serial, platform)

    def stop(self, serial: str) -> None:
        session = self._sessions.pop(serial, None)
        if session is not None:
            session.stop()
            logger.info("mirror 会话已关闭 serial={}", serial)

    def stop_all(self) -> None:
        for serial, session in list(self._sessions.items()):
            session.stop()
        self._sessions.clear()

    def get_session(self, serial: str):  # noqa: ANN201
        """供手动 input fast-path 拿 ``session.control`` 用。

        返回类型可以是 ``_MirrorSession``（Android）或 ``_IosMirrorSession``（iOS），
        二者都暴露 ``is_alive`` / ``control`` / ``resolution`` / ``get_device_size``
        四个 API。iOS 的 ``control`` 永远是 None，自然让 ``_handle_input`` 回退到
        driver 路径（IosDriver → WDA HTTP）。
        """
        return self._sessions.get(serial)


async def _handle_start_mirror(
    supervisor: _MirrorSupervisor, msg: Dict[str, Any]
) -> None:
    serial = str(msg.get("serial") or "").strip()
    if not serial:
        return
    # 绑定主 loop 后再 to_thread，防止 session.start 里用 asyncio.get_running_loop
    # 时因为在线程池中而 raise。
    supervisor.ensure_loop()
    # **关键**：iOS 场景下 session.start 里 _get_or_open_driver 会同步等 WDA
    # 就绪（可能 1-3 分钟，甚至 preflight 死锁 + 2 次 respawn 共 ~5 分钟）。
    # 必须放线程池，否则主 event loop 被阻塞 → rescan/heartbeat/其他设备的
    # start_mirror/input 全卡住 → 前端看起来"Android 设备也消失了"。
    await asyncio.to_thread(supervisor.start, serial)


async def _handle_stop_mirror(
    supervisor: _MirrorSupervisor, msg: Dict[str, Any]
) -> None:
    serial = str(msg.get("serial") or "").strip()
    if serial:
        # stop 也可能阻塞（stop_streamer → ffmpeg wait），一并扔线程池
        await asyncio.to_thread(supervisor.stop, serial)


def _normalize_agent_token(raw: Optional[str]) -> str:
    """清理 CLI/env token 两端常见复制粘贴字符。"""

    return str(raw or "").strip().strip("'\"‘’“”")


def run(
    server_ws: Optional[str] = None,
    token: Optional[str] = None,
    name: Optional[str] = None,
) -> None:
    settings = get_settings()
    effective_ws, derived_http_base = normalize_server_address(server_ws or settings.server_ws_url)
    default_http_base = "http://127.0.0.1:8000"
    if server_ws is not None:
        effective_http_base = derived_http_base
    elif settings.server_http_base.rstrip("/") == default_http_base:
        effective_http_base = derived_http_base
    else:
        effective_http_base = settings.server_http_base.rstrip("/")
    effective_token = _normalize_agent_token(token or settings.agent_token)
    effective_name = name or settings.agent_name or socket.gethostname()
    agent_id = stable_agent_id(effective_name)

    logger.info(
        "ai-phone agent starting | name={} id={} ws={} http={} os={}",
        effective_name,
        agent_id,
        effective_ws,
        effective_http_base,
        platform.platform(),
    )

    # 简单探测一次 iOS 支持是否就绪，便于用户快速判断"为什么扫不到 iPhone"
    try:
        import pymobiledevice3 as _pmd3  # noqa: F401, PLC0415

        logger.info("iOS 支持已就绪（pymobiledevice3 已安装）")
    except Exception:  # noqa: BLE001
        logger.info(
            "iOS 支持未启用：pymobiledevice3 未安装。需要 iPhone 请 "
            "`pip install -e \".[ios]\"` 后重启 agent。"
        )

    supervisor = _RunSupervisor()
    client = AgentWSClient(
        ws_url=effective_ws,
        token=effective_token,
        agent_id=agent_id,
        agent_name=effective_name,
        server_http_base=effective_http_base,
        device_provider=_device_provider,
    )
    mirror_sup = _MirrorSupervisor(client)

    async def _start_handler(c: AgentWSClient, msg: Dict[str, Any]) -> None:
        await _handle_start_run(c, supervisor, msg)

    async def _stop_handler(c: AgentWSClient, msg: Dict[str, Any]) -> None:
        await _handle_stop_run(c, supervisor, msg)

    async def _input_handler(c: AgentWSClient, msg: Dict[str, Any]) -> None:
        await _handle_input(c, msg, mirror_sup)

    async def _driver_command_handler(c: AgentWSClient, msg: Dict[str, Any]) -> None:
        await _handle_driver_command(c, msg)

    async def _start_mirror_handler(c: AgentWSClient, msg: Dict[str, Any]) -> None:
        await _handle_start_mirror(mirror_sup, msg)

    async def _stop_mirror_handler(c: AgentWSClient, msg: Dict[str, Any]) -> None:
        await _handle_stop_mirror(mirror_sup, msg)

    client.on(P.MSG_START_RUN, _start_handler)
    client.on(P.MSG_STOP_RUN, _stop_handler)
    client.on(P.MSG_INPUT, _input_handler)
    client.on(P.MSG_DRIVER_COMMAND, _driver_command_handler)
    client.on(P.MSG_START_MIRROR, _start_mirror_handler)
    client.on(P.MSG_STOP_MIRROR, _stop_mirror_handler)

    # 暴露给 _maybe_preload_ios 使用：必须在 ws loop 起来前绑定 ref；
    # event loop 则在 run_forever 里取（在此之前还没创建）
    global _ws_client_ref, _event_loop_ref
    _ws_client_ref = client

    # Readiness Gate（v1 第 1 梯队）：旁路 probe 轮询，上报"online 是否真的可派单"。
    # 纯新增模块，不触碰三端执行流程；关闭开关请置 AI_PHONE_READINESS_ENABLED=false。
    from .health import ReadinessSupervisor  # noqa: PLC0415

    def _readiness_device_lister():
        # _serial_platform 在每次 rescan/hello 之后被 _record_serial_platform 刷新，
        # 直接当作权威快照即可。拷贝一下，避免迭代时被其他线程改。
        return list(_serial_platform.items())

    async def _readiness_send(msg):
        await client.send(msg)

    readiness = ReadinessSupervisor(
        device_lister=_readiness_device_lister,
        send_message=_readiness_send,
    )

    async def _bootstrap() -> None:
        global _event_loop_ref
        _event_loop_ref = asyncio.get_running_loop()
        if get_settings().ios_wda_preload:
            logger.info(
                "iOS 即插即用模式已开启 (AI_PHONE_IOS_WDA_PRELOAD=true)：插上 iPhone 就会后台拉 WDA"
            )
        readiness.start(_event_loop_ref)
        try:
            await client.run_forever()
        finally:
            await readiness.stop()

    try:
        asyncio.run(_bootstrap())
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，退出")
    finally:
        mirror_sup.stop_all()
