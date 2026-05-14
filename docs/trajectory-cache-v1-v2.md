# 轨迹缓存 V1 / V2 方案契约

本文档描述 V1 / V2 轨迹缓存的业务语义和执行边界。所有会导致手机真实动作
的逻辑，必须同时遵守 [`executable-logic-contract.md`](./executable-logic-contract.md)。

## 1. 版本边界

V1 和 V2 必须从“决定使用缓存”的那一刻开始隔离。

- V1 表：`public.vlm_trajectory_cache`
- V2 表：`public.vlm_trajectory_cache_v2`
- V3 表：`public.vlm_trajectory_cache_v3`

相同设备、相同 goal 在 V1 / V2 / V3 中可以有相同语义 cache key，但必须落在
各自表里，不能互相覆盖、互相删除、互相回放。

## 2. V1 语义

V1 是固定动作回放。

V1 只做：

1. 命中 V1 表。
2. 读取首次成功轨迹里的固定动作。
3. 按缓存动作顺序执行。
4. 执行结束后做最终断言。

V1 不做：

- 状态路标图片对齐。
- 回放 gate。
- recovery VLM 局部恢复。
- optional_ephemeral 标签判断。
- 动态弹窗清障增强。

V1 的价值是简单、干净、可控。V2 的任何增强能力都不能污染 V1。

## 3. V2 保存

V2 保存首次成功轨迹时，需要保存：

- 可执行动作列表。
- 每个 action 的 `action_id`、动作类型、设备真实坐标、业务意图。
- 每个 action 后用于回放衔接的状态路标图。
- action 到下一步 handoff 的真实时间间隔。
- 最后一个业务 action 到最终断言前稳定状态的 handoff 图和时间间隔。
- 可选清障动作的标记信息。

注意：最后一个业务 action 也必须有可比对的 handoff 依据。它不是“没有下一步”
就可以不存；它的下一步是最终状态断言。

## 4. V2 回放普通 action

普通 action 的回放顺序：

1. 执行缓存 action。
2. 短观察后截图。
3. 与首次成功轨迹中该 action 的 handoff 路标图比对。
4. 如果一致，立刻进入下一条缓存 action。
5. 如果不一致，先按首次成功时的真实 handoff 间隔等待。
6. 等待后再次截图比对。
7. 如果仍不一致，进入 recovery VLM 局部恢复。

不允许把“首次真实间隔”跳过后直接走普通页面稳定检测。页面稳定检测只能是
首次截图或兜底机制，不能取代 V2 的历史 handoff 时间。

## 5. V2 回放 gate

这里的 gate 指回放时遇到 `optional_ephemeral` 清障动作的标签 VLM。

gate 是可操作手机的 VLM。它不是纯消息分类器。它可以输出：

- `SKIP`：当前没有同类弹窗，且页面能衔接下一步，跳过原缓存清障动作。
- `EXECUTE_ORIGINAL`：当前仍有同类弹窗，原缓存动作仍适用，执行原动作。
- `EXECUTE_REPAIR`：当前仍有同类弹窗，但关闭入口变化，输出一个局部修复动作。
- `ESCALATE`：交给 recovery VLM。
- `ASSERT_FAIL`：确认无法安全继续。

gate 的职责边界由 prompt 限制：它只处理当前已标记清障动作，不能自由重跑业务路径。
但这不是能力配置降级的理由。只要它输出会被 runner 执行的动作，它就是可执行 VLM。

## 6. V2 recovery

这里的 recovery 指回放状态路标对不上时的辅助 VLM。

recovery 是可操作手机的 VLM。它可以输出：

- `finished`：当前页面已经满足 handoff 语义，放行继续缓存回放。
- `wait`：当前页面仍在加载或过渡，等待后重新截图。
- 局部动作：`click`、`scroll`、`press_back`、`type` 等，用于恢复当前 action 的 handoff。
- `assert_fail`：确认路径偏航或功能不可达。

recovery 的职责边界由 prompt 限制：它只修当前 action 的局部偏航，不能自由完成整个 case。
但这同样不是能力配置降级的理由。只要它输出会被 runner 执行的动作，它就是可执行 VLM。

## 7. 过渡态 handoff 图

如果首次成功轨迹中某个 action 的 handoff 路标图本身是加载中、进度条、骨架屏、
动画、跳转、刷新、异步请求、动态题目等过渡态，那么严格像素对齐天然不可靠。

此时 recovery 应降级判断：

- 当前页面是否仍在同类加载/过渡中。
- 当前页面是否已经进入比路标图更靠后的可衔接状态。
- 下一条缓存 action 是否可以继续执行。

如果可以衔接，应输出 `finished` 放行，让缓存继续执行下一条 action。尤其当下一条
缓存 action 是 `wait(seconds=N)` 时，recovery 不应再额外输出一个 wait，而应放行
让缓存里的 wait 自己执行。

只有当前页面明显无关、错误、不可衔接，才进入局部修复或失败。

## 8. 海外模型执行能力规则

主 VLM、gate VLM、recovery VLM 都属于可执行 VLM，因为它们的输出都可能导致手机
真实动作。

因此三者必须使用同源执行能力配置：

- 豆包系：主链路和回放链路可以使用 responses / chat 等等价协议，但动作 DSL、
  坐标空间、parser、dispatcher 必须一致。
- Claude / GPT 海外系：如果主链路使用 `claude_cu` / `gpt_cu` Computer Use，
  gate / recovery 也必须复用 Computer Use 级别能力，或使用明确等价的执行适配器。
- 不能因为 gate / recovery 的 prompt 更窄，就把它们降级为普通 `claude_messages`
  或普通 `openai_compatible` 图片问答链路来产出可执行动作。

prompt 只负责约束“它只能处理哪个局部问题”。配置负责保证“它具备与主 VLM 一样的
可执行屏幕定位能力”。二者不能互相替代。

## 9. 当前代码审计结论

截至本文档写入时，V2 代码具备以下事实：

- V1 / V2 已经分表，V1 使用 `vlm_trajectory_cache`，V2 使用
  `vlm_trajectory_cache_v2`。
- V1 replay runner 会关闭 recovery 和 ephemeral gate。
- V2 recovery 当前可以调用 `doubao_responses`、`openai_compatible`、
  `claude_messages`。
- V2 gate 当前可以调用 `doubao_responses`、`openai_compatible`、
  `claude_messages`，默认复用 recovery 连接配置。
- 当主链路是 `claude_cu` / `gpt_cu` 时，V2 recovery / gate 会优先复用主 VLM
  Computer Use 配置，不再用普通 messages/chat 链路产出可执行动作。

这说明：海外 V2 gate / recovery 的代码目标已经对齐“主 VLM 可执行能力同源”。
仍需用真实海外模型跑端到端回放确认坐标准度、耗时和日志可读性。

因此当前状态应判定为：

- 豆包系 V2：可继续按现有能力验证。
- 海外 V2：已接入主 VLM Computer Use 执行级适配；发布前必须用
  `claude_cu` / `gpt_cu` 真机回放验证，不能只看普通 messages/chat 单测。

## 10. 后续改造检查清单

改 V2 海外前必须逐项确认：

- gate / recovery 是否复用主 VLM 的可执行能力配置。
- gate / recovery prompt 是否只缩窄职责，而没有改变执行协议。
- 输出动作是否仍进入统一动作对象。
- 坐标空间是否跟实际执行链路一致。
- absolute 坐标是否使用模型实际看到的截图尺寸缩放回设备坐标。
- runner 是否仍负责日志、限额、截图、重比、断言和失败兜底。
- 普通 assistant/chat 是否只用于不触发手机动作的分析任务。
