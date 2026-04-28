# codex后续计划表

## 文档目的

这份文档用于持续记录 `ai-phone` 后续要做的事项。  
方式不是一次性拍完整 roadmap，而是边讨论边沉淀：

- 目标
- 约束
- 优先级
- 阶段计划
- 风险
- 待确认问题

后续每聊到一个点，都可以继续往这份文档里补。

---

## 讨论来源与收敛过程

这份文档不是凭空设计出来的，而是从一整轮真实讨论中逐步收敛出来的。

最开始的出发点，并不是要把 `ai-phone` 做成一套传统测试平台，而是先解决“多端真机可连接、可操作、可执行”的现实问题。  
在这个过程中，我们先经历了 iOS 真机接入、WDA/Xcode 链路理解、Android 与 iOS 路线差异、HarmonyOS 可行性调研，然后才进一步回到平台层面，重新审视：

- `ai-phone` 到底应该做什么
- 什么能力应该保留
- 什么能力应该主动砍掉

这轮讨论里，最关键的几个收敛节点是：

- 一开始的目标里还带有一些“轻量化 Sonic”的惯性预期。
- 但讨论到后面已经非常明确：`ai-phone` 更适合作为
  `AI 云真机执行器 / 执行内核`
  而不是继续长成一个大而全的平台。
- 也就是说，它更应该专注于：
  - 接收请求
  - 调度设备
  - 驱动执行
  - 输出报告
  - 异步广播结果
  而不是承担权限、编排、用例管理、组织平台这些外围职责。

- 在执行协议上，最开始我们讨论过较多概念：
  - `submission`
  - `sessionId`
  - `caseId`
  - `runItem`
  - `runId`
- 但随着讨论深入，方向越来越明确：
  - 外部协议必须尽量瘦
  - 内部模型可以复杂，但不要逼调用方理解
- 最后收敛成：
  - `submissionId` 由执行器内部生成
  - 外部主关联键最终收敛为 `submissionId + caseId + platform`
  - `sessionId` 不进入 v1 外部契约
  - `runItem / runId` 只作为内部实现细节考虑

- 在输入内容上，最开始也讨论过“前置步骤 / 前置 case / dependsOnCaseId”这类能力。
- 但讨论后确认，这会把执行器慢慢拖向编排平台。
- 所以当前结论是：
  - `ai-phone` 只接收最终可执行的 `runContent`
  - 前置步骤由调用方在调用前自行拼接
  - 当前不提供前置依赖自动解析与注入

- 在广播与状态设计上，也经历了从“是否发中间态”到“先只发终态”的收敛。
- 当前阶段优先保证协议稳定、字段清晰，而不是一开始把事件体系做得过胖。

所以这份文档里的所有计划项，都不是“先想大而全再删”，而是从真实问题一路推出来的：

- 哪些概念会让调用方变重，就砍掉
- 哪些能力会把执行器拖成平台，就延后
- 哪些字段是执行真正不可缺的，就保留
- 哪些规则不提前说死，后面一定会混乱，就先定下来

这也是这份计划表最重要的原则：

`先把执行器的边界立住，再谈扩展能力。`

---

## 原始讨论记录

下面这部分保留原始讨论内容，目的是把后续结论的来源完整留住。  
这里不追求文档化表达，而是保留当时真实的思考过程、犹豫点、分歧点和逐步收敛的痕迹。

```text
我会一个点一个点的说我们接下来要完成的
1. 排队与调度
我的设想，我们的机器属于资源，并且分3类，那么我们接收参数应该是4个，caseid，sessionid，设备端（操作系统），执行设备，执行内容
从场景推算
如果用户发来30个case，每端10个，首先我们先组排队
我们按照一批请求带来的整体参数做排队，就算第一个人发了30后续又发了1，不好意思，你得排队下一轮
也就是我们先通过每一轮请求来带的内容进行一个可执行队列打包，他们是一个整体，暂时叫做：执行用户（或者执行集合的概念，我们可以自己创建一个集合或者用户传，这样比session方便多了？但也要考虑颗粒度问题啊，比如某个端不执行了，要放弃排队等，session还是有用的吧？）
会在web端新开一个tab叫做执行调度或者队列这种
里面显示了排队情况，首先就是按照执行用户排队，比如这个执行用户30个case，每端10个，那么他就可以调用全部的资源，或者是可配置的每个用户能调多少（暂时全部）
比如我们有每端只有1个设备，总共3个，那么他就会在这个tab下展示，已调用几个端设备，每个端展示一行，展示端数量，按这个场景就是
安卓1：用小格子代表case数量，比如（已经执行了2个，待执行排队中8个）
ios1：用小格子代表case数量，比如（已经执行了5个，待执行排队中5个）
鸿蒙1：用小格子代表case数量，比如（已经执行了9个，待执行排队中1个）

如果有排队的人就会再下面像上个例子一样列出来执行用户，他要执行几个格子（case数量），用端表示，只是他因为没有资源还在等待，他就似0个执行，排队N
当第一个执行用户部分机器出来了，那么排队的就可以先用执行其中一个端，此时这个执行用户也属于执行中了，只不过他的端可以先执行

那么其中的关系有点微妙，执行用户真的实在按人排队吗，不是的，只是我们让他们更统一，只要某个端出来了空机器，他们就可以调用了
当然这里要考虑执行设备，我们会给一个接口查询设备别名（用户团队app不同环境的安装），某些设备只安装某个环境包，所以我们用设备别名控制（这里可能又要将离线那个概念搬出来了，因为考虑设备拔插别名丢失，但是我们可以设计的更简约）

可以出一个api或自己来平台看，用户这批的执行进度到哪了

当用户撤销执行，也是可以的，相当于给我们指令，我们给他的候选执行小格子进行删除，但这里需要严格规定，执行中的不可逆，未执行的才能取消，可以按case id sessionid这两个标识取消，但难点就在于如果他相同的30case再次打进来请求，上一波还没有完成，我们怎么处理的事，这里需要好好想想，执行准入标准后面会继续聊

主要想表达的是设备资源与调度解构

会涉及排队时间，比如超过1小时直接全部踢出（标识我们的问题）但case就是失败了，只有通过才是success！

如果好多排队的和真实人员web占用怎么办？只要你抢得过！理论上人是抢不过的，因为我们一个容器是执行用户维护进行总时间控制的一个区间

这里其实非常好的一点我们开始就做了锁，比如进入机器内部才是占用，那么这个点可能会复用，但是会考虑并发场景

2.执行准入标准
我们仅支持一段标准的调用体，一次调用作为一个执行用户的容器块进行打包准入和编排
一个容器快可能需要存在多个设备，或者用户可能需要一条case三端执行，但这需要用户自己传入3个不同caseid并且不同sessionid的相同case体，按照要求打过来（我觉得这样们简单，没想到更好的方式，或者说我们给编排？）
请求过来我们会快速检测结构和机器资源，如果用户结构符合，那么我们就接收进入调度
如果某个端一个设备都没有（不是排队，而是没机器），我们也会全部打回，用户自己舍弃端重新请求
当然如果用户请求时设备有，排队执行中的时候这个端的所有设备掉了，那么我们会广播失败，并且标注是执行器的问题，但注意着不影响还在排队的，但有可能执行用户因为端没了进行踢出，排队的排入跟着踢出，只是想表达，如果只从开始进入执行的用户进行踢出，因为设备可能是抖动。


3. 设别健康探活+自愈
这里其实我们已经做了蛮多了，但是要确定一个事，就是我们是否真的准备好了才能下发有可执行设备，比如现在ios可能被锁屏了，鸿蒙被锁屏了，我们现在的处理他仍显示可用，但不行，我们要保证这两个端是即可可用而不用打开界面的，这里只需要检测可用，不用做自愈，因为端特殊所以靠人来控制就可以了

4. 并发隔离
这里有点复杂，这里应该会解答排队和执行标准的一些解释
首先我们在执行过程中和排队的所有case中，session是唯一的，有相同的就拒绝不让准入，在入口我们封住
用户有需求走并发执行，比比如相同case执行android的5个设备，那么草考执行准入标准，自己编排session或我们想一个编排方式？

5. 结果的幂等+去重
调用当kafka端了会不会丢消息，理论上会的，但其实不是我们考虑的事，我们可以先暂存记录几天也没关系。他们手动去查
同一个sessionid能不能重播，我认为能，但需要考虑4并发隔离，在队列中的不进准入，或者直接不行

其它：
日志留什么，怎么存，日志就是现在的执行日志，图片文字我们都要！，我们要做一个html存，你可以参考我的其他项目比如allure报告或者自己编排一个，这个好说
进入的字段和出口的字段定死了吗？没有，暂时只是说了几个，但精简的核心概念不变，只留应该留的，只增应该增的
我们关心调用执行者是谁吗，不关心，就是个id，我们就是个完成任务的执行器
执行超时或者阈值要设置吗？要，可以宽松，1个case1小时没完毕强制kill掉，执行失败，防止死循环，可以无限次排队吗，不行，理论上我们需要做一个标准，执行用户几十个不允许再调用排队了，每个用户进入的case量级不超过500个这种
还有其他的我暂时没有想出来



模型提出问题：
如果继续往下聊，我建议你下一个点优先聊这个：
submission / runItem / sessionId / runId 的最终数据模型
因为这个一旦定住，调度、准入、取消、广播、报告、幂等，基本都能跟着落。

submission其实就是集合吧，或者说一次请求包的唯一标识，这个用传入方传入，每个case执行完毕通知也会带这个id告知他是哪个集合下的
runItem单个执行单元，你说的有道理，但你这个是消费级唯一的吗？我认为直接叫caseid比较好，因为这个和case正相关，也用构造，外部传也是他case表的id，我们也要帮外部想想，他们好调用，我们适配也强
sessionId是一个重要标识，代表了用户本次执行的报告怎么拿，和往期区分，我在想submission+caseid是不是就够唯一了，不然调用方构造这个也麻烦把，搞得我真想让设计编排器了...，理论上所有人的caseid都不同的，但是别人可用从自己的submission带一个其他人的id，来执行，这很正常
runid是什么，我不理解

我想到一个点可能要考虑， 用户如果是有依赖的case，比如case1和case2是一块的，用户要执行的是case1，只是case2是前置，那么我们碰见这种结构需要帮助处理，也很简单，将case2的内容添加到case1的前缀语义上，但是他们怎么传来呢，这值得思考


ps：
理论上：submission可以是我们维护，不相同，历史也不能相同，他们准入通过我们马上返回，他们留着做唯一，不然他们有可能真的用case集合id传，那会相同的



那你的意思是runItem是我自己设计的执行单元，那么请求来了通过的话，我需要根据他的外部执行单元caseid映射一份runItem，用处在哪呢，你想说的是我设计一个oderid吧，控制顺序？
关于。但如果你考虑“重试 / 重跑 / 历史报告 / 广播去重”
这里方法是submission我们维护就好特别多了其实，这样submission+caseid就够，外部也轻松特别多，我们不考虑重试，用户可以根据结果打新的submission过来的，我们只负责对错，那么广播，历史其实也迎刃而解了吧

runid我感觉和runItem重复呢，如果说我自己入库管理，那么ubmission+caseid+入库主键其实就是这个东西吧


关于前置，我可以说，我现在的平台就是这样的，把他前置步骤挖出来给依赖的，这没问题，因为我们是纯语义执行，不管前置谁失败，我们只是挖了步骤，还是本地失败，这没歧义吧，主要矛盾点是。怎么让用户按照我们的标准传呢，他只传id的话我怎么去哇这个步骤，他传步骤的话那他自己拼不就完了，但是我们又订规则了，我们是想简洁的








最小请求体
最小广播体
最小状态机


最小请求体理论上我们可以推论出来了吧
我们可以接受用户发的结构包含不需要的内容，但是我们需要的少，就打回，通过了我们入库实时调整并给外部submission
所以应该是一个列表嵌套json，然后每个json块包含我们要的东西，比如caseid和端类型，执行体内容

最小广播体应该比较简单，我们是通过case执行一个就播报一个，那就是submission+caseid+html报告链接+执行结果+端类型
我只想到这么多，你帮我想想补充

最小状态机就是一个单独的执行单元吧，用户如果取消执行，他会传入，submission+caseid，我们通过caseid进行提出队列，执行过的就过滤，然后正常广播



那我们主要的排队和调度，用什么
```

---

## 当前定位（暂定）

`ai-phone` 不再继续朝“轻量化 Sonic”方向收敛，而是朝：

`AI 云真机执行器 / 执行内核`

推进。

它的核心职责更偏：

- 接收执行请求
- 驱动真机执行
- 支持人工调试与 AI 视觉执行
- 统一沉淀执行日志与结果
- 向外异步广播执行结果

它不优先承担：

- 部门权限管理
- 业务平台编排
- 用例管理平台
- 大而全测试平台能力

---

## 讨论规则

后续每个计划项，尽量按下面结构补充：

### 计划项名称

- 背景
- 目标
- 为什么要做
- 不做什么
- 优先级
- 依赖
- 风险
- 结果产物

---

## 计划区

### P0. 最小请求体

- 背景
  `ai-phone` 作为执行器，入口协议必须尽量轻。调用方可以很复杂，但执行器不应该被迫理解太多业务字段。
