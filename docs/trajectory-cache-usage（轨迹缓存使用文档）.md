# trajectory-cache-usage（轨迹缓存使用文档）

本文面向调用方和部署者，只说明轨迹缓存怎么打开、怎么投递、V1 / V2 / V3 怎么选。内部方案、prompt 约束、日志节拍和实现契约不放在公开文档里。

## 1. 能力定位

轨迹缓存用于重复执行同一条自然语言 case：

1. 第一次执行没有命中缓存时，仍走正常 VLM 主链路。
2. 这次 Run 成功后，Server 后台把可回放动作保存为当前 `cacheMode` 的缓存。
3. 下一次在同一设备、同一语义目标、同一缓存版本下再次投递时，Server 优先尝试缓存回放。
4. 缓存回放结束后仍会走最终断言，断言不通过会按失败收尾。

缓存不是通用脚本，也不是跨设备录制回放。它依赖起跑状态、账号状态、业务数据和页面结构足够稳定。

## 2. 开关

服务端总开关默认关闭：

```env
AI_PHONE_TRAJECTORY_CACHE_ENABLED=false
```

需要启用轨迹缓存时改成：

```env
AI_PHONE_TRAJECTORY_CACHE_ENABLED=true
```

这个开关只是允许请求里的 `cacheMode` 生效。没有打开时，请求里即使传了 `v1` / `v2` / `v3`，Run 也会把 `effective_cache_mode` 对齐为 `off`。

失败自动重跑可以和缓存一起用：

```env
AI_PHONE_RUN_RETRY_ENABLED=true
AI_PHONE_RUN_RETRY_MAX=1
AI_PHONE_RUN_RETRY_CLEAR_CACHE=true
```

`AI_PHONE_RUN_RETRY_CLEAR_CACHE=true` 表示缓存模式下失败后，下一次 attempt 前会删除当前 mode 对应缓存，避免一直命中同一份坏缓存。

### V3安卓截图通道

安装UiAutomator2可选依赖后，V3启动时会优先尝试常驻服务直接返回JPEG，保持原分辨率与原画质参数。原定位模型和Jev均可使用；已有Jev连接会被复用。启动检查失败时明确记录原因并保留原ADB截图通道，iOS/Harmony沿原通道。运行中不会隐式补拍另一通道。日志“V3截图通道”说明本次实际选择。

### 可选：V3 Jev 点击重定位

默认关闭。仅在命中V3缓存后，将普通click的目标重定位交给多模态Decisions模型；缓存格式和投递参数不变。其他动作继续使用原定位，定位失败进入已有救援。

在Server模型配置中设置：

```dotenv
AI_PHONE_JEV_ENABLED=false
AI_PHONE_JEV_PROVIDER=openrouter
AI_PHONE_JEV_API_KEY=
AI_PHONE_JEV_MODEL=
```

启用时将`ENABLED`改为`true`并填写对应渠道的Key。`PROVIDER`二选一：`openrouter`或`openai`，一次只使用一个渠道；Key不继承PHONE_VLM或AUX。

| 渠道 | 默认端点 | 默认模型 |
|---|---|---|
| openrouter | `https://openrouter.ai/api/alpha/decisions` | `openai/gpt-6-luna-decisions` |
| openai | `https://api.openai.com/v1/decisions` | `gpt-6-luna` |

两种Decisions协议由程序分别适配；普通Chat Completions兼容服务不能直接代替。`AI_PHONE_JEV_MODEL`可选，留空使用表中的渠道默认模型。开启JEV后，程序按设备平台选择配套快速通道与稳定检测；关闭时使用原定位和设备通道，快速通道初始化失败时保留原通道兜底。

Android普通缓存click在Jev初始化成功时复用已有UiAutomator2连接执行坐标点击，仍等待调用完成；救援修复和其他动作沿原驱动。日志会记录实际通道及点击耗时。

Android Agent需安装可选依赖：在backend目录运行`pip install -e '.[jev]'`；关闭开关时无需安装。iOS和Harmony复用该任务已有的WDA/UITest连接，Jev不创建第二个设备会话。Server和Agent均需使用支持这些配置的版本，启动V3时固定本次策略，后续click复用一次初始化的连接。

日志中的“Jev定位证据”包含实际模型输入红框图、所选编号和分段耗时。JEV分支仅展示画框后的模型输入图，画框前原图仍在内部复用，不重复上传；其他分支保留原截图展示。为降低回放耗时，V3不再逐步拍after留证图，任务结束仍单独截图供最终验收。Jev仅识别当前摘要目标，不传执行历史、不额外采集点击后文字；V3动作后观察等待为200ms。定位复用V3已有截图，只取一次UI树，模型返回后不反向读取设备验证。已有截图尺寸不一致、定位超时、API或采集失败、目标不确定进入原救援，不尝试切换另一渠道或旧定位器。

## 3. 投递方式

`POST /api/submissions` 支持批次级默认 `cacheMode`，也支持 item 覆盖。

