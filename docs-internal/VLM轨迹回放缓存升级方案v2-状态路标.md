# VLM 高速缓存回放系统升级方案 v2：整页状态路标

## 0. 为什么要做这个

### 0.1 行业痛点：视觉缓存的"快"和"准"是对立的

纯视觉驱动的 UI 自动化里有一对结构性矛盾：

```text
要快  →  缓存首跑结果，回放时不再问模型
要准  →  每一步都重新看截图、重新规划、重新算坐标
```

两者**只能选一个，无法同时拿到**。这不是工程能力问题，是视觉自动化路线的固有约束：
模型的判断带不确定性，缓存的判断带状态漂移；快慢和准不准本质上是同一枚硬币的两面。

更扎心的是另一层：**纯缓存模式下，偏航不会被发现，发现也不知道是哪步出的事。**
回放跑完看最终断言失败，回头排查却只看到一连串"成功 dispatch"的日志——
中间哪一步开始走偏的，没人知道。

### 0.2 主流方案的回避姿态

我们调研了行业里能找到的所有正面方案，结论是：**没人正面解决这个问题，都在回避**。

```text
midscene-js
  策略：直接放弃坐标缓存，每一步重新让模型看截图识别坐标
  代价：跟没缓存差不多慢
  状态：回避（用速度换准确）

LangChain / browser-use / agent-zero / autogen UI 系
  策略：默认不做轨迹缓存，每次 run 都从头跑模型
  代价：完全不快
  状态：回避（直接放弃缓存）

商业 SaaS（Adept / MultiOn 等闭源产品）
  策略：主流程跑模型 + 局部缓存（按钮位置之类的小颗粒度）
  代价：复杂、不开源、效果不可验证
  状态：回避（缓存力度不敢做大）
```

业界共识不是"我们要怎么解决"，而是"这个问题没解，所以你只能在快和准之间二选一"。

### 0.3 我们的判断：缓存的中间态比纯缓存还鸡肋

我们的历史脚本（早期版本）走过一条跟 midscene 很像的中间路线——**两层缓存：计划层 + 动作坐标层**。
midscene 的做法是缓存计划、放弃坐标，每步重新识别。但我们最后判断这条路是错的：

```text
既然每一步都已经付出了"重新问模型"的开销，
计划缓存就变得没有意义——
反正都问了，不如把计划也重新编排，这样模型对当下页面的理解才是新鲜的，
比"按缓存计划走、临时修坐标"更准。
```

所以中间态（缓存计划 + 重算坐标）在我们看来是个**两头不靠**的位置：
- 速度上没拿到完整缓存的快（因为坐标还要算）
- 准确性上又被旧计划锁住（因为计划是首跑那次的产物，没适应当下页面）

我们直接放弃了这条路，走两端：
**要快就完整缓存（V1）**，**要准就完整跑模型（VLMRunner）**——
中间不要任何"省一半工"的伪优化。

### 0.4 V2 的定位：不是兜底，是让 V1 不再失控

V1 解决了"快"——一个机器一份缓存，命中后纯 dispatch，速度接近极限。
但 V1 没解决"扑空不被发现"，跑挂了不知道是哪步挂的。

V2 不试图把缓存做成 100% 准确——做不到，行业里没人做到。
V2 的目标是：**在 V1 的快速通道上加一层"自检电路"，让偏航即时暴露 + 给一次自愈机会。**

```text
V2 解决的具体问题
  - 每个 action 后实时校验"是不是真的对齐到首跑那一刻的页面状态"
  - 偏航即时暴露，定位到具体哪一步出问题（不再等到最终断言才报错）
  - 给一次"局部 VLM 救场"机会，能修就修，修不了立即终止 + 删缓存

V2 不解决的
  - 不保证 100% 不扑空（视觉自动化的固有约束）
  - 不取代主 VLM 的判断（救场用同一家模型但走独立通道，不污染主流程）
  - 不变成 midscene 那样的中间态（V1 还在，想要纯模型走 VLMRunner）
```

### 0.5 三档清晰区分

跟前面调研的方案放一起看，我们的整体架构是：

| 路线 | 速度 | 准确性 | 自检 | 我们的对应模式 |
|---|---|---|---|---|
| 完全跑模型（如 VLMRunner / midscene 极端版）| 慢 | 高 | 强 | VLMRunner（缓存关闭时） |
| 缓存计划 + 重算坐标（midscene 默认）| 中等 | 中等 | 中等 | **被我们放弃** |
| 完整缓存盲飞（V1） | 快 | 取决运气 | 无 | V1（V2 关闭时降级到这里） |
| **完整缓存 + 自检 + 救场（V2）** | **健康 case 接近 V1，故障 case 慢 1-3s** | **比 V1 高一档** | **中等强度** | **V2（本方案）** |

V2 的位置是：**保留 V1 的速度上限，把 V1 最大的盲点（扑空不被发现）补上**。
不追求成为最准的方案，只追求"在快这条路上不再失控"。

---

## 1. 结论

该方案可行，并且适合在现有 v1 轨迹缓存之上演进。

核心变化不是“缓存更多截图”这么简单，而是把缓存从：

```text
action replay
```

升级为：

```text
action replay + Replay State Alignment
```

也就是：回放每个 action 后，不只等页面稳定，还要和首次成功轨迹里对应的
“下一步执行前状态”做整页状态对齐。对齐则继续高速回放；不对齐则结合历史
等待时间继续观察；仍不对齐才进入独立 VLM 专线处理。

建议拆成两个阶段：

```text
阶段 A：首次记录增强
  - 只增加首跑沉淀的数据
  - 不改变现有回放执行
  - 不改变现有 VLMRunner / ReplayRunner 主逻辑
  - 风险低，可先上线沉淀样本

阶段 B：智能缓存增强
  - 新增开关控制
  - 关闭时行为与当前 v1 完全一致
  - 开启时在现有回放基础上介入整页路标匹配与 VLM 专线
```

本方案继续坚持一个核心原则：

```text
高冗余，低耦合。
```

高冗余指：允许多存 action 元信息、handoff snapshot、hash、timing、原始模型输出、
清洗后 action、日志索引等证据，方便后续回放、排查、纠偏和模型切换。