- 目标
  定义一版最小必需请求结构，只接收执行真正需要的信息。
- 为什么要做
  没有最小请求体，后面准入、排队、幂等、广播都会越来越重。
- 不做什么
  不替调用方承载测试平台对象模型，不要求一次性把未来所有扩展字段定完。
- 优先级
  最高。
- 依赖
  submission 模型、case 粒度、平台资源池模型。
- 风险
  如果入口字段过多，调用接入成本会上升；如果字段过少，又可能无法稳定调度。
- 结果产物
  一版最小请求契约。

当前评估与建议：

- 你的推断方向是对的：最小请求体本质上就是一个 `items[]` 列表。
- 执行器不必拒绝“有额外字段”的请求，但必须只依赖最少的一组字段来完成准入与执行。
- 当前最小结构建议为：
  - 请求整体是一个 JSON 对象
  - 对象里有一个 `items` 数组
  - 每个 item 代表一个单独执行单元
- 每个 item 的最小必需字段建议是：
  - `caseId`
  - `platform`
  - `runContent`
- 含义分别是：
  - `caseId`：调用方自己的业务 case 标识
  - `platform`：目标端类型，例如 `android / ios / harmony`
  - `runContent`：最终可直接执行的语义内容
- 这三个字段是当前最小闭环，因为：
  - 没有 `caseId`，外部无法对齐结果
  - 没有 `platform`，执行器无法入对应资源池
  - 没有 `runContent`，执行器没有真正的执行输入
- 设备选择不是所有场景都需要，因此建议不是必填，而是可选字段：
  - `deviceAlias`：设备别名
- 所以你们的第一版最小请求体可以理解成：

```json
{
  "items": [
    {
      "caseId": "case-001",
      "platform": "ios",
      "runContent": "打开微信，进入通讯录，搜索张三并发消息",
      "deviceAlias": "ios-wechat-prod-01"
    }
  ]
}
```

- 其中 `deviceAlias` 缺省时，表示“只限定平台，不限定具体设备”。
- 关于“如果用户传了很多额外字段怎么办”，建议规则写成：
  - 未识别字段允许透传进入落库原始请求
  - 但不参与执行器核心逻辑判断
- 这样既不阻塞接入，也不污染执行器的核心协议。

✅ 用户决策（2026-04-21 下午补决，请求结构与逐 item 校验规则）：

- **请求体结构硬约束（根即数组，无包裹对象）**：
  - **请求 body 根直接就是 JSON 数组**，不再用 `{ "items": [...] }` 这种外层对象包装；
  - 数组每个元素必须是 JSON object（以下称 item），每个 item = 一条执行单元；
  - 空数组、非数组根、或元素不是 object → 整批拒绝，`rejectReason = invalid_request`。

- **v1 请求示例**：

```json
[
  {
    "caseId": "case-001",
    "platform": "ios",
    "runContent": "打开微信，进入通讯录，搜索张三并发消息",
    "deviceAlias": "ios-wechat-prod-01"
  },
  {
    "caseId": "case-002",
    "platform": "android",
    "runContent": "打开设置，进入 Wi-Fi 页面，截图"
  }
]
```

- **字段分层（item 内部，v1 定稿）**：

| 字段 | 必填 | 类型 | 枚举 / 约束 | 说明 |
|---|:---:|---|---|---|
| `caseId` | ✅ | string | 非空 | 调用方自身业务 case 标识 |
| `platform` | ✅ | string | `android` / `ios` / `harmony`（全小写） | 目标端类型 |
| `runContent` | ✅ | string | 非空 | **纯自然语言字符串**，不做结构化协议；比如"打开微信，进入通讯录，搜索张三并发消息" |
| `deviceAlias` | ⬜ | string | — | 可选；缺省 = 平台内任意可用设备 |
| 其他字段 | ⬜ | any | — | 允许透传、原样落库；不参与校验、不参与调度 |

- **`runContent` = 纯自然语言字符串**（v1 定稿）：
  - 不引入 `steps[]`、不引入 `systemGoal + userGoal` 这类多字段拆分；
  - 调用方把最终给 VLM 的"目标描述"拼好后，**整串放进来**即可；
  - 对齐现有后端 `/api/runs` 的 `goal` 字段语义，VLM runner 可以直接消费。

- **逐 item 独立校验（关键）**：
  - 执行器**逐个元素**遍历请求数组，对每条 item 独立验证必填字段；
  - 不要求各 item 字段集"一致"——第 1 条带 `deviceAlias`、第 2 条不带，完全合法；
  - 不要求额外字段"相同"——每条 item 可以自带不同的业务扩展字段，只要必填齐全，就允许通过；
  - **我们只校验我们要用的字段**，其他字段一律透传不碰。

- **拒绝粒度 = 整批**：
  - 任何一条 item 缺必填字段、或 `platform` 不在枚举内、或 `runContent` 非字符串 → **整批打回**，**不接受部分 item 通过**；
  - 同步返回 `accepted=false, rejectReason=invalid_request`，并在返回体里带 `rejectDetail` 字段辅助定位：

```json
{
  "accepted": false,
  "submissionId": null,
  "rejectReason": "invalid_request",
  "rejectDetail": {
    "itemIndex": 3,
    "missingField": "runContent"
  }
}
```

- `rejectDetail` 只面向调试用，字段可扩展，调用方不应将其作为正式依赖；正式消费仍然只看 `rejectReason`。
- 为什么整批打回不做"部分接收"：
  - 调用方的 30 条请求语义上是"一批"，接一半会让调用方分不清"拒收列表是哪几条"，下次重发又要对齐位置；
  - 逐条拒收还需要额外的"拒收列表"协议，在 v1 不值得付出这个复杂度；
  - 错误应当在调用方生成阶段修掉，而不是在 executor 侧做"部分修复"。

---

### P0. 执行调度与资源排队

- 背景
  `ai-phone` 作为 AI 云真机执行器，下一阶段不能只“能执行”，还要能稳定处理批量请求、资源竞争、设备分配与排队。
- 目标
  建立一套面向“执行请求批次”的调度模型，支持多端资源池、批量 case、按设备端并发执行、按资源占用进行排队。
- 为什么要做
  如果没有调度模型，一旦外部平台一次性打入大量 case，请求会互相覆盖、抢锁混乱、无法对外解释状态。
- 不做什么
  不做复杂组织级调度平台，不做部门权限，不做完整测试编排平台。
- 优先级
  最高。属于执行器从“能跑”进化到“可运营”的第一步。
- 依赖
  设备池状态、锁机制、运行生命周期模型、事件广播模型。
- 风险
  最大风险不是“技术不会写”，而是概念混乱。必须先定清楚到底按什么排队。
- 结果产物
  一套统一术语与调度数据模型。

当前评估与建议：

- 你的方向是对的，但队列单位不应叫“执行用户”，更准确应叫：
  - `submission`：一次外部提交的整体请求
  - `jobSet`：这次提交里的一组 case
  - `item`：单个 case 在单个端上的执行单元（= `caseId + platform` 粒度，不另起名）
- 也就是说，**排队应该按“提交批次 submission”展示，而不是按真实用户展示。**
- `caseId` 仍然保留，它是单个执行单元的外部关联键，不适合取代批次概念。
- 你说的“第一个人发了 30 个，后面再发 1 个要排下一轮”，这是可以成立的，但它其实不是“按人排队”，而是“按 submission 进入调度轮次”。
- 多端资源展示方式你已经想得很清楚，web 调度页可以直接按资源池展示：
  - `android`: 执行中 N / 排队中 M
  - `ios`: 执行中 N / 排队中 M
  - `harmony`: 执行中 N / 排队中 M
- 你说“某个端空出来，排队中的 submission 可以先执行其中一个端”，这也是对的。说明 submission 是一个整体，但内部各端可以局部推进。
- 这里建议你明确一个规则：
  - `submission` 是展示和调度归属单位
  - `item`（`caseId + platform`）是资源分配与状态流转单位
- 关于等待超时，“排队 1 小时直接踢出”这个思路成立，但结果建议不要写成笼统失败，而应区分：
  - `queue_timeout`
  - `resource_unavailable`
  - `executor_error`
  否则后面调用方看报告时会分不清是业务失败还是平台排队失败。

---

### P0. 执行准入标准

- 背景
  执行器不能什么请求都收。必须在入口先判定结构是否合法、设备端是否存在资源、是否允许进入调度。
- 目标
  定义单次调用的标准结构、批量请求边界、不可接收条件、直接拒绝条件。
- 为什么要做
  没有准入，后面的排队、幂等、调度、广播都会失控。
- 不做什么
  不负责业务编排，不替调用方理解业务含义。
- 优先级
  最高，与调度同级。
- 依赖
  请求模型、设备资源模型、状态机。
- 风险
  如果准入条件不清晰，后续补规则会越来越难。
- 结果产物
  对外 API 契约 + 入口校验规则。

当前评估与建议：

- 你这个思路整体是对的：**一次标准调用体，作为一个 submission 容器整体进入调度。**
- 你现在提出“用户自己传 3 个不同 caseId，来表达一条 case 的三端执行”，这个方向成立，但要补一条约束：
  - 同一个 `submission` 内，`caseId + platform` 必须唯一
- 我支持你短期先这样做，不要一开始给执行器塞进“跨端编排 DSL”。否则你会很快从执行器走回编排平台。
- 不过要补一个明确模型：
  - `submission`: 本次请求整体，也就是“一整批一起进来的任务包”
  - `items[]`: 每个待执行单元，也就是“submission 里的一条执行项”
  - 每个 `item` 自带 `platform / caseId / runContent / deviceAlias?`
- 这样“同一条业务 case 的三端执行”只是调用方传 3 个 item，不需要你理解业务关系。
- 你提出“如果某个端一个设备都没有，就整体打回”，这个成立，但建议区分两种拒绝：
  - `no_capacity_for_platform`: 当前这个平台完全没有可用机器
  - `invalid_device_selector`: 指定设备别名不存在
- 你也提到了设备在排队期间掉线的问题，这里建议规则明确成：
  - 准入时按“当前资源视图”接收
  - 调度执行时再次按实时资源判定
  - 如果中途平台端资源全灭，则相关执行项广播 `executor_resource_lost`
  - 是否连带踢出整个 submission，要做成配置策略，不要写死

✅ 用户决策（2026-04-21 下午补决）：

- 准入阶段的"资源视图"粒度定死为：**该平台至少有一台设备 online**。
  - 只要有 1 台设备在线（不要求 `ready`），该平台的 item 准入不拒绝。
  - 一台都没有，整批请求中属于该平台的 item 全部准入拒绝，`rejectReason = platform_no_available_resource`。
- 准入**不做资源预留**。真正的抢占发生在调度阶段（空出 `ready` 的设备先到先得）。
- 这样即使当下所有设备都 `busy`，只要 `online` 就允许进 queue 排队。
- 运行期中途平台端资源全灭时（2026-04-18 修正口径）：
  - **不主动快踢 queued**——agent 抖动 / USB 瞬断 / 设备拔插是常态，一次"平台瞬时全灭"很可能只是几秒钟就恢复；要是按"快踢"处理，一次抖动可能把整条队列全部打飞。
  - 策略：该平台 queued 的 item 继续排队等机器回来，设备恢复后正常派发；**等不到的 item 统一走 submission 3h 硬上限，由 `_scan_timeouts` 以 `submission_timeout` 逐条收口**。
  - `running` 的 item 走自身超时（1h）/ `executor_resource_lost` 终态，与本条无关。
  - 所以：外部消费方在"平台全灭"场景里收到的 `statusReason` 只会是 `submission_timeout`，**不会**是早期草案里的 `platform_pool_unavailable`（该枚举项 v1 从未 emit，已从 11 项里撤掉，见 P1）。

---

### P0. 设备健康探活与可执行性判断

- 背景
  “设备在线”不等于“设备可执行”。对执行器来说，真正重要的是可执行性。
- 目标
  建立设备健康状态分层，保证下发前设备是真正可用的。
- 为什么要做
  如果把“锁屏、wda 死、hdc 掉、agent 断”都算可用，调度就是假的。
- 不做什么
  先不做复杂自愈中心，不保证所有端都自动修复。
- 优先级
  很高，属于准入与调度的前置条件。
- 依赖
  Agent 侧探针、端能力检查、健康缓存。
- 风险
  健康状态定义过粗，会导致大量“接单后失败”。
- 结果产物
  统一设备健康状态模型。

当前评估与建议：

- 你这个点说得非常准：**在线 != 可执行。**
- 你现在需要的不是“设备状态灯”，而是“可接单状态”。
- 建议至少拆成这几类：
  - `offline`: 设备不在线
  - `online_unready`: 在线但不可执行（锁屏、wda 未就绪、hmdriver2 不通等）
  - `ready`: 可接单
  - `busy`: 已被执行器或 web 调试占用
  - `degraded`: 可勉强工作但能力降级
- 你说 iOS / HarmonyOS 锁屏时仍显示可用，这是必须修的。
- 这里“手工调试占用”和“自动执行占用”没有区别，本质上都是同一把设备锁：
  - 自动占用时，手工不能再进
  - 手工占用时，自动也不能再进
