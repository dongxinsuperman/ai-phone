"""方舟 Chat 的公共输出预算；不是目标生成长度或请求超时。

max_completion_tokens 包含思考与回答，并取消 max_tokens 的默认回答限制。
https://docs.volcengine.com/docs/ark/chat-api?lang=en
该接口公开取值上限为 65536；不得同时发送 max_tokens。
不用于 Anthropic、OpenAI 或方舟 Responses 协议。
"""

DOUBAO_CHAT_MAX_COMPLETION_TOKENS = 65536
