"""主 VLM · OpenAI computer-use-preview 实现（原生 Responses API）。

走 OpenAI 原生 Responses API（不是 chat completions），这是 ``computer-use-
preview`` 模型唯一支持的端点（OpenAI docs 明确说明）。

关键参考：OpenAI Computer Use 文档
（https://platform.openai.com/docs/guides/tools-computer-use）。

设计要点：

1. **端点**：``https://api.openai.com/v1/responses``
2. **Tool 声明**：
   ```json
   {
     "type": "computer_use_preview",
     "display_width": <width>,
     "display_height": <height>,
     "environment": "ubuntu"   // 移动端没专用 env，ubuntu 兼容性最好
   }
   ```
3. **服务端续历史**：与豆包 Responses 类似，OpenAI Responses API 也支持
   ``previous_response_id``，所以本类维护服务端 session id 而不是客户端
   messages 数组（与 Claude 不同）。
4. **绝对像素坐标**：computer-use-preview 输出绝对像素，构造 ParsedAction 时
   显式 ``coord_space="absolute"``。
5. **finished / assert_fail**：约定模型在 message text 里写关键字宣告，与
   Claude 路径一致。
6. **思考链**：``reasoning.effort = low/medium/high``（OpenAI 推理模型语义），
   不通过 budget_tokens 控制；computer-use-preview 自带推理，effort 仅微调。

注意：
- OpenAI computer-use-preview 文档要求每次请求都把"上一次 computer_call 的结
  果"以 ``computer_call_output`` 类型回传给模型——这是工具调用 ack。我们的
  runner 流程是"模型给动作 → executor 执行 → 下一轮重抓截图"，没有显式 ack。
  通过在每轮新 user input 顶部塞一个 ``computer_call_output`` 引用上一次的
  call_id 来满足协议（content 用 placeholder 截图字段）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger


# 网络/超时类异常 → 自动重试 1 次（与 VLMClient / ClaudeCUClient 保持一致）。
# ``TransportError`` 是基类，覆盖 ``NetworkError`` / ``RemoteProtocolError``
# / ``ReadError`` / ``WriteError``。
_GPT_RETRIABLE_NET_ERRORS: Tuple[type, ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
)

from ai_phone.config import get_settings
from ai_phone.shared.actions import ParsedAction, X11_TO_ANDROID_KEYCODE
from ai_phone.shared.llm.base import Decision, TokenCounter

__all__ = ["GPTComputerUseClient"]


# 终态关键字正则（与 Claude 同协议）
_FINISHED_RE = re.compile(r"^\s*FINISHED\s*[:：]\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ASSERT_FAIL_RE = re.compile(
    r"^\s*ASSERT_FAIL\s*[:：]\s*(.*)$", re.IGNORECASE | re.MULTILINE
)

# Platform Action 文本协议（与 Claude CU 同源），用于让 GPT computer-use-
# preview 在执行中调用平台原生能力（包名级 open_app / close_app 等）——
# 这些是 computer tool 不具备的"项目级抽象"，又是 VLM 走"home + 找图标"
# 路径最不可靠的部分。格式：
#   PLATFORM_ACTION: open_app(app_name='洋葱学园')
#   PLATFORM_ACTION: close_app(app_name='淘宝')
# 行首匹配 + 兼容全角冒号 + 单/双引号。匹配组：(action_name, app_name)。
_PLATFORM_ACTION_RE = re.compile(
    r"^\s*PLATFORM_ACTION\s*[:：]\s*(\w+)\s*\(\s*app_name\s*=\s*"
    r"['\"]([^'\"]+)['\"]\s*\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# 当前白名单只放 open_app / close_app（与 claude_cu.py 保持一致）。
_PLATFORM_ACTION_WHITELIST = frozenset({"open_app", "close_app"})


class GPTComputerUseClient:
    """主 VLM · OpenAI computer-use-preview 客户端。

    与豆包 ``VLMClient`` 一样走 Responses API + previous_response_id 续历史；
    与 Claude 不同的是 OpenAI 服务端会自己维护历史，客户端只需保存 session id。
    """

    def __init__(
        self,
        system_prompt: str,
        counter: Optional[TokenCounter] = None,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 180.0,
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
                "GPT 主 VLM 配置缺失，请到 backend/.env 填写后重试："
                + "、".join(missing)
            )

        self.timeout = timeout_seconds
        self.counter = counter or TokenCounter()
        self.system_prompt = system_prompt

        # 服务端会话 id；首轮请求带 system，之后只带 previous_response_id 续历史
        self.previous_response_id: Optional[str] = None
        self.pending_hints: List[str] = []
        # 上一次 computer_call 的 call_id，本轮请求需要回传 computer_call_output ack
        self._last_call_id: Optional[str] = None
        self._last_pending_safety_checks: List[Dict[str, Any]] = []

        self.segment_count = 1

        # 推理强度（low/medium/high），从 settings 读，默认 medium。
        # computer-use-preview 自带推理，必须有非零 effort，不能关。
        effort = (settings.vlm_main_reasoning_effort or "medium").strip().lower()
        if effort not in ("low", "medium", "high"):
            logger.warning(
                "vlm_main_reasoning_effort 取值非法 ({}), 回退 medium", effort
            )
            effort = "medium"
        self._reasoning_effort = effort

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
        # OpenAI Responses API 没有显式缓存的会话分段优化，但服务端历史会随
        # previous_response_id 不断累计计费——我们暂不做主动分段，让模型自己
        # 利用其上下文窗口。如未来发现 token 增长曲线不健康再补。
        return False

    def reset_session(self, resume_hint: Optional[str] = None) -> Optional[str]:
        old_id = self.previous_response_id
        self.previous_response_id = None
        self._last_call_id = None
        self._last_pending_safety_checks = []
        self.segment_count += 1
        if resume_hint:
            self.pending_hints.append(resume_hint)
        self.counter.last_prompt_tokens = 0
        return old_id

    # ------------------------------------------------------------------
    # HTTP 工具
    # ------------------------------------------------------------------
    async def _post_with_retry(
        self,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        *,
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        """对 OpenAI Responses API 做 1 次重试的 POST 包装。

        重试条件（仅这两类，业务异常立刻抛）：
        - httpx 网络/超时类：``TimeoutException`` / ``TransportError``
        - HTTP 5xx / 429（限流）

        4xx（除 429）= payload 本身错（鉴权 / schema / model 不存在等）→ 重试
        无意义；立刻抛带 body。``timeout_seconds`` 由调用方根据"分段后首轮要
        翻倍"等场景化决策传入。
        """
        attempts_max = 2
        retry_backoff_sec = 0.5
        last_exc: Optional[BaseException] = None
        for attempt_idx in range(attempts_max):
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    resp = await client.post(
                        self.api_url, json=payload, headers=headers
                    )
            except _GPT_RETRIABLE_NET_ERRORS as net_exc:
                last_exc = net_exc
                if attempt_idx + 1 >= attempts_max:
                    raise
                logger.warning(
                    "GPT 决策网络异常 ({}: {})，{:.1f}s 后重试 1 次 | 段={} 首轮={} timeout={}s",
                    net_exc.__class__.__name__, net_exc, retry_backoff_sec,
                    self.segment_count, self.previous_response_id is None,
                    timeout_seconds,
                )
                await asyncio.sleep(retry_backoff_sec)
                continue

            if resp.status_code == 200:
                return resp.json()

            if (resp.status_code == 429 or 500 <= resp.status_code < 600) \
                    and attempt_idx + 1 < attempts_max:
                logger.warning(
                    "OpenAI Responses API status={} body={}，{:.1f}s 后重试 1 次 | 段={} 首轮={}",
                    resp.status_code, resp.text[:200], retry_backoff_sec,
                    self.segment_count, self.previous_response_id is None,
                )
                await asyncio.sleep(retry_backoff_sec)
                continue

            raise RuntimeError(
                f"OpenAI Responses API 失败: status={resp.status_code} "
                f"body={resp.text[:500]}"
            )

        raise RuntimeError(
            f"GPT 决策异常: 重试 {attempts_max} 次仍失败"
            + (f"，最近一次：{last_exc.__class__.__name__}: {last_exc}" if last_exc else "")
        )

    # ------------------------------------------------------------------
    # 主决策
    # ------------------------------------------------------------------
    async def decide(
        self,
        screenshot_bytes: bytes,
        *,
        mime: str = "image/jpeg",
    ) -> Decision:
        """单步决策：截图 → OpenAI Responses API → ParsedAction 列表。"""
        screen_w, screen_h = _decode_image_size(screenshot_bytes)

        # ① 构造 input：本轮 user 的 content 列表
        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        # 上一次 computer_call 的 ack（OpenAI 协议要求）：每个新 user input 之
        # 前要回传 computer_call_output（携带最新截图作为执行后状态）。这层在
        # 第二轮起生效。
        input_items: List[Dict[str, Any]] = []

        if self._last_call_id is not None:
            ack_item: Dict[str, Any] = {
                "type": "computer_call_output",
                "call_id": self._last_call_id,
                "output": {
                    "type": "input_image",
                    "image_url": data_url,
                },
            }
            # 如果上一轮存在 pending_safety_checks，按官方文档要做 ack
            if self._last_pending_safety_checks:
                ack_item["acknowledged_safety_checks"] = (
                    self._last_pending_safety_checks
                )
            input_items.append(ack_item)

        # 当前 user：文本 hints + 截图（首轮时；非首轮的截图已在 ack 里给过，
        # 但模型仍然偶尔会问"current state"，多一份截图不会被惩罚）
        user_content: List[Dict[str, Any]] = []
        for hint in self.pending_hints:
            user_content.append({"type": "input_text", "text": hint})
        user_content.append({"type": "input_image", "image_url": data_url})
        if not self.pending_hints:
            user_content.append(
                {"type": "input_text", "text": "What's the next action?"}
            )

        input_items.append({"role": "user", "content": user_content})

        # 备份用于失败回滚
        pending_backup = list(self.pending_hints)
        self.pending_hints.clear()

        # ② Tool 声明（按当前截图实际尺寸）
        computer_tool: Dict[str, Any] = {
            "type": "computer_use_preview",
            "display_width": screen_w,
            "display_height": screen_h,
            # 移动端没有专门的 environment 选项；ubuntu 是最通用的（OpenAI 内
            # 部对 ubuntu 训练数据最多）。如果未来 OpenAI 开放 mobile env 再切。
            "environment": "ubuntu",
        }

        payload: Dict[str, Any] = {
            "model": self.model,
            "tools": [computer_tool],
            "input": input_items,
            "truncation": "auto",
            # computer-use-preview 的推理力度：low / medium / high。
            # 由 settings.vlm_main_reasoning_effort 控制（env: AI_PHONE_VLM_MAIN_REASONING_EFFORT）；
            # 默认 medium 平衡速度和准确度。
            "reasoning": {"effort": self._reasoning_effort},
        }
        if self.previous_response_id is None:
            # 首轮带 instructions（OpenAI Responses API 等价于 system prompt）
            payload["instructions"] = self.system_prompt
        else:
            payload["previous_response_id"] = self.previous_response_id

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 单次 timeout：分段重置后首轮（segment > 1 且 previous_response_id 已
        # 归零）服务端无法命中显式缓存，整个 instructions 前缀 + 续接 hint 都
        # 要重新推理，单次耗时显著拉长。给 timeout 翻倍兜底，避免 ReadTimeout
        # 把这个本来能正常响应的请求误杀。与 VLMClient 同款策略。
        is_first_turn_local = self.previous_response_id is None
        turn_timeout = float(self.timeout)
        if is_first_turn_local and self.segment_count > 1:
            turn_timeout = max(turn_timeout, float(self.timeout) * 2)

        t0 = time.monotonic()
        try:
            data = await self._post_with_retry(
                payload, headers, timeout_seconds=turn_timeout
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:
            if pending_backup:
                self.pending_hints[:0] = pending_backup
            logger.exception("GPT 决策异常")
            # 带上 exc 类名：httpx.ReadTimeout / httpx.ConnectError 等网络
            # 异常的 str(exc) 在新版 httpx 里是空字符串，没 class 名根本看不
            # 出错因。与 VLMClient / ClaudeCUClient 保持一致。
            raise RuntimeError(
                f"GPT 决策异常: {exc.__class__.__name__}: {exc}"
            ) from exc

        # ③ 解析 response.output blocks
        thought, computer_calls, platform_actions, finish_action = (
            _parse_gpt_response(data)
        )

        # 缓存最近一次 computer_call 的 call_id 与 safety_checks，下一轮 ack 用
        if computer_calls:
            last = computer_calls[-1]
            self._last_call_id = last.get("call_id")
            self._last_pending_safety_checks = last.get("pending_safety_checks") or []
        else:
            # 模型本轮没给 computer_call（只给 message text / reasoning /
            # PLATFORM_ACTION），下一轮就不需要 ack
            self._last_call_id = None
            self._last_pending_safety_checks = []

        # ④ 拼装动作链：平台动作 → computer_call → 终态。理由同 claude_cu.py
        parsed_actions: List[ParsedAction] = []
        action_strs: List[str] = []

        for pa in platform_actions:
            parsed_actions.append(pa)
            action_strs.append(pa.raw or pa.action)

        for cc in computer_calls:
            pa = _computer_call_to_parsed_action(cc)
            if pa is None:
                continue
            parsed_actions.append(pa)
            action_strs.append(pa.raw or pa.action)

        if finish_action is not None:
            parsed_actions.append(finish_action)
            action_strs.append(finish_action.raw or finish_action.action)

        if not parsed_actions:
            logger.warning(
                "GPT 决策未输出任何 computer_call / finished 关键字，按未知动作处理"
            )
            placeholder = ParsedAction(
                action="unknown",
                raw="(empty response)",
                coord_space="absolute",
            )
            parsed_actions = [placeholder]
            action_strs = [placeholder.raw]

        # ⑤ Token 统计
        usage = data.get("usage") or {}
        normalized: Dict[str, Any] = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": int(usage.get("total_tokens") or 0)
            or (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            ),
        }
        # cache 字段（OpenAI 可能未来加）
        details = usage.get("input_tokens_details")
        if isinstance(details, dict):
            normalized["input_tokens_details"] = details
        self.counter.record("VLM决策", self.model, normalized)

        # ⑥ 服务端会话 id
        new_id = data.get("id")
        if isinstance(new_id, str) and new_id:
            self.previous_response_id = new_id

        return Decision(
            thought=thought or "",
            action_str=action_strs[0] if action_strs else "",
            action_strs=action_strs,
            elapsed_ms=elapsed_ms,
            raw_content=json.dumps(data.get("output") or [], ensure_ascii=False),
            parsed_actions=parsed_actions,
        )


# ---------------------------------------------------------------------------
# Helper · 截图尺寸 decode（与 claude_cu.py 同实现，复制以避免跨家 import 耦合）
# ---------------------------------------------------------------------------
def _decode_image_size(image_bytes: bytes) -> Tuple[int, int]:
    """从截图 bytes 解码出 (width, height)。"""
    try:
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception as exc:
        logger.warning("PIL decode 截图尺寸失败({})，回退到 bytes 扫描", exc)

    try:
        if image_bytes[:3] == b"\xff\xd8\xff":
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
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(image_bytes[16:20], "big")
            h = int.from_bytes(image_bytes[20:24], "big")
            return int(w), int(h)
    except Exception:
        pass
    logger.warning("无法识别截图格式，退化使用 1080x2400 默认尺寸")
    return 1080, 2400


# ---------------------------------------------------------------------------
# Helper · 解析 OpenAI Responses 响应
# ---------------------------------------------------------------------------
def _parse_gpt_response(
    data: Dict[str, Any],
) -> Tuple[
    str, List[Dict[str, Any]], List[ParsedAction], Optional[ParsedAction]
]:
    """从 OpenAI Responses 响应抽取 thought / computer_call / 平台动作 / 终态。

    返回四元组：
    - thought：reasoning summary（如有） + message text 拼接（去掉所有协议关键字行）
    - computer_calls：每个 computer_call 块（含 call_id / action / pending_safety_checks）
    - platform_actions：从 message text 解析的 PLATFORM_ACTION 列表，已校验白名单
    - finish_action：扫到 FINISHED / ASSERT_FAIL 关键字时返回对应 ParsedAction
    """
    output_items = data.get("output") or []
    if not isinstance(output_items, list):
        return "", [], [], None

    reasoning_parts: List[str] = []
    text_parts: List[str] = []
    computer_calls: List[Dict[str, Any]] = []

    for item in output_items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            # reasoning.summary 是 list of text；reasoning 块本身的实际推理文本
            # OpenAI 不暴露（只暴露 summary）。可能为空。
            summary = item.get("summary") or []
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict) and isinstance(s.get("text"), str):
                        reasoning_parts.append(s["text"].strip())
        elif itype == "message":
            for c in item.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("output_text", "text"):
                    t = c.get("text") or ""
                    if isinstance(t, str) and t.strip():
                        text_parts.append(t.strip())
        elif itype == "computer_call":
            computer_calls.append(item)

    full_text = "\n".join(text_parts)

    platform_actions = _extract_platform_actions(full_text)

    finish_action: Optional[ParsedAction] = None
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

    cleaned_text = _PLATFORM_ACTION_RE.sub(
        "",
        _ASSERT_FAIL_RE.sub("", _FINISHED_RE.sub("", full_text)),
    ).strip()
    thought_pieces = [p for p in (reasoning_parts + [cleaned_text]) if p]
    thought = "\n".join(thought_pieces)

    return thought, computer_calls, platform_actions, finish_action


def _extract_platform_actions(full_text: str) -> List[ParsedAction]:
    """从 assistant text 拼接里抽取 PLATFORM_ACTION 行，转成 ParsedAction。

    白名单外的动作名记 warn 后丢弃，避免误执行未授权能力。与
    claude_cu.py._extract_platform_actions 同实现，复制以避免跨家 import 耦合
    （延续本目录"主 VLM 实现互不依赖"的风格惯例）。
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
# Helper · OpenAI computer_call → ParsedAction 映射
# ---------------------------------------------------------------------------
# computer-use-preview 内置动作集（来自 OpenAI 文档）：
#   click(x, y, button)            button ∈ {"left","right","wheel","back","forward"}
#   double_click(x, y)
#   scroll(x, y, scroll_x, scroll_y)
#   type(text)
#   keypress(keys=[])
#   wait(...)                      duration 字段官方文档没明确，但 ms 级
#   screenshot()                   仅 ack 用
#   move(x, y)                     悬停（移动端没意义）
#   drag(path=[{x,y}, {x,y}, ...])
def _computer_call_to_parsed_action(
    cc: Dict[str, Any],
) -> Optional[ParsedAction]:
    """把单个 computer_call 块翻译成项目内 ParsedAction。"""
    action_obj = cc.get("action") or {}
    if not isinstance(action_obj, dict):
        logger.warning("computer_call.action 不是字典: {}", action_obj)
        return None

    atype = (action_obj.get("type") or "").strip()
    raw_repr = f"computer.{atype}({json.dumps(action_obj, ensure_ascii=False)})"

    def _xy(obj: Dict[str, Any]) -> Optional[List[int]]:
        x = obj.get("x")
        y = obj.get("y")
        try:
            return [int(x), int(y)]
        except (TypeError, ValueError):
            return None

    if atype == "click":
        pt = _xy(action_obj)
        if pt is None:
            return None
        button = (action_obj.get("button") or "left").lower()
        if button == "right":
            return ParsedAction(
                action="long_press",
                point=pt,
                raw=raw_repr,
                coord_space="absolute",
            )
        # left / wheel / back / forward 都映射成普通 click（移动端没多按钮概念）
        return ParsedAction(
            action="click", point=pt, raw=raw_repr, coord_space="absolute"
        )

    if atype == "double_click":
        pt = _xy(action_obj)
        if pt is None:
            return None
        return ParsedAction(
            action="double_tap",
            point=pt,
            raw=raw_repr,
            coord_space="absolute",
        )

    if atype == "scroll":
        pt = _xy(action_obj) or [500, 500]
        try:
            sx = int(action_obj.get("scroll_x") or 0)
            sy = int(action_obj.get("scroll_y") or 0)
        except (TypeError, ValueError):
            sx = sy = 0
        # OpenAI 滚动矢量：正 y 向下，负 y 向上；正 x 向右，负 x 向左。
        # 我们项目 direction 语义：down=往下浏览，up=往上浏览。
        if abs(sy) >= abs(sx):
            direction = "down" if sy > 0 else "up"
            magnitude = abs(sy)
        else:
            direction = "right" if sx > 0 else "left"
            magnitude = abs(sx)
        # OpenAI 没有 amount 字段，用像素绝对值估算"模型想滚多少屏"——
        # 100px 折一次 swipe（和移动端典型 fling 距离一致）。钳到 [1, 10] 防
        # 极端值（实测见过模型给 sy=2000 想直接翻 N 屏的情况）。
        scroll_amount = max(1, min(10, int(round(magnitude / 100)))) if magnitude else 1
        return ParsedAction(
            action="scroll",
            point=pt,
            direction=direction,
            scroll_amount=scroll_amount,
            raw=raw_repr,
            coord_space="absolute",
        )

    if atype == "type":
        text = action_obj.get("text") or ""
        return ParsedAction(
            action="type",
            content=str(text),
            raw=raw_repr,
            coord_space="absolute",
        )

    if atype == "keypress":
        keys = action_obj.get("keys") or []
        if not isinstance(keys, list) or not keys:
            return None
        # 取第一个键做映射（多按键组合移动端没用）。优先查 Home/Back/Escape
        # 专用动作；其余查 X11 → Android keycode 表，转通用 ACTION_KEY_EVENT。
        # OpenAI 文档里 keypress 键名不严格 X11——可能是 "ENTER" / "Enter" /
        # "Return"，统一小写后查表（X11_TO_ANDROID_KEYCODE 用小写 key）。
        key_name = str(keys[0]).strip().lower()
        if "home" in key_name:
            return ParsedAction(
                action="press_home", raw=raw_repr, coord_space="absolute"
            )
        if any(s in key_name for s in ("back", "escape", "esc")):
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
            "暂不支持的按键组合: {}（不在 X11→Android keycode 表内），丢弃",
            keys,
        )
        return None

    if atype == "wait":
        # OpenAI 没明确 duration 字段；computer-use-preview 一般用默认 1s 等待
        return ParsedAction(
            action="wait", seconds=1, raw=raw_repr, coord_space="absolute"
        )

    if atype == "drag":
        path = action_obj.get("path") or []
        if not isinstance(path, list) or len(path) < 2:
            logger.warning("drag path 不足 2 点，丢弃")
            return None
        first = path[0]
        last = path[-1]
        if not (isinstance(first, dict) and isinstance(last, dict)):
            return None
        sp = _xy(first)
        ep = _xy(last)
        if sp is None or ep is None:
            return None
        return ParsedAction(
            action="drag",
            start_point=sp,
            end_point=ep,
            raw=raw_repr,
            coord_space="absolute",
        )

    # screenshot 是 GPT 的"观察意图"——首步常见、中途偶尔。OpenAI 文档说
    # 它是 ack 类，但模型仍然可能主动调来"看一眼当前状态"。我们的 runner
    # 已在每轮开头自动喂截图，screenshot 实质 noop——但**直接丢会让
    # parsed_actions 空 → 触发"未知动作"计数**（连发 3 次直接 kill）。
    # 转 wait(1s) 既不触发未知计数、又自然过渡到下一轮新截图，是模型
    # "看一眼再决定"意图的最忠实兑现。与 claude_cu.py 同处理保持一致。
    if atype == "screenshot":
        return ParsedAction(
            action="wait",
            seconds=1,
            raw=raw_repr,
            coord_space="absolute",
        )

    # move 是模型把手机当 PC（鼠标悬停）——理解偏差。直接丢 + 让它下一轮
    # 换策略，不要用 wait 掩盖。
    if atype == "move":
        logger.debug("OpenAI {} 在移动端无意义，丢弃", atype)
        return None

    logger.warning("未识别的 OpenAI computer_call.type: '{}'，丢弃", atype)
    return None
