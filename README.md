# ai-phone

[![CI](https://github.com/dongxinsuperman/ai-phone/actions/workflows/ci.yml/badge.svg)](https://github.com/dongxinsuperman/ai-phone/actions/workflows/ci.yml)

**面向中小型公司的三端真机 AI 自动化中台** —— iOS / Android / HarmonyOS 同级原生支持，自然语言驱动的纯视觉决策，开箱即用的调度队列与多设备并发，执行器可插拔，一台 Mac 即可起完整链路。

> **产品形态**：ai-phone 不是一个执行器 SDK，而是把"投递批次 → 设备池调度 → 自然语言执行 → 终态广播 + HTML 报告 + 大盘统计"做成 QA 团队 / 业务回归大盘开箱即用的中台能力。**执行器是其中一个可替换组件**：默认内置自研的 VLM 纯视觉决策循环（带卡死检测 / 审判 / 断言等辅助系统），也可挂载第三方执行器作为额外引擎选项。

---

## 分支说明

当前仓库长期保留两套架构分支，它们不是“主分支 / 临时实验分支”的关系，而是面向不同部署规模和演进阶段的两种执行架构。当前分支是 `next/server-brain`，采用 **Server 大脑，Agent 手脚** 的新架构，也是项目后续主力演进方向。

| 分支 | 定位 | 适合场景 |
|---|---|---|
| `main` | 轻量端到端架构，Agent 本地持有 VLM 决策与设备执行 | 个人开发者、微小公司、本机单套部署、端到端测试、真机调试验证 |
| `next/server-brain` | Server 大脑架构，VLM 决策 / 缓存 / 断言 / 命令证据链集中在 Server，Agent 只执行设备动作 | 项目未来主力分支；适合多办公区、多 Agent、统一模型密钥、统一调度、统一报告、权限 / 审计 / K8s 等生产化场景 |

两个分支会保持长期隔离：不做整体 merge，只按需同步通用能力和小修复。`next/server-brain` 是后续主力演进方向；`main` 主要用于轻量部署、个人/微小团队的端到端验证，以及对三端 driver、VLM、多模型协议等底层能力做快速调试。

> **轨迹缓存说明**：两条架构线都已具备 VLM 成功轨迹缓存 / 回放能力，但轨迹回放对 case 的起跑状态、账号状态、设备状态、业务页面稳定性要求很高。当前建议保持 `AI_PHONE_VLM_TRAJECTORY_CACHE_REPLAY_ENABLED=false`，先沉淀成功轨迹和日志；只有在被测业务起跑状态高度可控、重复执行稳定后，再按场景打开回放。

本分支的部署和差异说明见 [Server 大脑架构说明](./docs/server-brain.md)。

---

![iOS / HarmonyOS / Android 三端等价接入，业务化别名一栏管理](./assets/screenshots/devices-overview.png)

---

## 为什么选 ai-phone

| 能力维度 | ai-phone 提供的 |
|---|---|
| **三端真机原生** | iOS（WDA / mjpeg passthrough）/ Android（adb + scrcpy）/ HarmonyOS（hdc + hypium）三端等价。鸿蒙作为一等公民与 iOS / Android 同等支持，在开源生态里少见 |
| **调度队列 + 多设备并发** | `POST /api/submissions` 投递批次 → 实时按 `device_alias_pool` 分发到设备池 → Submission / Item TTL 兜底超时 → Kafka / Webhook 双通道终态广播 → HTML 报告自动落盘。设备占用锁 + readiness gate 防止派单到僵尸设备 |
| **自然语言驱动** | `runContent: "打开设置并进入关于本机"` 直接喂给 VLM，不写 selector / xpath / 步骤脚本 |
| **纯视觉决策** | 每步只看截图，不依赖 DOM / 控件树 / 无障碍服务，跨 App 跨平台不挑食 |
| **辅助系统护城河** | 卡死检测（本地 pHash 算法层、不烧 token）+ 异常介入审判（独立轻量模型，反复同坐标 / 同屏 / 震荡滑动自动召唤）+ 双图断言系统（before / after + 全步骤上下文对照终局裁决）+ 通道判定（结构化 / 自由对话自动分流）—— "VLM 是否真生效"不再是黑盒 |
| **三家协议自由组合** | 主 VLM 走 Doubao / Claude / GPT 三选一，辅助系统也可异家组合（如"主 Claude + 辅 Doubao 省成本"），全部走 env 切换、零代码改动 |
| **执行器可插拔** | 默认内置自研 VLM 决策循环；前端"引擎"下拉框允许挂载第三方执行器作为额外选项，调度 / 报告 / 设备池 / 终态广播仍然走中台统一链路 |
| **快速部署** | 一台 Mac + Postgres + 一根数据线即可起完整链路；生产部署模板在 Roadmap 中持续补齐 |

**典型用户**：

- 中小型公司 QA 团队 —— 真机上做 AI 化的兼容性 / 回归 / 冒烟测试
- 业务回归大盘想从"脚本维护"切到"自然语言投递"
- 海外团队需要切 Claude / GPT 跑英文 App（改两个 env 即用）

---

## 看一眼实际样子

按"调度 → 调试 → 决策护城河 → 产出 → 观测"5 个节点串起来看：

**1. 调度队列** —— 三端独立 FIFO + 正在执行 + 最近批次状态一栏拉通，外部投递的 Submission 实时分发到设备池：

![队列总览 · Android / iOS / HarmonyOS 三端独立排队 + 正在执行 + 最近批次](./assets/screenshots/queue-overview.png)

**2. 单设备调试** —— 浏览器即客户端：左实时画面（scrcpy / WDA / hypium 三端原生推流）、右自然语言 Goal 输入 + 步骤日志面板，回写通道与 VLM 共用，业务测试同学零安装、零配置：

![Android 设备工作台 · 实时画面 + Goal 输入 + 步骤日志面板](./assets/screenshots/device-workbench-android.png)

**3. 辅助系统护城河** —— 同坐标 `(~500, 500)` 累计点击 3 次自动召唤审判系统（**WARN #15 审判·召唤**），独立轻量模型审视后决定继续推进还是 KILL，VLM 决策不再是黑盒（这是 ai-phone 与 Midscene 等 Plan-Loop 框架的根本差异）：

![审判系统 · 同坐标震荡触发召唤，独立模型实时审视后放行](./assets/screenshots/assist-judge-system.png)

> 完整辅助能力（瞬态 UI 接管 / 断言系统 / 卡死检测 / 通道判定）见 [使用功能介绍 · 七、辅助系统](./使用功能介绍.md#七辅助系统ai-决策可信度护城河)。

**4. 自包含 HTML 报告** —— 单 case 与三端汇总两级，每步操作前 / 操作后双图对照 + Token 统计 + VLM 思考全留痕，零外部依赖、匿名可访问，便于外部平台直接嵌入：

![三端汇总 HTML 报告 · 操作前 / 操作后双图对照 + 步骤时间线 + token 统计](./assets/screenshots/report-three-platform.png)

**5. 运维大盘** —— 吞吐 / 设备健康 / Token 用量 / 稳定性四象限一页呈现；AI 分析卡片基于当日数据生成 4 段中文总结，跟随 `ASSISTANT_BACKEND` 在豆包 / Claude / GPT 间自由切换：

![运维大盘 · 吞吐 / 设备 / Token / 稳定性四象限 + AI 摘要](./assets/screenshots/analytics-overview.png)

---

## 30 秒上手

```bash
git clone https://github.com/dongxinsuperman/ai-phone.git
cd ai-phone/backend
cp .env.example .env  # 至少填 AI_PHONE_DB_URL + AI_PHONE_VLM_API_KEY
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .

# 终端 A：起 Server
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000 --reload

# 终端 B：起 Agent（接真机；本机开发可不传 server/token，走 .env）
python -m ai_phone agent
# 远端办公区电脑接入公司 Server 时：
# python -m ai_phone agent --server http://<server-host>:8000 --token <AI_PHONE_AGENT_TOKEN>

# 终端 C：起前端
cd ../web && npm install && npm run dev
```

打开 <http://127.0.0.1:5180> → 选设备 → 进工作台 → 输入自然语言 goal → 看 VLM 跑。

> 详细前置 / 数据库 / 调试参数请看 [本地开发指南](./docs/getting-started.md)。
> iOS / HarmonyOS 接入需要额外配置，见 [iOS 接入](./docs/ios-setup.md) 与 [HarmonyOS 接入](./docs/harmony-setup.md)。

---

## 投递一条 case（最小示范）

```bash
curl -X POST http://localhost:8000/api/submissions \
  -H 'Content-Type: application/json' \
  -d '[{"caseId":"demo_001","platform":"android","runContent":"打开设置并进入关于本机"}]'
```

完整字段、错误码、Kafka / Webhook 回调格式见 [对外调用清单](./对外调用清单.md)。

---

## 当前状态

| 模块 | 状态 |
|---|---|
| 三端真机 driver + 镜像（iOS / Android / HarmonyOS） | ✅ 完整 |
| 调度队列 + 设备池（Submission / Item TTL / 别名 / 锁 / readiness gate） | ✅ 完整 |
| 终态广播（Kafka / Webhook / stdout 三选一） | ✅ 完整 |
| 自包含 HTML 报告 + 运维大盘 | ✅ 完整 |
| VLM 决策循环 + 辅助系统（卡死 / 审判 / 断言 / 通道判定） | ✅ 完整 |
| 多协议适配（Doubao / Claude / GPT 自由组合） | ✅ 完整 |
| 执行器可插拔（内置 VLM + Midscene 桥接） | ✅ 完整 |

## Roadmap

- 历史回放页 / Case 加载对话框：API 就位，前端待补
- 日志服务系统：统一收集、检索、保留策略
- 生产部署模板：Docker Compose / K8s / Nginx / Ingress 示例
- Webhook 签名：面向公网集成的 HMAC 校验示例

---

## 维护模式

ai-phone 的主分支由原作者维护。欢迎通过 Issue 反馈问题、通过 Pull Request 提供参考实现，也欢迎 Fork 后按自己的节奏长期维护分支；但主分支是否采纳改动由维护者根据项目方向、稳定性和维护成本决定。

如果你基于本项目二次开发或分发，请保留原始 LICENSE 与来源说明。

---

## 文档导航

| 文档 | 受众 | 内容 |
|---|---|---|
| [使用功能介绍](./使用功能介绍.md) | 调用方 / 业务同学 | 产品手册：7 大核心功能、调度模型、终态枚举、稳定性工程 |
| [对外调用清单](./对外调用清单.md) | 调用方 / CI 集成 | API 契约（v1.5 冻结）：投递 / 查询 / 取消 / Kafka / Webhook 完整字段 |
| [架构设计](./架构设计.md) | 二次开发者 | 架构方案：Server / Agent 角色、消息协议、数据库 schema、镜像链路 |
| [本地开发指南](./docs/getting-started.md) | 本地开发者 | 起后端 / 起 agent / 起前端、env 配置详解、FAQ |
| [iOS 接入指南](./docs/ios-setup.md) | iOS 接入者 | WDA / pmd3 / Xcode 自动续签 / tunneld 完整流程 |
| [HarmonyOS 接入指南](./docs/harmony-setup.md) | 鸿蒙接入者 | hdc / hmdriver2 / hypium 镜像后端切换 |
| [辅助系统核心逻辑及效果](./ai-phone的辅助系统核心逻辑及效果.md) | 算法调优者 | 通道判定 / 审判 / 断言 / 卡死检测 / 链式动作的效果与调参 |
| [Midscene 执行器接入方案](./Midscene执行器接入方案.md) | 执行器扩展者 | 第三方执行器挂载方案 |
| [安全说明](./SECURITY.md) | 部署者 / 集成者 | 鉴权边界、默认 token、网络隔离和漏洞报告方式 |
| [贡献指南](./CONTRIBUTING.md) | 贡献者 | 本地开发、测试命令、PR 约定 |
| [第三方声明](./THIRD_PARTY_NOTICES.md) | 法务 / 维护者 | 捆绑组件与主要依赖的许可证说明 |

---

## 工程组成

- `backend/`：Python 3.11（`pyproject.toml` 锁 `>=3.11,<3.13`），同一个包按启动参数切换 Server / Agent 角色
- `web/`：Vue 3 + Vite 前端（**纯 JavaScript，无 TypeScript**）
- `midscene-bridge/`：第三方执行器桥接子工程（独立 Node 工程，按需启用）

> 发布源码包时建议使用 `git archive`，不要直接压缩本地工作目录；本地 `.env`、`.data/`、`node_modules/`、`dist/` 等运行产物都不应进入发布包。

---

## 致谢

三端能力栈站在巨人的肩膀上：

- [scrcpy](https://github.com/Genymobile/scrcpy)（Android 镜像）
- [WebDriverAgent](https://github.com/appium/WebDriverAgent)（iOS 控制）
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)（iOS DVT 截图）
- [hmdriver2](https://github.com/codematrixer/hmdriver2)（HarmonyOS 控制）
- [adbutils](https://github.com/openatx/adbutils)（Android 控制）
