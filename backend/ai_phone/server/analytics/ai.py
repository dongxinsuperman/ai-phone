"""大盘 AI 分析客户端：通过辅助系统工厂调多协议 chat completion。

设计变迁（重要）：

- 早期：本文件自己用 ``httpx`` 走豆包 ``/chat/completions``，硬绑死豆包协议
- 现在：通过 :func:`ai_phone.shared.llm.create_assistant` 拿到三家协议之一
  的 ``BaseAssistant`` 实例，调其 ``analyze_text`` —— 跟随 ``assistant_backend``
  自动切换到 Doubao / Claude / OpenAI

这样大盘 AI 分析与"包名匹配 / 通道判定 / 审判 / 断言"4 个辅助调用同源，
开源用户切到 Claude 主 VLM + OpenAI 辅助系统这种异构组合时，AI 分析自动跟
随辅助系统的协议切换，**不再绑死豆包**。

为什么 AI 分析归属辅助系统而非主 VLM：

- AI 分析是**纯文本任务**（喂大盘 JSON、出文字总结），用不上 vision 能力
- 与辅助系统的 4 个调用同属"chat completion 系"，模型档位、计费档位天然对齐
- 切到 Claude / GPT 时，主 VLM 仍走 computer-use 系列，辅助 + AI 分析共用普通 chat

本模块仅保留：

- :class:`AnalyticsAIError`：HTTP 状态码包装异常（路由层 try/except 用）
- :class:`AnalyticsAIClient`：薄包装；构造时拿 assistant 实例，``analyze`` 调
  ``analyze_text``
- :func:`build_user_prompt`：把聚合 snapshot 压成给模型的 user 消息
- ``_SYSTEM_PROMPT``：4 段输出格式约束

``AnalysisResult`` 类型从 ``shared.llm.base`` re-export，避免双 dataclass
漂移导致字段悄悄不一致。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from loguru import logger

from ai_phone.config import get_settings
from ai_phone.shared.llm import create_assistant
from ai_phone.shared.llm.base import AnalysisResult


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
    """给模型的单次 user 消息：数据 JSON 裸贴，前面加一句话问题。"""
    trimmed = _compact_payload(snapshot)
    body = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))
    date = snapshot.get("date") or "今日"
    return f"请分析 {date} 的平台大盘数据，并给出结论 / 预警 / 错误归因 / 建议。数据如下：\n```json\n{body}\n```"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class AnalyticsAIClient:
    """AI 分析客户端薄包装：通过辅助系统工厂走多协议。

    构造时按 ``settings.assistant_backend``（doubao_chat / claude / openai）
    创建对应实现；``analyze`` 调用其 ``analyze_text``，把 RuntimeError 翻译
    成 :class:`AnalyticsAIError`（带 HTTP 状态码，给路由层用）。
    """

    def __init__(
        self,
        *,
        assistant: Optional[Any] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        # 依赖注入支持：单测可以传 mock 实例进来；正常路径不传，按 backend 切
        self._assistant = assistant or create_assistant()
        self.timeout = timeout_seconds

    @property
    def is_configured(self) -> bool:
        """判断 assistant_* / vlm_* 是否至少一组完整。

        与三家 ``analyze_text`` 内部的校验等价：``assistant_api_url``、
        ``assistant_api_key or vlm_api_key``、``assistant_model`` 三件齐
        备即视为已配置。
        """
        s = get_settings()
        api_url = (s.assistant_api_url or "").strip()
        api_key = (s.assistant_api_key or s.vlm_api_key or "").strip()
        model = (s.assistant_model or "").strip()
        return bool(api_url and api_key and model)

    async def analyze(self, snapshot: Dict[str, Any]) -> AnalysisResult:
        """调辅助系统生成一段中文分析。失败抛 :class:`AnalyticsAIError`。"""
        if not self.is_configured:
            raise AnalyticsAIError(_DEFAULT_UNCONFIGURED_HINT, status_code=400)

        user_prompt = build_user_prompt(snapshot)

        try:
            result = await self._assistant.analyze_text(
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                label="AI 分析",
                thinking=False,
                temperature=0.2,
                timeout=self.timeout,
            )
        except RuntimeError as exc:
            # 三家 analyze_text 内部统一抛 RuntimeError（配置缺失 / 网络错误 /
            # 非 200 响应 / 解析失败）。这里不区分原因，统一翻译成 502，让
            # 前端原样展示 message——与历史"VLM 报错先不加重试"策略一致。
            raise AnalyticsAIError(str(exc), status_code=502) from exc

        logger.info(
            "[analytics] AI 分析完成 date={} model={} elapsed={}ms tokens={}",
            snapshot.get("date"), result.model, result.elapsed_ms, result.total_tokens,
        )
        return result


__all__ = [
    "AnalyticsAIClient",
    "AnalyticsAIError",
    "AnalysisResult",
    "build_user_prompt",
]
