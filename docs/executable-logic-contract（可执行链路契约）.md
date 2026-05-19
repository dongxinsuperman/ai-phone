# 主 VLM 首次执行链路契约

本文档定义“会导致手机真实动作”的机器可执行规则。主 VLM 首次执行是基线，
缓存回放里的 gate / recovery / 清障等只要会产出动作并交给 runner 执行，
也必须遵守同一套规则。

它不展开定义缓存保存、缓存回放、V2/V3 方案细节；这些方案应在各自文档中描述。
但后续任何缓存、辅助 VLM、清障、断言、回放逻辑，只要想生成或执行动作，
都必须复用本文档的规则，不能另起一套坐标、动作、解析或执行体系。

核心原则：**主 VLM 首次执行是系统唯一的可执行真源。**

## 1. 首次执行链路

一次主 VLM 执行必须遵循固定闭环：

1. Runtime 获取当前设备截图。
2. Runtime 把截图交给主 VLM。
3. 主 VLM 返回“下一步动作”。
4. Runtime 将模型输出解析为统一动作对象。
5. Runtime 按模型家族规则把模型坐标转换成设备真实坐标。
6. Runtime 调用 driver 执行动作。
7. Runtime 截取动作后的页面状态。
8. Runtime 继续下一轮，直到 `finished` 或 `assert_fail`。

任何可触发设备操作的逻辑，都必须进入这个闭环里的第 4-6 步。不能绕过统一 parser、坐标转换和 driver dispatcher。

## 2. 设备真实坐标

driver 只接受设备真实坐标。

- Android / iOS / Harmony driver 暴露的 `window_size()` 是最终执行坐标系。
- `click(x, y)`、`double_click(x, y)`、`long_press(x, y)`、`swipe(...)` 等 driver 动作都必须使用设备真实坐标。
- 模型坐标不是 driver 坐标，必须先转换。
- 坐标转换只能发生在执行边界，也就是“模型动作即将交给 driver 之前”。

禁止：

- 让业务逻辑直接猜 driver 坐标。
- 在 prompt 里混淆截图坐标和设备坐标。
- 已经转成设备坐标后再次归一化或再次缩放。
- 不带坐标空间标记地传递坐标。

## 3. 模型坐标空间

模型输出坐标只有两种合法空间。

### normalized

`normalized` 表示 0-1000 归一化坐标。

转换规则：

```text
device_x = model_x / 1000 * window_width
device_y = model_y / 1000 * window_height
```

转换后必须 clamp 到屏幕范围内。

### absolute

`absolute` 表示模型实际看到的截图像素坐标。

如果模型看到的截图尺寸与设备 `window_size()` 相同，可以直接使用。

如果模型看到的是压缩截图，必须按比例缩放：

```text
device_x = model_x * window_width / image_width
device_y = model_y * window_height / image_height
```

转换后必须 clamp 到屏幕范围内。

## 4. 主 VLM 家族规则

坐标空间由“实际主 VLM 链路”决定。

### 豆包 Responses

豆包主 VLM 输出文本 DSL。

坐标规则：

- 输出 `normalized`。
- parser 默认按 `normalized` 处理。
- 执行前用 `vlm_point_to_abs` 转成设备真实坐标。

示例：

```text
Action: click(point='<point>500 800</point>')
```

这里的 `500 800` 是 0-1000 归一化坐标。

### Claude Computer Use

Claude Computer Use 输出 tool_use 结构化动作。

坐标规则：

- 输出 `absolute`。
- 坐标相对 Claude 实际看到的截图。
- 如果截图被压缩，执行前必须按截图尺寸缩放回设备 `window_size()`。

禁止把 Claude Computer Use 输出当成 0-1000 归一化坐标。

### GPT Computer Use

GPT Computer Use 输出 computer_call / tool_use 结构化动作。

坐标规则：

