"""辅助系统协议实现集合。

每家协议一个独立模块，互不 import：
    - ``doubao.py``：方舟 Chat Completions（4 个调用从 vlm_loop 剪切迁出）
    - ``claude.py``：Anthropic Messages API（4 个调用独立实现）
    - ``openai.py``：OpenAI Chat Completions（4 个调用独立实现）

P1 阶段本目录除 ``__init__.py`` 外为空，待 P3-P8 逐步落地。
"""
