# assistant-systems（辅助系统核心逻辑及效果）

本文描述 ai-phone 主 VLM 执行链路外层的“执行安全层”。它不是另一个执行器，也不是简单的失败重试，而是把一次自然语言真机任务变成有截图证据、有状态对齐、有异常介入、有最终裁决的执行流水线。

代码主要分布在：

- VLM loop：`backend/ai_phone/agent/runner/vlm_loop.py`
- 页面稳定检测：`backend/ai_phone/agent/runner/stability.py`
- 辅助模型适配：`backend/ai_phone/shared/llm/assistants/*`
- 轨迹缓存保存 / 回放：`backend/ai_phone/server/trajectory_cache/*`
- 可执行链路契约：`docs/executable-logic-contract（可执行链路契约）.md`

## 1. 定位

主 VLM 负责看图、理解目标、产出手机动作；辅助系统负责判断“这个执行链路是否还可信”。它关心的不是再找一个模型替主 VLM 操作手机，而是回答几类更工程化的问题：

- 当前截图是否已经稳定，还是还在动画 / 加载 / 黑屏过渡里。
- 主 VLM 是否真的在朝目标推进，还是陷入同坐标点击、同屏重复、滑动震荡。
- 成功轨迹能不能安全复跑，复跑时页面是否仍然对齐。
- 首跑里出现的弹窗 / 浮层是不是非业务瞬态遮挡，后续复跑还该不该点。
- 最后是否真的达成了 goal，而不是模型自称完成。

所以 ai-phone 的核心差异不是“多调几个辅助模型”，而是把 VLM 决策、状态路标、瞬态 UI 处理、异常审判和最终断言串成一条可监督、可回放、可恢复的真机执行链路。

## 2. 能力总览

| 能力 | 作用 | 成本 / 边界 |
|---|---|---|
| 页面稳定检测 | 每步截图前等待画面稳定，减少看过渡帧、黑屏帧、加载帧 | 本地截图对比 |
| 通道判定 / 子步骤约束 | 结构化 case 与自由对话分流；要求主 VLM 先判定子步骤是否已满足再行动 | prompt 协议 + 辅助文本判定 |
| 本地卡死检测 | 捕捉同坐标点击、同屏重复、滑动震荡、滑动无进展、unknown 动作堆积 | 本地规则，不烧 token |
| 审判系统 | 结构化异常或周期抽查触发轻量模型，判断继续、修正或终止 | 辅助模型 token |
| 最终断言 | 用 goal、全步骤上下文、before / after 图判断是否真的完成 | 辅助模型 token |
| 轨迹缓存 V1 / V2 / V3 | 成功 Run 沉淀可复用轨迹；V2 做状态路标对齐，V3 按动作意图重定位 | 只适合起跑状态可控的重复 case |
| 状态路标 / recovery | V2 回放时把当前页面和首跑成功后的页面状态对齐；不对齐时等待、修复或失败 | 图片指标 + 可选辅助 VLM |
| 瞬态 UI 链式动作 | 自动隐藏工具栏 / Toast / 临时控件可在同一 Thought 内连续执行 2 个动作 | 只允许短链路，链内动作仍进卡死检测 |
| 瞬态弹窗标记 / gate | 首跑成功后把非业务弹窗清障动作标成 optional；复跑时按当前截图决定跳过、原动作、修复或失败 | 仅处理非业务遮挡，不替代业务步骤 |

## 3. 执行链路

一次普通 VLM Run 大致会经过这些护栏：

1. 截图前做页面稳定检测。
2. 主 VLM 输出 Thought + Action，结构化任务要求先写子步骤判定。
3. 本地审计记录坐标、屏幕 pHash、滑动方向、unknown 动作等信号。
4. 如果出现同坐标 / 同屏 / 震荡 / 无进展等异常，审判系统介入。
5. 每步保存 before / after 截图、动作、日志，形成报告证据链。
6. Run 结束时最终断言用 goal、步骤上下文、before / after 图做裁决。
7. 成功 Run 在缓存模式下清洗并保存轨迹；后续复跑仍要走状态对齐和最终断言。

这样做的目标是让“VLM 点手机”从一次次不可解释的模型输出，变成一条有证据、有回放、有兜底的执行链路。

