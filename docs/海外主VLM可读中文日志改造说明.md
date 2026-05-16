# 海外主 VLM 可读中文日志改造说明

本文档记录一个后续可做的小改造：让 Claude / GPT Computer Use 首次执行链路在
需要时输出更易读的中文日志，同时不改变任何可执行协议。

设计取舍：**默认保持海外 CU prompt 英文不变**，避免开源场景下对非中文用户不友好；
通过一个布尔 env 开关按需启用中文可读 prompt 片段。也就是说，中文化是本地 / 私有
调试增强，不是默认行为。

## 1. 背景

当前豆包主链路的 prompt 主要是中文，所以日志里的“思考”“完成原因”“失败原因”
天然更容易阅读。

海外主链路不同：

- `backend/ai_phone/shared/llm/prompts/claude_cu.py` 的 system prompt 是英文。
- `backend/ai_phone/shared/llm/prompts/gpt_cu.py` 的 system prompt 是英文。
- `backend/ai_phone/agent/runner/vlm_loop.py` 里的“思考”日志基本是原样记录模型输出，
  没有翻译层。

因此 Claude / GPT 首次执行时，即使用户目标是中文，模型也容易用英文输出
reasoning、forced verdict、FINISHED / ASSERT_FAIL 的说明文字。

这不是 Computer Use 必须用英文，而是当前 prompt 模板选择了英文。

## 2. 改造目标

目标只改“人读起来舒服”的部分，并且必须走 env 开关：

- 首次执行日志里的“思考”尽量使用简体中文。
- `FINISHED:` 后面的完成说明尽量使用简体中文。
- `ASSERT_FAIL:` 后面的失败说明尽量使用简体中文。
- 子步骤强制判定句尽量使用简体中文。
- env 关闭时，Claude / GPT CU prompt 输出与当前版本保持等价。

不改机器协议：

- 不翻译 `computer` tool。
- 不翻译 tool action 名称。
- 不翻译 `FINISHED:` / `ASSERT_FAIL:` / `PLATFORM_ACTION:` 关键字。
- 不翻译结构化字段名。
- 不翻译屏幕上真实出现的按钮、Tab、商品、App、页面名等 UI 文案。

一句话：**协议词保持英文，解释文字改中文，屏幕文案按原样引用。**

## 3. 依据

官方文档并没有要求 prompt 必须使用英文。

- Anthropic 多语言文档说明 Claude 支持多语言任务，并建议明确指定输入 / 输出语言来提高可靠性。
  参考：<https://platform.claude.com/docs/en/build-with-claude/multilingual-support>
- OpenAI Computer Use 文档说明 Computer Use 的核心是“截图 -> 模型返回 UI actions -> harness 执行 -> 再回传截图”的循环，
  输入是自然语言任务，不要求必须英文。
  参考：<https://developers.openai.com/api/docs/guides/tools-computer-use>

所以这里可以把“可读说明语言”设成中文，但必须保护 Computer Use 的结构化动作协议。

## 4. 推荐开关

新增配置：

```dotenv
AI_PHONE_VLM_CU_ZH_PROMPT_ENABLED=false
```

建议字段名：

```python
vlm_cu_zh_prompt_enabled: bool = Field(
    default=False,
    description=(
        "Claude/GPT Computer Use 主 VLM 是否启用中文可读日志 prompt。"
        "默认关闭，保持海外开源默认英文体验。env: AI_PHONE_VLM_CU_ZH_PROMPT_ENABLED"
    ),
)
```

语义：

- `false`：默认值。Claude / GPT CU prompt 保持英文，不影响开源用户。
- `true`：只给 Claude / GPT CU 主 prompt 注入中文可读语言策略，并把 forced verdict
  的自然语言模板切成中文。
- 豆包主链路不读取这个开关，因为豆包 prompt 本来就是中文。
- 辅助 VLM / 缓存回放 / 断言 prompt 暂不读取这个开关，避免一次性扩大影响面。

## 5. 改造范围

### 5.1 必改

`backend/ai_phone/shared/llm/prompts/claude_cu.py`

改动点：

