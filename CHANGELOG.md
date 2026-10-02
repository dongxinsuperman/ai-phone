# Changelog

本文只记录会影响部署、接入或排障口径的工程变化；细粒度代码历史仍以 Git commit 为准。

## Unreleased

### 豆包 V3 定位：回放内复用连接、传输失败后重建

- 豆包 V3 坐标定位在单次回放内使用独立 HTTP 客户端和连接池，空闲连接保留
  60 秒；回放成功、失败、取消或交回完整首跑时释放。不同 Run 不共享客户端；
  地址、鉴权或模型变化时丢弃旧客户端。外部注入的定位实例仍由调用方管理。
- 仅对 HTTP 传输错误关闭旧客户端并最多重试一次；原定位总超时预算仍覆盖
  初次请求和重试，不延长为两倍。若原预算已经耗尽，仍按原超时路径处理。
  HTTP 状态错误、模型解析失败及『无』不新增重试，不重复执行设备动作。
- 只复用网络连接，不带入响应 Cookie 或 `previous_response_id`；请求内容、
  `thinking=disabled`、主动缓存参数、截图、动作和等待语义不变。
- 主 VLM、辅助/救援、V1/V2 及海外定位请求未修改。无新增配置、依赖、数据库或
  对外字段；更新并重启空闲 Agent 后生效，Server 与提交方无需更新。

### Doubao V3 localization: replay-local connections and transport recovery

- Keep an independent HTTP client per owned replay locator, with a 60-second idle
  keep-alive expiry. Release it on success, failure, cancellation and restart handoff;
  replace it when endpoint, credentials or model changes. Caller-injected locators
  remain caller-owned.
- Rebuild and retry at most once on transport failures, within the unchanged outer
  localization timeout. Do not extend the deadline or retry HTTP status errors,
  invalid model output or target misses. No device operation is retried here.
- Preserve request payloads, disabled thinking, cache flags and screenshot/action
  semantics; do not introduce cookies or response-history chaining. Leave main VLM,
  auxiliary/rescue systems, V1/V2 and overseas locator transports unchanged.
- No new settings, dependencies, database/wire changes; requires an idle Agent restart,
  not a Server or caller update.

### V3 归档：整批语义清洗同时标记瞬态清障

- V3 在既有整批描述清洗请求中同时返回每条动作的瞬态清障分类，不再调用 V2
  逐条前后图片 classifier，也不上传新 V3 的弹窗对照图片。保留原动作 ID、顺序、
  执行参数和 Thought；结构错误仍在同一批内有限修正，校验完成前不写入部分结果。
- 仅对高置信、低风险且明确非业务的清障动作标记 `optional_ephemeral`；业务确认、
  登录/权限/安全、Case 要求及不确定分类保留 `business_required`。分类和原动作
  描述分别合回；同一首跑步骤的多动作共用 Thought 时不冒险标记可跳过。
- V3 使用独立的 `v3_ephemeral.py` 规则、gate 提示词和解析器，由 V2 逻辑复制后
  独立维护，而非共用 V2 业务函数。新标记以当前回放截图确认弹窗是否缺席及后续
  是否可衔接，不能凭历史标签直接跳过；旧 V3 缓存的前后图路径与缺图保守行为保留。
- 明确救援模型的通用救援身份：定位失败时恢复可续接缓存的状态，不机械重复动作，
  不重规划完整 Case。不增加业务专用路线、坐标规则或模型调用次数。
- 沿用既有开关、模型、推理强度、超时和救援预算；V1/V2、首跑与最终断言不变。
  无新增依赖、数据库迁移或外部请求字段。新语义标记存于既有 `ephemeral_meta`，
  更新并重启空闲 Agent 后生效；旧 Agent 缺对照图时仍走原动作重定位保守路径。

### V3 archive: combine intent cleaning and semantic popup tagging

- Classify incidental popup cleanup in the existing batch text-cleaning request. Remove
  V3's per-action image classifier calls and new popup-image uploads; preserve action IDs,
  order, parameters and Thought, with the same bounded whole-batch validation/repair.
- Keep risky, ambiguous and Case-required operations mandatory. V3 owns copied popup
  acceptance rules, gate prompts and parsing independently of V2 business implementations;
  only the existing low-level model transport/configuration is reused.
- New semantic tags require current-frame gate evidence before skipping. Keep legacy V3
  image-based cache handling and conservative missing-image behavior. Clarify the generic
  rescue role without adding business-specific navigation or fixed gestures.
- No new settings, dependencies, database migrations or external request fields. Preserve
  V1/V2, first runs, final assertions, models and budgets. Requires an idle Agent update/restart;
  older Agents conservatively relocate the original action when comparison images are absent.

### V3 救援输出：对齐已有执行能力

- 用同一份动作契约生成救援提示中的完整 JSON 动作 schema，并在解析与执行前
  校验。只声明既有 `click/double_tap/long_press/drag/wait/press_back/press_home`，
  明确移动端滑动应输出 `drag` 的 `start/end`，不使用未约定的 `swipe/scroll/tap`。
- 模型仍根据当前截图自主选择动作与坐标；不固定方向、位置或百分比，不静默将
  未支持动作改名，也不补缺失参数。非法提案记为协议错误，不当成有效放弃裁决；
  原救援预算、失败策略、首跑降级条件、主模型与 V1/V2 行为不变。
- 日志区分尚未执行的原始救援提案与实际下发动作，便于查明名称/字段不匹配。
  旧内部 `action` 名和数组点位保留兼容。无新配置、依赖、数据库或对外字段，
  更新并重启空闲 Agent 后生效。

### V3 rescue output: align with existing execution capabilities

- Generate explicit JSON repair schemas from the same contract used by parsing/execution
  checks. Declare the seven existing local operations; scrolling uses drag start/end,
  not undeclared swipe/scroll/tap names.
- Keep model-selected gestures/coordinates, budgets, failure/restart policies, first runs,
  and V1/V2 unchanged. Do not rename unsupported proposals or invent parameters.
- Log unexecuted proposals separately from executed operations. Preserve legacy internal
  action-name/array-point input compatibility; no new settings, dependencies, or wire fields.
  Requires an idle Agent update/restart.

