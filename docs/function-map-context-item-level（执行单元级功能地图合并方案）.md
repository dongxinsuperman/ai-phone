# 执行单元级 functionMapContext 合并方案

## 1. 背景故事

AI Phone 的批次执行会把一次外部投递拆成多个执行单元。调用方通常会在同一个 submission 里放一组相关 case，例如登录、个人资料、订单、支付状态、消息通知。

这些 case 有两类上下文：

- 公共上下文：整批都需要知道，例如测试账号、登录入口、验证码规则、通用弹窗处理。
- 单 case 上下文：只有某几个 case 需要知道，例如支付状态页入口、订单筛选规则、某个业务术语、某个异常页的处理方式。

现在只有顶层 `functionMapContext`。调用方如果想让支付 case 知道“支付状态页入口在 我的-订单-待支付”，只能把这段也塞进顶层。结果是整批所有执行单元都会看到支付说明，包括登录、个人资料、消息通知这些不需要它的 case。

这会带来两个实际问题：

1. 公共 map 越写越大。为了照顾少数 case，顶层 context 会堆很多二级说明。
2. 执行单元拿到不属于自己的参考。它虽然是只读资料，但会增加无关信息，降低“按需取用”的清晰度。

所以需要把 function map 扩展成两层：

- 批次级：放整批都会用到的公共资料。
- 执行单元级：放当前 raw item 自己需要的补充资料。

这样调用方可以把“大家都要知道的”放顶层，把“这个 case 才需要的”放 item。最终执行时，AI 看到的是刚好够用的合并参考，而不是整批所有业务说明。

简化例子：

```json
{
  "submissionName": "release-smoke",
  "functionMapContext": "公共：登录入口在首页右上角；测试账号 demo/password",
  "items": [
    {
      "caseId": "login",
      "runContent": "登录后确认进入首页",
      "platforms": ["android"]
    },
    {
      "caseId": "pay-status",
      "runContent": "登录后进入支付状态页，确认待支付状态展示正确",
      "platforms": ["android", "ios"],
      "functionMapContext": "支付：支付状态页入口在 我的-订单-待支付"
    }
  ]
}
```

执行效果：

- `login + android` 只看到公共登录说明。
- `pay-status + android` 看到公共登录说明 + 支付页入口说明。
- `pay-status + ios` 也看到公共登录说明 + 支付页入口说明。

这里 `pay-status` 同时选择 `android` 和 `ios`，后端会裂变成两个执行单元。item 级 function map 也跟着裂变，被这两个执行单元共享。当前不做端差异，因为这批 function map 解决的是 case 级补充，不是平台级补充。

## 2. 本方案要交付什么

实现 `POST /api/submissions` / `POST /api/internal/submissions` 支持执行单元级 function map：

1. 请求体允许每条 `items[]` 传 `functionMapContext`。
2. 顶层 `functionMapContext` 继续作为批次级公共参考，覆盖本批所有执行单元。
3. `items[].functionMapContext` 作为当前 raw item 的追加参考，随 raw item 按 `caseId + platform` 裂变。
4. 后端把 item 级 function map 落到 `submission_items.function_map_context`。
5. scheduler 派发执行单元时，合并 `submissions.function_map_context + submission_items.function_map_context`，并写入 `runs.function_map_context`。
6. Agent 下发运行时语义不改，继续通过现有 `function_map_context` / `functionMapContext` 字段收到合并后的文本；但 `StartRunMsg` 的类型声明要补上这两个字段，避免协议文档落后于实际 payload。
7. 取消当前 8000 字产品层限制，或者把限制调到足够大，避免 function map 被旧上限挡住。

验收标准：

- 只传顶层 `functionMapContext`：本批所有 `caseId + platform` 执行单元都拿到顶层文本。
- 只传 `items[].functionMapContext`：只有该 raw item 裂变出的执行单元拿到 item 文本。
- 同时传顶层和 item：最终 Run 拿到“顶层文本 + item 文本”。
- 一条 raw item 选择 `android` / `ios`：两个平台执行单元都拿到同一份 item 文本。
- `Run.function_map_context` 保存的是最终实际注入文本，重试时继续复用该文本。
- 配置为“不限”时，前端和后端都不能继续按 8000 字拦截。

不做：

