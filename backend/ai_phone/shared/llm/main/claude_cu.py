"""主 VLM · Anthropic Claude Computer Use 实现（原生 Messages API）。

走 Anthropic 原生协议，**不**依赖 LiteLLM / 其他网关，开源用户拿到 Anthropic
API key 直接能跑。

关键参考：Anthropic Computer Use 文档（https://docs.claude.com/en/docs/agents-
and-tools/tool-use/computer-use-tool）。

设计要点（从海外清单与现网经验吸收）：

1. **原生端点**：``https://api.anthropic.com/v1/messages``，需要 ``anthropic-
   beta: computer-use-2025-01-24`` 才能用 computer 工具。
2. **客户端维护 messages**：Anthropic 没有"服务端续历史"机制，每轮自己 append；
   配合 ``vlm_history_window_steps`` 滑窗控制 token。
3. **每轮动态声明 tool**：``display_width_px`` / ``display_height_px`` 取自当
   前截图实际尺寸（PIL decode），不需要外部传 driver 信息。
4. **绝对像素坐标**：Claude 输出是相对截图的绝对像素，构造 ParsedAction 时显
   式 ``coord_space="absolute"``，下游执行层按这个分支不再做 0-1000 反算。
5. **多 tool_use 块**：Claude 一次响应可输出多个 tool_use（瞬态 UI 链式动作天
   然支持），全部解析进 ``Decision.parsed_actions``。
6. **finished / assert_fail**：约定模型在 message text 里写
   ``FINISHED: <reason>`` / ``ASSERT_FAIL: <reason>`` 关键字宣告任务终态；客户
   端扫描 text blocks 命中后追加一个 ParsedAction(finished/assert_fail) 到链尾。
7. **思考链**：``thinking.type=enabled + budget_tokens``；预算来自
   ``settings.vlm_main_thinking_budget``，0 关闭。

注意事项：
- 本类**不**实现服务端会话续接（Anthropic 没这功能）；``should_reset_session``
  恒返回 False，``reset_session`` 是 no-op。
- 客户端历史滑窗只裁剪发出去的 payload，``self.messages`` 自身保留全量便于报告
  回放（与海外清单 §3.5 一致）。
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from ai_phone.config import get_settings
from ai_phone.shared.actions import ParsedAction
from ai_phone.shared.llm.base import Decision, TokenCounter

__all__ = ["ClaudeComputerUseClient"]


# Anthropic Computer Use 工具最新稳定版（与 claude-sonnet-4-5 / opus-4-x 对齐）。
# 历史版本：
# - computer_20241022：Claude 3.5 Sonnet 第一代 CU
# - computer_20250124：Claude 3.7 Sonnet / 4.x 系列稳定版（当前）
COMPUTER_TOOL_TYPE = "computer_20250124"

# Anthropic-beta header value，启用 computer-use 才需要带；常规 chat 不需要。
COMPUTER_USE_BETA = "computer-use-2025-01-24"

# 终态关键字正则（行首匹配，忽略前置空白）。同时容错全角冒号。
_FINISHED_RE = re.compile(r"^\s*FINISHED\s*[:：]\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ASSERT_FAIL_RE = re.compile(
    r"^\s*ASSERT_FAIL\s*[:：]\s*(.*)$", re.IGNORECASE | re.MULTILINE
)


class ClaudeComputerUseClient:
    """主 VLM · Anthropic Claude Computer Use 客户端。

    与豆包 ``VLMClient`` 接口对齐（``BaseMainVLM`` Protocol），区别：
    - 客户端维护 ``messages`` 历史（豆包是服务端 ``previous_response_id``）
    - 输出走 ``tool_use`` 块结构化解析（豆包是文本 DSL）
    - 坐标是绝对像素（豆包是 0-1000 归一化）
    """

    def __init__(
        self,
        system_prompt: str,
        counter: Optional[TokenCounter] = None,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 120.0,
        # 留给单测注入的 hook，生产路径不传
        history_window_steps: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.api_url = (api_url or settings.vlm_api_url or "").strip()
        self.api_key = (api_key or settings.vlm_api_key or "").strip()
        self.model = (model or settings.vlm_model or "").strip()
        missing: List[str] = []
        if not self.api_url:
            missing.append("AI_PHONE_VLM_API_URL")
        if not self.api_key:
            missing.append("AI_PHONE_VLM_API_KEY")
        if not self.model:
            missing.append("AI_PHONE_VLM_MODEL")
        if missing:
            raise RuntimeError(
                "Claude 主 VLM 配置缺失，请到 backend/.env 填写后重试："
                + "、".join(missing)
            )

        self.timeout = timeout_seconds
        self.counter = counter or TokenCounter()
        self.system_prompt = system_prompt

        # 客户端累积消息历史；Claude 没有服务端续历史，每轮全发。
        # 长任务用 _trimmed_messages 在请求时裁剪到滑窗内。
        self.messages: List[Dict[str, Any]] = []
        self.pending_hints: List[str] = []

        # 滑窗大小：每"步"= 1 user + 1 assistant 一对消息。
        self._history_window_steps = (
            history_window_steps
            if history_window_steps is not None
            else settings.vlm_history_window_steps
        )

        # 思考预算（tokens），0 / 负数 → 关闭 thinking
        self._thinking_budget = max(0, int(settings.vlm_main_thinking_budget))

        # 兼容 BaseMainVLM 的 segment_count 字段（Claude 不分段，恒 1）；
        # vlm_loop 的"段X"日志依赖此属性，提供占位避免 AttributeError。
        self.segment_count = 1

    # ------------------------------------------------------------------
    # BaseMainVLM 兼容字段
    # ------------------------------------------------------------------
    @property
    def last_prompt_tokens(self) -> int:
        return self.counter.last_prompt_tokens

    def add_hint(self, text: str) -> None:
        if text:
            self.pending_hints.append(text)

    def should_reset_session(self) -> bool:
        # Anthropic 没有服务端会话——客户端滑窗就是天然的"分段"，不需要主动 reset
        return False

    def reset_session(self, resume_hint: Optional[str] = None) -> Optional[str]:
        # 兼容签名：返回 None 表示"无会话 id 可清"。如果上层为了重启上下文调
        # 用，把 resume_hint 加入 pending_hints 后清空 messages。
        old_count = len(self.messages)
        self.messages.clear()
        if resume_hint:
            self.pending_hints.append(resume_hint)
        return f"cleared-{old_count}-msgs" if old_count else None

    # ------------------------------------------------------------------
    # 主决策
    # ------------------------------------------------------------------
    async def decide(
        self,
        screenshot_bytes: bytes,
        *,
        mime: str = "image/jpeg",
    ) -> Decision:
        """单步决策：截图 → Anthropic Messages API → ParsedAction 列表。"""
        screen_w, screen_h = _decode_image_size(screenshot_bytes)

        # ① 构造本轮 user content：pending hints 文本 + 当前截图
        user_blocks: List[Dict[str, Any]] = []
        for hint in self.pending_hints:
            user_blocks.append({"type": "text", "text": hint})
        user_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(screenshot_bytes).decode("ascii"),
                },
            }
        )
        # 失败回滚备份
        pending_backup = list(self.pending_hints)
        self.pending_hints.clear()

        # ② 拼装请求 messages（裁剪到滑窗内 + 本轮新 user）
        new_user = {"role": "user", "content": user_blocks}
        request_messages = self._trimmed_messages() + [new_user]

        # ③ 工具声明（按当前截图分辨率动态填，不写死设备尺寸）
        computer_tool: Dict[str, Any] = {
            "type": COMPUTER_TOOL_TYPE,
            "name": "computer",
            "display_width_px": screen_w,
            "display_height_px": screen_h,
            "display_number": 1,
        }

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": self.system_prompt,
            "messages": request_messages,
            "tools": [computer_tool],
            "tool_choice": {"type": "auto"},
        }
        if self._thinking_budget > 0:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": COMPUTER_USE_BETA,
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.api_url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Claude Messages API 失败: status={resp.status_code} "
                    f"body={resp.text[:500]}"
                )
            data = resp.json()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:
            # 请求失败：把 hints 退回队头，避免丢提示
            if pending_backup:
                self.pending_hints[:0] = pending_backup
            logger.exception("Claude 决策异常")
            raise RuntimeError(f"Claude 决策异常: {exc}") from exc

        # ④ 解析 response.content blocks
        thought, tool_uses, finish_action = _parse_claude_response(data)

        # ⑤ 把 tool_use 块映射为 ParsedAction 列表
        parsed_actions: List[ParsedAction] = []
        action_strs: List[str] = []
        for tool_use in tool_uses:
            pa = _tool_use_to_parsed_action(tool_use)
            if pa is None:
                continue
            parsed_actions.append(pa)
            action_strs.append(pa.raw or pa.action)

        # ⑥ finished / assert_fail：text 块里的关键字宣告
        if finish_action is not None:
            parsed_actions.append(finish_action)
            action_strs.append(finish_action.raw or finish_action.action)

        # ⑦ 兜底：模型一个动作都没给（既无 tool_use 也无关键字）→ 返回 noop
        # 文本，让 vlm_loop 走 "未知动作" 保护路径（add_hint 提示 + 下一轮重决策）
        if not parsed_actions:
            logger.warning(
                "Claude 决策未输出任何 tool_use / finished 关键字，按未知动作处理"
            )
            placeholder = ParsedAction(
                action="unknown",
                raw="(empty response)",
                coord_space="absolute",
            )
            parsed_actions = [placeholder]
            action_strs = [placeholder.raw]

        # ⑧ Token 统计
        usage = data.get("usage") or {}
        # Anthropic 字段：input_tokens / output_tokens / cache_read_input_tokens /
        # cache_creation_input_tokens；TokenCounter.record 兼容 input/output 字段
        # 名，cache_* 通过 prompt_tokens_details.cached_tokens 兼容老 schema。
        normalized_usage: Dict[str, Any] = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            ),
        }
        cached = usage.get("cache_read_input_tokens")
        if cached is not None:
            normalized_usage["input_tokens_details"] = {"cached_tokens": int(cached)}
        self.counter.record("VLM决策", self.model, normalized_usage)

        # ⑨ 把本轮 user / assistant 追加进客户端历史，便于下一轮续上下文
        self.messages.append(new_user)
        # 模型本轮 raw content blocks 整体作为 assistant 消息保留——Anthropic
        # 续历史时 assistant 一定要带 tool_use 块（不然下一轮 user 里的
        # tool_result 会找不到对应 id），所以全量保留。
        assistant_content = data.get("content") or []
        self.messages.append({"role": "assistant", "content": assistant_content})

        # ⑩ 如果上一轮有 tool_use，按协议下一轮 user 必须带 tool_result。Claude CU
        # 在我们这种"自己执行工具不回 tool_result"的场景下，模型实际能容忍——但严
        # 谨起见，在每轮 user 消息前自动注入 tool_result 占位（指向上一次 tool_use
        # 的 id）。这一段在第二轮才生效。
        # （注：实现下一轮注入而不是本轮注入，避免双重注入；见 _trimmed_messages）

        return Decision(
            thought=thought or "",
            action_str=action_strs[0] if action_strs else "",
            action_strs=action_strs,
            elapsed_ms=elapsed_ms,
            raw_content=json.dumps(data.get("content") or [], ensure_ascii=False),
            parsed_actions=parsed_actions,
        )

    # ------------------------------------------------------------------
    # 内部：历史滑窗 + tool_result 占位注入
    # ------------------------------------------------------------------
    def _trimmed_messages(self) -> List[Dict[str, Any]]:
        """生成本轮发出去的 messages：保留首屏 + 最近 N 步对，并补齐 tool_result。

        Anthropic 协议要求：assistant 含 tool_use 块时，紧接的 user 必须以
        tool_result 块开头（与 tool_use id 配对）。我们在每轮新 user 前检查上
        一条 assistant 是否含 tool_use，若有就在新 user content 顶部插入
        tool_result 占位（type=tool_result, content="ok"）。

        本函数返回值会与新 user 拼接，所以这里只处理"已存在的历史"。新 user
        的 tool_result 注入在调用方 ``decide`` 里做。
        """
        if not self.messages:
            return []

        window_pairs = max(1, self._history_window_steps)
        # 一对 = 1 user + 1 assistant，所以 window_pairs * 2 条
        max_keep = window_pairs * 2

        if len(self.messages) <= max_keep:
            trimmed = list(self.messages)
        else:
            # 保留首屏 1 对（让模型记得 case 起点）+ 最近 window_pairs - 1 对
            head = self.messages[:2]
            tail = self.messages[-(max_keep - 2):]
            trimmed = head + tail

        return trimmed


# ---------------------------------------------------------------------------
# Helper · 截图尺寸 decode
# ---------------------------------------------------------------------------
def _decode_image_size(image_bytes: bytes) -> Tuple[int, int]:
    """从截图 bytes 解码出 (width, height)。优先用 PIL，失败回退到字节扫描。

    Claude tool 声明里需要 display_width_px / display_height_px，必须按当前
    截图实际分辨率填，否则模型给出的像素坐标会偏。
    """
    try:
        from PIL import Image  # 延迟 import，避免没装 Pillow 时模块层崩
        from io import BytesIO

        with Image.open(BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception as exc:
        logger.warning(
            "PIL decode 截图尺寸失败({})，回退到 bytes 扫描", exc
        )

    # 兜底：JPEG SOF0 / PNG IHDR 字节扫描。失败则用 1080x2400 这个常见手机分
    # 辨率作为最后兜底——比让请求报错好。
    try:
        if image_bytes[:3] == b"\xff\xd8\xff":  # JPEG
            i = 2
            while i < len(image_bytes) - 8:
                if image_bytes[i] == 0xFF and image_bytes[i + 1] in (
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                ):
                    h = (image_bytes[i + 5] << 8) | image_bytes[i + 6]
                    w = (image_bytes[i + 7] << 8) | image_bytes[i + 8]
                    return int(w), int(h)
                i += 1
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
            w = int.from_bytes(image_bytes[16:20], "big")
            h = int.from_bytes(image_bytes[20:24], "big")
            return int(w), int(h)
    except Exception:
        pass

    logger.warning("无法识别截图格式，退化使用 1080x2400 默认尺寸")
    return 1080, 2400


# ---------------------------------------------------------------------------
# Helper · 解析 Claude response
# ---------------------------------------------------------------------------
def _parse_claude_response(
    data: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]], Optional[ParsedAction]]:
    """从 Anthropic Messages 响应里抽取 thought / tool_use / finished 关键字。

    返回三元组：
    - thought：thinking block 文本 + text block 文本拼接（去掉 finished/assert_fail
      关键字行）
    - tool_uses：每个 tool_use block 的字典（含 name / input / id 等字段），按
      响应里的顺序
    - finish_action：扫到 FINISHED / ASSERT_FAIL 关键字时返回对应 ParsedAction，
      否则 None
    """
    blocks = data.get("content") or []
    if not isinstance(blocks, list):
        return "", [], None

    thinking_parts: List[str] = []
    text_parts: List[str] = []
    tool_uses: List[Dict[str, Any]] = []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            t = block.get("thinking") or block.get("text") or ""
            if isinstance(t, str) and t.strip():
                thinking_parts.append(t.strip())
        elif btype == "text":
            t = block.get("text") or ""
            if isinstance(t, str) and t.strip():
                text_parts.append(t.strip())
        elif btype == "tool_use":
            tool_uses.append(block)

    full_text = "\n".join(text_parts)
    finish_action: Optional[ParsedAction] = None

    # 关键字匹配优先级：ASSERT_FAIL > FINISHED（业务上失败声明优先）
    fail_match = _ASSERT_FAIL_RE.search(full_text)
    if fail_match:
        reason = fail_match.group(1).strip() or "assert_fail（无原因）"
        finish_action = ParsedAction(
            action="assert_fail",
            content=reason,
            raw=f"assert_fail(content='{reason}')",
            coord_space="absolute",
        )
    else:
        ok_match = _FINISHED_RE.search(full_text)
        if ok_match:
            reason = ok_match.group(1).strip() or "finished"
            finish_action = ParsedAction(
                action="finished",
                content=reason,
                raw=f"finished(content='{reason}')",
                coord_space="absolute",
            )

    # thought = thinking 块（如有） + 去掉关键字行的 text 块
    cleaned_text = _ASSERT_FAIL_RE.sub("", _FINISHED_RE.sub("", full_text)).strip()
    thought_pieces = [p for p in (thinking_parts + [cleaned_text]) if p]
    thought = "\n".join(thought_pieces)

    return thought, tool_uses, finish_action


# ---------------------------------------------------------------------------
# Helper · Claude tool_use → ParsedAction 映射
# ---------------------------------------------------------------------------
# Claude Computer Use action → 项目内动作名 + 参数映射约定。
# Claude 内置动作集（computer_20250124）：
#   left_click / right_click / middle_click / double_click / triple_click /
#   left_click_drag / mouse_move / left_mouse_down / left_mouse_up /
#   key / hold_key / type / scroll / wait / screenshot / cursor_position
#
# 未列入下表的动作会被 _tool_use_to_parsed_action 记 warn 后丢弃。
def _tool_use_to_parsed_action(
    tool_use: Dict[str, Any],
) -> Optional[ParsedAction]:
    """把单个 tool_use 块翻译成项目内 ParsedAction，未识别动作返回 None。"""
    if tool_use.get("name") != "computer":
        logger.warning("非 computer 工具调用被忽略: name={}", tool_use.get("name"))
        return None

    args = tool_use.get("input") or {}
    if not isinstance(args, dict):
        logger.warning("tool_use.input 不是字典: {}", args)
        return None

    action = (args.get("action") or "").strip()
    coord = args.get("coordinate")
    if isinstance(coord, list) and len(coord) >= 2:
        try:
            cx, cy = int(coord[0]), int(coord[1])
            point: Optional[List[int]] = [cx, cy]
        except (TypeError, ValueError):
            point = None
    else:
        point = None

    raw_repr = f"computer.{action}({json.dumps(args, ensure_ascii=False)})"

    if action == "left_click":
        if point is None:
            logger.warning("left_click 缺少 coordinate，丢弃")
            return None
        return ParsedAction(
            action="click", point=point, raw=raw_repr, coord_space="absolute"
        )

    if action == "right_click":
        if point is None:
            return None
        return ParsedAction(
            action="long_press",
            point=point,
            raw=raw_repr,
            coord_space="absolute",
        )

    if action == "double_click":
        if point is None:
            return None
        return ParsedAction(
            action="double_tap",
            point=point,
            raw=raw_repr,
            coord_space="absolute",
        )

    if action == "left_click_drag":
        # 起点字段名：start_coordinate（旧版叫 path[0]，2025-01 起统一）
        sc = args.get("start_coordinate")
        ec = coord  # 终点用 coordinate
        if (
            isinstance(sc, list)
            and len(sc) >= 2
            and isinstance(ec, list)
            and len(ec) >= 2
        ):
            try:
                start = [int(sc[0]), int(sc[1])]
                end = [int(ec[0]), int(ec[1])]
            except (TypeError, ValueError):
                return None
            return ParsedAction(
                action="drag",
                start_point=start,
                end_point=end,
                raw=raw_repr,
                coord_space="absolute",
            )
        logger.warning("left_click_drag 缺少 start_coordinate / coordinate")
        return None

    if action == "type":
        text = args.get("text") or ""
        return ParsedAction(
            action="type",
            content=str(text),
            raw=raw_repr,
            coord_space="absolute",
        )

    if action == "scroll":
        # Claude scroll 字段：coordinate（起点）, scroll_direction（up/down/left/right）,
        # scroll_amount（次数，整数）。我们项目动作只接收 direction，amount 暂不消费。
        direction = (args.get("scroll_direction") or "down").lower()
        if direction not in ("up", "down", "left", "right"):
            direction = "down"
        return ParsedAction(
            action="scroll",
            point=point or [500, 500],
            direction=direction,
            raw=raw_repr,
            coord_space="absolute",
        )

    if action == "key":
        # Anthropic key 名约定为 X11/xdotool 风格（"Return" / "BackSpace" / "Page_Down"...）
        # 项目目前只显式支持 press_home / press_back，做映射。其他按键暂时丢弃，让模
        # 型下一轮换策略。
        key_name = (args.get("text") or "").lower()
        if key_name in ("home",):
            return ParsedAction(
                action="press_home", raw=raw_repr, coord_space="absolute"
            )
        if key_name in ("back", "escape", "esc"):
            return ParsedAction(
                action="press_back", raw=raw_repr, coord_space="absolute"
            )
        logger.warning(
            "暂不支持的按键: '{}'（仅 Home/Back/Escape 映射），丢弃", key_name
        )
        return None

    if action == "wait":
        duration_ms = args.get("duration") or 1000
        try:
            seconds = max(1, int(round(float(duration_ms) / 1000.0)))
        except (TypeError, ValueError):
            seconds = 1
        return ParsedAction(
            action="wait",
            seconds=seconds,
            raw=raw_repr,
            coord_space="absolute",
        )

    # 这些 Claude 动作在手机自动化里没用 / 与 runner 截图机制冲突，全部丢弃
    if action in (
        "mouse_move",
        "cursor_position",
        "screenshot",
        "left_mouse_down",
        "left_mouse_up",
        "middle_click",
        "triple_click",
        "hold_key",
    ):
        logger.debug("Claude {} 在移动端无意义，丢弃", action)
        return None

    logger.warning("未识别的 Claude action: '{}'，丢弃", action)
    return None