低耦合指：这些增强数据和智能判断不能反向绑死现有 VLMRunner、ReplayRunner、
断言系统和 action adapter。任何 v2 能力都应该可以独立关闭，关闭后回到当前 v1 行为。

## 2. 背景

当前系统是纯视觉 VLM 驱动的 UI 自动化。

首次执行时：

```text
截图 -> VLM 决策 -> 执行 action -> 截图 / 日志 / 断言
```

后续执行时：

```text
命中轨迹缓存 -> 顺序回放 action -> 最终断言
```

v1 已解决：

```text
1. 成功轨迹 action 清洗与保存。
2. device_code + run 语义强匹配。
3. 命中后独立 replay runner 回放。
4. 最终断言复核。
5. 失败 case 级删除缓存。
6. Doubao / Claude / GPT action adapter 分离。
```

v1 的边界是：中间步骤不理解页面，不知道 action_i 后是否已经偏航，只能等到
后续 action 报错或最终断言失败。

v2 要解决的是：**更早发现轨迹状态偏离，并给 VLM 专线足够的轨迹证据做纠偏。**

## 3. 不走元素树路线

继续坚持纯视觉路线，不引入 Appium / DOM / accessibility tree 作为主判断依据。

原因：

```text
1. 三端 App / Web / WebView / 原生控件结构不统一。
2. WebView context 切换成本高，且不稳定。
3. iOS / Android / HarmonyOS 元素树结构和语义 locator 不一致。
4. 原生控件、H5、小程序、自绘控件混杂。
5. 跨端维护元素语义可能比纯视觉更脆弱。
```

主路线仍是：

```text
纯视觉执行
  -> action 轨迹缓存
  -> 整页视觉状态对齐
  -> 异常时 VLM 专线介入
```

## 4. 页面稳定检测的定位

页面稳定检测只回答一个问题：

```text
什么时候适合继续操作 / 截图 / 交给 VLM？
```

它不负责证明：

```text
1. action 是否点中了。
2. action 是否生效。
3. 缓存轨迹是否偏航。
4. 当前页面是否符合首次成功轨迹。
```

因此 v2 不应该把“页面稳定”误用成“action 成功”。页面稳定检测仍然是等待层；
状态路标匹配才是 replay 对齐层。

## 5. 核心概念：handoff snapshot

每个 action 需要保存一个整页状态路标：

```text
handoff_snapshot_i
```

它的定义非常严格：

```text
action_i 执行完成
  -> 页面等待 / 稳定检测 / VLM 清障 / 其他处理
  -> 系统确认可以执行 action_{i+1}
  -> 保存此刻整页截图，作为 action_i 的 handoff snapshot
  -> 再执行 action_{i+1}
```

也就是说，`handoff_snapshot_i` 不是 action 后 100ms / 300ms / 500ms 的任意截图，
而是：

```text
action_i 完成后，action_{i+1} 执行前，系统已经准备进入下一步的页面状态。
```

它代表“上一步已经把页面带到了下一步可执行的状态”。

## 6. 不缓存中间等待截图

首跑中可能出现很多等待截图：

```text
T+100ms   动画中
T+300ms   loading 中
T+600ms   半稳定
T+900ms   稳定检测仍未通过
T+1400ms  准备执行下一步
```

主缓存只保存最后一张：

```text
T+1400ms  准备执行下一步
```

中间截图可以作为日志、debug、训练清洗辅助数据，但不进入主 replay cache。

原因：

```text
1. 中间态不代表下一步可执行状态。
2. 中间态很难在回放时稳定复现。
3. 中间态容易导致误匹配。
4. 中间态会污染缓存数据。
5. 中间态会让 VLM 专线输入变复杂。
```

## 7. 数据结构升级

v1 当前核心是：

```text
trajectory_json.actions[]
```

v2 建议扩展为：

```text
trajectory_json.actions[]
trajectory_json.state_landmarks[]
trajectory_json.replay_alignment_policy
```

### 7.1 action 字段要求

每个 action 至少需要明确：

```text
index
action_id
source_step
type
point / content / direction / app_name ...
intent
thought
raw
```

`action_id` 应该是稳定的内部 ID，而不是依赖数组位置。建议格式：

```text
a001
a002
a003
```

数组位置可以变，`action_id` 不应该变。后续 VLM 专线定位、跳步、局部重规划都需要
稳定 ID。

### 7.2 handoff snapshot 字段

建议结构：

```json
{
  "action_id": "a005",
  "after_action_index": 5,
  "before_action_index": 6,
  "image_url": "/files/...",
  "image_sha256": "...",
  "image_phash": "...",
  "captured_at_ms": 103450,
  "meaning": "action_5 完成后，action_6 执行前的页面状态"
}
```

注意：

```text
after_action_index = action_i
before_action_index = action_{i+1}
```

这能避免“第 5 步 after 图到底是第 5 步结束还是第 6 步 before”的歧义。

### 7.3 timing 字段

建议每个 action 保存：

```json
{
  "action_id": "a005",
  "action_start_ts_ms": 101000,
  "action_end_ts_ms": 102000,
  "handoff_snapshot_ts_ms": 103450,
  "next_action_start_ts_ms": 103460,
  "gap_to_next_action_ms": 1450
}
```

其中最重要的是：

```text
gap_to_next_action_ms
```

它不一定用于回放时完整 sleep，但用于判断“不匹配是不是因为截图太早”。

## 8. 回放阶段流程

关闭 v2 智能增强时：

```text
保持当前 v1 行为
```

开启 v2 智能增强时：

```text
execute(action_i)
  -> 基础观察延时 min_observe_delay_ms
  -> screenshot
  -> compare(current, handoff_snapshot_i)
  -> 匹配：继续 action_{i+1}
  -> 不匹配：结合 gap_to_next_action_ms 判断是否太早
  -> 可能太早：继续等待并重试
  -> 超过合理等待仍不匹配：触发 VLM 专线
```

伪代码：

```python
execute(action_i)
sleep(min_observe_delay_ms)

while True:
    current = screenshot()
    result = match_handoff(current, handoff_snapshot_i)
    if result.match:
        continue_replay()
        break

    if elapsed_after_action < reasonable_wait_from_history_gap(action_i):
        sleep(retry_interval_ms)
        continue

    trigger_vlm_recovery()
    break
```

## 9. 基础观察延时

建议新增：

```text
min_observe_delay_ms = 500
```