- 保留当前英文 prompt 为默认路径。
- 增加一个参数，例如 `zh_readable: bool = False`。
- 仅当 `zh_readable=True` 时，在主 system prompt 靠前位置增加“人类可读语言策略”。
- 仅当 `zh_readable=True` 时，把 forced verdict line 的自然语言模板改成中文表达。
- `zh_readable=True` 时，`[SATISFIED / NOT SATISFIED]` 可以改成 `[已满足 / 未满足]`：
  全代码搜索
  确认这两个枚举词**只出现在 prompt 文本里**，没有任何 runner / supervisor
  代码侧解析，纯粹是给模型自己 + 人类读日志看的判读结论。豆包 prompt
  (`backend/ai_phone/shared/prompt.py`) 早就用的是 `[已满足 / 未满足]`，跑了
  很久没炸，证明这个枚举词中文化零风险。
- 协议词的冒号已支持半/全角混用：`claude_cu.py` 里 `_FINISHED_RE` /
  `_ASSERT_FAIL_RE` / `_PLATFORM_ACTION_RE` 全部用 `[:：]` 同时匹配，
  所以模型写 `FINISHED：已成功进入习题页` 也能被识别。Prompt 里可以明说
  这一点，模型才敢自然地用中文冒号，而不是夹生的 `FINISHED: 已完成`。

建议的人类可读语言策略片段（仅 env=true 时注入）：

```text
人类可读语言策略：
- reasoning、说明、完成原因、失败原因使用简体中文。
- 协议关键字、tool 名称、action 名称、字段名保持英文（关键字后的冒号
  支持半/全角，写 `FINISHED:` 或 `FINISHED：` 都可以）。
- 屏幕真实 UI 文案按原样引用，不要翻译。
```

`backend/ai_phone/shared/llm/prompts/gpt_cu.py`

改动点与 Claude 相同。

`backend/ai_phone/shared/llm/prompts/__init__.py`

当前这是主 prompt 的分派入口：

```python
build_system_prompt_for_backend(goal, substeps_text=None, backend=None)
```

建议给工厂函数增加关键字参数：

```python
build_system_prompt_for_backend(
    goal,
    substeps_text=None,
    backend=None,
    zh_readable=False,
)
```

分派规则：

- `backend == "claude_cu"`：把 `zh_readable` 传给 `claude_cu.build_system_prompt(...)`。
- `backend == "gpt_cu"`：把 `zh_readable` 传给 `gpt_cu.build_system_prompt(...)`。
- 其他 backend 忽略该参数，继续走豆包原逻辑。

`backend/ai_phone/agent/runner/vlm_loop.py`

当前这里有两个主 prompt 构建入口：

- 初始化主 VLM 客户端时构建 system prompt。
- 结构化子步骤变化 / 重新构建 prompt 时再次构建 system prompt。

这两处都应从 `self._settings.vlm_cu_zh_prompt_enabled` 读取开关，再传给
`build_system_prompt_for_backend(...)`。不要在 prompt 模板内部直接 import
`settings`，保持模板函数纯净、方便单测。

### 5.2 可选

`backend/ai_phone/shared/llm/assistants/claude.py`

涉及：

- package matching assistant 的 system prompt。
- 断言 assistant 的 system prompt。

这些不是首次执行主链路的主要英文来源。package matching 通常不直接展示给用户；
断言 prompt 的调用侧多数已经给中文内容。因此可以后做。

`backend/ai_phone/shared/llm/assistants/openai.py`

同上。

## 6. 不需要改的地方

这些链路当前已经偏中文，不是本次主要问题：

- V3 `plan_intent` cleaner：`backend/ai_phone/server/trajectory_cache/v3_service.py`
- V3 locator prompt：`backend/ai_phone/server/trajectory_cache/v3_replay.py`
- V2 / V3 recovery prompt：`backend/ai_phone/server/trajectory_cache/recovery.py`
- ephemeral classifier / gate prompt：`backend/ai_phone/server/trajectory_cache/ephemeral.py`

**顺带收益（值得提一句）**：V3 plan_intent cleaner 在
`v3_service.py` 里专门维护了一条「英文 thought 噪声」正则
（`let me analyze | current screenshot | i can see | appears to | forced verdict | ...`），
用来识别"模型把英文 reasoning 误吐进 plan_intent"的污染。一旦海外 CU prompt
改中文，这条正则的命中率会自然下降——首跑 thought 中文 → cache 收集到的
原文也是中文 → cleaner 不需要二次救火。所以本改造对 V3 缓存质量是双向正向，
不仅"日志好读"，也"缓存更稳"。

也不建议加"展示层翻译"。展示层翻译会把真实模型输出和日志证据切开，
后续排查 Computer Use 行为时反而更难追责。