- 不做端维度 function map，例如 `functionMapContextsByPlatform`。
- 不改 Agent prompt 注入语义。
- 不改轨迹缓存逻辑。
- 不处理模型通用输出预算硬编码；只处理 function map 字段自己的上限硬编码。

## 3. 行为定义

`functionMapContext` 需要从“批次级只读参考”扩展为“两层合并参考”：

- 顶层 `functionMapContext`：批次默认参考，覆盖本批次展开后的所有执行单元。
- `items[].functionMapContext`：当前 raw item 的追加参考，会随 raw item 裂变到每个 `caseId + platform` 执行单元。
- 最终 Run 注入内容：`顶层 functionMapContext + 当前 item functionMapContext`。

这里的“合并”是追加，不是替换、清空或覆盖下面的内容。

## 4. 接口语义

请求体示例：

```json
{
  "submissionName": "release-smoke",
  "functionMapContext": "全局：登录入口在首页右上角，测试账号 demo/password",
  "items": [
    {
      "caseId": "pay-status",
      "caseName": "支付状态检查",
      "runContent": "登录后进入支付状态页，确认待支付状态展示正确",
      "platforms": ["android", "ios"],
      "functionMapContext": "支付：支付状态页入口在 我的-订单-待支付"
    },
    {
      "caseId": "profile",
      "runContent": "登录后进入个人资料页",
      "platforms": ["android"]
    }
  ]
}
```

展开规则：

- `pay-status + android` 注入：
  - 全局：登录入口在首页右上角，测试账号 demo/password
  - 支付：支付状态页入口在 我的-订单-待支付
- `pay-status + ios` 注入同样内容。
- `profile + android` 只注入顶层全局内容。

如果一条 raw item 同时选择 `android` 和 `ios`，item 级 function map 会随 raw item 一起裂变，被两个平台共享。当前不设计端维度 function map，不支持 `androidFunctionMapContext` / `functionMapContextsByPlatform` 这类复杂结构。

## 5. 数据模型

改造前现状：

- `submissions.function_map_context` 已存在，用于保存批次级参考。
- `runs.function_map_context` 已存在，用于保存本次 Run 实际注入给 Agent 的参考。
- `submission_items` 当前没有 `function_map_context` 字段。

本次新增：

```sql
ALTER TABLE public.submission_items
  ADD COLUMN IF NOT EXISTS function_map_context TEXT NULL;
```

迁移文件：

```bash
backend/migrations/function_map_context_item_v1.sql
```

存量 PostgreSQL 部署需要执行：

```bash
psql "$AI_PHONE_DB_URL" -f backend/migrations/function_map_context_item_v1.sql
```

ORM 需要在 `SubmissionItem` 上增加：

```python
function_map_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
```

`Run.function_map_context` 存最终合并后的内容。这样重试时继续复用同一个 Run 字段，不需要重新从 submission/item 再合并。

查询、广播和报告默认不输出 function map 正文。现有 `Run.to_dict()` / `Submission.to_dict()` 只暴露字符数，Agent 日志也只写“已注入 N 字符”。新增 item 级字段后，建议 `SubmissionItem.to_dict()` 最多补充 `function_map_context_chars`，不要默认返回正文，避免把测试账号、业务入口说明通过列表、Kafka、Webhook 或报告扩散出去。

## 6. 合并规则

建议增加一个小函数，集中处理合并和分隔：

```python
def merge_function_map_context(batch_text: str | None, item_text: str | None) -> str:
    parts = [
        (batch_text or "").strip(),
        (item_text or "").strip(),
    ]
    return "\n\n".join(p for p in parts if p)
```

执行流：

1. 准入解析顶层 `functionMapContext`。
2. 准入解析每条 raw item 的 `functionMapContext`。
3. raw item 按 `platforms` 裂变为多个 `SubmissionItem` 时，把 item 级 function map 原样带到每个执行单元。
4. scheduler 派发某条 `SubmissionItem` 时，读取 `submission.function_map_context` 和 `item.function_map_context`。
5. 合并后写入 `Run.function_map_context`。
6. `RunDispatchService` 继续按现有 payload 字段把 `Run.function_map_context` 下发给 Agent。
7. Agent / runner / prompt 注入链路不需要知道两层来源，只消费最终合并文本。

重试链路已经从 `Run.function_map_context` 读取上下文再次 dispatch，因此只要首跑建 Run 时写入的是合并后的最终文本，失败自动重跑不需要再从 submission/item 重新合并。

## 7. 上限策略

