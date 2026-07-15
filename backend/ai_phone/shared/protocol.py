"""Agent ↔ Server ↔ Browser 的 WebSocket 消息契约。

所有 WS 消息都是 JSON 对象且带 `type` 字段。以下 TypedDict 只描述"常见字段"，
运行时不做严格校验（避免引入 pydantic 代价），序列化即 dict。

变更这里时，Server / Agent / Browser 三侧要同步。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, NotRequired, Optional, TypedDict, Union

# ---------------------------------------------------------------------------
# 消息类型枚举（字符串字面量）
# ---------------------------------------------------------------------------
# Agent → Server
MSG_HELLO = "hello"
MSG_DEVICE_UPDATE = "device_update"
MSG_LOG = "log"
MSG_STEP_DONE = "step_done"
MSG_FRAME = "frame"
# [DEPRECATED] Server 大脑架构（server_brain）专用：Agent 把 driver_command 的执行
# 结果回给 Server。Distributed Agent Brain 已下沉执行脑到 Agent，不再使用此消息；
# Server 与 Agent 两侧均已移除收发 handler。常量保留仅为兼容 run_commands 历史数据，
# 不要在新链路注册或发送。
MSG_DRIVER_RESULT = "driver_result"
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
# 应用分发安装结果。Agent 只回命令执行结果，不做二次包名校验。
MSG_APP_INSTALL_RESULT = "app_install_result"
# Android VM：Agent 回传能力探查结果与生命周期状态。
MSG_VM_CAPABILITY = "vm_capability"
MSG_VM_STATUS = "vm_status"
# Distributed Agent Brain（M4）：Agent 首跑成功后用执行第一手数据整理的成品轨迹缓存
# 回传 Server；Server 算 cache_key 并 upsert vlm_trajectory_cache_v*（回放与归档下沉
# Agent，Server 只做薄存储）。经 M3 可靠通道上行，断线不丢、重连补发。
MSG_CACHE_ARCHIVE = "cache_archive"
# Distributed Agent Brain（M4）：命中缓存本地回放 / 断言失败时，Agent 通知 Server 把
# 该缓存标 suspect（避免坏缓存反复命中）。mark suspect 的写库实作留 Server（见片2）。
MSG_CACHE_SUSPECT = "cache_suspect"

# Server → Agent
MSG_START_RUN = "start_run"
MSG_STOP_RUN = "stop_run"
MSG_INPUT = "input"
MSG_START_MIRROR = "start_mirror"
MSG_STOP_MIRROR = "stop_mirror"
MSG_PING = "ping"
# Server 通知 Agent 对本机管辖设备执行应用安装。Agent 必须后台执行，不能阻塞 WS 收包。
MSG_APP_INSTALL_START = "app_install_start"
# Android VM：Server 下发能力探查与生命周期控制命令。
MSG_VM_CAPABILITY_PROBE = "vm_capability_probe"
# MSG_VM_START 载荷新增端口字段（Server 全局统一分配 emulator 端口，堵死跨机器 serial 撞号串台）：
#   - assigned_port: int | None —— Server 在全局 5554-5682 端口池里钦定的端口，Agent 本机也空闲时优先用。
#   - exclude_ports: list[int] —— 全局已占端口，Agent 选端口时一并避让（assigned_port 本机被占时的兜底）。
# 旧 Agent 不认这两个字段会忽略（退回本机自选），向后兼容。
MSG_VM_START = "vm_start"
MSG_VM_STOP = "vm_stop"
# 删除虚拟机配置 / 换绑到新 Agent 时，通知旧 Agent 清理远端 AVD（avdmanager delete）。
# Agent 侧需先确保 emulator 已停再删；Agent 离线时该指令丢失，留待后续兜底清理。
MSG_VM_DELETE = "vm_delete"
# 孤儿 AVD 对账：Agent（重）连后上报本机受管 AVD 的 vm_id 清单；Server 比对 DB，
# 对已不存在的 vm_id 回发 MSG_VM_DELETE 清理（复用删除链路，无需单独结果消息）。
MSG_VM_RECONCILE = "vm_reconcile"
# Distributed Agent Brain：Server 把"可下发执行配置"快照下发给 Agent。
# Agent 连接（hello 完成）后由 Server 主动下发一次；Agent 收到后用它覆盖本机
# Settings（仅覆盖下发集字段，连接 / 签名 / 本机路径等不受影响）。配置变更走
# "改 Server 配置 + 重启 → Agent 自动重连重新拉"，无需运行时主动推送。
MSG_AGENT_CONFIG = "agent_config"
MSG_AGENT_CONFIG_REQUEST = "agent_config_request"  # Agent → Server：按需补拉配置（下发漏达补偿）
# [DEPRECATED] Server 大脑架构（server_brain）专用：Server 调远端 BaseDriver 方法的
# RPC。Distributed Agent Brain 已下沉执行脑到 Agent，不再使用此消息；Server 与 Agent
# 两侧均已移除收发 handler。常量保留仅为历史兼容，不要在新链路注册或发送。
# Run 派发统一走 start_run（Agent 本地执行）、停止走 stop_run。
MSG_DRIVER_COMMAND = "driver_command"


# ---------------------------------------------------------------------------
# 设备描述
# ---------------------------------------------------------------------------
Platform = Literal["android", "ios", "harmony"]
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


class VmCapabilityProbeMsg(TypedDict, total=False):
    type: Literal["vm_capability_probe"]
    request_id: str
    vm_id: str
    alias: str
    profile_ref_type: str
    profile_ref_id: str
    profile_id: str
    profile_name: str
    config_version: int
    config_json: Dict[str, Any]
    capability_marks: Dict[str, Any]
    api_level: int
    abi: str
    system_type: str
    system_image: str
    screen_width: int
    screen_height: int
    density: int
    orientation: str


class VmCapabilityMsg(TypedDict, total=False):
    type: Literal["vm_capability"]
    request_id: str
    agent_id: str
    ok: bool
    reason: str
    details: Dict[str, Any]


class VmStartMsg(TypedDict, total=False):
    type: Literal["vm_start"]
    request_id: str
    vm_id: str
    name: str
    alias: str
    profile_ref_type: str
    profile_ref_id: str
    profile_id: str
    profile_name: str
    config_version: int
    config_json: Dict[str, Any]
    capability_marks: Dict[str, Any]
    api_level: int
    abi: str
    system_type: str
    system_image: str
    screen_width: int
    screen_height: int
    density: int
    orientation: str
    ram_mb: int
    cpu_cores: int
    vm_heap_mb: int
    internal_storage_mb: int
    sdcard_mb: int
    gpu_mode: str
    network_speed: str
    network_delay: str
    dns_server: str
    http_proxy: str
    wipe_data: bool
    snapshot_policy: str
    back_camera: str
    front_camera: str
    no_window: bool
    no_audio: bool
    no_boot_anim: bool
    writable_system: bool


class VmStopMsg(TypedDict, total=False):
    type: Literal["vm_stop"]
    request_id: str
    vm_id: str
    adb_serial: str


class VmStatusMsg(TypedDict, total=False):
    type: Literal["vm_status"]
    request_id: str
    vm_id: str
    state: str
    adb_serial: str
    ok: bool
    reason: str
    error: str
    details: Dict[str, Any]


class VmDeleteMsg(TypedDict, total=False):
    type: Literal["vm_delete"]
    request_id: str
    vm_id: str
    adb_serial: str


class VmReconcileMsg(TypedDict, total=False):
    type: Literal["vm_reconcile"]
    agent_id: str
    vm_ids: list[str]  # 全集（兼容旧 Agent）= running_vm_ids ∪ stopped_vm_ids
    running_vm_ids: list[str]  # 本机正在跑的（有 emulator 进程）
    stopped_vm_ids: list[str]  # 本机只有 AVD、没在跑的


LogLevel = Literal[1, 2, 3]  # 对齐 Sonic：1=info, 2=warn, 3=error


class LogMsg(TypedDict, total=False):
    type: Literal["log"]
    run_id: Optional[str]
    attempt: int
    level: LogLevel
    title: str
    content: str
    step_index: Optional[int]
    timestamp: float  # [历史/未填] epoch seconds；落库时间以 ts 为准
    # ts：Agent 端原始事件时间（make_event 的毫秒 epoch）。Server _message_datetime
    # 优先用它落 RunLog.ts，断线补发也不失真——缓存归档的"间隔时间/顺序"保真依赖它。
    ts: float
    # Distributed Agent Brain · 可靠上报：event_id 全局幂等键（uuid hex），seq 为
    # 同 (run_id, attempt) 内单调递增序号，供 Server 去重 + 保序、Agent 重连补发。
    event_id: str
    seq: int


class StepDoneMsg(TypedDict, total=False):
    type: Literal["step_done"]
    run_id: str
    attempt: int
    step_index: int
    thought: str
    action: str
    action_type: str
    before_url: Optional[str]
    after_url: Optional[str]
    elapsed_ms: int
    unknown: bool
    ts: float  # Agent 原始事件时间（毫秒 epoch），缓存归档时序保真用
    event_id: str  # 可靠上报幂等键
    seq: int  # 可靠上报保序序号


class FrameMsg(TypedDict, total=False):
    type: Literal["frame"]
    serial: str
    attempt: int
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


RunResult = Literal["finished", "pass", "assert_fail", "error", "cancelled", "fail"]
# 'pass' 是轨迹缓存回放成功的终态（agent orchestrate 发）；语义等同 'finished'，
# Server 的 status_map / scheduler 同样映射成 success。
# 'fail' 是外接引擎（Midscene）专用 —— 它不区分 finished / assert_fail，
# 所有"任务声称失败"统一是 fail。Server 端 _finalize_run 会映射成 status='failed'。

# 引擎选择。'vlm' = ai-phone 主 VLM 主循环（默认）；'midscene' = 外接寄居引擎。
# 详细方案见仓库根 `Midscene执行器接入方案.md`。
RunEngine = Literal["vlm", "midscene"]


class RunDoneMsg(TypedDict, total=False):
    type: Literal["run_done"]
    run_id: str
    attempt: int
    result: RunResult
    message: str
    steps: int
    elapsed_ms: int
    token_stats: Dict[str, Any]
    # 仅外接引擎填；ai-phone 自己的 vlm runner 永远不带
    external_report_url: Optional[str]
    event_id: str  # 可靠上报幂等键（终态去重已另由 run.finished_at 守卫兜底）
    seq: int  # 可靠上报保序序号


class CacheArchiveMsg(TypedDict, total=False):
    """Agent → Server：首跑成功后回传的成品轨迹缓存（M4）。

    ``archive`` 是与 next **同 schema** 的成品载荷（V1/V2 含 ``trajectory_json``；V3
    扁平 ``actions`` + ``meta``），由 ``server.trajectory_cache.repository`` upsert。
    ``cache_key`` 由 Server 统一计算、不取 Agent 传值。经 M3 可靠通道上行（``event_id``
    / ``seq`` 去重保序）。
    """
    type: Literal["cache_archive"]
    run_id: str
    attempt: int
    archive: Dict[str, Any]
    event_id: str
    seq: int


class CacheSuspectMsg(TypedDict, total=False):
    """Agent → Server：命中缓存回放 / 断言失败 → 请求把该缓存标 suspect（M4）。

    mark suspect 的写库在 Server（``trajectory_cache.v3_service`` 等），Agent 只发信号。
    """
    type: Literal["cache_suspect"]
    run_id: str
    cache_key: str
    cache_mode: str
    reason: str


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


class AppInstallResultMsg(TypedDict, total=False):
    type: Literal["app_install_result"]
    task_id: str
    item_id: str
    serial: str
    success: bool
    reason: str
    message: str


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
# [DEPRECATED · 历史协议] Server 大脑 RPC：driver_command / driver_result
# ---------------------------------------------------------------------------
# 这是 Server Brain 架构（next/server-brain）的历史协议：VLMRunner 跑在 Server
# 进程内，driver 调用经 RemoteDriver 透传成 driver_command / driver_result RPC，
# 由 DriverRpcWaiter 撮合。
#
# Distributed Agent Brain（本分支）已下沉执行脑到 Agent：Server 与 Agent 两侧的
# 收发 handler、RemoteDriver / DriverRpcWaiter / ServerRunnerService 均已移除。
# 下面的消息常量与 TypedDict 仅作历史/兼容保留（不注册任何 handler、不应在新链路
# 发送）。Run 派发统一走 start_run（Agent 本地执行）、停止走 stop_run。
#
# 历史语义备忘（仅供阅读旧数据 / 旧分支时参考）：
# - message_id 兼作 trace_id；method 限 DriverMethod 白名单。
# - params 为 BaseDriver 方法参数 dict（scroll.center 用 [cx, cy] 数组）。
# - result：截图返回 base64 信封；标量/结构直接 JSON；无返回为 None。
# - deadline_ms：Server 软超时。
# - 不引入背压 / 拒绝策略：见 6.10.6 的明确决策。

# 仅允许 BaseDriver 上已声明的方法名。新增 BaseDriver 方法时同步本表。
DriverMethod = Literal[
    # Run 前准备
    "prepare_for_run",
    # 屏幕信息
    "window_size",
    "rotation",
    # 截图
    "screenshot_png",
    "screenshot_jpeg",
    # Agent 近端稳定检测（vlm_phash / cache_phash / v3_compare），减少多图 RPC 往返。
    "wait_stable_screenshot_jpeg",
    # 触控
    "click",
    "double_click",
    "long_press",
    "swipe",
    # 输入 & 按键
    "type_text",
    "press_home",
    "press_back",
    "press_keycode",
    # 应用
    "list_third_party_packages",
    "list_all_packages",
    "list_installed_apps",
    "activate_app",
    "terminate_app",
    "current_app",
    # 基础信息
    "device_info",
    # 派生（BaseDriver 默认基于 swipe，但走 Agent 本地一次比 Server 多次跨进程便宜）
    "scroll",
]

# v2 PoC Web 错误归因 UI 的四个一级桶。详见 8.3 / 14 章决策。
# - model         : VLM / 模型层错误（超时、配额、拒绝、内容审核）
# - device        : 真机层错误（ADB offline / WDA 崩溃 / unknown method / 元素找不到）
# - network       : 跨进程链路错误（RPC 超时、WS 断开、握手失败）
# - agent_offline : Run 启动时所属 Agent 已离线 / 跑到一半 Agent 掉线
DriverErrorCategory = Literal["model", "device", "network", "agent_offline"]


class DriverErrorPayload(TypedDict, total=False):
    """driver_result.error 的结构化负载。

    不要只回 ok=false——错误类名 + 消息 + 关键栈片段是排障刚需。
    """

    category: DriverErrorCategory
    error_class: str  # 异常类名，如 'AdbError' / 'WDAStaleSession' / 'TimeoutError'
    message: str  # 异常消息文本
    traceback: str  # 关键栈片段（最后 N 行即可，不要整栈）


class WakePolicyPayload(TypedDict, total=False):
    wake_swipe: bool


class DriverCommandMsg(TypedDict, total=False):
    """Server → Agent：远端 BaseDriver 方法调用。"""

    type: Literal["driver_command"]
    # 兼作 trace_id；Server / Agent / DB(run_commands.message_id / run_logs.trace_id)
    # 三处用同一个 id 串日志。建议生成方式：``uuid.uuid4().hex[:16]``。
    message_id: str
    run_id: str  # 命令所属 Run（历史字段：曾用于 server_brain 按 run_id 批量取消 RPC）
    serial: str  # 目标设备
    method: DriverMethod  # 必须在白名单内
    params: Dict[str, Any]  # BaseDriver 方法参数 dict；空 dict 表示无参
    deadline_ms: int  # 软超时（毫秒）；建议截图 10000、动作 5000、应用类 15000


class DriverResultMsg(TypedDict, total=False):
    """Agent → Server：driver_command 的执行结果。"""

    type: Literal["driver_result"]
    message_id: str  # 与请求侧 driver_command.message_id 完全一致
    run_id: str  # 与请求侧一致；冗余字段方便日志单独看
    serial: str  # 与请求侧一致；冗余字段方便日志单独看
    method: DriverMethod  # 与请求侧一致；方便 Server 侧错误日志 / RunCommand 对账
    ok: bool
    # 仅当 ok=True 时填；类型见上方 docstring 关于 result 的约定
    result: Any
    # 仅当 ok=False 时填
    error: DriverErrorPayload
    # Agent 侧实际耗时（不含网络），单位 ms；Server 侧统计 rpc_elapsed_ms 用
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Server → Agent
# ---------------------------------------------------------------------------
class CacheSnapshot(TypedDict, total=False):
    """命中缓存随 start_run 一次性下发给 Agent 的回放载荷（M4）。

    一次只下发命中的那一条；未命中不带本字段。坐标已在 actions 里，数据本身够回放；
    Agent run 前直接从 state_landmarks / actions 的 ephemeral_meta 取证据图 URL 预取
    （见 agent orchestrate._prefetch_artifacts），不另列 manifest（trajectory 自取）。
    """
    cache_mode: str            # "v1" | "v2" | "v3"
    schema_version: int
    cache_key: str
    actions: list              # 回放动作序列
    state_landmarks: list      # V2/V3 路标（含证据图 image_url + sha256/phash）；V1 为空
    source_completion: Dict[str, Any]
    meta: Dict[str, Any]       # V3 plan meta 等
    source_vlm_backend: str    # 录制时主 VLM backend（V3 回放 locator/rescue 选模型用）


class StartRunMsg(TypedDict, total=False):
    type: Literal["start_run"]
    run_id: str
    device_serial: str
    goal: str
    # 本 Run 实际注入给 Agent 的功能地图上下文；camelCase 保留给旧客户端兼容。
    function_map_context: NotRequired[str]
    functionMapContext: NotRequired[str]
    attempt: NotRequired[int]
    engine: NotRequired[str]
    wake_policy: NotRequired[WakePolicyPayload]
    # M4：命中缓存时随 start_run 下发的回放快照（只下发命中那条）；未命中不带。
    cache_snapshot: NotRequired[CacheSnapshot]
    # M4 片3b：本 run 的 effective_cache_mode（off/v1/v2/v3）。Agent 首跑（未命中）据此
    # 决定成功后是否归档成品缓存并回传；命中回放看 cache_snapshot，不看本字段。
    cache_mode: NotRequired[str]


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


class AppInstallStartMsg(TypedDict, total=False):
    type: Literal["app_install_start"]
    task_id: str
    item_id: str
    serial: str
    platform: str
    package_url: str
    filename: str
    timeout_sec: int


class AgentConfigMsg(TypedDict, total=False):
    """Server → Agent：执行配置下发包（配置集中分发，全局一份，非 per-run 快照）。

    ``config`` 是 ``ai_phone.config.build_downlink_config()`` 的结果（仅含下发集
    字段，不含连接 / 签名 / 本机路径 / Server 敏感配置）。Agent 收到后用
    ``set_runtime_override(config)`` 覆盖本机 Settings。配置变更走"改 Server 配置
    + 重启 → Agent 重连重新下发"，不逐 Run 携带、不持久化。
    """

    type: Literal["agent_config"]
    config: Dict[str, Any]


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
    AppInstallResultMsg,
    VmCapabilityMsg,
    VmStatusMsg,
    VmReconcileMsg,  # 孤儿 AVD 对账：Agent 上报本机受管 AVD 清单
    DeviceReadinessMsg,
    DriverResultMsg,  # next/server-brain 新增
]

ServerToAgent = Union[
    StartRunMsg,
    StopRunMsg,
    InputMsg,
    StartMirrorMsg,
    StopMirrorMsg,
    PingMsg,
    AppInstallStartMsg,
    VmCapabilityProbeMsg,
    VmStartMsg,
    VmStopMsg,
    VmDeleteMsg,  # 删除 / 换绑清理远端 AVD
    AgentConfigMsg,  # Distributed Agent Brain：配置下发
    DriverCommandMsg,  # server_brain 历史（已 deprecated）
]