- 这里我同意你“先探活，不先做自愈”的策略。特别是 iOS / HarmonyOS 特殊性很强，先把可用性判准，比盲目自愈重要。
- 但 Android 可以保留更积极的自愈能力，后面你们可以按平台分层。

---

### P0. 并发隔离与唯一性约束

- 背景
  一个执行器如果不能定义并发边界，很快就会出现 submission 内标识冲突、同设备互抢、结果覆盖等问题。
- 目标
  明确 submission 唯一性、submission 内 item 唯一性、设备占用规则、取消规则。
- 为什么要做
  这是执行器最核心的“秩序层”。
- 不做什么
  不做复杂事务平台，但必须做执行唯一性。
- 优先级
  最高。
- 依赖
  入口校验、锁机制、调度模型、结果状态模型。
- 风险
  一旦上线后再改唯一性规则，成本会很大。
- 结果产物
  唯一性与并发隔离规则说明。

当前评估与建议：

- 这里建议彻底删掉 `sessionId`，不要再让它进入 v1 外部契约。
- 现在更简单、也更稳定的唯一性规则是：
  - `submissionId` 由执行器内部生成，代表一次被接收的请求批次
  - 同一个 `submission` 内，`caseId + platform` 必须唯一
- 也就是说，入口准入至少要拒绝：
  - 同一个 submission 请求体里重复的 `caseId + platform`
- 如果未来支持“同一个业务 case 多次并发执行”，再单独扩展新的外部字段；不要现在提前塞一个 `sessionId` 进来。
- 至于“同一个 case 并发跑 5 台 Android”，当前最简单的规则是：
  - 让调用方传 5 个可区分的 `caseId`
  - 执行器不帮他猜多实例语义

---

### P0. 结果幂等、去重与留存

- 背景
  执行器是异步系统，广播、查询、重试、补发都会遇到幂等问题。
- 目标
  定义结果的唯一主键、重复提交处理、结果留存策略、补查策略。
- 为什么要做
  没有幂等，Kafka 丢消息、调用方重试、你们补发时都会乱套。
- 不做什么
  不做调用方的消费保障系统，但要保证自己有“事实记录”。
- 优先级
  很高。
- 依赖
  结果主键规则、artifact 存储、事件模型。
- 风险
  如果只广播不留底，后期追查会非常痛苦。
- 结果产物
  幂等规则 + 留存策略 + 广播契约。

当前评估与建议：

- 你这个判断也很对：Kafka 丢不丢消息，不全是你要兜的；但你不能因此不存结果。
- 建议强制保留：
  - 运行元数据
  - 最终状态
  - HTML 报告
  - 关键截图/日志
  - 广播时间与广播次数
- 也就是说：
  - Kafka 是“通知层”
  - 你自己的数据库/对象存储是“事实层”
- 关于重跑，我建议规则简单化：
  - `submissionId` 由执行器每次接收新请求时重新生成
  - 历史 submission 不重放
  - 真要重跑，调用方直接发一笔新的请求
- 这会比“复用旧标识重放”稳定很多。

✅ 用户决策（2026-04-21 下午补决）：

- **`submissionId` 是 UUID**，由执行器在准入通过时生成，同步返回给调用方；调用方以此订阅/对账结果。
- **幂等键固定为 `submissionId + caseId + platform` 三元组**。
  - 在准入阶段用于同批唯一性约束（见 P0.并发隔离）。
  - 在消费阶段作为消息级幂等键——消费方按此三元组在自己侧建 "已处理" 表做去重；
  - **v1 不引入消息级 `messageId`**，`submissionId + caseId + platform` 在 submission 内天然唯一，天然可去重。
- 广播语义为 **at-least-once**，允许重发；消费方有责任自行去重。
- **结果留存**：元数据 / 最终状态 / HTML 报告 / 关键截图 / 广播记录保留 **15 天**，超期清理。
- 重跑策略：历史 `submission` 不重放；真要重跑，调用方重新发一笔新请求（新 `submissionId`）。

---

### P0. 外部标识与内部标识收敛

- 背景
  当前讨论里已经出现了 `submission / caseId / runItem / runId` 多层概念。如果不主动收敛，外部调用会变重，内部实现也会越来越绕。
- 目标
  明确哪些标识暴露给调用方，哪些只在执行器内部存在；尽量减少调用方负担，同时保留内部可追踪性。
- 为什么要做
  执行器的价值是“稳定接单并执行”，不是让调用方学习一套复杂对象模型。
- 不做什么
  不把内部数据库建模直接暴露给外部，不为了“理论完备”强塞多个标识。
- 优先级
  最高。因为这会直接影响 API、广播、报告、去重、历史查询。
- 依赖
  准入模型、调度模型、结果存储模型。
- 风险
  如果现在不收敛，后面每多一个接口都会把概念继续扩散。
- 结果产物
  一版足够稳定的最小外部契约。

当前评估与建议：

- 你这次的判断是对的，`submissionId` 更适合由执行器内部生成，而不是让外部传。
- 原因很直接：
  - 外部很可能把“case 集合 id / 测试计划 id / 任务单 id”直接当 submission 传进来
  - 这些值在历史上很可能重复
  - 一旦你把它当唯一键，准入、广播、历史查询都会变脏
- 所以更合理的方式是：
  - 外部提交请求
  - 执行器做准入校验
  - 一旦接收成功，执行器立即生成并返回 `submissionId`
  - 后续广播、查询、报告都以这个 `submissionId` 为总关联键
- 对外层面，建议保留的核心概念先压到最少：
  - `submissionId`：提交批次标识，由执行器生成
  - `caseId`：用例标识，由调用方传入
- 这里我认同你现在的收敛方向：
  - `runItem` 不应该成为外部概念
  - `runId` 也不应该先拿出来给调用方理解
- 如果内部要建模，完全可以这样理解：
  - 外部世界认 `submissionId + caseId + platform`
  - 内部数据库自己有主键 id
  - 如果以后有必要，再在内部把单条执行记录叫 `runItem`
- 也就是说，`runItem/runId` 可以存在，但只作为内部实现细节，而不是一开始就成为对外协议的一部分。

> ⚠️ 以上段落为讨论过程中的早期建议。最终立场以下方 ✅ 用户决策为准：**`runItem / runId` 彻底废弃，不做内部命名**。

- 现在可以更进一步，直接定成：
  - v1 对外彻底不引入 `sessionId`
  - 外部请求只传 case 相关信息与执行内容
  - `submissionId` 由你们生成
  - 内部主键由数据库生成

✅ 用户决策（2026-04-21 下午补决）：

- **`runItem / runId` 概念彻底废弃**——不作为外部协议，也不作为内部命名。
  - 内部数据库主键就叫"主键"，不再起"runItem"这种中间名词。
  - 代码、日志、文档、报告里**一律禁止**出现 `runItem / runId`。
  - 外部与内部统一只认两套键：
    - **外部关联键** = `submissionId + caseId + platform`
    - **内部存储主键** = 数据库自增/UUID 主键
- 这是一次有意识的"少一个抽象"：少一个概念，后人读代码时少一个要问"这是啥"的位置。
- 向后兼容：老文档 / 评论里如有 `runItem / runId`，改动时顺手删除。

---

### P1. 前置依赖支持边界

- 背景
  一些业务 case 存在前置步骤，例如“先登录再下单”。你现在的平台是纯语义执行器，不是传统 UI case 编排器，所以前置依赖的表达方式需要定边界。
- 目标
  明确当前阶段不引入前置依赖能力，保持执行器输入模型简单稳定。
- 为什么要做
  如果这个边界不先说清，后面很容易出现“只传 caseId，让执行器自己去找前置 case 内容”的需求，最终把执行器拖进外部系统耦合。
- 不做什么
  不在 v1 支持“只传依赖 caseId，由执行器去外部系统抓步骤并拼装”。
- 优先级
  高。它会影响请求契约设计。
- 依赖
  请求模型、执行内容组织方式、报告模型。
- 风险
  如果边界没立住，执行器会慢慢承担 case 平台职责。
- 结果产物
  一版简洁的前置依赖输入规则。

当前评估与建议：

- 当前先正式关闭这个概念，不进入 v1 范围。
- 规则直接定成：
  - `ai-phone` 只接收最终可执行的 `runContent`
  - 如果存在前置步骤，由调用方自己在调用前完成拼接
- 当前阶段不提供：
  - `dependsOnCaseId`
  - `preludeContent`
  - 前置 case 自动解析
  - 前置 case 自动注入
- 这样做的好处是边界极其清晰：
  - `ai-phone` 只负责执行
  - 调用方负责组织语义内容
  - 不引入额外 case 关系模型
- 这个决定是有意识地“先砍能力”，不是遗漏能力。
- 等执行器的调度、准入、结果广播、报告体系稳定后，如果后面确实有明确收益，再单独讨论是否引入轻量前置能力。

---

### P1. 执行日志与 HTML 报告

- 背景
  执行器不是只返回 pass/fail，而要输出可追溯、可消费的执行证据。
- 目标
  生成适合异步广播和外部平台消费的 HTML 报告与日志产物。
- 为什么要做
  调用方大概率不会接你原始终端日志，他们需要结构化且可视的执行报告。
- 不做什么
  不做复杂报表平台，但要把单次执行报告做到足够好。
- 优先级
  高。
- 依赖
  执行事件、截图产物、日志结构、artifact 存储。
- 风险
  如果报告只是一堆原始日志，平台价值会被低估。
- 结果产物
  HTML 报告规范与报告生成器。

当前评估与建议：

- 这块你方向也对，HTML 报告是必须做的，而且很适合你现在的平台定位。
- 你的报告建议至少包含：
  - `submissionId`：提交批次标识
  - `caseId`：用例标识
  - 设备信息
  - 平台信息
  - 开始/结束时间
  - 最终结果
  - AI 关键动作轨迹
  - 关键截图
  - 错误定位

✅ 用户决策（2026-04-21 下午补决）：

- **报告 URL 规则**：`${AI_PHONE_REPORT_HOST}/{submissionId}/{caseId}/{platform}.html`
  - `AI_PHONE_REPORT_HOST` 由外部提供（公司内部某 host，ai-phone 只做拼接，后续接入时由用户方补入配置）。
  - 报告**匿名访问**（不做鉴权），URL 直接可打开。
- **保留 15 天**，超期清理；过期后原 URL 返回 404。
- **同步生成**：item 进入终态后，先生成 HTML、写入对象存储/host 对应位置，再触发终态广播。
  - 广播发出时 `reportUrl` **必定可访问**，不存在"收到消息但报告 404"的窗口。
  - 预期增加 1-3s 广播延迟，可接受。
- **报告生成失败时的降级**：如果 HTML 生成异常（模板 bug / 存储失败等），不能阻塞广播；改为：
  - 广播依旧发出，`reportUrl` 字段带入兜底的 `GET /api/submissions/{id}/cases/{caseId}/report` JSON 接口地址；
  - executor 侧记一条 `report_generation_failed` 错误日志，便于事后排查。
- **报告内容（v1 硬性最低项）**：
  - 头部元信息：`submissionId / caseId / platform / deviceAlias / 设备型号 / 起止时间 / 最终 result / statusReason`
  - VLM 轨迹：每步 `Thought / Action / 前后截图 / 耗时 / unknown 标记`
  - 执行日志：Sonic 风格分级日志（level 1/2/3）
  - Token 汇总：`input / output / cached / 段数`
  - 失败上下文：异常栈 / 最后一张截图 / 卡死触发原因（若有）

---

### P0. 最小广播体

- 背景
  执行器是异步系统，调用方不会一直阻塞等待结果，因此广播协议必须简洁、稳定、足够定位问题。
- 目标
  定义单个执行单元结束或发生关键变化时，对外广播的最小字段。
- 为什么要做
  如果广播字段不统一，外部平台接 Kafka 或其他消息系统时会很难消费。
- 不做什么
  不在广播里塞过多调试细节，不把完整日志正文直接塞进消息体。
- 优先级
  最高。
- 依赖
  submission 模型、结果模型、报告产物。
- 风险
  广播过胖会影响消费，过瘦又无法定位问题。
- 结果产物
  一版最小广播契约。

当前评估与建议：

- 你给出的方向基本已经对了。
- 如果广播粒度是“一个 case 执行完就播报一个”，那最小广播体建议至少包含：
  - `submissionId`：提交批次标识
  - `caseId`：用例标识
  - `platform`：端类型
  - `result`：执行结果
  - `reportUrl`：HTML 报告链接
  - `finishedAt`：完成时间
- 如果想再补一个最有价值的字段，我建议增加：
  - `statusReason`：结果原因
- 因为调用方看到失败时，第一时间最想知道的是：
  - 这是业务执行失败
  - 还是设备资源失败
  - 还是排队超时
  - 还是人工取消
- 所以一版推荐的最小广播体是：

```json
{
  "submissionId": "sub_20260421_xxx",
  "caseId": "case-001",
  "platform": "ios",
  "result": "fail",
  "statusReason": "executor_resource_lost",
  "reportUrl": "https://xxx/report/sub_20260421_xxx/case-001.html",
  "finishedAt": "2026-04-21T21:30:00+08:00"
}
```