### 修复正常 VLM 首跑入口的缓存模式作用域错误

- V3 完整重跑分支给 `cache_mode` 赋值时误将其遮蔽为内层局部变量，导致未命中
  缓存的普通 VLM 首跑（包括关闭缓存）在进入模型前触发 `UnboundLocalError`。
  纠正为读取/更新本次任务外层的缓存模式，不改变首跑动作、模型、协议或缓存策略。
- 补充真实 Agent `start_run` → 主执行器的未命中入口回归，覆盖旧请求未提供
  `cache_mode` 与 `off/v1/v2/v3`，验证正常执行、单次终态及原归档模式。
  更新并重启空闲 Agent 后生效；Server、提交方和数据库无需调整。

### Fix cache-mode scope in the normal VLM first-run entry

- Correct the nested task's cache-mode binding introduced by the V3 restart branch.
  Cache misses and cache-off Runs can start the model instead of failing before execution.
  Preserve first-run semantics, models, protocols, and cache policies.
- Add actual Agent start-run/executor integration coverage for absent and off/v1/v2/v3
  cache modes, including terminal and archive behavior. Requires an idle Agent update/restart;
  no Server, caller, or database changes.

### V3 缓存共享：平台族 + Case 原文哈希

- 新 V3 成品由 Server 按平台族与 Case 原文哈希统一生成共享 key，同端不同设备
  可复用；Android、iOS、鸿蒙互相隔离，`ios_sim` 与 iOS 真机同属 iOS。
  哈希沿用完整 Run goal 的既有空白规范化口径，不使用 Case ID 或设备标识，
  也不把运行期 Map 新加入 key。设备、分辨率、首跑 Run 仍保留用于来源追溯。
- 回放使用当前承载设备与截图，复用既有 V3 重定位、救援、最终断言和失败首跑
  降级；不修改 Case/Map 输入、固定输入内容、App 参数或首跑执行规则。
  同一原文配不同 Map/账号的任务不会因此自动改写首跑留下的固定输入参数。
- 混合版本保守兼容：Agent hello 新增可选共享能力声明；旧 Server 忽略该字段。
  新 Server 仅向已声明能力的 Agent 下发平台共享缓存；旧 Agent 保持设备缓存，
  无共享命中则正常首跑。新归档用现有 meta 标记共享，不改缓存 schema/表结构。
  未知平台退回设备级缓存，旧缓存仍仅供原设备命中，不批量迁移或自动提升共享。
- V3 原子 upsert 处理多设备同时首跑成功的同 key 写入。共享成品带内部版本号；
  派发时在现有 RunLog 记录本 attempt 实际命中的 key/版本（包括未命中）。
  suspect/失败删除只作用于该版本，不误伤另一设备新写入的成功缓存；记录缺失
  或异常时不猜测删除共享缓存。不改变正常回放成功后“不重建缓存”的策略。
- 仅改变 V3 绑定和可选兼容信息，V1/V2、用户接口、模型/思考配置、数据库列与
  依赖不变。需更新 Server 与相关 Agent 后启用跨设备共享；可分批更新，不要求
  用户修改提交代码。模拟数据库/派发/回放回归不等同于真实设备跨机验收。

### V3 cache sharing: platform family + normalized Case-text hash

- Compute shared V3 keys on the Server from platform family and the existing full-goal
  semantic hash. Separate Android/iOS/Harmony; fold iOS simulators into iOS. Keep source
  devices, resolutions, and Runs as provenance, not binding. Do not add Map or Case IDs
  to the hash or dynamically rewrite cached fixed input/application parameters.
- Reuse current-device relocation, rescue, assertion, and full-run fallback. Preserve
  first-run semantics and the policy of not rebuilding caches after successful replay.
- Add optional Agent hello capability and archive-meta scope markers. Old Servers ignore
  additions; new Servers only dispatch shared caches to capable Agents. Keep legacy caches
  device-bound without bulk migration; unknown platforms remain device-bound.
- Use atomic V3 upsert for concurrent first runs. Persist the exact selected generation
  per Run attempt in existing logs; late failures cannot invalidate newer successful caches.
  Missing/corrupt bindings do not authorize deleting shared caches.
- Preserve V1/V2, user-facing contracts, settings, models, database schema, and dependencies.
  Sharing requires updated Server and Agents, supports staged updates, and needs no caller
  changes. Simulated regressions are not physical cross-device acceptance.

### V3 滑动回放：根据当前截图重定位操作区域

- `scroll` 接入现有 V3 定位与救援链路，根据缓存语义和当前截图重新取得滑动中心，
  不再直接执行首跑设备的历史中心坐标；没有 center 的旧缓存也重新定位。
  保留原动作类型、浏览方向与次数，不改写为拖拽，不新增固定中心或比例限制。
- 清洗提示词保留已明确的滚动对象/区域，不捏造区域；历史坐标仍作为首跑记录保存，
  不是回放依据。报告与本轮断言历史记录新取得的实际中心，缓存原数据不被覆盖。
- 定位失败复用既有救援/退出缓存处理，不降级照搬旧坐标。允许同一区域往返滑动
  使用相同中心，不误用点击目标的重复点判断；保留既有屏幕边界校验。
- 仅改变 Agent 的 V3 滑动定位及后台语义清洗提示；首跑、V1/V2、驱动手势、
  外部协议、缓存 key/schema、模型配置与数据库不变。无需用户修改 Case/Map；
  更新相关 Agent 后生效，每个回放滑动会增加一次现有定位模型请求。

### V3 scroll replay: relocate the operation region from the current screenshot

- Route scroll actions through the existing V3 locator/rescue path. Replace historical
  centers with current-image coordinates, including legacy actions without a center.
  Preserve action type, browsing direction, and amount; no fixed centers or new ratio rules.
- Keep explicitly described scroll regions during intent cleaning. Retain source coordinates
  for provenance only; report actual relocated centers without mutating the cache.
- Reuse existing miss/rescue handling rather than executing stale coordinates. Allow repeated
  centers for scrolls within the same region; retain existing screen-boundary validation.