它不是“无脑变慢”，而是降低误判。

典型页面：

```text
T0       执行 tap
T+100ms  按钮反馈 / ripple
T+300ms  loading 出现
T+700ms  页面跳转中
T+1200ms 新页面稳定
T+1450ms 首跑进入下一步前状态
```

如果 T+100ms 截图对比，很容易误判不匹配。500ms 是一个低成本缓冲，后续可按
历史 gap 动态调整：

```text
observe_delay = min(500ms, gap_to_next_action_ms * 0.3)
```

第一版建议先固定 500ms，避免策略过早复杂化。

当前实现口径：

```text
AI_PHONE_TRAJECTORY_CACHE_OBSERVE_DELAY_MS=500
```

它先接入缓存回放通道，在每个非 wait action 执行后、下一次页面稳定检测前等待。
该配置不影响 VLM 主通道；设为 0 即关闭。

## 10. 整页状态对齐

整页匹配不是为了证明 action 一定命中，而是判断：

```text
当前 replay 状态是否仍贴近首次成功轨迹的大状态。
```

匹配含义：

```text
current ≈ handoff_snapshot_i
  -> 当前轨迹大概率仍对齐，可以继续高速回放
```

不匹配含义：

```text
current != handoff_snapshot_i
  -> 可能截图太早、动态资源变化、弹窗阻挡、起跑状态不同、或轨迹偏航
```

整页匹配优先于局部 patch，因为局部区域可能是：

```text
1. 视频帧。
2. 动态运营图。
3. 列表内容。
4. 倒计时 / 价格 / 资源位。
5. 三端差异更明显的小控件。
```

整页更适合判断：

```text
页面结构
导航栏
弹窗 / 浮层
页面层级
整体布局
是否跳错页面
是否起跑页面不同
```

## 11. 对比算法建议

这里不是复用旧页面稳定检测的判断结果。

旧页面稳定检测回答的是：

```text
当前页面是否已经稳定到适合继续操作。
```

状态路标对比回答的是：

```text
当前页面是否像首次成功轨迹中 action_i 之后、action_{i+1} 之前的那张 handoff snapshot。
```

截图获取、等待、尾帧复用可以复用现有页面稳定检测的基础能力；
但 `MATCH / MISS` 需要是缓存通道独立的对齐判断。

v2 智能增强第一版不要追求“视觉 AI 对齐模型”，先用图像级组合：

```text
1. pHash 整页低频结构
2. center ROI 像素差
3. 黑屏比例差异
4. 横竖屏方向一致性
```

建议先实现接口抽象：

```text
StateAlignmentComparator.compare(current, landmark) -> AlignmentResult
```

结果包含：

```json
{
  "match": true,
  "reason": "match",
  "global_diff": 0.0039,
  "center_mae": 0.04,
  "black_ratio_diff": 0.01
}
```

这样后续算法换成更智能的，不影响 replay runner。

## 12. VLM 专线

VLM 专线必须独立，不耦合主 `VLMRunner`。

建议新模块：

```text
trajectory_cache/recovery.py
```

职责：

```text
输入当前状态、缓存路标、失败对齐结果
输出 recovery decision
```

它不应该直接改写主 replay runner，只返回决策：

```text
WAIT_MORE
CONTINUE_REPLAY
JUMP_TO_ACTION
CLEAR_BLOCKER
LOCAL_REPLAN
FALLBACK_TO_FULL_VLM
FAIL
```

### 12.1 VLM 专线输入

至少包括：

```text
1. 当前截图。
2. 当前 action_id / step_id。
3. 当前 action 内容。
4. 当前 action 对应 handoff snapshot。
5. 前后若干 handoff snapshots。
6. 完整 action 轨迹摘要。
7. gap_to_next_action_ms。
8. 当前已等待时间。
9. 对齐失败结果。
10. 当前 run 目标。
```

### 12.2 VLM 专线任务

它要判断：

```text
1. 是否只是视频资源 / 运营图 / 动态内容变化，不影响执行。
2. 是否截图太早，还需要等待。
3. 是否被弹窗 / 浮层 / 权限弹窗阻碍。
4. 是否起跑页面不同。
5. 当前页面更像缓存轨迹中的哪一个 step。
6. 是否可以跳到某个 action 继续回放。
7. 是否需要清障。
8. 是否需要局部重规划。
```

## 13. 起跑线不同

如果第一个 action 后就不匹配，通常有几类原因：

```text
1. App 起跑页面不同。
2. 已经处在缓存轨迹的后续页面。
3. 多了弹窗 / 活动页 / 权限页。
4. 账号状态不同。
5. 首屏加载速度差异。
```

VLM 专线可以用整个 handoff snapshot 序列做轨迹定位：

```text
current 更像 step_0？
current 更像 step_3？
current 是否只是多了弹窗？
current 是否已经越过某些前置步骤？
```

可能输出：

```text
清障后继续 action_1
跳到 action_4 继续
回到目标起跑页
局部重规划一段路径
全量回退 VLM
```

## 14. 视频 / 动态资源

整页不匹配不等于失败。

视频帧、资源位、运营图、价格、倒计时可能变化。状态路标 comparator 可能把
这些变化判成 mismatch，也可能在页面大结构一致时继续 MATCH。

处理原则：

```text
状态路标 MATCH 失败 -> 不立即 fail
  -> 结合时间等待
  -> 仍失败才交给 VLM 专线
```

VLM 专线可以判断：

```text
页面结构相同，只是动态内容不同 -> CONTINUE_REPLAY
```

## 15. 与现有实现的关系

当前 v1 在 `next/server-brain` 中的主要模块：

```text
backend/ai_phone/server/trajectory_cache/service.py
backend/ai_phone/server/trajectory_cache/replay.py
backend/ai_phone/server/trajectory_cache/assertion.py
backend/ai_phone/server/trajectory_cache/action_adapters.py
```

v2 不应改动现有 v1 默认行为。

实现原则：

```text
1. 允许重复保存数据，不为了“省一份字段”耦合主流程。
2. 允许新增独立模块，不把状态路标逻辑塞进 VLMRunner。
3. 允许独立 schema / 独立配置 / 独立开关。
4. 允许 replay、alignment、recovery 各自失败并降级，不影响原有通道。
5. 允许 action adapter 继续只负责“模型输出 -> 统一 action”，不承担缓存判断。
```

