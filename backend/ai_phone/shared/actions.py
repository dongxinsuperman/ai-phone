"""VLM 动作集定义、解析器与坐标转换。

迁移自 sonic_all_ai/5-VLM全权处理 copy.groovy 的 parseAction / extractPoint /
extractSecondsFromThought / vlmPointToAbs，原样保留正则与字段命名。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 动作名常量
# ---------------------------------------------------------------------------
# 官方规范 PHONE_USE_DOUBAO 对齐（click/long_press/type/scroll/drag/open_app/
# press_home/press_back/finished）+ 本执行层扩展（wait/close_app/assert_fail/
# double_tap）。模型偏离即进未知动作保护，由上层 runner 处理。
ACTION_CLICK = "click"
ACTION_DOUBLE_TAP = "double_tap"
ACTION_LONG_PRESS = "long_press"
ACTION_TYPE = "type"
ACTION_SCROLL = "scroll"
ACTION_DRAG = "drag"
ACTION_OPEN_APP = "open_app"
ACTION_CLOSE_APP = "close_app"
ACTION_PRESS_HOME = "press_home"
ACTION_PRESS_BACK = "press_back"
# 通用按键事件（数字 keycode），用于覆盖 Home/Back 之外的键——如 Enter / Tab /
# BackSpace / 方向键。承接 Claude / GPT computer tool 的 ``key("Return")``
# / ``keypress(["Tab"])`` 等输入；Android 直接走 ``adb shell input
# keyevent``，Harmony 走 hmdriver2.press_key（keycode 表与 Android 不同，需在
# 平台层做映射，未实现的键会按 NotImplementedError 走未知动作兜底）。
ACTION_KEY_EVENT = "key_event"
ACTION_WAIT = "wait"
ACTION_FINISHED = "finished"
ACTION_ASSERT_FAIL = "assert_fail"

KNOWN_ACTIONS = frozenset(
    {
        ACTION_CLICK,
        ACTION_DOUBLE_TAP,
        ACTION_LONG_PRESS,
        ACTION_TYPE,
        ACTION_SCROLL,
        ACTION_DRAG,
        ACTION_OPEN_APP,
        ACTION_CLOSE_APP,
        ACTION_PRESS_HOME,
        ACTION_PRESS_BACK,
        ACTION_KEY_EVENT,
        ACTION_WAIT,
        ACTION_FINISHED,
        ACTION_ASSERT_FAIL,
    }
)


# ---------------------------------------------------------------------------
# X11 / xdotool 风格键名 → Android KeyCode 映射
# ---------------------------------------------------------------------------
# Anthropic Computer Use 与 OpenAI computer-use-preview 的 key / keypress 动
# 作约定都用 X11 键名（"Return" / "BackSpace" / "Page_Down" 等）。以下表把
# 它们翻译为 Android ``KEYCODE_*``。
#
# **不**包含 "Home" / "Back" / "Escape"——这三个有专用 ParsedAction
# （ACTION_PRESS_HOME / ACTION_PRESS_BACK），claude_cu / gpt_cu 解析层会优先
# 走专用动作，避免 Home 键名歧义（Linux X11 "Home" = 光标到行首；移动端
# "Home" = 回桌面，约定走 press_home）。
#
# 鸿蒙 keycode 表与 Android 不同；本表当前**只对 Android 路径正确**，鸿蒙
# 走到这里会进 driver.press_keycode 的"穿透 hmdriver2.press_key"分支，键值
# 不一定生效——后续按需补 platform 维度的分叉表。iOS 只识别 HOME/BACK/
# APP_SWITCH（见 ios.py:press_keycode），其他键 raise NotImplementedError，
# 由 runner 走未知动作兜底。
X11_TO_ANDROID_KEYCODE: Dict[str, int] = {
    # 文本编辑（搜索框 / 表单确认最高频）
    "return": 66,        # KEYCODE_ENTER
    "enter": 66,
    "tab": 61,           # KEYCODE_TAB
    "backspace": 67,     # KEYCODE_DEL（Android 命名歧义：DEL = backspace）
    "delete": 112,       # KEYCODE_FORWARD_DEL
    "space": 62,         # KEYCODE_SPACE
    # 方向键
    "up": 19,            # KEYCODE_DPAD_UP
    "down": 20,          # KEYCODE_DPAD_DOWN
    "left": 21,          # KEYCODE_DPAD_LEFT
    "right": 22,         # KEYCODE_DPAD_RIGHT
    # 翻页
    "page_up": 92,       # KEYCODE_PAGE_UP
    "page_down": 93,     # KEYCODE_PAGE_DOWN
    # 多媒体 / 系统
    "menu": 82,          # KEYCODE_MENU
    "search": 84,        # KEYCODE_SEARCH
    "volume_up": 24,
    "volume_down": 25,
}


# ---------------------------------------------------------------------------
# 正则（与 Groovy 侧一致）
# ---------------------------------------------------------------------------
# 仅用于"找到函数名 + 起始左括号"。原来还要求字符串以 ``)`` 结尾的硬正则
# ``r"(\w+)\s*\((.*)\)\s*$"`` 在 VLM 输出尾随注释（如 ``wait(seconds=140)
# # 130s 是 30%``）时会整体匹配失败，导致 parse_action 走 finished 兜底，把任务
# 错判为成功——危险性极高。改成"只锚定函数名 + 左括号"，参数体由
# :func:`_split_call` 走括号深度+字符串字面量感知扫描出来，天然容忍后续注释。
_FN_HEAD_RE = re.compile(r"\s*(\w+)\s*\(")
_POINT_RE = re.compile(r"<point>(\d+)\s+(\d+)</point>")
_DIRECTION_RE = re.compile(r"direction='([^']*)'")
_CONTENT_RE = re.compile(r"content='(.*)'", re.DOTALL)
_APP_NAME_RE = re.compile(r"app_name\s*=\s*'([^']*)'")
_NAME_RE = re.compile(r"name\s*=\s*'([^']*)'")
_SECONDS_KV_RE = re.compile(r"seconds\s*=\s*['\"]?(\d+)['\"]?")
# scroll(amount=N)：豆包路径让 VLM 自己决定"一次滑多远"。Claude/GPT-CU 走
# 各自字段（scroll_amount / scroll_y）在 main/ 里换算，这里只负责豆包。
_AMOUNT_KV_RE = re.compile(r"amount\s*=\s*['\"]?(\d+)['\"]?")
_SECONDS_BARE_RE = re.compile(r"^\s*['\"]?(\d+)['\"]?\s*$")

_THOUGHT_RE = re.compile(r"Thought:\s*(.+?)(?=\nAction:|$)", re.DOTALL)
_ACTION_RE = re.compile(r"Action:\s*(.+)", re.DOTALL)
# 多行 Action 抽取（chain 用）：每行单独抓一条 `Action: <call>`，按出现顺序返回
_ACTION_LINE_RE = re.compile(r"^\s*Action:\s*(.+?)\s*$", re.MULTILINE)

_CN_NUM_MAP = {
    "零": 0,
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class ParsedAction:
    """Action 解析结果。

    参考 Groovy parseAction 返回的 Map 字段命名，保持跨语言一致：
    - action: 动作名（如 "click"）
    - point/start_point/end_point: [x, y]，坐标空间见 ``coord_space``
    - content: type / finished / assert_fail 的文本
    - direction: scroll 的方向
    - name: open_app / close_app 的应用名
    - seconds: wait 的秒数
    - coord_space: 坐标空间标记，多协议适配层用来区分模型的输出系。
        * ``"normalized"``（默认）：归一化 0-1000 坐标，豆包系标准输出
        * ``"absolute"``：相对模型 input image 的绝对像素坐标（Claude / OpenAI
          的 computer 工具就是这类）。Runner 在做 viewport 反向缩放时按本字段
          决定走"× 比例"还是"先 ÷ 1000 再乘屏幕宽高"。
    """

    action: str
    point: Optional[List[int]] = None
    start_point: Optional[List[int]] = None
    end_point: Optional[List[int]] = None
    content: Optional[str] = None
    direction: Optional[str] = None
    name: Optional[str] = None
    seconds: Optional[int] = None
    # key_event 专用：Android keycode（Claude/GPT 走 X11 键名 → 查
    # X11_TO_ANDROID_KEYCODE 表得到）。
    keycode: Optional[int] = None
    # scroll 专用：连续滚动次数（默认 1，与历史豆包路径行为一致）。Claude
    # ``scroll_amount`` 字段直接透传；OpenAI ``scroll_x/scroll_y`` 像素值
    # 在 main/gpt_cu.py 按"每 100px ≈ 1 次"换算。
    scroll_amount: int = 1
    raw: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    coord_space: str = "normalized"

    @property
    def is_known(self) -> bool:
        return self.action in KNOWN_ACTIONS

    @property
    def is_terminal(self) -> bool:
        return self.action in (ACTION_FINISHED, ACTION_ASSERT_FAIL)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"action": self.action}
        for k in (
            "point", "start_point", "end_point", "content", "direction",
            "name", "seconds", "keycode",
        ):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        # scroll_amount 仅在 >1 时显式输出，默认值不污染日志
        if self.scroll_amount and self.scroll_amount > 1:
            out["scroll_amount"] = self.scroll_amount
        # coord_space 仅在非默认值时显式输出，避免污染豆包系日志/快照
        if self.coord_space and self.coord_space != "normalized":
            out["coord_space"] = self.coord_space
        return out


# ---------------------------------------------------------------------------
# Thought / Action 字段提取
# ---------------------------------------------------------------------------
def extract_thought(content: str) -> str:
    """从 VLM 输出中抽取 Thought 文本。"""
    if not content:
        return ""
    m = _THOUGHT_RE.search(content)
    return m.group(1).strip() if m else ""


def extract_action(content: str) -> str:
    """从 VLM 输出中抽取 Action 文本。

    解析失败时返回 ``assert_fail`` 降级动作（带原文前 100 字），让 Run 以
    "格式异常"路径退出。**绝不**能再返回 ``finished`` ——历史上那样写会让
    "VLM 没输出 Action 行 / 输出乱七八糟"被静悄悄翻译成"任务完成"。
    """
    if not content:
        return "assert_fail(content='无法解析决策输出: (空)')"
    m = _ACTION_RE.search(content)
    if m:
        return m.group(1).strip()
    snippet = content[:100]
    return f"assert_fail(content='无法解析决策输出: {snippet}')"


def extract_actions(content: str) -> List[str]:
    """从 VLM 输出中抽取**所有** ``Action:`` 行，按出现顺序返回。

    用于支持 VLM 在同一 Thought 下输出 ≥ 2 个 Action 形成"链式动作"
    （瞬态 UI 操作专用）。例如：

    ::

        Thought: ...
        Action: click(point='<point>485 486</point>')
        Action: click(point='<point>66 75</point>')

    会返回 ``["click(point='<point>485 486</point>')",
    "click(point='<point>66 75</point>')"]``。

    解析失败 / 空输入时退化为单元素列表（与 :func:`extract_action` 同样的
    ``assert_fail`` 兜底字符串），保证调用方拿到的列表非空且不会被错判为
    "任务完成"。
    """
    if not content:
        return ["assert_fail(content='无法解析决策输出: (空)')"]
    matches = [m.strip() for m in _ACTION_LINE_RE.findall(content) if m.strip()]
    if matches:
        return matches
    snippet = content[:100]
    return [f"assert_fail(content='无法解析决策输出: {snippet}')"]


# ---------------------------------------------------------------------------
# Action 字符串 → ParsedAction
# ---------------------------------------------------------------------------
def _split_call(action_str: str) -> Optional[Tuple[str, str]]:
    """从 ``action_str`` 中拆出 ``(fn_name, params_str)``，容忍尾部注释/装饰文本。

    比起原来"必须以 `)` 结尾"的硬正则鲁棒得多，能正确处理：

    - ``wait(seconds=140)  # 7分33秒的30%约为130秒`` → ("wait", "seconds=140")
    - ``click(point='<point>500 500</point>')   // 唤起工具栏``
      → ("click", "point='<point>500 500</point>'")
    - ``type(content='hello (world)')`` → ("type", "content='hello (world)'")
      （字符串字面量内的括号不计入深度）

    扫描器逻辑：找到第一个左括号位置后，按字符走，状态机识别 ``'...'`` /
    ``"..."`` 字面量（含 ``\\'`` 转义），只在字面量外计数 ``(`` ``)`` 深度，
    深度归零的右括号即匹配的闭合点。匹配失败返回 ``None``。
    """
    if not action_str:
        return None
    head = _FN_HEAD_RE.match(action_str)
    if head is None:
        return None
    fn_name = head.group(1)
    # 起始位置：左括号之后第一个字符
    start = head.end()
    depth = 1
    i = start
    in_squote = False
    in_dquote = False
    n = len(action_str)
    while i < n:
        ch = action_str[i]
        if in_squote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                in_squote = False
        elif in_dquote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_dquote = False
        else:
            if ch == "'":
                in_squote = True
            elif ch == '"':
                in_dquote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    params_str = action_str[start:i]
                    return fn_name, params_str.strip()
        i += 1
    return None


def parse_action(action_str: str) -> ParsedAction:
    """解析 VLM 输出的单行 Action 调用，如 `click(point='<point>500 500</point>')`。

    与 Groovy parseAction 行为对齐，但兜底语义升级：

    - 解析失败 → 返回 ``assert_fail(content='无法解析 Action: ...')``。
      历史上是 ``finished``，会让"VLM 在动作行尾加注释"这种格式瑕疵被错判为
      "任务成功结束"——典型现场：``wait(seconds=140)  # 注释`` 被吞成 finished，
      Run 直接结束且 ok=true。改成 assert_fail 后，主循环在收到无法解析的
      Action 时会以 fail 路径退出，红字写明"VLM 输出格式异常 / 含注释"。
    - 只认官方 + 扩展动作名；偏离的动作名原样返回，上层判定为未知动作保护。
    """
    if action_str is None:
        action_str = ""
    action_str = action_str.strip()

    split = _split_call(action_str)
    if split is None:
        snippet = action_str[:200]
        return ParsedAction(
            action=ACTION_ASSERT_FAIL,
            content=(
                f"无法解析 Action: {snippet} "
                "（动作行不能加 # 或 // 注释、不能在调用尾部加任何装饰文本，"
                "请只输出 `动作名(参数)` 一行）"
            ),
            raw=action_str,
        )

    fn_name, params_str = split
    parsed = ParsedAction(action=fn_name, raw=action_str)

    if not params_str:
        return parsed

    if fn_name in (ACTION_CLICK, ACTION_DOUBLE_TAP, ACTION_LONG_PRESS):
        pt = _extract_point(params_str)
        if pt is not None:
            parsed.point = list(pt)
        return parsed

    if fn_name == ACTION_SCROLL:
        pt = _extract_point(params_str)
        if pt is not None:
            parsed.point = list(pt)
        dm = _DIRECTION_RE.search(params_str)
        if dm:
            parsed.direction = dm.group(1)
        # amount 可选：豆包 prompt 默认 1（温和翻一页），允许 1-10。这里只
        # 钳到 ≥1，上限钳留给 vlm_loop（max 10），与 Claude/GPT-CU 保持一致。
        am = _AMOUNT_KV_RE.search(params_str)
        if am:
            try:
                parsed.scroll_amount = max(1, int(am.group(1)))
            except (TypeError, ValueError):
                parsed.scroll_amount = 1
        return parsed

    if fn_name == ACTION_DRAG:
        pts: List[Tuple[int, int]] = [
            (int(x), int(y)) for x, y in _POINT_RE.findall(params_str)
        ]
        if len(pts) >= 2:
            parsed.start_point = list(pts[0])
            parsed.end_point = list(pts[1])
        return parsed

    if fn_name == ACTION_TYPE:
        cm = _CONTENT_RE.search(params_str)
        if cm:
            parsed.content = cm.group(1).replace("\\'", "'").replace("\\n", "\n")
        return parsed

    if fn_name == ACTION_OPEN_APP:
        am = _APP_NAME_RE.search(params_str)
        if am:
            parsed.name = am.group(1)
        else:
            nm = _NAME_RE.search(params_str)
            if nm:
                parsed.name = nm.group(1)
        return parsed

    if fn_name == ACTION_CLOSE_APP:
        nm = _NAME_RE.search(params_str)
        if nm:
            parsed.name = nm.group(1)
        return parsed

    if fn_name in (ACTION_PRESS_HOME, ACTION_PRESS_BACK):
        return parsed

    if fn_name == ACTION_WAIT:
        sm = _SECONDS_KV_RE.search(params_str)
        if sm:
            parsed.seconds = int(sm.group(1))
        else:
            bm = _SECONDS_BARE_RE.search(params_str)
            if bm:
                parsed.seconds = int(bm.group(1))
        return parsed

    if fn_name in (ACTION_FINISHED, ACTION_ASSERT_FAIL):
        cm = _CONTENT_RE.search(params_str)
        if cm:
            parsed.content = cm.group(1).replace("\\'", "'").replace("\\n", "\n")
        return parsed

    # 未知动作：action 原样保留，上层据 KNOWN_ACTIONS 判断走保护逻辑。
    return parsed


def _extract_point(s: str) -> Optional[Tuple[int, int]]:
    m = _POINT_RE.search(s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


# ---------------------------------------------------------------------------
# 坐标归一化 0-1000 → 实际屏幕像素
# ---------------------------------------------------------------------------
def vlm_point_to_abs(vlm_x: int, vlm_y: int, screen_w: int, screen_h: int) -> Tuple[int, int]:
    """0-1000 归一化坐标 → 实际屏幕像素；并 clamp 到 [0, w-1] / [0, h-1]。"""
    abs_x = int((vlm_x / 1000.0) * screen_w)
    abs_y = int((vlm_y / 1000.0) * screen_h)
    abs_x = max(0, min(abs_x, screen_w - 1))
    abs_y = max(0, min(abs_y, screen_h - 1))
    return abs_x, abs_y


# ---------------------------------------------------------------------------
# Thought 中兜底捞"X 秒"（兼容阿拉伯数字与常见中文数字 0-99）
# ---------------------------------------------------------------------------
_THOUGHT_WAIT_PRIMARY = re.compile(r"等(?:待)?[ \t]*([0-9零一二两三四五六七八九十]+)[ \t]*秒")
_THOUGHT_WAIT_FALLBACK = re.compile(r"([0-9零一二两三四五六七八九十]+)[ \t]*秒")


def extract_seconds_from_thought(thought: str) -> Optional[int]:
    """从 Thought 中捞回 X 秒，未命中返回 None（上层按默认 3 秒处理）。

    仅认 1-60 秒范围内的整数；超出视为无效，上层走默认值。
    """
    if not thought:
        return None
    for regex in (_THOUGHT_WAIT_PRIMARY, _THOUGHT_WAIT_FALLBACK):
        m = regex.search(thought)
        if not m:
            continue
        n = _parse_cn_num(m.group(1))
        if n is not None and 0 < n <= 60:
            return n
    return None


def _parse_cn_num(s: str) -> Optional[int]:
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    if "十" in s:
        parts = s.split("十")
        tens = 1
        ones = 0
        if parts[0] and parts[0] in _CN_NUM_MAP:
            tens = _CN_NUM_MAP[parts[0]]
        if len(parts) > 1 and parts[1] and parts[1] in _CN_NUM_MAP:
            ones = _CN_NUM_MAP[parts[1]]
        return tens * 10 + ones
    return None
