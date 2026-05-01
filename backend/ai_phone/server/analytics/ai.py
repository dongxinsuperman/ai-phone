"""大盘 AI 分析客户端：纯文本一次性调用 chat completion。

和 ``shared/vlm.py``（主 VLM 决策）的区别：

- VLMClient 走 Responses API（会话状态 / 截图输入 / caching 前缀），不适合单次纯文本
- 这里走 ``/chat/completions`` 端点，单次问答、不落 previous_response_id、不维护历史
- 不做重试：用户明确要求"报错先不加重试"；前端失败就把错误原样展示，让用户决定

模型档位选择（与辅助系统对齐）：

- AI 分析是**纯文本任务**（喂 JSON 出文字总结），不需要 vision 能力。
- 默认走 ``assistant_*`` 配置（如 ``doubao-seed-1-6-250615`` 通用版），比 vision
  专版便宜快一档；与"包名匹配 / 通道判定 / 审判 / 断言"4 个辅助调用同档。
- ``assistant_*`` 留空时回退到 ``vlm_*``（兼容老 .env：主辅同 key 同模型）。
- 调用方可显式传 ``api_url / api_key / model`` 覆盖默认值。

输入：聚合切片 dict；输出：人类可读中文分析文本 + usage 摘要。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from ai_phone.config import get_settings


# 默认给前端的简要兜底文案（仅在 client 未配置时使用，避免 500）
_DEFAULT_UNCONFIGURED_HINT = (
    "AI 分析未启用：尚未配置辅助系统或主 VLM 接入信息 "
    "（需要 ASSISTANT 或 VLM 任一组：API_URL + API_KEY + MODEL）。"
)


class AnalyticsAIError(RuntimeError):
    """AI 分析专用异常。路由层用它分 400/502 返回。"""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class AnalysisResult:
    model: str
    text: str
    elapsed_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# ---------------------------------------------------------------------------
# Prompt 构造
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """你是资深测试平台分析助手，专门点评"AI 云真机执行器"的当日运行情况。
你说中文，结论冷静、可执行，不啰嗦、不寒暄、不自我介绍。

输出格式严格遵守（前端会按这个解析渲染）：

1. 全文必须分成 4 段，每段以 `【段名】` 单独成行作为段头（中文方括号），段内只能用 `- `项目符号或纯文本，禁止 Markdown 标题、表格、代码块、链接、emoji。
2. 段头按以下顺序，且名称必须完全一致：
   - 【整体结论】 一句话给当日定性（成功率高低 / 是否有平台风险 / 有无重要预警），不超过 60 字
   - 【关键指标】 3-5 条要点；带具体数字（成功率、Token 消耗、p95 耗时、活跃设备数等），引用数据要准确不能编造
   - 【错误归因】 3-6 条要点；只关注"平台原因"（VLM 不可达 / 卡死 / 设备掉线 / 排队超时 / 内部错误等），按设备 / 平台 / 模型聚类讲；明确指出"业务断言失败"已被排除在稳定率外
   - 【改进建议】 1-3 条要点；每条要可执行（动作 + 受益），避免"建议关注稳定性"这种空话