- Preserve first-run execution, V1/V2, driver gestures, public protocols, cache keys/schema,
  models/settings, and storage. No Case/Map changes required. Updated Agents perform one
  existing locator request per replayed scroll.

### V3 救援失败：退出缓存并完整重新首跑

- Agent 在 V3 救援预算耗尽或模型明确 `GIVE_UP` 后，将旧缓存标为 suspect，
  仅在当前任务内转一次完整首跑。使用原始 Case 与 Map，新建主 VLM 执行器与
  记录器，不续接旧缓存进度，不合并缓存片段与新轨迹，也不递归重入缓存回放。
- 复用原首跑的前置准备、子步骤、执行与断言，不修改首跑源码/System/模型配置，
  不额外强制清数据或重置设备。新阶段按现有首跑成功标准完成且具有可回放动作时，
  才由原后台归档生成新缓存；失败或取消不归档。正常缓存回放成功仍不重建缓存。
- 缓存阶段不提前发 Run 终态或触发收尾熄屏；保持同一设备占用直到新首跑结束，
  最终只发一条结果。模型内部步骤重新开始，报告序号在缓存阶段之后连续排列，
  新缓存的 action index 从1生成、source_step 对应实际报告位置。
  最终报告耗时包含两阶段，新阶段日志注明完整首跑阶段耗时。
- 仅预算耗尽/有效放弃裁决触发该转换；模型调用异常、非法裁决、设备执行错误、
  取消和最终断言失败沿用原处理，不盲目重新首跑。此本地缓存降级不改变 Server
  的外部 retryMax/attempt/TTL 或取消规则，不新增协议字段、配置、数据库迁移或依赖。
  旧内部回放调用默认仍发原终态；正式 Agent 编排显式启用新转换，V1/V2 不变。
- 更新相关 Agent 并在空闲时重启后生效；新行为可能增加异常任务耗时和模型调用，
  模拟回归不代表真实设备已完成该流程验收。

### V3 rescue failure: leave cache replay and restart the complete first-run flow

- After rescue exhaustion or an explicit valid `GIVE_UP`, invalidate the old cache and
  restart the original Case/Map once inside the Agent task. Create fresh executor/recorder
  instances; do not resume cache progress, stitch trajectories, or recurse into cache replay.
- Reuse unchanged first-run preparation, substeps, execution, and assertion. Do not force
  extra data clearing/device resets. Only successful new-stage records with replayable actions
  can produce a new archive; failure/cancellation cannot. Normal replay success still reuses
  its cache without rebuilding it.
- Keep one device lease and one terminal result/power hook. Model-local steps start fresh;
  report steps are offset after the cache phase, while new action indices start at 1 and
  source-step metadata matches the report. Report elapsed time includes both phases.
- Do not apply this restart to provider/protocol/device failures, cancellation, or final
  assertion failure. Preserve external Server retry/attempt/TTL policies and V1/V2 behavior.
  No new wire fields, settings, database migration, or dependencies. Legacy internal replay
  callers retain their terminal behavior; formal Agent orchestration opts into the transition.
- Update/restart relevant idle Agents to activate. Exceptional tasks may cost more time and
  model calls; simulated regressions are not real-device acceptance.

### V3 最终断言：单向对齐首跑的证据语义

- 首跑断言代码、System、核心两层规则、模型与思考强度保持不变。V3 本轮摘要
  使用首跑现有的 100 步覆盖上限，按最后 100 个缓存步骤分组保留全部已记录的
  缓存动作、救援、等待、跳过与异常，不把 100 步误作 100 行。
- V3 断言不再传入首跑历史完成日志、最后思考和通过理由，也不再优先采用历史
  成功解释；本轮仍按原始用户目标与当前有效证据裁决。仅移除断言输入中的旧说明，
  缓存数据本身与 schema 不变，旧缓存仍可读取。
- 纠正 V3 图片说明：前图是最后缓存步骤开始前、后图是回放结束后的画面，期间
  可能包含局部修复、等待、原动作或跳过，不能假定只跨一个物理动作；单图模式
  明确其为唯一最终画面。只改变证据说明，不改变图片采集或回放执行。
- V1/V2 的既有摘要窗口和提示词保持不变；PASS/FAIL/SKIP 协议、异常收尾、
  缓存删除策略与超时配置不变。本次规则对齐不代表已经验证与首跑同等准确率。

### V3 final assertion: align evidence semantics with the unchanged first-run baseline

- Keep first-run assertion code, System, shared two-layer rules, models, and reasoning
  unchanged. Apply its existing 100-step coverage limit to V3 cache-step groups, retaining
  all recorded cached actions, repairs, waits, skips, and errors within those steps.
- Exclude historical first-run completion claims/thoughts/PASS reasons and their priority
  from V3 assertion input. Judge the original goal against current evidence; stored cache
  data and schema remain compatible and unchanged.
- Describe the before image as the start of the final cache step and the final image as
  replay completion. The interval can contain multiple operations or a skipped action;
  clarify single-image mode without changing image capture or execution.
- Preserve V1/V2 prompts/windows, result protocols, failure handling, cache-deletion policy,
  and timeouts. Semantic alignment alone is not proof of equivalent real-world accuracy.

### V3 可选弹窗动作：接通首跑标记与回放证据

- V3 首跑后台归档复用 V2 的保守弹窗分类器，仅将高置信度、低风险、非业务必需
  的清障动作标为 `optional_ephemeral`。支付、登录安全、必要权限或用例目标相关
  动作仍按既有分类规则保留为必执行动作，不更改动作类型、参数或顺序。
- 仅为被标记动作上传首跑前后两张证据图；不生成 V2 路标或引入 pHash 对齐。
  缺截图、分类不可用、上传失败或同一步包含多个无法独立归属的动作时，保留
  `business_required`。分类仍在首跑结束后的后台执行，不增加手机动作。
- V3 回放复用现有图片预取和 gate：由模型结合当前图及首跑两张证据图判断跳过、
  执行原动作或局部修复，不要求新旧截图相同。取图失败沿用原 gate 的缺图降级。
- 复用原开关、模型与 gate 预算，不增加客户端字段、数据库迁移或缓存 schema。
  旧缓存仍可读取，但不会自动补造缺失标签或图片；需新首跑归档才能得到新标记。

