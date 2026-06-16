# ENV Migration Guide

This guide is for users upgrading from the old model ENV layout to the new
`.env.defaults` + `PHONE_VLM/AUX` layout.

## What changed

Runtime now loads config in this order:

```text
code defaults < backend/.env.defaults < backend/.env < backend/.env.local < process environment
```

`backend/.env.defaults` is committed and carries project defaults. Your local
`backend/.env` is ignored by git and should only contain real deployment values,
secrets, local machine paths, and the model choices you intentionally operate.

The public model entry is now only two blocks:

```text
AI_PHONE_PHONE_VLM_*   phone-touching main visual model
AI_PHONE_AUX_*         non-phone auxiliary model
```

Do not keep using old user-facing model entries such as `AI_PHONE_VLM_*` or
`AI_PHONE_ASSISTANT_*` as the source of truth. The server derives the internal
execution fields from `PHONE_VLM/AUX` and sends them to agents.

## New install

```bash
cd backend
cp .env.example .env
```

Then fill the required sections in `.env`:

```text
1. Server basics: AI_PHONE_DB_URL, AI_PHONE_AGENT_TOKEN
2. Model config: AI_PHONE_PHONE_VLM_* and AI_PHONE_AUX_*
3. Agent connection: AI_PHONE_SERVER_WS_URL, AI_PHONE_SERVER_HTTP_BASE
4. Local device values: WDA / local paths only if this machine needs them
```

Most tuning switches now live in `.env.defaults`. Copy a default into `.env`
only when you intentionally want to override it for this deployment.

## Existing install after git pull

Your local `backend/.env` will not be changed by `git pull`, because it is
ignored by git. After pulling this version, update the existing `.env` manually:

```bash
cd backend
cp .env .env.backup
```

Add these eight required model lines:

```env
AI_PHONE_PHONE_VLM_PROVIDER=<doubao|claude|openai>
AI_PHONE_PHONE_VLM_MODEL=<phone-vlm-model>
AI_PHONE_PHONE_VLM_API_KEY=<phone-vlm-api-key>
AI_PHONE_PHONE_VLM_BASE_URL=<phone-vlm-base-url>

AI_PHONE_AUX_PROVIDER=<doubao|claude|openai>
AI_PHONE_AUX_MODEL=<aux-model>
AI_PHONE_AUX_API_KEY=<aux-api-key>
AI_PHONE_AUX_BASE_URL=<aux-base-url>
```

Then remove old model connection lines from `.env` if they exist, so the file
does not mislead future debugging:

```text
AI_PHONE_VLM_BACKEND
AI_PHONE_VLM_API_URL
AI_PHONE_VLM_API_KEY
AI_PHONE_VLM_MODEL
AI_PHONE_ASSISTANT_BACKEND
AI_PHONE_ASSISTANT_API_URL
AI_PHONE_ASSISTANT_API_KEY
AI_PHONE_ASSISTANT_MODEL
AI_PHONE_TRAJECTORY_CACHE_*_VLM_API_URL
AI_PHONE_TRAJECTORY_CACHE_*_VLM_API_KEY
AI_PHONE_TRAJECTORY_CACHE_*_VLM_MODEL
```

Those internal fields are now derived by the server. Agents should normally keep
only server connection, token, device paths, and local physical-machine values.

## Model examples

Doubao:

```env
AI_PHONE_PHONE_VLM_PROVIDER=doubao
AI_PHONE_PHONE_VLM_MODEL=doubao-seed-1-6-vision-250815
AI_PHONE_PHONE_VLM_API_KEY=<volcengine-ark-api-key>
AI_PHONE_PHONE_VLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3

AI_PHONE_AUX_PROVIDER=doubao
AI_PHONE_AUX_MODEL=doubao-seed-1-6-250615
AI_PHONE_AUX_API_KEY=<volcengine-ark-api-key>
AI_PHONE_AUX_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
```

Claude:

```env
AI_PHONE_PHONE_VLM_PROVIDER=claude
AI_PHONE_PHONE_VLM_MODEL=claude-sonnet-4-5
AI_PHONE_PHONE_VLM_API_KEY=<anthropic-or-gateway-api-key>
AI_PHONE_PHONE_VLM_BASE_URL=https://api.anthropic.com

AI_PHONE_AUX_PROVIDER=claude
AI_PHONE_AUX_MODEL=claude-haiku-4-5
AI_PHONE_AUX_API_KEY=<anthropic-or-gateway-api-key>
AI_PHONE_AUX_BASE_URL=https://api.anthropic.com
```

If you use an internal gateway, the model name must match that gateway. For
example, a Bedrock-backed gateway may require a model id such as
`global.anthropic.claude-haiku-4-5-20251001-v1:0`.

OpenAI/GPT:

```env
AI_PHONE_PHONE_VLM_PROVIDER=openai
AI_PHONE_PHONE_VLM_MODEL=computer-use-preview
AI_PHONE_PHONE_VLM_API_KEY=<openai-api-key>
AI_PHONE_PHONE_VLM_BASE_URL=https://api.openai.com/v1

AI_PHONE_AUX_PROVIDER=openai
AI_PHONE_AUX_MODEL=gpt-4o-mini
AI_PHONE_AUX_API_KEY=<openai-api-key>
AI_PHONE_AUX_BASE_URL=https://api.openai.com/v1
```

## Quick check

After editing `.env`, run this from `backend`:

```bash
python - <<'PY'
from ai_phone.config import build_downlink_config, get_settings

cfg = build_downlink_config(get_settings())
print("env ok:", cfg["vlm_backend"], cfg["assistant_backend"])
PY
```

Expected examples:

```text
Doubao: env ok: doubao_responses doubao_chat
Claude: env ok: claude_cu claude
OpenAI: env ok: gpt_cu openai
```

## 中文速记

这次不是让用户继续补一堆内部模型变量，而是把填写入口收成两块：

```text
PHONE_VLM = 会碰手机的主视觉模型
AUX       = 不碰手机的辅助判断模型
```

新用户复制 `.env.example` 填。老用户拉代码后，本机 `.env` 不会被 git 自动改，所以
要手动加上 `AI_PHONE_PHONE_VLM_*` 和 `AI_PHONE_AUX_*` 八行，并把旧的
`AI_PHONE_VLM_* / AI_PHONE_ASSISTANT_*` 连接项从 `.env` 里清掉，避免后续排查时看错。

