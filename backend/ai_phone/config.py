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
    # --- 主 VLM 协议后端开关（多协议适配层）---
    # 主 VLM 走哪家协议，决定执行链上"看图 → 决策 → 输出动作"那一坨怎么发请求。
    # 三家协议差异较大（方舟 Responses / Anthropic Messages-tools / OpenAI Responses-computer_use_preview），
    # 走"高冗余、低耦合"——每家在 ai_phone/shared/llm/main/ 下独立一个文件，互不影响。
    # 切换后 vlm_api_url / vlm_api_key / vlm_model 三个字段含义会跟随协议变化：
    #   - doubao_responses（默认）：方舟 Responses API，model 用 doubao-seed-1-6-vision-*
    #   - claude_cu：Anthropic Messages API + computer 工具，model 用 claude-*-claude-*-sonnet-*
    #   - gpt_cu：OpenAI Responses API + computer_use_preview 工具，model 用 computer-use-preview / gpt-*
    # 注：辅助系统协议由 assistant_backend 单独控制，二者可自由组合（比如 主用 Claude + 辅用 Doubao）。
    vlm_backend: str = Field(
        default="doubao_responses",
        description=(
            "主 VLM 协议后端。可选 doubao_responses（默认）/ claude_cu / gpt_cu。"
            "env: AI_PHONE_VLM_BACKEND"
        ),
    )
    # 主 VLM 思考链预算（tokens），仅在 backend 支持 thinking 时生效：
    # - doubao_responses: 不读本字段（豆包 vision 不开 thinking，关闭节省 token）
    # - claude_cu: payload.thinking.budget_tokens；0 表示关闭 thinking
    # - gpt_cu: 不读（GPT computer-use-preview 自带推理，不可关也不需配额）
    # 默认 1024 tokens 是 Claude Computer Use 官方建议起点，足够单步决策推理。
    vlm_main_thinking_budget: int = Field(
        default=1024,
        ge=0,
        le=8192,
        description=(
            "主 VLM 思考预算（tokens）。仅 claude_cu 生效（0 关闭）；"
            "doubao_responses / gpt_cu 忽略。env: AI_PHONE_VLM_MAIN_THINKING_BUDGET"
        ),
    )
    # 主 VLM 历史窗口大小（仅 stateless 协议生效）：
    # - doubao_responses: 服务端续历史 + 显式缓存，本字段忽略
    # - claude_cu / gpt_cu: 客户端累积 messages，过长会指数级抬 token；
    #   长任务 30 步以上时只保留首屏 1 步 + 最近 N 步对，避免 token 爆。
    vlm_history_window_steps: int = Field(
        default=12,
        ge=2,
        le=64,
        description=(
            "Claude / GPT 主 VLM 历史滑窗保留的最近步数。仅 stateless 协议生效。"
            "env: AI_PHONE_VLM_HISTORY_WINDOW_STEPS"
        ),
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
    # --- 辅助系统协议后端开关（多协议适配层）---
    # 4 个辅助调用（包名匹配 / 通道判定 / 审判 / 断言）走哪家协议。和 vlm_backend
    # 完全解耦：可以"主 VLM 走 Claude，辅助走 Doubao（成本低）" 这种组合。
    # 各家在 ai_phone/shared/llm/assistants/ 下独立文件实现，确保零交叉。
    #   - doubao_chat（默认）：方舟 Chat Completions，model 用 doubao-seed-1-6-*
    #   - claude：Anthropic Messages API，model 用 claude-*-sonnet-*
    #   - openai：OpenAI Chat Completions，model 用 gpt-4o / gpt-4.1 等
    # 切换后 assistant_api_url / assistant_api_key / assistant_model 含义跟随协议变化。
    # thinking 开关在不同协议下含义略不同：
    #   - 豆包：payload.thinking.type = enabled/disabled
    #   - Claude：payload.thinking.type = enabled + budget_tokens
    #   - OpenAI：reasoning.effort（仅 o-系列）；非推理模型忽略
    assistant_backend: str = Field(
        default="doubao_chat",
        description=(
            "辅助系统协议后端。可选 doubao_chat（默认）/ claude / openai。"
            "env: AI_PHONE_ASSISTANT_BACKEND"
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

    # --- Midscene 执行器（外接，寄居在 ai-phone/midscene-bridge/）---
    # 详细方案见 `Midscene执行器接入方案.md`。
    # 默认关闭：未显式开启时，ai-phone 主仓零行为变化。
    midscene_enabled: bool = Field(
        default=False,
        description=(
            "是否暴露 Midscene 执行器选项。关闭时：Web 不显示引擎下拉框，"
            "POST /api/runs 收到 engine=midscene 直接 400。"
        ),
    )
    midscene_run_timeout_sec: int = Field(
        default=60 * 60,
        ge=60,
        le=24 * 60 * 60,
        description=(
            "Midscene run 单次硬超时（秒）。默认 60 分钟，给长链 case 留余量；"
            "业务 case 永远短链可调小。"
        ),
    )
    midscene_bridge_dir: Optional[Path] = Field(
        default=None,
        description=(
            "midscene-bridge 目录绝对路径。空 = 自动定位到 <repo>/midscene-bridge。"
            "在自定义安装位置时显式指定。"
        ),
    )
    midscene_node_bin: str = Field(
        default="node",
        description="Node 可执行文件路径；默认走 PATH。多版本 Node 时可写绝对路径。",
    )

    # ──────────────────────────────────────────────────────────────────
    # Submission 调度 TTL（开源运维必改项）
    # ──────────────────────────────────────────────────────────────────
    # scheduler 兜底超时：批次 / item 跑过这个时长强制收口，防止异常 agent
    # 永远不回 RUN_DONE 把队列死锁。这两个值是开源用户**几乎一定要按业务调整**
    # 的——跑短任务嫌 1h item 太长（单设备 stuck 1h 才回收），跑长 case 嫌
    # 3h 批次太短（一个长 case 可能跑 1h，10 条就不够）。
    submission_ttl_sec: int = Field(
        default=3 * 60 * 60,
        ge=60,
        le=24 * 60 * 60,
        description=(
            "Submission（批次）总硬超时（秒）。从 admit 时刻开始计时，超过仍未收口"
            "则强制把剩余 item 标 expired 并触发终态广播。默认 3 小时。"
            "env: AI_PHONE_SUBMISSION_TTL_SEC"
        ),
    )
    item_ttl_sec: int = Field(
        default=60 * 60,
        ge=60,
        le=12 * 60 * 60,
        description=(
            "单个 SubmissionItem 硬超时（秒）。item 进入 running 后超时仍无 RUN_DONE"
            "则强制 finalize 为 timeout。默认 1 小时，覆盖典型业务 case。"
            "env: AI_PHONE_ITEM_TTL_SEC"
        ),
    )
    scheduler_tick_sec: float = Field(
        default=2.0,
        ge=0.1,
        le=60.0,
        description=(
            "scheduler 后台 tick 周期（秒）。事件驱动路径会主动 kick，这是兜底巡检。"
            "短任务高吞吐场景可调小（0.5-1s）；空闲集群可调大省 CPU。"
            "env: AI_PHONE_SCHEDULER_TICK_SEC"
        ),
    )

    # ──────────────────────────────────────────────────────────────────
    # Run 行为硬上限（业务调优项）
    # ──────────────────────────────────────────────────────────────────
    # 单 Run 最大 VLM 决策步数。超过强制终止为 fail，防止 VLM 在死路上无限刷
    # token。100 步对绝大多数业务 case 充裕，长链 case（>30 步操作链）需要调大。
    run_max_steps: int = Field(
        default=100,
        ge=10,
        le=1000,
        description=(
            "单 Run 最大决策步数硬上限。超过强制 fail 收尾，防 VLM 在死路打转烧 token。"
            "长链 case（>30 步）适当调大；常规短 case 100 已经偏宽松。"
            "env: AI_PHONE_RUN_MAX_STEPS"
        ),
    )
    # 单步 wait action 最大等待秒数。VLM 输出 wait(seconds=N) 时被裁剪到该上限。
    # 默认 1800s（30 分钟）覆盖"等视频播完 / 等服务端跑批"等场景；普通业务可调小。
    run_max_wait_sec: int = Field(
        default=1800,
        ge=10,
        le=24 * 60 * 60,
        description=(
            "单次 wait action 最大允许等待秒数。VLM 申请超过该值会被裁剪并标 clipped。"
            "默认 30 分钟为长等场景兜底；常规业务可调到 60-300s。"
            "env: AI_PHONE_RUN_MAX_WAIT_SEC"
        ),
    )
    # 审判系统单次调用超时；超时按 ALLOW 处理（不阻塞 Run 收尾）。
    audit_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description=(
            "结构化通道审判（_run_struct_audit）单次调用超时（秒）。"
            "超时按 ALLOW 处理不阻塞 Run；网络环境差/海外节点可调到 60-90s。"
            "env: AI_PHONE_AUDIT_TIMEOUT_SEC"
        ),
    )
    # 断言系统终局裁决超时；超时按 SKIP 处理（回退采纳主 VLM 结果）。
    assertion_timeout_sec: float = Field(
        default=60.0,
        ge=10.0,
        le=600.0,
        description=(
            "断言系统（finished 终局裁决）单次调用超时（秒）。"
            "超时按 SKIP 处理回退主 VLM 结果，不阻塞 Run 收尾。"
            "env: AI_PHONE_ASSERTION_TIMEOUT_SEC"
        ),
    )
    # 审判 ALLOW 上限：审判放行多少次后下次召唤直接 KILL 绕过审判模型。
    # 30 次覆盖典型长 case 全程合法重试；觉得审判不靠谱、误 KILL 多发的运维可上调到 50/100。
    audit_allow_limit: int = Field(
        default=30,
        ge=1,
        le=500,
        description=(
            "审判系统单 Run 累计 ALLOW 次数上限。达上限后探测器召唤直接 KILL，绕过审判。"
            "审判模型不稳/容易误 KILL 时调大；想让 Run 早死省 token 时调小。"
            "env: AI_PHONE_AUDIT_ALLOW_LIMIT"
        ),
    )
    # 周期巡检间隔：每 N 步主动召唤一次审判，兜底"VLM 一鼓作气走错路"。
    # 5 太频繁前期合法跳步常被误 KILL；30 给足 VLM 上下文再监督。
    audit_periodic_interval: int = Field(
        default=30,
        ge=0,
        le=200,
        description=(
            "审判周期巡检间隔（步）。每 N 步主动召唤一次审判检查推进合理性。"
            "0 = 关闭周期巡检（仅 detector 触发）；调大让 VLM 多跑几步再检查。"
            "env: AI_PHONE_AUDIT_PERIODIC_INTERVAL"
        ),
    )
    # 链式动作上限：同一轮 VLM 决策内串联多个 Action 用于追"瞬态 UI"。
    chain_max_actions: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "单轮 VLM 决策最多串联多少个 Action（超出截断保留前 N 个）。"
            "1 = 关闭链式（每步只走一个 Action）；视频/工具栏场景需要 ≥ 2。"
            "env: AI_PHONE_CHAIN_MAX_ACTIONS"
        ),
    )
    chain_inner_gap_ms: int = Field(
        default=200,
        ge=0,
        le=2000,
        description=(
            "链内相邻两个 Action 的硬等间隔（毫秒）。"
            "0 = 不等（极激进）；过大会让链式失去毫秒级追瞬态 UI 的意义。"
            "env: AI_PHONE_CHAIN_INNER_GAP_MS"
        ),
    )

    # ──────────────────────────────────────────────────────────────────
    # 卡死检测阈值（"误 kill 多发"运维必改区）
    # ──────────────────────────────────────────────────────────────────
    # 这一组是本地纯算法（不调 LLM）的兜底卡死探测器。命中后会向 VLM 注入
    # "卡死提示"或直接终止 Run。阈值偏小 → 误 kill 多发；偏大 → 真死循环
    # 拖太久。开源用户根据自家 app 的"合法重试节奏"调整。
    click_stuck_threshold: int = Field(
        default=4,
        ge=2,
        le=20,
        description=(
            "同坐标连续点击触发卡死提示的次数。点过 N 次同位置仍无屏变化 → 注入提示。"
            "调大可减少误 kill（合法连点场景，如刷新按钮/抽奖）。"
            "env: AI_PHONE_CLICK_STUCK_THRESHOLD"
        ),
    )
    scroll_stuck_threshold: int = Field(
        default=3,
        ge=2,
        le=20,
        description=(
            "同方向连续无效滚动触发卡死提示的次数。N 次 swipe 屏幕几乎不动 → 注入提示。"
            "长列表底部场景调大；常规列表保持默认。"
            "env: AI_PHONE_SCROLL_STUCK_THRESHOLD"
        ),
    )
    unknown_action_streak_limit: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "VLM 连续输出未知 / 解析失败 action 的次数上限。超过 → 注入纠正提示。"
            "模型偶尔抖动可调到 5；想严格收口调到 2。"
            "env: AI_PHONE_UNKNOWN_ACTION_STREAK_LIMIT"
        ),
    )
    consecutive_screenshot_fail_limit: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "连续截图失败次数上限。超过 → 终止 Run 标 fail（设备可能掉线/崩溃）。"
            "弱网/老旧设备可调到 5-8；线上稳定环境保持默认。"
            "env: AI_PHONE_CONSECUTIVE_SCREENSHOT_FAIL_LIMIT"
        ),
    )

    # ──────────────────────────────────────────────────────────────────
    # 异常介入触发阈值（结构化通道审判 detector）
    # ──────────────────────────────────────────────────────────────────
    # 这一组是"召唤审判"的本地探测器阈值。命中后**不直接 kill**，是丢给
    # 轻量审判模型判定继续 / 终止。阈值偏小 → 频繁召唤审判（多 token 多延迟）；
    # 偏大 → 真偏离也叫不醒审判。"误 kill 多发"运维场景的关键调节区。
    audit_click_bucket_px: float = Field(
        default=50.0,
        ge=10.0,
        le=300.0,
        description=(
            "同坐标桶聚类容差（像素，质心欧氏距）。距离 ≤ 此值视为同一桶。"
            "高分屏可调大（80-100）；密集 UI 调小（20-30）。"
            "env: AI_PHONE_AUDIT_CLICK_BUCKET_PX"
        ),
    )
    audit_click_bucket_trigger: int = Field(
        default=3,
        ge=2,
        le=20,
        description=(
            "同坐标桶累计 click ≥ N 次召唤审判。"
            "调大允许更多合法重试；调小快速发现反复点同一处模式。"
            "env: AI_PHONE_AUDIT_CLICK_BUCKET_TRIGGER"
        ),
    )
    audit_screen_revisit_hamming: int = Field(
        default=8,
        ge=0,
        le=64,
        description=(
            "pHash 汉明距阈值，≤ 此值视为同屏。256 bit 哈希默认 8 ≈ 3% 差异。"
            "调小（4-6）严格判同屏；调大（12-16）容忍更多视觉抖动。"
            "env: AI_PHONE_AUDIT_SCREEN_REVISIT_HAMMING"
        ),
    )
    audit_screen_revisit_trigger: int = Field(
        default=3,
        ge=2,
        le=20,
        description=(
            "同屏访问累计 ≥ N 次召唤审判。Tab 切换 / 抽屉开合等合法多次访问可调大。"
            "env: AI_PHONE_AUDIT_SCREEN_REVISIT_TRIGGER"
        ),
    )
    audit_scroll_flip_window: int = Field(
        default=6,
        ge=2,
        le=30,
        description=(
            "滚动方向翻转检测窗口（最近 N 次滚动）。"
            "env: AI_PHONE_AUDIT_SCROLL_FLIP_WINDOW"
        ),
    )
    audit_scroll_flip_trigger: int = Field(
        default=2,
        ge=1,
        le=10,
        description=(
            "窗口内方向翻转 ≥ N 次召唤审判（震荡 / 东找西找）。"
            "env: AI_PHONE_AUDIT_SCROLL_FLIP_TRIGGER"
        ),
    )
    audit_scroll_noprogress_diff: float = Field(
        default=0.02,
        ge=0.0,
        le=0.5,
        description=(
            "滚动前后帧 diff_rate ≤ 此值视为页面几乎没动（无效滑动）。"
            "调大（0.05）容忍更多视觉抖动；调小（0.005）严格判无效滑动。"
            "env: AI_PHONE_AUDIT_SCROLL_NOPROGRESS_DIFF"
        ),
    )
    audit_scroll_noprogress_trigger: int = Field(
        default=3,
        ge=2,
        le=20,
        description=(
            "同方向连续无效滑动 ≥ N 次召唤审判（已到列表底/无更多内容场景）。"
            "env: AI_PHONE_AUDIT_SCROLL_NOPROGRESS_TRIGGER"
        ),
    )

    # ──────────────────────────────────────────────────────────────────
    # 通道判定阈值（结构化 vs 自由对话）
    # ──────────────────────────────────────────────────────────────────
    # 启动期判定 case 走哪条通道：结构化（带审判 + 严格步骤校验）还是自由
    # 对话（VLM 自主决策）。阈值控制"严格度多高才进结构化"。
    # - 关键字命中 ≥ HARD_HIT       → 直接结构化（最高置信，免审判分类调用）
    # - 严格度评分 ≥ HARD_SCORE     → 直接结构化（长 case + 密集约束）
    # - 严格度评分 ≥ AUDIT_SCORE    → 借审判模型一次性分类
    # - 都不达标                     → 自由对话通道
    struct_keyword_hard_hit: int = Field(
        default=2,
        ge=1,
        le=10,
        description=(
            "四级标签命中数 ≥ 此值直接走结构化（免审判分类调用）。"
            "标签：测试标题/前置条件/操作步骤/预期结果/起跑线/资源选择/兜底等。"
            "env: AI_PHONE_STRUCT_KEYWORD_HARD_HIT"
        ),
    )
    struct_strictness_hard_score: int = Field(
        default=5,
        ge=1,
        le=10,
        description=(
            "严格度综合评分 ≥ 此值直接走结构化（0-7 分制）。"
            "评分维度：引号/数字约束/逻辑词/顺序词/动词等。"
            "env: AI_PHONE_STRUCT_STRICTNESS_HARD_SCORE"
        ),
    )
    struct_strictness_audit_score: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "严格度评分 ≥ 此值借审判模型分类（中等信号 case）。"
            "调小让更多 case 进结构化（严格但慢）；调大放宽自由对话覆盖。"
            "env: AI_PHONE_STRUCT_STRICTNESS_AUDIT_SCORE"
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取进程级单例配置。"""
    return Settings()