### V3 optional popup actions: connect first-run tags and replay evidence

- Reuse V2's conservative classifier during asynchronous V3 first-run archival. Only
  high-confidence, low-risk, non-business cleanup actions may become `optional_ephemeral`;
  existing business/security/permission/Case-related veto rules still apply.
- Upload before/after evidence only for marked actions, without V2 landmarks or pHash
  alignment. Missing evidence, unavailable classification/upload, and ambiguous multi-action
  frames retain `business_required`; runtime action types, parameters, and order do not change.
- Reuse image prefetch and the existing gate to judge skip/original/repair from current and
  historical popup evidence, not pixel equality. Keep existing missing-image fallback.
- Reuse existing switches, models, and gate budgets; no client fields, database migration,
  or cache schema change. Old caches stay readable, but tags/evidence require a new first-run
  archive rather than fabricated historical data.

### V3 局部救援：承接本次业务上下文与连续修复记录

- 将现有 `trajectory_cache_v3_rescue_max_calls_per_replay` 的默认值由 3 提高为
  10，公开默认配置与完整配置示例同步。整条回放共用调用预算，一次仍最多执行
  一个修复动作；等待裁决同样计数，显式配置优先，不修改 V1/V2 或 gate 配额。
- 沿用已有 Run 的完整目标与 `function_map_context` / `functionMapContext` 字段，
  将本次 Map 传给 V3 救援模型；保留既有 Map 开关，不新增外部提交字段、配置或
  缓存 schema。旧调用或旧消息没有 Map 时仍可运行。
- 连续救援携带当前缓存步骤内已经执行的修复动作、等待、调用状态和理由，
  同时提供最新截图与最新定位失败原因；换到另一缓存步骤后重新开始局部历史。
  历史坐标明确标为设备像素，不作为当前落点照搬；调用完成不等于业务成功。
- 复用已有本轮执行记录，不新增会话存储或历史截图续接。Map 不写回缓存，也不
  注入常规定位或最终断言；不调整模型配置、接管策略或成功后缓存重建。

### V3 local rescue: carry current business context and repair history

- Raise the existing per-replay rescue-call default from 3 to 10 and align public
  defaults/configuration examples. The entire replay shares the budget; each call still
  produces at most one repair action, and wait decisions also count. Explicit settings
  take precedence; V1/V2 and ephemeral-gate budgets remain unchanged.
- Forward the existing Run goal and Map to V3 rescue, honoring the existing Map switch.
  No external submission fields, settings, or cache schema changes are introduced;
  older callers/messages without a Map still work.
- Include executed repairs/waits, call states, and reasons for the current cache step,
  with the latest screenshot and locating failure. Reset local history for the next step.
  Mark historical coordinates as device pixels, not reusable current targets; completed
  calls do not by themselves prove business success.
- Reuse current-run records without adding session storage or chained historical images.
  Do not persist the Map in caches or inject it into regular locating/final assertion.
  Model configuration, takeover policy, and successful-replay cache reuse
  remain unchanged.

### V3 回放：动作承接一致性与本轮断言证据

- V3 保持语义重定位机制：需要坐标的动作在当前截图上定位，并保留源动作的
  非坐标参数。输入确认目标可见后直接承接已激活输入框，不再隐式增加聚焦点击；
  原轨迹显式点击仍保留。弹窗 gate 和救援策略、预算与衔接规则不变。
- 首跑归档补齐实际发生的瞬态工具栏重唤起点击，使用触发目标自己的语义与
  设备坐标；等待记录实际执行秒数，并保留截图参数。V3 长等待不再额外截为
  60 秒，而使用首跑已有的 `run_max_wait_sec` 上限；V1/V2 仍保持原等待上限。
- 最终断言原本已重新调用模型，本次修正的是证据来源：V3 传入本轮重新定位后的
  动作、救援、等待、跳过和异常状态，不再把缓存计划当作执行事实。
  保留最后 100 个缓存步骤及这些步骤中的全部已记录操作；无异常只证明调用完成，
  不证明 UI 业务成功。当前截图仍是当前结果证据，非 PASS 的处理策略不变。
- V3 缓存 schema 和外部接口不变，旧缓存仍可读取；旧记录里漏掉的信息不会被
  自动补造，新首跑归档才会写入补齐的信息。无需数据库迁移或客户端代码调整；
  已运行的相关 Agent 需更新代码并在合适时机重启，才能使用新的归档与回放逻辑。

### V3 replay: preserve source actions and use current-run assertion evidence

- Keep semantic relocation on current screenshots and preserve non-coordinate source
  parameters. Input confirms the target is visible and types into the already active
  field without adding an implicit focus tap; explicit source taps remain. Ephemeral
  gates, rescue budgets, and handoff policies are unchanged.
- Record actual first-run transient-toolbar retrigger taps with their own semantics and
  native coordinates, actual wait seconds, and screenshot parameters. V3 uses the existing
  first-run `run_max_wait_sec` limit instead of an additional 60-second cap; V1/V2 keep
  their previous wait limit.
- Final assertion was already fresh. Replace its cached-plan summary with current-run
  relocated actions, repairs, waits, skips, and execution states, retaining the last 100
  cache steps and their recorded operations. A completed call does not prove UI success;
  current screenshots remain current-state evidence and non-PASS handling is unchanged.
- Cache schema and external interfaces stay compatible. Missing historical information
  is not fabricated; complete new records require a new first-run archive. No database
  migration or client changes are needed. Update and safely restart relevant Agents to
  activate the new archive/replay behavior.

### V3 缓存生成：整批语义清洗与完整性校验

- 将每个动作分别请求模型的清洗流程改为整批生成 `plan_intent`；每条仍独立描述
  首跑真实行为，不重新规划、合并或优化路线。执行类型、顺序、输入内容、包名和
  其他执行参数由程序保留，模型只返回原动作 ID、描述、置信度与理由。
- 程序校验 JSON 结构、字段、动作 ID 的完整性与唯一性，并按首跑 ID 顺序合回；
  缺项、重复、未知 ID、重复 JSON 键等错误反馈模型修正。最多首次生成加两次
  修正请求，每次沿用既有超时设置；默认单次上限 300 秒，总预算最多 900 秒。