- 输出 `absolute`。
- 坐标相对 GPT 实际看到的截图。
- 如果截图被压缩，执行前必须按截图尺寸缩放回设备 `window_size()`。

禁止把 GPT Computer Use 输出当成 0-1000 归一化坐标。

## 5. 统一动作对象

主 VLM 输出必须解析成统一动作对象后才能执行。

当前可执行动作集合：

```text
click
double_tap
long_press
type
scroll
drag
open_app
close_app
press_home
press_back
wait
finished
assert_fail
```

动作约束：

- `click` / `double_tap` / `long_press` 必须带一个点。
- `drag` 必须带起点和终点。
- `scroll` 必须有方向，可选中心点和幅度。
- `type` 只输入文本，不负责找输入框。
- `finished` 和 `assert_fail` 是状态结论，不是设备动作。
- 任何未知动作都不能执行，必须进入异常保护或重新询问。

## 6. Parser 责任

Parser 只做三件事：

1. 识别动作类型。
2. 提取动作参数。
3. 标记坐标空间。

Parser 不能做业务推断。

例如：

- 可以把 `click(point='<point>500 800</point>')` 解析成 click + point。
- 可以把 Claude tool_use 的 left_click 解析成 click + point + `absolute`。
- 不可以把“可能要点底部标签”改写成“点击底部标签”。
- 不可以把模型未输出的动作补出来执行。

## 7. 执行边界

所有设备动作必须通过统一 dispatcher 进入 driver。

执行前必须完成：

- 动作类型已知。
- 坐标空间已知。
- 坐标已经转换到设备真实坐标。
- 坐标已 clamp。
- 当前设备锁仍然有效。

执行后必须记录：

- 执行动作类型。
- 最终设备坐标。
- 执行耗时。
- 执行前截图。
- 执行后截图。

## 8. 输入动作

`type` 是文本输入动作，不是点击动作。

如果输入框未聚焦，主 VLM 应先输出点击输入框动作。Runtime 执行点击后，下一轮或同一合规链路再执行 `type`。

禁止：

- 把 `type(content='xxx')` 隐式改成“先点某处再输入”。
- 手动拆成键盘逐字点击，除非模型明确要求且 parser 支持。
- 在输入框未聚焦时直接注入文本并假装成功。

## 9. 多动作链

默认每轮只执行一个动作。

只有明确允许的场景可以一轮执行多个动作，例如瞬态 UI 的“唤起 + 立即点击”。

多动作链必须满足：

- 每个动作都能独立解析。
- 每个坐标都按各自坐标空间转换。
- 中间不依赖额外 VLM 推理。
- 日志中必须展示每个动作。

禁止用多动作链绕过卡死检测或跳过业务步骤。

## 10. 截图尺寸

主 VLM 看到的截图尺寸是坐标转换的一部分。

要求：

- 调用主 VLM 前记录本轮截图尺寸。
- 如果模型家族输出 `absolute`，执行前必须使用这张截图尺寸做缩放。
- 如果无法识别截图尺寸，不能退化成 `normalized` 逻辑。
- 截图尺寸缺失时，应保守失败或明确降级，不要静默执行可疑坐标。

## 11. 状态动作

`finished` 和 `assert_fail` 不进入 driver。

`finished` 表示模型认为任务完成，但 Runtime 仍可通过断言系统复核。

`assert_fail` 表示模型认为任务无法继续或结果不满足。

状态动作必须记录原因，不能只写空结论。

## 12. 日志规则

首次执行链路日志必须让人能还原：

- 模型看到的是哪一帧截图。
- 模型输出了什么动作。
- parser 得到的动作类型是什么。
- 坐标空间是什么。
- 转换后的设备坐标是什么。
- driver 实际执行了什么。
- 执行后页面是否变化。

日志不能只写内部名词。业务侧应该能看懂“模型想点谁，最终点到哪里”。

## 13. 新增模型接入规则

新增主 VLM 后端前，必须先回答：

