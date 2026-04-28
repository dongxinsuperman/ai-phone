from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """统一配置，环境变量优先，.env 兜底。

    所有变量名以 `AI_PHONE_` 前缀暴露。约定：
    - Server / Agent 同一套 Settings，各自只读自己需要的字段。
    - 不在代码里硬编码路径；本地 / 生产都靠 env 注入差异。
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_PHONE_",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 通用 ---
    env: str = Field(default="local", description="部署环境标签（local/prod/...）")
    log_level: str = Field(default="INFO")

    # --- Server 侧 ---
    db_url: str = Field(
        default="postgresql+asyncpg://aiphone:aiphone@127.0.0.1:5432/aiphone",
        description="SQLAlchemy async 连接串（asyncpg 驱动）",
    )
    storage_dir: Path = Field(
        default=Path("./data"),
        description="截图 / 文件落盘根目录",
    )
    server_host: str = Field(default="0.0.0.0")
    server_port: int = Field(default=8000)
    cors_origins: List[str] = Field(
        default=["http://127.0.0.1:5180", "http://localhost:5180"],
        description="前端 dev / 生产域名白名单",
    )

    # --- Agent 侧 ---
    server_ws_url: str = Field(
        default="ws://127.0.0.1:8000/ws/agent",
        description="Agent 连接 Server 的 WS 地址",
    )
    server_http_base: str = Field(
        default="http://127.0.0.1:8000",
        description="Agent 上传文件 / 调 REST 的 HTTP 基址",
    )
    agent_name: Optional[str] = Field(
        default=None,
        description="Agent 展示名，未设则取 hostname",
    )

    # --- 共享鉴权 ---
    agent_token: str = Field(default="dev", description="Agent <-> Server 鉴权 token")
    # 第 2 梯队内部投递/取消接口复用同一把 token（避免新增鉴权模型）。浏览器
    # 的队列总览页从环境变量读到 VITE_AI_PHONE_SUBMISSION_TOKEN 后带到
    # Authorization: Bearer 头里。对外 API（第 3 梯队）暂不走这条，走独立 token。
    submission_internal_token: Optional[str] = Field(
        default=None,
        description="内部 submissions 接口 Bearer token；为空则 fall back 到 agent_token",
    )
    # 第 3 梯队用：Kafka 等异步广播 backend。v1 先留 "stdout"，真正上线前改 kafka
    broadcast_backend: str = Field(
        default="stdout",
        description="Submission 终态广播 backend：stdout / kafka / null",
    )
    # Kafka 占位配置：broker 地址 / topic / ACL。v1 未接入，KafkaPublisher 当前
    # 是 mock 打日志，真接入时这些字段直接透传到 aiokafka.AIOKafkaProducer。
    kafka_brokers: str = Field(
        default="",
        description="Kafka bootstrap servers（如 'kafka-1:9092,kafka-2:9092'），未接入前留空",
    )
    kafka_topic: str = Field(
        default="ai-phone.submission.result",
        description="Submission 终态广播 topic；v1 固定",
    )
    kafka_sasl_username: str = Field(default="", description="SASL/PLAIN 用户名，可空")
    kafka_sasl_password: str = Field(default="", description="SASL/PLAIN 密码，可空")
    # 对外可查窗口：terminal 后多少天内能从 /api/submissions/<id> 查到结果 +
    # HTML 报告可访问。超过后 submission 被标记 external_expired，对外接口 404。
    # 真正的数据/文件清理留到后期单独任务做，这里只影响对外可见性。
    submission_external_retention_days: int = Field(
        default=15,
        ge=1,
        le=365,
        description="对外 API 可查窗口（天）；超过后返回 404 expired",
    )

    # --- 大盘 Analytics ---
    # 大盘一切按"本地日历日"切片；DB 里时间戳都是 UTC，需要一个时区把日期范围还原回 UTC。
    # 默认中国大陆上海时区；海外部署可以改成 America/Los_Angeles 等。
    analytics_timezone: str = Field(
        default="Asia/Shanghai",
        description="大盘单日切片用的 IANA 时区名（date=YYYY-MM-DD 按该时区落到 UTC 区间）",
    )
    # AI 分析可查日范围上限：今天往前推几天（含今天）可以手动触发；超过不允许。
    # 用户明确要求"超过单日不允许调 AI"→ 这里保留单日为硬下限；再给一个安全边界，
    # 避免查询"未来日期"或"远古日期"浪费 token。
    analytics_ai_max_age_days: int = Field(
        default=3,
        ge=1,
        le=30,
        description="AI 分析最多允许往前查的天数（单日切片，默认只允许今天 / 昨天 / 前天）",
    )
    # 大盘"展示开关"：后端聚合逻辑照常跑，只是给前端吐一个 display 标志，
    # 让前端决定 Token / 稳定性两块整卡片要不要渲染。
    # 背景：对外部署早期稳定性差、token 花销大，贸然展示容易引发误判；这里给
    # 运维一把"展示开关"，不改任何计算逻辑，随时可以打开恢复完整大盘。
    analytics_show_token: bool = Field(
        default=True,
        description="大盘是否展示 Token 板块（仅影响前端渲染，后端仍会计算）",
    )
    analytics_show_stability: bool = Field(
        default=True,
        description="大盘是否展示稳定性板块（仅影响前端渲染，后端仍会计算）",
    )

    # --- VLM ---
    # 主决策走 Responses API（/responses）：服务端维护对话历史，配合显式缓存
    # 长任务也能把 prompt 前缀复用起来，避免 Chat API 滑窗把缓存打穿。
    vlm_api_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3/responses",
        description="主决策端点。默认方舟 Responses API（/responses）",
    )
    # 包名匹配这种"一次性纯文本、无上下文复用"场景仍走 Chat API；
    # 用它兜 Responses 端点不支持的单次调用，顺便避免污染会话 id。
    vlm_chat_api_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        description="包名匹配等单次纯文本调用走的 Chat API 端点",
    )
    vlm_api_key: str = Field(default="", description="VLM 服务 API key")
    vlm_model: str = Field(default="doubao-seed-1-6-vision-250815")
    # Responses API 会话分段阈值：上一轮 prompt_tokens ≥ 此值时，下一轮请求前
    # 自动重置 previous_response_id（方舟视觉模型 ≤32K 一档、>32K 二档 ×2 计费）。
    # 30000 = 32000(一档上限) - 2000(单步增量 buffer)；<=0 关闭分段（纯 Cache 行为）。
    vlm_session_reset_prompt_threshold: int = Field(
        default=30000,
        description="上一轮 prompt ≥ 该 tokens 触发分段重置 previous_response_id；<=0 禁用",
    )

    # --- 辅助系统模型（Assistant）---
    # 主 VLM 走 vision 大模型负责"看图决策"，但项目里还有几类**非主决策**的辅助 LLM
    # 调用：①起跑线包名匹配 ②通道判定（结构化 vs 自由对话） ③审判（结构化通道
    # 防偏移）⑥断言系统（finished 终局复核）。这些任务都不需要 vision 大模型
    # 的能力，再用 vlm_model 既贵又慢，且断言系统会出现"主 VLM 自验主 VLM"的
    # 左手验右手问题。
    #
    # 设计：所有非主决策的 LLM 辅助调用统一走 1.6 通用版（doubao-seed-1-6-250615），
    # 该模型同样支持图像输入，断言系统看图能力不丢；包名匹配 / 通道判定 / 审判
    # 这些纯文本任务则不传图，享受文本档计费 + 更快推理。
    #
    # 卡死检测、瞬态 UI 检测/接管 是本地纯算法（pHash + 计数器），不调 LLM，
    # 不在本配置块的辖区。
    assistant_model: str = Field(
        default="doubao-seed-1-6-250615",
        description=(
            "辅助系统统一模型：起跑线包名匹配 / 通道判定 / 审判 / 断言系统都走它。"
            "默认 1.6 通用版（同时支持文本 + 图像，但比 vision 专版便宜快一档）"
        ),
    )
    assistant_api_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        description="辅助系统端点；默认方舟 Chat Completions（同时跑得动文本和图像）",
    )
    assistant_api_key: str = Field(
        default="",
        description="辅助系统 API key；留空时回退使用 vlm_api_key（多数情况下两者同一个 key）",
    )
    # 1.6 通用版是混合推理模型，通过 ``thinking`` 字段切两种模式：
    #   - disabled：直接给答案，~1s 出结果——适合字符串匹配 / 标签分类等"一眼出"
    #   - enabled：内部先生成 chain-of-thought 再下结论，~5-15s——适合视觉细察 /
    #     多约束验证 / 终局裁决这类容错率低的判断密集任务
    # 关闭 thinking 一刀切会让审判和断言"扫一眼就回答"，导致进度条 1/3 填充这种
    # 边界视觉判断频繁错判。这两个系统在一个 Run 里各自只跑 1-2 次，多花 5-10s
    # 换一次准确裁决完全划算。包名匹配 / 通道判定 / 子步骤拆解 / 主 VLM 因为是
    # 高频或轻任务，保持 disabled 跑得动 + 跑得快。
    #
    # 历史包袱：之前用过 reasoning_effort 参数，那是 OpenAI o1/GPT-5 风格 API，
    # 方舟会静默吞掉不报错也不生效——已废弃删除。
    assistant_thinking_judge: bool = Field(
        default=True,
        description=(
            "审判系统是否开启 thinking。审判要识别 case 偏移并给 OK/ALLOW/KILL 决策，"
            "开思考能显著降低错判率。env: AI_PHONE_ASSISTANT_THINKING_JUDGE"
        ),
    )
    assistant_thinking_assertion: bool = Field(
        default=True,
        description=(
            "断言系统是否开启 thinking。断言要看截图 + 思考链 + 预期文本做终局裁决，"
            "开思考能识别细微视觉证据。env: AI_PHONE_ASSISTANT_THINKING_ASSERTION"
        ),
    )

    # --- VLM Agent · 瞬态 UI 检测（动态判断系统） ---
    # 视频播放工具栏 / Toast / 半透明菜单这类"自动隐藏"的瞬态控件，单帧策略下
    # VLM 看到 → 推理 → 执行的 6s+ 链路一定追不上。系统层 snapshot-replay 机制
    # 由本配置在 Run 启动期一次性决定是否挂上：
    # - transient_ui_enabled = false：彻底关，detect_transient_ui 不会被调用
    # - transient_ui_enabled = true 且 goal 命中 transient_ui_keywords 任一关键词：
    #     本次 Run 启用瞬态 UI 检测/接管
    # - transient_ui_enabled = true 但 goal 未命中：本次 Run 禁用（高成本能力按需启用）
    transient_ui_enabled: bool = Field(
        default=False,
        description=(
            "瞬态 UI 检测/接管总开关。env: AI_PHONE_TRANSIENT_UI_ENABLED。"
            "默认关闭，仅在 goal 命中关键词白名单时启用，避免对普通用例引入额外计算"
        ),
    )
    transient_ui_keywords: str = Field(
        default="视频,播放,倍速,直播,video,player,play,toast",
        description=(
            "瞬态 UI 白名单关键词（CSV，goal 含任一关键词且总开关开启时本次 Run 才启用）。"
            "env: AI_PHONE_TRANSIENT_UI_KEYWORDS"
        ),
    )
    # detector 抓 late 帧的延迟（毫秒）。需要大于工具栏典型寿命，否则 late 帧时
    # 工具栏还在，三段判定的"消失率"达不到阈值。当前默认 6000ms 来自洋葱学园
    # ~5s 寿命的实测；做对照实验或适配工具栏寿命更长的 App 时通过 env 调整。
    transient_ui_late_delay_ms: int = Field(
        default=6000,
        description=(
            "瞬态 UI detector 抓 late 帧的等待时长（毫秒）。"
            "env: AI_PHONE_TRANSIENT_UI_LATE_DELAY_MS"
        ),
    )

    # --- iOS WDA（Xcode/XCTest 启动链） ---
    # 留空表示禁用 agent 自动 xcodebuild test；agent 会跳过 WDA 启动，
    # 假设用户已手动在 Xcode 里 Cmd+U 起好 WDA（过渡态）。
    wda_project_dir: Optional[Path] = Field(
        default=None,
        description=(
            "WebDriverAgent.xcodeproj 所在目录（绝对路径）。"
            "项目已 vendored 在 ai-phone/third_party/WebDriverAgent，"
            "例：/Users/<本机用户>/<仓库 clone 位置>/ai-phone/third_party/WebDriverAgent。"
            "留空则禁用 agent 自动 xcodebuild test，需用户手动在 Xcode 里 Cmd+U 起 WDA。"
            "env: AI_PHONE_WDA_PROJECT_DIR"
        ),
    )
    wda_scheme: str = Field(
        default="WebDriverAgentRunner-nodebug",
        description=(
            "xcodebuild -scheme 名。Appium WebDriverAgent 工程有 WebDriverAgentRunner（带 debug 诊断）"
            "和 WebDriverAgentRunner-nodebug（去掉 GPU debug / logging 插桩，agent 场景推荐）"
        ),
    )
    wda_local_port: int = Field(
        default=8100,
        description="agent 侧暴露 WDA 的本地端口；多设备时起点，后续设备递增",
    )
    wda_startup_timeout_s: float = Field(
        default=300.0,
        description="从触发 xcodebuild test 到 /status ready 的最大等待时间（首次编译慢）",
    )
    wda_self_check: bool = Field(
        default=True,
        description="WDA 就绪后是否做三层自检（/status /session /window-size），关掉可以加速",
    )
    # --- iOS WDA 签名信息：xcodebuild 命令行覆盖 .pbxproj，让两台 Mac 共用同一份工程文件 ---
    # 背景：免费 Apple ID 不同账号 → Team ID 不同；同 Bundle Id 每年限签 10 个，
    # 多机协作建议各自起独立 Bundle Id。把这两个值放 .env 后 .pbxproj 不再"个人化"，
    # git 上看到的工程文件可以两机共用，每台 Mac 自己 .env 各写各的。
    wda_bundle_id: Optional[str] = Field(
        default=None,
        description=(
            "WDA 的 Product Bundle Identifier，xcodebuild 命令行覆盖 .pbxproj 用。"
            "建议 com.<本机用户>.wda 这种唯一值（避免免费 Apple ID 同 Bundle Id 撞 10 个/年配额）。"
            "留空则使用 .pbxproj 里的默认值。env: AI_PHONE_WDA_BUNDLE_ID"
        ),
    )
    wda_team_id: Optional[str] = Field(
        default=None,
        description=(
            "Apple Developer Team ID（10 字符大写，在 https://developer.apple.com/account 查），"
            "xcodebuild 命令行覆盖 .pbxproj 里的 DEVELOPMENT_TEAM。"
            "留空则使用 .pbxproj 里的默认值。env: AI_PHONE_WDA_TEAM_ID"
        ),
    )

    # --- iOS 镜像后端切换 ---
    # 三选一（env：AI_PHONE_IOS_MIRROR_BACKEND）：
    #
    # - ``mjpeg_passthrough``（**默认**，Sonic 方案）：
    #     WDA mjpeg server (device:9100) → usbmux → 切 JPEG → 原样推浏览器
    #     → 浏览器 <img> / canvas 绘制。**每帧独立**，设备旋转 / 分辨率变化
    #     天然自适应（前端重设 img.src 就是新尺寸新方向，零管理）。
    #     不经 ffmpeg，agent CPU 最低、延迟最小。
    #     iOS 17+ 业界主流路径，Sonic / Appium inspector 都是这一路。
    #
    # - ``wda_mjpeg``（备选）：
    #     同上拉 mjpeg，但再过 ffmpeg 编 H.264 → fmp4 → MSE。保留这一路是为了
    #     在 mjpeg_passthrough 出问题时能回退到 MSE 路径。代价：H.264 init segment
    #     定死分辨率，设备旋转时需要重建 init + WDA canvas，整链路脆弱。
    #
    # - ``dvt_screenshot``（最老的 PNG 轮询方案）：
    #     pmd3 DVT Screenshot.get_screenshot()（~350ms/张）→ fmp4 → MSE。
    #     帧率低（~2-3fps）、iPhone 发烫。只在 WDA 装不上的环境下作保底。
    ios_mirror_backend: str = Field(
        default="mjpeg_passthrough",
        description=(
            "iOS 镜像后端："
            "mjpeg_passthrough（默认，WDA mjpeg 直通）| "
            "wda_mjpeg（WDA mjpeg → H.264/MSE）| "
            "dvt_screenshot（PNG 轮询兜底）"
        ),
    )
    # WDA 在设备上监听 mjpeg 的 TCP 端口，Appium WDA 默认 9100；改了要同步改 WDA 配置
    wda_mjpeg_device_port: int = Field(
        default=9100,
        description="WDA MJPEG server 在 iOS 设备上监听的端口（Appium WDA 默认 9100）",
    )
    # WDA mjpeg 帧率：通过 appium settings 动态设置；20fps 在画质和延迟之间的甜点
    wda_mjpeg_fps: int = Field(
        default=20,
        description="wda_mjpeg 模式下请求 WDA 输出的目标帧率（10-30 合理）",
    )
    # WDA mjpeg JPEG 质量：1-100，60 ~ 80KB/帧，文件小延迟低；过低会糊
    wda_mjpeg_quality: int = Field(
        default=60,
        description="wda_mjpeg 模式下请求 WDA 输出的 JPEG 质量（10-100）",
    )
    # WDA mjpeg 最长边降采样阈值：和 dvt_screenshot 路径一致，720 是甜点
    wda_mjpeg_long_edge: int = Field(
        default=720,
        description="wda_mjpeg 模式下让 WDA 直接输出的最长边像素（设 0 = 不让 WDA 缩放）",
    )

    # ------------------------------------------------------------------
    # iOS 预热：插线就启 WDA，而不是等浏览器点"进入工作台"才启
    # ------------------------------------------------------------------
    # 默认 False（按需启动，历史行为）：
    #   - 插上 iPhone 只读 lockdown 元信息，设备卡出现在 web
    #   - 浏览器点"进入工作台"才触发 xcodebuild test，首次冷编 1~3 分钟
    # True（即插即用）：
    #   - agent rescan 发现新 iOS 就后台拉起 WDA，不等浏览器
    #   - xcodebuild 启动进度通过 MSG_DEVICE_STATUS 推到 web
    #   - 适合自动化 7x24 随时被调用，以"Mac 一直烧点 CPU"换"点开就能用"
    # 多台 iOS 共存时注意：并发 xcodebuild 会把 Mac 吃满，建议单台时用
    ios_wda_preload: bool = Field(
        default=False,
        description="True=插线就启 WDA（即插即用），False=按需启动（浏览器打开才启）",
    )

    # iOS 进入工作台 / WDA 就绪后自动唤醒屏幕
    # Face ID 机型长时间不操作会息屏，即使没锁 lockdown 也读不到元信息；
    # 这里用 WDA 调一下 unlock + press home，让屏幕重新亮起来。
    # 只影响"亮屏"；如果 iPhone 是带密码的锁屏，仍然需要人手或 VLM 输密码解。
    ios_wake_on_enter: bool = Field(
        default=True,
        description="WDA 就绪后自动点亮屏幕（Face ID 机型防息屏）",
    )

    # ------------------------------------------------------------------
    # HarmonyOS 镜像参数（M4）
    # ------------------------------------------------------------------
    # 两个后端可切（env ``AI_PHONE_HARMONY_MIRROR_BACKEND``）：
    # - ``screenshot``（**遗留兜底**）：hmdriver2 截图轮询 → JPEG 直推浏览器。
    #     实测单帧 200-400ms（USB2 + JPEG 编码 + hdc 往返），8-10fps 上限。稳定但卡。
    #     **致命短板**：snapshot_display 抓不到 XComponent 等独立硬件视频图层，
    #     视频播放期间画面全黑，VLM 截图兜底也接不上（兜底源是同一个 API）。
    # - ``hypium``（**默认**）：hypium Captures MJPEG 协议（hmdriver2 内部
    #     RecordClient 同款）。设备硬编码后通过 uitest socket 主动 push JPEG 帧序列，
    #     实测 30fps 左右、延迟 <100ms。**关键能力**：包含完整合成画面（含视频
    #     图层），是 VLM 视频期间唯一可用的截图源。和 iOS ``mjpeg_passthrough``
    #     上层数据契约一致，旋转/折叠屏天然自适应。
    #
    # 默认从 screenshot 升级为 hypium 是 P0 工程决策：视频不黑屏 + 性能更好，无回退理由。
    # 若 hypium 在某些 OEM 设备上行为异常，可临时通过 env 切回 screenshot。
    harmony_mirror_backend: str = Field(
        default="hypium",
        description=(
            "鸿蒙镜像后端：hypium（默认，~30fps、含视频图层）/ screenshot（遗留，"
            "~8fps、视频黑屏）。env: AI_PHONE_HARMONY_MIRROR_BACKEND"
        ),
    )
    # 下面 fps / quality / long_edge 仅 screenshot 后端生效；hypium 后端帧率/分辨率
    # 由设备硬编码自决（参数走不到 streamer，hmdriver2 的 startCaptureScreen 当前
    # args=[]，由设备默认行为决定，所以这里不引入新字段）。
    harmony_mirror_fps: int = Field(
        default=8,
        description="鸿蒙镜像目标帧率（仅 screenshot 后端；实测上限 ~10fps）",
    )
    harmony_mirror_jpeg_quality: int = Field(
        default=55,
        description="鸿蒙镜像 JPEG 重压质量（1-100；仅 screenshot 后端，hypium 走设备原图）",
    )
    harmony_mirror_long_edge: int = Field(
        default=720,
        description="鸿蒙镜像最长边降采样阈值（0 = 不缩放；仅 screenshot 后端）",
    )

    # --- Readiness Gate（v1 第 1 梯队） ---
    # 设备可调度入口探活。与已有的 WDA 启动进度（DEVICE_STATUS）并行，专门回答
    # "online 的设备是否真的可以被派单"这个问题。纯旁路、只读；不触发任何执行或
    # 唤醒动作。
    readiness_enabled: bool = Field(
        default=True,
        description="是否启用 readiness 探活。关掉时所有 online 设备默认视为 ready",
    )
    readiness_poll_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="每台设备的 readiness 探活轮询间隔（秒），默认 5s",
    )
    readiness_fail_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="连续失败 N 次才把设备从 ready 降级为 not_ready，避免抖动",
    )
    readiness_probe_timeout_sec: float = Field(
        default=3.0,
        ge=0.5,
        le=30.0,
        description="单次 probe 的超时（秒）；超时算一次失败",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取进程级单例配置。"""
    return Settings()