## 7. 推荐 prompt 片段

建议在 `AI_PHONE_VLM_CU_ZH_PROMPT_ENABLED=true` 时加入 Claude / GPT CU 主 prompt
的靠前位置，放在任务和动作规则之前：

```text
## Human-readable Language Policy

Use Simplified Chinese for all human-readable reasoning, explanations,
status summaries, FINISHED reasons, and ASSERT_FAIL reasons.

Keep protocol keywords, tool names, action names, and field names exactly as
specified in English: `computer`, `FINISHED`, `ASSERT_FAIL`, `PLATFORM_ACTION`,
and action names. The colon after these keywords accepts both half-width `:`
and full-width `：` — pick whichever reads naturally in context (e.g. write
`FINISHED：已成功进入习题页` when the reason is in Chinese).

When referring to visible UI text, quote it exactly as shown on screen. Do not
translate button names, tab names, app names, product names, or page titles.
```

env=true 时，forced verdict 模板可以直接整体中文化，与豆包 prompt 一致：

```text
"子步骤 N「<原始片段>」→ 目标状态：<把动作转成状态>。
当前截图：[已满足 / 未满足]，依据：<具体视觉证据>。"
```

`[已满足 / 未满足]` 不会破坏任何机器协议——这两个枚举词 runner / supervisor
代码侧根本不解析，只给模型自己分支判断和人类读日志用，豆包 prompt 早就这么写。

env=false 时必须保留当前英文模板，避免默认行为变化。

## 8. 风险

主要风险不是模型不会中文，而是误翻译协议。

风险点：

- 把 `FINISHED:` 翻译成“已完成：”，runner 可能无法识别终态。
- 把 `ASSERT_FAIL:` 翻译成“断言失败：”，runner 可能无法识别失败终态。
- 把 `PLATFORM_ACTION:` 或 `open_app(...)` 改成中文，平台动作解析会失效。
- 把屏幕 UI 文案翻译了，后续 VLM / 日志对照会变差。

所以改造必须坚持：协议不翻译，UI 文案不翻译，只翻译解释。

额外注意：

- 不要把这个开关写进轨迹缓存 key。轨迹缓存关注动作语义和截图状态，不应因为
  “日志语言”变化导致旧缓存全失效。
- 如果 Claude prompt caching 开启，system prompt 会因为 env=true/false 不同而
  分成两份缓存，这是正常现象；默认 false 不影响开源用户的缓存表现。
- 不要在展示层翻译模型输出。展示层翻译会把真实模型输出和日志证据切开，
  后续排查 Computer Use 行为时反而更难追责。

## 9. 验收标准

最小验收：

- env=false 时，Claude / GPT CU prompt 与当前默认英文行为保持等价。
- env=true 时，Claude CU 首次执行“思考”日志主体为中文。
- env=true 时，GPT CU 首次执行“思考”日志主体为中文。
- `FINISHED:` / `ASSERT_FAIL:` 仍能被 runner 正确解析（半角或全角冒号都行）。
- `PLATFORM_ACTION:` 仍能被 runner 正确解析。
- click / type / scroll / wait 等 Computer Use 动作仍能执行。

**最直观的肉眼验收**：跑一个中文 goal 的 case，看 `vlm_loop.py` 输出的
`#N 思考 — ...` 那一行是不是中文。如果还是大段英文 reasoning，说明 prompt
策略段位置不够靠前，或者被后续英文规则段稀释了——把策略段往最顶上挪一挪。

业务验收：

- 中文 goal 场景下，日志不再大段输出英文推理。
- V3 保存的 `plan_intent` 不因英文 thought 变差。
- 遇到英文 UI 时，日志可以中文解释，但 UI 文案保持英文原样引用。

## 10. 建议实施顺序

1. 先加 `Settings.vlm_cu_zh_prompt_enabled`，默认 `False`。
2. 给 `build_system_prompt_for_backend(...)` 加 `zh_readable` 参数，默认 `False`。
3. 只接入 `claude_cu.py`，分别跑 env=false / env=true 的 Claude 简单 case。
4. 确认终态关键字和平台动作仍可解析。
5. 再接入 `gpt_cu.py`，分别跑 env=false / env=true 的 GPT 简单 case。
6. 如断言日志仍有明显英文，再评估 assistant prompt 是否需要补中文语言策略。

不要一次性改所有 prompt，避免把主链路、辅助链路、缓存链路的问题混在一起。