- 校验全部通过后再合回描述；空描述或与既有规则冲突时仍保留原规则候选。
  最终结构不合规时不上传本次新缓存，不改变已完成 Case 的结论。
  未配置清洗模型时仍使用规则候选；旧逐条模型调用入口已移除。
- 清洗仍在首跑结束后的后台归档中执行，不把缓存生成耗时算成手机执行提速。
  V3 缓存 schema 仍为 3，现有缓存无需清空，外部 Case/Map/Submission 接口不变。

### V3 cache generation: batch semantic cleaning with integrity validation

- Replace serial per-action model calls with batch `plan_intent` generation. Describe
  each actual first-run action independently without replanning or optimizing the route.
  Runtime retains action types, order, input contents, packages, and execution parameters;
  model output contains only original action IDs, descriptions, confidence, and reasons.
- Validate JSON structure, fields, complete/unique IDs, and duplicate JSON keys before
  merging in source order. Feed errors back for at most two corrective calls after the
  initial request. Existing per-call limits apply: 300 seconds by default, at most 900
  seconds for the entire generation/repair budget.
- Preserve rule candidates for empty or rejected descriptions. Invalid final structure
  prevents uploading this new cache without changing the completed Case verdict.
  Unconfigured cleaners still use rule candidates; the serial model entry point is removed.
- Generation remains asynchronous after first-run completion. Cache schema stays at 3;
  existing caches and external Case/Map/Submission interfaces remain compatible.

### 豆包 GUI Agent：主执行与缓存恢复统一为 Seed XML 协议

- 豆包主 VLM 的动作输出由历史 `Thought: / Action:` 调用式 DSL 升级为
  `Thought + <seed:tool_call>` 文本 XML；请求仍使用 Responses API 且不传
  `tools`，动作由官方 `ui-tars==0.5.1` 解析后转换为项目统一的
  `ParsedAction`。Case、Function Map、Driver、Submission、数据库、WS、报告与
  回调接口均不变，外部调用方无需修改代码。
- System Prompt 现在提供可直接复制的官方 XML、`parameter`、`string=true/false`
  与 0-1000 坐标示例，同时保留滚动步幅、等待、截图保存、长按和终态证据等
  ai-phone 行为规则。坐标、等待范围、必填参数、单 function 和动作白名单均在
  解析层再次校验；非法输出只在同一截图纠正重试一次，不执行半截动作。
- 豆包轨迹缓存 recovery 与主执行统一使用 Responses + Seed XML。一次缓存 action
  的首轮携带完整恢复规则、固定 handoff 图和当前图；后续通过
  `previous_response_id` 只发送最新当前截图和本轮变化，并明确旧当前图已失效。
  action 变化或缺少稳定身份时自动切断 recovery 会话，避免路标和截图串线。
- Recovery XML 仍转换成既有 `ParsedAction` 后交给 ReplayRunner；现有缓存记录、
  回放动作、可读日志和报告中的 `click(...)` 等兼容格式保持不变。V3 定位继续输出
  坐标标签，V3 救援和瞬态弹窗 gate 继续输出内部 JSON，未被强制改成动作 XML。
- 豆包开源示例默认模型更新为主执行 `doubao-seed-evolving`、辅助系统
  `doubao-seed-2-1-turbo-260628`。主执行、recovery、V3 定位和门控均从同一
  `PHONE_VLM` 配置派生到 `/responses`；辅助判断仍从独立 `AUX` 配置派生。
- 新增 `ui-tars==0.5.1` 运行依赖。升级后按原方式安装
  `backend/requirements.txt` 或项目依赖即可；豆包用户仍需在方舟控制台为模型开启
  上下文缓存，项目不会新增提交字段或部署端口。
- 明确移动端动作语义但不重写模型决策：`scroll.direction` 表示想浏览的内容方向，
  `down` 看下方、`up` 看上方或回顶；`drag` 仅表示把具体对象从起点拖到终点。
  执行层按模型返回的原始方向与坐标操作，下一轮由模型根据新截图自行调整方向、落点
  或动作类型。没有新增安全区硬编码，也不改变 Driver、Case、Function Map 或外部接口。

### Doubao GUI Agent: unify main execution and cache recovery on Seed XML

- Replace the legacy Doubao `Thought: / Action:` call-style DSL with textual
  `Thought + <seed:tool_call>` XML. Requests still use the Responses API without
  the `tools` parameter. Official `ui-tars==0.5.1` parsing is adapted into the
  existing `ParsedAction` contract, so Cases, Function Maps, Drivers, Submissions,
  storage, WebSocket events, reports, and callbacks remain source-compatible.
- Add copyable XML, parameter typing, and normalized-coordinate examples while
  retaining ai-phone behavior rules for scrolling, waiting, screenshots, long press,
  and completion evidence. Runtime validation enforces coordinates, wait bounds,
  required parameters, the one-function recovery contract, and model-visible action
  allowlists. Invalid output receives one same-frame correction retry and is never
  partially executed.
- Move Doubao trajectory-cache recovery to Responses + Seed XML as well. The first
  turn for one cached action sends the full policy, fixed handoff image, and current
  image. Follow-up turns reuse `previous_response_id` but send only the latest current
  image and changed evidence, explicitly invalidating the previous current image.
  Changing actions—or lacking a stable action identity—resets the recovery session.
- Keep external cache and replay contracts unchanged: recovery XML becomes the same
  `ParsedAction` consumed by ReplayRunner, while stored actions and human-readable
  reports retain their compatible `click(...)` form. V3 locating remains coordinate
  output; V3 rescue and ephemeral gates remain internal JSON protocols.
- Update open-source Doubao examples to `doubao-seed-evolving` for phone
  execution and `doubao-seed-2-1-turbo-260628` for auxiliary judgments. Phone-side
  execution, recovery, V3 locating, and gates derive from one `PHONE_VLM` Responses
  connection; non-device judgments continue to use the independent `AUX` connection.
- Add the `ui-tars==0.5.1` runtime dependency. Normal dependency installation is
  sufficient; no new submission fields, ports, or client changes are required.