- 它输出文本 DSL 还是结构化 tool_use？
- 它输出 `normalized` 还是 `absolute`？
- 如果是 `absolute`，截图是否会被压缩？
- parser 如何标记坐标空间？
- dispatcher 如何转换坐标？
- 是否支持 `finished` 和 `assert_fail`？
- token / 超时 / 重试是否影响动作执行闭环？

未回答清楚之前，不能接入执行链路。

## 14. 可执行 VLM 同源要求

所有会让手机发生真实动作的 VLM，都按“可执行 VLM”管理。

当前属于可执行 VLM 的链路包括：

- 主 VLM：首次完整执行用户目标，CU 家族走 agent loop。
- 回放 gate VLM（标签 vlm）：回放遇到已标记清障动作时，决定跳过、执行原动作或输出修复动作。
- 回放 recovery VLM（辅助 vlm）：回放偏航时，决定等待、放行、失败或输出局部修复动作。
- V3 plan locator（定位 vlm）：基于当前截图和 plan_intent 重新定位坐标。
- V3 rescue verifier（V3 救场 vlm）：V3 定位多次未命中后做轻量救场裁决。

### 14.1 同模型，不必同协议

“能力同源”指**用同一把模型 / 同一把 key 看图**，不是“用同一种调用协议”。

可执行 vlm 必须满足：

- 与主 VLM 是**同一个模型**（同一把 key、同一个 endpoint 主体）。
- 看图能力、坐标定位能力一致。
- 输出的坐标必须能被主 VLM 链路的 parser / dispatcher 接收、转换、执行。

可执行 vlm **不**必须满足：

- “主 VLM 用什么协议，辅 vlm 也必须用什么协议”。
  - 主 VLM 之所以挂 CU 工具走 agent loop，是因为它要在多轮里持续决策、
    持续接收 tool_result。
  - 辅 vlm 是“一次性裁决”：给一张图，吐一个 verdict 或一个修复动作，runner
    立刻执行后再决定要不要再问。这种场景不需要 agent loop，**走 chat 单次
    协议反而更稳**——CU 在被当成单次调用时容易回退到 thinking + 自然语言
    敷衍，verdict 拿不到，反而把缓存回放卡死。

### 14.2 海外辅 vlm 协议翻译规则（claude_cu / gpt_cu 主链路）

主链路是 `claude_cu` / `gpt_cu` 时，可执行辅 vlm 一律走 **chat 单次协议**：

| 主 VLM backend | 辅 vlm backend | api_url | api_key / model |
|---|---|---|---|
| `claude_cu` | `claude_messages` | 主 vlm url 复用 | 主 vlm key / model 复用 |
| `gpt_cu` | `openai_compatible` | 主 vlm `/v1/responses` 后缀替换为 `/v1/chat/completions` | 主 vlm key / model 复用 |

实现细节：

- claude_cu 翻译：`/v1/messages` 端点本身既能跑 CU（带 `anthropic-beta` +
  computer 工具）也能跑普通 chat（不带）。url 复用即可，但**禁止**打 CU
  beta header、**禁止**挂 computer 工具——否则又退回 agent loop。
- gpt_cu 翻译：主链路用 `/v1/responses` + `computer_use_preview` 工具；chat
  通道必须用 `/v1/chat/completions`，自部署代理保持原 host / path 前缀。
- 协议翻译统一封装在 `backend/ai_phone/server/trajectory_cache/_overseas_chat.py`
  的 `overseas_cu_to_chat_config` 中，所有可执行辅 vlm（gate / recovery /
  v3 locator / v3 rescue）都从这里走，避免分散实现导致行为漂移。

豆包系不需要翻译——豆包主链路本来就是 chat / responses 文本 DSL，辅 vlm
直接复用同款协议即可。

### 14.3 多步局部恢复能力归 runner，不归 verifier

“辅 vlm 能不能处理多步操作”是常见误解。能，但**不是辅 vlm 自己持有 agent
loop，而是 runner 持循环 + verifier 单次裁决**：

