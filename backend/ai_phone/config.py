from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILES: tuple[str, str, str] = (".env.defaults", ".env", ".env.local")


class Settings(BaseSettings):
    """统一配置，环境变量优先，项目默认配置兜底。

    所有变量名以 `AI_PHONE_` 前缀暴露。约定：
    - Server / Agent 同一套 Settings，各自只读自己需要的字段。
    - 不在代码里硬编码部署值；默认策略走 .env.defaults，真实部署值走 .env / .env.local。
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_PHONE_",
        env_file=ENV_FILES,
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

    # ── Android 虚拟机（Emulator）行为参数 ──
    # 统一收归 Settings，纳入「下发集」由 Server 集中控制（Agent 不自行改）。
    # 真正"能起几台"由 capability.probe 按宿主实时可用内存自适应，不依赖配置，
    # 所以这些统一下发不会有"每台机器容量不同"的问题。env 名 = AI_PHONE_ + 字段名大写。
    android_vm_max_instances: int = Field(
        default=15,
        description="探查 details 里展示用的参考上限；**不再拦截**（数量/内存都改软提示，能起几台由实际资源决定）。env: AI_PHONE_ANDROID_VM_MAX_INSTANCES",
    )
    android_vm_min_free_mb: int = Field(
        default=2048,
        description="软提示阈值：宿主可用内存低于「该 VM RAM + 此余量」时，探查仍可用但附风险提醒（不拦截）。env: AI_PHONE_ANDROID_VM_MIN_FREE_MB",
    )
    android_vm_no_window: bool = Field(
        default=True,
        description="模拟器是否无头运行（不弹宿主窗口，画面仍走投屏）。env: AI_PHONE_ANDROID_VM_NO_WINDOW",
    )
    android_vm_boot_timeout_sec: int = Field(
        default=120,
        description="模拟器开机等待上限秒数。env: AI_PHONE_ANDROID_VM_BOOT_TIMEOUT_SEC",
    )
    android_vm_density: int = Field(
        default=420,
        description="模拟器默认显示密度（dpi）。env: AI_PHONE_ANDROID_VM_DENSITY",
    )
    android_vm_kill_foreign: bool = Field(
        default=False,
        description="是否清理非本系统启动的野生 emulator。env: AI_PHONE_ANDROID_VM_KILL_FOREIGN",
    )
    android_vm_orphan_cleanup: bool = Field(
        default=True,
        description="(重)连后是否上报本机受管 AVD 清单做孤儿对账清理。env: AI_PHONE_ANDROID_VM_ORPHAN_CLEANUP",
    )
    android_vm_image_cache_sec: int = Field(
        default=300,
        description="探查时列举已装 system-image 的结果缓存秒数（首次 sdkmanager 扫描慢，缓存长一些减少冷跑/探查超时）。env: AI_PHONE_ANDROID_VM_IMAGE_CACHE_SEC",
    )
    android_vm_locale: str = Field(
        default="zh-CN",
        description="新建虚拟机的系统语言 locale（开机后 setprop persist.sys.locale + 重启 framework 生效）。空串=不改，保持镜像默认。env: AI_PHONE_ANDROID_VM_LOCALE",
    )
    android_vm_timezone: str = Field(
        default="Asia/Shanghai",
        description="新建虚拟机的时区（启动 -prop + 开机后 setprop persist.sys.timezone）。空串=不改。env: AI_PHONE_ANDROID_VM_TIMEZONE",
    )
    android_vm_optimize_for_automation: bool = Field(
        default=True,
        description="开机后做自动化友好预置：关闭系统动画三件套 + 24 小时制。env: AI_PHONE_ANDROID_VM_OPTIMIZE_FOR_AUTOMATION",
    )
    android_setup_stay_awake: bool = Field(
        default=True,
        description=(
            "Android driver 打开/扫描设备时是否沿用旧策略：设置超长 screen_off_timeout "
            "+ svc power stayon true。默认 True 保持历史行为；想让设备空闲自然息屏时设 False。"
            "env: AI_PHONE_ANDROID_SETUP_STAY_AWAKE"
        ),
    )
    android_wake_before_run: bool = Field(
        default=False,
        description=(
            "Android VLM Run 开始前是否主动唤醒屏幕并尝试收起无安全认证的 keyguard。"
            "默认 False 保持历史行为；关闭 stay_awake 后建议设 True。"
            "env: AI_PHONE_ANDROID_WAKE_BEFORE_RUN"
        ),
    )
    android_screen_off_dispatchable: bool = Field(
        default=False,
        description=(
            "Android readiness 遇到屏幕息屏/DOZE 时是否仍允许派发。默认 False 保持历史"
            "行为；开启后由 Run 前 wake + dismiss-keyguard 负责把设备拉回可操作态。"
            "env: AI_PHONE_ANDROID_SCREEN_OFF_DISPATCHABLE"
        ),
    )
    android_wake_before_run_settle_ms: int = Field(
        default=500,
        ge=0,
        le=5000,
        description=(
            "Android Run 前唤醒后的短等待毫秒数，用于等待屏幕点亮和首帧刷新。"
            "仅 AI_PHONE_ANDROID_WAKE_BEFORE_RUN=true 时生效。"
            "env: AI_PHONE_ANDROID_WAKE_BEFORE_RUN_SETTLE_MS"
        ),
    )
    android_wake_on_enter: bool = Field(
        default=False,
        description=(
            "Android 进入工作台/启动镜像前是否主动唤醒。默认 False 保持历史行为；"
            "开启后复用 Android Run 前 wake 逻辑。env: AI_PHONE_ANDROID_WAKE_ON_ENTER"
        ),
    )

    harmony_setup_stay_awake: bool = Field(
        default=True,
        description=(
            "HarmonyOS driver 打开/扫描设备时是否沿用旧策略：power-shell timeout 长亮续约。"
            "默认 True 保持历史行为；想让设备空闲自然息屏时设 False。"
            "env: AI_PHONE_HARMONY_SETUP_STAY_AWAKE"
        ),
    )
    harmony_screen_off_dispatchable: bool = Field(
        default=False,
        description=(
            "HarmonyOS readiness 遇到息屏时是否仍允许派发。默认 False 保持当前"
            "screen_locked 行为；开启后由 Run preflight 负责 wake，必要设备再按 Server 策略上滑。"
            "env: AI_PHONE_HARMONY_SCREEN_OFF_DISPATCHABLE"
        ),
    )
    harmony_wake_before_run: bool = Field(
        default=False,
        description=(
            "HarmonyOS VLM Run 开始前是否用纯 hdc 主动唤醒屏幕。默认 False "
            "保持历史行为。env: AI_PHONE_HARMONY_WAKE_BEFORE_RUN"
        ),
    )

    harmony_wake_swipe_enabled: bool = Field(
        default=True,
        description=(
            "HarmonyOS Run 前唤醒后是否允许按 Server 下发策略上滑进入桌面/可操作态。"
            "默认 True 但只有 AI_PHONE_HARMONY_WAKE_BEFORE_RUN=true 时生效。"
            "env: AI_PHONE_HARMONY_WAKE_SWIPE_ENABLED"
        ),
    )
    harmony_wake_settle_ms: int = Field(
        default=500,
        ge=0,
        le=5000,
        description=(
            "HarmonyOS power-shell wakeup 后等待屏幕亮起的毫秒数。"
            "env: AI_PHONE_HARMONY_WAKE_SETTLE_MS"
        ),
    )
    harmony_wake_swipe_settle_ms: int = Field(
        default=500,
        ge=0,
        le=5000,
        description=(
            "HarmonyOS 唤醒后上滑完成的短等待毫秒数。"
            "env: AI_PHONE_HARMONY_WAKE_SWIPE_SETTLE_MS"
        ),
    )
    harmony_wake_on_enter: bool = Field(
        default=False,
        description=(
            "HarmonyOS 进入工作台/启动镜像前是否用纯 hdc 唤醒。默认 False 保持"
            "历史行为；黑屏可派发策略下建议开启。env: AI_PHONE_HARMONY_WAKE_ON_ENTER"
        ),
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
            "内部派生字段：主 VLM 协议后端。可选 doubao_responses（默认）/ "
            "claude_cu / gpt_cu；外部请配置 AI_PHONE_PHONE_VLM_PROVIDER。"
        ),
    )
    # 海外 Computer Use prompt 默认保持英文，避免开源用户拿到中文化默认体验；
    # 私有中文调试时可打开，让 Claude/GPT CU 的 reasoning / 完成说明尽量输出中文。
    vlm_cu_zh_prompt_enabled: bool = Field(
        default=False,
        description=(
            "Claude/GPT Computer Use 主 VLM 是否启用中文可读日志 prompt。默认关闭，"
            "保持海外开源默认英文体验。env: AI_PHONE_VLM_CU_ZH_PROMPT_ENABLED"
        ),
    )
    # 主 VLM 思考链预算（tokens），仅在 backend 支持 thinking 时生效：
    # - doubao_responses: 不读本字段（豆包 vision 不开 thinking，关闭节省 token）
    # - claude_cu: payload.thinking.budget_tokens；0 表示关闭 thinking
    # - gpt_cu: 不读（GPT 用 reasoning.effort 而不是 budget_tokens 控推理强度，
    #           见下方 vlm_main_reasoning_effort）
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
    # 主 VLM 推理强度，仅 gpt_cu 生效：
    # - doubao_responses / claude_cu: 忽略（各自走 thinking 字段控制）
    # - gpt_cu: payload.reasoning.effort = low/medium/high
    #   * low：~30% 推理 token，适合 8 步内的简单 case
    #   * medium（默认）：平衡速度和准确度，适合常规自动化 case
    #   * high：~3x 推理 token + 更慢，适合多步骤复杂决策（注意成本）
    # 不可关——computer-use-preview 是推理模型，必须有非零 effort。
    vlm_main_reasoning_effort: str = Field(
        default="medium",
        pattern=r"^(low|medium|high)$",
        description=(
            "主 VLM 推理强度。仅 gpt_cu 生效，可选 low / medium（默认）/ high；"
            "doubao_responses / claude_cu 忽略。env: AI_PHONE_VLM_MAIN_REASONING_EFFORT"
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
    # --- VLM 轨迹缓存回放 ---
    # 默认关闭：保存/删除轨迹可以先沉淀数据；真正命中后旁路 replay 需要
    # 等断言、报告和实机烟测稳定后再打开，避免影响现有 VLM 首跑主流程。
    vlm_trajectory_cache_replay_enabled: bool = Field(
        default=False,
        description=(
            "是否启用 VLM 成功轨迹缓存回放。默认关闭；开启后 server_brain/vlm "
            "会先按 device_code + run 语义强匹配查缓存，命中则走独立 replay 通道。"
            "env: AI_PHONE_VLM_TRAJECTORY_CACHE_REPLAY_ENABLED"
        ),
    )
    trajectory_cache_enabled: bool = Field(
        default=False,
        description=(
            "统一轨迹缓存能力总开关。true 时 Run payload 可通过 cacheMode 选择 "
            "off/v1/v2/v3；false 时任何 cacheMode 都静默对齐为 off。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ENABLED"
        ),
    )
    # Anthropic prompt caching 开关：
    # - 仅 claude_cu 生效（GPT 走 previous_response_id 服务端续历史，自带缓存；
    #   doubao_responses 同理）。
    # - 默认 **关闭**——cache write 成本是普通 input 的 1.25x（5min TTL），
    #   break-even 在 ~3 次 cache hit 之后才赚。短 case + Run 间隔长的场景
    #   反而亏，须按业务量评估再开。开启后 Anthropic 会对标了
    #   ``cache_control`` 的 system / tools / messages 前缀做缓存复用。
    # - 实测建议：生产长 case 跑 20+ 步 + Run 间隔 < 5 分钟时开启，可省
    #   60-90% input token；短 case / 调试场景关。
    vlm_main_prompt_caching_enabled: bool = Field(
        default=False,
        description=(
            "Anthropic prompt caching 开关。仅 claude_cu 生效。默认关闭"
            "（短 case / Run 间隔长场景 cache write 成本反而亏）；长 case +"
            "频繁 Run 场景开启可省 60-90% input token。"
            "env: AI_PHONE_VLM_MAIN_PROMPT_CACHING_ENABLED"
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
            "内部派生字段：辅助系统协议后端。可选 doubao_chat（默认）/ "
            "claude / openai；外部请配置 AI_PHONE_AUX_PROVIDER。"
        ),
    )

    # ── 新版统一模型配置（两块连接身份 · 必填 · 不再回退 legacy）────────
    # 对外只暴露两块连接身份，详见 backend/.env.example：
    #   1) phone_vlm_*：碰手机的 VLM（主决策 + 辅助恢复 / 定位 / 门控），vision 专版。
    #      系统内部自动拆协议——主决策走 /responses 主动式缓存续接；辅助恢复 / 定位 /
    #      门控走 /chat/completions 单次。用户不需要、也不应该手填这两个端点。
    #   2) aux_*：辅助模型（包名 / 通道 / 审判 / 断言 / 瞬态分类 / 大盘分析），通用版，
    #      单次判断，不碰手机；必须显式配置，不能跟随主模型。
    # 生效规则：phone_vlm_* 四项和 aux_* 四项都必填；
    # 由 _derive_new_model_config() 自动派生并覆盖下方内部 vlm_* / assistant_* /
    # trajectory_cache_*_vlm_* 连接字段。缺项直接报配置错误，不再回退旧式 VLM_*。
    # provider 决定内部执行链路：
    #   - doubao：主决策走方舟 Responses，手机层单次走方舟 Chat。
    #   - claude：主决策走已验证的 claude_cu，手机层单次走 claude_messages。
    #   - openai/gpt：主决策走 gpt_cu，手机层单次走 OpenAI Responses。
    # 重要约束：doubao 主执行必须在火山方舟控制台**手动开启“上下文缓存 /
    # 主动缓存”开关**，否则主动缓存不生效、长任务 token 成本会暴涨。
    phone_vlm_provider: str = Field(
        default="",
        description=(
            "碰手机 VLM 提供方：doubao / claude / openai(gpt)。"
            "env: AI_PHONE_PHONE_VLM_PROVIDER"
        ),
    )
    phone_vlm_model: str = Field(
        default="",
        description="碰手机 VLM 模型（vision 专版，如 doubao-seed-1-6-vision-*）。env: AI_PHONE_PHONE_VLM_MODEL",
    )
    phone_vlm_api_key: str = Field(
        default="",
        description="碰手机 VLM API key。env: AI_PHONE_PHONE_VLM_API_KEY",
    )
    phone_vlm_base_url: str = Field(
        default="",
        description=(
            "碰手机 VLM API 地址。doubao 填到 /api/v3；claude 可填 /v1/messages；"
            "openai 可填 /v1 或 /v1/responses。填完整 endpoint 也会自动归一。"
            "env: AI_PHONE_PHONE_VLM_BASE_URL"
        ),
    )
    aux_provider: str = Field(
        default="",
        description=(
            "辅助模型提供方：doubao / claude / openai(gpt)。"
            "必填，不跟随 PHONE_VLM。env: AI_PHONE_AUX_PROVIDER"
        ),
    )
    aux_model: str = Field(
        default="",
        description="辅助模型（通用版，不碰手机的判断 / 裁决）。env: AI_PHONE_AUX_MODEL",
    )
    aux_api_key: str = Field(
        default="",
        description="辅助模型 API key。必填，不跟随 PHONE_VLM。env: AI_PHONE_AUX_API_KEY",
    )
    aux_base_url: str = Field(
        default="",
        description="辅助模型 API 地址。必填，不跟随 PHONE_VLM。env: AI_PHONE_AUX_BASE_URL",
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
    ios_screen_off_dispatchable: bool = Field(
        default=False,
        description=(
            "iOS readiness 遇到 /wda/locked=true 时是否仍允许派发。默认 False 保持"
            "历史行为；开启后由 Run 前 wda.unlock 负责把屏幕拉起来。"
            "env: AI_PHONE_IOS_SCREEN_OFF_DISPATCHABLE"
        ),
    )
    ios_wake_before_run: bool = Field(
        default=False,
        description=(
            "iOS VLM Run 开始前是否主动调 wda.unlock 唤醒屏幕。默认 False 保持历史"
            "行为；与 ios_screen_off_dispatchable 配套开启。"
            "env: AI_PHONE_IOS_WAKE_BEFORE_RUN"
        ),
    )
    ios_wake_before_run_settle_ms: int = Field(
        default=500,
        ge=0,
        le=5000,
        description=(
            "iOS Run 前唤醒后的短等待毫秒数。仅 ios_wake_before_run=true 时生效。"
            "env: AI_PHONE_IOS_WAKE_BEFORE_RUN_SETTLE_MS"
        ),
    )

    # ------------------------------------------------------------------
    # iOS WDA 生命周期策略（auto / stable）
    # ------------------------------------------------------------------
    # 详见 docs/ios-setup（iOS接入指南）.md。
    #
    # auto   = 调试期（默认）。允许插线预热、preflight_deadlock / runtime_drop
    #          自动 respawn、/status 不通时关 driver 重建——与本字段引入前的
    #          行为完全等价，调试期热拔插自愈能力不下降。
    # stable = 部署期。人工准备一次后 agent 只 attach/reuse，不主动重启 WDA；
    #          WDA 失效后抛 StableWdaUnavailable 让上层报错等人工处理。
    #
    # 注意：本字段仅控制 iOS WDA 生命周期，不影响 Android / HarmonyOS，也不
    # 影响 iOS 已有 tap / swipe / type / screenshot / mirror action。
    ios_wda_lifecycle_mode: str = Field(
        default="auto",
        description=(
            "iOS WDA 生命周期策略：auto=调试期自动恢复（默认），"
            "stable=部署期稳定复用。env: AI_PHONE_IOS_WDA_LIFECYCLE_MODE"
        ),
    )

    # stable 模式下是否允许「每次 USB 插入会话内首次自动 spawn WDA」。
    # True（默认，§7.5.1 B 子方案）：每次 USB 物理插入后允许 agent spawn 一次
    #   WDA；之后禁止 respawn；拔出 USB → policy 清状态 → 重新插入又允许一次。
    #   覆盖"人工准备一次 / 长期复用 / 拔插作为唯一重置入口"的 95% 部署诉求。
    # False（§7.5.1 A 子方案）：严格 attach-only，即使首次插入也要求外部已起
    #   WDA（Xcode / 手工 xcodebuild / 独立守护进程）；仅在外部统一管 WDA 的
    #   严苛部署机房启用。
    # 注意：本字段只在 ios_wda_lifecycle_mode=stable 下生效；auto 模式忽略。
    ios_wda_stable_allow_initial_spawn: bool = Field(
        default=True,
        description=(
            "stable 模式下是否允许每次 USB 插入会话内首次自动 spawn WDA。"
            "env: AI_PHONE_IOS_WDA_STABLE_ALLOW_INITIAL_SPAWN"
        ),
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

    # --- Android scrcpy 镜像（MSE 直传：scrcpy → fmp4 → 浏览器 <video>）---
    # 历史上这几项走 os.environ 直读（agent/main.py 模块级常量），未纳入 Settings，
    # 导致 Server 无法统一控制。Distributed Agent Brain 收归 Settings，纳入下发。
    mirror_max_width: int = Field(
        default=1280,
        description=(
            "scrcpy server 缩放后的长边像素。720 模糊，1280 锐利，1920 接近原生。"
            "env: AI_PHONE_MIRROR_MAX_WIDTH"
        ),
    )
    mirror_max_fps: int = Field(
        default=30,
        description="Android scrcpy H.264 帧率上限。env: AI_PHONE_MIRROR_MAX_FPS",
    )
    mirror_bitrate: int = Field(
        default=6_000_000,
        description="Android scrcpy H.264 编码码率（bit/s）。env: AI_PHONE_MIRROR_BITRATE",
    )
    mirror_frag_ms: int = Field(
        default=50,
        description=(
            "fmp4 媒体分片时长（毫秒），端到端延迟关键项；不要小于 16。"
            "env: AI_PHONE_MIRROR_FRAG_MS"
        ),
    )
    mirror_gop_sec: int = Field(
        default=1,
        description="Android scrcpy IDR 关键帧间隔（秒）。env: AI_PHONE_MIRROR_GOP_SEC",
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
    orphan_reap_grace_sec: float = Field(
        default=60.0,
        ge=0.0,
        le=600.0,
        description=(
            "Agent 断连后回收其名下在跑 Run 的宽限期（秒）。宽限期内同一 agent_id "
            "重连（网络抖动、同进程仍在执行）则跳过回收，不误杀仍在本地跑的 Run；"
            "超期仍未重连（进程重启 / 真死）则把其名下仍非终态的 Run 判 failed、"
            "释放设备锁并收口批次（配了 retry 的按既有逻辑重投）。设 0 表示断连即回收。"
            "env: AI_PHONE_ORPHAN_REAP_GRACE_SEC"
        ),
    )
    function_map_context_enabled: bool = Field(
        default=True,
        description=(
            "功能地图上下文注入开关。关闭时字段仍接收/落库/校验，但不会注入主 VLM。"
            "env: AI_PHONE_FUNCTION_MAP_CONTEXT_ENABLED"
        ),
    )
    function_map_context_max_chars: int = Field(
        default=8000,
        ge=1,
        le=20000,
        description=(
            "functionMapContext 硬字符上限，默认 8000；超限拒绝且不截断。"
            "env: AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS"
        ),
    )
    run_retry_enabled: bool = Field(
        default=False,
        description=(
            "同一 Run 内失败自动重跑总开关。默认关闭；开启后 payload.retryMax "
            "会被 run_retry_max 截断。env: AI_PHONE_RUN_RETRY_ENABLED"
        ),
    )
    run_retry_max: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "单个 Run 允许的最大重跑次数，不含首跑；0 表示即使开关打开也不重跑。"
            "env: AI_PHONE_RUN_RETRY_MAX"
        ),
    )
    run_retry_clear_cache: bool = Field(
        default=True,
        description=(
            "失败后进入下一次 attempt 前是否删除当前 cacheMode 对应轨迹缓存。"
            "env: AI_PHONE_RUN_RETRY_CLEAR_CACHE"
        ),
    )
    run_retry_cooldown_sec: float = Field(
        default=2.0,
        ge=0.0,
        le=60.0,
        description=(
            "失败 attempt 和下一次 attempt 之间的冷却秒数。"
            "env: AI_PHONE_RUN_RETRY_COOLDOWN_SEC"
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
    # 页面稳定检测：VLM 主通道与缓存回放通道共用同一个稳定函数，但配置完全独立。
    # 关闭某个通道后，函数只抓一张当前截图并直接放行，不再做 pHash 轮询；
    # 业务流程不会中断，但截图可能处于动画中间态。
    vlm_page_stable_enabled: bool = Field(
        default=True,
        description=(
            "VLM 主通道页面稳定检测开关。False=直接截图放行，不做像素哈希等待。"
            "env: AI_PHONE_VLM_PAGE_STABLE_ENABLED"
        ),
    )
    vlm_page_stable_timeout_s: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="VLM 主通道页面稳定检测总超时（秒）。env: AI_PHONE_VLM_PAGE_STABLE_TIMEOUT_S",
    )
    vlm_page_stable_poll_s: float = Field(
        default=0.4,
        ge=0.1,
        le=10.0,
        description="VLM 主通道页面稳定检测轮询间隔（秒）。env: AI_PHONE_VLM_PAGE_STABLE_POLL_S",
    )
    vlm_page_stable_threshold: float = Field(
        default=0.04,
        ge=0.0,
        le=1.0,
        description="VLM 主通道页面稳定检测 pHash 变化率阈值。env: AI_PHONE_VLM_PAGE_STABLE_THRESHOLD",
    )
    trajectory_cache_page_stable_enabled: bool = Field(
        default=True,
        description=(
            "轨迹缓存回放通道页面稳定检测开关。"
            "env: AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_ENABLED"
        ),
    )
    trajectory_cache_page_stable_timeout_s: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description=(
            "轨迹缓存回放通道页面稳定检测总超时（秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_TIMEOUT_S"
        ),
    )
    trajectory_cache_page_stable_poll_s: float = Field(
        default=0.4,
        ge=0.1,
        le=10.0,
        description=(
            "轨迹缓存回放通道页面稳定检测轮询间隔（秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_POLL_S"
        ),
    )
    trajectory_cache_page_stable_threshold: float = Field(
        default=0.04,
        ge=0.0,
        le=1.0,
        description=(
            "轨迹缓存回放通道页面稳定检测 pHash 变化率阈值。"
            "env: AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_THRESHOLD"
        ),
    )
    trajectory_cache_observe_delay_ms: int = Field(
        default=500,
        ge=0,
        le=10_000,
        description=(
            "轨迹缓存回放 action 执行后、下一次页面稳定检测前的基础观察延迟（毫秒）。"
            "用于避开点击反馈/动画早期帧；0=关闭。"
            "env: AI_PHONE_TRAJECTORY_CACHE_OBSERVE_DELAY_MS"
        ),
    )
    trajectory_cache_alignment_enabled: bool = Field(
        default=False,
        description=(
            "轨迹缓存回放整页状态路标对齐开关。False=保持 v1 稳定检测回放。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_ENABLED"
        ),
    )
    trajectory_cache_alignment_threshold: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        description=(
            "轨迹缓存状态路标 pHash diff 阈值，越小越严格。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_THRESHOLD"
        ),
    )
    trajectory_cache_alignment_roi_threshold: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description=(
            "轨迹缓存状态路标中心 ROI 像素差阈值，越小越严格。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_ROI_THRESHOLD"
        ),
    )
    trajectory_cache_alignment_black_ratio_threshold: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description=(
            "轨迹缓存状态路标黑屏比例差异阈值，越小越严格。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_BLACK_RATIO_THRESHOLD"
        ),
    )
    trajectory_cache_alignment_retry_interval_ms: int = Field(
        default=300,
        ge=50,
        le=10_000,
        description=(
            "轨迹缓存状态路标 MISS 后的重试间隔（毫秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_RETRY_INTERVAL_MS"
        ),
    )
    trajectory_cache_alignment_min_wait_ms: int = Field(
        default=1000,
        ge=0,
        le=60_000,
        description=(
            "轨迹缓存状态路标 MISS 后最小等待窗口（毫秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_MIN_WAIT_MS"
        ),
    )
    trajectory_cache_alignment_max_wait_ratio: float = Field(
        default=1.3,
        ge=0.1,
        le=10.0,
        description=(
            "轨迹缓存状态路标等待窗口相对首跑 gap_to_next_action_ms 的放大系数。"
            "env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_MAX_WAIT_RATIO"
        ),
    )
    # ------------------------------------------------------------------
    # v2 缓存回放 · recovery_vlm 三态裁决专线
    # ------------------------------------------------------------------
    # 通道独立：与辅助系统 / 断言系统 / 主 VLM 完全分离，所有字段都不 fallback。
    # 典型用法是把 backend/url/key/model 填成与主 VLM 同款；也可以单独换成
    # chat completions 端点做实验。
    trajectory_cache_recovery_vlm_enabled: bool = Field(
        default=False,
        description=(
            "v2 缓存回放 alignment_miss 后的 VLM 三态裁决专线开关。"
            "False=维持当前行为（alignment 等待窗口耗尽直接 assert_fail）。"
            "True 时还需配齐 backend / api_url / api_key / model 才会真正生效，"
            "任何一项缺失都会按 ASSERT_FAIL 兜底。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_ENABLED"
        ),
    )
    trajectory_cache_recovery_vlm_backend: str = Field(
        default="doubao_responses",
        description=(
            "recovery_vlm 后端协议。支持 'doubao_responses'（可直接复用主 VLM responses "
            "配置）/ 'openai_compatible'（豆包/OpenAI 兼容 chat completions）/ "
            "'openai_responses' / 'claude_messages'。当主 VLM 为 claude_cu/gpt_cu 时，V2 recovery "
            "会优先复用主 VLM Computer Use 配置，本字段仅作为非 CU/历史兼容路径。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_BACKEND"
        ),
    )
    trajectory_cache_recovery_vlm_api_url: str = Field(
        default="",
        description=(
            "recovery_vlm 接口地址。doubao_responses 可填方舟 /responses；openai_compatible "
            "可填 chat completions。留空 = 通道未配置。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_API_URL"
        ),
    )
    trajectory_cache_recovery_vlm_api_key: str = Field(
        default="",
        description=(
            "recovery_vlm api key。留空 = 通道未配置。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_API_KEY"
        ),
    )
    trajectory_cache_recovery_vlm_model: str = Field(
        default="",
        description=(
            "recovery_vlm 模型 ID。建议与主 vlm_model 同款，协议由 backend 决定。"
            "留空 = 通道未配置。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MODEL"
        ),
    )
    trajectory_cache_recovery_vlm_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description=(
            "recovery_vlm 单次调用超时（秒）。超时按 ASSERT_FAIL 兜底，不阻塞 Run 收尾。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_TIMEOUT_SEC"
        ),
    )
    trajectory_cache_recovery_vlm_wait_more_ms: int = Field(
        default=1500,
        ge=100,
        le=10_000,
        description=(
            "recovery_vlm 判 WAIT_MORE 时默认等待时长（毫秒）。"
            "VLM 响应里的数字若在 100-10_000 范围内会覆盖该默认值。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_WAIT_MORE_MS"
        ),
    )
    trajectory_cache_recovery_vlm_max_wait_more: int = Field(
        default=1,
        ge=0,
        le=5,
        description=(
            "recovery_vlm 在单个 alignment 周期内最多接受多少次 WAIT_MORE。"
            "0=不允许 WAIT_MORE，超过即降级 ASSERT_FAIL。第一版默认 1，最保守。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MAX_WAIT_MORE"
        ),
    )
    trajectory_cache_recovery_vlm_max_repair_actions: int = Field(
        default=5,
        ge=0,
        le=20,
        description=(
            "recovery_vlm 在单个 alignment 周期内最多执行多少个局部修复 action。"
            "0=不允许修复动作，只允许 finished/wait/assert_fail。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MAX_REPAIR_ACTIONS"
        ),
    )
    trajectory_cache_recovery_vlm_max_calls_per_replay: int = Field(
        default=5,
        ge=0,
        le=50,
        description=(
            "单条缓存回放最多允许召唤 recovery_vlm 多少次。"
            "超过说明该 case/cache 健康度不足，直接失败，避免整条 run 被反复救场拖慢。"
            "0=不允许调用 recovery_vlm。"
            "env: AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MAX_CALLS_PER_REPLAY"
        ),
    )
    # ------------------------------------------------------------------
    # v3 语义轨迹回放 · 坐标定位 / 救场专线
    # ------------------------------------------------------------------
    trajectory_cache_v3_coord_enabled: bool = Field(
        default=True,
        description=(
            "V3 plan_intent 坐标定位专线开关。默认开启。定位层只做单图短 prompt "
            "坐标询问；当主 VLM 为 claude_cu/gpt_cu 时默认复用主 VLM Computer "
            "Use 能力配置，但使用一次性短会话，不携带主 Run 上下文。其它路径默认"
            "复用 recovery_vlm，或通过 V3_COORD_USE_RECOVERY_VLM_CONFIG=false "
            "单独配置历史兼容定位模型。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_ENABLED"
        ),
    )
    trajectory_cache_v3_coord_use_recovery_vlm_config: bool = Field(
        default=True,
        description=(
            "V3 coord 是否复用 recovery_vlm 的 backend/url/key/model。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_USE_RECOVERY_VLM_CONFIG"
        ),
    )
    trajectory_cache_v3_coord_backend: str = Field(
        default="doubao_responses",
        description=(
            "V3 coord 非 CU/历史兼容后端协议。claude_cu/gpt_cu 主链路不会读取此字段；"
            "仅在非 CU 路径且 V3_COORD_USE_RECOVERY_VLM_CONFIG=false 时使用。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_BACKEND"
        ),
    )
    trajectory_cache_v3_coord_api_url: str = Field(
        default="",
        description=(
            "V3 coord 非 CU/历史兼容接口地址。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_API_URL"
        ),
    )
    trajectory_cache_v3_coord_api_key: str = Field(
        default="",
        description=(
            "V3 coord 非 CU/历史兼容 API key。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_API_KEY"
        ),
    )
    trajectory_cache_v3_coord_model: str = Field(
        default="",
        description=(
            "V3 coord 非 CU/历史兼容模型 ID。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_MODEL"
        ),
    )
    trajectory_cache_v3_coord_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description=(
            "V3 coord 单次调用超时（秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_TIMEOUT_SEC"
        ),
    )
    trajectory_cache_v3_coord_claude_thinking_budget: int = Field(
        default=0,
        ge=0,
        le=64000,
        description=(
            "V3 coord 使用 claude_cu 主链路定位时的 thinking token 预算。"
            "默认 0=关闭 thinking，只做短 prompt 坐标定位。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_CLAUDE_THINKING_BUDGET"
        ),
    )
    trajectory_cache_v3_coord_gpt_reasoning_effort: str = Field(
        default="low",
        description=(
            "V3 coord 使用 gpt_cu 主链路定位时的 reasoning effort：low/medium/high。"
            "默认 low，只做短 prompt 坐标定位。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_COORD_GPT_REASONING_EFFORT"
        ),
    )
    trajectory_cache_v3_stable_threshold: float = Field(
        default=0.08,
        ge=0.0,
        le=1.0,
        description=(
            "V3 回放专用稳定检测 global pHash diff 阈值。独立于 V2 alignment。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_STABLE_THRESHOLD"
        ),
    )
    trajectory_cache_v3_stable_roi_threshold: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "V3 回放专用稳定检测中心 ROI 像素差阈值。独立于 V2 alignment。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_STABLE_ROI_THRESHOLD"
        ),
    )
    trajectory_cache_v3_stable_black_ratio_threshold: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description=(
            "V3 回放专用稳定检测黑屏比例差异阈值。独立于 V2 alignment。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_STABLE_BLACK_RATIO_THRESHOLD"
        ),
    )
    trajectory_cache_v3_rescue_enabled: bool = Field(
        default=True,
        description=(
            "V3 coord 未定位后的救场 VLM 开关。当主 VLM 为 claude_cu/gpt_cu 时优先"
            "复用主 VLM Computer Use 配置；其它路径默认复用 recovery_vlm 连接配置。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_ENABLED"
        ),
    )
    trajectory_cache_v3_rescue_use_recovery_vlm_config: bool = Field(
        default=True,
        description=(
            "V3 rescue 是否复用 recovery_vlm 的 backend/url/key/model。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_USE_RECOVERY_VLM_CONFIG"
        ),
    )
    trajectory_cache_v3_rescue_backend: str = Field(
        default="doubao_responses",
        description=(
            "V3 rescue 独立后端协议。仅在 V3_RESCUE_USE_RECOVERY_VLM_CONFIG=false 时使用。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_BACKEND"
        ),
    )
    trajectory_cache_v3_rescue_api_url: str = Field(
        default="",
        description=(
            "V3 rescue 独立接口地址。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_API_URL"
        ),
    )
    trajectory_cache_v3_rescue_api_key: str = Field(
        default="",
        description=(
            "V3 rescue 独立 API key。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_API_KEY"
        ),
    )
    trajectory_cache_v3_rescue_model: str = Field(
        default="",
        description=(
            "V3 rescue 独立模型 ID。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_MODEL"
        ),
    )
    trajectory_cache_v3_rescue_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description=(
            "V3 rescue 单次调用超时（秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_TIMEOUT_SEC"
        ),
    )
    trajectory_cache_v3_rescue_max_calls_per_replay: int = Field(
        default=3,
        ge=0,
        le=20,
        description=(
            "单条 V3 回放最多允许调用 rescue VLM 多少次。"
            "env: AI_PHONE_TRAJECTORY_CACHE_V3_RESCUE_MAX_CALLS_PER_REPLAY"
        ),
    )
    # ------------------------------------------------------------------
    # v2 缓存回放 · 瞬态弹窗动作标记与按需回放
    # ------------------------------------------------------------------
    trajectory_cache_ephemeral_action_enabled: bool = Field(
        default=False,
        description=(
            "轨迹缓存 optional_ephemeral 动作总开关。False=不标记、不 gate，"
            "完全保持旧 V2 回放。env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_ACTION_ENABLED"
        ),
    )
    trajectory_cache_ephemeral_classify_enabled: bool = Field(
        default=True,
        description=(
            "成功轨迹保存阶段是否启用瞬态 action classifier。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFY_ENABLED"
        ),
    )
    trajectory_cache_ephemeral_classifier_backend: str = Field(
        default="openai_compatible",
        description=(
            "瞬态 action classifier 后端协议：doubao_responses / openai_compatible / "
            "openai_responses / claude_messages。classifier url/key/model 留空时会复用 ASSISTANT_* 并按 "
            "assistant_backend 自动映射。env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFIER_BACKEND"
        ),
    )
    trajectory_cache_ephemeral_classifier_api_url: str = Field(
        default="",
        description=(
            "瞬态 action classifier 接口地址。留空=复用 assistant_api_url。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFIER_API_URL"
        ),
    )
    trajectory_cache_ephemeral_classifier_api_key: str = Field(
        default="",
        description=(
            "瞬态 action classifier API key。留空=复用 assistant_api_key / vlm_api_key。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFIER_API_KEY"
        ),
    )
    trajectory_cache_ephemeral_classifier_model: str = Field(
        default="",
        description=(
            "瞬态 action classifier 模型 ID。留空=复用 assistant_model。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFIER_MODEL"
        ),
    )
    trajectory_cache_ephemeral_classifier_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description=(
            "瞬态 action classifier 单次调用超时（秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFIER_TIMEOUT_SEC"
        ),
    )
    trajectory_cache_ephemeral_classify_min_confidence: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "classifier 判 optional_ephemeral 的最低置信度；低于阈值一律 business_required。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_CLASSIFY_MIN_CONFIDENCE"
        ),
    )
    trajectory_cache_ephemeral_gate_enabled: bool = Field(
        default=True,
        description=(
            "回放遇到 optional_ephemeral action 时是否启用 gate。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_ENABLED"
        ),
    )
    trajectory_cache_ephemeral_gate_use_recovery_vlm_config: bool = Field(
        default=True,
        description=(
            "ephemeral gate 是否复用 recovery_vlm 的 backend/url/key/model 连接配置。"
            "只复用连接，不复用 prompt。当主 VLM 为 claude_cu/gpt_cu 时，V2 gate "
            "会优先复用主 VLM Computer Use 配置，本字段仅作为非 CU/历史兼容路径。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_USE_RECOVERY_VLM_CONFIG"
        ),
    )
    trajectory_cache_ephemeral_gate_backend: str = Field(
        default="openai_compatible",
        description=(
            "ephemeral gate 独立后端协议。仅在 GATE_USE_RECOVERY_VLM_CONFIG=false 时使用。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_BACKEND"
        ),
    )
    trajectory_cache_ephemeral_gate_api_url: str = Field(
        default="",
        description=(
            "ephemeral gate 独立接口地址。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_API_URL"
        ),
    )
    trajectory_cache_ephemeral_gate_api_key: str = Field(
        default="",
        description=(
            "ephemeral gate 独立 API key。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_API_KEY"
        ),
    )
    trajectory_cache_ephemeral_gate_model: str = Field(
        default="",
        description=(
            "ephemeral gate 独立模型 ID。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_MODEL"
        ),
    )
    trajectory_cache_ephemeral_gate_timeout_sec: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description=(
            "ephemeral gate 独立通道单次调用超时（秒）。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_TIMEOUT_SEC"
        ),
    )
    trajectory_cache_ephemeral_gate_max_calls: int = Field(
        default=3,
        ge=0,
        le=50,
        description=(
            "单条缓存回放最多允许调用 ephemeral gate 多少次。"
            "env: AI_PHONE_TRAJECTORY_CACHE_EPHEMERAL_GATE_MAX_CALLS"
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
        default=2,
        ge=2,
        le=20,
        description=(
            "同坐标连续点击触发卡死提示的次数。点过 N 次同位置仍无屏变化 → 注入提示。"
            "历史默认 4 在「VLM 第 1-3 次反复点已满足按钮」期间无任何干预，配合"
            "强制判读句协议（shared/prompt.py substeps_block）从 4 → 2，第 2 次"
            "同位置就立即注入「目标可能已满足」提示，作为强制判读句的兜底。"
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
        default=10,
        ge=2,
        le=20,
        description=(
            "同坐标桶累计 click ≥ N 次召唤审判。"
            "调大允许更多合法重试；调小快速发现反复点同一处模式。"
            "历史默认 3 在长 case / 多视觉证据复点场景偏严，调到 10。"
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
        default=10,
        ge=2,
        le=20,
        description=(
            "同屏访问累计 ≥ N 次召唤审判。Tab 切换 / 抽屉开合等合法多次访问可调大。"
            "历史默认 3 在'同一页面找多个视觉证据'场景偏严，调到 10。"
            "env: AI_PHONE_AUDIT_SCREEN_REVISIT_TRIGGER"
        ),
    )
    audit_scroll_flip_window: int = Field(
        default=10,
        ge=2,
        le=30,
        description=(
            "滚动方向翻转检测窗口（最近 N 次滚动）。"
            "env: AI_PHONE_AUDIT_SCROLL_FLIP_WINDOW"
        ),
    )
    audit_scroll_flip_trigger: int = Field(
        default=6,
        ge=1,
        le=10,
        description=(
            "窗口内方向翻转 ≥ N 次召唤审判（震荡 / 东找西找）。"
            "长列表上下找东西天然有方向变化，历史默认 2 偏严，调到 6。"
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
        default=10,
        ge=2,
        le=20,
        description=(
            "同方向连续无效滑动 ≥ N 次召唤审判（已到列表底/无更多内容场景）。"
            "长页面翻 6-7 次找内容是正常行为，历史默认 3 偏严，调到 10。"
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
        default=10,
        ge=1,
        le=10,
        description=(
            "严格度综合评分 ≥ 此值直接走结构化（0-7 分制）。"
            "默认 10 表示关闭评分入口，只保留标签命中入口。"
            "env: AI_PHONE_STRUCT_STRICTNESS_HARD_SCORE"
        ),
    )
    struct_strictness_audit_score: int = Field(
        default=10,
        ge=1,
        le=10,
        description=(
            "严格度评分 ≥ 此值借审判模型分类（中等信号 case）。"
            "默认 10 表示关闭评分审判入口，只保留标签命中入口。"
            "env: AI_PHONE_STRUCT_STRICTNESS_AUDIT_SCORE"
        ),
    )


# ===========================================================================
# 配置三分区（Distributed Agent Brain）
# ===========================================================================
# 单仓库、server/agent 同一份代码、不同命令启动 —— Settings 仍是单类，但用下面
# 三个清单把"谁该读、谁控制"的边界显式钉死，避免平铺大类带来的职责模糊。
#
#   - AGENT_LOCAL_FIELDS : Agent 本机物理必需（Server 替不了），不下发；Agent 读
#                          本机 env。改这些在 Agent 机器本地生效。
#   - SERVER_ONLY_FIELDS : Server 基础设施 / 敏感 / 调度 / 通用，**绝不下发**给
#                          Agent（含 db_url、kafka 密码、内部 token 等）。
#   - 下发集（可下发）    : = 全部字段 − AGENT_LOCAL − SERVER_ONLY。Server 控制、
#                          下发给 Agent 覆盖。新增执行类字段自动纳入下发。
#
# 单机部署（server+agent 同一 .env）下三分区无感知差异；多机部署下 Agent 机器只需
# 配 AGENT_LOCAL 那几个，其余执行配置由 Server 下发覆盖。
#
# 新版模型 ENV 的归属：
#   - PHONE_VLM 四项（provider/base_url/api_key/model）：必须下发并覆盖；缺任一项直接报错。
#   - AUX 四项（provider/base_url/api_key/model）：必须下发并覆盖；缺任一项直接报错。
#   - 派生出来的 vlm_* / assistant_* / trajectory_cache_*_vlm_*：同属下发集，由 Server
#     基于 PHONE_VLM/AUX 统一派生后下发，Agent 本机残留不参与兜底。

# Agent 本机物理必需（代码证据：连接四元组 + iOS WDA 本机签名/路径/端口）
AGENT_LOCAL_FIELDS: frozenset[str] = frozenset({
    # 连接 / 身份（Agent 启动连 Server 前就要用）
    "server_ws_url",
    "server_http_base",
    "agent_token",
    "agent_name",
    # iOS WDA 本机签名 / 工程 / 端口（每台 Mac 客观不同，Server 无法统一）
    "wda_project_dir",
    "wda_scheme",
    "wda_bundle_id",
    "wda_team_id",
    "wda_local_port",
    "wda_mjpeg_device_port",
    # 本机文件系统路径
    "storage_dir",
    "midscene_bridge_dir",
    "midscene_node_bin",
})
# 注：Android 虚拟机行为参数（android_vm_*）不在此列——它们是 Server 强控制的执行
# 行为，纳入下发集统一下发。容量自适应由 capability.probe 按实时可用内存兜底。

# Server 专属 / 敏感 / 调度 / 通用：绝不下发给 Agent
SERVER_ONLY_FIELDS: frozenset[str] = frozenset({
    # 通用（server / agent 各读各的本机，不下发）
    "env",
    "log_level",
    # Server 基础设施
    "db_url",
    "server_host",
    "server_port",
    "cors_origins",
    # 广播 / 消息队列（含敏感密码）
    "broadcast_backend",
    "kafka_brokers",
    "kafka_topic",
    "kafka_sasl_username",
    "kafka_sasl_password",
    # 对外 / 内部 token（敏感）
    "submission_internal_token",
    "submission_external_retention_days",
    # 大盘 analytics（Server 端）
    "analytics_timezone",
    "analytics_ai_max_age_days",
    "analytics_show_token",
    "analytics_show_stability",
    # 调度（Server 进程用，Agent 不读）
    "submission_ttl_sec",
    "item_ttl_sec",
    "scheduler_tick_sec",
    "orphan_reap_grace_sec",
    "run_retry_enabled",
    "run_retry_max",
    "run_retry_clear_cache",
    "run_retry_cooldown_sec",
})


def downlink_field_names() -> frozenset[str]:
    """可下发字段集 = 全部 Settings 字段 − 本机保留 − Server 专属。"""
    all_fields = set(Settings.model_fields.keys())
    return frozenset(all_fields - AGENT_LOCAL_FIELDS - SERVER_ONLY_FIELDS)


# 执行配置由 Server 集中控制：普通字段空串也要下发 / 覆盖，用来清理 Agent 本机残留。
# 新版 PHONE_VLM/AUX 缺项时直接报错，不再保留旧式 VLM_* 作为本机兜底。
_PHONE_VLM_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("phone_vlm_provider", "AI_PHONE_PHONE_VLM_PROVIDER"),
    ("phone_vlm_base_url", "AI_PHONE_PHONE_VLM_BASE_URL"),
    ("phone_vlm_api_key", "AI_PHONE_PHONE_VLM_API_KEY"),
    ("phone_vlm_model", "AI_PHONE_PHONE_VLM_MODEL"),
)

_AUX_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("aux_provider", "AI_PHONE_AUX_PROVIDER"),
    ("aux_base_url", "AI_PHONE_AUX_BASE_URL"),
    ("aux_api_key", "AI_PHONE_AUX_API_KEY"),
    ("aux_model", "AI_PHONE_AUX_MODEL"),
)


def _missing_required_fields(
    values: object,
    fields: tuple[tuple[str, str], ...],
) -> list[str]:
    missing: list[str] = []
    for field_name, env_name in fields:
        if isinstance(values, dict):
            raw = values.get(field_name)
        else:
            raw = getattr(values, field_name, None)
        if not str(raw or "").strip():
            missing.append(env_name)
    return missing


def _missing_phone_vlm_fields(values: object) -> list[str]:
    return _missing_required_fields(values, _PHONE_VLM_REQUIRED_FIELDS)


def _missing_aux_fields(values: object) -> list[str]:
    return _missing_required_fields(values, _AUX_REQUIRED_FIELDS)


def _strip_endpoint_suffix(url: str) -> str:
    """把可能带 ``/responses`` 或 ``/chat/completions`` 的地址归一成 API 根。

    新版只让用户填 base_url（到 ``/api/v3``）；但容忍用户误填完整 endpoint，
    自动剥掉已知后缀，保证内部能干净地拼出两个端点。
    """
    raw = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if raw.endswith(suffix):
            return raw[: -len(suffix)].rstrip("/")
    return raw


def _normalize_model_provider(provider: str, *, default: str) -> str:
    value = (provider or "").strip().lower().replace("-", "_")
    if not value:
        value = default
    if value in {"doubao", "ark", "volcengine", "volc"}:
        return "doubao"
    if value in {"claude", "anthropic"}:
        return "claude"
    if value in {"openai", "gpt", "gpt_cu"}:
        return "openai"
    return value


def _anthropic_messages_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    if raw.endswith("/messages"):
        return raw
    if raw.endswith("/v1"):
        return f"{raw}/messages"
    return f"{raw}/v1/messages"


def _openai_responses_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    if raw.endswith("/responses"):
        return raw
    if raw.endswith("/chat/completions"):
        return raw[: -len("/chat/completions")].rstrip("/") + "/responses"
    if raw.endswith("/v1"):
        return f"{raw}/responses"
    return f"{raw}/v1/responses"


def _openai_chat_completions_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    if raw.endswith("/chat/completions"):
        return raw
    if raw.endswith("/responses"):
        return raw[: -len("/responses")].rstrip("/") + "/chat/completions"
    if raw.endswith("/v1"):
        return f"{raw}/chat/completions"
    return f"{raw}/v1/chat/completions"


def _derive_aux_model_config(settings: "Settings") -> dict:
    missing = _missing_aux_fields(settings)
    if missing:
        raise RuntimeError(
            "新版 AUX 配置缺失，无法派生辅助模型执行链路："
            + ", ".join(missing)
            + "。请配置 AI_PHONE_AUX_*；辅助模型必须显式配置，不再跟随 PHONE_VLM。"
        )
    provider = _normalize_model_provider(
        getattr(settings, "aux_provider", ""),
        default="",
    )
    base_url = (getattr(settings, "aux_base_url", "") or "").strip()
    api_key = (getattr(settings, "aux_api_key", "") or "").strip()
    model = (getattr(settings, "aux_model", "") or "").strip()

    if provider == "doubao":
        aux_base = _strip_endpoint_suffix(base_url)
        assistant_backend = "doubao_chat"
        assistant_api_url = f"{aux_base}/chat/completions" if aux_base else ""
        classifier_backend = "openai_compatible"
    elif provider == "claude":
        assistant_backend = "claude"
        assistant_api_url = _anthropic_messages_url(base_url)
        classifier_backend = "claude_messages"
    elif provider == "openai":
        assistant_backend = "openai"
        assistant_api_url = _openai_chat_completions_url(base_url)
        classifier_backend = "openai_compatible"
    else:
        raise RuntimeError(
            "AI_PHONE_AUX_PROVIDER 只支持 doubao / claude / openai，"
            f"当前为 {provider!r}"
        )

    return {
        "assistant_backend": assistant_backend,
        "assistant_api_url": assistant_api_url,
        "assistant_api_key": api_key,
        "assistant_model": model,
        "trajectory_cache_ephemeral_classifier_backend": classifier_backend,
        "trajectory_cache_ephemeral_classifier_api_url": assistant_api_url,
        "trajectory_cache_ephemeral_classifier_api_key": api_key,
        "trajectory_cache_ephemeral_classifier_model": model,
    }


def _derive_new_model_config(settings: "Settings") -> "Settings":
    """新版两块配置（``phone_vlm_*`` / ``aux_*``）派生覆盖内部连接字段。

    新模式对外只暴露 PHONE_VLM / AUX 两块，内部按 provider 和“形态”自动拆协议：

    - doubao：主决策 ``doubao_responses``；手机层单次 ``openai_compatible``。
    - claude：主决策保持已验证的 ``claude_cu``；手机层单次 ``claude_messages``。
    - openai/gpt：主决策 ``gpt_cu``；手机层单次 ``openai_responses``。
    - AUX：按 ``aux_provider`` 分发到 ``doubao_chat`` / ``claude`` / ``openai``；
      瞬态分类跟随 AUX，但只走单次视觉判断协议。

    ``phone_vlm_provider`` + ``phone_vlm_base_url`` + ``phone_vlm_api_key`` +
    ``phone_vlm_model`` 四项必填；缺项直接报错，不再回退旧式 ``VLM_*``。
    ``aux_provider`` + ``aux_base_url`` + ``aux_api_key`` + ``aux_model``
    四项也必填；辅助模型不跟随主模型，避免海外 CU 主模型被误用成通用辅助模型。
    """
    missing = _missing_phone_vlm_fields(settings)
    if missing:
        raise RuntimeError(
            "新版 PHONE_VLM 配置缺失，无法派生模型执行链路："
            + ", ".join(missing)
            + "。请配置 AI_PHONE_PHONE_VLM_*；不再回退旧式 AI_PHONE_VLM_*。"
        )
    missing = _missing_aux_fields(settings)
    if missing:
        raise RuntimeError(
            "新版 AUX 配置缺失，无法派生辅助模型执行链路："
            + ", ".join(missing)
            + "。请配置 AI_PHONE_AUX_*；辅助模型必须显式配置，不再跟随 PHONE_VLM。"
        )
    raw_base = (getattr(settings, "phone_vlm_base_url", "") or "").strip()
    key = (getattr(settings, "phone_vlm_api_key", "") or "").strip()
    model = (getattr(settings, "phone_vlm_model", "") or "").strip()

    provider = _normalize_model_provider(
        getattr(settings, "phone_vlm_provider", ""),
        default="",
    )

    if provider == "doubao":
        base = _strip_endpoint_suffix(raw_base)
        chat_url = f"{base}/chat/completions"
        update = {
            # 形态 1 · 主 VLM（碰手机 · responses 续接 + 主动缓存）
            "vlm_backend": "doubao_responses",
            "vlm_api_url": f"{base}/responses",
            "vlm_chat_api_url": chat_url,
            "vlm_api_key": key,
            "vlm_model": model,
            # 形态 2 · 辅 VLM 恢复（碰手机 · chat 单次）；v3 / gate 跟随它
            "trajectory_cache_recovery_vlm_backend": "openai_compatible",
            "trajectory_cache_recovery_vlm_api_url": chat_url,
            "trajectory_cache_recovery_vlm_api_key": key,
            "trajectory_cache_recovery_vlm_model": model,
            "trajectory_cache_v3_coord_use_recovery_vlm_config": True,
            "trajectory_cache_v3_rescue_use_recovery_vlm_config": True,
            "trajectory_cache_ephemeral_gate_use_recovery_vlm_config": True,
        }
        update.update(_derive_aux_model_config(settings))
        return settings.model_copy(update=update)

    if provider == "claude":
        messages_url = _anthropic_messages_url(raw_base)
        update = {
            "vlm_backend": "claude_cu",
            "vlm_api_url": messages_url,
            "vlm_chat_api_url": messages_url,
            "vlm_api_key": key,
            "vlm_model": model,
            "trajectory_cache_recovery_vlm_backend": "claude_messages",
            "trajectory_cache_recovery_vlm_api_url": messages_url,
            "trajectory_cache_recovery_vlm_api_key": key,
            "trajectory_cache_recovery_vlm_model": model,
            "trajectory_cache_v3_coord_use_recovery_vlm_config": True,
            "trajectory_cache_v3_rescue_use_recovery_vlm_config": True,
            "trajectory_cache_ephemeral_gate_use_recovery_vlm_config": True,
        }
        update.update(_derive_aux_model_config(settings))
        return settings.model_copy(update=update)

    if provider == "openai":
        responses_url = _openai_responses_url(raw_base)
        update = {
            "vlm_backend": "gpt_cu",
            "vlm_api_url": responses_url,
            "vlm_chat_api_url": _openai_chat_completions_url(raw_base),
            "vlm_api_key": key,
            "vlm_model": model,
            "trajectory_cache_recovery_vlm_backend": "openai_responses",
            "trajectory_cache_recovery_vlm_api_url": responses_url,
            "trajectory_cache_recovery_vlm_api_key": key,
            "trajectory_cache_recovery_vlm_model": model,
            "trajectory_cache_v3_coord_use_recovery_vlm_config": True,
            "trajectory_cache_v3_rescue_use_recovery_vlm_config": True,
            "trajectory_cache_ephemeral_gate_use_recovery_vlm_config": True,
        }
        update.update(_derive_aux_model_config(settings))
        return settings.model_copy(update=update)

    raise RuntimeError(
        "AI_PHONE_PHONE_VLM_PROVIDER 只支持 doubao / claude / openai，"
        f"当前为 {provider!r}"
    )


def _maybe_derive_new_model_config(settings: "Settings") -> "Settings":
    """本机启动期宽松派生：PHONE_VLM/AUX 齐全就派生，不齐先保留基础配置。

    Agent 需要先读取 server_ws_url / token 才能连上 Server 拿下发配置，所以启动期
    不能因为本机没有模型 key 直接退出；真正执行配置由 build_downlink_config /
    set_runtime_override 严格校验，不允许走本机旧式模型字段。
    """
    if _missing_phone_vlm_fields(settings) or _missing_aux_fields(settings):
        return settings
    return _derive_new_model_config(settings)


def build_downlink_config(settings: "Settings | None" = None) -> dict:
    """Server 端：从 Settings 抽出"可下发执行配置"快照（纯 JSON 可序列化）。

    供 agent_ws 在 Agent 连接后下发。只含下发集字段，绝不含 AGENT_LOCAL /
    SERVER_ONLY（即不含 db_url、kafka 密码、内部 token、Agent 本机签名等）。
    """
    cfg = _derive_new_model_config(settings or _base_settings())
    out: dict = {}
    for name in downlink_field_names():
        value = getattr(cfg, name, None)
        # Path 等非 JSON 原生类型转成字符串，保证可序列化
        if isinstance(value, Path):
            value = str(value)
        # None 不下发（Optional 字段未设）；空串照常下发，用来清理 Agent 本机残留。
        if value is None:
            continue
        out[name] = value
    return out


# 运行时配置覆盖：Agent 收到 Server 下发的配置后设置，使全进程 get_settings()
# 返回"被 Server 下发值覆盖过"的 Settings。Server 端不设置，始终走本机。
_runtime_override: "Settings | None" = None


@lru_cache(maxsize=1)
def _base_settings() -> Settings:
    """本机 .env 解析出的基线 Settings（进程级单例）。

    启动期只做宽松派生：PHONE_VLM/AUX 齐全就覆盖内部连接字段；不齐则先保留基础配置，
    让 Agent 能连上 Server 拉取集中下发。Server 下发 / Agent 应用下发时会严格校验
    PHONE_VLM/AUX，缺项直接失败。
    """
    return _maybe_derive_new_model_config(Settings())


def get_settings() -> Settings:
    """获取进程级配置。

    若 Agent 通过 :func:`set_runtime_override` 设过 Server 下发的配置，则返回覆盖
    后的版本；否则返回本机 .env 基线。Server 端从不 override，始终读本机。
    """
    if _runtime_override is not None:
        return _runtime_override
    return _base_settings()


# 兼容历史调用 ``get_settings.cache_clear()``（测试 / 热重载用）。
get_settings.cache_clear = _base_settings.cache_clear  # type: ignore[attr-defined]


def set_runtime_override(config: dict | None) -> Settings:
    """Agent 端：用 Server 下发的执行配置覆盖本机 Settings（配置集中分发）。

    只接受下发集字段（AGENT_LOCAL / SERVER_ONLY 即便混进来也被丢弃，双保险确保
    Agent 的连接 / 签名 / 本机路径永不被 Server 覆盖）。返回覆盖后的 Settings。
    config 为空时清除覆盖、回退本机基线。配置全局一份，非 per-run 快照。
    """
    global _runtime_override
    snapshot = config
    if not snapshot:
        _runtime_override = None
        return get_settings()
    allowed_names = downlink_field_names()
    # None 一律不覆盖；空串照常覆盖，用来清理 Agent 本机残留。
    update: dict = {}
    for k, v in snapshot.items():
        if k not in allowed_names or v is None:
            continue
        update[k] = v
    missing = _missing_phone_vlm_fields(update) + _missing_aux_fields(update)
    if missing:
        raise RuntimeError(
            "Server 下发缺少新版模型配置："
            + ", ".join(missing)
            + "。Agent 不再使用本机旧式 VLM_* 或本机 AUX 兜底。"
        )
    # 先合并 Server 下发值，再严格派生内部连接字段；不接受旧式 legacy 下发。
    _runtime_override = _derive_new_model_config(
        _base_settings().model_copy(update=update)
    )
    return _runtime_override


def clear_runtime_override() -> None:
    """清除运行时覆盖，回退本机基线（测试 / Agent 断连复位用）。"""
    global _runtime_override
    _runtime_override = None


def has_runtime_override() -> bool:
    """是否已应用 Server 下发的配置覆盖。

    vlm_loop 用它决定要不要把 import 期固化的模块级阈值常量刷新成下发值：
    仅生产 Agent（收到下发）刷新；测试环境（无 override）保持模块常量原值 /
    monkeypatch 值，不受影响。
    """
    return _runtime_override is not None