```json
{
  "submissionId": "sub_20260421_xxx",
  "caseId": "case-001",
  "platform": "ios",
  "result": "success",
  "statusReason": "completed",
  "reportUrl": "https://xxx/report/sub_20260421_xxx/case-001-ios.html",
  "finishedAt": "2026-04-21T21:20:00+08:00"
}
```

- 其中：
  - `result` 只保留两种稳定取值：`success / fail`
  - `statusReason` 用来补充失败或取消的细分原因，可以按需要扩展
- `finishedAt` 的中文意思就是“完成时间”：
  - 这条执行单元什么时候进入终态
  - 不管是成功、失败还是取消，都记录这个时间
- 对于“取消”这种情况，当前规则建议按：
  - `result = fail`
  - `statusReason = cancelled_by_request`
- 如果后面需要更细扩展，可以再加：
  - `deviceAlias`
  - `durationMs`
  - `artifactSummary`
- 但它们不必进入第一版最小广播体。

---

### P1. statusReason 枚举初稿

- 背景
  既然广播结果字段已经收敛为 `result = success / fail`，那么所有更细的执行结果解释都需要落到 `statusReason` 上。
- 目标
  先沉淀一版足够小、但能覆盖主要执行场景的 `statusReason` 草案，避免后面实现时每个模块各自发明字符串。
- 为什么要做
  如果不先给出候选集合，后面 agent、调度器、报告生成器、广播层很容易各写各的原因值。
- 不做什么
  当前不追求一次列全所有原因，也不追求分类体系特别复杂。
- 优先级
  高。
- 依赖
  状态机、准入规则、超时规则、设备资源模型。
- 风险
  如果原因值膨胀太快，后面外部平台很难消费；如果过少，又会丢失定位价值。
- 结果产物
  一版可继续收敛的 `statusReason` 枚举草案。

当前评估与建议：

- 这里先明确分层：
  - `statusReason` 是广播字段，用来解释“这条执行项为什么得到这个最终结果”
  - 它不是准入字段
  - 准入阶段如果请求不合法、设备别名不存在，这些都应该走同步返回里的 `rejectReason`
- 所以 `statusReason` 只保留执行维度的原因，不混入准入拒绝原因。
- 当前最稳的做法不是先追求“大而全”，而是先给出一版能覆盖 v1 主流程的候选值。
- 建议先按两类去想：
  - 成功类
  - 执行失败类
- 第一版候选值建议如下：
  - `completed`
    中文：正常完成
  - `queue_timeout`
    中文：排队超时
  - `run_timeout`
    中文：执行超时
  - `executor_resource_lost`
    中文：执行过程中资源丢失
  - `platform_pool_unavailable`
    中文：某个平台资源池整体不可用
  - `device_unavailable`
    中文：目标设备不可执行
  - `cancelled_by_request`
    中文：被外部请求取消
  - `executor_error`
    中文：执行器内部异常
- 这里有一个当前建议：
  - `result = success` 时，`statusReason` 固定为 `completed`
  - `result = fail` 时，再根据具体原因取其他值
- 这样外部消费时会很简单：
  - 先看 `result`
  - 再看 `statusReason`

✅ 用户决策（v1 枚举定稿，2026-04-21 初版 12 项 → 2026-04-18 精简到 11 项）：

- v1 正式枚举如下（**11 项**，含 1 成功 + 8 平台失败 + 2 业务/人为）：

| statusReason | 中文 | result | 归类 | 触发时机 |
|---|---|---|---|---|
| `completed` | 正常完成 | success | — | VLM 判定 `finished` 或等价成功条件 |
| `assert_failed` | VLM 明确判定失败 | fail | 业务 | VLM 下发 `assert_fail` 动作 |
| `run_timeout` | 单条执行超时 | fail | 平台 | item 进入 running 后超 1h 未终态 |
| `queue_timeout` | 排队超时 | fail | 平台 | 预留；v1 由 submission 3h 兜底，单独 queue 超时暂不细分 |
| `submission_timeout` | 整批超时收口 | fail | 平台 | submission 到 3h 硬上限时，仍在 `queued` 的 item 逐条广播此值；**某平台所有设备长期离线 / 抖动收不回来，最终也走这个** |
| `cancelled_by_request` | 被外部请求取消 | fail | 人为 | 外部调用取消接口命中 `queued` item |
| `stuck_detected` | 卡死保护触发 | fail | 平台 | 点击/滚动连续 N 次无效果，卡死检测命中 |
| `vlm_unavailable` | VLM 服务不可用 | fail | 平台 | VLM API 超时 / 5xx / 鉴权失败等外部依赖故障 |
| `device_unavailable` | 目标设备不可执行 | fail | 平台 | 调度时指定 deviceAlias 不 ready / 锁屏 / WDA 未就绪 |
| `executor_resource_lost` | 执行中资源丢失 | fail | 平台 | running 中设备掉线 / Agent 断链 |
| `executor_error` | 执行器内部异常 | fail | 平台 | Agent/Server 进程级异常、未分类错误兜底 |

- **为什么是 11 而不是最早的 12？** 2026-04-18 复盘时撤掉了一项 `platform_pool_unavailable`：
  - 原语义是"某平台所有设备 offline 时，queued 里属该平台的 item 逐条快踢"；
  - 复盘确认该行为太激进——agent 抖动 / USB 瞬断 / 设备拔插是**常态**而非异常，一次"平台瞬时全灭"若按快踢处理，一次抖动可能把整条队列全部打飞；
  - 实际策略：queued 继续等机器回来，等不到的统一交给 submission 3h 硬上限用 `submission_timeout` 收口；
  - `platform_pool_unavailable` 从 scheduler `STATUS_REASONS` 元组、对外清单、本表全部撤回，避免"登记了但没人用"的死字面量误导后人。
- **归类口径**（Analytics 稳定性 KPI 统一锚定这个分类）：
  - 成功（1）：`completed` — 分母里，不计失败；
  - 平台失败（8）：`run_timeout / queue_timeout / submission_timeout / stuck_detected / vlm_unavailable / device_unavailable / executor_resource_lost / executor_error` — **算稳定性分子**；
  - 业务/人为（2）：`assert_failed / cancelled_by_request` — 只计数，**不进 KPI 分母**。
- 规则：
  - `result = success` 时，`statusReason` **只能**是 `completed`；
  - `result = fail` 时，`statusReason` 必须是上表中除 `completed` 外的 10 项之一；
  - 上述枚举均为"结果层"解释；**不与** `rejectReason`（同步准入拒绝原因）共用。
- **`statusReason` 并不要求把所有技术细节塞进字面量**——具体错误堆栈、页面截图、卡死详情全部在 HTML 报告里体现，消费方需要深挖时打开报告看。

---

### P1. 同步准入返回初稿

- 背景
  执行器在收到请求后，会先做一次准入判断。这个阶段还没有真正开始执行，因此需要一组独立于广播结果的返回字段。
- 目标
  明确同步返回只负责表达“接不接单”，不混入执行阶段的结果语义。
- 为什么要做
  如果把准入拒绝原因和执行失败原因混在一套字段里，调用方会很难区分“没接单”和“接单后执行失败”。
- 不做什么
  当前不把同步返回设计得过胖，只保留最小必需信息。
- 优先级
  高。
- 依赖
  请求体结构、设备别名表、准入规则。
- 风险
  如果同步返回语义不清，后面外部平台会误以为“拒绝准入”也是一种执行失败。
- 结果产物
  一版最小同步返回契约。

当前评估与建议：

- 同步返回建议至少包含：
  - `accepted`
    中文：是否准入成功
  - `submissionId`
    中文：提交批次标识
  - `rejectReason`
    中文：拒绝原因
- 规则建议直接定成：
  - `accepted = true` 时，返回 `submissionId`
  - `accepted = false` 时，返回 `rejectReason`
- 第一版 `rejectReason` 初稿确定如下：
  - `invalid_request`
    中文：请求结构不合法
  - `invalid_device_alias`
    中文：设备别名不存在
  - `submission_too_large`
    中文：submission 内执行项超过上限
  - `duplicate_case_platform`
    中文：同一个 submission 内出现重复的 `caseId + platform`
  - `platform_no_available_resource`
    中文：包含所属端无可用资源，请调整后再试
- 这里的关键原则是：
  - `rejectReason` 只出现在同步准入返回中
  - `statusReason` 只出现在异步执行广播中
- 同步准入返回示例建议如下：

```json
{
  "accepted": true,
  "submissionId": "sub_20260421_xxx",
  "rejectReason": null
}
```

```json
{
  "accepted": false,
  "submissionId": null,
  "rejectReason": "platform_no_available_resource"
}
```

- 这两条示例分别表示：
  - 第一条：请求已被接收，执行器返回新的 `submissionId`
  - 第二条：请求未被接收，因为本次请求里包含所属端无可用资源

✅ 用户决策（2026-04-21 下午补决，`rejectDetail` 可选返回字段）：

- 当 `accepted=false` 时，返回体允许再带一个**可选**对象字段 `rejectDetail`，用于帮助调用方快速定位问题位置，例如：
  - `invalid_request` 场景 → `{ "itemIndex": 3, "missingField": "runContent" }`
  - `duplicate_case_platform` 场景 → `{ "itemIndex": 5, "conflictWith": 2 }`
  - `invalid_device_alias` 场景 → `{ "itemIndex": 7, "alias": "ios-foo-01" }`
  - `platform_no_available_resource` 场景 → `{ "platform": "harmony" }`
- `rejectDetail` **只作调试辅助**，字段结构随 `rejectReason` 浮动，调用方的正式判定逻辑仍只看 `rejectReason`。
- 不做"多 item 一次性汇总报错"；**发现第一处问题立即返回**，调用方修掉再重试。

---

### P0. 单执行单元状态机

- 背景
  执行器外部是 submission，内部真正流转的是一个个单独执行单元。取消、重试、广播、队列剔除，都需要状态机先定清楚。
- 目标
  定义单个 case 执行单元从准入到终态的最小状态流转。
- 为什么要做
  如果状态机不先定，取消逻辑和广播时机很容易互相打架。
- 不做什么
  不做过细状态爆炸，不引入一堆只为内部调试存在的中间态。
- 优先级
  最高。
- 依赖
  请求模型、调度模型、结果模型。
- 风险
  状态机一旦上线后再改，兼容成本很高。
- 结果产物
  一版最小状态枚举与流转规则。

当前评估与建议：

- 你这次抓得很准，最小状态机就应该先围绕“单执行单元”去定，而不是先围绕 submission。
- 这里“单执行单元”的中文意思就是：
  - 一个 `caseId` 在一个 `platform` 上的一次执行
- 当前建议的最小状态枚举：
  - `queued`
  - `running`
  - `success`
  - `failed`
  - `cancelled`
- 这五个已经足够支撑 v1。
- 建议流转规则为：
  - 准入通过后进入 `queued`
  - 被设备实际接单后进入 `running`
  - 正常完成进入 `success`
  - 执行中异常结束进入 `failed`
  - 在 `queued` 阶段被外部取消，进入 `cancelled`
- 关于取消，你的思路是对的：
  - 外部传 `submissionId + caseId + platform`
  - 执行器只尝试从队列里剔除对应单元
  - 已经进入 `running` 的不允许取消
  - 已经终态的直接忽略
- 所以取消规则可以直接定死成：
  - 只允许取消 `queued`
  - 不回滚 `running`
  - 已终态请求视为幂等忽略
- 取消完成后，仍然要正常广播结果，只是：
  - `result = fail`
  - `statusReason = cancelled_by_request`
- submission 自身的状态可以后面再从 item 聚合出来，不需要先成为核心状态机。

✅ 用户决策（2026-04-21 下午补决，submission 与 item 超时分层）：

- **`submission` 3h 为硬上限**（submission 接收后算起）。到点动作：
  - 仍在 `queued` 的 item → 逐条转终态，`result=fail`、`statusReason=submission_timeout`，逐条广播；
  - 已经 `running` 的 item → **不打断**，继续跑到自己的终态（`success / failed / run_timeout` 皆可）。
  - 换句话说：submission 3h 超时只"收口 queued"，不"回收 running"。
- **`item` 1h 超时**只在 item 进入 `running` 后开始计时。
  - `queued` 阶段的"不动"不由 item 层单独超时，由 submission 3h 兜底。
  - 到点动作：终止当前执行 → `result=fail`、`statusReason=run_timeout` → 正常广播。
- 超时判定后的设备处理：
  - 该设备锁立即释放，后续 item 正常调度到该设备；
  - 不做"因为超时就整台机标记异常"的操作。
- 状态流转补充：
  - `queued → cancelled`（`cancelled_by_request` 或 `submission_timeout`）
  - `running → failed`（`run_timeout / stuck_detected / vlm_unavailable / executor_error` 等）
  - 任何终态一经写入不可回退。

---

### P0. 设备别名与匹配策略

- 背景
  部分调用方会希望指定固定设备，例如某个环境包只装在部分真机上，因此需要一个轻量设备选择能力。
- 目标
  定义 `deviceAlias` 的角色、是否必填、匹配规则和匹配失败处理方式。
- 为什么要做
  没有设备别名，调用方只能按平台盲打；但如果把设备匹配设计得太重，又会拖慢执行器模型。
- 不做什么
  不在 v1 做复杂标签路由与表达式调度。
- 优先级
  高。
- 依赖
  设备池模型、设备注册信息、准入规则。
