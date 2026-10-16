# 生命周期简化：隔离 HTTP 与浏览器验收准备

本文件保留执行前的准备说明；最终隔离 HTTP/浏览器验收已通过，见 [交接结果](RESULT.md) 和 [最终摘要](final-result.json)。root 已复核三端启动失败卡片、终态及清理。本轮始终没有启动真实 Agent/VM/设备或访问主服务及公司服务。

准备阶段完成脚本、语法与 14 项离线守卫检查后等待 root 冻结，未提前启动验收；执行中补齐两个已定位的夹具入口回归守卫，最终为 16 项。

范围是实际三端 FastAPI VM 路由、实际状态处理器和统一 lifecycle DTO，以及现场构建的 Vue 页面。Agent 端使用可控协议桩：只发送 capability/status 输入，不构造 lifecycle 输出，不导入或调用任何原生 Manager。它证明 Server 与页面的契约，不能宣称真实 VM/设备行为通过。

## 文件

- `common.py`：21 个固定且互不重复的 12 位十六进制 VM ID、三端原生身份构造、路径归属和源码冻结核验；导入只读取定义。
- `browser_fixture.py`：真实路由、独立 SQLite 与 FakeHub。请求关联、租约、状态落库和失败记录均由产品代码处理；Python audit 拦截子进程、信号、外部网络及其他项目的环境文件。
- `browser_acceptance.js`：通过真实 DOM 按钮操作，并回读真实 HTTP DTO/operation，核对失败原因、动作权限、只读观察先后及消息次数。
- `run_browser.cjs`：使用已安装的 Playwright 1.62.1 启动全新私有 BrowserServer，保存精确子进程与退出证据；不用公共 MCP profile 或用户浏览器。
- `run_acceptance.py`：默认只输出计划；显式冻结后才可执行一次。构建输出、Vite cache/空环境目录、SQLite、浏览器临时文件均在本轮目录。构建不启动 Vite dev/preview，也不改 `web/dist`。
- `test_fixture_guards.py`：纯离线身份、路径、freeze、实际 audit 函数拒绝分支检查，不导入产品模块或创建任何网络连接。

## 每端七台配置

| 场景 | 实际验收点 |
| --- | --- |
| 已确认停止，但旧 cleanup 标记为 false | DTO 显示已停止，普通 Start/Stop 可用，新观察胜过旧标记 |
| 启动失败，实例实际已停止 | 一次新请求失败后显示原因；被动观察不能抹掉失败；下一次显式 Start 成功才清除 |
| 停止失败，实例实际仍运行 | 显示运行中和失败原因；只允许普通 Stop，不通过 Start 隐式重启 |
| 所属 Agent 离线 | 不提供启停，服务端也拒绝；可探查其他 Agent，仍可立即删除后台配置 |
| 在线但状态未知，观察结果停止 | 普通 Start 先进行一次关联的只读 inspect，再发送一次 start，绝不先 stop |
| 在线但状态未知，观察结果运行 | 原实例显示运行中，不发送 start/stop；重复 Start 被拒绝 |
| 操作进行中 | 页面禁用冲突动作，实际 API 也拒绝启停、删除、修改和换机 |

所有 fake observation 按真实记录生成身份：Android 的受管 AVD 名、iOS 的固定 simulator 名和 UUID、Harmony 的实例名/私有路径及当前租约 token/serial。请求 ID、目标 Agent 和实际行记录相关联。Harmony 配置具有真实临时数据库 lease row；未知或缺失观察不假装资源已释放。

失败后的被动观察使用空 request_id 与旧失败的 observed_request_id，且带独有 observation.reason 标记。浏览器既检查这个新事实被 Server 接收，又检查旧 operation.outcome 仍 failed、last_failure 仍存在，避免把“整条回报被忽略”冒充失败保留验证。

页面展示和按钮都读取真实 Server 的 lifecycle；原 raw state 保留在 HTTP 证据中。浏览器不向 Vue 或响应体注入状态，也不直接改数据库来制造通过。

## 冻结后的一次执行方式

由 root 明确冻结后，在 worktree 根目录使用已有个人后端 venv：

```text
backend/.venv/bin/python docs/experiments/vm-lifecycle-simple-20261016/acceptance/run_acceptance.py --freeze docs/experiments/vm-lifecycle-simple-20261016/acceptance/frozen-final.json

backend/.venv/bin/python docs/experiments/vm-lifecycle-simple-20261016/acceptance/run_acceptance.py --execute --manifest docs/experiments/vm-lifecycle-simple-20261016/acceptance/frozen-final.json --run-id personal-simple-final-20261016-a1
```

冻结包含后端源码、前端源码/构建配置和本目录执行脚本。执行在构建前、构建后、HTTP 夹具启动/结束、整体结束检查摘要一致；任一变化明确失败，不自动更新冻结或重跑。以上命令只是准备说明，尚未执行。

运行目录为 `acceptance/runs/<run-id>`，必须全新且不能通过 symlink 指向其他目录。Server 只监听 127.0.0.1 的随机端口，明确排除 8000/5180；浏览器只允许该 origin。环境 token 是本轮随机值，仅存私有 runtime 文件和本轮私有 localStorage，不读取全局 CLI/profile。

正常收尾关闭私有浏览器，通知本轮 Server 停止，核实精确子进程退出、端口释放、DB/runtime 与临时 profile 删除、探查 waiter 清空、blocked_calls/fake_errors 为空。异常时也只清理本轮持有的 Popen 与目录，不搜索或终止其他进程。日志、截图、原始请求/回报、最终数据库记录及 source hash 会保留在运行目录；不自动补发失败操作或再跑一轮。

## 准备阶段检查记录

准备阶段 14 项纯离线守卫、Python AST 和两份 JavaScript 语法通过，并只读核实 Playwright 1.62.1 与 Vite configLoader runner 参数。该阶段没有创建 runs 目录、冻结文件或启动 HTTP/browser。后续实际执行结果、失败原件和最终来源摘要均保留在 RESULT.md 链接的记录中，不把准备阶段检查当作浏览器验收。