3. 项目符号每行一个要点，不要写成长段落；每行 ≤ 80 字。
4. 如果当日样本极少（< 5 条已完成）或完全没失败，第三段直接写 `- 样本不足，无可归因事件` 即可，不要硬凑。
5. 严禁重复回显输入 JSON、严禁列举所有 case 名字。
6. 数字使用阿拉伯数字；时长用「秒」「分」；token 用「k」/「万」简化。
"""


def _compact_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """把聚合切片里前端专用的大字段裁剪掉，只留分析需要的结构。

    失败 case 里 ``firstErrorLog`` / ``statusReason`` 保留；截图 URL、所有
    reportUrl、设备历史里的 serial 明细都是展示用的，AI 不需要。
    """
    throughput = snapshot.get("throughput", {}) or {}
    stability = snapshot.get("stability", {}) or {}
    token = snapshot.get("token", {}) or {}
    devices = snapshot.get("devices", {}) or {}
    devices_today = (devices.get("today", {}) or {}).get("byDevice", []) or []
    devices_health = (devices.get("health", {}) or {}).get("byDevice", []) or []

    # 失败 case 给 AI 时最多 15 条；够归因了，不再膨胀 prompt。
    failed = list(stability.get("failedCases", []) or [])[:15]
    trimmed_failed = [
        {
            "caseName": f.get("caseName"),
            "platform": f.get("platform"),
            "deviceSerial": f.get("deviceSerial"),
            "state": f.get("state"),
            "statusReason": f.get("statusReason"),
            "elapsedMs": f.get("elapsedMs"),
            "firstErrorLog": f.get("firstErrorLog"),
        }
        for f in failed
    ]

    # 设备明细只传"当日跑过的" + "历史前 10 失败率最高的"——AI 关心问题机而非全量。
    trimmed_today = [
        {
            "serial": d.get("serial"),
            "platform": d.get("platform"),
            "itemsTotal": d.get("itemsTotal"),
            "success": d.get("success"),
            "failed": d.get("failed"),
            "cancelled": d.get("cancelled"),
        }
        for d in devices_today
    ]
    trimmed_health = [
        {
            "serial": d.get("serial"),
            "platform": d.get("platform"),
            "model": d.get("model"),
            "totalRuns": d.get("totalRuns"),
            "successRate": d.get("successRate"),
            "currentStatus": d.get("currentStatus"),
        }
        for d in devices_health[:10]
    ]

    submissions = [
        {
            "submissionName": s.get("submissionName"),
            "state": s.get("state"),
            "totalItems": s.get("totalItems"),
            "counts": s.get("counts"),
            "platformCounts": s.get("platformCounts"),
            "elapsedMs": s.get("elapsedMs"),
        }
        for s in (snapshot.get("submissions") or [])[:20]
    ]

    return {
        "date": snapshot.get("date"),
        "timezone": snapshot.get("timezone"),
        "totalSubmissions": snapshot.get("totalSubmissions"),
        "totalItems": snapshot.get("totalItems"),
        "throughput": {
            "byState": throughput.get("byState"),
            "byPlatform": throughput.get("byPlatform"),
            "successRate": throughput.get("successRate"),
            "avgElapsedMs": throughput.get("avgElapsedMs"),
            "p95ElapsedMs": throughput.get("p95ElapsedMs"),
        },
        "stability": {
            "platformStabilityRate": stability.get("platformStabilityRate"),
            "platformFailureCount": stability.get("platformFailureCount"),
            "businessFailureCount": stability.get("businessFailureCount"),
            "failureByReason": stability.get("failureByReason"),       # 仅平台原因
            "businessReasons": stability.get("businessReasons"),       # 业务/人为，告知 AI 已被排除
            "failedCases": trimmed_failed,                             # 仅平台原因失败
        },
        "devices": {
            "today": trimmed_today,
            "problematic": trimmed_health,
        },
        "token": {
            "callCount": token.get("callCount"),
            "totalTokens": token.get("totalTokens"),
            "cachedTokens": token.get("cachedTokens"),
            "byPlatform": token.get("byPlatform"),
            "byModel": token.get("byModel"),
        },
        "submissions": submissions,
    }


def build_user_prompt(snapshot: Dict[str, Any]) -> str:
    """给豆包的单次 user 消息：数据 JSON 裸贴，前面加一句话问题。"""
    trimmed = _compact_payload(snapshot)
    body = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
    date = snapshot.get("date") or "今日"
    return f"请分析 {date} 的平台大盘数据，并给出结论 / 预警 / 错误归因 / 建议。数据如下：\n```json\n{body}\n```"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class AnalyticsAIClient:
    """封装一次 chat completion 请求；不带重试、不带多轮。"""

    def __init__(
        self,
        *,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        s = get_settings()
        # 与辅助系统对齐：assistant_* 优先（通用版便宜快、纯文本最匹配），
        # 留空时回退 vlm_*（老 .env 兼容；主辅同 key 同模型场景无感）。
        self.api_url = (api_url or s.assistant_api_url or s.vlm_chat_api_url or "").strip()
        self.api_key = (api_key or s.assistant_api_key or s.vlm_api_key or "").strip()
        self.model = (model or s.assistant_model or s.vlm_model or "").strip()
        self.timeout = timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key and self.model)

    async def analyze(self, snapshot: Dict[str, Any]) -> AnalysisResult:
        """调豆包生成一段中文分析。失败抛 :class:`AnalyticsAIError`。"""
        if not self.is_configured:
            raise AnalyticsAIError(_DEFAULT_UNCONFIGURED_HINT, status_code=400)

        user_prompt = build_user_prompt(snapshot)

        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.api_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AnalyticsAIError(f"VLM 请求失败: {exc}", status_code=502) from exc

        if resp.status_code != 200:
            raise AnalyticsAIError(
                f"VLM 返回 {resp.status_code}: {resp.text[:400]}",
                status_code=502,
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            data = resp.json()
        except ValueError as exc:
            raise AnalyticsAIError("VLM 返回非 JSON", status_code=502) from exc

        text = _extract_chat_text(data)
        if not text:
            raise AnalyticsAIError(
                "VLM 响应没有可用文本内容：" + json.dumps(data, ensure_ascii=False)[:400],
                status_code=502,
            )

        usage = data.get("usage") or {}
        result = AnalysisResult(
            model=self.model,
            text=text,
            elapsed_ms=elapsed_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
        logger.info(
            "[analytics] AI 分析完成 date={} model={} elapsed={}ms tokens={}",
            snapshot.get("date"), self.model, elapsed_ms, result.total_tokens,
        )
        return result


def _extract_chat_text(data: Dict[str, Any]) -> str:
    """豆包 Chat Completions: ``choices[0].message.content``；防御性处理一下 content 是 list 的情况。"""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    # 少数情况下 content 是 list of {type, text}
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("text", "output_text"):
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts).strip()
    return ""


__all__ = [
    "AnalyticsAIClient",
    "AnalyticsAIError",
    "AnalysisResult",
    "build_user_prompt",
]