- 风险
  如果 alias 定义模糊，后面设备漂移和人工维护会很痛苦。
- 结果产物
  一版简单可用的设备选择规则。

当前评估与建议：

- 你的判断是对的，`deviceAlias` 不应该是强制字段。
- 建议先定成：
  - 有 `deviceAlias`：尝试按指定别名匹配设备
  - 无 `deviceAlias`：只按 `platform` 进入资源池调度
- 这里需要区分两种情况：
  - 压根没有这个别名设备
  - 有这个别名设备，但当前正忙
- 所以建议规则是：
  - `deviceAlias` 缺省：允许平台内任意可用设备
  - `deviceAlias` 存在且设备存在：只允许排队等待该设备
  - `deviceAlias` 存在但设备当前忙：进入队列等待，不降级到其他设备
  - `deviceAlias` 存在但系统内根本不存在该别名：准入直接拒绝，同步返回 `rejectReason = invalid_device_alias`
- `deviceAlias` 的定位建议是：
  - 人可读
  - 业务相关
  - 可稳定维护
- 不建议直接用：
  - udid
  - 序列号
  - 临时系统名
- 更适合的风格是：
  - `ios-wechat-prod-01`
  - `android-payment-staging-02`
  - `harmony-mainline-01`
- 后面如果你们要做更强的设备能力筛选，可以再引入：
  - `deviceTags`
  - `devicePool`
  - `capabilitySelector`
- 但这些都不应该进入现在这版最小协议。

- 暂无

---

### P0. 对外补查 API

- 背景
  Kafka 无法保证 Exactly Once，消费方一定会遇到"消息没收到 / 收到但处理失败"的场景，必须有一条"主动查"的兜底通路。
- 目标
  定义最小的同步查询 API，让调用方能按 `submissionId` 对账整批，按 `caseId + platform` 拉具体报告。
- 为什么要做
  没有对账 API，Kafka 丢消息后只能人工介入排查；执行器本来声称"事实层"，没有查口就等于白说。
- 不做什么
  不做分页、不做关键字搜索、不做跨 submission 聚合、不做时间区间筛选。
- 优先级
  最高。
- 依赖
  submission / item 存储、HTML 报告产物、结果广播契约。
- 风险
  如果字段过多或返回结构太重，会倒逼出"列表 / 搜索 / 导出"等下游需求，慢慢把执行器变成平台。
- 结果产物
  两个 GET 接口 + 返回体契约 + 过期策略。

当前评估与建议（已与用户对齐）：

- v1 仅开放两个查询接口，匿名访问（与 HTML 报告一致）：

**1. 整批汇总**

```
GET /api/submissions/{submissionId}/reports
```

返回体：

```json
{
  "submissionId": "sub_xxx",
  "acceptedAt": "2026-04-21T20:00:00+08:00",
  "expireAt":   "2026-05-06T20:00:00+08:00",
  "summary": {
    "total": 30,
    "success": 24,
    "fail": 4,
    "queued": 1,
    "running": 1
  },
  "items": [
    {
      "caseId": "case-001",
      "platform": "ios",
      "state": "success",
      "result": "success",
      "statusReason": "completed",
      "deviceAlias": "ios-wechat-prod-01",
      "reportUrl": "https://<host>/sub_xxx/case-001/ios.html",
      "startedAt":  "2026-04-21T20:02:00+08:00",
      "finishedAt": "2026-04-21T20:10:00+08:00"
    }
  ]
}
```

**2. 单 case 详情（JSON，等价报告数据源）**

```
GET /api/submissions/{submissionId}/cases/{caseId}/report?platform=ios
```

- `platform` 为**必传查询参数**（因为同 submission 内 `caseId + platform` 才唯一，一个 caseId 可能跨端）。
- 不传 `platform` 时若匹配到多条，返回 `400 multiple_matches`。
- 返回体字段与 HTML 报告一一对应（头部元信息 / VLM 轨迹 / 日志 / Token 汇总 / 错误上下文），方便调用方二次渲染。

**过期策略**：

- submission 超过 15 天，两个接口都返回 `404 expired`；HTML 报告 URL 同步失效。
- 不提供任何"归档恢复"能力。

**不提供**：

- 列表页 / 跨 submission 查询 / 基于时间区间筛选；
- WebSocket / SSE 实时推送；
- 对 submission 的手工编辑 / 状态回滚。

✅ 用户决策（2026-04-21 下午补决，对外取消 API）：

- v1 同时开放两条**匿名**（暂不鉴权）取消接口：

**3. 取消整批**

```
POST /api/submissions/{submissionId}/cancel
```

- 仅对该 submission 下仍处于 `queued` 的 item 生效；
- 已经 `running` / `success` / `failed` / `cancelled` 的 item：状态保持，不做任何变更；
- 对每条被取消的 item 立即触发一次广播（见下），相当于一次"失败结束"。

**4. 取消单条**

```
POST /api/submissions/{submissionId}/cases/{caseId}/cancel?platform=ios
```

- `platform` 必传（同查询接口，`caseId + platform` 才唯一）；
- 语义同上，仅对目标 item 是 `queued` 时生效。

**取消后的广播行为**：

- 取消成功的 item → 立即按状态机走终态 `cancelled`，通过 Kafka 下发一条广播消息，其中：
  - `result = fail`
  - `statusReason = cancelled_by_request`
  - `reportUrl = null`（**不生成 HTML 报告**，因为根本没跑过）
  - 其余字段按常规广播契约填（`submissionId / caseId / platform / deviceAlias? / finishedAt` 等）
- 调用方消费到此消息即视为该 item 已收口，后续不会再有任何广播。

**幂等与并发**：

- 取消接口幂等：对已经终态或已被取消过的 item，返回 200，body 里标注 `noop=true`；
- 对 `running` 中的 item，v1 **不做在途中断**——调用取消也不会打断正在跑的任务，接口直接返回 `noop=true, state=running`（后续版本再考虑软中断）；
- 整批接口返回体示例：

```json
{
  "submissionId": "sub_xxx",
  "cancelled": [
    { "caseId": "case-003", "platform": "ios" },
    { "caseId": "case-005", "platform": "android" }
  ],
  "noop": [
    { "caseId": "case-001", "platform": "ios", "state": "success" },
    { "caseId": "case-002", "platform": "android", "state": "running" }
  ]
}
```

**鉴权**：

- v1 **不做鉴权**，任何知道 `submissionId` 的调用方都能触发取消；
- 由调用平台自行保证 `submissionId` 不外泄；
- 后续如有权限需求，再增加 header / token 维度，v1 不预设。

---

### P0. 广播通道技术选型（Kafka v1）

- 背景
  广播通道选型直接影响调用方接入门槛和 ai-phone 侧的运维成本。公司已有 Kafka 基建，调用方大多为平台侧服务，走 MQ 通路比 HTTP webhook 更契合。
- 目标
  定义 v1 以 Kafka 为**唯一**广播通道的契约：topic 命名、分区策略、消息 schema、消费侧约定。
- 为什么要做
  不先定 topic / 分区，调用方接的时候会出现"一个 submission 跨多个分区、顺序错乱 / 消费慢"的问题。
- 不做什么
  不在 v1 做多通道并行（webhook + Kafka），不做消息层鉴权 / 订阅白名单（由 Kafka 集群 ACL 负责）。
- 优先级
  最高。
- 依赖
  公司 Kafka 集群（broker、topic 申请、ACL 配置）、结果幂等决策、HTML 报告生成。
- 风险
  分区键选错（例如按 `caseId`），同 submission 内不同 item 会跨分区，消费侧聚合困难；消息过胖（直接塞完整日志）会拖慢消费。
- 结果产物
  Kafka 接入契约 + 消息 schema + 消费侧使用说明。

当前评估与建议（已与用户对齐）：

- **通道**：Kafka（公司基建）。
  - 具体 broker 列表、topic 名、ACL 策略由用户方后续反馈，写入 `AI_PHONE_KAFKA_*` 环境变量。
- **topic 建议命名**：
  - 生产：`ai-phone.submission.result`
  - 开发/预发：`ai-phone.submission.result.dev`
- **分区键**：`submissionId`。
  - 理由：同一 submission 内的所有 item 终态落到同一分区，消费侧天然顺序；跨 submission 天然并行。
- **消息 value 结构（最小广播体，JSON）**：

```json
{
  "submissionId": "sub_xxx",
  "caseId": "case-001",
  "platform": "ios",
  "result": "success",
  "statusReason": "completed",
  "reportUrl": "https://<host>/sub_xxx/case-001/ios.html",
  "deviceAlias": "ios-wechat-prod-01",
  "startedAt":  "2026-04-21T20:02:00+08:00",
  "finishedAt": "2026-04-21T20:10:00+08:00",
  "durationMs": 480000
}
```

- **语义**：at-least-once。executor 侧 Producer 做 3 次指数退避重试；最终失败落 executor 自有"广播失败表"供后台补发（不回退 item 状态，item 已是终态）。
- **消费方约定**（对外文档写明，不侵入 executor）：
  - 消费者自行决定 consumer group；
  - **必须**按 `submissionId + caseId + platform` 三元组幂等去重；
  - 对 `result=fail` 按 `statusReason` 区分"平台问题 / 业务问题"，自行决定是否发新请求重跑（executor 侧不自动重跑）。
- **adapter 预留**：executor 内部将广播抽象为 `ResultPublisher` 接口，v1 实现 `KafkaPublisher`；未来若需多通道（webhook / 飞书机器人 / 邮件摘要），按 adapter 插入，不动主流程。
- **待办（阻塞项）**：用户方需补入 Kafka broker 地址 + topic 名 + ACL 账号密码；否则 v1 只能先在本地 mock producer 日志验证。

---

### P0. Agent 侧并发契约

- 背景
  Server 层已经规划了调度与准入，但真正跑的是 Agent。Agent 内部并发与容灾规则不定清，线上会出现"同台设备两个 item 互相踩踏""Agent 挂了 item 一直在 running"等问题。
- 目标
  定义 Agent 侧并发边界、设备连续执行行为、crash 恢复时的终态策略，以及与 Web 手工占用的互斥规则。
- 为什么要做
  Server 的调度优化没有 Agent 层配合，全部失效。
- 不做什么
  不做 Agent 内部任务队列重排、不做 item 级自动重试、不做跨 Agent 任务迁移。
- 优先级
  最高。
- 依赖
  Driver 抽象、Agent WS 协议、mirror 管线、设备锁机制、结果状态机。
- 风险
  并发规则写错（例如允许同设备并发两个 item），会出现 WDA / hdc 抢占导致的随机卡死，且很难复现。
- 结果产物
  Agent 并发与容灾契约 + 锁统一规则。

当前评估与建议（已与用户对齐）：

- **多设备并发**：同一个 Agent 下，多台**不同**设备**允许并发**各跑一个 item；每台设备的 VLM runner / Driver / mirror 会话互相独立，不共享状态。
- **同设备串行**：同一台设备在任意时刻**最多 1 个 item running**。由"设备锁"保证，与 Web 手工调试共用同一把锁。
- **item 之间不清场**：同一台设备连续两个 item，中间**不做强制复位**（不杀 app、不回首页、不重启 WDA / hmdriver2）。
  - 理由：`runContent` 本身就是语义化的执行指令，由调用方在内容里自行写明"打开 xxx app / 回到首页 / 重启 xxx"等起跑线动作；executor 不擅自做这个决定。
  - 好处：连续 case 快得多，且不干扰调用方预期。
- **Agent crash 处理**：
  - Agent 进程挂掉时，Server 端按 WS 断开 + 心跳超时检测，其上所有 `running` 的 item 转终态 `result=fail, statusReason=executor_error`，逐条广播；
  - 这些 item **不自动重跑**；
  - Agent 重连后按最新设备列表重新上线，不承接旧任务、不恢复旧 running 状态。
- **Web 手工占用与 item 调度走同锁**：
  - 锁起点 = "进入设备工作台页面"；终点 = "离开页面"（关 tab / 切页 / 关浏览器）。
  - 手工持锁期间，调度器不派 item 给该设备（调度视其为 busy）；
  - item running 期间，Web 看到设备 `busy`，无法进入工作台进行手动操作；
  - 不设手工占用时长上限（用户承诺"没人会长期占用，抢得过就抢"，保持策略简单）。

✅ 用户决策（2026-04-21 下午补决，平台并发无上限）：

- **不设置任何平台级并发上限**：
  - iOS / Android / HarmonyOS 各自的 `running item` 数量天然等于该平台**当前处于 `ready` 的设备数**，不再设置额外的人工上限（例如"iOS 最多并发 N"）；
  - 超过可用设备数的 item 继续在 `queued` 队列里排队，由调度器按"设备空闲即派发"的原则消化；
  - 平台内某台设备挂掉 → 该设备对应的 running item 按 `executor_error` 收尾，其余设备不受影响继续消化队列；
  - 结论：执行器侧唯一的并发硬约束 = **"一台设备同时最多 1 个 item"**，其余都是排队问题。

✅ 用户决策（2026-04-21 下午补决，Server ↔ Agent WS 协议升级）：

- 新模型下 Server → Agent 的执行命令协议需要同步升级；**对外 HTTP 层无感**（调用方感知不到这次升级）。
- 旧契约（现网）：

