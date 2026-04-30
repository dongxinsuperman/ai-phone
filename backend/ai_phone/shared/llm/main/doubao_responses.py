"""主 VLM · Doubao Responses API 实现（facade）。

历史背景：``shared.vlm.VLMClient`` 是项目最早实装的主 VLM 客户端，对应方
舟 Responses API（``/api/v3/responses``）。该实现已经在生产上跑了 200+
case，行为深度耦合 vlm_loop 的会话分段、token 统计、pending hints 等机
制。我们做多协议适配的"绝对铁律"是：

    **不动 Doubao 实现，只在 llm/ 子包下加一个调度入口。**

因此本文件只是个 facade——把现有 ``VLMClient`` 重命名导出为
``DoubaoResponsesMainVLM``。``shared.llm.create_main_vlm`` 在 backend=
``doubao_responses`` 时绕过这个 facade 直接 import ``VLMClient``，本文
件主要给"想直接 import 单家实现"的人（测试 / 文档示例）用。

后续若要为豆包加协议层优化（Chat fallback / 自适应缓存重置策略），改本
文件 / 改 ``shared.vlm.VLMClient`` 都不会影响 Claude / GPT 实现——三家
互相不 import。
"""
from __future__ import annotations

from ai_phone.shared.vlm import VLMClient as DoubaoResponsesMainVLM

__all__ = ["DoubaoResponsesMainVLM"]