建议新增：

```text
trajectory_cache/landmarks.py
trajectory_cache/alignment.py
trajectory_cache/recovery.py
```

或保持更明确：

```text
trajectory_cache/state_landmarks.py
trajectory_cache/state_alignment.py
trajectory_cache/recovery_vlm.py
```

## 16. 两阶段实施

### 阶段 A：记录增强

目标：

```text
只增加数据，不改变执行。
高冗余保存首跑证据，为后续智能增强准备数据。
```

内容：

```text
1. 为 action 增加 action_id。
2. 保存 action_i -> action_{i+1} 的 handoff snapshot。
3. 保存 gap_to_next_action_ms。
4. 保存 snapshot hash / phash。
5. 在 trajectory_json 中增加 state_landmarks。
6. 缓存 schema_version 升级。
7. 保留 raw action / canonical action / intent / thought / source backend 等冗余信息。
8. 保存截图与日志索引关系，方便后续从报告或 DB 追溯。
```

阶段 A 即使采集 handoff snapshot 失败，也不能影响 case 通过后的缓存保存。

建议保存：

```text
landmark.status = available | unavailable
landmark.missing_reason = image_not_found | capture_failed | unsupported_step | unknown
```

这样后续阶段 B 开启状态对齐时，可以针对缺图 landmark 自动降级，而不是让回放异常退出。

不做：

```text
1. 不改变 replay 执行。
2. 不每步做状态对齐。
3. 不调用 VLM 专线。
4. 不改变失败删除策略。
5. 不让主 VLMRunner 依赖 state_landmarks。
```

风险：

```text
低。主要是存储体积增加、截图保存路径和生命周期管理。
```

### 阶段 B：智能缓存增强

目标：

```text
在 v1 replay 基础上按开关介入状态对齐。
低耦合接入，关闭后完全回到 v1 replay。
```

建议 env：

```text
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_ENABLED=false
AI_PHONE_TRAJECTORY_CACHE_OBSERVE_DELAY_MS=500
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_RETRY_INTERVAL_MS=300
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_MIN_WAIT_MS=1000
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_MAX_WAIT_RATIO=1.3
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_THRESHOLD=0.03
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_ROI_THRESHOLD=0.25
AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_BLACK_RATIO_THRESHOLD=0.15
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_ENABLED=false
```

关闭时：

```text
完全保持 v1 replay 行为。
```

开启时：

```text
action_i replay 后
  -> observe delay
  -> screenshot
  -> MATCH：global pHash + center ROI + black ratio + orientation
  -> MATCH：跳过完整页面稳定检测，并复用该帧作为 action_{i+1}.before
  -> MISS：结合 gap_to_next_action_ms 等待并重试
  -> 超过等待窗口仍 MISS：回落现有页面稳定检测
  -> 后续再扩展 recovery VLM
```

## 17. 已拉齐的实现口径

### 17.1 handoff snapshot 的准确采集点

关键问题：

```text
系统什么时候能确定“下一步即将执行”？
```

可选采集点：

```text
1. 当前 VLMRunner 每步 before screenshot。
   - 优点：天然就是 action_i 后、action_{i+1} 前。
   - 缺点：step 编号要重新映射到上一 action 的 handoff。

2. replay / runner 内部显式记录“下一步 decision 前帧”。
   - 优点：语义最清晰。
   - 缺点：要加事件或缓存 collector。
```

确定口径：

```text
把 action_{i+1} 真正执行 action 前的 before screenshot
作为 action_i 的 handoff_snapshot。
```

也就是：

```text
action_i -> handoff_snapshot_i = next_step.before
```

注意：

```text
这里的 next_step.before 不是页面稳定检测过程中的任意中间截图，
也不是 action 后 100ms / 300ms 截到的过渡帧。

它必须是“下一个有效 action 即将执行前”的那张可操作截图。
```

如果中间存在页面稳定检测、等待、清障、截图重试，只取最终进入下一 action 前的那张图。

最后一个 action 的 handoff 使用 `finished` / 断言前能拿到的最终可判定截图。

如果该图不存在或读取失败：

```text
1. 阶段 A：保存 landmark unavailable，不影响当前 v1 缓存保存。
2. 阶段 B：alignment 开启时跳过该 action 的状态对齐，按 v1 replay 或 recovery 策略继续。
3. 缺图必须打日志，方便后续排查采集链路。
```

### 17.2 step 与 action 的对应关系

现状里一个 step 可能包含：

```text
单 action
动作链 action A -> action B
系统起跑线 close/open
wait
finished/assert_fail
```

v2 必须明确：

```text
action_id
source_step
chain_index
```

建议：

```json
{
  "action_id": "a004",
  "source_step": 3,
  "chain_index": 1,
  "type": "click"
}
```

实现上由缓存 collector 决定映射规则，不要求业务方感知。

确定口径：

```text
action_id 使用缓存内部递增编号，例如 a001 / a002 / a003。
source_step 保留原 Run step 编号。
chain_index 表示同一个 step 内第几个有效 action。
```

`finished`、纯截图、纯断言日志不进入 replay action 序列。

### 17.3 最后一个 action 的 handoff

普通 action 后有下一步 before 图。

最后一个业务 action 后，下一步通常是 finished/断言阶段。

确定口径：

```text
最后一个业务 action 的 handoff_snapshot = 主 VLM 申请 finished 前看到的 before 图
或 finished_ok/final assertion 前最终稳定截图。
```

这里说的是首跑普通 VLM 通道的最后状态截图来源，
不是 v2 recovery VLM，也不是断言模型的判断结果。

优先级：

```text
1. 主 VLM 申请 finished / 断言系统开始前的最终可判定截图。
2. finish_ok 截图。
3. final assertion 前截图。
4. 都不存在则 landmark unavailable。
```

### 17.4 截图存储成本

每条 action 增加一张 handoff snapshot，会带来存储增长。

可选策略：

```text
1. 不把大图塞进主缓存表。
2. 主表只保存 image_path / image_url + sha256 + phash + status。
3. 图片文件优先复用现有报告截图；必要时另存 JPEG 压缩图，max_side=720。
4. 设置缓存生命周期，删除 cache 时可删除关联截图。
5. 图片删除失败或路径失效不影响缓存删除主流程。
6. 回放读取不到图时，按 landmark unavailable 降级，不直接报错中断。
7. 中间等待截图不进主缓存。
```

