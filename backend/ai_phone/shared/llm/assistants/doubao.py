"""辅助系统 · Doubao Chat Completions 实现。

把 vlm_loop.py 里 ``_chat_text`` / ``_match_package_name`` /
``_verify_finished_assertion`` 三段网络调用搬过来，剥离业务编排（log /
错误重试 / SKIP-PASS-FAIL 解析），只保留"打方舟 Chat Completions →
解析返回 → 累加 token"这一段纯协议层。

vlm_loop.py 在 P4 接入工厂后会保留这三个方法名作为薄包装，调用本类的
对应方法实现。这样：

1. 4 处历史调用点（_chat_text 在 audit / judge_channel / decompose 三处 +
   _match_package_name 在 open_app / close_app 两处共 4 处 + verify_finished
   在 finished 路径 1 处）的代码都不用改。
2. 切换到 Claude / GPT 时只改 ``settings.assistant_backend``，工厂返回另一
   家实现，vlm_loop 端零改动。
3. 行为保持与现状完全一致——本文件搬运的代码与 vlm_loop 原版逐行对比无
   遗漏（除把 ``self._log`` 调用搬走以外，因为日志属于业务编排不属于协议）。
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import httpx

from ai_phone.config import get_settings
from ai_phone.shared.llm.base import TokenCounter

__all__ = ["DoubaoAssistant"]


class DoubaoAssistant:
    """辅助系统 · 方舟 Chat Completions（``doubao-seed-1-6-*``）。

    端点统一从 ``settings.assistant_*`` 读取，与主 VLM 配置完全独立。
    api_key 留空时回退使用 ``settings.vlm_api_key``（多数场景两者同 key）。
    """

    def __init__(self, *, counter: Optional[TokenCounter] = None) -> None:
        self.counter = counter or TokenCounter()

    # ------------------------------------------------------------------
    # 通用底层：发起一次 Chat Completions 调用
    # ------------------------------------------------------------------
    async def _post(
        self,
        *,
        messages: List[Dict[str, Any]],
        thinking: bool,
        scene: str,
        timeout: float = 60.0,
    ) -> str:
        """发送 messages → 返回 assistant 文本。

        ``scene`` 仅用于 token 统计分桶（counter.record(scene=scene, ...)）。

        thinking 在豆包的体现是 ``payload.thinking.type = enabled/disabled``。
        历史用过 ``reasoning_effort`` 是 OpenAI o1/GPT-5 风格 API，方舟会
        静默吞掉——已废弃，不要回潮。
        """
        settings = get_settings()
        model = settings.assistant_model
        api_key = settings.assistant_api_key or settings.vlm_api_key
        api_url = settings.assistant_api_url
        if not (api_key and api_url and model):
            raise RuntimeError(
                "辅助系统配置缺失，请检查 AI_PHONE_ASSISTANT_API_URL / "
                "AI_PHONE_ASSISTANT_API_KEY / AI_PHONE_ASSISTANT_MODEL"
            )

        thinking_type = "enabled" if thinking else "disabled"
        payload: Dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "top_p": 0,
            "messages": messages,
            "thinking": {"type": thinking_type},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"{scene} 请求失败: status={resp.status_code} body={resp.text[:200]}"
            )
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        content = message.get("content")
        # 文本场景下 content 通常是 str；带图（断言系统）有时模型返回
        # ``[{"type":"text","text":"..."}]`` 列表形式，两种都兼容。
        if isinstance(content, list):
            text = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            ).strip()
        elif isinstance(content, str):
            text = content.strip()
        else:
            text = ""
        if not text:
            raise RuntimeError(f"{scene} Chat Completions 未返回可解析文本")
        self.counter.record(scene, model, data.get("usage"))
        return text

    # ------------------------------------------------------------------
    # ① 起跑线 · 包名匹配（特殊协议：NONE → 空串）
    # ------------------------------------------------------------------
    async def match_package(self, app_name: str, packages: List[str]) -> str:
        """根据应用名从已安装包列表里挑出最佳包名。

        协议约束（与 vlm_loop 现状一致）：
        - 模型只输出"一个完整包名"或字面量 "NONE"
        - 返回 NONE / 空 / 仅空白 → 适配层翻译为空串 ``""``
        - 包名匹配是高频轻任务，固定 thinking=disabled（~1s 完成）
        """
        if not packages:
            raise RuntimeError("无法获取设备应用列表")

        prompt = (
            "任务：从设备已安装的第三方应用包名列表中，找出与用户描述最匹配的一个应用包名。\n\n"
            f"用户描述：{app_name}\n\n"
            "设备已安装的第三方应用包名列表：\n"
            + "\n".join(packages)
            + "\n\n要求：\n"
            "1. 分析用户描述中的应用名称关键词\n"
            "2. 从包名列表中找出最匹配的应用\n"
            "3. 只返回一个完整的包名，不要有任何解释或额外文字\n"
            "4. 如果无法匹配，返回\"NONE\"\n\n"
            "输出格式：仅输出包名"
        )
        target = await self._post(
            messages=[
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            thinking=False,
            scene="包名匹配",
        )
        if target == "NONE":
            return ""
        return target

    # ------------------------------------------------------------------
    # ② / ③ / ⑤ 通用纯文本（通道判定 / 审判 / 子步骤拆解）
    # ------------------------------------------------------------------
    async def chat_text(
        self,
        prompt: str,
        *,
        label: str = "辅助",
        thinking: bool = False,
    ) -> str:
        """通用纯文本 chat 调用，由调用方控制 prompt 与 thinking。

        ``label`` 仅用于 token 统计分桶（"审判" / "结构化分类" / "子步骤拆解"
        三个场景在大盘里各自计数）。
        """
        return await self._post(
            messages=[
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            thinking=thinking,
            scene=label,
        )

    # ------------------------------------------------------------------
    # ④ 断言系统 · finished 终局裁决（带图）
    # ------------------------------------------------------------------
    async def verify_finished(
        self,
        *,
        prompt: str,
        prev_before_bytes: Optional[bytes],
        final_bytes: bytes,
        thinking: bool = True,
    ) -> str:
        """断言系统裁决调用：双图 + 文本，返回模型原始文本。

        与现有 vlm_loop ``_verify_finished_assertion`` 网络部分行为完全一致：
        - 系统消息固定为 "你是严格保守的结果验收裁决器。"
        - prev_before_bytes 为 None 时退化单图
        - 双图均使用 image_url 的 base64 data URL 形式
        - 失败抛 RuntimeError，由调用方翻译成 SKIP（不动主 VLM 决策）
        """
        user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if prev_before_bytes is not None:
            prev_b64 = base64.b64encode(prev_before_bytes).decode("ascii")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{prev_b64}"},
                }
            )
        final_b64 = base64.b64encode(final_bytes).decode("ascii")
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{final_b64}"},
            }
        )
        # 断言系统的 timeout 由调用方通过 asyncio.wait_for 在外层控制（与现
        # 状一致），本层 httpx 给一个相对宽松的硬上限。
        return await self._post(
            messages=[
                {"role": "system", "content": "你是严格保守的结果验收裁决器。"},
                {"role": "user", "content": user_content},
            ],
            thinking=thinking,
            scene="断言系统",
            timeout=120.0,
        )
