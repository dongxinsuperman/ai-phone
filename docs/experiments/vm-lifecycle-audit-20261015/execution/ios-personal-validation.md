# iOS 个人隔离验收

状态：**最终冻结源码的个人 iPhone 功能与视觉验收通过**，包含真实输入/回执、停止、同 UDID 再启动、数据保留、重启后的可见页面，以及离线删账后新 Agent 会话重连清孤儿；全部本次实例已清理，私有端口已释放。16 个相关源码指纹在本轮前后完全一致，工作者与总控均独立查看两张截图确认结果。旧轮次失败和中间轮截图缺口保留，不能与最终证据混淆。

执行入口：`ios_personal_e2e.py`。不带参数只打印计划；`--execute-personal-ios` 才会创建、启动个人实例。

## 隔离边界

- 所有服务只监听 localhost；独立 SQLite、storage、Agent ID、host ID、认证 token。
- 先清除进程继承的 `AI_PHONE_*`，再切换至本次新建运行目录导入配置；不读取现有项目 `.env`。
- 所有 simctl 命令由 xcrun guard 添加每轮固定的短 ASCII `--set /private/tmp/aiphone-personal-ios-<run>`；拒绝重写集合路径。早期失败轮使用工作树内长路径，已停止使用。
- 创建只能使用本次 HTTP 创建返回的 VM ID；后续原生命令必须使用 guard 记录的精确 UDID，并复核原生名字。
- 禁止 `all`、`booted`、`unavailable` 等集合/模糊目标，禁止默认模拟器集合、真机、公司包与服务。
- 只注册 iOS Simulator provider；不运行完整 Agent main，不发现真机、Android、鸿蒙。
- 使用当前个人工作树 vendored WDA，独立 DerivedData；验收使用私有 18300/19300 端口段，保留生产分配算法。
- 清理只调用本次 VM ID 的生产 Manager；最后检查专用集合为空、数据目录消失、端口预留归还。

## 同一业务 Case 与检查点

1. HTTP 创建配置，确认没有 Agent 与 UDID。
2. WebSocket 真实能力探查；HTTP 首次绑定并启动原生 Simulator/WDA。
3. 启动中删除返回冲突；运行后设备身份与 VM ID 正确对应。
4. 真实 `IosSimProbe` 确认可操作，再走 `/api/runs` → Agent WebSocket 下发。
5. 专用确定性 handler 调用生产 `IosSimulatorDriver`：Safari 打开共享 `personal-fixture.html`，输入本轮 token，点击“确认”。
6. 同时要求真实截图、原生 UI 显示 token、localhost receipt 收到 `confirm + token + count=1`。
7. 停止确认原生 `Shutdown`，归属与 UDID 保留；再次启动 UDID/端口不变。
8. 同 URL 页面重新上报 `load + token + count=1`，证明本地数据保留。
9. 停止后断开个人 Agent，按最终规则 HTTP 删除立即返回成功、Server 记录为 404，原生实例此时仍在。
10. 同一 host_id 的新 agent_id 重连对账驱动孤儿清理，最终原生实例与私有数据目录不存在、端口预留释放。

这验证真实 HTTP/WS/原生控制/业务终态闭环；专用 handler 不代表生产 VLM/Runner、完整实时镜像或公司业务应用已经验收。

## iPad 边界

iPad 的生产 WDA 路径使用 xcodebuild；在验证其能精确选择专用 device set 之前脚本明确拒绝 iPad，不会退回默认集合。本机其他模拟器所有权未知，不用于绕过这个边界。

## 本轮实测结果

### 输入步骤的验收脚本修正

前两轮 `ios-20261015-235440-e6d673`、`ios-20261015-235748-36ec28` 在首次 Safari 点击/键盘激活阶段停止。不能把点击 HTTP 成功当作输入框已聚焦：第一轮截图输入为空，第二轮原生 XML 没有键盘。两轮均保留失败截图并确认清理完成。

第三轮加入目标矩形稳定观察、点击前截图、WDA 请求/返回记录，以及一次有界的重新读取并聚焦；没有 JS 输入替代，没有改生产 driver。真实输入和确认成功。首次点击后仍未出现键盘，第二次聚焦后才出现，这是现场观察，尚不能据此确定为生产控制 bug。

### 第三轮证据

目录：`personal-runtime/ios-20261016-000628-3b49cc/`。

- VM ID：`08086d1cf8234aa8ae64ccc2001c9087`；UDID：`77D195A1-2A1C-4AE8-970E-CC8A5CC365CD`。
- 首次绑定在 112.8 秒进入 running，其中 WDA 独立冷构建 70.7 秒；期间 Agent 心跳与设备刷新仍在进行。
- 真实 `/api/runs` 下发完成，run `88faceb73c80` 状态 success。
- `confirmed.png`、`source-confirmed.xml`、`receipts.json` 同时证明输入 `personal-9ab0c769d0da`、确认次数 1、终端回执已到达。
- 停止成功：原生 `Shutdown`；Server 仍保留原 Agent 和 UDID。
- 再启动复用同一 UDID，但 Apple `simctl bootstatus` 持续 `Waiting on System App`；`restart-waiting-system-app.png` 是真实黑屏转圈，`restart-bootstatus-diagnostic.json` 记录 1 分 59 秒仍未完成。
- 180 秒后生产 Manager 报 `start_failed`，Server 没有伪报 running；失败细节保留 `instance_created=true`、`cleanup_confirmed=true`。
- 随后个人清理调用生产 Manager 删除成功；`post-cleanup.json` 再次确认专用集合为空、该 UDID 数据目录不存在、18304/19304 可重新绑定、45 条原生命令全部指定专用集合。