这种方式更符合“高冗余、低耦合”：主缓存记录足够证据和索引，但不依赖图片文件永远存在。

### 17.5 对齐算法阈值

v2 智能增强第一版只保留一个外部判断结果：`MATCH / MISS`。

MATCH 必须是严格条件：

```text
global pHash 通过
AND center ROI 通过
AND black ratio 通过
AND orientation 通过
```

MISS 不直接判失败：

```text
MISS -> 结合 gap_to_next_action_ms 等待重试
MISS 超时 -> 回落页面稳定检测
后续再接 recovery_vlm
```

### 17.6 VLM 专线动作边界

这里的“第一版”指 v2 智能缓存增强第一版，不是当前 v1 action replay。

v1 是当前已经存在的纯 action 缓存回放。

v2 智能增强第一版需要先明确 VLM 专线允许做什么。

建议 v2 智能增强第一版只允许：

```text
WAIT_MORE
CONTINUE_REPLAY
FAIL_AND_DELETE_CACHE
FALLBACK_TO_FULL_VLM
```

实现上先单独建立 `recovery_vlm` 通道和 prompt，不复用正常 VLM 决策 prompt。
即使第一版只做保守决策，也保持模块独立，后续再慢慢调优。

暂缓：

```text
JUMP_TO_ACTION
CLEAR_BLOCKER
LOCAL_REPLAN
```

原因：跳步和局部重规划很强，但需要更多执行安全边界。

## 18. 风险与边界

该方案不能绝对证明每步 action 命中。

典型边界：

```text
1. action 无视觉反馈。
2. action 只触发后台请求，页面不变。
3. action 点空但页面状态刚好类似。
4. 动态内容大面积变化导致整页相似度低。
5. 首跑 handoff 本身采集到了错误但断言通过的状态。
```

因此 v2 的目标不是“证明每步 action 生效”，而是：

```text
尽早发现轨迹状态偏离。
```

## 19. 建议下一步

按低风险顺序：

```text
1. 先补 action_id / source_step / chain_index。
2. 复用 step_{i+1}.before 生成 handoff_snapshot_i。
3. 保存 gap_to_next_action_ms。
4. 保存 phash / sha256，不启用 replay 对齐。
5. 跑真实 case 沉淀样本，人工查看 landmarks 是否符合预期。
6. 再加 alignment comparator，开关默认关闭。
7. 最后加 VLM recovery 专线，开关默认关闭。
```

## 20. 当前实施进度

### 2026-05-09：阶段 A 记录增强已落地

本次先按 doubao 系验证优先的路线推进，但实现仍基于统一 canonical action，
不在保存层引入 doubao 专属耦合。海外模型后续只需要重点验证 action adapter
输出的 canonical action 是否足够稳定。

已完成：

```text
1. CACHE_SCHEMA_VERSION 升级为 2。
2. trajectory_json.actions 每个 action 增加：
   - action_id，例如 a001 / a002
   - chain_index，表示同一 source_step 内第几个有效 action
3. trajectory_json 增加 state_landmarks。
4. state_landmarks 记录：
   - action_id / before_action_id
   - after_action_index / before_action_index
   - source_step / snapshot_step / snapshot_phase
   - image_url / image_path / image_sha256 / image_phash / image_size_bytes
   - status / missing_reason
   - action_start_ts_ms / action_end_ts_ms / handoff_snapshot_ts_ms
   - next_action_start_ts_ms / gap_to_next_action_ms
5. 图片路径读取失败、图片缺失、无独立 handoff 图时，只记录 unavailable，
   不影响缓存保存。
6. 缓存回放通道增加 action 后基础观察延迟：
   - env: AI_PHONE_TRAJECTORY_CACHE_OBSERVE_DELAY_MS
   - 默认 500ms
   - 只影响缓存回放，不影响 VLM 主通道
   - 日志标题：轨迹缓存观察延迟
7. 当前 replay 行为已接入严格 state_landmarks 对齐快路径：
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_ENABLED
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_THRESHOLD
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_ROI_THRESHOLD
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_BLACK_RATIO_THRESHOLD
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_RETRY_INTERVAL_MS
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_MIN_WAIT_MS
   - env: AI_PHONE_TRAJECTORY_CACHE_ALIGNMENT_MAX_WAIT_RATIO
   - MATCH = global pHash + center ROI + black ratio + orientation 全部通过
   - MATCH 时跳过完整页面稳定检测
   - MATCH 后复用该帧作为下一 action 的 before
   - MISS 时结合 gap_to_next_action_ms 等待并重试
   - MISS 超过等待窗口时判定轨迹偏航，当前阶段终止缓存回放
   - unavailable / 老缓存缺字段时回落现有页面稳定检测
8. wait 已明确纳入 replay action：
   - wait 前后也会走 before / action / observe / state landmark 对齐
   - 不再把 wait 当作“无需状态路标”的特殊动作
9. 最后一个业务 action 的 handoff 图取值已收紧：
   - 只能使用后续有效 action 的 before 图，或 finished 前的 before 图
   - 找不到真正 handoff 图时标记 unavailable
   - 不再使用当前 action 的 after 图冒充最终 handoff，避免把加载中/白屏当成正确路标
10. alignment_miss 已成为 recovery_vlm 的明确接入点：
   - 当前尚未接 VLM 专线
   - 因此有可用 landmark 且等待窗口耗尽仍 MISS 时，先收口为缓存对齐失败
   - Server 层将 alignment_miss 收成 assert_fail / TrajectoryCacheAlignmentError
11. 状态路标日志已补充可观察流程：
   - 开始对齐：observe、history_gap、max_wait、阈值
   - 重截图：action_id、attempt、scheduled_elapsed
   - MISS/MATCH：global、center、black、reason
   - 等待完成：next_scheduled_elapsed
   - 超时偏航：轨迹偏航，终止缓存回放
12. 缓存相关测试已更新并通过，包括：
   - 图片路径失效时 landmark unavailable 的降级用例
   - wait action 也进入状态路标对齐
   - 最终 action 不使用 action-after 冒充 handoff
   - alignment_miss 不继续回放，并由 Server 层收成 assert_fail
```

当前没有完成：

