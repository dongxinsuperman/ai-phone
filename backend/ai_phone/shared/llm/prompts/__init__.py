"""模型专属 Prompt 模板集合。

每家协议有自己的"系统提示词风格"——豆包系喜欢严格的 ``Action: click(...)``
DSL，Claude 推荐 ``<thinking>...</thinking>`` 自然语言 + tool_use，OpenAI
则需要"don't ask permission"等针对 computer_use_preview 的规约。同一份 case
prompt 在三家身上效果差异巨大，所以分家维护：
    - ``doubao.py``：现有 SYSTEM_PROMPT_TEMPLATE / build_user_prompt 的 facade
    - ``claude_cu.py``：Claude Computer Use 专用模板
    - ``gpt_cu.py``：OpenAI computer_use_preview 专用模板

P1 阶段本目录除 ``__init__.py`` 外为空，待 P2/P5/P7 逐步落地。
"""