改造前代码里有产品层字符上限：

- 配置默认：`function_map_context_max_chars = 8000`
- 配置上界：`le=20000`
- 校验：超过上限直接拒绝，不截断。
- 前端：超过上限时禁用提交。

本需求要求取消 8000 字限制，或者给一个足够大的值，等价于解除。

落地方案：

- 服务端语义改成 `AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS <= 0` 表示不做产品层字符数拒绝。
- 默认值改为 `0`，即默认不限。
- 移除或放宽配置上的 `le=20000`。
- 前端收到 `function_map_context_max_chars <= 0` 时，只展示当前字数，不做超限拦截。
- 仍保留类型校验：字段必须是字符串；空字符串按未传处理。
- 不做自动截断。截断会让执行器看到不完整手册，比显式失败更危险。

注意：这里解除的是 ai-phone 产品层字符拦截，不等于解除模型供应商的上下文窗口。超大 function map 仍会进入首轮 system prompt，可能被 provider 自身上下文上限、请求体大小或网关限制拒绝；这不是当前仓库里的 8000 字硬编码。

保守替代方案：

- 默认改成 `1000000`。
- 配置上界同步放到 `1000000` 或更高。
- 前端继续按上限显示，但实际等价于不限制普通业务输入。

## 8. 硬编码清单与处理项

本功能只处理和 `functionMapContext` 字段接收、校验、展示、落库、合并、代入直接相关的硬编码。模型调用里的通用输出预算不属于这个功能的实现范围，只作为排查结果记录，不作为本方案风险。

### 8.1 必须随本方案一起修改

| 位置 | 谁的哪个功能 | 改造前硬编码 | 影响 | 处理方案 |
|---|---|---:|---|---|
| `backend/ai_phone/config.py` | Server 配置：function map 字符上限默认值 | `function_map_context_max_chars default=8000` | 默认仍按 8000 字拒绝，和“等价解除”冲突 | 默认改为 `0`，表示不限；或改为超大值 |
| `backend/ai_phone/config.py` | Server 配置：function map 字符上限取值范围 | `function_map_context_max_chars ge=1` | 配置无法设置为 `0`，不能表达“不限” | 如果采用 `0=不限`，改成 `ge=0` |
| `backend/ai_phone/config.py` | Server 配置：function map 字符上限取值范围 | `function_map_context_max_chars le=20000` | 即使用 env 放大，也最多 20000，不符合解除限制 | 移除 `le` 或放到足够大 |
| `backend/.env.defaults` | 项目默认运行策略：function map 字符上限 | `AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS=8000` | 即使代码默认改了，本项目运行默认仍会从 `.env.defaults` 覆盖回 8000 | 同步改成 `0` 或超大值 |
| `backend/ai_phone/server/function_map_context.py` | Server 校验：functionMapContext 文本归一化 | `limit <= 0` 直接 `invalid_config` | 服务端收到非空 function map 且配置为 `0` 会报错 | 改成 `max_chars <= 0` 时跳过长度校验 |
| `backend/ai_phone/server/scheduler/service.py` | Submissions 准入：批次级 function map 解析 | `parse_function_map_context` 只解析顶层 | item 级 `functionMapContext` 会继续被忽略 | 新增 item 级解析；raw item 裂变时同步带到每个执行单元 |
| `backend/ai_phone/server/scheduler/service.py` | Submissions 准入：function map 上限取值 | `limit = int(max_chars or settings...)` | `max_chars=0` 会被 `or` 逻辑吞掉，无法稳定表达“不限” | 改成显式 `if max_chars is None` 判断 |
| `backend/ai_phone/server/api/runs.py` | 单 Run API：function map 校验 | 创建单 Run 仍走同一个长度校验 | 单 Run 页面也无法使用不限 function map | 同步使用新的 `0=不限` 校验语义 |
| `backend/ai_phone/shared/protocol.py` | Server → Agent 协议类型声明 | `StartRunMsg` 未声明 `function_map_context` / `functionMapContext` | 运行时已发送但类型/文档不完整，后续改动容易误删字段 | 在 TypedDict 中补充两个可选字段 |
| `web/src/pages/Queue.vue` | Web 队列页：批次投递 function map 字数配置 | public config 默认 `8000` | config 未加载或为 `0` 时会回落成 8000 | `0` 要被识别为“不限”，不能用 `|| 8000` |
| `web/src/pages/Queue.vue` | Web 队列页：批次投递按钮禁用逻辑 | `functionMapContextTooLong` 按固定上限判断 | 后端不限时前端仍可能禁用投递 | 上限 `<=0` 时恒为 false，只显示当前字数 |
| `web/src/pages/Queue.vue` | Web 队列页：item 表单与 payload 构造 | `newFormItem` / `buildRawItem` 没有 item 级字段 | 后端支持后，Web 手工投递仍无法填写 `items[].functionMapContext` | 给每条 item 增加输入、长度提示、预览和 payload 字段 |
| `web/src/pages/DeviceWork.vue` | Web 单设备工作台：单 Run function map 字数配置 | `functionMapContextLimit = ref(8000)` 和多处 `|| 8000` | 单 Run 工作台仍保留 8000 拦截 | 同 Queue 页，支持 `<=0` 不限 |
| `docs/external-api（对外调用清单）.md` | 对外 API 文档：functionMapContext 字段说明 | 文档写“默认 8000” | 调用方会继续按旧限制理解 | 改成“默认不限 / 可配置限制” |
| `README.en.md` / `docs/features（使用功能介绍）.md` / `docs/architecture（架构设计）.md` / `docs/server-brain（Server大脑架构说明）.md` / `docs/agent-brain（分布式Agent大脑架构说明）.md` | README 与架构/功能文档：submission 示例 | 示例只体现顶层 function map 或完全未提 function map | 调用方不知道 item 级写法，文档之间不一致 | 同步补充 item 级示例或指向 external-api |
| `backend/tests/*` | 测试：function map 上限和 submission 解析 | 部分测试断言默认 8000 或超 8000 拒绝 | 改配置后测试会失败 | 改成覆盖 `0=不限`、有限上限拒绝、item 合并后校验 |

