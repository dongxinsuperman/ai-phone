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
from ai_phone.shared.actions import ParsedAction, X11_TO_ANDROID_KEYCODE
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

# Platform Action 文本协议（与 FINISHED / ASSERT_FAIL 同源），用于让 Claude
# CU 在执行中调用平台原生能力（包名级 open_app / close_app 等）——这些是
# computer tool 不具备的"项目级抽象"，又是 VLM 走"home + 找图标"路径最不
# 可靠的部分。格式：
#   PLATFORM_ACTION: open_app(app_name='洋葱学园')
#   PLATFORM_ACTION: close_app(app_name='淘宝')
# 行首匹配 + 兼容全角冒号 + 单/双引号。匹配组：(action_name, app_name)。
_PLATFORM_ACTION_RE = re.compile(
    r"^\s*PLATFORM_ACTION\s*[:：]\s*(\w+)\s*\(\s*app_name\s*=\s*"
    r"['\"]([^'\"]+)['\"]\s*\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# 当前白名单只放 open_app / close_app。新增动作时同步扩白名单 + 在 prompt
# 文档里添加示例 + runner do_action 加 dispatch 分支（已有的复用即可）。
_PLATFORM_ACTION_WHITELIST = frozenset({"open_app", "close_app"})


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

        # ① 构造本轮 user content：tool_result 块（如有）+ pending hints + 当前截图
        # Anthropic 协议硬约束：上一条 assistant 含 tool_use 块时，紧接的
        # user 必须以 tool_result 块开头，且每个 tool_use_id 都要有配对的
        # tool_result——少一个 API 直接 400 报
        # ``tool_use ids were found without tool_result blocks``。
        # 我们把"动作执行后的当前截图"放进**第一个 tool_result**（这是模型
        # 看到的执行后状态），其余 tool_use（瞬态 UI 链式动作场景）用文本
        # 占位 ack——模型只需要知道"那一串动作都执行了"，不需要中间帧。
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(screenshot_bytes).decode("ascii"),
            },
        }

        prev_tool_use_ids = self._extract_prev_tool_use_ids()

        user_blocks: List[Dict[str, Any]] = []
        for idx, tu_id in enumerate(prev_tool_use_ids):
            if idx == 0:
                user_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "content": [image_block],
                    }
                )
            else:
                user_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu_id,
                        "content": [{"type": "text", "text": "ok"}],
                    }
                )

        for hint in self.pending_hints:
            user_blocks.append({"type": "text", "text": hint})

        # 首轮（无 tool_use 配对）才单独把截图作为 image 块给——避免与上面
        # tool_result 内的截图重复，节省 token。
        if not prev_tool_use_ids:
            user_blocks.append(image_block)

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
            # Anthropic 硬约束：thinking enabled 时 temperature 必须 = 1，
            # 否则 API 返回 400 ``temperature may only be set to 1 when
            # thinking is enabled``。当前 API 默认值就是 1.0，但显式写死
            # 是防御性的——避免后人加 temperature 配置时连带 crash。
            payload["temperature"] = 1

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
                # 4xx 客户端错误（含 422 schema 校验）：dump 本次请求 messages
                # 里所有 assistant 块的关键字段摘要，便于快速判断是 tool_use ↔
                # tool_result 失配、还是 thinking 缺 signature、还是其它新增
                # 校验。5xx 服务端错误不 dump（与我们 payload 无关）。
                if 400 <= resp.status_code < 500:
                    block_summary = _summarize_assistant_blocks(request_messages)
                    logger.error(
                        "Claude Messages 4xx | status={} | body={} | "
                        "assistant_blocks={}",
                        resp.status_code,
                        resp.text[:500],
                        block_summary,
                    )
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
        thought, tool_uses, platform_actions, finish_action = _parse_claude_response(
            data
        )

        # ⑤ 把动作链拼成 ParsedAction 列表，顺序：平台动作 → tool_use → 终态
        # 平台动作放在最前面：PLATFORM_ACTION 多用于 open_app / close_app
        # 这种"切换运行 App"动作，自然先于 computer tool 的屏幕交互执行。
        # 若同一轮模型既输出 PLATFORM_ACTION 又输出 tool_use（极少见），
        # 平台动作先跑也更安全（避免在错误 App 上 click）。
        parsed_actions: List[ParsedAction] = []
        action_strs: List[str] = []

        for pa in platform_actions:
            parsed_actions.append(pa)
            action_strs.append(pa.raw or pa.action)

        for tool_use in tool_uses:
            pa = _tool_use_to_parsed_action(tool_use)
            if pa is None:
                continue
            parsed_actions.append(pa)
            action_strs.append(pa.raw or pa.action)

        # ⑥ finished / assert_fail：text 块里的关键字宣告，永远在链末尾
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
        # 模型本轮 raw content blocks 整体作为 assistant 消息保留——这一份
        # "原样保留"同时满足三条 Anthropic 协议硬约束，缺一报 4xx：
        #   ① tool_use 块必须保留：下一轮 user 里的 tool_result 要按
        #      tool_use_id 配对，少了就 ``tool_use ids were found
        #      without tool_result blocks``。
        #   ② thinking 块必须保留 ``signature`` 字段：Anthropic 用它校验
        #      思考链完整性，缺了下一轮请求直接 422
        #      ``messages.X.content.Y.thinking.signature: Field required``。
        #   ③ tool_use 的 ``input`` / ``id`` / ``name``、thinking 的
        #      ``thinking`` 文本本体——续历史时缺任一字段都会 422。
        # 整存整取天然覆盖这些字段；任何"按需筛 block 字段省 token"的优化
        # 都要先确认 Anthropic 当前协议要求，别图省事误伤校验字段。
        assistant_content = data.get("content") or []
        self.messages.append({"role": "assistant", "content": assistant_content})

        # ⑩ tool_result 注入：见步骤 ① 顶部 `_extract_prev_tool_use_ids` +
        # `user_blocks` 构造逻辑。本轮已经把"上一条 assistant 的所有 tool_use"
        # 配对回了 tool_result，无需在此再做处理。

        return Decision(
            thought=thought or "",
            action_str=action_strs[0] if action_strs else "",
            action_strs=action_strs,
            elapsed_ms=elapsed_ms,
            raw_content=json.dumps(data.get("content") or [], ensure_ascii=False),
            parsed_actions=parsed_actions,
        )

    # ------------------------------------------------------------------
    # 内部：tool_use id 提取 + 历史滑窗
    # ------------------------------------------------------------------
    def _extract_prev_tool_use_ids(self) -> List[str]:
        """提取上一条 assistant 消息里所有 tool_use 块的 id（按出现顺序）。

        用于本轮新 user content 的 tool_result 配对。Anthropic 协议硬约束：
        每个 tool_use_id 都必须有一个 tool_result，否则 API 直接 400。

        返回空列表表示：① 首轮（self.messages 为空）；② 上一轮模型只输出
        text/thinking 没调 computer 工具（罕见，但可能发生）；③ 上一条不
        是 assistant（理论不会出现，防御性返回空）。
        """
        if not self.messages:
            return []
        last = self.messages[-1]
        if not isinstance(last, dict) or last.get("role") != "assistant":
            return []
        ids: List[str] = []
        for block in last.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tu_id = block.get("id")
                if isinstance(tu_id, str) and tu_id:
                    ids.append(tu_id)
        return ids

    def _trimmed_messages(self) -> List[Dict[str, Any]]:
        """生成本轮发出去的历史 messages：保留首屏 + 最近 N 步对。

        滑窗策略：保留首屏 1 对（让模型记得 case 起点）+ 最近 window-1 对。
        tool_use / tool_result 配对由调用方 ``decide`` 在拼接新 user 时已经
        处理好；本函数只负责"已存在的历史"按 step 对齐裁剪。

        ⚠️ 裁剪需保证不破坏 tool_use ↔ tool_result 配对：因为我们以"user+
        assistant 一对"为最小单元裁剪，每对内部的配对关系完整（user 的
        tool_result 对应 head/tail 范围内 assistant 的 tool_use），不会出
        现孤立 tool_use。
        """
        if not self.messages:
            return []

        window_pairs = max(1, self._history_window_steps)
        # 一对 = 1 user + 1 assistant，所以 window_pairs * 2 条
        max_keep = window_pairs * 2

        if len(self.messages) <= max_keep:
            trimmed = list(self.messages)
        else:
            head = self.messages[:2]
            tail = self.messages[-(max_keep - 2):]
            trimmed = head + tail

        return trimmed


