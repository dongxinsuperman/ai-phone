"""VLM 主循环（迁移自 Groovy `vlmAgentRun`）。

Runner 与外界的唯一耦合是两个回调：
  1. ``driver``：实现 :class:`BaseDriver` 的任意对象（当前只有 Android，iOS 后续 M3）
  2. ``emit(event)``：消费事件，事件是 JSON-safe dict，可以对接 WS / 控制台 / 测试断言

所以 runner 本身纯同步逻辑 + asyncio，不依赖 FastAPI / DB / WS 实现，可以单独
跑脚本验证，也能无缝塞进 Agent 的 WS 上下文。

已覆盖 Groovy 行为：
- 稳定检测 + 复用上步尾帧（pixel 模式）
- 卡死检测：点击连续 4 次相同坐标 / 滚动连续 3 次同方向
- 未知动作保护：连续 3 次无法识别则失败
- Thought 秒数兜底 + 连续长等防护
- Responses API + 显式缓存 + 会话分段（超阈值自动重置 previous_response_id
  + 注入续接提示），取代老版的 CONTEXT_TURNS 滑窗
- open_app / close_app 的"包名匹配 VLM 二次调用"（走 Chat API 端点）
- token 使用量统计（兼容 input_tokens / cached_tokens）
- 安全上限 SAFETY_MAX_STEPS=100
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from ai_phone.agent.drivers.base import BaseDriver
from ai_phone.config import get_settings
from ai_phone.shared import actions as A
from ai_phone.shared.llm import (
    BaseAssistant,
    BaseMainVLM,
    create_assistant,
    create_main_vlm,
)
from ai_phone.shared.llm.prompts import (
    build_system_prompt_for_backend,
    build_unknown_action_hint,
)
from ai_phone.shared.vlm import TokenCounter

from .events import (
    EVT_ACTION,
    EVT_EXEC_RESULT,
    EVT_RUN_FINISH,
    EVT_RUN_START,
    EVT_SCREENSHOT,
    EVT_STEP_END,
    EVT_STEP_START,
    EVT_THOUGHT,
    EVT_TOKEN_SUMMARY,
    log_event,
    make_event,
)
from .phash import compute_phash, hamming_distance
from .stability import wait_page_stable_pixel
from .transient_ui import (
    TransientUISnapshot,
    build_takeover_hint,
    detect_transient_ui,
    TRANSIENT_TAKEOVER_WAIT_MS,
)

# ---- 常量（开源后所有可调项已搬到 ai_phone.config.Settings）----
# 模块加载时一次性从 settings 拍下；改 .env 后重启 agent 生效。
# 保留模块级常量名是为：
#   1) 测试 monkeypatch 直接 setattr 用得上（test_vlm_runner.py）
#   2) 内部 1000+ 行引用不需要散改
#   3) 让"想调哪个就改哪个 env"的运维不用看代码就能上手（每个 env 见 config.py 注释）
_settings = get_settings()

# --- Run 行为硬上限（业务调优项） ---
SAFETY_MAX_STEPS = _settings.run_max_steps                                     # env: AI_PHONE_RUN_MAX_STEPS
MAX_WAIT_SECONDS = _settings.run_max_wait_sec                                   # env: AI_PHONE_RUN_MAX_WAIT_SEC
DEFAULT_WAIT_SECONDS = 3                                                        # 协议常量：VLM 不指定 wait 秒数时的默认值
POST_ACTION_SETTLE_MS = 500                                                     # 协议常量：动作执行后等待 UI 沉降的固定窗口

# --- 卡死检测阈值（误 kill 多发时调大） ---
CLICK_STUCK_THRESHOLD = _settings.click_stuck_threshold                         # env: AI_PHONE_CLICK_STUCK_THRESHOLD
SCROLL_STUCK_THRESHOLD = _settings.scroll_stuck_threshold                       # env: AI_PHONE_SCROLL_STUCK_THRESHOLD
UNKNOWN_ACTION_STREAK_LIMIT = _settings.unknown_action_streak_limit             # env: AI_PHONE_UNKNOWN_ACTION_STREAK_LIMIT
CONSECUTIVE_SCREENSHOT_FAIL_LIMIT = _settings.consecutive_screenshot_fail_limit  # env: AI_PHONE_CONSECUTIVE_SCREENSHOT_FAIL_LIMIT

# ---- 链式动作（同一轮 VLM 决策内串联多个 Action） ----
# 默认仍是"每步一个 Action"。仅当 VLM 需要操作"会自动隐藏的瞬态 UI"
# （视频播放工具栏 / Toast / 半透明菜单）时，可在同一 Thought 下输出 ≤ N 个
# Action 让系统在毫秒级间隔内顺序执行，跳过中间稳定检测/截图。
CHAIN_MAX_ACTIONS = _settings.chain_max_actions     # env: AI_PHONE_CHAIN_MAX_ACTIONS
CHAIN_INNER_GAP_MS = _settings.chain_inner_gap_ms   # env: AI_PHONE_CHAIN_INNER_GAP_MS
# 链式仅允许"无需中间反馈即可决断"的动作。VLM 在第 1 击前已经从截图看清目标
# 位置（坐标 / 起止点），整链在毫秒级窗口完成，不依赖中间帧。
#   - click / long_press / double_tap：瞬时点击类
#   - drag：拖拽类（driver.swipe ~500ms 完成，工具栏 2-3s 寿命窗内可达）
# scroll/type 因为依赖反馈（滑动后位置 / 输入后焦点）不准入链。
_CHAIN_ALLOWED_ACTIONS = frozenset({
    A.ACTION_CLICK, A.ACTION_LONG_PRESS, A.ACTION_DOUBLE_TAP, A.ACTION_DRAG,
})

# ---- 断言系统（finished 终局裁决）调用超时 ----
# 主 VLM 输出 finished() 后，系统会再调用一次 VLM 做终局裁决（带最终截图 +
# 主 VLM 的 thought / finish_msg），仅返回 PASS/FAIL/SKIP 三态。这一次属于
# "走完末班车"，不能因为网络抖动卡死整个 Run，给一个偏宽松的默认 60s 超时；
# 超时会按 SKIP 处理（回退采纳主 VLM 结果），不会阻塞 Run 收尾。
ASSERTION_SYSTEM_TIMEOUT_SECONDS = _settings.assertion_timeout_sec  # env: AI_PHONE_ASSERTION_TIMEOUT_SEC

# ---- 结构化 case 「触发审判」阈值 ----
# 设计原则：本地探测器只做"召唤"，**不直接 kill**。命中阈值后丢给独立的轻量
# 文本模型审判（复用 _match_package_name 的 chat 端点），由审判判定继续 / 终止。
# 这样既能允许"功能慢，多次合法重试"，又能在"VLM 跑去尝试 case 没提的别的
# 入口"时由审判一句话锁死，理由直接归咎为 case 描述不严谨，方便用户优化 case。
#
# 阈值取得相对宽松（比直接 kill 时大 1-2），因为多了一层审判兜底，可以容忍更
# 长的合法重试，又不至于让明显死循环拖太久。审判误 KILL 多发时把 trigger 调大。
STRUCT_CLICK_BUCKET_PX = _settings.audit_click_bucket_px                         # env: AI_PHONE_AUDIT_CLICK_BUCKET_PX
STRUCT_CLICK_BUCKET_TRIGGER = _settings.audit_click_bucket_trigger               # env: AI_PHONE_AUDIT_CLICK_BUCKET_TRIGGER
STRUCT_SCREEN_REVISIT_HAMMING = _settings.audit_screen_revisit_hamming           # env: AI_PHONE_AUDIT_SCREEN_REVISIT_HAMMING
STRUCT_SCREEN_REVISIT_TRIGGER = _settings.audit_screen_revisit_trigger           # env: AI_PHONE_AUDIT_SCREEN_REVISIT_TRIGGER
STRUCT_SCROLL_FLIP_WINDOW = _settings.audit_scroll_flip_window                   # env: AI_PHONE_AUDIT_SCROLL_FLIP_WINDOW
STRUCT_SCROLL_FLIP_TRIGGER = _settings.audit_scroll_flip_trigger                 # env: AI_PHONE_AUDIT_SCROLL_FLIP_TRIGGER
STRUCT_SCROLL_NOPROGRESS_DIFF = _settings.audit_scroll_noprogress_diff           # env: AI_PHONE_AUDIT_SCROLL_NOPROGRESS_DIFF
STRUCT_SCROLL_NOPROGRESS_TRIGGER = _settings.audit_scroll_noprogress_trigger     # env: AI_PHONE_AUDIT_SCROLL_NOPROGRESS_TRIGGER

# 审判相关
#
# 设计原则：**效果优先，经济（token / 调用次数 / 延迟）完全不考虑**。
# 任何"省 token / 省调用 / 截断历史"的折衷都已被全部解除——历史教训表明，
# 省下来的几毛钱 token 远不及一次错杀 case 的重跑成本。
# ALLOW 上限：审判放行多少次后，下次探测器召唤直接 KILL（绕过审判模型）。
# 取值历史：
#   - 10：长 case 多个独立合法慢点（网络慢/复杂表单/连续点同区域）累加易耗尽 → 误 KILL
#   - 30 (当前默认)：覆盖典型长 case 全程的合法重试需求；同时仍是"审判模型一直
#     被 VLM thought 骗放行"的兜底。第一道总闸是 MAX_WAIT_SECONDS（30 分钟超时）。
STRUCT_AUDIT_ALLOW_LIMIT = _settings.audit_allow_limit  # env: AI_PHONE_AUDIT_ALLOW_LIMIT
# HISTORY_LIMIT：喂给审判的最近动作步数。
# 历史教训：定 10 时长 case（>10 步）前期偏离会被吞掉。doubao-seed-1-6
# context 256K，100 步全量历史 ≈ 50K-150K tokens，离上限远；模型也未触发
# "lost in the middle"（每条都是结构化短文本，不是大块文档）。
# 这个值不开放 env：调小直接降低审判判断质量，没有运维场景需要省这点 token。
STRUCT_AUDIT_HISTORY_LIMIT = 100
STRUCT_AUDIT_TIMEOUT_SECONDS = _settings.audit_timeout_sec  # env: AI_PHONE_AUDIT_TIMEOUT_SEC

# 周期巡检：兜底"VLM 一鼓作气走通了错误路线"——这种走得很顺的偏离，本地探测
# 器（同坐标反复点 / 屏幕重访 / 滚动震荡）抓不到，因为不存在"反复"模式。
# 解决：每 N 步主动召唤一次审判，让它对照 case 操作步骤序列检查是否在按
# 顺序推进；合法跳过（截图证明状态已满足）算 OK，不计 ALLOW 上限；偏离
# （子步骤被跳但截图没"已满足"证据）则 KILL。
#
# 间隔历史：
#   - v1: 5 → 起跑/Tab 切换/合法跳步阶段就被巡，误 KILL 高发
#   - v2 (当前默认 30) → 让 VLM 进入主流程后再开始巡检，避开前期合法跳步密集区
# env=0 可关闭周期巡检（仅靠 detector 触发审判）。
STRUCT_AUDIT_PERIODIC_INTERVAL = _settings.audit_periodic_interval  # env: AI_PHONE_AUDIT_PERIODIC_INTERVAL
PERIODIC_TRIGGER_PREFIX = "[周期巡检]"  # 协议常量：trigger 字符串前缀，区分 detector / periodic

# ---- 结构化通道判定信号 ----
# 不再只看「测试标题/前置条件/操作步骤/预期结果」四级标签——那是 QA 同学的写法，
# 用户说"严格约束 + 大量定语"也应该走结构化通道。改成多信号综合评分：
#   关键字命中 ≥ HARD_HIT     → 直接结构化（最高置信，免审判调用）
#   严格度评分 ≥ HARD_SCORE   → 直接结构化（即使关键字 < 2，长 case + 密集约束也算）
#   严格度评分 ≥ AUDIT_SCORE  → 借审判模型一次性分类（中等置信，让模型拍板）
#   两条都不达标               → 自由对话通道
# 启动横幅会把所有信号 + 决策路径打到日志里，避免出现"为什么走这个通道"的疑问。
STRUCT_KEYWORD_HARD_HIT = _settings.struct_keyword_hard_hit              # env: AI_PHONE_STRUCT_KEYWORD_HARD_HIT
STRUCT_STRICTNESS_HARD_SCORE = _settings.struct_strictness_hard_score    # env: AI_PHONE_STRUCT_STRICTNESS_HARD_SCORE
STRUCT_STRICTNESS_AUDIT_SCORE = _settings.struct_strictness_audit_score  # env: AI_PHONE_STRUCT_STRICTNESS_AUDIT_SCORE

_STRUCT_LABELS = (
    "测试标题", "测试用例", "前置条件",
    "操作步骤", "测试步骤", "预期结果",
    "[起跑线]", "[资源选择]", "[兜底]",
)
# 数字 + 单位/序号（30%、3 张、第 1 节、5 秒……）
_NUMERIC_CONSTRAINT_RE = re.compile(
    r"\d+\s*(?:%|秒|分|分钟|小时|次|张|条|节|章|段|页|个|位|步|轮|帧|个数|遍)"
    r"|第\s*\d+\s*[个张次节章条段步页位轮]"
)
_QUOTED_TERM_RE = re.compile(r"「[^」]+」|《[^》]+》|【[^】]+】")
# 条件分支与逻辑连词（"若 A 则 B 否则 C"、"按相同规则改用…"、"直至找到…"）
_LOGICAL_KEYWORDS = (
    "若", "则", "否则", "直至", "按相同规则",
    "如果不", "如果", "且仅当", "并且", "或者", "除非",
)
# 顺序词（"首先/然后/接着/最后/再/依次"）
_SEQUENCE_KEYWORDS = (
    "首先", "然后", "接着", "其次", "最后", "依次", "随后",
)
# 操作动词（点击/下滑/上滑/输入/等待/返回/切换/进入/唤起/退回……）
_ACTION_VERBS = (
    "点击", "下滑", "上滑", "左滑", "右滑", "输入", "等待",
    "返回", "切换", "进入", "唤起", "退回", "断言", "校验",
    "验证", "勾选", "选择",
)


@dataclass
class StructuredSignal:
    """结构化判定的所有原始信号，方便日志透明化与单元测试。

    每一项都对应一个客观可量化的语言学/排版特征；最终的 ``strictness_score``
    把这些信号按"是否超过经验阈值"折成 0-7 的小整数，分数越高越像 QA case。
    """
    keyword_hits: int                # 四级标签命中数
    keyword_labels: List[str]        # 命中的具体标签
    length_chars: int                # goal 总字符数
    quoted_terms: int                # 「」/《》/【】 引用次数
    numeric_constraints: int         # 数字+单位/序号 命中数（30%、第 1 节）
    logical_connectors: int          # 条件/逻辑连词命中数（若/则/否则/按相同规则）
    sequence_markers: int            # 顺序词命中数（首先/然后/最后）
    action_verbs: int                # 操作动词出现总次数（点击/下滑/输入……）
    strictness_score: int            # 综合评分（0-7）

    def to_brief(self) -> str:
        """日志用单行摘要。前端时间线一眼看清判定依据。"""
        return (
            f"标签{self.keyword_hits}({','.join(self.keyword_labels) or '无'}) "
            f"| 长度{self.length_chars}字 "
            f"| 「」{self.quoted_terms} "
            f"| 数字约束{self.numeric_constraints} "
            f"| 逻辑词{self.logical_connectors} "
            f"| 顺序词{self.sequence_markers} "
            f"| 动词{self.action_verbs} "
            f"| 综合评分{self.strictness_score}/7"
        )


def _compute_structured_signal(goal: str) -> StructuredSignal:
    """对 goal 抽取所有结构化判定信号，并按经验阈值给出综合评分。

    评分规则（每命中一项 +1，最高 7 分）：
    - 长度 ≥ 150 字
    - 长度 ≥ 300 字（再 +1，所以特别长的 case 自动得 2 分）
    - 「」/《》/【】 引用 ≥ 3 处
    - 数字+单位/序号 ≥ 2 处
    - 条件/逻辑连词 ≥ 1 处
    - 顺序词 ≥ 2 处
    - 操作动词总出现 ≥ 4 次
    """
    keyword_labels = [label for label in _STRUCT_LABELS if label in goal]
    length_chars = len(goal)
    quoted_terms = len(_QUOTED_TERM_RE.findall(goal))
    numeric_constraints = len(_NUMERIC_CONSTRAINT_RE.findall(goal))
    logical_connectors = sum(1 for k in _LOGICAL_KEYWORDS if k in goal)
    sequence_markers = sum(1 for k in _SEQUENCE_KEYWORDS if k in goal)
    action_verbs = sum(goal.count(v) for v in _ACTION_VERBS)

    score = 0
    if length_chars >= 150:
        score += 1
    if length_chars >= 300:
        score += 1
    if quoted_terms >= 3:
        score += 1
    if numeric_constraints >= 2:
        score += 1
    if logical_connectors >= 1:
        score += 1
    if sequence_markers >= 2:
        score += 1
    if action_verbs >= 4:
        score += 1

    return StructuredSignal(
        keyword_hits=len(keyword_labels),
        keyword_labels=keyword_labels,
        length_chars=length_chars,
        quoted_terms=quoted_terms,
        numeric_constraints=numeric_constraints,
        logical_connectors=logical_connectors,
        sequence_markers=sequence_markers,
        action_verbs=action_verbs,
        strictness_score=score,
    )


def _classify_structured_local(
    signal: StructuredSignal,
) -> Tuple[Optional[bool], str]:
    """本地启发式分类。返回 ``(verdict, reason)``：

    - ``verdict=True`` ：高置信结构化，免审判调用
    - ``verdict=False``：高置信自由对话，免审判调用
    - ``verdict=None`` ：模棱两可，需要审判模型一次性兜底分类
    """
    if signal.keyword_hits >= STRUCT_KEYWORD_HARD_HIT:
        return True, (
            f"四级标签命中 {signal.keyword_hits} 个 "
            f"({','.join(signal.keyword_labels)}) ≥ {STRUCT_KEYWORD_HARD_HIT}"
        )
    if signal.strictness_score >= STRUCT_STRICTNESS_HARD_SCORE:
        return True, (
            f"严格度评分 {signal.strictness_score}/7 ≥ {STRUCT_STRICTNESS_HARD_SCORE}"
            "（关键字虽不足，但 goal 含密集约束/定语/序列）"
        )
    if signal.strictness_score >= STRUCT_STRICTNESS_AUDIT_SCORE:
        return None, (
            f"严格度评分 {signal.strictness_score}/7 处于"
            f" [{STRUCT_STRICTNESS_AUDIT_SCORE}, {STRUCT_STRICTNESS_HARD_SCORE}) "
            "中等档，借审判模型最终拍板"
        )
    return False, (
        f"四级标签 {signal.keyword_hits} 个 + 严格度 {signal.strictness_score}/7"
        "（短对话/弱约束，自由通道）"
    )

# ---- 起跑线"打开 App / 关闭并重新打开"的代码层强制注入 ----
# 背景：VLM 视觉模型有强烈的"看到什么干什么"先验，单靠 prompt 很难压住——多次
# 实测，即使把"必须先 close_app + open_app"放在 prompt 最顶端也会被忽略，VLM
# 看到当前截图凑巧已在目标页就直接跳到操作步骤。改成代码侧扫描 goal、命中即
# 程序化跑前两步，**不让 VLM 选择**。
#
# 触发条件分两档（任一命中即触发起跑线，且都跑同一段 close_app + open_app
# 流程；close_app 在 App 未运行时是无害 no-op，重启更安全）：
#
#   档 1：goal 同时含"杀进程"类 + "重新打开"类关键词 + 「App名」
#         → 用户显式要求重启场景（如稳定性回归 / 冷启 case）
#
#   档 2：goal 含"启动"类关键词且**紧贴**「App 名」 + 「App 名」
#         → 简单"打开 X 做 Y"场景（如 goal="打开「淘宝」搜耳机"）。
#           退回 VLM 后，Claude/GPT CU 没有 open_app 抽象，走 home + 找
#           图标的路径既慢又不靠谱，必须由起跑线兜底
#
# 「应用名」始终要求 ASCII / CJK 引号包裹——避免误抓 goal 内 free-text 提
# 到的菜单 / Tab 名当 App 名。
_PRELUDE_KILL_KEYWORDS = (
    "杀进程", "杀掉", "关闭app", "关闭App", "kill", "关掉",
    "强制停止", "强制关闭", "强制退出",
)
_PRELUDE_RESTART_KEYWORDS = (
    "重新打开", "重启app", "重启App", "重开", "再次打开", "重新启动",
)
# 启动型关键词集合：和「App名」紧贴匹配才算触发档 2。"打开" 这种泛动词单独
# 出现不算（goal="打开蓝牙开关" 不应触发），必须紧跟 「」 包裹的 App 名。
_PRELUDE_OPEN_KEYWORDS = (
    "打开", "进入", "启动", "跳转到", "跳转至", "前往",
    "open", "launch", "go to", "goto", "enter",
)
_APP_NAME_RE = re.compile(r"「([^」]+)」")
# 档 2 专用：检测"启动词 + 「App名」"的紧贴模式。允许中间有空格 / "的" 等
# 助词，但不允许换行或长 free text 间隔。
_OPEN_APP_TIGHT_RE = re.compile(
    r"(?:" + "|".join(re.escape(k) for k in _PRELUDE_OPEN_KEYWORDS) + r")"
    r"\s*(?:的|了|下|一下)?\s*「([^」]+)」",
    re.IGNORECASE,
)
# 结构化 case 段切分：按 "测试标题：/前置条件：/操作步骤：/..." 这类标签把
# goal 切成 {标签: 段内容}。只挑"段头"型标签（不含括号方括号变体），后者是
# 内嵌内容标记，不是段头。
_SEGMENT_HEAD_LABELS = ("测试标题", "测试用例", "前置条件", "操作步骤", "测试步骤", "预期结果")
_SEGMENT_SPLIT_RE = re.compile(
    r"(" + "|".join(re.escape(label) for label in _SEGMENT_HEAD_LABELS) + r")\s*[：:]\s*"
)


def _split_goal_by_segments(goal: str) -> Dict[str, str]:
    """把结构化 case goal 按段头标签切成 {label: content}。

    输入示例（标签 ∈ ``_SEGMENT_HEAD_LABELS``，半 / 全角冒号都吃）::

        测试标题：<标题文案 · 内可能含「UI 元素名」>
        前置条件：<环境准备 · 内可能含「App 名」与触发关键词>
        操作步骤：<分步骤 · 内可能含「UI 元素名」>
        预期结果：<断言文案 · 内可能含「UI 元素名」>

    输出 ``{"测试标题": <…>, "前置条件": <…>, "操作步骤": <…>, "预期结果": <…>}``。

    设计目的：让起跑线 / 子步骤拆解等系统能按段精确取数，避免"全文扫"
    把段间内容混淆——例如测试标题段里的「UI 元素」与前置条件段里的
    「App 名」频次并列时被误抓。

    若 goal 不是结构化格式（无任何段头标签命中），返回空 dict —— 调用方应
    退到 "全文扫" 兜底逻辑（兼容自由对话型 goal）。
    """
    parts = _SEGMENT_SPLIT_RE.split(goal)
    if len(parts) < 3:
        return {}
    out: Dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        label = parts[i]
        content = parts[i + 1].strip()
        if label and content and label not in out:
            out[label] = content
    return out


def _detect_app_lifecycle_prelude(goal: str) -> Optional[str]:
    """检查 goal 是否要求起跑线注入。命中返回 app 名，否则 None。

    设计要点（按段感知，2026-05 重构）：

    - 起跑线只对"前置条件"段有意义——"杀进程+重启 X"在前置条件段表示
      "Run 启动前的环境准备"，应该提前；同样的关键词在"操作步骤"段则是
      业务步骤的一部分（如"重启后验证配置持久化"），必须由 VLM 按顺序执
      行，不能被起跑线偷跑。
    - 因此结构化 case：只在"前置条件"段里跑档1/档2 判定；前置条件段不命
      中就直接放弃起跑线，**不再 fallback 到全文**，避免"操作步骤段被起
      跑线误抓"。
    - 自由对话 / 平铺型 goal（无任何段头标签）：退到"全文扫"兜底逻辑，
      行为与重构前一致，保留对"杀掉淘宝再打开"、"打开「淘宝」搜耳机"这
      类短句的支持。

    返回的 app 名统一交给 _run_app_lifecycle_prelude 跑同一段 close_app +
    open_app 流程——两档之间不再分支：close_app 对未运行的 App 等价于
    no-op（驱动层 force-stop 报 "not running" 几十毫秒），重启对 case 行
    为更确定（避免上次脏 session 残留）。
    """
    segments = _split_goal_by_segments(goal)
    if segments:
        # 结构化 case：只看"前置条件"段；没有该段或段内不命中 → 放弃起跑线
        precondition = segments.get("前置条件", "")
        if not precondition:
            return None
        return _detect_in_text(precondition)

    # 自由对话 / 平铺型 goal：兜底走原"全文扫"逻辑
    return _detect_in_text(goal)


def _detect_in_text(text: str) -> Optional[str]:
    """在指定文本片段内跑档1/档2 判定。供前置条件段 / 全文扫共用。"""
    text_compact = text.replace(" ", "")

    # 档 1：杀进程 AND 重新打开 AND 「App名」
    has_kill = any(k.replace(" ", "") in text_compact for k in _PRELUDE_KILL_KEYWORDS)
    has_restart = any(
        k.replace(" ", "") in text_compact for k in _PRELUDE_RESTART_KEYWORDS
    )
    if has_kill and has_restart:
        matches = _APP_NAME_RE.findall(text)
        if matches:
            # 多次出现取最高频，避免误抓段内偶提的菜单 / 选项名当 App 名
            return Counter(matches).most_common(1)[0][0]

    # 档 2：启动词紧贴「App名」（"打开「淘宝」搜耳机"、"进入「微信」找联系人..." 类）
    tight_matches = _OPEN_APP_TIGHT_RE.findall(text)
    if tight_matches:
        # 多次出现取最高频，与档 1 同语义
        return Counter(tight_matches).most_common(1)[0][0]

    return None


def _parse_csv_keywords(raw: str) -> List[str]:
    """把逗号分隔的关键词串规整成去重后的列表。"""
    out: List[str] = []
    seen = set()
    for item in (raw or "").split(","):
        kw = str(item or "").strip()
        if not kw or kw in seen:
            continue
        out.append(kw)
        seen.add(kw)
    return out


def _detect_transient_ui_arm(goal: str, keywords: List[str]) -> List[str]:
    """动态判断系统：goal 命中白名单关键词则为本次 Run 挂上瞬态 UI 能力。

    这是 run 级的一次性预判，不是 step 级在线推断。设计目标与"起跑线"一致：
    在主循环开始前先把高成本能力是否启用定下来，后续只消费这个布尔标记，避免
    每一步都重新猜，保证行为稳定、日志可解释。
    """
    if not goal or not keywords:
        return []
    text = goal.lower().replace(" ", "")
    hits: List[str] = []
    for kw in keywords:
        token = kw.lower().replace(" ", "")
        if token and token in text:
            hits.append(kw)
    return hits


# emit 回调支持同步或异步，runner 内部统一 await
EmitFn = Callable[[Dict[str, Any]], Any]


@dataclass
class RunResult:
    ok: bool
    reason: str = ""
    steps: int = 0
    elapsed_ms: int = 0
    token_summary: Dict[str, Any] = field(default_factory=dict)


async def _maybe_await(result: Any) -> None:
    if asyncio.iscoroutine(result):
        await result


class VLMRunner:
    """单台设备上的单次任务执行器。

    使用方式::

        runner = VLMRunner(run_id="xxx", driver=android_driver, goal="打开微信", emit=callback)
        result = await runner.run()
    """

    def __init__(
        self,
        run_id: str,
        driver: BaseDriver,
        goal: str,
        *,
        emit: Optional[EmitFn] = None,
        vlm_client: Optional[BaseMainVLM] = None,
        assistant: Optional[BaseAssistant] = None,
        stop_event: Optional[asyncio.Event] = None,
        max_steps: int = SAFETY_MAX_STEPS,
    ) -> None:
        if not goal or not goal.strip():
            raise ValueError("goal 不能为空")
        self.run_id = run_id
        self.driver = driver
        self.goal = goal.strip()
        self._emit = emit
        self._stop_event = stop_event or asyncio.Event()
        self.max_steps = max_steps

        # 配置单例：断言系统、瞬态 UI 门控等都要从 Settings 读 VLM 端点 / env 开关。
        # 抽到 self 是为了让单测能通过 monkeypatch 替换；运行期等价于 get_settings()。
        self._settings = get_settings()

        # 主 VLM 客户端：通过 ``create_main_vlm`` 工厂按 ``settings.vlm_backend`` 分派
        # （doubao_responses / claude_cu / gpt_cu）。测试时可以传 ``vlm_client``
        # 参数注入 mock，绕过工厂。
        # System prompt 同样按 backend 分家：豆包走文本 DSL 模板，Claude 走
        # ``computer`` tool + ``FINISHED:`` 关键字模板，GPT 走 computer-use-
        # preview 模板——三家协议输出形态完全不同，共用一份模板会让 Claude/
        # GPT 退化成"忠实输出豆包文本 DSL"，runner 的 tool_use 解析全部 miss。
        system_prompt = build_system_prompt_for_backend(
            self.goal, backend=self._settings.vlm_backend
        )
        self.counter = TokenCounter()
        self.vlm = vlm_client or create_main_vlm(
            system_prompt=system_prompt, counter=self.counter
        )
        # 辅助系统：完全独立于主 VLM 的开关，工厂按 ``settings.assistant_backend``
        # 分派（doubao_chat / claude / openai）。共享同一份 counter 让 token 大盘
        # 一并统计；测试可注入 mock。
        self._assistant: BaseAssistant = assistant or create_assistant(
            counter=self.counter
        )

        # 运行态（循环内会维护）
        self._last_tail_bytes: Optional[bytes] = None
        # 最近一次喂给 ``vlm.decide`` 的截图分辨率 (width, height)。仅在
        # ``coord_space="absolute"`` 路径（Claude / GPT CU）反算坐标时使用——
        # CU 系截图会被 ``max_long_edge=1344`` 缩放，模型给的像素是相对这张
        # 缩图，需要按设备/截图比例缩回设备坐标。豆包路径不读本字段。
        self._last_vlm_screenshot_size: Optional[Tuple[int, int]] = None
        # 上一个 step 主 VLM 决策时看到的 before 帧。在 finished 走断言系统时，
        # 用作"最后一个动作前"的对照帧——和当前 final 帧组成"动作前/后"双图，
        # 让断言模型直接对比"主 VLM 自述的状态变化"是否真的发生。
        # 跨度永远 = 一个 step（最后那个动作），不会被 case 长度污染。
        # 第一步就 finished 时为 None，断言会退化成单图模式。
        self._previous_before_bytes: Optional[bytes] = None
        self._recent_clicks: List[Tuple[int, int]] = []
        self._scroll_streak: Tuple[Optional[str], int] = (None, 0)
        self._unknown_streak = 0
        self._last_wait_was_explicit = False
        self._consecutive_screenshot_fails = 0
        self._current_step = 0

        # 结构化通道判定：综合信号评分（四级标签 + 长度 + 定语 + 数字约束 +
        # 逻辑词 + 顺序词 + 动词），把单一关键字判定升级为可解释的多信号决策。
        # 中等档由审判模型在 run() 入口异步兜底拍板，全程信号 + 决策路径都会
        # 进 RunLog，避免出现"为什么走/不走结构化通道"的疑问。
        self._struct_signal: StructuredSignal = _compute_structured_signal(self.goal)
        local_verdict, local_reason = _classify_structured_local(self._struct_signal)
        self._struct_decision_local_reason: str = local_reason
        # 高置信即定；模棱两可（None）先占位 False，run() 启动时调审判模型补判
        self._is_structured: bool = bool(local_verdict) if local_verdict is not None else False
        self._needs_supervisor_classify: bool = local_verdict is None

        # 点击桶用"质心 + 半径聚类"：欧氏距 ≤ STRUCT_CLICK_BUCKET_PX 视为同按钮，
        # 结构 [(centroid_x, centroid_y, count), ...]
        self._click_buckets: List[List[int]] = []
        # before-screenshot 指纹访问历史：[(phash, [step1, step2, ...]), ...]
        # 用汉明距聚类同一屏，**累计**所有访问步号到 list 里数次数
        self._screen_visits: List[Tuple[int, List[int]]] = []
        # 步号 → 该步执行的 click 坐标（仅 click 类动作记录；其他动作不记录）。
        # 用于 _check_screen_revisit 的"同屏但 click 坐标显著不同 → 豁免"判定，
        # 避免把"多级选择弹窗内逐级下钻"这种合理多步操作误召唤。
        self._step_click_xy: Dict[int, Tuple[int, int]] = {}
        # 滚动方向序列（仅 scroll 动作进入），用于检测 up↔down 震荡找不到目标
        self._scroll_history: List[str] = []
        # 滑动"没让页面动"的连击计数：(direction, count)
        # 只数 phash 几乎不变的同方向滑动，区分"长列表合法多滑"与"已到底瞎滑"
        self._scroll_no_progress: Tuple[Optional[str], int] = (None, 0)

        # —— 审判通道运行态 ——
        # 本步累积的"触发审判原因"列表；step 收尾时一次性合并发审判，避免同步
        # 命中多个探测器导致重复调审判。step 之间清空。
        self._pending_audit_triggers: List[str] = []
        # 已审判过的步号（防抖）：同一 step 不重复审
        self._audit_called_steps: set[int] = set()
        # 已被审判 ALLOW 放过的次数；到 STRUCT_AUDIT_ALLOW_LIMIT 下次审判直接 KILL
        self._audit_allow_count: int = 0
        # 审判 / 探测器最终判死时的硬 kill 信号：(kind, detail)
        # 与 VLM 主动 assert_fail 走同一通道，但消息体加"系统硬约束触发"前缀
        self._fatal_signal: Optional[Tuple[str, str]] = None

        # 喂给审判的"动作历史"：[{step, thought, action_str}, ...]
        # 每步主循环里 VLM 决策完成后 append；审判取最近 N 条
        self._action_log: List[Dict[str, Any]] = []

        # —— 瞬态 UI 接管态 ——
        # 由 click 后的 ⑤.5 检测器写入；下一步 ① 启动时如果非空则走"接管路径"
        # （缓存帧代替 before + chain 重组重唤起）；用完立即清空。
        # 严格"只活 1 步"，避免跨步使用过期的工具栏帧。
        self._transient_snapshot: Optional[TransientUISnapshot] = None

        # —— 瞬态 UI 动态判断系统：run 级一次性预判 ——
        # 总开关 + 关键词白名单都在 Settings 里，用户用 .env 控制；这里只读结果，
        # 主循环里判完 _transient_ui_armed 一个布尔即可决定是否走检测路径。
        # 设计同"起跑线"：高成本能力按需启用，避免普通 case 莫名其妙多花算力。
        self._transient_ui_enabled: bool = self._settings.transient_ui_enabled
        self._transient_ui_keywords: List[str] = _parse_csv_keywords(
            self._settings.transient_ui_keywords
        )
        self._transient_ui_late_delay_ms: int = int(
            self._settings.transient_ui_late_delay_ms
        )
        self._transient_ui_hits: List[str] = (
            _detect_transient_ui_arm(self.goal, self._transient_ui_keywords)
            if self._transient_ui_enabled
            else []
        )
        # armed = 总开关开 且 goal 命中关键词；后续主循环只看这一个布尔
        self._transient_ui_armed: bool = bool(
            self._transient_ui_enabled and self._transient_ui_hits
        )

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def run(self) -> RunResult:
        task_start = time.monotonic()
        await self._emit_event(make_event(EVT_RUN_START, self.run_id, goal=self.goal))
        await self._log(1, "智能Agent 启动", f"目标: {self.goal}")

        # —— 通道判定第 1 步：本地信号摘要 ——
        # 不管最终走哪条通道，都把所有信号+本地预判先打到日志，方便用户在
        # 时间线顶端核对"为什么这条 case 被这样判"。
        await self._log(
            1,
            "通道判定 · 信号扫描",
            self._struct_signal.to_brief(),
        )
        await self._log(
            1,
            "通道判定 · 本地预判",
            self._struct_decision_local_reason,
        )

        # —— 通道判定第 2 步：模棱两可时由审判模型一次性兜底分类 ——
        # 失败/超时按"非结构化"处理（保守：宁可不开启硬约束也不要因为基础
        # 设施卡顿就把 Run 直接打死）。
        decision_source = "本地启发式"
        if self._needs_supervisor_classify:
            try:
                self._is_structured = await self._classify_structured_via_supervisor()
                decision_source = "审判模型兜底分类"
                await self._log(
                    1,
                    "通道判定 · 审判兜底",
                    f"严格度落在中间档，审判模型判为：{'结构化' if self._is_structured else '自由对话'}",
                )
            except Exception as exc:  # noqa: BLE001
                self._is_structured = False
                decision_source = "审判失败 → 保守自由对话"
                await self._log(
                    2,
                    "通道判定 · 审判调用失败",
                    f"审判模型调用异常 → 默认走自由对话通道。错误：{exc}",
                )

        # —— 通道判定第 3 步：横幅明示最终通道 + 阈值 ——
        if self._is_structured:
            await self._log(
                1,
                "[结构化通道] 已启用 · 审判兜底",
                f"判定来源：{decision_source} | "
                f"探测阈值（命中即召唤审判，由模型决定继续 / 终止）："
                f"同坐标桶 ≥{STRUCT_CLICK_BUCKET_TRIGGER} | "
                f"同屏访问 ≥{STRUCT_SCREEN_REVISIT_TRIGGER} | "
                f"无效滑动 ≥{STRUCT_SCROLL_NOPROGRESS_TRIGGER} | "
                f"滚动震荡 ≥{STRUCT_SCROLL_FLIP_TRIGGER} | "
                f"审判 ALLOW 上限 {STRUCT_AUDIT_ALLOW_LIMIT} 次",
            )
        else:
            await self._log(
                1,
                "[自由对话通道]",
                f"判定来源：{decision_source} | 审判通道未启用，VLM 拥有最大自由度。"
                "如想启用，goal 含 ≥2 节四级标签 或 严格度评分 "
                f"≥ {STRUCT_STRICTNESS_HARD_SCORE}/7 即可强制开启；"
                f"评分 ≥ {STRUCT_STRICTNESS_AUDIT_SCORE}/7 会借审判模型再判一次。",
            )

        # —— 动态判断系统 · 瞬态 UI（视频工具栏 / Toast）——
        # 三态明示，避免出现"为什么没触发瞬态接管"的疑问：
        # ① env 总开关关 → 全程不调 detect_transient_ui
        # ② env 开 + goal 命中关键词 → 启用，主循环 ⑤.5 才会跑检测
        # ③ env 开但 goal 没命中 → 普通 case 不挂载，避免空跑算力
        if not self._transient_ui_enabled:
            await self._log(
                1,
                "动态判断系统 · 瞬态UI",
                "env 总开关关闭（AI_PHONE_TRANSIENT_UI_ENABLED=false），本次 Run 禁用瞬态 UI 检测/接管。",
            )
        elif self._transient_ui_armed:
            await self._log(
                1,
                "动态判断系统 · 瞬态UI",
                f"命中白名单关键词：{','.join(self._transient_ui_hits)}"
                f" | 本次 Run 启用瞬态 UI 检测/接管。",
            )
        else:
            await self._log(
                1,
                "动态判断系统 · 瞬态UI",
                "env 总开关已开，但 goal 未命中白名单关键词；本次 Run 禁用瞬态 UI 检测/接管。"
                f"白名单={','.join(self._transient_ui_keywords) or '空'}",
            )

        # —— 子步骤清单注入（结构化通道专属）——
        # 让 VLM 每轮都能看到"完整地图 + 还剩什么"，治本 VLM 单帧推理无状态导致
        # 的跳步盲区。详见 _extract_struct_substeps。失败/不可用时不影响 Run，
        # 仍按原 prompt 渲染。自由对话通道不跑，因为没有"操作步骤"段落可拆。
        if self._is_structured:
            substeps_text = await self._extract_struct_substeps()
            if substeps_text:
                line_count = sum(
                    1 for line in substeps_text.splitlines() if line.strip()
                )
                self.vlm.system_prompt = build_system_prompt_for_backend(
                    self.goal,
                    substeps_text=substeps_text,
                    backend=self._settings.vlm_backend,
                )
                await self._log(
                    1,
                    "子步骤清单 · 已注入",
                    f"已把 case 操作步骤拆为 {line_count} 条有序子步骤，"
                    "贯穿全 Run 注入到 system prompt 顶部",
                )

        try:
            # ⓪ 起跑线强制注入：goal 里有"杀进程+重新打开「应用名」"组合时，
            #    系统直接跑 close_app + open_app，跳过 VLM 决策。这是 VLM
            #    最常见的失败模式（光看截图凑巧已在 App 内就忽略起跑线）。
            prelude_app = _detect_app_lifecycle_prelude(self.goal)
            if prelude_app:
                await self._run_app_lifecycle_prelude(prelude_app)
            ok, reason = await self._main_loop()
        except asyncio.CancelledError:
            await self._log(2, "任务取消", "外部取消信号")
            ok, reason = False, "cancelled"
        except Exception as exc:  # noqa: BLE001
            logger.exception("VLMRunner 异常退出 run_id={}", self.run_id)
            ok, reason = False, f"runner_error: {exc}"
            await self._log(3, "任务异常", f"{exc}")

        elapsed_ms = int((time.monotonic() - task_start) * 1000)
        summary = self.counter.summary()
        summary["vlm_backend"] = self._settings.vlm_backend
        await self._emit_event(
            make_event(EVT_TOKEN_SUMMARY, self.run_id, **summary)
        )
        await self._log(
            1,
            "任务总耗时",
            f"{elapsed_ms}ms ({elapsed_ms / 1000:.2f}秒)",
        )
        # token_stats 必须**和 RUN_FINISH 一起发**：下游（agent_ws._finalize_run）
        # 只看 MSG_RUN_DONE.token_stats 去写 Run.token_summary。之前少带这个字段，
        # 导致 Run.token_summary 一直是 ``{}``，大盘"Token 当日 VLM 开销"永远 0。
        # 单独的 EVT_TOKEN_SUMMARY 事件只用来出日志，并不会落库。
        await self._emit_event(
            make_event(
                EVT_RUN_FINISH,
                self.run_id,
                ok=ok,
                reason=reason,
                steps=self._current_step,
                elapsed_ms=elapsed_ms,
                token_stats=summary,
            )
        )
        return RunResult(
            ok=ok,
            reason=reason,
            steps=self._current_step,
            elapsed_ms=elapsed_ms,
            token_summary=summary,
        )

    def stop(self) -> None:
        """外部请求停止（下一步循环开始时会检测到并结束）。"""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _main_loop(self) -> Tuple[bool, str]:
        # 起跑线 prelude 可能已经消耗了 1-2 步（self._current_step 被推进过）；
        # main_loop 接着这个数继续往下走，整段步号在前端 / 报告里是连续的
        start_step = max(1, self._current_step + 1)
        for step in range(start_step, self.max_steps + 1):
            if self._stop_event.is_set():
                await self._log(2, "任务停止", "收到外部停止信号")
                return False, "stopped"

            self._current_step = step
            await self._emit_event(make_event(EVT_STEP_START, self.run_id, step=step))
            await self._log(
                1, f"━━ 第 {step} 步 ━━", f"段={self.vlm.segment_count}"
            )

            # ⓪ 会话分段重置判定：
            #    - 上一轮 prompt ≥ 阈值（默认 30000）→ 预判下一轮将跨入二档（>32K, ×2）
            #    - 重置 previous_response_id 让服务端重新从更短的前缀起步
            #    - 注入"已完成 X 步"续接提示，避免模型完全失忆
            #    - 单段内仍享受显式缓存红利；跨段切多段，每段都留在一档
            if self.vlm.should_reset_session():
                resume_hint = (
                    f"【会话续接】这是任务的第 {self.vlm.segment_count + 1} 段，"
                    f"此前已完成 {step - 1} 步操作（详细历史已归档）。"
                    "请根据当前截图分析剩余进度，继续推进任务。"
                    "如果当前页面已满足完成条件，请直接执行 finished()。"
                )
                old_id = self.vlm.reset_session(resume_hint)
                await self._log(
                    2,
                    "会话分段触发",
                    f"第 {step} 步进入第 {self.vlm.segment_count} 段 | "
                    f"上轮 prompt 已超阈值 | "
                    f"previous_response_id {str(old_id)[:20]}… → null",
                    step=step,
                )

            # ① 稳定检测 + 截图（复用上步尾帧）
            #
            # 瞬态 UI 接管分支：上一步 click 后检测到了"出现又消失"的瞬态 UI
            # （:class:`TransientUISnapshot`），这一步**不抓新帧、不做稳定检测**，
            # 直接拿缓存的"工具栏完整可见"那张图给 VLM 看，让 VLM 在这张静止图
            # 上输出精准坐标。系统会在 ④ 执行阶段自动重唤起 + 立即点击。
            takeover = self._transient_snapshot
            if takeover is not None:
                screenshot_bytes = takeover.visible_frame
                self.vlm.add_hint(build_takeover_hint(takeover))
                await self._emit_screenshot(step, "before", screenshot_bytes)
                await self._log(
                    1,
                    "瞬态UI接管 · 复用缓存帧",
                    f"上一步 {takeover.trigger_action}{takeover.trigger_point_abs} "
                    f"唤起了瞬态 UI，本步 VLM 看的是缓存的工具栏可见帧；"
                    f"执行阶段会自动重唤起 + 立即点 VLM 给的坐标",
                    step=step,
                )
            else:
                stable = await wait_page_stable_pixel(
                    self._screenshot_jpeg,
                    self._last_tail_bytes,
                    log=lambda lvl, title, content, _s=step: asyncio.create_task(
                        self._log(lvl, title, content, step=_s)
                    ),
                )
                if stable.bytes_ is None:
                    self._consecutive_screenshot_fails += 1
                    await self._log(
                        3,
                        "截图失败",
                        f"第{self._consecutive_screenshot_fails}次连续失败",
                        step=step,
                    )
                    if self._consecutive_screenshot_fails >= CONSECUTIVE_SCREENSHOT_FAIL_LIMIT:
                        return False, "screenshot_failed"
                    continue
                self._consecutive_screenshot_fails = 0

                screenshot_bytes = stable.bytes_
                await self._emit_screenshot(step, "before", screenshot_bytes)

            # ①.5 结构化 case 屏幕指纹召唤：进入 VLM 决策**之前**先看当前屏幕
            #     是否已被反复访问。命中只是把"召唤原因"挂上去，最终是否 kill
            #     由 step 收尾时的审判调用统一裁决（防止本地探测器误杀合法重试）。
            self._check_screen_revisit(screenshot_bytes, step=step)
            if await self._maybe_audit(step):
                return await self._raise_fatal(step, screenshot_bytes)

            # ② 决策
            # CU 系（claude_cu / gpt_cu）需要在喂截图前缓存其分辨率，下游
            # ``_vlm_point_to_abs(coord_space="absolute")`` 用它把模型像素
            # 反算回设备坐标。豆包路径只是多写一次 self 字段，不影响行为。
            backend = (self._settings.vlm_backend or "").lower()
            if backend in ("claude_cu", "gpt_cu"):
                self._last_vlm_screenshot_size = _decode_jpeg_size(screenshot_bytes)
            try:
                # driver.screenshot_jpeg 出来是 JPEG；显式告诉 VLMClient 用哪种 mime
                decision = await self.vlm.decide(screenshot_bytes, mime="image/jpeg")
            except Exception as exc:  # noqa: BLE001
                await self._log(3, "决策失败", f"错误: {exc}", step=step)
                return False, f"vlm_error: {exc}"

            thought = decision.thought
            # —— 链式动作解析 ——
            # VLM 默认每轮输出 1 个 Action；遇到瞬态 UI（视频工具栏 / Toast 等
            # 自动隐藏的浮层）允许在同一 Thought 下输出 ≤ CHAIN_MAX_ACTIONS
            # 个 Action。这里把所有 Action 一次性解析为 ParsedAction 列表，
            # 后面分别做"长度截断 / 白名单过滤 / 顺序执行"。
            #
            # 多协议适配：豆包系输出文本 DSL，走 parse_action 文本解析；Claude/GPT
            # 通过 tool_use / computer_call 已经给出结构化字段，会在 client 内直接
            # 构造 ParsedAction 列表挂到 decision.parsed_actions。优先消费结构化
            # 字段，没有再 fallback 到文本解析。
            if decision.parsed_actions:
                parsed_chain: List[A.ParsedAction] = list(decision.parsed_actions)
            else:
                all_action_strs = decision.action_strs or [decision.action_str]
                parsed_chain = [A.parse_action(s) for s in all_action_strs]
            for pa in parsed_chain:
                pa.thought = thought

            # 链长度截断：超出 CHAIN_MAX_ACTIONS 一律按规范丢回提示
            if len(parsed_chain) > CHAIN_MAX_ACTIONS:
                truncated_count = len(parsed_chain)
                parsed_chain = parsed_chain[:CHAIN_MAX_ACTIONS]
                self.vlm.add_hint(
                    f"⚠️ 你本步输出了 {truncated_count} 个 Action，超过单步上限 "
                    f"{CHAIN_MAX_ACTIONS} 个。系统已截断保留前 {CHAIN_MAX_ACTIONS} 个。"
                    "除瞬态 UI（视频播放工具栏 / Toast / 半透明菜单等会自动隐藏的浮层）"
                    "外，请每步只输出 1 个 Action，看清反馈再决定下一步。"
                )
                await self._log(
                    2, "动作链截断",
                    f"{truncated_count} → {CHAIN_MAX_ACTIONS} 个", step=step,
                )

            # 链内动作白名单：只允许 click / long_press / double_tap 这种"瞬时点击"
            # 串联；type / scroll / drag / wait / open_app / close_app / finished /
            # assert_fail 都需要看反馈再决定下一步，强制单独一行。
            if len(parsed_chain) > 1:
                bad = [
                    p for p in parsed_chain
                    if (p.action or "") not in _CHAIN_ALLOWED_ACTIONS
                ]
                if bad:
                    bad_names = ",".join(sorted({(p.action or "?") for p in bad}))
                    parsed_chain = parsed_chain[:1]
                    self.vlm.add_hint(
                        f"⚠️ 你输出了链式 Action 但其中含非点击类动作（{bad_names}）。"
                        "动作链只允许 click / long_press / double_tap 这几种瞬时点击；"
                        "其他动作（type / scroll / drag / wait / open_app / close_app / "
                        "finished / assert_fail）必须单独成行——它们都需要看反馈再决定"
                        "下一步。本步已只执行第 1 个 Action。"
                    )
                    await self._log(
                        2, "动作链不合规",
                        f"含非点击动作 {bad_names}，已只执行第 1 个", step=step,
                    )

            # 链首作为终止判定 + 卡死检测的"主体动作"
            parsed = parsed_chain[0]
            action_type = parsed.action or A.ACTION_FINISHED
            is_chain = len(parsed_chain) > 1

            # 展示文案：链式用"→"分隔多个原始 Action 字符串
            display_action = (
                " → ".join((p.raw or "").strip() for p in parsed_chain)
                if is_chain else (parsed.raw or "")
            )

            # 思考 / 动作的可见日志由这里 self._log 唯一承担——它是 RunLog 表
            # 与 HTML 报告时间线 log 行 "思考" / "动作" 文字的唯一来源（RunStep
            # 表的 thought / action 字段当前未被 bridge 写入，报告 step 块只展
            # 示截图，思考 / 动作完全靠这两条 log 行）。
            #
            # 历史 bug：runner_bridge 之前把 EVT_THOUGHT / EVT_ACTION 也翻译成
            # MSG_LOG，与本路径同内容、间隔 ~100ms 重复一次。修复落在 bridge
            # 那侧（不再翻译），见 runner_bridge.emit() 注释。
            #
            # 这里的 EVT_THOUGHT / EVT_ACTION 仍然 emit，给未来扩展（比如把
            # thought / action 一并写进 RunStep）保留事件源；bridge 当前会忽略
            # 它们，不会重复落库 / 重复 WS。
            await self._emit_event(
                make_event(EVT_THOUGHT, self.run_id, step=step, text=thought)
            )
            await self._emit_event(
                make_event(
                    EVT_ACTION, self.run_id, step=step,
                    text=display_action, elapsed_ms=decision.elapsed_ms,
                )
            )
            await self._log(1, "思考", thought, step=step)
            await self._log(
                1,
                f"动作链（×{len(parsed_chain)}）" if is_chain else "动作",
                display_action,
                step=step,
            )

            # 把这一步思考+动作压入审判用的动作历史。即便后续是终止动作也压一条，
            # 方便审判看到"VLM 是怎么决定 finished/assert_fail 的"。链式时整段
            # 字符串一起入历史，让审判能看出"这是 1 步 2 击"。
            self._action_log.append({
                "step": step,
                "thought": thought,
                "action_str": display_action,
            })
            # 不主动裁剪：每条 dict 约 1KB，1000 步累积 ~1MB，对内存可忽略；
            # 过去裁剪是为了"省内存"的折衷，已按"效果优先"原则解除——审判随时
            # 可调出全 Run 任意片段，不再因裁剪丢失早期偏离证据。

            # ③ 终止动作（只看链首；前面的白名单已确保 finished / assert_fail
            #    不可能出现在链中段——所以遇到终止动作意味着 chain 长度=1）
            if action_type == A.ACTION_ASSERT_FAIL:
                fail_msg = parsed.content or "断言不通过"
                await self._emit_screenshot(step, "finish_fail", screenshot_bytes)
                await self._log(
                    3,
                    "断言不通过",
                    f"共执行 {step} 步 | {fail_msg}",
                    step=step,
                )
                return False, f"assert_fail: {fail_msg}"

            if action_type == A.ACTION_FINISHED:
                finish_msg = parsed.content or "任务完成"
                # —— 断言系统：finished 必须经独立 VLM 复核才能落锤 ——
                # 主 VLM 决定"完成"后，再喂一次 VLM（带最终截图 + 主 VLM 的
                # thought/finish_msg）做终局裁决，只返回 PASS/FAIL/SKIP：
                #   PASS  → 走原 finished 收尾
                #   FAIL  → 改写为 assert_fail，避免主 VLM 在难 case 下"自我
                #          安慰式"完成
                #   SKIP  → 配置缺失 / 调用失败 / 协议不合法，回退采纳主 VLM
                # 该方法只读、不再继续执行任何 step；超时/异常都回退而非阻塞。
                await self._log(
                    1,
                    "主VLM申请完成",
                    f"共执行 {step} 步 | {finish_msg}",
                    step=step,
                )
                verdict, verify_reason = await self._verify_finished_assertion(
                    prev_before_bytes=self._previous_before_bytes,
                    final_bytes=screenshot_bytes,
                    thought=thought,
                    finish_msg=finish_msg,
                    step=step,
                )
                if verdict == "FAIL":
                    fail_msg = f"【断言系统驳回 finished】{verify_reason}"
                    await self._emit_screenshot(step, "finish_fail", screenshot_bytes)
                    await self._log(
                        3,
                        "断言系统 · 驳回",
                        f"共执行 {step} 步 | {fail_msg}",
                        step=step,
                    )
                    return False, f"assert_fail: {fail_msg}"
                if verdict == "PASS":
                    await self._log(
                        1,
                        "断言系统 · 通过",
                        verify_reason,
                        step=step,
                    )
                # SKIP / PASS 都继续走原 finished 收尾
                await self._emit_screenshot(step, "finish_ok", screenshot_bytes)
                await self._log(
                    1,
                    "任务完成",
                    f"共执行 {step} 步 | {finish_msg}",
                    step=step,
                )
                return True, f"finished: {finish_msg}"

            # ④ 执行链：链内不抓中间帧、不做稳定检测；中间间隔
            #    CHAIN_INNER_GAP_MS=200ms（瞬态 UI 寿命 ~2s 已足够），
            #    最后一个动作仍走 POST_ACTION_SETTLE_MS=500ms 让 UI 稳定。
            #
            # 瞬态 UI 接管分支：takeover 非空时，VLM 看的是缓存的"工具栏可见
            # 帧"，但当下设备上工具栏早消失了，直接执行 VLM 的 click 一定扑空。
            # 必须在执行 VLM 给的链之前，**先重唤起**：复制上一步触发瞬态 UI
            # 的那个 click（坐标记在 takeover.trigger_point_abs），再 sleep
            # TRANSIENT_TAKEOVER_WAIT_MS（500ms）让工具栏出现，然后立即执行
            # VLM 给的目标 click。整个"重唤起+目标点击"链 ~700ms，足够命中
            # 工具栏 ~2s 寿命窗。
            #
            # 重唤起 click **不进 chain 解析、不计入卡死检测**——它是系统编排
            # 的，VLM 没主动做这一击，不应该让它撑爆 _check_click_stuck 的同
            # 坐标计数。VLM 给的目标 click 走原 _execute_action 路径，stuck
            # 检测正常生效。
            if takeover is not None and parsed_chain[0].action == A.ACTION_CLICK:
                rcx, rcy = takeover.trigger_point_abs
                await self._log(
                    2,
                    "瞬态UI接管 · 重唤起",
                    f"系统在 VLM 目标 click 之前先重放 click({rcx},{rcy}) "
                    f"+ 等 {TRANSIENT_TAKEOVER_WAIT_MS}ms",
                    step=step,
                )
                try:
                    await asyncio.to_thread(self.driver.click, rcx, rcy)
                except Exception as exc:  # noqa: BLE001
                    await self._log(
                        2,
                        "瞬态UI接管 · 重唤起失败",
                        f"错误: {exc}，回退到普通执行",
                        step=step,
                    )
                else:
                    await asyncio.sleep(TRANSIENT_TAKEOVER_WAIT_MS / 1000.0)
            # 用完即清，绝不跨步保留
            self._transient_snapshot = None

            total_elapsed_ms = 0
            unknown_in_chain = False
            chain_count = len(parsed_chain)
            for chain_idx, parsed_i in enumerate(parsed_chain):
                if chain_idx > 0:
                    await asyncio.sleep(CHAIN_INNER_GAP_MS / 1000.0)
                # 链内非末尾动作不留 settle，让外层 200ms gap 控制节奏
                settle_ms = (
                    POST_ACTION_SETTLE_MS if chain_idx == chain_count - 1 else 0
                )
                try:
                    exec_log_i = await self._execute_action(
                        parsed_i, step=step, settle_ms=settle_ms,
                    )
                except Exception as exc:  # noqa: BLE001
                    await self._log(
                        3,
                        "执行失败",
                        f"动作: {parsed_i.action}, 错误: {exc}",
                        step=step,
                    )
                    return False, f"execute_error: {exc}"
                total_elapsed_ms += exec_log_i["elapsed_ms"]
                if exec_log_i.get("unknown"):
                    unknown_in_chain = True
                    break  # 链中遇到未知动作就停，剩下的不强行执行

            exec_log = {
                "action": display_action if is_chain else (parsed.action or "unknown"),
                "elapsed_ms": total_elapsed_ms,
                "unknown": unknown_in_chain,
                "chain_len": chain_count,
            }
            await self._emit_event(
                make_event(EVT_EXEC_RESULT, self.run_id, step=step, **exec_log)
            )
            await self._log(
                1,
                "执行完成",
                f"动作: {exec_log['action']}, 耗时: {exec_log['elapsed_ms']}ms"
                + (f"（链 ×{chain_count}）" if is_chain else ""),
                step=step,
            )

            # ④.1 未知动作连续保护
            if exec_log["unknown"]:
                self._unknown_streak += 1
                if self._unknown_streak >= UNKNOWN_ACTION_STREAK_LIMIT:
                    await self._log(
                        3,
                        "动作名连续异常",
                        f"连续 {self._unknown_streak} 次模型输出无法识别的动作，放弃",
                        step=step,
                    )
                    return False, "unknown_action_exceeded"
            else:
                self._unknown_streak = 0

            # ⑤ 尾帧：操作后截图 + 作为下一步 frame A
            try:
                tail_bytes = await self._screenshot_jpeg()
            except Exception as exc:  # noqa: BLE001
                await self._log(2, "操作后截图失败", f"错误: {exc}", step=step)
                tail_bytes = None

            if tail_bytes:
                await self._emit_screenshot(step, "after", tail_bytes)
            self._last_tail_bytes = tail_bytes

            # ⑤.4 接管步闭环验证
            #
            # 这一步是接管步（用了缓存帧 + 系统重唤起 chain）。如果 VLM 给的目标
            # click 真的命中了瞬态 UI 上的按钮（如倍速按钮 → 弹出倍速面板），
            # tail_bytes 应该和 takeover.visible_frame 显著不同（弹出新内容）；
            # 如果扑空（坐标不对 / 重唤起没成功），tail_bytes 大概率回到 click 前
            # 的样子（即和 takeover.visible_frame 差不多——因为 chain 内重唤起又
            # 让工具栏出现一次然后被认为没点中目标）。
            #
            # 这里**仅打日志、不自动重试**：让下一轮 VLM 看到接管后的实际画面，
            # 由它自己决定是再点一次还是改方案——避免系统反复重试形成死循环。
            if takeover is not None and tail_bytes is not None:
                h_visible = compute_phash(takeover.visible_frame)
                h_tail = compute_phash(tail_bytes)
                if h_visible is not None and h_tail is not None:
                    bits = hamming_distance(h_visible, h_tail)
                    rate = bits / 256.0
                    if rate > 0.05:
                        await self._log(
                            1,
                            "瞬态UI接管 · 闭环命中",
                            f"chain 后画面 vs 缓存帧差异率={rate:.3f} > 0.05，"
                            f"VLM 目标 click 应已命中（产生了新 UI 变化）",
                            step=step,
                        )
                    else:
                        await self._log(
                            2,
                            "瞬态UI接管 · 闭环可疑",
                            f"chain 后画面 vs 缓存帧差异率={rate:.3f} ≤ 0.05，"
                            f"目标 click 可能扑空（坐标偏差 / 重唤起失败 / "
                            f"工具栏寿命过短），VLM 下一步将看到接管后的实际画面"
                            f"自行决定是否重试",
                            step=step,
                        )

            # ⑤.5 瞬态 UI 检测（仅 click 类动作 / 非链式 / 非接管步触发）
            #
            # 三段式 pHash 三角检测：要看 click 是否引发"出现 → 消失 → 回退"
            # 模式。命中即把"工具栏完整可见"那帧缓存进 self._transient_snapshot，
            # 下一步 ① 检测到非空就走接管路径，下一步 ④ 执行前自动重唤起。
            #
            # 跳过条件：
            # - 链式动作：链中已经做过 chain 内点击，下一步开始也通常已经看到结果
            # - 接管步：takeover 非空时本步是接管的"目标点击"，再触发会无限套娃
            # - 非 click 类：scroll/drag/type 等不会触发瞬态 UI
            #
            # 仅在普通的、非接管的 click/long_press/double_tap 后做一次检测；
            # 且必须本次 Run 已经被动态判断系统挂载（_transient_ui_armed=True），
            # 否则普通 case 不要做这层多余计算。
            if (
                self._transient_ui_armed
                and takeover is None
                and not is_chain
                and action_type in (
                    A.ACTION_CLICK, A.ACTION_LONG_PRESS, A.ACTION_DOUBLE_TAP,
                )
                and tail_bytes is not None
            ):
                trigger_abs = await self._vlm_point_to_abs(
                    parsed.point or [500, 500], coord_space=parsed.coord_space
                )
                snapshot = await detect_transient_ui(
                    before_bytes=screenshot_bytes,
                    early_bytes=tail_bytes,
                    screenshot=self._screenshot_jpeg,
                    trigger_action=action_type,
                    trigger_point_abs=trigger_abs,
                    trigger_point_norm=parsed.point or [500, 500],
                    step=step,
                    late_delay_ms=self._transient_ui_late_delay_ms,
                    log=lambda lvl, title, content, _s=step: asyncio.create_task(
                        self._log(lvl, title, content, step=_s)
                    ),
                )
                if snapshot is not None:
                    self._transient_snapshot = snapshot
                    # 用 late_frame 替换 tail —— 它才是"瞬态 UI 自隐后"的稳定
                    # 画面，下一步若**不**走接管（例如外部 stop 或终止动作）也
                    # 应该用这帧做 frame A，不能用 visible_frame（会把已消失
                    # 的工具栏当成现状）。
                    self._last_tail_bytes = snapshot.late_frame

            # ⑥ 探测器召唤：本地零成本（pHash + 计数）先扫一遍动作侧异常，
            #    任意一条命中只是把"召唤原因"挂上去，**不直接 kill**；最终由
            #    `_maybe_audit` 一次性发到审判模型决定继续 / 终止。这样：
            #    - 多个探测器同步命中 → 合并成一次审判，省 token
            #    - 审判 ALLOW（如"功能慢，正在合法重试"）→ 重置探测器继续跑
            #    - 审判 KILL → 走 _raise_fatal，理由归咎为 case 不严谨
            #
            # 链式动作下，链内每一个 click 都过 _check_click_stuck，避免 VLM
            # 用"链式 2 击同坐标"绕过卡死检测；scroll / drag 不会进链，调一次
            # 即可。
            for parsed_i in parsed_chain:
                self._check_click_stuck(parsed_i, step=step)
            self._check_scroll_stuck(parsed, step=step)
            self._check_scroll_no_progress(
                parsed, step=step,
                before_bytes=screenshot_bytes, after_bytes=tail_bytes,
            )

            # —— 周期巡检：每 N 步主动召唤一次审判 ——
            # 兜底"VLM 走得很顺、但跳过了 case 子步骤"的盲区：本地探测器抓不到
            # （没有"反复刨"特征），只能靠周期性把 case + 最近历史交给审判模型
            # 做"步骤序列对齐"判断。详见 _supervisor_audit prompt 的"触发种类"段。
            #
            # 防抖：
            # 1. 同一 step 已有 detector trigger → 不重复挂 periodic（detector 审判
            #    会把同一份历史给到审判模型，再加一条 periodic 是 token 浪费）
            # 2. step % N == 0 才挂；step=0 / step=N+1 都不挂
            # 3. 同一 step 不重复审（_audit_called_steps 防抖）
            if (
                self._is_structured
                and step > 0
                and step % STRUCT_AUDIT_PERIODIC_INTERVAL == 0
                and not self._pending_audit_triggers
                and step not in self._audit_called_steps
            ):
                self._pending_audit_triggers.append(
                    f"{PERIODIC_TRIGGER_PREFIX} 每 {STRUCT_AUDIT_PERIODIC_INTERVAL} 步主动审"
                )

            if await self._maybe_audit(step):
                return await self._raise_fatal(step, tail_bytes or screenshot_bytes)

            # ⑦ 把"本 step 主 VLM 看到的 before 帧"滚到 _previous_before_bytes，
            #    供下一 step 万一直接 finished 时给断言系统当"动作前对照帧"。
            #    放在 step 末尾、所有提前 return 路径之外——finished/assert_fail
            #    都已在中段 return，case 已结束，不需要再滚。
            self._previous_before_bytes = screenshot_bytes

            await self._emit_event(make_event(EVT_STEP_END, self.run_id, step=step))

        # 超过安全上限
        await self._log(
            3,
            "安全中断",
            f"已达安全上限 {self.max_steps} 步，疑似死循环，强制停止",
        )
        return False, "max_steps_exceeded"

    async def _raise_fatal(
        self, step: int, screenshot_bytes: Optional[bytes]
    ) -> Tuple[bool, str]:
        """读取 ``self._fatal_signal`` 并以 assert_fail 形态终止 Run。

        与 VLM 主动 ``assert_fail()`` 走同一条通道（finish_fail 截图 + 日志 +
        ``assert_fail:`` 前缀的 reason），保证前端时间线和报告呈现一致：
        ``Run 结束 → failed`` + 红色"断言不通过"。
        """
        kind, detail = self._fatal_signal or ("fatal", "未知硬约束")
        kind_label = {
            "supervisor_kill": "审判模型判定偏离 case",
            "supervisor_exhausted": "审判 ALLOW 上限耗尽，强制终止",
            "supervisor_misbehave_kill": "VLM 不按规范执行（系统直接终止）",
        }.get(kind, kind)
        fail_msg = (
            f"【系统硬约束触发：{kind_label}】{detail}。"
            "case 与 App 实际状态偏离，按照「早死早超生」原则停止本 Run。"
        )
        if screenshot_bytes:
            await self._emit_screenshot(step, "finish_fail", screenshot_bytes)
        await self._log(
            3,
            "断言不通过（系统硬约束）",
            f"共执行 {step} 步 | {fail_msg}",
            step=step,
        )
        return False, f"assert_fail: {fail_msg}"

    # ------------------------------------------------------------------
    # 动作执行（迁移自 Groovy executeAction）
    # ------------------------------------------------------------------
    async def _execute_action(
        self,
        parsed: A.ParsedAction,
        *,
        step: int,
        settle_ms: int = POST_ACTION_SETTLE_MS,
    ) -> Dict[str, Any]:
        """执行单个动作。

        ``settle_ms``：动作执行后给 UI 渲染的缓冲时间。默认 500ms 让页面稳下来；
        链式动作（同步执行 ≥ 2 个 Action）的非末尾动作会传 0，由外层用更短的
        :data:`CHAIN_INNER_GAP_MS` 间隔统一控制，避免 UI 在链中途就把瞬态浮层
        （视频工具栏 / Toast 等 ≤ 2s 自隐的元素）藏起来。
        """
        action = parsed.action or "unknown"
        t0 = time.monotonic()

        # wait 以外的动作，清除 "刚刚长等过" 标记
        if action != A.ACTION_WAIT:
            self._last_wait_was_explicit = False

        if action == A.ACTION_CLICK:
            abs_xy = await self._vlm_point_to_abs(
                parsed.point or [500, 500], coord_space=parsed.coord_space
            )
            await self._log(1, "点击", f"坐标{abs_xy}", step=step)
            await asyncio.to_thread(self.driver.click, abs_xy[0], abs_xy[1])

        elif action == A.ACTION_DOUBLE_TAP:
            abs_xy = await self._vlm_point_to_abs(
                parsed.point or [500, 500], coord_space=parsed.coord_space
            )
            await self._log(1, "双击", f"坐标{abs_xy}", step=step)
            await asyncio.to_thread(self.driver.double_click, abs_xy[0], abs_xy[1])

        elif action == A.ACTION_LONG_PRESS:
            abs_xy = await self._vlm_point_to_abs(
                parsed.point or [500, 500], coord_space=parsed.coord_space
            )
            await self._log(1, "长按", f"坐标{abs_xy}，1000ms", step=step)
            await asyncio.to_thread(self.driver.long_press, abs_xy[0], abs_xy[1], 1000)

        elif action == A.ACTION_TYPE:
            content = parsed.content or ""
            await self._log(1, "输入", f"内容: {content!r}", step=step)
            await asyncio.to_thread(self.driver.type_text, content)

        elif action == A.ACTION_SCROLL:
            direction = parsed.direction or "down"
            # 滚动次数：Claude/GPT CU 在长列表场景常给 amount>1（"快速翻 N 屏"），
            # 不透传会一直只滑 1 屏 → 模型见截图变化不大反复 scroll → 卡死被
            # 审判 KILL。豆包路径 ParsedAction 默认 amount=1，行为不变。
            amount = max(1, int(parsed.scroll_amount or 1))
            # VLM 明确给点 → 以该点为中心做局部滑动（分块/分栏场景精准滑）
            # VLM 没给点    → 走 driver 内置全屏中线兜底（整页 list 翻页）
            if parsed.point:
                abs_xy: Optional[Tuple[int, int]] = await self._vlm_point_to_abs(
                    parsed.point, coord_space=parsed.coord_space
                )
                amount_suffix = f"，连续 {amount} 次" if amount > 1 else ""
                await self._log(
                    1, "滑动",
                    f"方向: {direction}，中心: {abs_xy}{amount_suffix}",
                    step=step,
                )
            else:
                abs_xy = None
                amount_suffix = f"，连续 {amount} 次" if amount > 1 else ""
                await self._log(
                    1, "滑动",
                    f"方向: {direction}（屏幕中线兜底）{amount_suffix}",
                    step=step,
                )
            await asyncio.to_thread(self.driver.scroll, direction, abs_xy, amount)

        elif action == A.ACTION_DRAG:
            sp = await self._vlm_point_to_abs(
                parsed.start_point or [500, 500], coord_space=parsed.coord_space
            )
            ep = await self._vlm_point_to_abs(
                parsed.end_point or [500, 500], coord_space=parsed.coord_space
            )
            await self._log(1, "拖拽", f"从{sp} → {ep}", step=step)
            await asyncio.to_thread(
                self.driver.swipe, sp[0], sp[1], ep[0], ep[1], 500
            )

        elif action == A.ACTION_WAIT:
            secs = self._decide_wait_seconds(parsed)
            # clipped=True 时把"申请 N 秒、实际 M 秒"都打到日志，避免 VLM
            # 想等很久（比如等视频播完）却被裁到默认上限，用户却找不到原因。
            if secs.get("clipped"):
                detail = (
                    f"等待 {secs['seconds']} 秒（{secs['source']}；"
                    f"申请 {secs['requested_seconds']} 秒，被 MAX_WAIT_SECONDS={MAX_WAIT_SECONDS} 裁剪）"
                )
            else:
                detail = f"等待 {secs['seconds']} 秒（{secs['source']}）"
            await self._log(1, "等待", detail, step=step)
            await asyncio.sleep(secs["seconds"])

        elif action == A.ACTION_OPEN_APP:
            app_name = parsed.name or ""
            await self._log(1, "打开App", f"应用: {app_name}", step=step)
            await self._open_app_by_name(app_name)

        elif action == A.ACTION_CLOSE_APP:
            app_name = parsed.name or ""
            await self._log(1, "关闭App", f"应用: {app_name}", step=step)
            await self._close_app_by_name(app_name)

        elif action == A.ACTION_PRESS_HOME:
            await self._log(1, "系统按键", "HOME", step=step)
            await asyncio.to_thread(self.driver.press_home)

        elif action == A.ACTION_PRESS_BACK:
            await self._log(1, "系统按键", "BACK", step=step)
            await asyncio.to_thread(self.driver.press_back)

        elif action == A.ACTION_KEY_EVENT:
            # 通用按键：Claude/GPT 输出 X11 键名（"Return"/"Tab"/"BackSpace"
            # 等），main 解析层已查表转 Android keycode。这里直接调
            # driver.press_keycode；harmony 的 keycode 表与 Android 不同，
            # 暂走"穿透 hmdriver2.press_key"，键值不一定生效（follow-up）；
            # iOS 只支持 HOME/BACK/APP_SWITCH，其他键 raise，此处 try
            # 包住转 warn 让 runner 当未知动作进入兜底而不是 fail Run。
            keycode = parsed.keycode
            if keycode is None:
                await self._log(
                    2, "按键事件失败", "缺少 keycode，跳过", step=step
                )
            else:
                await self._log(
                    1, "按键事件", f"keycode={keycode}", step=step
                )
                try:
                    await asyncio.to_thread(self.driver.press_keycode, keycode)
                except NotImplementedError as exc:
                    await self._log(
                        2, "按键事件不支持",
                        f"keycode={keycode} 在当前平台不可用：{exc}",
                        step=step,
                    )

        else:
            # 未知动作：注入修正提示供下一轮 VLM 消费
            # 提示文本按 backend 分家——豆包动作集和 Claude/GPT 的 computer
            # tool 动作集差异大（前者有 open_app/close_app/press_home 等手机
            # 自动化项目级抽象，后两家只有 PC 风格的 left_click/keypress
            # 等）。直接发豆包 DSL 给 Claude/GPT 会让它们主动模仿，把
            # ``open_app(app_name='X')`` 整串当 type 的 text 输入到屏幕，
            # 完全跑偏（实测 error52 step3）。详见 prompts/__init__.py 注释。
            await self._log(
                2, "未知动作", f"无法识别: {action}，已注入修正提示", step=step
            )
            self.vlm.add_hint(
                build_unknown_action_hint(
                    action, backend=self._settings.vlm_backend
                )
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {"action": action, "elapsed_ms": elapsed_ms, "unknown": True}

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # settle_ms：动作之后给 UI 渲染的缓冲（默认 500ms；链内非末尾动作传 0）
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
        return {"action": action, "elapsed_ms": elapsed_ms, "unknown": False}

    def _decide_wait_seconds(self, parsed: A.ParsedAction) -> Dict[str, Any]:
        """对齐 Groovy wait 三档逻辑。"""
        action_sec = parsed.seconds
        thought_sec = A.extract_seconds_from_thought(parsed.thought or "")

        if action_sec is not None:
            secs = action_sec
            source = "模型指定"
            self._last_wait_was_explicit = secs > 3
        elif thought_sec is not None and not self._last_wait_was_explicit:
            secs = thought_sec
            source = "Thought 兜底"
            self._last_wait_was_explicit = secs > 3
        else:
            secs = DEFAULT_WAIT_SECONDS
            source = (
                "默认轮询（上轮已长等，本轮防双等）"
                if self._last_wait_was_explicit
                else "默认轮询"
            )
            self._last_wait_was_explicit = False

        # 同时返回"申请秒数"和"实际秒数"：调用方看 clipped=True 就能在日志里
        # 明确告诉用户"VLM 想等 N 秒，被 MAX_WAIT_SECONDS 裁到 M 秒"，便于
        # 在长 wait 场景排查"等待为什么被吃了"。
        requested = int(secs)
        actual = max(1, min(requested, MAX_WAIT_SECONDS))
        return {
            "seconds": actual,
            "requested_seconds": requested,
            "source": source,
            "clipped": actual != requested,
        }

    # ------------------------------------------------------------------
    # 卡死检测
    # ------------------------------------------------------------------
    def _check_click_stuck(self, parsed: A.ParsedAction, *, step: int) -> None:
        click_like = {A.ACTION_CLICK, A.ACTION_DOUBLE_TAP, A.ACTION_LONG_PRESS}
        if parsed.action not in click_like or not parsed.point:
            return
        point = (parsed.point[0], parsed.point[1])
        # 记录 step → click 坐标，供 _check_screen_revisit 做"动作差异豁免"判定。
        # 仅 click 类动作记录；scroll / wait / back 等不写入。
        self._step_click_xy[step] = point

        # —— 结构化 case 探测：同区域点击累计（**不要求相邻**） ——
        # 解决 VLM "进错误页 → 关掉 → 又点同一个按钮 → 又进错误页" 的死循环。
        # 用质心聚类（≤ STRUCT_CLICK_BUCKET_PX 视为同一按钮）累计 Run 内点击次数，
        # 命中 ≥ STRUCT_CLICK_BUCKET_TRIGGER 即把"召唤原因"挂到 pending 队列，
        # 由 step 收尾的 _maybe_audit 决定是 KILL 还是 ALLOW（功能慢就允许重试）。
        if self._is_structured:
            matched: Optional[List[int]] = None
            for bucket in self._click_buckets:
                if (
                    abs(bucket[0] - point[0]) <= STRUCT_CLICK_BUCKET_PX
                    and abs(bucket[1] - point[1]) <= STRUCT_CLICK_BUCKET_PX
                ):
                    matched = bucket
                    break
            if matched is None:
                self._click_buckets.append([point[0], point[1], 1])
            else:
                # 质心微调（向新点漂一点点），count + 1
                matched[0] = (matched[0] + point[0]) // 2
                matched[1] = (matched[1] + point[1]) // 2
                matched[2] += 1
                if matched[2] >= STRUCT_CLICK_BUCKET_TRIGGER:
                    self._pending_audit_triggers.append(
                        f"同坐标桶(~{matched[0]},{matched[1]}) 累计点击 {matched[2]} 次"
                        f"（最近一次精确点 {point}）"
                    )

        self._recent_clicks.append(point)
        if len(self._recent_clicks) > CLICK_STUCK_THRESHOLD:
            self._recent_clicks.pop(0)
        if len(self._recent_clicks) >= CLICK_STUCK_THRESHOLD:
            first = self._recent_clicks[0]
            if all(
                abs(p[0] - first[0]) < 30 and abs(p[1] - first[1]) < 30
                for p in self._recent_clicks
            ):
                msg = (
                    f"注意：你已经连续{CLICK_STUCK_THRESHOLD}次点击了几乎相同的位置，"
                    "但似乎没有生效。请优先排查【最常见原因】："
                    "**该子步骤的目标状态可能已经达成**——比如「进入 X 页」时 Tab 已高亮、"
                    "「切换为 X」时页签已显示 X，这种情况下根本不需要再点，应该直接"
                    "跳到下一条子步骤（Thought 写明「截图显示 X 已满足，跳过子步骤 N」）。"
                    "若确认尚未达成，再考虑：①目标元素是否唤起后短时间内会自动消失"
                    "（自动隐藏的控件 / 浮层）——这种情况请按 §C 用链式动作（同 Thought 下"
                    "连写 2 个 Action：先唤起，再立即点击/拖动目标）；②滑动页面查找目标 / "
                    "点击元素的不同区域 / 检查是否有弹窗遮挡。"
                )
                self.vlm.add_hint(msg)
                asyncio.create_task(
                    self._log(
                        2,
                        "卡死检测",
                        f"连续{CLICK_STUCK_THRESHOLD}次点击相同位置{first}，注入提示",
                        step=step,
                    )
                )
                self._recent_clicks.clear()

    def _check_scroll_stuck(self, parsed: A.ParsedAction, *, step: int) -> None:
        if parsed.action != A.ACTION_SCROLL:
            self._scroll_streak = (None, 0)
            return
        direction = parsed.direction or "down"

        # —— 结构化 case 探测：滚动方向震荡 ——
        # "上滑→下滑→上滑"这种"东找找西找找"的发散场景，命中即把召唤原因
        # 挂上 pending，等 step 收尾时由审判定夺。窗口内累计翻转次数，
        # 单次召唤后**清空 history**，避免同一段震荡被反复召唤造成审判抖动。
        if self._is_structured:
            self._scroll_history.append(direction)
            window = self._scroll_history[-STRUCT_SCROLL_FLIP_WINDOW:]
            flips = sum(
                1 for i in range(1, len(window)) if window[i] != window[i - 1]
            )
            if flips >= STRUCT_SCROLL_FLIP_TRIGGER:
                self._pending_audit_triggers.append(
                    f"滚动方向震荡：最近 {len(window)} 次方向 {','.join(window)}，"
                    f"翻转 {flips} 次"
                )
                self._scroll_history.clear()

        prev_dir, count = self._scroll_streak
        if direction == prev_dir:
            count += 1
        else:
            count = 1
        self._scroll_streak = (direction, count)

        if count >= SCROLL_STUCK_THRESHOLD:
            msg = (
                f"注意：你已经连续{SCROLL_STUCK_THRESHOLD}次向{direction}滚动了。"
                "如果要找的内容不在当前方向，请尝试反方向滚动或使用其他操作。"
            )
            self.vlm.add_hint(msg)
            asyncio.create_task(
                self._log(
                    2,
                    "卡死检测",
                    f"连续{SCROLL_STUCK_THRESHOLD}次向{direction}滚动，注入提示",
                    step=step,
                )
            )
            self._scroll_streak = (direction, 0)

    def _check_scroll_no_progress(
        self,
        parsed: A.ParsedAction,
        *,
        step: int,
        before_bytes: Optional[bytes],
        after_bytes: Optional[bytes],
    ) -> None:
        """结构化 case 硬约束：滑动未带来视觉变化 → 判定真到底/不可滚。

        与 ``_check_scroll_stuck`` 互补：那条只数 VLM 输出的方向，对"页面在动 vs
        不动"零感知。本检测对每次 scroll 拿前后帧 pHash 比，diff ≤
        ``STRUCT_SCROLL_NOPROGRESS_DIFF`` 视为页面没动；同方向连续 ≥
        ``STRUCT_SCROLL_NOPROGRESS_TRIGGER`` 次没动即把"召唤原因"挂上去，
        最终是否 kill 由审判裁决。
        """
        if not self._is_structured:
            return
        if parsed.action != A.ACTION_SCROLL:
            return
        if not (before_bytes and after_bytes):
            return
        h_before = compute_phash(before_bytes)
        h_after = compute_phash(after_bytes)
        if h_before is None or h_after is None:
            return
        diff = hamming_distance(h_before, h_after) / 256.0
        direction = parsed.direction or "down"
        if diff <= STRUCT_SCROLL_NOPROGRESS_DIFF:
            prev_dir, prev_n = self._scroll_no_progress
            if prev_dir == direction:
                prev_n += 1
            else:
                prev_n = 1
            self._scroll_no_progress = (direction, prev_n)
            if prev_n >= STRUCT_SCROLL_NOPROGRESS_TRIGGER:
                self._pending_audit_triggers.append(
                    f"无效滑动：向 {direction} 已连续 {prev_n} 次滑但 pHash"
                    f" 几乎不变（diff={diff:.3f}），疑似真到底 / 不可滚 / 落点错"
                )
                self._scroll_no_progress = (None, 0)
        else:
            self._scroll_no_progress = (None, 0)

    def _check_screen_revisit(
        self, screenshot_bytes: Optional[bytes], *, step: int
    ) -> None:
        """结构化 case 探测：同一屏被反复访问 → 召唤审判。

        VLM 经常陷入"点 A → 进错误页 → 关掉 → 点 A → 又进错误页"或
        "在错误弹窗里前后翻滚多次回到顶部"的死循环。光看 click 坐标抓不住——
        因为 VLM 中间会穿插 scroll/back。但**截图指纹**会反复重合，是抓
        "无进度"的最可靠信号。

        实现：用 16x16 pHash（256 bit）；汉明距 ≤ STRUCT_SCREEN_REVISIT_HAMMING
        视为同屏；同屏访问累计达 STRUCT_SCREEN_REVISIT_TRIGGER 即把召唤原因
        挂到 pending，由 step 收尾的审判定夺。
        """
        if not self._is_structured or not screenshot_bytes:
            return
        h = compute_phash(screenshot_bytes)
        if h is None:
            return

        match_idx: Optional[int] = None
        for i, (old_h, _steps) in enumerate(self._screen_visits):
            if hamming_distance(h, old_h) <= STRUCT_SCREEN_REVISIT_HAMMING:
                match_idx = i
                break

        if match_idx is None:
            self._screen_visits.append((h, [step]))
            if len(self._screen_visits) > 30:
                self._screen_visits.pop(0)
            return

        _old_h, steps = self._screen_visits[match_idx]
        steps.append(step)
        if len(steps) >= STRUCT_SCREEN_REVISIT_TRIGGER:
            # —— 豁免：弹窗 / 选择器内的合理多步操作 ——
            # 弹窗类容器（多级选择器 / 内容选择器 / 分类切换器）天然让大量像素重复，
            # pHash 会把"逐级下钻不同子项"误判为"卡在同屏"。这里反查这些 step
            # 已记录的 click 坐标，如果 ≥ 2 个 click 落在不同坐标桶（间距 >
            # STRUCT_CLICK_BUCKET_PX），认为 VLM 在合理切换不同元素 → 豁免本次召唤。
            # 真正的卡死（同屏 + 同坐标 N 次）仍会被 _check_click_stuck 抓到，
            # 不影响"反复点同一按钮"这个核心场景的检测。
            if self._steps_have_distinct_clicks(steps):
                # 重置当前桶的 step 计数，但保留 phash 指纹与 step 记录种子，
                # 让下次重新累计——避免一次豁免后永久关闭对该屏幕的检测。
                last_step = steps[-1]
                steps.clear()
                steps.append(last_step)
                return
            self._pending_audit_triggers.append(
                f"同屏复访问：相同屏幕已被访问 {len(steps)} 次（步号 {steps}）"
            )
            steps.clear()

    def _steps_have_distinct_clicks(self, steps: List[int]) -> bool:
        """判断 steps 列表里已记录的 click 坐标是否落在 ≥ 2 个不同坐标桶。

        若是 → VLM 在同一屏内点击了不同元素，是合理的弹窗内多步操作；
        若否（坐标都在同一桶 / 没有 click 信息）→ 不豁免，按原逻辑召唤审判。
        """
        known: List[Tuple[int, int]] = [
            self._step_click_xy[s] for s in steps if s in self._step_click_xy
        ]
        if len(known) < 2:
            return False
        for i in range(len(known)):
            for j in range(i + 1, len(known)):
                if (
                    abs(known[i][0] - known[j][0]) > STRUCT_CLICK_BUCKET_PX
                    or abs(known[i][1] - known[j][1]) > STRUCT_CLICK_BUCKET_PX
                ):
                    return True
        return False

    # ------------------------------------------------------------------
    # 审判通道：触发后由独立的轻量文本模型裁决"继续 vs KILL"
    # ------------------------------------------------------------------
    async def _maybe_audit(self, step: int) -> bool:
        """如本步有审判触发原因（detector 或 periodic），发审判调用判定继续 / kill。
        返回 True 表示应当 kill。

        三种"立刻 kill"短路：
        1. 已被审判 ALLOW 上限次数（防止审判被反复骗）→ 直接 supervisor_exhausted
           注意：仅 **detector** 触发的 ALLOW 计入上限；周期巡检 ALLOW 不计
           （否则 30 步的 case 单是周期巡检 OK 就会把 ALLOW 配额耗尽，等到真正
           的 detector 触发时无配额可用 → 误杀）。
        2. 审判返回 KILL → supervisor_kill
        3. 审判调用失败 / 超时 → 不 kill（保守 ALLOW，记 WARN）

        防抖：同一 step 不重复审；审完 / 跳过都清空 _pending_audit_triggers。
        """
        # 总是先抓快照、清队列：异常路径也不能让 trigger 残留到下一步
        triggers = list(self._pending_audit_triggers)
        self._pending_audit_triggers.clear()

        if not self._is_structured or not triggers:
            return False
        if step in self._audit_called_steps:
            # 同一 step 已审过（如 step 起始 screen_revisit 召唤 + step 收尾
            # click 召唤同时挂上）→ 不重复扣 token，复用上次结论：上次 ALLOW 就继续
            return False
        self._audit_called_steps.add(step)

        # 触发种类：全部都是周期巡检 → "巡检模式"；含任意 detector → "异常模式"
        # detector 触发抓"反复刨"，周期巡检抓"走得很顺但跳步"，两种侧重不同
        is_periodic_only = all(
            t.startswith(PERIODIC_TRIGGER_PREFIX) for t in triggers
        )

        # ALLOW 上限：仅检查 detector 触发场景；周期巡检 OK 不该耗配额
        if (
            not is_periodic_only
            and self._audit_allow_count >= STRUCT_AUDIT_ALLOW_LIMIT
        ):
            self._fatal_signal = (
                "supervisor_exhausted",
                f"审判已 ALLOW {self._audit_allow_count} 次（上限 {STRUCT_AUDIT_ALLOW_LIMIT}）"
                f"仍未推进，本次新触发：{'; '.join(triggers)}",
            )
            await self._log(
                3,
                "审判 · ALLOW 上限耗尽",
                f"已 ALLOW {self._audit_allow_count} 次，本次直接 KILL",
                step=step,
            )
            return True

        # 调审判
        trigger_text = " / ".join(triggers)
        await self._log(
            2,
            "审判 · 巡检" if is_periodic_only else "审判 · 召唤",
            f"触发原因：{trigger_text}",
            step=step,
        )
        try:
            verdict, reason = await asyncio.wait_for(
                self._supervisor_audit(trigger_text, step, is_periodic_only),
                timeout=STRUCT_AUDIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._log(
                2,
                "审判 · 超时",
                f"审判调用超过 {STRUCT_AUDIT_TIMEOUT_SECONDS}s 未返回，本次按 ALLOW 处理",
                step=step,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            await self._log(
                2,
                "审判 · 调用失败",
                f"审判调用异常：{exc}，本次按 ALLOW 处理（不阻塞 Run）",
                step=step,
            )
            return False

        if verdict == "KILL":
            self._fatal_signal = ("supervisor_kill", reason or "审判模型未提供理由")
            await self._log(
                3,
                "审判 · KILL",
                f"理由：{reason}",
                step=step,
            )
            return True

        # OK 路径分流：
        # - 周期巡检 OK：常态，巡检通过不该惩罚 VLM。不计 ALLOW、不重置探测器、不注入 hint
        # - detector OK：本地探测器疑似抓到偏离但模型放行，要扣配额 + 重置探测器 + 注入提醒
        if is_periodic_only:
            await self._log(
                1,
                "审判 · 巡检通过",
                f"周期巡检：步骤推进合规 | 理由：{reason}",
                step=step,
            )
            return False

        self._audit_allow_count += 1
        await self._log(
            1,
            "审判 · ALLOW",
            f"第 {self._audit_allow_count}/{STRUCT_AUDIT_ALLOW_LIMIT} 次放行 | 理由：{reason}",
            step=step,
        )
        self._click_buckets.clear()
        self._scroll_history.clear()
        self._scroll_no_progress = (None, 0)
        self.vlm.add_hint(
            f"【审判系统提示】系统检测到你疑似偏离 case（{trigger_text}），"
            f"已被审判模型放行（第 {self._audit_allow_count}/{STRUCT_AUDIT_ALLOW_LIMIT} 次）。"
            "请严格按 case 的「前置条件 / 操作步骤」推进，不要再尝试 case 没说的"
            "别的入口；如确认无法完成，请直接 assert_fail。"
        )
        return False

    async def _supervisor_audit(
        self, trigger_text: str, step: int, is_periodic_only: bool = False
    ) -> Tuple[str, str]:
        """独立的轻量文本模型审判：根据 case + 动作历史判定 OK / KILL。

        触发种类（``is_periodic_only``）：
        - ``False``（默认）：本地探测器召唤——抓"反复刨"。审判主要看 a/b/c 标准
        - ``True``：周期巡检（每 N 步主动审一次）——兜底"VLM 走得很顺、但跳过了
          case 子步骤"。这种偏离本地探测器抓不到（没有反复模式），只能靠对照 case
          操作步骤序列检查；判决重点切到标准 d（跳步）。

        输出协议（极简化，让模型只做二选一）：
        - 第一行 ``OK`` 或 ``KILL:<理由>``
        - 后续任意，忽略
        """
        history = self._format_action_history(STRUCT_AUDIT_HISTORY_LIMIT)
        history_count = len(self._action_log[-STRUCT_AUDIT_HISTORY_LIMIT:])

        # 巡检模式专用补充段：明确"按顿号顺序"+ "合法跳过 vs 违规跳过"边界。
        # 关键设计：默认 OK——单凭"VLM 没显式说自己跳过了 X"不能 KILL，因为 VLM
        # 可能确实做了步骤但 thought 没说全。KILL 必须举出 case 里的明确子步骤
        # + VLM 实际执行的与之矛盾的动作 + 截图无"已满足"证据这三连。
        periodic_block = ""
        if is_periodic_only:
            periodic_block = (
                f"\n【⚠️ 本次为周期巡检模式（每 {STRUCT_AUDIT_PERIODIC_INTERVAL} 步主动审一次）】\n"
                "周期巡检和 detector 召唤的差异：本次不是因为 VLM 反复刨，而是\n"
                "系统主动来对照 case 操作步骤顺序检查 VLM 是否在按节奏推进。\n\n"
                "【步骤序列对齐方法】\n"
                "1. 把 case「操作步骤」段落用顿号「、」/逗号「，」/句号「。」拆成\n"
                "   有序子步骤数组 S = [s1, s2, ..., sn]。\n"
                "2. 把 VLM 最近 N 步的动作映射到 S 的某个 si。\n"
                "3. 检查映射顺序是否单调推进；中间被跳过的 si 必须满足"
                "「合法跳过」条件，否则属于偏离。\n\n"
                "【合法跳过判定标准】（这些情况下子步骤被跳过 = 合法）\n"
                "  ① 子步骤是条件式（如「如果未登录则登录」），且条件不成立\n"
                "  ② 子步骤目标状态在跳过当时**已经满足**（例如「切换为 X」"
                "时页签已是 X、「进入 X 页 / Tab」时 Tab 已高亮且页面已是 X）\n"
                "  ③ VLM 的 Thought 明确说明「截图显示 X 已满足，跳过子步骤 Y」\n\n"
                "【违规跳过判定标准】（满足任一即偏离 → KILL）\n"
                "  ① VLM 直接执行了 si+k（k≥1），跳过 si，但 si 不是条件式\n"
                "    且当时无证据显示 si 的目标状态已满足\n"
                "  ② VLM 把前面的子步骤「延后」到后续页面里完成（即使后续页面\n"
                "    确实有同样入口——case 没说从那入就不准从那入）\n"
                "  ③ 多个子步骤被合并执行但顺序错位（顿号是有序的，不是无序集）\n\n"
                "【KILL 输出要求（巡检模式）】\n"
                "  必须举出：\n"
                "    a. case 里被跳过的子步骤原文（顿号分隔的某一段）\n"
                "    b. VLM 实际在哪一步做了什么动作（具体 step 号 + 动作）\n"
                "    c. 为什么不构成「合法跳过」（截图无「已满足」证据 + thought 没说明）\n"
            )

        # 通用 prompt + 巡检模式增强段
        prompt = (
            "你是测试用例执行裁判。VLM Agent 在执行结构化用例时被本地探测器或周期巡检召唤。\n"
            "**召唤 ≠ 确凿偏离**——请严格保守判定。\n\n"
            "【判决优先级】\n"
            "  默认：OK（保守放行）\n"
            "  例外：只有以下四种情况之一**且能在最近动作历史里举出具体步号 + 具体动作证据**\n"
            "  时，才能 KILL：\n"
            "    a) VLM 反复点击同一坐标桶——最近 N 步里 ≥ 6 次 click 落在同一区域\n"
            "       （坐标差距 < 50px），且页面没有任何推进；\n"
            "    b) VLM 进入了用例完全没有提及的入口/页面，且持续游离不回归；\n"
            "    c) VLM 在同一弹窗/错误页反复进出 ≥ 6 次仍无法跳出；\n"
            "    d) **跳步偏离**：VLM 跳过了 case 操作步骤里的某个子步骤，且不属于"
            "「合法跳过」（详见周期巡检模式专项标准）。\n\n"
            "【禁止条款】（违反任意一条 → 你的输出会被判为无效，按 OK 处理）\n"
            "  - 禁止以「用例描述模糊/不严谨」作为唯一 KILL 理由。VLM 没真正卡住时，\n"
            "    case 写得粗一点完全无所谓，不构成 KILL 依据。\n"
            "  - 禁止引用 VLM 还没访问到的字段 / 控件作为 KILL 依据\n"
            "    （即：case 后段才需要操作的元素，VLM 还没走到那一步时不能拿它做 KILL 借口）\n"
            "  - KILL 理由必须包含至少一个最近动作历史里的具体步号（如「step 7」）。\n"
            "  - 拿不准、举不出具体步号证据时 → 一律 OK。\n\n"
            "【典型 OK 场景】（不要 KILL）\n"
            "- VLM 在同一弹窗内**点击不同元素**逐级下钻（多级选择弹窗里逐级选择）\n"
            "- VLM 因加载慢而 wait / 重试同一动作 1-2 次\n"
            "- VLM 关闭非业务弹窗 / 回退 / 等待 / 重新取焦输入框\n"
            "- VLM 切换分类 / 章节 / Tab 寻找符合 case 状态要求的资源（case 允许的兜底）\n"
            "- 子步骤目标状态在截图里已经满足，VLM 直接进入下一子步骤（合法跳过）\n\n"
            f"{periodic_block}"
            "【输出格式】（只输出第一行，多余文字会被丢弃）\n"
            "  OK\n"
            "  或\n"
            "  KILL:<必须包含具体步号与具体动作。\n"
            "        detector 例：「step 7、8、9 都在 (140,403) 附近点击同一图标 X，页面无任何推进」\n"
            "        periodic 例：「case 子步骤『切换 X 为 Y、选择 Z』被跳过，"
            "step 4 直接点了『下一级入口 W』，截图当前 X 仍是默认值 V 未切换，thought 也未声明"
            "已满足跳过」>\n\n"
            f"=== 用户测试用例（原文）===\n{self.goal}\n\n"
            f"=== 本次审判触发原因 ===\n{trigger_text}\n\n"
            f"=== 最近 {history_count} 步思考与动作 ===\n{history}"
        )
        raw = await self._chat_text(
            prompt,
            label="审判",
            thinking_enabled=get_settings().assistant_thinking_judge,
        )
        first_line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        upper = first_line.upper()
        if upper == "OK" or upper.startswith("OK:") or upper.startswith("OK "):
            reason = first_line.split(":", 1)[-1].strip() if ":" in first_line else "正常推进"
            return "OK", reason
        if upper.startswith("KILL"):
            reason = first_line.split(":", 1)[-1].strip() if ":" in first_line else "审判模型未提供理由"
            return "KILL", reason
        # 模型抽风返回非协议内容 → 保守 ALLOW（宁可漏抓不可错杀）
        logger.warning("审判模型返回非协议内容: {!r}，按 OK 处理", first_line)
        return "OK", f"审判模型输出无法解析（{first_line[:60]}），保守放行"

    def _format_action_history(self, limit: int) -> str:
        """把最近 N 步的 (step, thought, action_str) 格式化成审判可读的文本。

        ⚠️ **不要再加截断**。历史教训：
          - v1: thought 截到 120 → 切掉「子步骤 X 已满足，跳过」声明 → 误 KILL
          - v2: 提到 600 → 长 thought（>600 字）依然被切，同样误 KILL
          - v3 (当前): 完全不截断 —— 让审判看 VLM 全部推理，避免任何因截断
            造成的语义丢失

        担心 token 失控？完全没必要：
          - STRUCT_AUDIT_HISTORY_LIMIT=10，最多 10 步
          - 极端情况下每步 thought 2000 字 → 总 ≈ 20K 字 ≈ 30K tokens
          - 1.6 模型 context 几百 K，离爆还远
          - 实际跑 case 平均每次审判 prompt ≈ 8K-30K tokens，缓存命中率高

        若未来真的发现某 case thought 暴涨到几万字（极端罕见），优先排查 VLM
        是否陷入循环复述，而不是再加截断。
        """
        if not self._action_log:
            return "(无)"
        rows = self._action_log[-limit:]
        lines = []
        for row in rows:
            thought = (row.get("thought") or "").strip().replace("\n", " ")
            action = (row.get("action_str") or "").strip().replace("\n", " ")
            lines.append(f"step {row['step']:>3} 思考:{thought} 动作:{action}")
        return "\n".join(lines)

    async def _classify_structured_via_supervisor(self) -> bool:
        """中等档严格度 + 关键字弱时，借审判模型一次性分类 goal 是否走结构化通道。

        只在 run() 启动时调用一次，结果缓存到 self._is_structured。
        失败 / 超时由调用方 try-except，本方法只负责调用 + 解析。

        注：分类标准比 QA case 模板更宽——只要用户的诉求包含**严格约束 +
        大量定语 + 明确步骤序列**，即便没有「测试标题」这种标签也算 STRUCTURED；
        只有"短口语化 + 模糊请求"才算 FREEFORM。
        """
        prompt = (
            "你是用户输入分类器。判断以下用户输入是否需要走「严格执行通道」"
            "（VLM Agent 必须按字面照做、禁止自由发挥）。\n\n"
            "【走严格通道（STRUCTURED）的特征——任一即可】\n"
            "1. 含「测试标题/前置条件/操作步骤/预期结果」等分节标签\n"
            "2. 含密集的定语限定（如「第 1 章第 1 节第 1 张未开始的视频卡片」"
            "、「全宽卡片」、「左上角返回箭头」）\n"
            "3. 含明确的条件分支（「若...则...否则...」、「按相同规则改用...」、"
            "「直至找到...」）\n"
            "4. 含明确数值约束（30%、第 N 步、走至 30% 位置）\n"
            "5. 含明确动作序列（「点击 → 等待 → 返回 → 校验」）\n"
            "6. 通常很长（数百字以上），是 QA / PM 写给自动化的精细 case\n\n"
            "【走自由通道（FREEFORM）的特征】\n"
            "- 口语化短请求（如「帮我打开微信发消息」、「查一下今天天气」）\n"
            "- 没有明确步骤、没有定语限定、没有数值约束\n"
            "- 模糊或开放式（「看看怎么样」、「随便逛逛」）\n\n"
            "【判定原则】\n"
            "- 用户写得越严谨、越长、越多定语 → 越倾向 STRUCTURED\n"
            "- 用户写得越口语、越短、越模糊 → 越倾向 FREEFORM\n"
            "- 拿不准时倾向 STRUCTURED（误开严格通道只是多了审判保护，"
            "误关严格通道会让 VLM 自由发挥造成错乱）\n\n"
            f"=== 用户输入 ===\n{self.goal}\n\n"
            "请只输出第一行：\n"
            "  STRUCTURED\n"
            "  或\n"
            "  FREEFORM"
        )
        raw = await asyncio.wait_for(
            self._chat_text(prompt, label="结构化分类"),
            timeout=STRUCT_AUDIT_TIMEOUT_SECONDS,
        )
        first_line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        return first_line.upper().startswith("STRUCTURED")

    async def _extract_struct_substeps(self) -> Optional[str]:
        """让助手模型把 case「操作步骤」按顿号 / 逗号 / 句号拆成有序编号清单。

        返回 markdown 编号列表（每行一条 ``N. <子步骤>``）。失败 / 协议异常 /
        模型输出 NONE → 返回 None，调用方按"清单不可用"渲染原 prompt。

        设计目标：解决 VLM 单帧推理无状态的盲区——VLM 不内置"我做到第几步"的
        进度，每轮都从一长串顿号串里临时拆解，成本高且容易直觉短路（看到显眼
        按钮就跳）。把 case 在起跑线就拆好，常驻 system prompt 顶部，VLM 每轮
        都能直观看到"完整地图"。
        """
        prompt = (
            "你是测试用例解析器。请把下面用户输入的「操作步骤」段落，按"
            "顿号「、」、逗号「，」、句号「。」分隔成有序编号清单。\n\n"
            "【硬约束 · 必须严格执行】\n"
            "1. 只拆「操作步骤」段落，**不要**拆「测试标题/前置条件/预期结果」\n"
            "2. 保留原文措辞，不改写、不合并、不简化、不增删\n"
            "3. **顿号「、」/ 逗号「，」/ 句号「。」都是子步骤的硬边界**——\n"
            "   每遇到一个分隔符就必须断一行，禁止把多个动作合在一条里\n"
            "4. 一条子步骤里**只允许有一个动作**（动词如 点击/切换/进入/拖拽/返回等）；\n"
            "   如果你写出来的一条里出现了 2 个及以上动词，说明你忘了断行，必须再拆\n"
            "5. 括号内的补充说明（如「（工具栏 3 秒后会消失）」）保留在所属子步骤里\n"
            "6. 每行一个子步骤，格式严格为：N. <原文片段>\n"
            "7. 整段「操作步骤」不存在 / 无法识别时，只输出一行：NONE\n\n"
            "【正确拆解示例】\n"
            "原文：『从底部 Tab 进入「我的」、点击顶部头像，"
            "进入个人资料页。点击「账号安全」入口、下滑至底部点击「注销账号」』\n"
            "正确输出：\n"
            "1. 从底部 Tab 进入「我的」\n"
            "2. 点击顶部头像\n"
            "3. 进入个人资料页\n"
            "4. 点击「账号安全」入口\n"
            "5. 下滑至底部点击「注销账号」\n"
            "（注意：5 个分隔符 → 5 条子步骤；每条只有一个动词；不允许合并）\n\n"
            "【输出格式】\n"
            "  仅输出编号清单（首行就是 1.）；或单独一行 NONE。\n"
            "  禁止任何前置说明、Markdown 标题、引导语、结尾总结。\n\n"
            "===== 用户输入 =====\n"
            f"{self.goal}"
        )
        try:
            raw = await asyncio.wait_for(
                self._chat_text(prompt, label="子步骤拆解"),
                timeout=STRUCT_AUDIT_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            await self._log(
                2,
                "子步骤清单 · 拆解失败",
                f"调用异常：{exc}（不影响 Run 继续，按无清单模式渲染 prompt）",
            )
            return None

        text = raw.strip()
        if not text or text.upper().startswith("NONE"):
            await self._log(
                1,
                "子步骤清单 · 跳过",
                "case 不含可识别的「操作步骤」段落，未注入清单",
            )
            return None

        # 协议校验：第一行必须是 "1." 开头。模型抽风返回大段散文 / 加了引导语
        # / 输出 markdown 标题时直接放弃注入，避免污染 system prompt 顶部。
        first_line = text.splitlines()[0].strip()
        if not first_line.startswith("1."):
            await self._log(
                2,
                "子步骤清单 · 协议异常",
                f"返回非编号清单（首行：{first_line[:40]}），未注入",
            )
            return None

        return text

    async def _chat_text(
        self,
        prompt: str,
        *,
        label: str = "审判",
        thinking_enabled: bool = False,
    ) -> str:
        """辅助系统统一文本调用入口：委托给当前 assistant 后端。

        承载三个文本场景（label 取值）：
        - "结构化分类"（通道判定）：thinking_enabled=False
        - "审判"（防偏移）：thinking_enabled=True（由 assistant_thinking_judge 控制）
        - "子步骤拆解"（起跑线对 case 操作步骤分段）：thinking_enabled=False

        其余协议细节（消息体、thinking 字段、token 累加）由对应 assistant 实现
        承担。切换 assistant_backend 时本函数零改动。
        """
        return await self._assistant.chat_text(
            prompt, label=label, thinking=thinking_enabled
        )

    # ------------------------------------------------------------------
    # 断言系统：finished 终局裁决（走 1.6 通用版，独立于主 VLM）
    # ------------------------------------------------------------------
    async def _verify_finished_assertion(
        self,
        *,
        prev_before_bytes: Optional[bytes],
        final_bytes: bytes,
        thought: str,
        finish_msg: str,
        step: int,
    ) -> Tuple[str, str]:
        """断言系统：在主 VLM 输出 ``finished()`` 后做一次终局裁决。

        统一走 ``assistant_*``（默认 1.6 通用版），避免"主 VLM 自验主 VLM"。
        1.6 通用版同样支持图像输入。

        输入双图（"动作前/后"对照模式）：

        - ``prev_before_bytes``：上一个 step 主 VLM 看到的 before 帧，也即
          finished 那一击之前的画面。跨度始终为"最后一个动作"，不会被 case
          长度污染。第一步就 finished 时该参数为 ``None``，自动退化为单图模式。
        - ``final_bytes``：finished 这一步主 VLM 看到的截图，也是断言要验收
          的"最终落点"。

        prompt 会指引模型把两张图作为"动作前/后"对照，配合主 VLM 的 thought
        和 finish_msg 文本，判断"thought 自述的状态变化是否真的发生"——既能
        正确放过过程导向 case（"点击返回"），也能识破状态导向 case 的伪成功
        （"拖到 30%" 但截图无变化）。

        约束：
        - 只读，不允许继续执行任何 step
        - 只返回 PASS / FAIL / SKIP 三态
        - FAIL 有权把 finished 改写成 assert_fail
        - SKIP 表示配置缺失 / 请求失败 / 协议无法解析，此时回退到历史行为
        """
        settings = self._settings
        # 配置缺失检查保留在 vlm_loop——业务侧需要在 ASSISTANT_* 三件套缺失时
        # 走 SKIP 路径回退主 VLM 结果，而不是抛异常打断 Run。
        if not (
            (settings.assistant_api_key or settings.vlm_api_key)
            and settings.assistant_api_url
            and settings.assistant_model
        ):
            reason = "断言系统配置缺失，跳过裁决，回退采纳主 VLM 结果"
            await self._log(2, "断言系统 · 跳过", reason, step=step)
            return ("SKIP", reason)

        has_prev = prev_before_bytes is not None
        prompt = self._build_finished_assertion_prompt(
            thought=thought,
            finish_msg=finish_msg,
            has_prev=has_prev,
        )

        await self._log(
            1,
            "断言系统 · 复核",
            "主VLM 已申请 finished，断言系统开始最终裁决（不再继续执行步骤）"
            f" | 对照模式={'双图(动作前/后)' if has_prev else '单图(无前置帧)'}",
            step=step,
        )

        try:
            # 走当前 assistant 的 verify_finished 实现：协议层（图片 base64
            # 编码、消息体构造、thinking 字段、token 累加）由各家协议适配层负
            # 责，本函数继续承担"业务编排"——配置校验、SKIP 路径、PASS/FAIL
            # 解析、调用日志。切换 assistant_backend 时本函数零改动。
            text = await asyncio.wait_for(
                self._assistant.verify_finished(
                    prompt=prompt,
                    prev_before_bytes=prev_before_bytes,
                    final_bytes=final_bytes,
                    thinking=settings.assistant_thinking_assertion,
                ),
                timeout=ASSERTION_SYSTEM_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"断言系统调用失败：{exc}；回退采纳主 VLM 结果"
            await self._log(2, "断言系统 · 调用失败", reason, step=step)
            return ("SKIP", reason)

        first_line = text.splitlines()[0].strip() if text else ""

        if first_line.upper().startswith("PASS:"):
            return (
                "PASS",
                first_line.split(":", 1)[1].strip() or "截图足以支持完成",
            )
        if first_line.upper().startswith("FAIL:"):
            return (
                "FAIL",
                first_line.split(":", 1)[1].strip() or "截图不足以支持完成",
            )

        reason = (
            f"断言系统返回非协议内容：{first_line[:80]}；回退采纳主 VLM 结果"
        )
        await self._log(2, "断言系统 · 协议异常", reason, step=step)
        return ("SKIP", reason)

    def _build_finished_assertion_prompt(
        self,
        *,
        thought: str,
        finish_msg: str,
        has_prev: bool,
    ) -> str:
        """构造 finished 断言系统提示词。

        双图对照模式（``has_prev=True``）：附图 1 = 主 VLM 最后一个动作之前的
        画面，附图 2 = 当前最终画面。模型应同时利用"两图差异"和主 VLM 的
        thought 文本，判断"thought 自述的状态变化是否真的发生"。

        单图模式（``has_prev=False``）：仅有附图 = 当前最终画面（首步即
        finished 的极端情况）。退化到只验最终状态。

        结构化通道 / 自由通道使用不同口径：

        - 结构化 case：以"预期结果"为唯一验收标准，逐条核对
        - 自由对话：以"最后一个动作结果"为唯一验收对象，不审过程
        """
        history = self._format_action_history(STRUCT_AUDIT_HISTORY_LIMIT)
        img_index_intro = (
            "本提示词附带两张图（按消息顺序）：\n"
            "- 附图 1：主 VLM **最后一个动作之前**看到的画面（动作前对照帧）\n"
            "- 附图 2：当前**最终落点**画面（断言要验收的对象）\n"
            "两张图之间只跨越主 VLM 最后一个动作。"
        ) if has_prev else (
            "本提示词附带一张图：\n"
            "- 附图：当前**最终落点**画面（断言要验收的对象）\n"
            "本次没有动作前对照帧（首步即 finished 的极端情况），请仅基于该图与主 VLM 自述判断。"
        )

        cmp_block_struct = (
            "对照判断（仅当存在双图时启用）：\n"
            "- 把附图 1 / 附图 2 当成「动作前/后」快照，**优先用两图差异验证**主 VLM "
            "thought 自述的状态变化是否真的发生（例如：返回上一级 → 页面整体不同；"
            "弹窗关闭 → 浮层消失；进度拖动 → 进度元件位置/填充明显改变）。\n"
            "- thought 声称发生的视觉变化，必须能在两图差异中找到对应证据；"
            "如果两图几乎相同而 thought 声称「已切换/已变化」，则该自述不可信。\n"
            "- 但最终是否 PASS 仍以「附图 2 是否满足预期结果」为准——双图对照只是辅助"
            "排除「伪成功」，不替代预期结果验收。\n\n"
        ) if has_prev else ""

        if self._is_structured:
            return (
                "你是手机自动化任务的最终断言系统，只负责裁决主 VLM 的 finished 是否可被采纳。"
                "你不能继续执行步骤，也不能把本次 finished 改写成新的动作建议。\n\n"
                f"{img_index_intro}\n\n"
                "当前是结构化测试用例。你的唯一职责：根据用户输入中的“预期结果”做最终验收。\n\n"
                f"{cmp_block_struct}"
                "裁决规则：\n"
                "1. “预期结果”是唯一验收标准，优先级最高；你必须逐条检查预期结果是否被附图 2 可靠支持。\n"
                "2. 你做的是语义验收，不是逐字匹配：\n"
                "   - 不要求与预期结果一字不差\n"
                "   - 允许同义表达、界面别名、常见产品话术变体\n"
                "   - 只要截图中的实际表达在业务语义上等价于预期结果，即可判定该条成立\n"
                "3. 你禁止脑补：\n"
                "   - 如果截图中没有足够证据，就不能自行补全\n"
                "   - 如果证据模糊、被遮挡、过小、无法可靠判断，则该条不成立\n"
                "4. 对数值、比例、区间、状态类要求：\n"
                "   - 若截图中能直接看到明确数值/文字，优先按明确证据判断\n"
                "   - 若没有明确文字，但可以从两图差异做稳定、可靠的视觉判断，也可判定成立\n"
                "   - 若只能猜测大概如此，则不能通过\n"
                "5. 只要有任一关键预期结果未被附图 2（必要时配合附图 1 对照）可靠支持，就必须 FAIL。\n"
                "6. 如果主 VLM 的 thought / finished 内容与截图明显矛盾（如自述「已切换」但两图几乎相同），"
                "也必须 FAIL。\n"
                "7. 不允许输出 UNSURE，也不允许建议继续执行；只能做最终裁决。\n\n"
                "输出协议：只输出第一行，且只能是以下两种之一：\n"
                "PASS: <一句话原因>\n"
                "FAIL: <一句话原因>\n\n"
                "请特别注意：\n"
                "- 你只验“预期结果”，不验前置条件，不验操作过程，不验历史顺序。\n"
                "- 严格验收 != 逐字匹配。\n"
                "- 语义等价可以通过，证据不足不能通过。\n"
                "- FAIL 时必须明确指出：是哪一条预期结果没有被截图可靠支持。\n\n"
                f"【用户目标】\n{self.goal}"
                f"\n\n【最近动作历史】\n{history}"
                f"\n\n【主VLM最后思考】\n{thought}"
                f"\n\n【主VLM finished 内容】\n{finish_msg}\n"
            )

        cmp_block_free = (
            "对照判断（仅当存在双图时启用）：\n"
            "- 把附图 1 / 附图 2 当成「动作前/后」快照，**优先用两图差异验证**主 VLM "
            "thought 自述的「最后一个动作结果」是否真的发生（例如返回 → 页面整体不同；"
            "关闭 → 浮层消失；拖动 → 元件位置改变）。\n"
            "- 这一对照对「过程导向任务」特别重要：例如「点击返回」，「上一级页面」是什么"
            "你不知道，但只要附图 1 和附图 2 是**可识别的不同页面**，"
            "且 thought 自述与差异方向一致，就足以判 PASS。\n"
            "- 反过来，若 thought 自述了明显的视觉变化但两图几乎相同，则该自述不可信。\n\n"
        ) if has_prev else ""

        return (
            "你是手机自动化任务的最终断言系统，只负责裁决主 VLM 的 finished 是否可被采纳。"
            "你不能继续执行步骤，也不能把本次 finished 改写成新的动作建议。\n\n"
            f"{img_index_intro}\n\n"
            "铁律：仅看“用户最后一个 action 执行步骤”与截图是否一致。\n"
            "也就是说：\n"
            "- 你要先从用户目标里抽取“最后一个动作”是什么\n"
            "- 然后判断这个最后动作完成后的结果，是否已在附图 2 中成立"
            "（必要时用附图 1 做「动作前对照」辅助判断）\n"
            "- 前面的动作、顺序、中间过渡、是否真的点过某个前置按钮，一律不审查\n\n"
            f"{cmp_block_free}"
            "裁决规则：\n"
            "1. 如果用户目标包含多个顺序动作（“点击A后点击B”“先做X再做Y”“完成步骤M后立即执行步骤N”），"
            "你只抽取最后一个动作，并只验证截图是否支持这个最后动作的结果已经成立。\n"
            "2. 你必须忽略“先/后/马上/立即/随后/双动作链/连续点击”等过程词带来的历史顺序要求；"
            "这些词不能成为 FAIL 依据。\n"
            "3. 严禁使用以下理由 FAIL：\n"
            "   - 无法证明之前是否点击过某按钮\n"
            "   - 无法确认点击顺序\n"
            "   - 无法证明是否使用了双动作链\n"
            "   - 截图未体现中间过程\n"
            "   - 最终页面标题/模块名没有逐字等于最后点击的按钮文案；"
            "只要截图显示已进入该按钮对应的结果页或功能列表，就应 PASS\n"
            "4. 只有当最后一个动作对应的最终结果，与截图明确矛盾时，才允许 FAIL。\n"
            "   过程导向动作（如「返回」「关闭」）：附图 1 与附图 2 是同一页面、且 thought 自述了切换 → FAIL；\n"
            "   状态导向动作（如「拖到 30%」「选中某项」）：附图 2 没有 thought 自述的视觉证据 → FAIL。\n"
            "5. 不允许输出 UNSURE，也不允许建议继续执行；只能做最终裁决。\n\n"
            "输出协议：只输出第一行，且只能是以下两种之一：\n"
            "PASS: <一句话原因>\n"
            "FAIL: <一句话原因>\n\n"
            "请特别注意：\n"
            "- 如果截图已经足以支持“最后一个动作”的结果成立，应直接 PASS。\n"
            "- “过程不可回溯”绝不等于“结果未达成”。\n"
            "- 你的 FAIL 必须指出“最后一个动作结果”层面的明确矛盾，不能只谈过程。\n\n"
            f"【用户目标】\n{self.goal}"
            f"\n\n【最近动作历史】\n{history}"
            f"\n\n【主VLM最后思考】\n{thought}"
            f"\n\n【主VLM finished 内容】\n{finish_msg}\n"
        )

    # ------------------------------------------------------------------
    # open_app / close_app：通过二次 VLM 做包名匹配
    # ------------------------------------------------------------------
    async def _open_app_by_name(self, app_name: str) -> None:
        # 走全量（含系统应用）列表，让 open_app('设置' / '相册' / '浏览器' /
        # '应用市场' 等系统应用) 也能被 VLM 二次包名匹配命中——过去用
        # list_third_party_packages 会把这些系统包过滤掉，导致 VLM 返回 NONE
        # 整条动作判失败
        pkgs = await asyncio.to_thread(self.driver.list_all_packages)
        target = await self._match_package_name(app_name, pkgs)
        await asyncio.to_thread(self.driver.activate_app, target)
        await self._log(1, "打开App", f"成功: {target}")

    async def _close_app_by_name(self, app_name: str) -> None:
        # 同 _open_app_by_name：close 也用全量，避免"关闭设置"这类命中系统包失败
        pkgs = await asyncio.to_thread(self.driver.list_all_packages)
        target = await self._match_package_name(app_name, pkgs)
        await asyncio.to_thread(self.driver.terminate_app, target)
        await self._log(1, "关闭App", f"成功: {target}")

    # ------------------------------------------------------------------
    # 起跑线强制注入：close_app + open_app（不走 VLM）
    # ------------------------------------------------------------------
    async def _run_app_lifecycle_prelude(self, app_name: str) -> None:
        """程序化执行"杀进程→重新打开"两步前置动作。

        在前端时间线 / HTML 报告里这两步看起来和 VLM 步骤完全一致（都有 step
        number / before-after 截图 / "执行完成"日志），但**没有**思考行——
        因为这两步是系统决策，不是模型决策，让人一眼能看出"这是起跑线自动跑的"。

        失败处理：close 或 open 任何一步抛错都只记 WARN 不中断 Run；理由是设备
        本来就可能没装这个 App / 包名匹配失败 / 当前 App 已是 closed——这些
        情况下让 VLM 接着自己处理比直接 fail 更可救。
        """
        plan = (
            ("close_app", "关闭App（系统起跑线）", self._close_app_by_name),
            ("open_app", "打开App（系统起跑线）", self._open_app_by_name),
        )
        for offset, (action_name, log_title, exec_fn) in enumerate(plan, start=1):
            if self._stop_event.is_set():
                return
            step = offset
            self._current_step = step
            await self._emit_event(make_event(EVT_STEP_START, self.run_id, step=step))
            await self._log(1, f"━━ 第 {step} 步 ━━", "段=起跑线", step=step)
            await self._log(1, log_title, f"应用: {app_name}", step=step)

            t0 = time.monotonic()
            try:
                await exec_fn(app_name)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                await self._emit_event(
                    make_event(
                        EVT_EXEC_RESULT,
                        self.run_id,
                        step=step,
                        action=action_name,
                        elapsed_ms=elapsed_ms,
                    )
                )
                await self._log(
                    1, "执行完成",
                    f"动作: {action_name}, 耗时: {elapsed_ms}ms", step=step,
                )
            except Exception as exc:  # noqa: BLE001
                await self._log(
                    2, f"起跑线 {action_name} 失败",
                    f"{exc}（继续后续步骤，由 VLM 兜底）",
                    step=step,
                )

            # close 与 open 之间留 1.5s 让 App 真正退出，避免 open 立刻收到旧 PID
            if action_name == "close_app":
                await asyncio.sleep(1.5)

            # 操作后截图 + 进时间线
            try:
                tail = await self._screenshot_jpeg()
            except Exception:
                tail = None
            if tail:
                await self._emit_screenshot(step, "after", tail)
                self._last_tail_bytes = tail
            await self._emit_event(make_event(EVT_STEP_END, self.run_id, step=step))

        # 给 VLM 注入一条系统提示，避免它看到 App 主页又想重做一遍 close+open
        self.vlm.add_hint(
            f"【系统提示】起跑线动作（关闭并重新打开「{app_name}」）已由系统在第 1-2 步"
            "自动执行完毕，请直接从下一个未完成步骤开始（通常是登录判断或进入主页 Tab），"
            "不要再尝试 close_app / open_app。"
        )

    async def _match_package_name(self, app_name: str, packages: List[str]) -> str:
        """① 起跑线 · 包名匹配：从设备包名列表里挑出与 ``app_name`` 最匹配的包名。

        协议层（构造 prompt、发请求、解析返回、token 累加）下沉到 assistant
        实现；本函数只承担两层薄编排：
        - 未匹配时（assistant 返回空串） → 翻译成 RuntimeError，让上游 open_app /
          close_app 走"动作判失败"路径
        - 命中时打一条业务日志（"「微信」→ com.tencent.mm"）

        切换 assistant_backend 时本函数零改动；各家把"NONE / null / 空"等家家
        各异的"未匹配"信号统一翻译为空串后返回。
        """
        target = await self._assistant.match_package(app_name, packages)
        if not target:
            raise RuntimeError(f"未找到与「{app_name}」匹配的应用")
        await self._log(1, "包名匹配", f"「{app_name}」→ {target}")
        return target

    # ------------------------------------------------------------------
    # 截图 / 坐标辅助
    # ------------------------------------------------------------------
    async def _screenshot_jpeg(self) -> bytes:
        # 截图参数按主 VLM 协议分家，**豆包路径保持 (25, None) 行为不变**：
        # - doubao_responses（默认）：模型对 JPEG 压缩伪影不敏感，低画质 +
        #   设备原始分辨率即可；保留历史参数避免老 case 行为漂移。
        # - claude_cu / gpt_cu：Anthropic / OpenAI Computer Use 官方推荐截
        #   图长边 ≤1344px、quality ≥70。低画质会让小文字 / 细线条图标糊
        #   到识别不出来，原图分辨率会让 token 翻倍但信噪比反而更差，实
        #   测此组合对 CU 系元素定位精度提升明显。
        backend = (self._settings.vlm_backend or "").lower()
        if backend in ("claude_cu", "gpt_cu"):
            quality, max_long_edge = 75, 1344
        else:
            quality, max_long_edge = 25, None
        return await asyncio.to_thread(
            self.driver.screenshot_jpeg, quality, max_long_edge
        )

    async def _vlm_point_to_abs(
        self,
        point: List[int],
        coord_space: str = "normalized",
    ) -> Tuple[int, int]:
        """模型坐标 → 设备像素，按 ``coord_space`` 走分家路径。

        - ``"normalized"``（默认，豆包系）：0-1000 归一化坐标，走原
          ``A.vlm_point_to_abs`` 路径；**调用时不传或传 normalized 等价于
          老行为，豆包不受影响**。
        - ``"absolute"``（Claude / GPT Computer Use）：模型给的是相对喂给
          它那张截图的绝对像素。需要按 ``(设备 / 截图)`` 比例缩放回设备
          像素——CU 系会通过 ``max_long_edge=1344`` 缩图，截图分辨率不等
          于设备分辨率，直接把像素当设备坐标会偏。截图尺寸取自最近一次
          ``vlm.decide`` 喂入帧（见 ``_last_vlm_screenshot_size``）。
        """
        w, h = await asyncio.to_thread(self.driver.window_size)
        if coord_space == "absolute" and self._last_vlm_screenshot_size is not None:
            sw, sh = self._last_vlm_screenshot_size
            if sw > 0 and sh > 0:
                abs_x = int(point[0] * (w / sw))
                abs_y = int(point[1] * (h / sh))
                abs_x = max(0, min(abs_x, w - 1))
                abs_y = max(0, min(abs_y, h - 1))
                return abs_x, abs_y
        abs_xy = A.vlm_point_to_abs(int(point[0]), int(point[1]), w, h)
        return int(abs_xy[0]), int(abs_xy[1])

    # ------------------------------------------------------------------
    # Emit 辅助
    # ------------------------------------------------------------------
    async def _emit_event(self, event: Dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            result = self._emit(event)
            await _maybe_await(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("emit 回调失败: {} | event_type={}", exc, event.get("type"))

    async def _emit_screenshot(self, step: int, phase: str, data: bytes) -> None:
        await self._emit_event(
            make_event(
                EVT_SCREENSHOT,
                self.run_id,
                step=step,
                phase=phase,
                size=len(data),
                bytes=data,  # 上层选择是否要转 base64 / 上传 / 落盘
            )
        )

    async def _log(
        self, level: int, title: str, content: str, *, step: Optional[int] = None
    ) -> None:
        evt = log_event(self.run_id, level, title, content, step=step or self._current_step or None)
        await self._emit_event(evt)
        logmsg = f"[{self.run_id}][step={evt.get('step')}] {title} | {content}"
        if level >= 3:
            logger.error(logmsg)
        elif level == 2:
            logger.warning(logmsg)
        else:
            logger.info(logmsg)


# ---------------------------------------------------------------------------
# JPEG 尺寸 decode（仅 CU 系坐标反算用，模块级以避免类膨胀）
# ---------------------------------------------------------------------------
def _decode_jpeg_size(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    """从 JPEG bytes 解码出 (width, height)。失败返回 None。

    实现优先级：PIL → JPEG SOF0 字节扫描。**只服务 CU 系坐标反算路径**，
    豆包路径不会调到本函数。
    """
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception as exc:  # noqa: BLE001
        logger.debug("PIL decode 截图尺寸失败({})，回退到字节扫描", exc)

    try:
        if image_bytes[:3] == b"\xff\xd8\xff":  # JPEG SOI
            i = 2
            n = len(image_bytes)
            while i < n - 8:
                if image_bytes[i] == 0xFF and image_bytes[i + 1] in (
                    0xC0, 0xC1, 0xC2, 0xC3,
                ):
                    h = (image_bytes[i + 5] << 8) | image_bytes[i + 6]
                    w = (image_bytes[i + 7] << 8) | image_bytes[i + 8]
                    return int(w), int(h)
                i += 1
    except Exception:  # noqa: BLE001
        pass

    logger.warning("无法从截图 bytes 识别 JPEG 尺寸，CU 坐标反算将退化为豆包归一化路径")
    return None