- Clarify mobile action semantics without rewriting model decisions:
  `scroll.direction` expresses the content browsing direction (`down` reveals lower content;
  `up` reveals upper content or returns to the top), while `drag` moves a concrete object
  between explicit endpoints. Runtime executes the model's original direction and coordinates;
  the model uses the next screenshot to adjust its direction, point, or action type. No safe-zone
  constants, Driver changes, Case/Function Map changes, or client API changes are introduced.

### 辅助模型：统一默认超时与豆包推理强度配置

- 最终断言默认超时由 120 秒延长为 300 秒；审判、缓存恢复、V3 定位与救援、
  瞬态分类与 gate，以及辅助分析的默认请求超时也统一为 300 秒。最终断言的
  HTTP 层读取同一个既有超时字段，避免外层允许等待、内层却提前超时。
  显式配置仍优先；延长的是等待上限，不是强制每次等待 5 分钟。
- 新增可选 Settings 字段 `aux_reasoning_effort`，默认 `high`，支持
  `low` / `medium` / `high`，空值表示继承模型默认。仅在豆包 AUX 请求已开启
  thinking 时发送档位；不改变原思考开关、主 VLM 或海外模型推理策略。
- 沿用现有 Server → Agent 配置下发机制；新 Agent 接收旧配置时使用默认值，
  旧 Agent 忽略未知可选字段。不新增数据库迁移、依赖或必填客户端字段。
  首跑断言超时后的 `SKIP` 回退及缓存回放非 PASS 的处理策略保持不变。

### Auxiliary models: align timeout defaults and Doubao reasoning configuration

- Increase the final assertion default from 120 to 300 seconds, and align auxiliary
  analysis, audit, cache recovery, V3 locating/rescue, and ephemeral classification/gate
  defaults to 300 seconds. Assertion HTTP calls use the existing outer timeout setting.
  Explicit settings still win; this raises the limit rather than forcing a five-minute wait.
- Add optional `aux_reasoning_effort` to Settings, defaulting to `high`, with
  `low`/`medium`/`high` and an empty value for the model default. Apply it only to
  Doubao AUX calls whose existing thinking switch is enabled. Main VLM thinking
  and overseas-provider reasoning policies are unchanged.
- Reuse runtime configuration distribution: new Agents default the missing field,
  and older Agents ignore unknown optional fields. No database migration, dependency,
  or required client field is added. Existing first-run assertion `SKIP` fallback and
  non-PASS cache-replay handling remain unchanged.

### 批次单 Case 超时：补齐 Agent 已无 Run 时的自动收口

- 沿用 `AI_PHONE_ITEM_TTL_SEC`（默认 1 小时）作为 SubmissionItem 的硬上限；
  到期先标记 `run_timeout`，再向 Agent 下发停止，不新增第二套超时口径。
- 断线导致 run 路由丢失时，仅对 scheduler 批次任务按 `Run.agent_id` 补投停止；
  Agent 本地已无该 Run 时通过既有可靠队列回 `run_done(cancelled)`，不再沉默挂起。
- `run_timeout` 成为显式禁止重试的终态分支，不依赖 Agent 的结果字符串；若真实成功
  结果先到则仍保留成功，后到的“不存在”回复由既有幂等逻辑跳过。
- 不改工作台/API 手动任务的一小时规则和浏览器锁。旧 Agent 不回“不存在”终态时
  Server 不会无确认强制释放，残留继续显式暴露。

### 最终断言：按证据职责综合截图与动作历史

- 豆包、Claude 和 OpenAI 辅助模型的断言 System 从“严格保守”改为结果导向：
  有效证据能合理支持任务结果时 PASS，只有任务要求与证据明确矛盾时 FAIL。
- 结构化断言明确拆分动作历史：Runtime 动作记录只证明明确写出的调用状态，不证明
  UI 业务结果；历史 thought、最后 thought 与 finished 均属主 VLM 自述，不能作证。
- 三家辅助模型共用一套中英双语 System 证据契约；User Prompt 只保留本次字段说明和
  任务类型规则，避免两层重复维护后口径漂移。
- 两图相同不再自动构成 FAIL；最后动作必须产生可见变化但最终图仍无目标状态时，
  可直接驳回，不再依赖主 VLM 自述。自由模板恢复只验最后动作/最终状态，缓存回放
  同步采用相同证据边界。
- PASS/FAIL 协议、API、图片传输和现有 SKIP 兜底不变：配置缺失、调用失败或协议
  无法解析时，仍记录原因并采纳主 VLM 的 finished。

### Final assertion: evaluate screenshots and action history by evidence role

- Doubao, Claude, and OpenAI assertion systems are now result-oriented instead of
  defaulting to strict conservatism: PASS when valid evidence reasonably supports the
  result, and FAIL only on a clear conflict between the requirement and the evidence.
- Assertion history now separates Runtime action records from VLM statements. Runtime
  records establish only their explicit call status, not a successful UI outcome; historical
  thoughts, final thought, and finished text are all non-evidentiary VLM statements.
- All three assistant providers now share one bilingual System evidence contract. The User
  prompt keeps only field mapping and task-specific rules to prevent policy drift.
- Identical before/after images are not an automatic failure, and visible-change failure no
  longer depends on a VLM success claim. Freeform templates remain scoped to the last action
  or final state. Cache replay follows the same boundary; the existing SKIP fallback remains.

### Function Map：从 System 指令降为首轮 User 业务执行上下文

- `functionMapContext` 字段继续保持非必填；未提供时，主 VLM 仍完整依靠 Goal、
  子步骤与当前截图执行，不触发降级或另一套兜底逻辑。
- 提供 Map 时，正文不再进入 System Prompt，而是在每个逻辑会话段的首条 User
  消息中注入一次；正常轮次不重复，豆包会话熔断重置后自动重新注入。
- System 只保留 Map 使用契约：Map 在页面关系、对象、路径、测试数据、业务术语与
  异常处理范围内高权重参考，优先于模型常识和无依据猜测；但不能新增任务、跨越或
  重排子步骤、替换明确测试对象、改变预期结果或充当完成证据。