# ---------------------------------------------------------------------------
# Helper · 4xx 故障诊断：assistant 块结构摘要
# ---------------------------------------------------------------------------
def _summarize_assistant_blocks(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从请求 messages 抽取所有 assistant 消息的块结构摘要。

    用于 4xx 报错时定位是哪类 schema 校验挂了：
    - thinking 块：上报 ``has_signature`` 是否齐
    - tool_use 块：上报 ``id`` 前缀（用于和 user 里 tool_result 对账）
    - text 块：仅上报长度
    其它块类型只上报 type。**只摘要不打全文**，避免日志爆。
    """
    summary: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                summary.append(
                    {
                        "type": "thinking",
                        "has_signature": "signature" in block,
                        "thinking_len": len(str(block.get("thinking") or "")),
                    }
                )
            elif btype == "tool_use":
                tu_id = str(block.get("id") or "")
                summary.append(
                    {
                        "type": "tool_use",
                        "id_prefix": tu_id[:16] + ("..." if len(tu_id) > 16 else ""),
                        "name": block.get("name"),
                    }
                )
            elif btype == "text":
                summary.append(
                    {"type": "text", "text_len": len(str(block.get("text") or ""))}
                )
            else:
                summary.append({"type": btype})
    return summary


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
) -> Tuple[
    str, List[Dict[str, Any]], List[ParsedAction], Optional[ParsedAction]
]:
    """从 Anthropic Messages 响应里抽取 thought / tool_use / 平台动作 / 终态。

    返回四元组：
    - thought：thinking block 文本 + text block 文本拼接（去掉所有协议关键字行）
    - tool_uses：每个 tool_use block 的字典（含 name / input / id 等字段），按
      响应里的顺序
    - platform_actions：从 text 块解析出的 PLATFORM_ACTION 列表（每行一个），
      按出现顺序，已校验白名单
    - finish_action：扫到 FINISHED / ASSERT_FAIL 关键字时返回对应 ParsedAction，
      否则 None
    """
    blocks = data.get("content") or []
    if not isinstance(blocks, list):
        return "", [], [], None

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

    # 平台动作（行级）：先扫，转成 ParsedAction 列表；从 thought 文本里剥掉
    platform_actions = _extract_platform_actions(full_text)

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

    # thought = thinking 块（如有） + 去掉所有协议关键字行的 text 块
    cleaned_text = _PLATFORM_ACTION_RE.sub(
        "",
        _ASSERT_FAIL_RE.sub("", _FINISHED_RE.sub("", full_text)),
    ).strip()
    thought_pieces = [p for p in (thinking_parts + [cleaned_text]) if p]
    thought = "\n".join(thought_pieces)

    return thought, tool_uses, platform_actions, finish_action


def _extract_platform_actions(full_text: str) -> List[ParsedAction]:
    """从 assistant text 拼接里抽取 PLATFORM_ACTION 行，转成 ParsedAction。

    白名单外的动作名（如未来模型自己 hallucinate 的 ``send_intent`` 等）会
    记 warn 后丢弃——避免误执行未授权能力。
    """
    out: List[ParsedAction] = []
    for match in _PLATFORM_ACTION_RE.finditer(full_text):
        action_name = match.group(1).strip().lower()
        app_name = match.group(2).strip()
        if action_name not in _PLATFORM_ACTION_WHITELIST:
            logger.warning(
                "未知 PLATFORM_ACTION 动作名 '{}'（白名单外，丢弃）",
                action_name,
            )
            continue
        if not app_name:
            logger.warning("PLATFORM_ACTION {} 缺 app_name，丢弃", action_name)
            continue
        out.append(
            ParsedAction(
                action=action_name,
                name=app_name,
                raw=f"platform.{action_name}(app_name='{app_name}')",
                coord_space="absolute",
            )
        )
    return out


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
        # Claude scroll 字段：coordinate（起点）, scroll_direction（up/down/left/
        # right）, scroll_amount（次数，整数）。amount 透传给 driver.scroll 的
        # ``amount`` 参数，避免长列表场景模型反复 scroll → 卡死被审判 KILL。
        direction = (args.get("scroll_direction") or "down").lower()
        if direction not in ("up", "down", "left", "right"):
            direction = "down"
        # Anthropic 默认 scroll_amount=3；钳到 [1, 10] 防止模型给极端值（实测
        # 见过给 100 的，driver 真照办会卡 1 分钟）
        try:
            raw_amount = int(args.get("scroll_amount") or 1)
        except (TypeError, ValueError):
            raw_amount = 1
        scroll_amount = max(1, min(10, raw_amount))
        return ParsedAction(
            action="scroll",
            point=point or [500, 500],
            direction=direction,
            scroll_amount=scroll_amount,
            raw=raw_repr,
            coord_space="absolute",
        )

    if action == "key":
        # Anthropic key 名走 X11/xdotool 风格（"Return" / "BackSpace" /
        # "Page_Down"...）。优先查项目专用动作（Home/Back/Escape 走
        # press_home/press_back，约定不变），其余查 X11 → Android keycode 表
        # 转 ACTION_KEY_EVENT，让 runner 调 driver.press_keycode 走通用通道。
        # 表外的键名记 warn 后丢——不要瞎映射避免误触。
        key_name = (args.get("text") or "").lower()
        if key_name in ("home",):
            return ParsedAction(
                action="press_home", raw=raw_repr, coord_space="absolute"
            )
        if key_name in ("back", "escape", "esc"):
            return ParsedAction(
                action="press_back", raw=raw_repr, coord_space="absolute"
            )
        keycode = X11_TO_ANDROID_KEYCODE.get(key_name)
        if keycode is not None:
            return ParsedAction(
                action="key_event",
                keycode=keycode,
                raw=raw_repr,
                coord_space="absolute",
            )
        logger.warning(
            "暂不支持的按键: '{}'（不在 X11→Android keycode 表内），丢弃",
            key_name,
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

    # screenshot 是 Claude 的"观察意图"——首步常见、中途偶尔出现，模型只
    # 是想再看一眼当前状态。我们的 runner 已在每轮开头自动喂截图，所以
    # screenshot 实质是 noop——但**直接丢会让 parsed_actions 空 → 走
    # ParsedAction(unknown) 兜底 → 触发"未知动作"计数 + 提示注入**（无端
    # 占用 unknown_action_streak_limit 配额，连发 3 次就 kill）。转成
    # wait(1s) 既不触发未知计数、又自然过渡到下一轮（下一轮带新截图，模型
    # 看完继续决策），是模型"看一眼再决定"意图的最忠实兑现。
    # 旧版（老 Groovy）就是这么处理的，效果稳定。
    if action == "screenshot":
        return ParsedAction(
            action="wait",
            seconds=1,
            raw=raw_repr,
            coord_space="absolute",
        )

    # 这些 Claude 动作在手机自动化里没用——和 screenshot 不同，它们是模型
    # **理解偏差**（把手机当 PC 处理：鼠标移动 / 按下抬起 / 中键 / 三击 /
    # 长按某键等）。转 wait 反而掩盖问题让模型继续走错；**直接丢 + 让它
    # 下一轮换策略**才是对的——会触发 unknown 兜底，runner 注入"动作不
    # 规范"提示，模型自我纠偏。
    if action in (
        "mouse_move",
        "cursor_position",
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