## 4. 页面稳定检测

主链路通过 `wait_page_stable_pixel()` 等待连续截图差异低于阈值后再交给 VLM。相关 env：

```env
AI_PHONE_VLM_PAGE_STABLE_ENABLED=true
AI_PHONE_VLM_PAGE_STABLE_TIMEOUT_S=5.0
AI_PHONE_VLM_PAGE_STABLE_POLL_S=0.4
AI_PHONE_VLM_PAGE_STABLE_THRESHOLD=0.04
```

轨迹缓存回放有独立阈值，避免复跑时和首跑调优互相污染：

```env
AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_ENABLED=true
AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_TIMEOUT_S=5.0
AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_POLL_S=0.4
AI_PHONE_TRAJECTORY_CACHE_PAGE_STABLE_THRESHOLD=0.04
```

## 5. 本地卡死检测

卡死检测优先在本地做，只有达到阈值才召唤辅助模型。典型信号：

- 点击落在同一坐标桶太多次。
- 屏幕 pHash 反复回到同一状态。
- 滑动方向来回震荡。
- 滑动后画面差异长期很小。
- VLM 连续产出 unknown 或无效动作。

相关 env 集中在 `.env.example` 的辅助系统章节，例如：

```env
AI_PHONE_AUDIT_CLICK_BUCKET_PX=50
AI_PHONE_AUDIT_CLICK_BUCKET_TRIGGER=20
AI_PHONE_AUDIT_SCREEN_REVISIT_HAMMING=8
AI_PHONE_AUDIT_SCREEN_REVISIT_TRIGGER=10
AI_PHONE_AUDIT_SCROLL_FLIP_WINDOW=10
AI_PHONE_AUDIT_SCROLL_FLIP_TRIGGER=6
AI_PHONE_AUDIT_SCROLL_NOPROGRESS_DIFF=0.02
AI_PHONE_AUDIT_SCROLL_NOPROGRESS_TRIGGER=10
```

这层是成本最低的护栏：多数“模型还在动但其实已经卡住”的问题，先靠本地规则拦住。

## 6. 审判系统

审判系统读取当前目标、最近步骤、截图证据和结构化异常，输出继续、修正或终止的建议。它解决的是“主 VLM 仍在输出动作，但行为已经不再朝目标推进”的问题。

相关 env：

```env
AI_PHONE_AUDIT_TIMEOUT_SEC=30
AI_PHONE_AUDIT_ALLOW_LIMIT=30
AI_PHONE_AUDIT_PERIODIC_INTERVAL=30
AI_PHONE_ASSISTANT_THINKING_JUDGE=true
```

`AI_PHONE_AUDIT_PERIODIC_INTERVAL` 控制主动抽查步频；阈值调大可减少误 kill，但会让异常链路多跑几步。

## 7. 最终断言

最终断言不是只看最后一张图，而是综合：

- 原始 goal / runContent。
- 全步骤摘要。
- 操作前后截图。
- 失败原因、unknown、审判记录。

相关 env：

```env
AI_PHONE_ASSISTANT_THINKING_ASSERTION=true
AI_PHONE_ASSERTION_TIMEOUT_SEC=60
```

断言模型可与主 VLM 使用不同后端，常见部署是主 VLM 用强视觉模型，辅助系统用更便宜的 chat / vision 能力。

## 8. 轨迹缓存也是辅助系统

轨迹缓存不是普通录制回放。它保存的是一次成功 Run 的可执行证据，然后在下一次相同语义目标、相同设备、相同缓存版本下复用。用法见 [`trajectory-cache-usage（轨迹缓存使用文档）.md`](./trajectory-cache-usage（轨迹缓存使用文档）.md)。

三种缓存模式的安全边界不同：

| 模式 | 机制 | 适合场景 |
|---|---|---|
| `v1` | 固定动作回放 | 页面、账号、分辨率都高度稳定的短链路 |
| `v2` | 固定动作 + 状态路标对齐 | 稳定回归链路，偶尔有加载慢或小范围动态内容 |
| `v3` | 保存动作意图，回放时重新定位当前坐标 | 控件位置可能变化，但业务路径稳定 |