### 8.2 排查过但不随本功能处理的硬编码

下面这些是模型调用通用能力的硬编码，不负责 function map 字段的接收、落库、合并或代入。它们不作为本功能风险，只记录“确认过，不在本方案处理”。

| 位置 | 谁的哪个功能 | 已排查硬编码 | 本方案处理 |
|---|---|---:|---|
| `backend/ai_phone/shared/llm/main/claude_cu.py` | Claude 主 VLM 单轮输出预算 | `max_tokens: 8192` | 不改。它是输出预算，不是 function map 字段限制 |
| `backend/ai_phone/shared/llm/assistants/claude.py` | Claude 辅助系统输出预算 | `max_tokens: 8192` | 不改。和 item 级 function map 代入无关 |
| `backend/ai_phone/agent/trajectory_cache/recovery.py` | 轨迹缓存恢复 VLM 输出预算 | `max_tokens: 8192` | 不改。和本功能无关 |
| `backend/ai_phone/agent/trajectory_cache/v3_replay.py` | V3 轨迹缓存 locator 输出预算 | `max_tokens: 8192` | 不改。和本功能无关 |
| `backend/ai_phone/agent/trajectory_cache/ephemeral.py` | 瞬态 UI 判定输出预算 | `max_tokens: 8192` | 不改。和本功能无关 |
| `backend/ai_phone/config.py` | Claude thinking 预算配置 | `vlm_main_thinking_budget default=1024, le=8192` | 不改。不是 function map 字段限制 |
| `backend/ai_phone/config.py` | 主 VLM 会话分段阈值 | `vlm_session_reset_prompt_threshold default=30000` | 不改。它控制长任务会话切段，不是 function map 字符准入 |
| `backend/ai_phone/shared/llm/main/gpt_cu.py` | GPT Responses 历史截断策略 | `truncation: "auto"` | 不改。不是本功能的字段处理逻辑 |
| `backend/ai_phone/shared/vlm.py` | Doubao Responses 主链路请求 | 未发现显式 `max_tokens` | 不改。当前没有仓库侧输出 token 上限字段 |

## 9. 本功能风险与处理

1. item 级 map 没有落库

如果只在准入期内存对象里带 `items[].functionMapContext`，执行单元排队、重启或恢复后会丢。处理方式：新增 `submission_items.function_map_context`，raw item 裂变后每个 `caseId + platform` 行都保存同一份 item map。

2. 合并位置放错

如果只在 dispatch payload 临时拼接，不写入 `Run.function_map_context`，重试、报告和后续排查拿不到本次 Run 的真实注入内容。处理方式：scheduler 派发前合并批次级 + item 级，并把合并结果写入 `Run.function_map_context`。

