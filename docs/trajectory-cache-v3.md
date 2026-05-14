# 轨迹缓存 V3 方案契约

V3 的业务目标是：信任首次成功沉淀下来的动作计划，不信任首次成功的旧坐标。
回放时每一步都根据当前截图重新识别目标位置，再交给真实设备执行。

## 1. 保存阶段

首次 Run 成功后，V3 保存到独立表 `public.vlm_trajectory_cache_v3`。

保存内容包括：

- 用户目标归一化后的语义。
- 首次成功 Run 的动作列表。
- 每个动作的 `plan_intent`。
- 可选弹窗动作的 `role` / `ephemeral_meta` 标记。
- 首次成功的最终断言信息。
- 设备、平台、分辨率、来源主 VLM backend 等审计信息。

V3 复用 V2 的动作提取、截图证据和瞬态弹窗标记能力，但不复用 V2 表。

## 2. plan_intent 规则

`plan_intent` 是给下次回放定位用的目标短语，不是首跑模型的完整思考记录。
它只回答“当前这一步要找哪个控件 / 区域 / 输入目标”，不解释为什么这么做。

`plan_intent` 由模型在保存阶段清洗生成，脚本规则只做兜底。它必须来自用户原始目标
和首次成功轨迹中的当前 action / 前后 action / role 标记，不能凭空补充业务限定。

生成原则：

- `type` 已经保存动作类型；`plan_intent` 主要保存目标，不重复表达整段动作。
- 不生成、翻译或改写动作协议；动作协议由缓存 action 自身保存。
- 点按类动作输出被点按的控件或区域。
- 输入类动作输出输入框或输入区域，不把输入内容当作目标。
- 移动 / 拖拽 / 滚动类动作输出起点区域、目标区域或可滚动区域。
- 不需要屏幕定位的动作，如果无法提炼目标，则不编造定位目标。
- 如果是 `optional_ephemeral`，必须表达真实清障目标，而不是后续业务目标。
- 如果是普通业务 action，保持用户目标粒度；用户没指定具体文案、编号、条目时，
  不把首跑画面里偶然出现的业务内容写死进缓存。
- 可以保留稳定控件文字，例如按钮、输入框、标签页名称。
- 题目、商品、活动、文章、列表项标题等首跑业务内容，只有用户明确指定时才可保留。
- `thought` 只能作为目标短语的证据来源，不能整体复制。
- 不允许把模型分析过程、裁决文本、失败码、调试日志写入 `plan_intent`。

需要过滤的典型噪声：

- `Let me analyze...`
- `current screenshot`
- `Forced verdict`
- `ASSERT_FAIL`
- `CONTINUE_REPLAY`
- locator / verdict / raw / traceback 等系统元信息

## 3. 回放阶段

命中 V3 缓存后，执行流程是：

1. 截当前屏幕并等待页面相对稳定。
2. 读取当前缓存动作的 `plan_intent`。
3. 让定位 VLM 在当前截图中重新寻找目标坐标。
4. 校验动作类型、坐标边界和重复坐标风险。
5. 执行动作。
6. 全部动作结束后做最终断言。

如果最终断言失败，或回放中途失败，当前 cache 标为 `suspect`，后续不再命中。

## 4. 三类 VLM 角色

V3 有三类模型角色：

- 定位 VLM：每个坐标动作重新识别当前目标位置。
- 标签 VLM：处理 `optional_ephemeral` 弹窗动作本次是否需要执行。
- 辅助 VLM：定位失败后的局部纠偏。

定位 VLM 负责在当前截图里找目标坐标。对海外主链路，它默认复用主 VLM 的
Computer Use 能力配置，保证坐标协议和可执行链路一致；但每次定位都会新建一次性
短会话，只给当前截图和当前 `plan_intent`，不复用主 Run 上下文。Claude 路径强制
关闭 thinking budget，GPT 路径使用 low reasoning effort。

如需把定位 VLM 完全切到独立模型，再设置：

```env
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_USE_MAIN_VLM_CONFIG=false
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_USE_RECOVERY_VLM_CONFIG=false
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_BACKEND=claude_messages
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_API_URL=...
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_API_KEY=...
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_MODEL=...
```

默认值是：

```env
AI_PHONE_TRAJECTORY_CACHE_V3_COORD_USE_MAIN_VLM_CONFIG=true
```

标签 VLM 和辅助 VLM 可能会输出清障或修复动作。只要它们可能操作手机，就必须使用
主 VLM 的可操作能力配置。Prompt 可以不同，但能力配置必须一致。

对 `claude_cu` / `gpt_cu` 的标签 / 辅助 VLM：

- 使用主 VLM Computer Use 配置。
- 坐标按截图实际像素处理，再换算到设备坐标。

对豆包系：

- 坐标按 0-1000 归一化处理。
- 回放仍然只信当前识别结果，不直接复用旧坐标。

## 5. 返回无与局部辅助

定位 VLM 不确定时必须返回无，不能猜测。

必须返回无的情况：

- 目标不可见。
- 目标被弹窗遮挡。
- 当前页面不在预期状态。
- 只能猜测坐标。
- 坐标落在屏幕边缘或不同目标连续返回同一坐标。

返回无后进入辅助 VLM。辅助 VLM 只能做局部衔接：

- `WAIT`：页面加载中，等待后重试。
- `POPUP_CLOSE`：关闭明显遮挡。
- `REPAIR_ACTION`：执行一个安全局部修复动作。
- `CONTINUE_REPLAY`：当前步骤其实已完成，继续下一步。
- `GIVE_UP`：不确定或不可安全恢复。

辅助 VLM 不能自由重跑完整任务。

## 6. 与 V1 / V2 的区别

- V1：信旧坐标，直接固定动作回放，最快但最脆。
- V2：信旧坐标 + 状态路标图对齐，通常最快。
- V3：信动作计划，每步重新识别坐标，速度更慢，但抗 UI 位置变化能力更强。