V2 的关键是状态路标：保存成功轨迹时，每个 action 后会记录“这个动作完成后、下个动作执行前”的页面状态。回放时如果当前截图和路标对不上，系统可以等待、请求 recovery VLM 修复，或者把缓存回放判失败，而不是盲目继续点旧坐标。

V3 的关键是意图回放：它不信任旧坐标，而是把首跑动作的 `plan_intent` 带到当前截图上重新定位。位置变化时更稳，但每步更慢，也更依赖定位模型质量。

缓存回放完成后仍会走最终断言；失败 Run 会删除当前 mode 对应缓存，V3 回放失败或最终断言失败时会把缓存标记为 suspect，避免坏缓存反复命中。

## 9. 瞬态 UI 与弹窗标记

瞬态 UI 是纯视觉自动化里很容易被低估的问题。很多 App 的工具栏、Toast、临时控件只出现 2-3 秒，而截图、模型思考、动作下发可能要 4-6 秒。为此主 VLM prompt 允许一种严格受限的链式动作：

```text
Thought: 当前界面控件被自动隐藏，第 1 击唤起后立即点击目标按钮。
Action: click(point='<point>500 500</point>')
Action: click(point='<point>66 75</point>')
```

系统会在短时间内顺序执行这 2 个 action，不抓中间截图；链内 action 仍会进入卡死检测，且只允许 `click` / `long_press` / `double_tap` / `drag` 这类不依赖中间反馈的动作。

轨迹缓存还额外处理“非业务瞬态弹窗”：

- 首跑保存 V2 缓存时，classifier 会用 action 前后截图判断某个动作是否只是清理营销弹窗、升级提示、系统通知、引导浮层等非业务遮挡。
- 高置信度通过后，该动作会写入 `role=optional_ephemeral` 和 `ephemeral_meta`，并保留弹窗出现前、弹窗关闭后的截图证据。
- 复跑遇到这个动作时，gate 会看当前截图、首跑弹窗截图和首跑关闭后截图，决定 `SKIP`、`EXECUTE_ORIGINAL`、`EXECUTE_REPAIR`、`ESCALATE` 或 `ASSERT_FAIL`。

这解决的是一个非常具体但很致命的问题：首跑为了关闭弹窗点了一下，复跑时弹窗已经没有了，如果还盲目点旧坐标，可能会误触业务按钮。optional gate 的意义就是让“清障动作”可跳过、可修复，而不是污染整个成功轨迹。

## 10. 协议适配

主 VLM 后端：

```env
AI_PHONE_VLM_BACKEND=doubao_responses
AI_PHONE_VLM_API_URL=...
AI_PHONE_VLM_API_KEY=...
AI_PHONE_VLM_MODEL=...
```

辅助系统后端：

```env
AI_PHONE_ASSISTANT_BACKEND=doubao_chat
AI_PHONE_ASSISTANT_API_URL=...
AI_PHONE_ASSISTANT_API_KEY=
AI_PHONE_ASSISTANT_MODEL=...
```

`AI_PHONE_ASSISTANT_API_KEY` 为空时回落到主 VLM key，便于同一家模型服务部署。

会产出手机动作、会影响轨迹缓存回放、会改变 Run 终态的逻辑，必须遵守 [`executable-logic-contract（可执行链路契约）.md`](./executable-logic-contract（可执行链路契约）.md)。普通辅助聊天可以走 `AI_PHONE_ASSISTANT_BACKEND`；但 gate / recovery / replay 中会产出动作的部分，不能随意降级成非结构化聊天。

## 11. 调参建议

- 误 kill 多：增大 `AI_PHONE_AUDIT_PERIODIC_INTERVAL` 和对应 trigger。
- 卡死发现太晚：降低同坐标、同屏、滑动无进展触发阈值。
- 断言过严：检查 runContent 是否写了可观测目标，避免让模型猜业务成功标准。
- 缓存回放偏航：优先打开 V2 状态对齐；仍不稳时降低缓存使用范围，而不是强行提高阈值。
- 弹窗导致复跑误点：只给高置信度非业务遮挡启用 optional gate，业务确认、权限、登录、安全、支付类弹窗不要标成可跳过。
- token 压力大：保留本地卡死规则，减少周期性审判，辅助模型换更轻后端。
