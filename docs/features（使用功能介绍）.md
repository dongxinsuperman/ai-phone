# features（使用功能介绍）

ai-phone 是三端真机 AI 自动化中台，不是单个执行器 SDK。它把“批次投递、设备池调度、视觉决策、辅助审判、报告和大盘”做成一条完整链路。

## 1. 设备总览

设备页展示 Android / iOS / HarmonyOS 三端设备：

- 平台、serial、业务别名、机型、系统版本、分辨率。
- Agent 归属和在线状态。
- readiness：可运行、未就绪、WDA 编译中、设备锁、离线等。
- 手动进入工作台、改名、锁占用状态。

iOS 会额外展示 WDA 状态。stable 模式下如果 WDA 未起，页面会提示“进入工作台或跑任务触发本次 USB 会话首次启动”，而不是后台反复预热。

## 2. 队列总览

队列页按平台展示：

- queued / running / terminal item。
- 最近 submission。
- 每条 item 的 case、platform、deviceAliasPool、runId、状态原因。
- 取消整批或取消单条。
- 成功 / 失败 item 的 HTML 报告入口。

调度规则是三端独立 FIFO，派发瞬间按 ready 设备、锁、别名池共同筛选。黑屏待机线路开启后，Android / HarmonyOS 可在息屏态保持可派发，由 Run preflight 唤醒。

## 3. 工作台

工作台是浏览器里的真机客户端：

- 左侧实时画面：Android scrcpy、iOS WDA MJPEG、HarmonyOS hypium。
- 右侧自然语言目标输入。
- 手动点击、滑动、输入。
- VLM 步骤日志、截图、思考、动作、错误归因。
- engine 选择：默认 `vlm`；可按配置挂 Midscene 作为外接执行器。

工作台手动进入时，iOS / HarmonyOS 会按 env 尝试 wake-on-enter；这只是点亮屏幕，不绕过系统安全锁。

### 3.1 open_app 与手动控制的差异

手动点击、滑动、输入只依赖设备控制链路。Run 语义里的 `open_app(app_name='洋葱')` 会额外查询设备应用列表，把自然语言 App 名匹配成包名或 bundle id 后再启动。

- iOS 控制链路：WDA 负责截图、点击、滑动、输入、已知 bundle id 启动。
- iOS 应用列表链路：`pymobiledevice3 installation_proxy` 负责查询用户 App / 系统 App。当前实现分开查询 `User` 与 `System`，不再依赖 `Any` 一条路。
- 如果“控制台能点，但 `open_app` 报列应用失败”，优先排查应用列表链路，不要误判为 WDA 整体不可用。

## 4. 批次投递

外部系统通过 `/api/submissions` 投递自然语言 case：

```json
{
  "submissionName": "release-smoke",
  "functionMapContext": "可选：本次批次会用到的功能入口、测试账号或异常处理说明",
  "items": [
    {
      "caseId": "C001",
      "caseName": "登录后进入首页",
      "runContent": "打开 App，登录测试账号并确认进入首页",
      "platforms": ["android", "ios"],
      "deviceAliasPools": {"ios": ["iPhone-1"]}
    }
  ]
}
```

平台数组会展开成多条执行单元。调用方不需要写 selector、xpath 或脚本步骤，只描述目标和验收意图。

完整契约见 [`external-api（对外调用清单）.md`](./external-api（对外调用清单）.md)。

## 5. 报告

报告分两级：

- 单 item 报告：每步 before / after 截图、thought、action、耗时、token、日志。
- Submission 汇总报告：三端结果聚合、每条 case 报告入口、状态计数。

报告是自包含 HTML，路径挂在 `/files/reports/...`，方便外部平台嵌入。

## 6. 运维大盘

大盘聚合：

- 吞吐：提交数、item 状态、平台分布、耗时。
- 设备：在线数、平台、归属 Agent。
- Token：主 VLM 与辅助系统消耗。
- 稳定性：失败原因、平台失败、异常样本。
- AI 摘要：按当前 `AI_PHONE_ASSISTANT_BACKEND` 生成中文分析。

Token / 稳定性展示可通过 env 开关隐藏，但后端仍会保留计算能力。

## 7. 辅助系统

辅助系统不是普通聊天模型，而是围绕 VLM 执行可信度的保护层：

- 卡死检测：同坐标反复点击、同屏反复出现、滑动来回震荡等本地规则。
- 审判系统：触发异常时调用轻量模型判断继续、调整还是终止。
- 最终断言：对照目标、全步骤上下文和前后截图裁决是否达成。
- 通道判定：避免把应产出手机动作的链路误走普通聊天。

详细设计见 [`assistant-systems（辅助系统核心逻辑及效果）.md`](./assistant-systems（辅助系统核心逻辑及效果）.md)。

## 8. 三端稳定策略

推荐部署默认：

- iOS：WDA stable 线路，减少自动重启、自动配对、后台预热造成的扰动；`open_app` 另依赖应用列表查询链路。
- Android：空闲息屏，Run 前唤醒。
- HarmonyOS：空闲息屏，Run 前纯 hdc 唤醒。

配置清单见 [`recommended-env（推荐部署Env清单）.md`](./recommended-env（推荐部署Env清单）.md)。

## 9. 适合的场景

- QA 回归和冒烟。
- 多机型兼容性探索。
- 业务平台从脚本步骤转向自然语言投递。
- 内部工具 / App 的视觉闭环巡检。
- 需要保留执行证据链和 HTML 报告的自动化任务。

不适合直接承诺的场景：

- 绕过系统安全锁。
- 公网匿名开放执行入口。
- 对毫秒级确定性有强要求的脚本替代。
- 设备状态不可控、账号状态不可控时的大规模轨迹缓存回放。