```json
{
  "submissionName": "smoke-cache-demo",
  "cacheMode": "v2",
  "retryMax": 1,
  "items": [
    {
      "caseId": "settings-about",
      "caseName": "进入关于本机",
      "runContent": "打开设置并进入关于本机页面",
      "platforms": ["ios"],
      "deviceAliasPools": {"ios": ["iPhone-1"]}
    },
    {
      "caseId": "dynamic-page",
      "runContent": "打开首页并检查活动入口",
      "platforms": ["android"],
      "cacheMode": "off"
    }
  ]
}
```

可选值：

| 值 | 含义 |
|---|---|
| `off` | 不使用轨迹缓存，始终走正常 VLM 主链路 |
| `v1` | 固定动作回放 |
| `v2` | 固定动作 + 状态路标对齐 |
| `v3` | 保存动作意图，回放时重新定位当前坐标 |

非法值会按 `off` 处理。批次级 `cacheMode` 是默认值，单条 item 的 `cacheMode` 优先级更高。

## 4. V1 / V2 / V3 怎么选

| 模式 | 适合场景 | 特点 | 风险 |
|---|---|---|---|
| `v1` | 页面、账号、数据、分辨率都非常稳定的短链路 | 最快；按首次成功动作直接回放 | UI 稍微移动、弹窗变化、列表内容变化时容易点错 |
| `v2` | 稳定回归链路，偶尔有加载慢或小范围动态内容 | 每步动作后比对首次成功的状态路标；可配 recovery / 瞬态弹窗 gate | 比 V1 慢；状态路标过严会误判，过松会放过偏航 |
| `v3` | 控件位置可能变化，但业务路径相对稳定 | 不信旧坐标；每步按 `plan_intent` 在当前截图重新找目标 | 每步要重新定位，更慢、更依赖定位模型质量 |

建议顺序：

1. 默认生产先用 `off`。
2. 稳定、重复、起跑状态可控的回归 case，先小流量试 `v2`。
3. 如果 UI 位置经常变但语义路径稳定，再试 `v3`。
4. `v1` 只用于高度受控链路，不建议给动态业务页面默认打开。

## 5. 命中与保存规则

缓存命中依赖：

- 同一真实设备标识。
- 同一条自然语言目标的归一化语义。
- 同一缓存版本：V1 / V2 / V3 分开存储，互不覆盖。
- 缓存状态是 active。

保存规则：

- 只有成功 Run 会保存缓存。
- 缓存回放成功不会覆盖已有缓存。
- 失败 Run 会删除当前 mode 对应缓存。
- V3 回放失败或最终断言失败时，会把当前缓存标记为 suspect，后续不再命中。

所以第一次投递 `cacheMode=v2` 时，通常会先走 VLM 主链路并保存；第二次同设备同目标再投递，才会看到缓存回放。

## 6. 推荐使用方式

先选低风险 case：

- 起跑 App、账号、地区、语言、业务数据都固定。
- 每次从同一页面开始。
- 业务路径不要依赖随机活动、动态列表排序、一次性弹窗。
- 优先绑定设备别名池，避免同一 case 在不同设备之间复用预期不一致。

再看日志：

| 日志 | 说明 |
|---|---|
| `轨迹缓存 未命中` / `V3缓存回放 未命中缓存` | 没有可用缓存，继续走 VLM 主链路 |
| `已保存 V1/V2/V3 轨迹缓存` | 成功 Run 已沉淀缓存 |
| `缓存步骤` / `缓存完成` | 进入缓存回放步骤 |
| `轨迹缓存断言 PASS` / `V3最终校验 PASS` | 缓存回放最终断言通过 |
| `trajectory_cache_alignment_fail` | V2 状态路标对不上 |
| `trajectory_cache_v3_replay_failed` | V3 定位或回放失败 |

如果请求传了 `cacheMode=v2` 但日志一直没有缓存行为，先检查服务端 `AI_PHONE_TRAJECTORY_CACHE_ENABLED` 是否为 `true`，再看 Run 返回里的 `requested_cache_mode` 和 `effective_cache_mode`。

## 7. 和自动重跑的关系

`retryMax` 解决的是环境瞬时波动，缓存解决的是重复执行时的路径复用，两者可以组合。

推荐组合：

| 场景 | 建议 |
|---|---|
| 普通生产回归 | `cacheMode=off`，必要时 `retryMax=1` |
| 稳定链路试缓存 | `cacheMode=v2`，`retryMax=1`，`AI_PHONE_RUN_RETRY_CLEAR_CACHE=true` |
| UI 位置常变 | `cacheMode=v3`，先小流量观察 |
| 动态页面或探索任务 | `cacheMode=off` |

缓存不是越开越好。只要 case 起跑状态不可控，缓存命中越高，错误复用的风险也越高。

### Jev定位图与兜底

三端共用定位图绘制函数。候选区域重叠时，仅将展示框的宽高缩至45%，中心保持在原点击坐标；编号就近避让，通过短引线连接红点。红点表示选中该编号后执行的实际点击位置。候选列表、编号、原始bounds与执行坐标不会因绘图改变。

绘图失败时保留原红框绘制作为兜底，提示词同步改为原图说明；定位证据的`overlay_mode`记录实际使用`point_overlay`或`legacy`。该兜底只处理图片生成失败，不替代已有定位救援，也不保证模型一定选对或页面已经加载完毕。
