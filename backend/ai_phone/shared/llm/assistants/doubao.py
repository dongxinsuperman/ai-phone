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
import time
from typing import Any, Dict, List, Optional

import httpx

from ai_phone.config import get_settings
from ai_phone.shared.llm.base import AnalysisResult, TokenCounter

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
    async def match_package(
        self,
        app_name: str,
        packages: List[str],
        *,
        function_map_context: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> str:
        """根据应用名从已安装包列表里挑出最佳包名。

        协议约束（与 vlm_loop 现状一致）：
        - 模型只输出"一个完整包名"或字面量 "NONE"
        - 返回 NONE / 空 / 仅空白 → 适配层翻译为空串 ``""``
        - 开启 thinking：让模型可靠地"先从自然语言里识别目标 App、再模糊匹配包名"。
          实测关思考时对带"打开/app"等噪声的自然语言 query 会不稳（偶发 NONE），
          开一点点思考即稳定；这层"先识别再匹配"必须由模型做，本地剥壳无法通用。

        ``function_map_context`` / ``platform`` 是可选软参考：map 里有精准包名就
        优先用，没有就退回按 ``app_name`` 模糊匹配；绝不从 map 硬联想。
        """
        if not packages:
            raise RuntimeError("无法获取设备应用列表")

        platform_line = f"当前设备平台：{platform}\n\n" if platform else ""
        map_block = (
            "\n\nApp Map（只读软参考，可能为空）：\n" + function_map_context
            if function_map_context
            else ""
        )
        prompt = (
            "任务：判断用户想要启动/重启的目标应用，并从设备已安装应用包名列表中"
            "找出对应的一个包名。\n\n"
            + platform_line
            + f"用户描述（可能是一句话，也可能是一段前置条件原文）：{app_name}\n\n"
            "设备已安装的应用包名列表：\n"
            + "\n".join(packages)
            + map_block
            + "\n\n规则：\n"
            "1. 先从用户描述里理解要启动的目标应用是哪一个。描述可能夹带"
            "“打开/关闭/重新打开/杀掉/启动/进入”等动作词，以及“app/应用”等泛称，"
            "请忽略这些外壳，只聚焦真正的应用名（如“打开某应用App”的目标就是“某应用”本身）。\n"
            "2. App Map 只是可选参考：若其中明确给出该应用在当前平台对应的包名，"
            "优先采用这个更精准的包名。\n"
            "3. 这是**模糊匹配**不是精确字符串匹配：用户只会说中文应用名，不会说包名。"
            "中文应用名常以其**拼音 / 拼音缩写 / 英文名**出现在包名主段里"
            "（例如中文名的拼音就是包名的核心片段）。只要用户描述的应用名与某个包名在"
            "语义或拼音上高度对应，即视为命中——不要因为“字面不完全一致”就拒绝。\n"
            "4. App Map 只是加分项：为空 / 未包含该应用 / 未规定其包名时，不要从 map "
            "硬读硬联想，直接按上面的模糊匹配在“已安装应用包名列表”里找即可。\n"
            "5. 只有当用户描述根本没有指向任何具体应用（如仅说“重新打开APP”而无 App 名），"
            "或“已安装应用包名列表”里确实没有任何语义/拼音相近的包名时，才返回\"NONE\"；"
            "不要轻易返回 NONE，也不要随便挑一个不相关的包。\n"
            "6. 只返回一个完整包名，或\"NONE\"，不要任何解释或额外文字。\n\n"
            "输出格式：仅输出包名或 NONE"
        )
        target = await self._post(
            messages=[
                {"role": "user", "content": [{"type": "text", "text": prompt}]}
            ],
            thinking=True,
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

    # ------------------------------------------------------------------
    # ⑥ 大盘 AI 分析（高级文本：system + user + 透出 usage / 耗时）
    # ------------------------------------------------------------------
    async def analyze_text(
        self,
        *,
        system: str,
        user: str,
        label: str = "AI 分析",
        thinking: bool = False,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> AnalysisResult:
        """大盘 AI 分析专用：system + user 两条消息，返回 ``AnalysisResult``。

        与 ``_post`` 高度相似但**故意不复用**——前端要展示这一次调用的 token /
        耗时，需要在调用现场拿到原始 usage；硬塞给 ``_post`` 会污染辅助系统其
        他 4 处轻量 Q&A 的代码路径。约 30 行重复代码换来彻底解耦，符合项目
        "高冗余、低耦合"的设计原则。
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
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "thinking": {"type": thinking_type},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(api_url, json=payload, headers=headers)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            raise RuntimeError(
                f"{label} 请求失败: status={resp.status_code} body={resp.text[:200]}"
            )
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            ).strip()
        elif isinstance(content, str):
            text = content.strip()
        else:
            text = ""
        if not text:
            raise RuntimeError(f"{label} Chat Completions 未返回可解析文本")

        usage = data.get("usage") or {}
        # 同步累加到 TokenCounter，让大盘的"按平台/模型 token 统计"也能反映 AI 分析消耗
        self.counter.record(label, model, usage)
        return AnalysisResult(
            model=model,
            text=text,
            elapsed_ms=elapsed_ms,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