```text
1. 未做 recovery_vlm 专线；当前 alignment_miss 先直接 assert_fail。
2. 未做更复杂的语义级 comparator（当前仍是图像级严格 MATCH）。
3. 未做跳步、清障、局部重规划。
4. 未对海外模型做专项回归，仅保持 canonical action 层兼容。
```

接力注意：

```text
1. 阶段 B 的 alignment 开关已存在，默认仍应保持关闭；验证时按需开启。
2. 阶段 B 开启后，遇到 landmark unavailable 不能硬失败，应回落现有稳定检测。
3. 有可用 landmark 且等待窗口耗尽仍 MISS，不能继续执行后续缓存 action；
   当前阶段先 assert_fail，下一阶段接 recovery_vlm。
4. 首轮真实验证建议只用 doubao_responses 跑成功 case，观察 state_landmarks
   是否拿到了真正“下一有效 action 前”的图。
5. 确认 doubao 样本稳定后，再拿 claude_cu / gpt_cu 分别验证 action_id、
   source_step、chain_index、state_landmarks 是否合理。
```

### 2026-05-09：阶段 B 对齐验证样本结论

真实样本已经验证出几个关键行为：

```text
1. “点击同步刷题，点击切换”首跑成功后，回放阶段可正确识别 a001 后页面状态不一致：
   - 多次重截图后 global diff 稳定在 0.25 左右
   - 阈值为 0.03
   - 因此持续 MISS，不会误判为 MATCH
2. 历史 gap 等待窗口生效：
   - 使用首跑 action_i 结束到 action_{i+1} 开始的 gap_to_next_action_ms
   - 再乘以 max_wait_ratio 形成最大等待窗口
   - elapsed 已改为墙钟时间，截图/对比耗时也计入
3. MISS 超过窗口后不会继续执行后续 action：
   - 不再回落页面稳定检测继续乱跑
   - 当前直接形成 alignment_miss
4. 服务层已将 alignment_miss 归为缓存对齐断言失败：
   - result=assert_fail
   - error_class=TrajectoryCacheAlignmentError
   - 后续 recovery_vlm 专线应接在这个点之前
5. 日志已经能看出完整过程：
   - 开始对齐
   - 重截图
   - MISS 对比指标
   - 等待完成
   - 轨迹偏航终止
```

下一阶段目标：

```text
在 alignment_miss 后接独立 recovery_vlm：
  -> 输入当前截图、缓存 handoff 图、当前 action、前后 action 摘要、等待窗口信息
  -> VLM 判 CONTINUE_REPLAY / WAIT_MORE / ASSERT_FAIL
  -> 第一版只做判断，不做跳步、清障、局部重规划
```

### 2026-05-09：recovery_vlm 三态裁决专线已落地

接力完成 alignment_miss 之后的最后一道防线。继续遵守"先做最小闭环"：第一版
**只判断、不执行新动作**，跳步 / 清障 / 局部重规划仍然全部留待后续阶段。

通道选型：

```text
1. recovery_vlm 是独立通道，与辅助系统、断言系统、主 VLMRunner 完全隔离。
2. 不复用 BaseAssistant / BaseMainVLM 抽象类，避免污染 token 统计 / 限流 /
   会话语义。
3. 第一版直连 OpenAI 兼容 chat completions（豆包方舟 chat 端点同协议），
   双图 + 一次性 prompt + thinking=enabled。
4. 模型 / URL / Key 由独立 env 控制，典型用法是填成与主 VLM 同款模型，
   但 endpoint 必须是 chat completions（不能填 responses 端点）。
5. 任何字段缺失或调用失败统一收成 ASSERT_FAIL，符合"保守原则"。
```

新增 env（默认全部关闭，回放路径与上一阶段完全等价）：

```text
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_ENABLED=false
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_BACKEND=openai_compatible
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_API_URL=
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_API_KEY=
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MODEL=
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_TIMEOUT_SEC=30
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_WAIT_MORE_MS=1500
AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MAX_WAIT_MORE=1
```

接入路径：

```text
ServerRunnerService._maybe_run_trajectory_cache
  -> settings.trajectory_cache_recovery_vlm_enabled 为 True 时构造 verifier
  -> 注入到 ReplayRunner(recovery_verifier=, goal=)
  -> alignment 等待窗口耗尽（_handle_alignment_miss）才调用一次 VLM
  -> 不在窗口中段提前介入，保持现有 alignment 主循环零改动
```

三态处理：

```text
CONTINUE_REPLAY
  -> 接受当前截图作为 after，并把它 carry 给下一 action.before
  -> 写 RunLog: 「轨迹缓存 VLM 介入」 verdict=CONTINUE_REPLAY ...

WAIT_MORE
  -> 写 RunLog 标注本次配额（第 N/max_wait_more 次）
  -> 等待 verdict.wait_ms（被夹在 100-10000ms 之间）
  -> 重新截图 + 重新 _compare_alignment
  -> MATCH：直接接受当前帧，写 RunLog「MATCH-after-WAIT_MORE」
  -> MISS：再问一次 VLM；若已超出 max_wait_more 配额，按 ASSERT_FAIL 兜底
  -> 死循环防御：max_wait_more 默认 1，且 wait_ms 被强制夹紧

ASSERT_FAIL（含通道未配置 / 调用失败 / 协议外内容 / 配额耗尽）
  -> 写 RunLog 终止理由
  -> raise ReplayActionError，沿用现有 alignment_miss 上行通路
  -> Server 层仍然收成 result=assert_fail / TrajectoryCacheAlignmentError
```

可观察性：

```text
1. RunLog 标题「轨迹缓存 VLM 介入」携带 verdict / wait_ms / elapsed / reason，
   并截取模型 raw 文本首行 120 字符方便排查。
2. CONTINUE_REPLAY 也写 RunLog，便于追踪"v2 替我们救了一次"的次数。
3. recovery 通道未注入或未配置时，原「轨迹偏航，终止缓存回放」日志保留，
   行为与上一阶段一致。
```

补充进度（2026-05-09）：

