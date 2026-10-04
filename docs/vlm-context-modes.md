# 豆包主 VLM 上下文切换

豆包主 VLM 默认使用 Chat 严格完整轮次滑窗，默认 5 轮；可显式切回原 Responses 会话续接。
不修改模型、Prompt 规则、动作协议、辅助系统或轨迹缓存格式。

## Server 配置及下发

未配置时使用随代码提交的默认值；也可在 **Server 的 `backend/.env`** 显式覆盖：

```env
# 默认；Chat 严格完整轮次滑窗 + 隐式缓存
AI_PHONE_VLM_CONTEXT_MODE=sliding_window

# 仅滑窗模式使用，1-64，默认 5
AI_PHONE_VLM_HISTORY_WINDOW_ROUNDS=5
```

切回原方式时只将 `AI_PHONE_VLM_CONTEXT_MODE` 改为 `session`。这两项属于现有
Settings 执行配置下发集；Agent 接收 Server 快照后，通过主客户端工厂选择实现。
不要把 `PHONE_VLM_PROVIDER` 改成 Chat，也不要为了切主请求改变 AUX 配置。

默认值变更会影响没有显式配置这两项的部署；已有 `.env`、`.env.local` 或进程 ENV
覆盖仍优先，不自动改写。需要保留原行为时，升级前在 Server 显式配置 `session`。

部署步骤：在空闲时更新并重启需要新能力的 Agent，更新 Server 并按现有流程
重启/重新下发配置；确认 Agent 已接收目标值，再提交新 Run。既有 Run 在创建时
固定客户端与窗口大小，不因后续配置下发混换历史。

- 旧 Agent 会忽略未知的新字段，继续使用原链路，不会因这两个字段报错；但不会启用滑窗。
- 旧 Server 不下发这两个字段时，新 Agent 使用自身配置，干净默认是 `sliding_window`、5 轮。
  要统一由 Server 控制新开关，应同步更新 Server。
- 回退时 Server 配置改回 `session`，重新下发，对新 Run 生效。
- 新模式仅对豆包主 VLM 生效；Claude、GPT 与它们原有窗口配置不变。

## 严格完整轮次窗口

5 轮 **包含当前请求**：最多保留最近 4 轮用户截图/文字及模型完整响应，加当前
截图/文字。第 6 轮请求只含第 2-6 轮，超过窗口的图片、Thought、Action 和临时提示
一起退出请求；不额外保留首轮、早期文字、完成摘要或全量历史。

完整 Case、System、子步骤清单与原 Map 是固定任务内容，始终提供，不占历史轮次。
子步骤由现有 Runner 注入后使用，不增加规划器或摘要模型。

窗口按成功返回的模型对话轮次计，不按设备 Action 数计。HTTP 网络重试不新增轮次；
解析失败的原始响应则保留给原 Runner 的同图纠正，下一次纠正调用是一轮模型对话，
但不代表那条非法动作已执行。

仅模型请求历史被裁剪。本地完整日志、动作记录、断言历史、报告和 V3 清洗来源仍
沿用原链路；模型可能因信息范围变化改变决策，必须另做真实质量验收。

## Chat 与隐式缓存

新实现使用 PHONE 配置派生的 `/chat/completions`、相同模型及凭证；仍不传 tools，
返回相同 Seed XML，转换为同一 Decision/ParsedAction。保持 `thinking=disabled`，
不额外增加输出上限，不修改动作或业务 Prompt。

不发送 `previous_response_id`、显式 `caching` 或 `store`。使用每 Run 独立且 Run 内
稳定的不透明 `prompt_cache_key` 提高隐式缓存路由亲和；它不是历史 ID。不同设备/
并行 Run 不共享该键，不放手机号、凭证或任务原文。

隐式缓存自动开启，但命中率不保证。只有公共前缀可复用，窗口滚动后重叠的图片
不等于能全部命中。固定 System/Map 放在前部；既有 TokenCounter 从 Chat 的
`usage.prompt_tokens_details.cached_tokens` 采集真实命中量，并沿原汇总/落库/报告/
回调链路上报。无新数据库字段或消息格式。

不要把命中率当作速度保证，也不要仅凭总输入减少认定费用降低。应分别核对命中
输入、未命中输入、输出及真实计费单价。隐式缓存不收缓存存储费。官方路由键
策略为 best effort，同键高频请求可能回退普通路由，不能靠人为限速掩盖这种波动。

官方文档：

- [上下文缓存原理与计费](https://docs.volcengine.com/docs/ark/context-cache?lang=zh)
- [GUI Agent Chat + Seed XML](https://docs.volcengine.com/docs/ark/gui-task-processing?lang=zh)

## 影响边界

`session` 分支原客户端及参数不变；`sliding_window` 客户端不触发原会话硬切阈值。
原 24 万安全阈值仍用于 `session`。HTTP 失败保留既有一次重试语义，错误仍交给原
Runner 处理；不自动换模式或降级为另一种上下文策略。

无需更改 Submission/Case/Map 输入、Driver、首跑断言/审判、V1/V2/V3 定位与救援、
数据库或业务消息协议。首跑完整重跑若由现有 V3 策略触发，会自然使用当次新建
Runner 的主客户端选择；V3 回放内部模型请求本身不受该开关影响。
