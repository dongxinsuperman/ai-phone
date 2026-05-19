# assistant-systems（辅助系统核心逻辑及效果）

本文描述当前辅助系统在 ai-phone 主 VLM 执行链路里的位置。代码主要分布在：

- VLM loop：`backend/ai_phone/agent/runner/vlm_loop.py`
- 页面稳定检测：`backend/ai_phone/agent/runner/stability.py`
- 辅助模型适配：`backend/ai_phone/shared/llm/assistants/*`
- 可执行链路契约：`docs/executable-logic-contract（可执行链路契约）.md`

## 1. 定位

主 VLM 负责看图决策和产出手机动作；辅助系统负责判断“这个决策链路是否还可信”。它不是替代主 VLM 的第二个执行器，也不应该直接绕开可执行动作契约。

## 2. 四类能力

| 能力 | 作用 | 成本 |
|---|---|---|
| 页面稳定检测 | 每步截图前等待画面稳定，减少看过渡帧 | 本地截图对比 |
| 结构化卡死检测 | 捕捉同坐标点击、同屏重复、滑动震荡、滑动无进展 | 本地规则，不烧 token |
| 审判系统 | 结构化异常触发轻量模型，判断继续 / 修正 / kill | 辅助模型 token |
| 最终断言 | 用目标、步骤上下文、before / after 图判断是否达成 | 辅助模型 token |

## 3. 页面稳定检测

主链路通过 `wait_page_stable_pixel()` 等待连续截图差异低于阈值后再交给 VLM。相关 env：

```env
AI_PHONE_VLM_PAGE_STABLE_ENABLED=true
AI_PHONE_VLM_PAGE_STABLE_TIMEOUT_S=5.0
AI_PHONE_VLM_PAGE_STABLE_POLL_S=0.4
AI_PHONE_VLM_PAGE_STABLE_THRESHOLD=0.04
```

轨迹缓存回放有独立阈值，避免复跑时和首跑调优互相污染。

## 4. 本地卡死检测

典型信号：

- 点击落在同一坐标桶太多次。
- 屏幕 pHash 反复回到同一状态。
- 滑动方向来回震荡。
- 滑动后画面差异长期很小。
- VLM 连续产出 unknown 或无效动作。

相关 env 集中在 `.env.example` 的辅助系统章节，例如：

```env
AI_PHONE_AUDIT_CLICK_BUCKET_PX=50
AI_PHONE_AUDIT_CLICK_BUCKET_TRIGGER=10
AI_PHONE_AUDIT_SCREEN_REVISIT_HAMMING=8
AI_PHONE_AUDIT_SCREEN_REVISIT_TRIGGER=10
AI_PHONE_AUDIT_SCROLL_FLIP_WINDOW=10
AI_PHONE_AUDIT_SCROLL_FLIP_TRIGGER=6
AI_PHONE_AUDIT_SCROLL_NOPROGRESS_DIFF=0.02
AI_PHONE_AUDIT_SCROLL_NOPROGRESS_TRIGGER=10
```

这些规则先在本地判定，只有达到阈值才召唤辅助模型。

## 5. 审判系统

审判系统读取当前目标、最近步骤、截图证据和结构化异常，输出继续、修正或终止的建议。它解决的是“主 VLM 可能还在动作，但行为已经不再朝目标推进”的问题。

相关 env：

```env
AI_PHONE_AUDIT_TIMEOUT_SEC=30
AI_PHONE_AUDIT_ALLOW_LIMIT=30
AI_PHONE_AUDIT_PERIODIC_INTERVAL=30
AI_PHONE_ASSISTANT_THINKING_JUDGE=true
```

`AI_PHONE_AUDIT_PERIODIC_INTERVAL` 控制主动抽查步频；阈值调大可减少误 kill，但会让异常链路多跑几步。

## 6. 最终断言

最终断言不是只看最后一张图，而是综合：

- 原始 goal / runContent。
- 全步骤摘要。
- 操作前后截图。
- 失败原因、unknown、审判记录。

相关 env：

```env
AI_PHONE_ASSISTANT_THINKING_ASSERTION=true
```

断言模型可与主 VLM 使用不同后端，常见部署是主 VLM 用强视觉模型，辅助系统用更便宜的 chat / vision 能力。

## 7. 协议适配

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

## 8. 可执行链路边界

会产出手机动作、会影响轨迹缓存回放、会改变 Run 终态的逻辑，必须遵守 [`executable-logic-contract（可执行链路契约）.md`](./executable-logic-contract（可执行链路契约）.md)。普通辅助聊天可以走 `AI_PHONE_ASSISTANT_BACKEND`；但 gate / recovery / replay 中会产出动作的部分，不能随意降级成非结构化聊天。

## 9. 调参建议

- 误 kill 多：增大 `AI_PHONE_AUDIT_PERIODIC_INTERVAL` 和对应 trigger。
- 卡死发现太晚：降低同坐标、同屏、滑动无进展触发阈值。
- 断言过严：检查 runContent 是否写了可观测目标，避免让模型猜业务成功标准。
- token 压力大：保留本地卡死规则，减少周期性审判，辅助模型换更轻后端。