```text
runner.while True:
    decision = verifier.decide(<最新截图>)
    if CONTINUE        -> 接受当前帧，跳出
    if ASSERT_FAIL     -> 终止
    if REPAIR_ACTION   -> runner 执行 decision.repair_action
                          重新截图 -> 重比对齐
                          对齐则跳出，否则 continue（再问 verifier）
    if WAIT_MORE       -> sleep -> 重截图重比 -> continue
```

- recovery 多步连续修复上限：`recovery_max_repair_actions`（默认 5）。
- WAIT_MORE 上限：`trajectory_cache_recovery_vlm_max_wait_more`（默认 1）。
- 单次 cache 回放期间 recovery 调用上限：`recovery_max_calls_per_replay`。
- V3 rescue / locator 走类似的 `V3ReplayRunner` 内循环，上限独立配置。

也就是说：

- 协议层面：辅 vlm 走 chat 单次协议。
- 调度层面：runner 在外层循环，反复喂最新截图给辅 vlm，把多步操作组合起来。

这套循环模型在豆包系上一直 work，海外切到 chat 协议后行为完全一致。

### 14.4 协议层不变项

不论辅 vlm 走 CU 还是 chat，以下统一动作执行边界仍然成立：

- 动作集合统一（见第 5 节）。
- parser 输出统一动作对象（见第 6 节）。
- 坐标空间标记 + 一次性转换（见第 2-4、7、10 节）。
- driver dispatcher 唯一入口（见第 7 节）。
- 日志最低要求（见第 12 节）。
- 普通 assistant / chat / messages 模型只能用于**不触发手机动作**的分析、
  分类、文案；一旦输出会被 dispatcher 执行，就必须按可执行 VLM 管理（同模
  型、同看图能力、同坐标空间、同执行边界）。

prompt / 触发时机 / 上下文可以按角色裁剪：

- 主 VLM prompt 面向完整目标执行。
- gate prompt 只允许处理当前已标记清障动作。
- recovery prompt 只允许处理当前 action 的局部偏航。
- v3 locator prompt 只允许回坐标，不允许决定动作类型。

prompt 负责缩小行为边界，配置 / 协议负责保证可执行能力。二者不能互相替代。

## 15. 修改前检查清单

任何改动只要可能触发设备动作，都必须检查：

- 动作是不是统一动作集合里的动作？
- parser 是否明确坐标空间？
- 模型截图尺寸是否已记录？
- 坐标是否只在执行边界转换一次？
- 最终传给 driver 的是否是设备真实坐标？
- `type` 是否只负责输入文本？
- `finished` / `assert_fail` 是否没有进 driver？
- 日志是否能还原模型意图和实际执行位置？
- 新增模型是否有坐标空间测试？
- 新增动作是否有 parser、dispatcher、执行测试？

新增辅 vlm（gate / recovery / v3 locator / v3 rescue 等会输出可执行动作的辅
助 vlm）还要额外检查：

- 主链路是 CU 时，是否走的是 `_overseas_chat.overseas_cu_to_chat_config`
  翻译后的 chat 单次协议？没有任何辅 vlm 应该再直接挂 CU agent loop。
- 是否复用主 VLM 的 model / api_key / api_url？
- 是否有"调用失败 / 解析失败"的兜底裁决？最少要有一次保护性 verdict（如
  ephemeral 兜 EXECUTE_ORIGINAL，recovery 兜 WAIT_MORE），不能因为一次空响
  应直接 ASSERT_FAIL 把 cache 回放卡死。
- 多步操作是否依赖 runner 外层循环（while + 重比 + continue），而不是让辅
  vlm 自己尝试拿 agent loop？
- 单次 cache 回放期间是否有调用上限（如 `max_repair_actions`、
  `recovery_max_calls_per_replay`），避免循环失控？

如果任何一项不明确，不允许合并。