```json
{ "type": "start_run", "run_id": "...", "device_serial": "...", "goal": "..." }
```

- 新契约（v1 定稿，由执行侧负责在内部改造，不影响对外协议）：

```json
{
  "type": "start_run",
  "submissionId": "sub_xxx",
  "caseId": "case-001",
  "platform": "ios",
  "deviceSerial": "00008150-00041CAE3478401C",
  "deviceAlias": "ios-wechat-prod-01",
  "runContent": "打开微信，进入通讯录..."
}
```

- 对应的 Agent → Server 回报事件同步迁移：
  - `run_started / run_progress / run_finished` 统一携带 `submissionId + caseId + platform` 三元组，不再使用 `run_id`；
  - Agent 内部仍可以用本地自增 id 做日志关联，但**不出现在对外 WS 协议里**。
- 老字段过渡策略：
  - v1 切换期间允许同时接受 `run_id / goal`（旧）与 `submissionId / caseId / platform / runContent`（新）；
  - 收到旧字段时仅打 WARNING 日志，不做业务处理；
  - 一次全量上线后，`run_id / goal` 字段**直接删除**，不保留兼容。

---

## v1 执行优先级与硬约束（2026-04-21 下午重排）

### 核心思想

> **先把平台自身的"设备可调度入口 + 内部排队"立稳，再对外开放 API。**

历史上讨论时我们先把"对外契约"打磨得很细（请求体 / 广播 / 取消 / Kafka…），但真正的短板其实是
——**"设备看起来 online 却不能跑"**（iOS 锁屏、Harmony 锁屏、WDA 未起、hmdriver2 断连）。
只有先把 `ready` 与排队机制稳住，对外接口接进来才有意义。

### 梯队划分（按严格先后顺序执行，上一梯队未稳前不启动下一梯队）

#### 第 1 梯队 · 设备可调度入口（Readiness Gate）

为每台设备定义清晰的 `ready` 状态，让"设备能否被派单 / 能否进入 web 工作台"有统一判据。

**状态机**：`offline → online → ready → busy`

| 端 | `online` 判据 | `ready` 增量判据（read-only 探活） |
|---|---|---|
| Android | `adb devices` 可见 | 屏幕亮 + 未锁（`dumpsys window` / 已有 driver 探测）+ `scrcpy` 通路通 |
| iOS | `pymobiledevice3` 列出 | WDA `GET /status` 200 + `GET /wda/locked` = false + MJPEG 9100 可连 |
| Harmony | `hdc list targets` 可见 | 屏幕亮（`hidumper -s PowerManagerService` 或 `hmdriver2.display.screen_on`）+ `hmdriver2` 握手成功 |

**探活节奏**：
- **5 秒轮询**每台设备一次；
- 连续失败 3 次才降级为 `online 非 ready`；
- **只读、旁路**——绝不触发执行流程，不做自动唤醒解锁。

**派生规则**：
- 准入阶段只看"该平台是否至少 1 台 `online`"（v1 不变）；
- 调度阶段**只派 `ready`**；`online` 但非 `ready` 的设备跳过，item 继续排队；
- Web 设备列表实时反映四态 + 透出 `not_ready_reason` 枚举。

**`not_ready_reason` v1 枚举**：
`screen_locked / wda_not_ready / hmdriver2_disconnected / adb_offline / driver_probe_failed`

#### 第 2 梯队 · 内部排队机制 + Web 展示

目标：把"排队"这件事在本平台内部跑通，**不开放外网入口**就能端到端演练 queued → running → completed。

- **队列**：内存 + DB 双写（重启不丢），按 `platform` 分池，FIFO，不支持优先级；
- **调度循环**：事件驱动，不轮询（设备 busy→ready 触发 drain / 新 item 入队触发 drain）；
- **Web 新增两页**（不动现有工作台）：
  1. **设备总览页**：每台设备 `离线 / 未就绪 / 空闲 / 占用中` + `not_ready_reason` + 当前占用方；
  2. **队列总览页**：按 `platform` 分栏显示 queued / running；
- **内部提交入口**：在设备总览页加一个"手工投递一条 item"按钮（只走内部接口，外网拿不到），用来在没有 Kafka / HTML 报告之前就把排队链路打通。

#### 第 3 梯队 · 对外 HTTP API + Kafka + HTML 报告 + 取消 API

前两梯队稳了之后，剩下的只是"把队列入口开到外网 + 把终态广播出去"。
- 准入 `POST /api/submissions`（body = `[{},{}]`）；
- 查询两条（整批汇总 / 单 case 详情）；
- 取消两条（整批 / 单条，匿名不鉴权）；
- Kafka 广播（`ai-phone.submission.result`，分区键 `submissionId`）；
- HTML 报告同步生成 + 15 天过期；
- **开发期 broadcast backend 开关**：`AI_PHONE_BROADCAST_BACKEND=kafka|stdout`，默认 `stdout`，没接 Kafka 前广播事件打日志。

##### 第 3 梯队落位进度（2026-04-22）

> 全部完成（Kafka broker 未到位，KafkaPublisher 以 mock 形态占位，broker 到手后只替换 `_send_async`）。

- ✅ **ResultPublisher 抽象 + 三种实现**：`backend/ai_phone/server/submissions/publisher.py`
  - `StdoutPublisher`（默认）：单行 JSON 打到 loguru，`bind(broadcast=True)`，tail 日志即可观察终态流。
  - `KafkaPublisher`（mock）：broker 地址未配置时进 mock 模式，结构化日志带 `topic / kafka_key / bytes / payload`，broker 到手时替换 `_send_async` 改为 `aiokafka.AIOKafkaProducer.send_and_wait` 即可，其他零改动。
  - `NullPublisher`：`AI_PHONE_BROADCAST_BACKEND=null` 关闭广播（仅单测用）。
  - 工厂 `make_publisher(settings)` 按环境变量选实现，未知值回落 stdout 并打 WARN。
- ✅ **终态事件统一 schema**：`backend/ai_phone/server/submissions/events.py`
  - 字段：`event / version / ts / submissionId / caseId / platform / state / statusReason / runId / deviceSerial / deviceAlias / startedAt / finishedAt / elapsedMs / steps / tokenStats / reportUrl / origin`。
  - cancelled queued / submission_timeout 的 item：`runId=null`、`reportUrl=null`、`elapsedMs=null`，但广播一样发（让外部能收到"整批超时"信号）。
- ✅ **HTML 报告同步生成**：`backend/ai_phone/server/submissions/reports.py`
  - 落盘路径 `storage_dir/reports/<submissionId>/<caseId>__<platform>.html`，对外 URL `/files/reports/...`（走已有 `mount_static`）。
  - 自包含 HTML：元信息 + runContent + 步骤时间线（含 before/after 截图 `<img>`）+ 日志时间线 + tokenStats，无外部 CDN 依赖。
  - 报告落盘 + 广播发射都放在 scheduler `_finalize_and_publish`，异常全部吞掉 **不回滚** item 终态。
- ✅ **scheduler 三处终态挂广播**（零侵入 on/off 主路径）：
  - `on_run_done` → commit 后生成 HTML、广播（success / failed / cancelled from running）。
  - `cancel_submission` / `cancel_item` → queued 直接转 cancelled 的那一批也广播一次（`reportUrl=null`）。
  - `_scan_timeouts` → submission_timeout 把 queued 踢出时逐条广播。
- ✅ **对外 HTTP API（匿名）**：`backend/ai_phone/server/submissions/public_routes.py`，前缀 `/api/submissions`
  - `POST /api/submissions`：body 直吞 `[{}, {}]`，`origin=external`。
  - `GET /api/submissions/{id}`：整批汇总 + 每 item 的 `report_url` + 状态计数。
  - `GET /api/submissions/{id}/items/{case_id}/{platform}`：单 item 详情，含 Run + Steps + Logs。
  - `POST /api/submissions/{id}/cancel` / `POST /api/submissions/{id}/cases/{case_id}/cancel?platform=<p>`：匿名取消。
  - 内部 `/api/internal/submissions` 保留（Bearer）。
- ✅ **15 天对外窗口**：`AI_PHONE_SUBMISSION_EXTERNAL_RETENTION_DAYS`（默认 15）
  - 计算时机：对外 API 查询时按 `finished_at` / 兜底 `expire_at` + retention 判定。
  - 过期行为：对外 API 统一 `404 {"error": "expired", ...}`；内部 API + 前端 Queue 页继续可见（用户明确要求"只打标记"）。
  - 数据真删留后期单独任务。
- ✅ **SubmissionItem 新增 `report_url` 字段**：`to_dict()` 返回里加，只在 `run_id` 存在且 state ∈ {success, failed} 时非空；内部 & 对外 API、前端 Queue 页免改即可拿到链接。
- ✅ **前端 Queue 页**：item 卡片右侧 / 抽屉顶栏加"查看报告"按钮（基于 `report_url`，另开新标签）。
- ✅ **报告改版 v1.1（2026-04-22 二次迭代）**：参考 `sonic_all_ai/pc_agent/action.py::_generate_report` 的深色主题 + 步骤折叠卡片，自己重写，零外部依赖。
  - **图文一体**：单 case 报告里每个步骤是可折叠卡片（默认全展开），卡片内同时展示 `thought / action / 截图前后 / 该 step 的日志`，按 `RunLog.step` 分桶嵌入；未挂任何 step 的启动 / 收尾日志归到末尾"运行级日志"区。
  - **批次汇总改为 SPA**：`_summary.html` 单文件，左侧侧边栏 case 列表（带状态徽章 + 平台 chip + 耗时），右侧主区切换显示完整 case 报告（与单 case 同模板），无 iframe 无跳转。默认选中第一条 `failed` / `cancelled`，全成功时选第一条；`cancelled queued` 无 Run 的 item 也能渲染（步骤区显示"本条目尚未真正执行"占位）。
  - **截图点击放大**：内置纯 JS modal，所有截图统一 `_aiPhoneShowImg(src)`。
  - **复用渲染器**：把"单 case 内容片段"抽成 `_render_case_inner()`，单 case 报告 = 框架 + 片段；批次 SPA = 顶栏 + 多份片段（按 `data-case-key` 隐藏切换），从根本上保证两边视觉一致。
- ✅ **报告改版 v1.2（2026-04-22 三次迭代）**：按用户反馈"单 case 不要分离展示、与 run 执行时日志一样图文混排"、"批次是嵌套关系，case 为单元切换"、"全部中文化"、"引入 case_name"四点改造，`REPORT_VERSION = "1.2"`。
  - **时间线流式视图**：`_render_case_inner()` 把 `RunStep`（按 `created_at`）+ `RunLog`（按 `ts`）合并为统一 `_TLEntry` 列表按时间正序渲染；日志条就是紧凑一行（时间 / 级别 / 消息），步骤是一条"胖行"（步骤号 + action tag + 耗时 + 思考 + 动作 + 操作前后双图）。单 case 彻底去掉折叠，从上到下一个流，与 Web Queue Run 抽屉里看到的日志流观感一致。
  - **批次嵌套**：`_render_summary_spa_html()` 左侧列表的切换单元从 case_id 升级为"执行单元"（用 `case_name` 展示，副标附 `case_id` / 平台 / 耗时），右侧切到所选单元的时间线流式视图——与单 case 同模板，做到"点进去就跟单 case 一样"。
  - **中文化**：顶栏 chips 全中文（用例 ID / 平台 / 设备 / 步数 / 耗时 / 开始 / 结束 / Run ID / 状态原因 / 来源 / 总数 / 总耗时）、状态徽章（成功 / 失败 / 已取消 / 运行中 / 排队中 / 已完成 / 已过期）、`statusReason` 12 项全部有中文映射、平台 `android → Android / ios → iOS / harmony → HarmonyOS`、耗时人类化（ms → s → m+s）。
  - **`case_name` 贯穿全链路**：`SubmissionItem` 新增 `case_name`（nullable，空时回落 `case_id`）；`ItemDraft.case_name` + `parse_and_validate` 解析外部 `caseName` 字段；`admit` 写入 DB；`SubmissionItem.to_dict()` 暴露 `case_name`；`events.build_terminal_event()` 新增 `caseName` 字段；报告标题 / SPA 侧栏列表 / 抽屉单元名一律用 `case_name`，`case_id` 退到 chips / tooltip / 副标，视觉上更像"注册 · 主流程"而不是裸的业务 key。
  - **前端**：Queue 页排队小卡与详情 item 卡都改成"主标题 case_name + 副标/chip case_id"；手工投递表单新增"caseName (可选)"输入框，预览区也显示 case_name；三批示例数据全部补上 case_name 字段；`npm run build` 通过。