- 豆包、Claude Computer Use 与 GPT Computer Use 均使用独立首轮上下文字段，
  不复用临时纠偏 hints；外部 API、数据库、WS、包名匹配、审判、最终断言与报告
  数据结构保持不变。Map 原文仍不进入 RunLog、RunStep 或 HTML 报告。

### Function Map: move the body from System to first-turn User context

- `functionMapContext` remains optional. Runs without a Map keep the complete Goal +
  substeps + screenshot execution path, with no hidden fallback or degraded mode.
- When supplied, the Map body is injected once in the first User message of each logical
  session segment, and is re-injected after a session reset instead of being repeated every turn.
- The System prompt now contains only the usage contract: give the Map substantial weight
  for business execution knowledge, while preventing it from changing the task, substep order,
  explicit test object, expected result, or completion evidence.
- API/storage/reporting compatibility is unchanged, and the raw Map remains excluded from
  RunLog, RunStep, and generated HTML reports.

### 结构化用例：子步骤按完整任务语义拆解

- 子步骤模型直接读取完整 goal，自行识别「操作步骤：」「[操作步骤]」、编号列表等
  不同写法，不再依赖本地关键字与冒号格式截取正文。
- 自然段、换行、原有编号、标点和动作语义均可作为边界信号，不再把某几种中文标点
  固定为必须切分的硬边界；输出保持原文措辞，不做优化、润色或内容增删。
- 模型直接生成一层连续编号清单，Runner 不再二次编号；豆包、Claude 和 GPT 均以
  注入清单的编号作为唯一子步骤边界。

### 结构化用例：移除固定步数周期巡检

- 删除每执行 N 步主动召唤审判的周期巡检，以及巡检专用的步骤重拆分和判决提示词；
  审判只在本地异常探测器发现同坐标反复点击、屏幕重访、滑动震荡或滑动无进展时触发。
- 子步骤软约束、异常探测器审判、最终断言和 `max_steps` 安全上限均保持不变。
- `AI_PHONE_AUDIT_PERIODIC_INTERVAL` 不再出现在默认配置和示例配置中；混合版本部署时
  Server 仍按历史默认值 `30` 下发该字段：新版 Agent 忽略它，未升级 Agent 继续按旧逻辑
  运行。全部 Agent 升级后可以从部署环境中删除该变量。
- 行为变化：正常长任务不会再在固定步数被主动打断；相应地，路径虽然偏离但未触发任何
  本地异常特征时，不再有周期审判介入。周期巡检没有隐藏替代入口。

## 0.7.0 - 2026-08-02

### iOS 虚拟机（Simulator）完整接入（`main` 独有）

- 新增与 iOS 真机完全隔离的「iOS 虚拟机」配置页、API、数据库表、Agent Manager、
  端口域和生命周期状态机；启动后带 `virtual` 标识进入统一设备池，复用 iOS 真机的
  工作台、镜像、调度、执行和报告链路。
- 支持按设备类型（iPhone / iPad）、机型和官方支持的系统版本选择配置；支持 Agent
  能力探查、下发、启动、停止、复制、删除、换 Agent、重连认领和孤儿实例对账。
  生命周期语义与 Android 逐环节对齐。
- 机型目录随 Server 内置发布，来自 Xcode 官方 `simctl` 导出，不依赖某台 Agent
  当前装了什么；某台机器实际能起哪些组合由该 Agent 的能力探查确认。
- 支持向 iOS 虚拟机分发应用：识别 `.zip` 内的 `.app` 包并经 `simctl install` 安装。
  与真机 `.ipa` 是两条独立线路；**虚拟机不需要签名与开发者证书**。
- **数据库迁移（部署需执行）**：`backend/migrations/ios_sim_v1.sql`。
- Agent 宿主准备见
  [`docs/agent-ios-sim-vm-env-setup（Agent iOS虚拟机环境准备）.md`](./docs/agent-ios-sim-vm-env-setup（Agent%20iOS虚拟机环境准备）.md)。

### 平台标识：内部通道与对外平台分离

- 引入两层模型：**内部通道** `platform`（`android` / `ios` / `ios_sim` / `harmony`）
  与**对外平台** `platform_family`（`android` / `ios` / `harmony`）。iOS 虚拟机在
  内部是独立通道，对外仍是 `ios`——**对外仍然只有三个端**。
- **对外接口口径不变**：提交任务时 `platforms` 仍只接受 `android` / `ios` /
  `harmony`；`GET /api/devices/available` 与 `/api/devices/statuses` 的 `platform`
  字段也报对外平台。iOS 虚拟机只是以 `ios` 身份多出现在设备池里，别名池写法与真机
  完全一致，**外部调用方无需任何改造**。
- 一个 `platforms: ["ios"]` 的批次可以同时铺到 iOS 真机与虚拟机上并发执行。
- 「这台是不是虚拟机」由 `extra.is_virtual` / `extra.vm_platform` 表达，不靠
  `platform` 承载。工作台内部接口 `GET /api/devices` 保留内部通道值用于路由。

### 就绪探针：失败退避与虚拟机 WDA 自愈

- 就绪探针新增**失败退避**：设备连续探测失败到阈值后逐步拉长探测间隔（封顶 30 秒），
  探通立刻恢复常速。健康设备零影响，阈值内的偶发失败仍按原频率快速重试。
- 新增 **iOS 虚拟机 WDA 卡死自愈**：连续探不通约半分钟、**且该设备当前空闲**
  （没有任务在跑、没有人在工作台）时，自动重启这台虚拟机的 WDA。
- 自愈**只作用于 iOS 虚拟机**。Android / 鸿蒙没有可单独重起的等价控制通道；
  iOS 真机沿用既有 stable 策略——**WDA 掉线不自动重启，等人工拔插**，本次未改动。

### 三端虚拟机页面口径统一（以 Android 为基准）

- 统一状态措辞：`agent_offline` 显示为「待恢复」（此前鸿蒙与 iOS 显示为
  「Agent 离线」，像是故障，实际是等 Agent 重连认领的正常中间态）；`draft`、
  `error` 同步对齐。
- 可自动恢复的中间态不再渲染成红色错误行；真正的失败（`error` / `unavailable`）
  仍然显示。