3. 旧 8000 字限制没有统一拆掉

如果后端配置、后端校验、前端 Queue、前端 DeviceWork 只改了一部分，会出现某个入口仍然拦截。处理方式：所有入口统一支持 `max_chars <= 0` 表示不限。

4. raw item 裂变时漏传 item map

一条 raw item 会按 `platforms` 展开成多条执行单元。如果展开时只给第一条带 item map，其他平台会缺参考。处理方式：构造每个 `ItemDraft` / `SubmissionItem` 时都复制同一份 item map。

5. 迁移没有跑到已有库

新库 `create_all()` 可以建出新列，但已有库不会自动加列。处理方式：新增 SQL migration，并在发布步骤里明确执行。

6. Web 手工投递没有入口

如果只改 API，外部调用方可以传 `items[].functionMapContext`，但 Queue 页临时投递仍只有批次级文本框。处理方式：Queue 页每条 item 增加可选 function map 输入，并在 payload 预览里显示该 item 有补充上下文。

7. 协议类型声明落后于运行时

`RunDispatchService` 运行时已经下发 `function_map_context` / `functionMapContext`，Agent 也已经读取；但 `StartRunMsg` TypedDict 未声明这两个字段。处理方式：补类型声明，不改变真实 WS payload。

8. 产品层“不限”被误解成模型层“不限”

把 `AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS` 改成不限后，只是不再由 ai-phone 以 8000 字拒绝。模型上下文窗口仍然存在。处理方式：文档里说明“产品层不限”，不承诺绕过 provider context limit。

## 10. 测试建议

后端：

- `parse_and_validate` 支持 `items[].functionMapContext`，并随多平台裂变到每个 `ItemDraft`。
- 非字符串 item function map 拒绝。
- 顶层和 item 级合并后写入 `Run.function_map_context`。
- 重试时继续使用 `Run.function_map_context`，不重新合并也不丢失。
- `AI_PHONE_FUNCTION_MAP_CONTEXT_MAX_CHARS <= 0` 时不做长度拒绝。
- `backend/.env.defaults`、代码默认、公开配置返回值三处对“不限”的默认语义一致。

前端：

- Queue 页 item 表单能填写本条 function map。
- payload 构建包含 `items[].functionMapContext`。
- `function_map_context_max_chars <= 0` 时不禁用提交。
- DeviceWork 单 Run 页同样支持 `function_map_context_max_chars <= 0`，避免后端不限但页面仍拦截。

协议：

- `start_run` 消息里下发的是合并后的 `function_map_context` / `functionMapContext`。
- `StartRunMsg` 类型声明包含上述两个可选字段。

## 11. 对现有工作流的影响结论

当前已有调用方只传顶层 `functionMapContext` 时，行为应保持不变：所有执行单元仍拿到顶层文本，最终仍由 `Run.function_map_context` 下发给 Agent。

新增字段是向后兼容字段。老请求没有 `items[].functionMapContext`，解析结果为空；合并后仍等于原来的顶层文本。

对当前工作流的实际影响主要在发布和入口一致性：

- DB：已有库必须执行新增列 SQL，否则 `SubmissionItem` ORM 新字段会和老表结构不一致。
- Web Queue：不改就无法从页面使用 item 级 function map，但不影响外部 API 调用。
- Web DeviceWork：和 item 级功能无关，但会被“解除 8000 字限制”影响，必须同步改不限逻辑。
- 重试：不需要改重试算法。首跑把合并结果写入 `Run.function_map_context` 后，现有重试已经复用这个字段。
- 报告 / Kafka / Webhook：不需要默认输出正文；保持只暴露字符数或不暴露，避免泄漏参考资料。
- 轨迹缓存：不需要改缓存逻辑。本功能只负责合并代入 function map，不改变 cacheMode、缓存命中、回放或归档规则。

简化场景：

一批有 3 个 case：登录、支付、消息。顶层写“测试账号和登录入口”，支付 item 单独写“支付状态页入口”。系统会生成 4 个执行单元，例如支付同时跑 Android 和 iOS。最后登录只看到账号和登录入口；支付 Android / iOS 都看到账号、登录入口和支付入口；消息只看到账号和登录入口。原来的登录、消息工作流不变，只是支付 case 多拿到自己的补充说明。
