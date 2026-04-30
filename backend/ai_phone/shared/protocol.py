"""Agent ↔ Server ↔ Browser 的 WebSocket 消息契约。

所有 WS 消息都是 JSON 对象且带 `type` 字段。以下 TypedDict 只描述"常见字段"，
运行时不做严格校验（避免引入 pydantic 代价），序列化即 dict。

变更这里时，Server / Agent / Browser 三侧要同步。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

# ---------------------------------------------------------------------------
# 消息类型枚举（字符串字面量）
# ---------------------------------------------------------------------------
# Agent → Server
MSG_HELLO = "hello"
MSG_DEVICE_UPDATE = "device_update"
MSG_LOG = "log"
MSG_STEP_DONE = "step_done"
MSG_FRAME = "frame"
# MSE 镜像新通道：fragmented MP4 init segment + media segments
# - VIDEO_INIT 在 mirror 启动 / scrcpy 重启 / 设备旋转时下发，浏览器据此重建
#   MediaSource + SourceBuffer
# - VIDEO_SEGMENT 每个 fmp4 fragment 一条，浏览器按序 appendBuffer 即可播
# 完整替代旧的 MSG_FRAME (live=true) JPEG 路径；旧路径仅在 VLM step before/after
# 截图等"非实时"场景仍然在用
MSG_VIDEO_INIT = "video_init"
MSG_VIDEO_SEGMENT = "video_segment"
# iOS WDA mjpeg 直通：把 WDA mjpeg server 的 JPEG 字节（每帧独立）原样推到
# 浏览器，由浏览器用 <img> / canvas 绘制。**不**经 ffmpeg 转 H.264，因此：
#   - 不用 ``MSE`` / ``SourceBuffer``，无 init segment 概念
#   - 每帧独立，设备旋转 / 分辨率变化天然自适应（canvas.drawImage 按帧绘制）
#   - agent 侧 CPU 占用更低（省掉 libx264 编码）
# 场景：iOS 方案 C 默认走这一路。Android（scrcpy H.264）仍用 MSE。
MSG_MIRROR_JPEG = "mirror_jpeg"
MSG_RUN_DONE = "run_done"
MSG_PONG = "pong"
# 设备启动进度（iOS WDA 编译 / 解锁 / preflight 死锁 / 就绪等），由 Agent
# 主动上报给 Server，Server 转发给订阅该设备的 Browser，让用户不用看终端就
# 知道下一步需要做什么（解锁 iPhone / 等待编译 / ...）。
MSG_DEVICE_STATUS = "device_status"

# Server → Agent
MSG_START_RUN = "start_run"
MSG_STOP_RUN = "stop_run"
MSG_INPUT = "input"
MSG_START_MIRROR = "start_mirror"
MSG_STOP_MIRROR = "stop_mirror"
MSG_PING = "ping"


# ---------------------------------------------------------------------------
# 设备描述
# ---------------------------------------------------------------------------
Platform = Literal["android", "ios"]
DeviceStatus = Literal["idle", "busy", "offline"]


class DeviceInfo(TypedDict, total=False):
    serial: str
    platform: Platform
    name: str
    resolution: str  # "1080x2400"
    status: DeviceStatus


# ---------------------------------------------------------------------------
# Agent → Server
# ---------------------------------------------------------------------------
class HelloMsg(TypedDict):
    type: Literal["hello"]
    agent_id: str
    agent_name: str
    host_os: str
    devices: List[DeviceInfo]


class DeviceUpdateMsg(TypedDict):
    type: Literal["device_update"]
    serial: str
    status: DeviceStatus


LogLevel = Literal[1, 2, 3]  # 对齐 Sonic：1=info, 2=warn, 3=error


class LogMsg(TypedDict, total=False):
    type: Literal["log"]
    run_id: Optional[str]
    level: LogLevel
    title: str
    content: str
    step_index: Optional[int]
    timestamp: float  # epoch seconds


class StepDoneMsg(TypedDict, total=False):
    type: Literal["step_done"]
    run_id: str
    step_index: int
    thought: str
    action: str
    action_type: str
    before_url: Optional[str]
    after_url: Optional[str]
    elapsed_ms: int
    unknown: bool


class FrameMsg(TypedDict, total=False):
    type: Literal["frame"]
    serial: str
    # 两种传法：直接 base64 或先上传后带 url，二选一
    frame_base64: Optional[str]
    frame_url: Optional[str]
    ts: float


class VideoInitMsg(TypedDict, total=False):
    type: Literal["video_init"]
    serial: str
    data: str  # base64(fragmented MP4 init segment: ftyp + moov)
    mime: str  # 完整 MIME，如 'video/mp4; codecs="avc1.42E01E"'
    width: int
    height: int
    ts: float


class VideoSegmentMsg(TypedDict, total=False):
    type: Literal["video_segment"]
    serial: str
    data: str  # base64(media segment: moof + mdat [+ ...])
    ts: float


class MirrorJpegMsg(TypedDict, total=False):
    type: Literal["mirror_jpeg"]
    serial: str
    data: str  # base64(JPEG frame)
    width: int
    height: int
    ts: float


RunResult = Literal["finished", "assert_fail", "error", "cancelled", "fail"]
# 'fail' 是外接引擎（Midscene）专用 —— 它不区分 finished / assert_fail，
# 所有"任务声称失败"统一是 fail。Server 端 _finalize_run 会映射成 status='failed'。

# 引擎选择。'vlm' = ai-phone 主 VLM 主循环（默认）；'midscene' = 外接寄居引擎。
# 详细方案见仓库根 `Midscene执行器接入方案.md`。
RunEngine = Literal["vlm", "midscene"]


class RunDoneMsg(TypedDict, total=False):
    type: Literal["run_done"]
    run_id: str
    result: RunResult
    message: str
    steps: int
    elapsed_ms: int
    token_stats: Dict[str, Any]
    # 仅外接引擎填；ai-phone 自己的 vlm runner 永远不带
    external_report_url: Optional[str]


class PongMsg(TypedDict):
    type: Literal["pong"]
    ts: float


# 设备启动/链路状态。stage 对应 web 上的提示条颜色/图标：
#   - initializing    起手，通常瞬间闪过
#   - compiling       xcodebuild 编译 WDA（蓝色，"请稍候"）
#   - need_unlock     iPhone 锁屏，等用户解锁（黄色，"请解锁并进入主屏幕"）
#   - preflight_deadlock  Xcode preflight 死锁，Agent 即将自动重启 WDA（黄色）
#   - ready           WDA 就绪（绿色，2s 后前端自动收起）
#   - error           终态失败，需要人工（红色）
DeviceStage = Literal[
    "initializing",
    "compiling",
    "need_unlock",
    "preflight_deadlock",
    "ready",
    "error",
]


class DeviceStatusMsg(TypedDict, total=False):
    type: Literal["device_status"]
    serial: str
    stage: DeviceStage
    title: str  # 简短主标题
    hint: str  # 面向用户的操作提示（多行用 \n）
    elapsed_ms: int  # 当前 stage 已累计耗时
    ts: float


# Readiness Gate（v1 第 1 梯队）：把"online 却不能跑"的情况显式抽出来。
# 与 DeviceStatus(WDA 启动进度) 并行存在、互不覆盖——WDA 启动是 iOS 专属的上线动
# 作；readiness 是所有平台稳态下的 "是否可被派单" 的持续探活结果。
#
# not_ready_reason 的 5 个 v1 枚举：
#   - screen_locked            屏幕锁屏（Android keyguard / iOS /wda/locked=true / Harmony 屏幕息屏）
#   - wda_not_ready            iOS WDA 未起 / usbmux 转发未就位
#   - hmdriver2_disconnected   HarmonyOS hmdriver2 socket 不通
#   - adb_offline              Android adb 不通 / 设备 unauthorized
#   - driver_probe_failed      其它 probe 失败（兜底原因，比如 timeout / 异常）
MSG_DEVICE_READINESS = "device_readiness"

NotReadyReason = Literal[
    "screen_locked",
    "wda_not_ready",
    "hmdriver2_disconnected",
    "adb_offline",
    "driver_probe_failed",
]


class DeviceReadinessMsg(TypedDict, total=False):
    type: Literal["device_readiness"]
    serial: str
    platform: str  # android / ios / harmony
    ready: bool
    # ready=true 时 not_ready_reason = None；ready=false 时必有一个 reason
    not_ready_reason: Optional[NotReadyReason]
    # 人类可读提示，例如 "iPhone 锁屏中，请解锁后继续"——可选
    hint: str
    # 连续失败计数（用于诊断；超过阈值才真正降级）
    fail_streak: int
    ts: float


# ---------------------------------------------------------------------------
# Server → Agent
# ---------------------------------------------------------------------------
class StartRunMsg(TypedDict):
    type: Literal["start_run"]
    run_id: str
    device_serial: str
    goal: str


class StopRunMsg(TypedDict):
    type: Literal["stop_run"]
    run_id: str


InputKind = Literal["tap", "swipe", "long_press", "type", "press_home", "press_back"]


class InputMsg(TypedDict, total=False):
    type: Literal["input"]
    serial: str
    kind: InputKind
    params: Dict[str, Any]


class StartMirrorMsg(TypedDict):
    type: Literal["start_mirror"]
    serial: str


class StopMirrorMsg(TypedDict):
    type: Literal["stop_mirror"]
    serial: str


class PingMsg(TypedDict):
    type: Literal["ping"]
    ts: float


# ---------------------------------------------------------------------------
# 联合类型（文档性质，不用于强校验）
# ---------------------------------------------------------------------------
AgentToServer = Union[
    HelloMsg,
    DeviceUpdateMsg,
    LogMsg,
    StepDoneMsg,
    FrameMsg,
    VideoInitMsg,
    VideoSegmentMsg,
    MirrorJpegMsg,
    RunDoneMsg,
    PongMsg,
    DeviceStatusMsg,
    DeviceReadinessMsg,
]

ServerToAgent = Union[
    StartRunMsg,
    StopRunMsg,
    InputMsg,
    StartMirrorMsg,
    StopMirrorMsg,
    PingMsg,
]
