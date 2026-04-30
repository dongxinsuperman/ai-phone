"""VLM HTTP 客户端 + token 使用量统计。

迁移自 sonic_all_ai/5-VLM全权处理 copy.groovy 的 vlmDecide / recordTokenUsage /
logTokenSummary。与老实现的关键差异：

1. 走方舟 **Responses API**（``/api/v3/responses``）而非 Chat Completions。
2. 对话历史由服务端维护：客户端只下发新 user 消息 + ``previous_response_id``，
   首轮额外带 system。服务端配合 ``caching:enabled + store:true`` 把 prompt 前缀
   缓存下来，长任务不会每轮重发 system 被打穿缓存。
3. 客户端这边原本的 ``messages: List[Dict]``（对话历史）语义不再适用；
   改成 ``pending_hints: List[str]``：主循环在"发现模型卡死 / 输出非法动作"时
   append 一条文本，下一轮 :meth:`decide` 把它们拼在 user content 最前头一次性
   发出去，发送后清空；请求失败时回滚，避免提示丢失。
4. Token 统计兼容 Responses 返回的 ``input_tokens / output_tokens``，并新增
   ``cached_tokens`` 命中统计（Chat API 通常为 0）。
5. 新增 :meth:`reset_session`：prompt_tokens 逼近单价跨档阈值时，外层可以主动
   归零 ``previous_response_id`` 把任务"切段"，后续每段重新从 system 前缀起步，
   避免整体被拉进 ×2 / ×3 档。
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from ai_phone.config import get_settings
from ai_phone.shared.actions import extract_action, extract_actions, extract_thought


# ---------------------------------------------------------------------------
# Token 统计
# ---------------------------------------------------------------------------
@dataclass
class _SceneAgg:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0


@dataclass
class TokenCounter:
    """累计 prompt / completion / total / cached tokens，按 '{model}|{scene}' 分桶。

    兼容 Chat API（prompt_tokens/completion_tokens）与 Responses API
    （input_tokens/output_tokens）两套字段名；Responses 的 cached 命中走
    ``prompt_tokens_details.cached_tokens`` / ``input_tokens_details.cached_tokens``。
    """

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cached_tokens: int = 0
    call_count: int = 0
    # 上一次成功 record 的 prompt_tokens；主循环用它判定是否触发会话分段。
    last_prompt_tokens: int = 0
    by_scene: Dict[str, _SceneAgg] = field(default_factory=dict)

    def record(self, scene: str, model: str, usage: Optional[Dict[str, Any]]) -> None:
        if not usage:
            logger.warning("{} 响应未返回 usage 字段，无法统计 token", scene)
            return
        # Chat / Responses 两套字段名都兼容
        pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))

        cached = 0
        details = (
            usage.get("prompt_tokens_details")
            or usage.get("input_tokens_details")
            or None
        )
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)

        self.total_prompt_tokens += pt
        self.total_completion_tokens += ct
        self.total_tokens += tt
        self.total_cached_tokens += cached
        self.call_count += 1
        self.last_prompt_tokens = pt

        key = f"{model}|{scene}"
        agg = self.by_scene.setdefault(key, _SceneAgg())
        agg.prompt_tokens += pt
        agg.completion_tokens += ct
        agg.total_tokens += tt
        agg.cached_tokens += cached
        agg.calls += 1

        hit_rate = (cached * 100.0 / pt) if pt > 0 else 0.0
        logger.info(
            "Token 记录 scene={} model={} prompt={}(cached={}, {:.1f}%) completion={} total={} "
            "累计 prompt={} cached={} total={} calls={}",
            scene,
            model,
            pt,
            cached,
            hit_rate,
            ct,
            tt,
            self.total_prompt_tokens,
            self.total_cached_tokens,
            self.total_tokens,
            self.call_count,
        )

    def summary(self) -> Dict[str, Any]:
        details: List[Dict[str, Any]] = []
        for key, agg in self.by_scene.items():
            model, _, scene = key.partition("|")
            details.append(
                {
                    "model": model,
                    "scene": scene,
                    "calls": agg.calls,
                    "prompt_tokens": agg.prompt_tokens,
                    "completion_tokens": agg.completion_tokens,
                    "total_tokens": agg.total_tokens,
                    "cached_tokens": agg.cached_tokens,
                }
            )
        return {
            "call_count": self.call_count,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.total_cached_tokens,
            "by_scene": details,
        }


# ---------------------------------------------------------------------------
# VLM 决策结果
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    """VLM 单轮决策结果。

    历史上每轮只允许 1 个 Action，``action_str`` 是唯一字段。后续放开了"瞬态
    UI 链式动作"（同一 Thought 下输出 ≥ 2 个 Action，由 runner 在不抓中间
    截图的前提下顺序执行），新增 ``action_strs`` 暴露完整列表，``action_str``
    保留为列表第一项以保持向后兼容（旧测试 / 直接消费 Decision 的代码不受影响）。

    ``parsed_actions`` 是为 Claude / GPT 等"结构化输出"协议预留的可选直通字段：
    豆包系输出文本 DSL，runner 走 ``parse_action(action_str)`` 解析；Claude /
    GPT 通过 tool_use / computer_call 已经给出结构化字段，可以在客户端直接装
    成 ``ParsedAction`` 列表写入本字段——runner 端见到非空时优先消费它，跳过
    文本解析；否则回退到旧路径。这样三家协议共用同一个 ``Decision`` 类型，
    runner 上层调用零改动。
    """

    thought: str
    action_str: str
    elapsed_ms: int
    raw_content: str = ""
    action_strs: List[str] = field(default_factory=list)
    # 用 Any 而非 List[ParsedAction] 是为了避免 vlm.py ↔ actions.py 类型层面的
    # 循环（actions.py 已经被 vlm.py 间接导入）；运行期实际类型仍是
    # ``List[ai_phone.shared.actions.ParsedAction]``。
    parsed_actions: Optional[List[Any]] = None


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------
def _extract_response_text(data: Dict[str, Any]) -> str:
    """从 Responses API 返回体里捞 assistant 文本。

    优先 ``output[*].content[*].text``（type ∈ {output_text, text}）；兜底
    ``output_text`` 顶层字段；都没命中则抛异常，上层会 rollback pending hints。
    """
    output = data.get("output") or []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and item.get("role") == "assistant":
                contents = item.get("content") or []
                if not isinstance(contents, list):
                    continue
                for c in contents:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") in ("output_text", "text"):
                        text = c.get("text")
                        if isinstance(text, str) and text.strip():
                            return text.strip()
    ot = data.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot.strip()
    raise RuntimeError(
        "Responses API 响应格式异常，未找到 assistant 文本：" + str(data)[:500]
    )


# ---------------------------------------------------------------------------
# VLM 客户端（Responses API）
# ---------------------------------------------------------------------------
class VLMClient:
    """按 goal 维护一次任务的会话状态，对外提供 :meth:`decide` 单步决策。

    与 Chat 版最大不同：客户端**不再**维护完整 ``messages`` 历史，只维护两样：

    - ``previous_response_id``：上一次响应 id，下一轮请求把它塞进 payload，
      服务端据此续历史（并命中显式缓存）。
    - ``pending_hints``：下一轮请求开始前，主循环可以往里塞提示文本
      （"你连续点击相同位置请换策略"、"动作名不规范请改写"等）。请求时把这些 hint
      拼在 user content 最前面一次性发出去，发送后清空；请求失败则回滚到队头。
    """

    DEFAULT_USER_PROMPT = "What's the next step that you will do to help with the task?"

    def __init__(
        self,
        system_prompt: str,
        counter: Optional[TokenCounter] = None,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 120.0,
        session_reset_prompt_threshold: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.api_url = (api_url or settings.vlm_api_url or "").strip()
        self.api_key = (api_key or settings.vlm_api_key or "").strip()
        self.model = (model or settings.vlm_model or "").strip()
        # 启动期显式校验：比让 httpx 扔 "Illegal header value b'Bearer '" 友好得多
        missing: List[str] = []
        if not self.api_url:
            missing.append("AI_PHONE_VLM_API_URL")
        if not self.api_key:
            missing.append("AI_PHONE_VLM_API_KEY")
        if not self.model:
            missing.append("AI_PHONE_VLM_MODEL")
        if missing:
            raise RuntimeError(
                "VLM 配置缺失，请到 backend/.env 填写后重试："
                + "、".join(missing)
            )

        self.timeout = timeout_seconds
        self.counter = counter or TokenCounter()

        # 会话状态
        self.system_prompt = system_prompt
        self.previous_response_id: Optional[str] = None
        self.pending_hints: List[str] = []
        self.segment_count = 1
        if session_reset_prompt_threshold is None:
            session_reset_prompt_threshold = int(
                getattr(settings, "vlm_session_reset_prompt_threshold", 0) or 0
            )
        self.session_reset_prompt_threshold = session_reset_prompt_threshold

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------
    @property
    def last_prompt_tokens(self) -> int:
        """最近一次 VLM 决策的 prompt_tokens；用于主循环做分段判定。"""
        return self.counter.last_prompt_tokens

    def add_hint(self, text: str) -> None:
        """主循环注入提示文本（卡死检测 / 未知动作保护等）。下一轮 decide 会带上。"""
        if text:
            self.pending_hints.append(text)

    def should_reset_session(self) -> bool:
        """上一轮 prompt 超过阈值且已有会话 → 应该在下一轮请求前重置。"""
        thr = self.session_reset_prompt_threshold
        if not thr or thr <= 0:
            return False
        if self.previous_response_id is None:
            return False
        return self.last_prompt_tokens >= thr

    def reset_session(self, resume_hint: Optional[str] = None) -> Optional[str]:
        """主动切断服务端会话，下一轮从 system 前缀重新开一段。

        返回被清理的旧 response id（字符串或 None），方便上层打日志。
        """
        old_id = self.previous_response_id
        self.previous_response_id = None
        self.segment_count += 1
        if resume_hint:
            self.pending_hints.append(resume_hint)
        # 下一次 record 前把 last_prompt_tokens 归零，防止连续两轮都误判触发
        self.counter.last_prompt_tokens = 0
        return old_id

    async def decide(
        self,
        screenshot_bytes: bytes,
        *,
        mime: str = "image/jpeg",
    ) -> Decision:
        """输入一张截图 bytes，返回一次 VLM 决策（Thought + Action 字符串）。

        与 Chat 版差异：
        - 请求体走 ``input`` 结构而非 ``messages``。
        - 首轮带 system；后续只发 user（服务端靠 previous_response_id 续历史）。
        - content type 是 ``input_image``（``image_url`` 直接是字符串）+
          ``input_text``。
        - 启用 ``caching=enabled`` + ``store=true`` 让服务端缓存前缀。
        """
        b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        # ① 构造 user content：pending hints 在最前，然后截图，最后默认英文提示
        user_content: List[Dict[str, Any]] = []
        for hint in self.pending_hints:
            user_content.append({"type": "input_text", "text": hint})
        user_content.append({"type": "input_image", "image_url": data_url})
        user_content.append({"type": "input_text", "text": self.DEFAULT_USER_PROMPT})

        # 备份用于失败回滚；成功则清空队列
        pending_backup = list(self.pending_hints)
        self.pending_hints.clear()

        # ② 构造 input：首轮带 system；后续由服务端维护历史
        input_items: List[Dict[str, Any]] = []
        is_first_turn = self.previous_response_id is None
        if is_first_turn:
            input_items.append({"role": "system", "content": self.system_prompt})
        input_items.append({"role": "user", "content": user_content})

        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "input": input_items,
            "caching": {"type": "enabled"},
            "store": True,
            "thinking": {"type": "disabled"},
        }
        if self.previous_response_id is not None:
            payload["previous_response_id"] = self.previous_response_id

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.api_url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"VLM Responses API 失败: status={resp.status_code} body={resp.text[:500]}"
                )
            data = resp.json()
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            content = _extract_response_text(data)

            # 保存会话 id 供下一轮 previous_response_id 引用
            new_id = data.get("id")
            if isinstance(new_id, str) and new_id:
                self.previous_response_id = new_id

            self.counter.record("VLM决策", self.model, data.get("usage"))
            short_pid = (self.previous_response_id or "")[:20]
            logger.info(
                "VLM 决策耗时 {}ms | 段={} | previous_response_id={}...",
                elapsed_ms,
                self.segment_count,
                short_pid,
            )

            all_actions = extract_actions(content)
            primary_action = all_actions[0] if all_actions else extract_action(content)
            return Decision(
                thought=extract_thought(content),
                action_str=primary_action,
                action_strs=all_actions,
                elapsed_ms=elapsed_ms,
                raw_content=content,
            )
        except Exception as exc:
            # 请求失败：把注入的 hints 退回队列头，避免丢提示
            if pending_backup:
                self.pending_hints[:0] = pending_backup
            logger.exception("VLM 决策异常")
            raise RuntimeError(f"VLM 决策异常: {exc}") from exc