- ✅ **批次汇总报告 + submission.terminal 事件**（2026-04-22 追加）：
  - `reports.build_submission_summary_html()` 在最后一条 item 终态时同步生成 `storage_dir/reports/<submissionId>/_summary.html`，对外 `/files/reports/<id>/_summary.html`，包含批次元信息、state 计数、平台分布、每条 item 行（带状态徽章 + statusReason + device + 耗时 + 单条 HTML 链接）。
  - `events.build_submission_terminal_event()` 组 `submission.terminal` 事件（`event / version / submissionId / origin / submissionState / acceptedAt / finishedAt / totalItems / counts / platformCounts / totalElapsedMs / summaryReportUrl`），与 item 级事件复用同一个 publisher → 同一个 Kafka topic + 同一个分区键 `submissionId`，消费方按 `event` 字段分流。
  - scheduler 新增 `_maybe_finalize_submission()`：每条 item 终态后 ping 一次，全部终态时 (1) `accepted → done`、补 `finished_at`；(2) 生成汇总 HTML；(3) 广播 `submission.terminal`。进程内 `_finalized_submissions` 集合做幂等去重，避免 cancel/timeout 路径下 N 条 queued 同时收口时重复触发。
  - `Submission.to_dict()` 新增 `summary_report_url`（仅 `finished_at` 非空时给值），内部 + 对外 API + 前端 Queue 页直接拿到链接。
  - 前端 Queue 页：列表项右上挂 "📊 汇总报告" 小标签，详情顶栏挂 "📊 批次汇总报告" 按钮；演示数据 subC 已带链接，可直接预览交互。
- ⏸ **KafkaPublisher 真实接入**（待 broker）：
  - 配置占位已落在 `Settings`：`AI_PHONE_KAFKA_BROKERS / AI_PHONE_KAFKA_TOPIC / AI_PHONE_KAFKA_SASL_USERNAME / AI_PHONE_KAFKA_SASL_PASSWORD`。
  - 切换 `AI_PHONE_BROADCAST_BACKEND=kafka` 已经能打出带 `topic / key / payload` 的 mock 日志，等 broker 到手在 `publisher.py::KafkaPublisher._send_async` 内部替换成 aiokafka 即可。

##### 本地验证 checklist（第 3 梯队）

1. `uvicorn ai_phone.server.app:app --reload`，观察启动日志里 `[broadcast] publisher=stdout backend=stdout`。
2. 在 Queue 页"手工投递"跑一条到终态 → 应看到：
   - 服务端日志有一行 `[broadcast:stdout] {... "event": "submission.item.terminal" ...}`；
   - item 卡片出现"查看报告"按钮，点开是自包含 HTML（含截图、步骤、日志）；
   - `/api/submissions/{id}`（**对外路由**）匿名可查，返回 `report_url`。
3. 切 `AI_PHONE_BROADCAST_BACKEND=kafka` 重启 server → 终态时日志变成 `[broadcast:kafka-mock] topic=ai-phone.submission.result key=<subId> ...`，功能完全一致。
4. 把环境变量 `AI_PHONE_SUBMISSION_EXTERNAL_RETENTION_DAYS=0` 临时设为 0 重启 → 对外 API 立即对历史批次返 `404 expired`；内部 API + Queue 页照常。
5. **批次汇总报告**：投递 ≥2 条 item 的 submission，等所有 item 终态。期望日志依次出现 `submission.item.terminal × N` 然后 `submission.terminal × 1`；查 `_summary.html` 落盘成功，前端列表 / 详情各有一个 "📊 汇总报告" 入口。再调一次 `cancel_submission`（其中夹带正在 running 的 item）：cancelled queued 那批立刻广播 item.terminal、submission.terminal 不发；等到 running 那条 run_done 回流后才补发一条 submission.terminal（验证幂等去重）。

#### 第 4 梯队 · 收尾

- 12 项 `statusReason` 全路径落位；
- `README.md` / `架构设计.md` 对外契约章节按新模型重写；
- `/api/runs` 加 `deprecated` 标记，不删不改。

---

### DB 手工迁移清单（无 Alembic 期间的约定）

团队约定：在引入正式 migration 工具前，**schema 变更走手工 SQL**，开发在 ORM
里同步字段即可；`init_db()` 只做 `create_all`（建缺的表），不做 `ALTER
COLUMN`。每次给 ORM 加字段时，把对应 SQL 追加到下面，以便新环境启动 / 老环境
升级时回查。

**PostgreSQL 执行顺序**：按"新 → 旧"从上往下检查，已经有的列跳过即可。

| 日期 | 表 | 变更 | SQL |
|---|---|---|---|
| 2026-04-22 | `submission_items` | 新增 `case_name` 列（展示用例名称，可选，空时回落 `case_id`）| `ALTER TABLE submission_items ADD COLUMN IF NOT EXISTS case_name VARCHAR(255) NOT NULL DEFAULT '';` |
| 2026-04-18 | `submissions` | 新增 `submission_name` 列（批次名称 / 外部"集合"概念，可选，空时回落 `id`）；配合对外 wrapper 格式 `{submissionName, items}` | `ALTER TABLE submissions ADD COLUMN submission_name VARCHAR(255) NOT NULL DEFAULT '';`（PG 9.4 不支持 `ADD COLUMN IF NOT EXISTS`，先按下方注意事项查列再加） |
| 2026-04-18 | `device_aliases`（**新表**）| v1.4：设备别名一对一映射；`deviceAlias` 外部契约改为严格匹配 | 见下方 SQL 块 |

```sql
-- v1.4 设备别名表（serial ↔ 友好名一一映射，软引用，不 FK 到 devices）
CREATE TABLE device_aliases (
  serial     VARCHAR(128) PRIMARY KEY,
  alias      VARCHAR(128) NOT NULL UNIQUE,
  note       TEXT         NOT NULL DEFAULT '',
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX idx_device_aliases_alias ON device_aliases (alias);
```

如果 PG 版本 < 9.6（不支持 `IF NOT EXISTS`），先 `SELECT column_name FROM
information_schema.columns WHERE table_name='xxx' AND column_name='yyy';` 查一眼再 `ALTER`。

---

### 硬约束：三端执行流程冻结清单

**本次整个 v1 落地期间，以下内容只允许新增旁路，不允许修改主路径**：

| 不允许改的文件/模块 | 原因 |
|---|---|
| `backend/ai_phone/agent/drivers/{android,harmony,ios,ios_wda_launcher,hdc,adb}.py` 主路径 | 三端驱动目前稳定，改动风险过高 |
| `backend/ai_phone/agent/mirror/*` | 三端镜像管线刚刚稳定（iOS mjpeg / Harmony hypium / Android scrcpy） |
| `backend/ai_phone/agent/runner/*` | VLM 执行循环 |
| `/api/runs` 现有路径 / `/ws/agent` 现有握手 / 手工触控链路 | 现网链路不能被 v1 改动波及 |

**允许新增**（全部是独立新模块，零侵入）：
- `backend/ai_phone/agent/health/*` —— 旁路 readiness probe 聚合
- `backend/ai_phone/server/scheduler/*` —— 新调度器
- `backend/ai_phone/server/submissions/*` —— 准入 / 查询 / 取消 / 广播
- `frontend` 新增两页（设备总览 / 队列总览），不改工作台

---

### 第 2 阶段（大盘 Analytics，2026-04-18）验收记录

大盘是"纯读聚合 + 手动 AI 分析"的旁路页面，不改主路径、不影响任何第 1 梯队链路。

**新增模块**：

- `backend/ai_phone/server/analytics/aggregator.py` —— 单日聚合（按 `settings.analytics_timezone` 切片）
- `backend/ai_phone/server/analytics/ai.py` —— AnalyticsAIClient（豆包 Chat Completions，一次性纯文本、不带会话）
- `backend/ai_phone/server/api/analytics.py` —— `/api/internal/analytics/summary` + `/api/internal/analytics/ai-analyze`（Bearer）
- `web/src/pages/Analytics.vue` —— 大盘页（日期切换 + 4 卡片 + 集合块 + AI 手动按钮），路由 `/analytics`

**口径约束**：

- 一切"日"均按 `settings.analytics_timezone`（默认 `Asia/Shanghai`）切片，DB 里时间戳仍是 UTC
- 设备健康分两层：**当日** = 仅当日被调度过的设备；**历史** = 所有 Run 聚合的成功率（和日期无关）
- AI 分析只允许最近 `analytics_ai_max_age_days` 天（默认 3，即今天/昨天/前天），不允许分析未来日期
- AI 分析 **不带重试**（遵循用户早期决策：VLM 不可达直接返回错误）
- AI 分析不跨天、不带历史上下文；每次手动触发都是独立一次调用
- 集合块 **不内嵌 HTML 报告**，只给"汇总报告"外链（点击新页签打开）

**不做的事**：

- ❌ 不做数据清理任务（留到后续独立任务）
- ❌ 不做 WebSocket 推送；大盘每 20s 轮询一次 summary（仅在"今日"开启），AI 必须手动点按钮
- ❌ 不做跨日对比 / 周月聚合（先把单日做扎实）

**无 DB 迁移**：全部从现有表（submissions/submission_items/runs/run_logs/devices）做只读聚合。

---

### 第 3 阶段（设备别名 v1.4，2026-04-18）验收记录

把"外部 `deviceAlias` ≈ serial/model 前缀"的松散语义升级为**严格别名表**，
让外部系统可以把 serial 当黑盒、只认平台给的业务名。

**新增模块**：

- `backend/ai_phone/server/aliases/{store,service}.py` —— CRUD + 整批 `validate_aliases`（查不到抛 `AliasError`）
- `backend/ai_phone/server/api/device_aliases.py` —— 内部管理 API（Bearer，GET/PUT/DELETE）
- `backend/ai_phone/server/api/devices.py::list_available` —— 匿名对外 `GET /api/devices/available`
- `web/src/pages/DeviceGrid.vue` —— 设备卡右上角"改名"入口 + 编辑弹窗（含"删除绑定"）
- `web/src/lib/api.js::internal.deviceAliases.*` + Queue / Analytics 里 serial → alias 展示降级

**契约变更（对外 v1.4）**：

- `deviceAlias` 含义由"模糊前缀匹配"→ **严格别名**：`device_aliases` 表查不到 → 整批 400 `unknown_device_alias`；未传或空串 → 调度器在平台池里随便挑
- 新增 **匿名** `GET /api/devices/available`：暴露完整 `serial` + `alias`（可能为空串，代表还没绑）
- `rejectReason` 新增 `unknown_device_alias` / `no_device_on_platform` 两项（后者只是补录，原来就会在准入阶段拒）
- 已在 `对外调用清单.md` v1.4 变更记录里同步

**口径约束**：

- 严格 1:1：`serial` 主键、`alias` 全局唯一；同一别名不可绑到两台设备（DB 层 UNIQUE 拦）
- 别名可改可删；删除不影响运行中 Run，但后续对该 serial 的指名调用会 400
- 别名表**不 FK** 到 `devices`，支持"先规划、后插设备"——即插即用语义
- `_pick_device`：传了 alias 只查别名表反查出的 serial；没传 alias 沿用老的池子随便挑

**不做的事**：

- ❌ 不做"同一 serial 多别名"（场景少、徒增歧义）
- ❌ 不做"别名注释在对外 API 暴露"（note 只对内部管理面可见）
- ❌ 不做"alias 历史审计 / 谁改的"（规模小、有 updated_at 即可）

**DB 迁移**：见上方"现场 DB 迁移"新增的 `device_aliases` 建表 SQL。

---

### 第 4 阶段（收尾 v1.5，2026-04-18）验收记录

两件小事一次性收齐，不动主路径。

**1. `statusReason` 枚举从 12 项精简到 11 项**

- `scheduler/service.py::STATUS_REASONS` 撤掉 `platform_pool_unavailable`（无人 emit 的死字面量）；
- `analytics/aggregator.py` 的 `PLATFORM_FAILURE_REASONS / BUSINESS_FAILURE_REASONS` 从"老版 8 项（`vlm_format_invalid / stuck_no_progress / unknown_action / device_offline / internal_error / step_limit / user_abort / ...`）"一次性**校准到 v1 的 11 项**（8 平台 + 2 业务 + 1 成功），两个 frozenset 的并集严格 = `STATUS_REASONS \ {"completed"}`；
- `对外调用清单.md` 的 status_reason 表完全重写（之前是一套和代码对不上的旧名 `device_offline / cancelled_by_user / assert_fail / internal_error / no_eligible_device / dispatch_timeout / vlm_error / cancelled_by_shutdown` 全部撤回），新表锚定实际 emit 值并打"平台/业务/人为"归类标签；
- 变更记录里挂 v1.5 条，并明确写出"平台所有设备同时离线 / agent 抖动时不快踢队列，靠 submission 3h 以 `submission_timeout` 统一收口"的口径。

**2. iOS WDA 续签与 Xcode 依赖结论（明确写进文档，避免日后再讨论）**

iOS WDA 跑的是 `xcodebuild test -allowProvisioningUpdates`，`-allowProvisioningUpdates` 让 Xcode 在**每次子进程启动**时自动续签一次（免费 Apple ID 7 天 / 付费 1 年都走这条）。

| 场景 | 是否需要人工 | 说明 |
|---|---|---|
| 免费 Apple ID 7 天到期 | ❌ 自动 | 证书失效后 `xcodebuild test` 子进程会自己崩，`ios_wda_launcher._respawn_once(runtime_drop)` 自动 kill + 重启 → 触发自动重签；agent 主进程不重启，该设备 WDA 瞬断 10-20s（增量编译缓存命中）|
| 付费开发者 / 企业账号 1 年到期 | ❌ 自动 | 同上；只要 Apple Developer Portal 上账号本身没过期，`-allowProvisioningUpdates` 会自动续 |
| 付费账号本身到期 | ✅ | 每年一次到 developer.apple.com 续费，keychain 里换一下最新的 cert/profile |
| 新 iPhone 第一次接入 agent 机 | ✅ 一次性 | iPhone 上信任电脑 / 信任开发者 / Xcode 里选 Team / 改 Bundle Identifier |
| 老 iPhone 日常维护 | ❌ | 全自动 |