该轮结论：启动、真实操作和停止取得终端证据；正常重启、重启后数据读回、离线删除重连当时未完成。没有跳过系统启动完成判据，也没有延长超时掩盖问题。

## 短路径基底与中间完整验证

主执行者重新授权后，先运行一次**不含 Server/Agent/WDA/浏览器**的纯原生对照：`personal-runtime/ios-native-20261016-003100-31f280/`。短集合路径 `/private/tmp/aiphone-personal-ios-native-20261016-003100-31f280` 下，首次 `bootstatus` 24.503 秒、停机后第二次 `bootstatus` 10.349 秒均正常完成；46.538 秒结束并确认清理。截图仅作原始附件，不能替代自动化就绪判断。

随后只执行一次最新完整流程：`personal-runtime/ios-20261016-003239-b7992f/`，总用时 166.669 秒。

| 检查 | 实际证据 |
|---|---|
| 配置创建不绑定 | HTTP 返回 draft，没有 Agent/UDID |
| 首次探查绑定启动 | VM `dccac9347c714c01b40cac9641db8107`，UDID `86F92A1B-9930-45FA-A608-F55F053F5F27`，94.757 秒进入 running；独立 WDA 冷构建 51.5 秒 |
| 启动中禁止删除 | HTTP 409 |
| 真实业务操作 | run `f73f787d17f5` success；`confirmed.png` 与原生 source 可见 `personal-0267d0931592`，receipt 为 confirm/count=1 |
| 停止保留 | 原生 Shutdown，VM ID、UDID、Agent 不变 |
| 再启动保留 | 同 UDID、同 WDA 端口；新 `load` receipt 读取同 token/count=1 |
| 离线删账 | DELETE `deleted=true/cleanup_sent=false`，随后 GET404，原生实例仍在 |
| 新会话对账清孤儿 | 新 agent_id `personal-ios-reconnect-e135b921ba68`、原 host_id，上报停机实例后清理完成 |
| 资源释放 | 原生实例与数据目录不存在，预留删除；独立检查 18303/19303 均可 bind；55 条 guarded 原生命令全部为专用集合 |

这轮 `after-restart.png` 在 receipt 到达后立即截图，视觉检查发现仍是 Safari 加载白屏；因此这张图片**不提供重启后页面已显示的证据**。脚本补上重启后原生 XML 可见结果的等待，并在主执行者重新授权后执行下面的最终验收。

早期长路径轮重启失败，短路径纯原生与最新完整流程通过：这支持继续把隔离目录约束作为验收环境因素检查；但不是严格的单变量 A/B，不能宣称已经证明长路径是唯一原因，更不能把调整验收路径说成生产代码修复。

本轮没有覆盖 iPad、生产 VLM/Runner、完整实时镜像或真实业务 App；已覆盖的功能链路与未覆盖范围需分别陈述。

## 最终冻结源码验收

主执行者授权一次最终完整验收，目录 `personal-runtime/ios-20261016-012711-37f48c/`，157.519 秒完成，没有额外重跑。

- VM ID `bc1095238b06`；UDID `57122E96-DA8A-488C-90B5-AA09CE780B22`；真实 HTTP/WS run `ff37e9a47324`。
- 首次绑定 93.202 秒就绪；原生输入 `personal-88862240df2c` 并确认，页面原生可见结果、`confirmed.png` 与确认 receipt 均为 count=1。
- 停止后原生为 Shutdown；再启动继续使用同一 UDID 和 WDA/MJPEG 端口 18309/19309。
- 重启后收到同 token/count=1 的 `load` receipt，再等待原生可见 StaticText 包含 token、确认次数、1、本地回执已记录，才保存 `after-restart.png`。
- 工作者和总控分别实际查看 `confirmed.png` 与 `after-restart.png`：两图均显示保存的 `personal-88862240df2c`、次数 1、本地回执已记录；重启后输入框为空而已保存结果仍在，符合重新加载后的持久化行为。
- 离线 DELETE 返回 `deleted=true/cleanup_sent=false`，Server GET404，原生实例仍在；新会话 `personal-ios-reconnect-80c10708240c` 使用原 host_id 重连，清理孤儿完成。
- `post-cleanup.json` 独立确认：专用集合为空、实例数据目录不存在，18309/19309 和个人 HTTP 端口 55536 可重新绑定；58 条 guarded 原生命令全部指定专用集合，无默认集合生命周期命令。
- `result.json.source_sha256` 记录 16 个源码/fixture/harness 文件；结束后重新计算，差异为 0，未修改产品源码。
- 按主执行者授权，仅删除五轮明确属于本验收的 `storage/ios_sim_wda_build`，释放 943.1 MiB；大小、路径和删除结果见 `ios-owned-build-cleanup.json`。所有结果、原生日志、源码指纹、截图、SQLite 证据、脚本均保留，未触碰默认 WDA/Xcode 缓存。

最终可用摘要：`ios-evidence-summary.json`。最终两张截图来自 `ios-20261016-012711-37f48c`，不要使用前一轮白屏截图作为通过依据。
