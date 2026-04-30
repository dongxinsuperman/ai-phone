"""主 VLM 协议实现集合。

每家协议一个独立模块，互不 import：
    - ``doubao_responses.py``：方舟 Responses API（facade，复用 shared.vlm.VLMClient）
    - ``claude_cu.py``：Anthropic Messages API + computer 工具
    - ``gpt_cu.py``：OpenAI Responses API + computer_use_preview 工具

P1 阶段本目录除 ``__init__.py`` 外为空，待 P2-P7 逐步落地。
"""
