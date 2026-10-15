# 三端 UI 验收 — 当前最终契约

结果：**102 项实际浏览器检查通过，0 失败，21 张截图。** 已使用 Chromium 查看三端概览以及绑定进度、绑定失败和新 ID 换机卡片，布局和操作结果可辨认，无横向溢出。

这是基于已构建 `web/dist` 与本地内存 HTTP fixture 的页面/交互验收；不代表原生虚拟机端到端验收。没有调用业务 Server、Agent、原生虚拟机、真机或公司服务。

## 本轮固定规则

| 场景 | 已检查的页面结果 |
| --- | --- |
| 正常运行 | 保留停止、复制配置操作。 |
| 启动或停止中 | 禁止冲突操作，复制按钮禁用。 |
| 已绑定实例停止失败 | 错误可见，再启动前要求停止；允许删除与换 Agent 例外。 |
| 首次绑定启动中 | 显示“启动中 / 绑定中：目标 Agent”，不宣称绑定已成功。 |
| 首次绑定失败 | 保留错误配置、显示未分配，允许重新探查或删除；没有自动再次启动。 |
| 首次绑定成功 | 只有运行成功回报后显示已绑定 Agent 和运行中。 |
| 离线删除 | 一次删除立即移除后台行，不展示待清理或清理重试流程。 |
| 离线换 Agent | 旧 ID 行立即移除，新 ID 行启动一次；没有旧宿主清理后的后台接力。 |
| 跨行并发探查 | A 的旧响应不会覆盖 B 的候选或清除 B 的忙碌状态。 |
| 列表乱序 | 真正延迟的旧“已停止”快照不能覆盖新的“启动中”。 |

## 证据

- `browser-report.json`：102 项检查、确认框文本、HTTP 请求计数、截图清单。
- `http-events.ndjson`：真实本地 HTTP 请求及响应，包含探查交错顺序、首次绑定确认/失败事件和延迟快照。
- `tested-build-sha256.json`：被测构建与三端 Vue 文件摘要。
- `fixture_server.py`、`browser_acceptance.js`：独立本地 fixture 与浏览器交互脚本。
- 每端截图：`*-overview.png`、`*-probe-current.png`、`*-first-bind-progress.png`、`*-first-bind-failed.png`、`*-first-bind-confirmed.png`、`*-switch-new-id.png`、`*-late-list-protected.png`。

页面构建通过；`backend/tests/test_vm_pages_align_with_android.py` 为 8 passed，其中一项执行 33 个 Node 页面行为测试，全部通过。

## 隔离与收尾

- 临时服务只监听 `127.0.0.1:60045`，所有 fixture 数据均为合成记录。
- 没有导入业务应用或读取 `.env`，没有任何原生设备命令。
- 单个全新浏览器页面，网络拦截与 CSP 限制为本地同源。
- 浏览器错误 0，非本地请求尝试 0；测试页面和临时服务均已关闭。

`superseded-pending-cleanup/` 保存已作废的旧验收，仅供历史对照，不可计入当前结果。