```text
- recovery_vlm 已支持 doubao_responses backend，可直接复制主 VLM 的
  backend/url/key/model；同时保留 openai_compatible chat completions 分支。
- backend/.env 已按“独立字段、值复制主 VLM”补齐，便于单独开关和调参。
- recovery_vlm 已从“三态文本裁决”升级为 doubao 主 VLM DSL：
  Thought + Action，不另起 JSON 协议。
- finished = 放行继续回放；wait = 等待后重比；assert_fail = 终止；
  click/type/scroll/drag 等合法 action = 局部修复动作。
- ReplayRunner 会执行局部修复 action，随后重新截图与 handoff 图对齐；
  对齐成功则继续缓存回放，不对齐则再次交给 recovery_vlm。
- 新增 AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MAX_REPAIR_ACTIONS，默认 5。
- 新增 AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_MAX_CALLS_PER_REPLAY，默认 5。
  单条缓存回放召唤 recovery_vlm 超限时，判定 case/cache 不健康并直接失败。
- recovery_vlm 的判断口径已收紧：必须同时看 goal、首次成功 handoff 图、
  本次回放当前图。除非 goal 明确允许忽略某类页面上下文差异，否则稳定上下文
  不一致不能因为“下一按钮可见”就放行；无法解释差异或无法证明不影响后续衔接 /
  最终断言时，禁止 finished 放行，应尝试局部修复或 assert_fail。
- AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_ENABLED=false 时，不调用 recovery_vlm。
  回放仍会按历史 action 间隔等待并重新截图对齐；若仍不匹配，直接按
  alignment_miss / assert_fail 结束，便于临时撤销 VLM 介入能力而不影响主 VLM。
```

测试覆盖（test_trajectory_cache.py 共 48 用例 / backend 全量 283 用例通过）：

```text
- parse_recovery_response：CONTINUE / WAIT_MORE 默认 ms / WAIT_MORE 含 ms /
  WAIT_MORE 极端 ms 夹紧 / ASSERT_FAIL / 协议外兜底 / doubao finished 映射放行 /
  doubao click 映射局部修复动作
- build_recovery_prompt：局部恢复边界 + 主 VLM Thought/Action DSL + 动态内容放行规则
- CacheReplayRecoveryVerifier：disabled 兜底 ASSERT_FAIL / 配置缺失诊断 /
  网络失败兜底 ASSERT_FAIL / doubao_responses backend 分支
- ReplayRunner（注入 fake verifier）：CONTINUE 路径接受当前帧 +
  carry_before / WAIT_MORE 后 recheck MATCH / WAIT_MORE 配额耗尽兜底 /
  REPAIR_ACTION 执行后 recheck MATCH / recovery case 级调用超限 /
  ASSERT_FAIL 终止
```

接力注意：

```text
1. 真实 case 验证前，可以直接把 AI_PHONE_TRAJECTORY_CACHE_RECOVERY_VLM_*
   复制主 VLM 配置；doubao_responses 对应 /responses，openai_compatible
   对应 chat completions。
2. 默认 max_wait_more=1，先观察实际命中模式再考虑放宽。
3. recovery 通道自身不会写 token 统计到 RunCommand / 主 counter，按设计
   独立计费；如需对账请在外部接入额外统计。
4. 当前支持 doubao_responses / openai_compatible；Claude messages API 后续再加，
   填错 backend 会在第一次调用时显式报错。
```

## 20.x 三家主 VLM backend 适配落地（2026-05-10）

doubao_responses 路线先行打通后，沿"高冗余 / 低耦合 / 不动主 VLM"原则把
recovery 通道扩展到 claude_cu / gpt_cu。所有改动只在 trajectory_cache /
recovery 模块及其测试中，主 VLM 与 assistants 完全不动。