**所以 agent 不需要因为"要重签"而重启**——重签只是 xcodebuild 子进程级事件，agent 主进程一直在跑。

**Xcode 依赖**（要诚实对用户说明）：

- `xcodebuild test` 是 CLI 工具，**要求每台 agent Mac 装 Xcode.app**（~20GB 磁盘，`xcode-select -s` 指一下即可）；
- **不需要打开 Xcode GUI**、不需要日常操作 Xcode；
- 企业开发者账号的好处不是"不用 Xcode"，是**不用在 developer portal 给每台 iPhone 注册 UDID**——值得向公司申请。

**潜在的"agent 机不装 Xcode"方案**（不推荐现在做，记下备忘）：

- 找一台 build 机装 Xcode，用企业账号一次性签好 `WebDriverAgent.ipa`；
- agent 机只装 `tidevice` / `pymobiledevice3`，用 `tidevice install / xctest` 推 ipa + 启动 XCTest；
- 违反当前"三端执行流程冻结"原则（要整体替换 `ios_wda_launcher.py` 的 spawn 路径），且 iOS 17/26 上 `tidevice xctest` 稳定性还在追 Apple 的 CoreDevice 新协议；
- 等 Apple 出正式 CLI 工具或 tidevice 在 iOS 17+ 上稳定后再考虑，不急。

---

### 本次重排后的决策回顾（2026-04-21 下午晚些）

- ✅ 执行优先级：**设备 Readiness → 内部排队 + Web → 对外 API → 收尾**，禁止跳阶；
- ✅ 三端执行流程冻结：只新增模块，不改主路径；
- ✅ Readiness 探活节奏：5 秒轮询，失败 3 次降级，read-only；
- ✅ `not_ready_reason` 5 项：`screen_locked / wda_not_ready / hmdriver2_disconnected / adb_offline / driver_probe_failed`；
- ✅ 第 2 梯队加"内部投递按钮"做端到端演练；
- ✅ 开发期广播后端开关：`AI_PHONE_BROADCAST_BACKEND=kafka|stdout`，默认 `stdout`；
- ✅ 新模块落位：`server/submissions/`、`server/scheduler/`、`agent/health/`。

---

## 待确认问题

> 原来这里挂过 3 条，2026-04-18 盘点时都已经收口，留着是为了后人能看到演进路径。

- ✅ **广播只发终态**：已定。v1 只广播 item / submission 终态事件，不发 `queued / running` 中间态；中间态通过 `GET /api/submissions/{id}`、`GET /api/submissions/{id}/items/{caseId}/{platform}` 拉取。
- ⏸ **Kafka broker / topic / ACL**：`KafkaPublisher` 以 mock 形态已经落位，`AI_PHONE_KAFKA_*` 环境变量占位已留；broker 地址到手后把 `KafkaPublisher._send_async` 替换为 `aiokafka.AIOKafkaProducer.send_and_wait`，零改动其他模块。
- ⏸ **`AI_PHONE_REPORT_HOST`**：报告已经自包含落盘（`storage_dir/reports/<submissionId>/...`）+ 通过 FastAPI `/files/reports/` 静态挂载访问；公司内网报告 host 接入时只改这一个 env 就能把对外 URL 改成公司地址。
- ⏸ **其他部署类依赖**：日志服务 / k8s 模板 / 监控告警，用户明确"不做/等要部署时再谈"，不在 v1 范围里。

---

## 风险记录

- 如果先写代码再补概念，后面“submission / caseId / platform / 设备池”会互相打架。
- 如果把“在线”误判成“可执行”，调度系统会经常接单后失败。
- 如果 submission 内允许重复 `caseId + platform` 进入队列，后面广播、报告、取消、重试都会混乱。
- 如果只广播不存事实记录，Kafka 丢消息后将不可追溯。

---

## 决策记录

- 2026-04-21：文档创建，作为后续讨论与计划沉淀入口。
- 2026-04-21：确认 `ai-phone` 的下一阶段核心不是“继续像 Sonic 一样扩平台”，而是把“AI 云真机执行器”的调度、准入、健康、并发、结果契约立住。
- 2026-04-21：初步倾向由执行器内部生成 `submissionId`，外部不再传该字段；外部最小识别优先围绕 `caseId` 收敛。
- 2026-04-21：确认前置依赖概念暂不进入 v1；`ai-phone` 只接收最终可执行的 `runContent`，由调用方自行完成内容拼接。
- 2026-04-21：确认 v1 外部契约不引入 `sessionId`；单次执行结果以 `submissionId + caseId + platform` 作为外部主关联键。
- 2026-04-21：确认广播先只发送终态事件；`statusReason` 作为结果解释字段保留。
- 2026-04-21：确认 `deviceAlias` 为可选字段；指定别名设备存在但忙时排队等待，只有别名不存在才拒绝准入。
- 2026-04-21：确认单次执行项在同一个 `submission` 内以 `caseId + platform` 作为唯一键。
- 2026-04-21：确认广播结果字段采用二值：`result = success / fail`；`statusReason` 负责解释具体原因。
- 2026-04-21：确认设备占用统一走锁模型；手工调试占用与自动执行占用没有区别。
- 2026-04-21：确认超时规则：`submission` 超时 3 小时，`item` 超时 1 小时。中文含义分别是“一整批请求包最多存活 3 小时”和“单条执行项最多运行 1 小时”。
- 2026-04-21：确认容量上限：一个 `submission` 内的执行单元不超过 500 个。中文含义是“一整批请求包里最多允许 500 条执行项进入准入与调度”。
- 2026-04-21：确认某个平台资源池整体不可用时，不做整批 `submission` 一起失败，而是将受影响的平台执行项逐条失败、逐条广播。中文含义是“这个端整体已经没有可执行设备后，所有再也等不到该端资源的项，按单条失败顺序收尾”。
- 2026-04-21：确认同步准入返回中的 `rejectReason` v1 初稿为：`invalid_request / invalid_device_alias / submission_too_large / duplicate_case_platform / platform_no_available_resource`。
- 2026-04-21 下午：`runItem / runId` 概念彻底废弃；代码、文档、日志、报告中一律禁止出现；外部统一以 `submissionId + caseId + platform` 为关联键，内部仅保留数据库主键。
- 2026-04-21 下午：广播幂等键 = `submissionId + caseId + platform` 三元组；不引入消息级 `messageId`；消费方按三元组自行去重；v1 广播语义为 at-least-once。
- 2026-04-21 下午：结果数据（元数据 + 终态 + HTML 报告 + 关键截图 + 广播记录）保留 **15 天**；补查 API 对过期 submission 统一返回 `404 expired`。
- 2026-04-21 下午：HTML 报告 URL 规则 = `${AI_PHONE_REPORT_HOST}/{submissionId}/{caseId}/{platform}.html`；匿名访问；**同步生成完毕再触发终态广播**；预期 1-3s 广播延迟。报告生成异常降级为 `reportUrl` 指向 JSON 接口，不阻塞广播。
- 2026-04-21 下午：准入的"平台资源视图"定义 = "该平台至少 1 台设备 `online`（不要求 ready）"；一台都没有才拒绝该平台的 item；准入**不做资源预留**，抢占发生在调度阶段。
- 2026-04-21 下午：submission 3h 超时 = 所有仍在 `queued` 的 item 逐条广播 `submission_timeout`；`running` 的 item 不打断，继续跑到自身终态；item 1h 超时仅在进入 `running` 后开始计时。
- 2026-04-21 下午：`statusReason` v1 枚举定稿 12 项——在初稿 8 项（`completed / queue_timeout / run_timeout / executor_resource_lost / platform_pool_unavailable / device_unavailable / cancelled_by_request / executor_error`）基础上补充 `submission_timeout / vlm_unavailable / assert_failed / stuck_detected`。
- 2026-04-18：`statusReason` 精简到 **11 项**——撤掉 `platform_pool_unavailable`。该枚举本意是"平台全灭快踢 queued"，复盘后确认过激：agent 抖动 / USB 瞬断 / 设备拔插是常态，一次瞬时全灭按快踢处理会把整条队列打飞。改为 queued 继续等机器回来，等不到由 submission 3h 硬上限以 `submission_timeout` 统一收口。影响面：`scheduler.STATUS_REASONS`、`对外调用清单.md`、本文 P1 枚举表同步。
- 2026-04-21 下午：v1 广播通道选型 = **Kafka**（公司基建），topic 建议 `ai-phone.submission.result`，分区键 `submissionId`，at-least-once；broker/topic/ACL 具体配置待用户方反馈。
- 2026-04-21 下午：对外补查 API v1 = `GET /api/submissions/{id}/reports`（整批汇总）+ `GET /api/submissions/{id}/cases/{caseId}/report?platform=<p>`（单 case 详情）；匿名访问；15 天后 `404 expired`；不提供列表 / 搜索 / 分页。
- 2026-04-21 下午：Web 手工占用锁点 = "进入设备工作台页面"；离开页面自动释放；与自动调度共用同一把锁；不设手工占用时长上限。
- 2026-04-21 下午：Agent 并发规则 = 同 Agent 下多台不同设备可并发；同一台设备同一时刻最多 1 个 item `running`；**item 之间不做清场**，起跑线由 `runContent` 自行负责。
- 2026-04-21 下午：请求体根直接就是 JSON 数组 `[{}, {}]`，不再使用 `{ "items": [...] }` 外层对象包装；根为非数组 / 空数组 / 元素非 object → 整批拒绝 `invalid_request`。
- 2026-04-21 下午：`runContent` = **纯自然语言字符串**，不引入结构化子字段；调用方自行把要给 VLM 的目标描述拼好后整串传入。
- 2026-04-21 下午：`platform` 枚举 v1 定稿 = `android / ios / harmony`（全小写），不在此枚举范围内 → `invalid_request` 整批拒绝。
- 2026-04-21 下午：新增两条取消 API——`POST /api/submissions/{id}/cancel`（整批）与 `POST /api/submissions/{id}/cases/{caseId}/cancel?platform=<p>`（单条）；**匿名无鉴权**；只对 `queued` 生效，对 `running` 返回 `noop=true`；取消成功后立即走 `result=fail, statusReason=cancelled_by_request, reportUrl=null`（不生成 HTML 报告）并触发一次 Kafka 广播。
- 2026-04-21 下午：Server ↔ Agent WS 协议内部升级 = `start_run` 由 `run_id + goal` 改为 `submissionId + caseId + platform + deviceSerial + runContent`；对外 HTTP 层无感，由执行侧统一改造；切换期允许双字段并存+打 WARNING，全量上线后删除旧字段。
- 2026-04-21 下午：平台级不设人工并发上限——iOS / Android / Harmony 的并发天花板 = 当前 `ready` 设备数；超出容量的 item 继续排队；唯一硬约束 = "一台设备同一时刻最多 1 个 item"。
- 2026-04-21 下午晚些：v1 执行优先级重排 = **设备 Readiness Gate → 内部排队 + Web 展示 → 对外 API + Kafka + 取消 + 报告 → 收尾**，禁止跳阶；核心原则是"先把自己平台的设备可调度入口立住再开对外口"。
- 2026-04-21 下午晚些：三端执行流程冻结——`agent/drivers/` 主路径、`agent/mirror/*`、`agent/runner/*`、`/api/runs`、`/ws/agent` 握手、手工触控链路本次 v1 全程**不允许修改**；所有新增能力走 `agent/health/`、`server/scheduler/`、`server/submissions/` 三个独立新模块 + `frontend` 新增两页实现。
- 2026-04-21 下午晚些：Readiness 探活规则 = 5 秒轮询、失败 3 次降级、纯 read-only、不做自动唤醒；`not_ready_reason` v1 枚举 5 项 `screen_locked / wda_not_ready / hmdriver2_disconnected / adb_offline / driver_probe_failed`，全部透传给 Web 设备总览。
- 2026-04-21 下午晚些：第 2 梯队新增 Web 设备总览页 + 队列总览页 + 内部投递按钮（只走内部接口，外网拿不到），用于在对外 API / Kafka / HTML 报告都没接入之前端到端验证排队 → 执行 → 完结链路。
- 2026-04-21 下午晚些：开发期广播后端开关 `AI_PHONE_BROADCAST_BACKEND=kafka|stdout` 默认 `stdout`，Kafka 配置到位前终态事件打日志，便于本地调试。
- 2026-04-21 下午：Agent 进程 crash 时，其上所有 `running` item 由 Server 检测后转终态 `executor_error`，不自动重跑；Agent 重连后不承接旧任务。
- 2026-04-21 下午：请求体结构硬约束 = `{ items: [...] }`；逐 item 独立校验；每条 item 必填 `caseId / platform / runContent`，可选 `deviceAlias`，其他字段允许透传不校验；任一条 item 缺必填 → 整批打回 `invalid_request`，不做部分接收；同步返回可选带 `rejectDetail` 辅助定位（调用方不应作为正式依赖）。