- 卡片字段改为「名 + 值」同一行，与 Android 一致，卡片高度减半。
- 新增自动检查，以 Android 页面为基准比对三端措辞与渲染，避免再次各说各话。

### 版本号统一

- 项目版本升级为 `0.7.0`；后端健康检查、右上角版本展示、Python 包元数据和 Web
  包元数据同步更新。

## 0.6.0 - 2026-07-31

### HarmonyOS 虚拟机完整接入（`main` 独有）

- 新增与 Android 完全隔离的「鸿蒙虚拟机」配置页、API、数据库表、Agent Manager、
  HDC 端口租约和生命周期状态机；启动后带 `virtual` 标识进入统一设备池，复用
  鸿蒙真机的工作台、调度、执行和报告链路。
- 支持按 DevEco 官方设备形态、机型和实测可创建系统版本选择配置；支持折叠屏初始
  形态、Agent 能力探查、下发、启动、停止、复制、删除、重连认领和孤儿实例对账。
- 新增全局共享 Emulator UUID / UDID 配置，便于开发证书只登记一次；配置只在
  虚拟机下次启动时生效，写入失败会阻断启动，不静默使用错误身份。
- **数据库迁移（部署需执行）**：`backend/migrations/harmony_vm_v1.sql`。
- Agent 宿主准备见
  [`docs/agent-harmony-vm-env-setup（Agent鸿蒙虚拟机环境准备）.md`](./docs/agent-harmony-vm-env-setup（Agent鸿蒙虚拟机环境准备）.md)。

### 当前 GUI 边界与无头演进

- 当前 DevEco 本地 Emulator 没有经过验证的公开 Headless / `-no-window` 等价入口，
  因此首期是 **Agent 承接的本地 GUI 模式**。生命周期已自动化，但宿主需要已登录
  图形会话；这属于官方能力限制下的阶段性形态，不是项目最终偏好。
- 官方一旦提供本地 Headless，现有 Agent 启动层直接切换；前端、Server API、
  数据库、HDC 端口、调度、设备池和停止回收不变，产品生命周期逻辑与 Android 统一。
- 集中式场景另规划 Harmony Linux gRPC Provider。Provider 未通过交付物、协议、
  真正无头和 HDC 映射验证前不进入当前 capability。
- 明确禁止隐藏兜底：Headless 启动失败不偷偷弹回 GUI，gRPC Provider 失败不自动
  改派员工 Agent，有窗口模式不伪装成 Headless。
- 完整架构与演进说明见
  [`docs/harmony-vm-architecture（鸿蒙虚拟机当前架构与演进规划）.md`](./docs/harmony-vm-architecture（鸿蒙虚拟机当前架构与演进规划）.md)。

### 版本号统一

- 项目版本升级为 `0.6.0`；后端健康检查、右上角版本展示、Python 包元数据和 Web
  包元数据统一，不再继续显示历史占位版本 `0.0.1`。

## 2026-06-09

### Android 虚拟机（Emulator）接入（`main` 独有）

- 新增「虚拟机」页：按品牌 / 机型 / 系统 / 分辨率创建 AVD 下发 Agent 启动；启动后作为普通 android 设备进设备池，复用真机同一条调度与执行链路。
- **数据库迁移（部署需执行）**：`backend/migrations/android_vm_v1.sql`，新增 `android_vm_instances` / `android_device_profiles` / `android_vm_coverage_profiles` 三张表。
- Agent 宿主需准备 Android SDK / Emulator 环境（JDK / cmdline-tools / 系统镜像矩阵，含 Windows），见 [`docs/agent-vm-env-setup（Agent虚拟机环境准备）.md`](./docs/agent-vm-env-setup（Agent虚拟机环境准备）.md)。

### 应用分发与黑屏工程文档化

- README / features 补齐**应用分发**（上传 APK/IPA、按平台筛可分发设备、批量安装、失败重试、超时兜底）与**黑屏工程**（三端空闲息屏 + Run 前唤醒、息屏态可派发）说明；两项功能此前已上线，本次仅补文档口径。

### 分支策略

- `main` 为推荐主线，新功能优先落地 `main`；**Android 虚拟机等大功能为 `main` 独有、暂不同步 `next/server-brain`**；`next` 仍持续维护、可继续使用。新接入建议直接用 `main`。

## 2026-05-28

### 依赖安全告警

- 修复 GitHub Dependabot 告警 `GHSA-q8mj-m7cp-5q26`：`midscene-bridge` 通过 npm `overrides` 将间接依赖 `qs` 固定到 `6.15.2`。
- 影响范围仅限可选 Midscene Bridge 子工程，不影响默认 VLM 主链路。

### iOS open_app 应用列表链路

- `open_app(app_name="某个 App")` 会先查询 iPhone 应用列表，再把自然语言 App 名匹配为 bundle id。
- iOS 应用列表不再依赖 `ApplicationType=Any` 作为唯一入口，改为分别查询 `User` 与 `System` 后合并。
- 单侧查询失败不会拖死另一侧；常见系统 App bundle id 有兜底列表。
- 排障口径：控制台点击/滑动正常但 Run 的 `open_app` 报错时，优先排查应用列表查询链路，而不是 WDA 控制链路。

### iOS 终端清单

- 基础运行进程统一为 Server、Agent、Web 三个。
- `pymobiledevice3 remote tunneld` 改为 iOS 17+ / RSD / DVT / 部分设备服务场景按需常驻，不再描述为所有 iOS Agent 的固定第四个必开终端。
- iOS 15 / 16 基础 WDA 控制通常不需要 tunneld；iOS 17+ 若遇到 RSD、DVT 或设备服务错误再开启。

### 息屏 Run 默认策略

- `.env.example` 默认仍是全端息屏 Run 模型开启：Android / HarmonyOS / iOS 均允许息屏待机派发，并在 Run 前唤醒。
- `AI_PHONE_IOS_WAKE_ON_ENTER` 仅表示进入工作台 / WDA 就绪后的点亮体验，不是 iOS 息屏 Run 的核心开关。
- HarmonyOS wake 后是否上滑继续由 Server DB / Web「设备配置」页按 serial 维护；Agent 本地不维护设备白名单。