```text
Layer A · trajectory 写库阶段
  - 改动：service.py::_ACTION_VERB_RE 扩为中英文双语动词集
    （新增 click/type/swipe/scroll/drag/long_press/double_tap/press/launch/
    open/close/wait/select/toggle/back/home/navigate 等英文动词）。
  - 原因：claude_cu / gpt_cu 主 VLM 输出的 thought 是英文（thinking + text
    拼接），原正则只识中文 + tap 单词，会让 _intent_from_thought() 在海外
    backend 下抽不到含动词的句子，trajectory 落库后 intent 字段是首句而非
    动作句。
  - 测试：tests/test_trajectory_cache.py::
    test_intent_from_thought_supports_chinese_and_english_verbs

Layer C · recovery_vlm 三家适配
  C-1 prompt 强约束：build_recovery_prompt 加一段"严格约束（针对 Claude /
      GPT 系模型常见误用）"，禁止 markdown 加粗 / 反引号 / 代码块包装
      Action 行；动作名保持英文 DSL。
  C-2 parse 预清洗：parse_recovery_response 入口先调用
      _strip_markdown_decorations，剥 **Action:**、`Action:`、```python
      Action: ... ```、列表项 `- Action:` 等装饰，然后再走 A.extract_actions
      原解析路径。三家偶发的装饰输出都能命中。
  C-3 coord_space 自动派发：CacheReplayRecoveryVerifier 接受
      ``main_vlm_backend`` 参数（service.py 实例化时传 settings.vlm_backend），
      ``coord_space`` 属性按 backend 派发：
        - doubao_responses / 默认 / 自部署代理 → "normalized" (0-1000 归一化)
        - claude_cu / gpt_cu               → "absolute"  (图像绝对像素)
      build_recovery_prompt(coord_space=...) 同步生成不同的"坐标系说明"段
      塞进 prompt；parse_recovery_response(coord_space=...) 解析后给所有
      ParsedAction.coord_space 显式覆写，下游 _parsed_point_to_abs 走对应
      分支（normalized → vlm_point_to_abs 反算；absolute → 直接 clamp）。
      未知 backend 兜底走 normalized，保护现网豆包行为。
  C-4 REPAIR_ACTION 注入：replay.py::_replay_action_from_parsed +
      _parsed_point_to_abs 的 absolute 分支已存在，本次落地时端到端
      verify：claude/gpt 路径 REPAIR_ACTION → 设备坐标的链路打通。
  C-5 absolute 坐标等比反算（关键 bug 修复）：模型看到的"附图 2"是
      driver.screenshot_jpeg(25, 720) 出来的 720 max-edge 压缩 JPEG，
      不是设备真实分辨率。claude/gpt 按这个尺寸输出 absolute 像素坐标，
      下游 _parsed_point_to_abs 直接 clamp 到 device window_size 会**坐标
      错位**。修复方案：
        - replay.py 加 _decode_image_size(jpeg_bytes) 用 PIL 读出实际 (w,h)
        - ReplayRunner._handle_alignment_miss 在每次 verify 前重置
          self._recovery_image_size 为当下截图的实际尺寸
        - _parsed_point_to_abs(absolute) 按 (img_size → device_size) 等比
          反算，img_size 缺失时退化为兜底 clamp（不破坏老 fixture 测试）
      这条 bug 在 v2 默认 OFF 状态下不会触发；只在用户主动开启 V2 + 海外
      模型时显形。回归测试：
        - test_decode_image_size_handles_jpeg_and_garbage（PIL 解码兜底）
        - test_replay_runner_recovery_repair_action_absolute_rescales_to_device
          （720x360 模型坐标 → 1000x2000 设备坐标等比反算）
  C-6 claude_messages backend（Claude 用户的 recovery 通道）：
      Anthropic /v1/messages 协议跟 OpenAI Chat Completions / 方舟
      Responses 都不兼容（headers: x-api-key + anthropic-version；多模态用
      ``{"type":"image","source":{"type":"base64",...}}``；响应在
      ``data.content`` 数组里）。recovery.py 加 _messages_double_image +
      _extract_messages_text；_chat_double_image 路由加 claude_messages
      分支。三家 backend 一一对应主 VLM：
        - 主 VLM=doubao_responses → recovery_vlm=doubao_responses
        - 主 VLM=claude_cu        → recovery_vlm=claude_messages
        - 主 VLM=gpt_cu           → recovery_vlm=openai_compatible（GPT
          Responses + Chat 都按 chat completions 形式吃 image_url，复用即可）
      .env / .env.example 注释同步三家 backend 可选值与对应 URL / model。
      回归测试：
        - test_recovery_extract_messages_text_concatenates_text_blocks
          （text + thinking 块拼接、空 / 损坏 / 缺字段兜底）
        - test_recovery_verifier_supports_claude_messages_backend（路由命中
          messages、双图传参、coord_space 自动派发 absolute、_chat /
          _responses 误调用断言）
        - test_recovery_verifier_unknown_backend_raises_with_three_options
          （报错信息列出三家可选 backend，方便用户自查 .env）

Layer D / E · 测试矩阵
  - parse_recovery_response 三家装饰样本单测：markdown bold / inline code /
    code fence / list prefix（4 个，全部 pass）
  - parse_recovery_response × 四种 verdict × absolute coord_space 矩阵
    （CONTINUE / WAIT_MORE / REPAIR_ACTION / ASSERT_FAIL，全部 pass）
  - verifier.coord_space backend 派发单测（doubao / claude_cu / gpt_cu /
    custom_proxy / 空 backend，全部 pass）
  - build_recovery_prompt 坐标系说明文案切换（normalized vs absolute，pass）
  - ReplayRunner 集成测试：absolute coord_space 下 REPAIR_ACTION 注入到
    driver.calls，断言 (540, 1024) 原样下发（不被 0-1000 反算成 (540, 1999)）

不动的边界（避免影响主 VLM 与豆包现网 / 海外 V1）：
  - vlm_loop / assistants / claude_cu / gpt_cu 主 VLM 路径全程不改。
  - 默认 coord_space 仍是 normalized，未传 main_vlm_backend 或 backend 字符
    串中不含 claude / 不以 gpt 开头时一律按 normalized 兜底，不会破坏豆包
    现网行为。
  - 海外 backend 接入后，recovery 通道直接输出 absolute 坐标，无需用户额外
    配置；如需强制覆写，后续可加 trajectory_cache_recovery_vlm_coord_space
    配置项做 override。
  - V1/V2 共用的 trajectory 写库阶段（service.py）：_ACTION_VERB_RE 扩了双
    语动词集，但 intent 字段只用于 UI/日志展示，不参与 cache_key 计算、不
    参与 dispatch、不参与断言；V1 海外回放路径**0 执行影响**。
  - V2 总开关：trajectory_cache_alignment_enabled / trajectory_cache_
    recovery_vlm_enabled，两个 .env 默认 false；现网海外 V1 用户不动
    env 时完全保持原 V1 行为，本次所有 V2 改动都不会触达 V1 路径。

遗留观察项（不在本次升级范围，留给后续决策）：
  - service.py::_action_from_parsed_raw 的 absolute 分支直接返回原坐标不
    缩放。该路径仅在 RunCommand 缺失（极少）时触发；主流程下 vlm_loop 已
    经把 claude/gpt 1344-缩图坐标反算到设备坐标存进 RunCommand。当前未触
    发回归，如未来扩展把该路径变成主链路，需要补 (1344-img → device)
    等比反算逻辑。
```

测试覆盖增量（共 +17 用例，trajectory_cache.py 累计 67 用例通过）：

```text
- test_intent_from_thought_supports_chinese_and_english_verbs
- test_parse_recovery_response_strips_claude_markdown_bold
- test_parse_recovery_response_strips_inline_code_keyword
- test_parse_recovery_response_strips_code_fence_wrapper
- test_parse_recovery_response_strips_list_prefix
- test_parse_recovery_response_overrides_coord_space_for_absolute
- test_parse_recovery_response_default_coord_space_is_normalized
- test_recovery_verifier_coord_space_dispatch_by_backend
- test_build_recovery_prompt_coord_space_block_switches
- test_parse_recovery_response_absolute_covers_all_four_verdicts[*]（4 个）
- test_replay_runner_recovery_repair_action_absolute_coord_space
- test_decode_image_size_handles_jpeg_and_garbage
- test_replay_runner_recovery_repair_action_absolute_rescales_to_device
- test_recovery_extract_messages_text_concatenates_text_blocks
- test_recovery_verifier_supports_claude_messages_backend
- test_recovery_verifier_unknown_backend_raises_with_three_options
```

## 21. 一句话定义

v2 不是把缓存回放变成“每步重新问模型”，而是给每个缓存 action 增加一个
“下一步执行前的整页状态路标”。

回放时：

```text
action 快速执行
  -> 延时观察
  -> 整页状态对齐
  -> 对齐则继续
  -> 不对齐则结合历史 gap 等待
  -> 仍不对齐才进入 VLM 专线
```

这样既保留缓存回放速度，也能更早发现偏航，并为后续清障、纠偏、跳步、局部重规划
留下足够证据。
