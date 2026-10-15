# 个人 CLI 与三端生命周期更新的兼容性

> 最终交付更新：个人 CLI 修复 e65dad5 已经独立 58 项回归及 12 项真实本地 Server→CLI 验证，并由总控 fast-forward 合入个人 CLI main；未推送、未发布、未改全局 profile 或公司发行包。下文保留旧版 0.3.0 的原始不兼容复现与修复边界。

核对日期：2026-10-16。

**结论：建议配套一个小型 CLI 兼容更新，保留现有命令体系和正常操作。主要修正“等待哪一次操作的结果”和“换 Agent 失败后应查哪个 ID”，不需要重做 CLI 或增加一套通信协议。**

## 范围与版本

| 对象 | 本轮核实结果 |
|---|---|
| 个人 CLI | `/Users/dongxin/aiphonecli开源`，origin 为 `github.com/dongxinsuperman/aiphone-cli.git`，工作区干净。 |
| CLI 版本 | `aiphone-cli@0.3.0`，commit `dba438b2ede72b1c43b545fbb7a55bb0a2b5b07d`；package.json 的 `private: true`，本报告不表示已发布 npm 新版。 |
| 后端比较 | `452e7d77326f9fc0e5b003ecbf6cec5fc499e4bf` → `74f209dc6556d4678291b0dce63a9817741b6bea`。 |
| 个人 Server 仓库内 CLI | 当前提交未跟踪 `cli/` 源码，实际外部客户端在上述独立个人 CLI 仓库。 |
| “CRI” | 在两个个人仓库的受管代码/文档中检索 CRI、Container Runtime Interface、RuntimeService、ImageService 等，没有找到独立 CRI 协议实现；本项目这里实际是 HTTP API 与 `aiphone` CLI。 |

审计过程中发现 Server 工作区另有在途修改，因此**后端契约比较和共享操作规则验证固定读取 `git show 74f209d:...`，不把未提交修改算进该版本**。完整版本、哈希与检索记录见 [cli-compatibility-evidence.json](./cli-compatibility-evidence.json)。

基线审计未调用安装版 `aiphone`、未使用默认服务地址、未访问公司仓库/服务/设备。审计完成后获授权在个人 CLI 的独立 worktree 做最小兼容修复，见文末；未修改 ai-phone 产品源码，未发布或推送。

## 无需更换接口或命令形式的部分

| 能力 | 兼容判断 |
|---|---|
| `devices available/statuses`、`submissions ...`、`apps ...` | 对外路径、请求形式和主要回包不因本次生命周期变更而更换；原有调用可以沿用。 |
| `vm list/get/status` | 三端实例列表、ID/别名解析和基础查询仍兼容；`status` 已通过设备归属及 available 判断 schedulable。 |
| `catalog/preset/systems/create` | 三端受控创建请求 schema 未变；创建仍只保存配置。已有预设回包一致性校验保留。 |
| 首次 `dispatch` / `create --start` 的正常路径 | 仍先探查，再向指定 Agent 下发；`sent` 与 `instance.id` 均保留。首次启动期间 `assigned_agent_id` 可以为空，CLI 当前不会因此误报 schedulable。 |
| 同宿主 `start/stop` 的正常路径 | URL、POST 方法、`{sent, instance}` 回包形式不变；无需重命名命令或重组参数。 |
| 成功换 Agent | CLI 已使用返回的 `response.instance.id` 等待新实例，三端成功换 ID 回放均通过。不存在独立 `switch` 命令，入口是 `dispatch --agent`。 |
| `delete` | 仍明确区分 `deleted` 与 `cleanup_sent`；CLI 已返回 `host_cleanup_confirmed:false`，不会把通知成功宣称为宿主文件已清理。新增加的 `alias_deleted/avd_cleanup_sent` 字段能够原样透传。 |
| 新增操作、占用、宿主归属约束 | 后端的 409 会被 CLI 正常输出为失败，不会暗中绕过约束；需同步说明何时必须等待、停止或结束占用。 |
| 端口、租约、宿主 ID | 属于 Server/Agent 内部协调；CLI 仍传 VM ID 和 Agent ID，不需要客户端分配端口或管理租约。 |

三端管理根路径均未变化：

```text
Android /api/internal/vm/instances
iOS     /api/internal/ios-sim/instances
Harmony /api/internal/harmony-vm/instances
```

## 必须修正的两项结果处理

### 1. 一次停止已判失败，CLI restart 仍可能继续启动

新后端区分“本次操作失败”与“后来确认机器已经停止”。它允许一次超时后收到的真实停止事实更新 `state=stopped`，同时保留 `operation.outcome=failed`。

当前 CLI 的 `waitFor()` 只看实例状态；看见 `stopped` 就返回成功，`restart()` 随后继续 POST start。因此，**一次停止已经失败的重启链路，仍会继续启动并以退出码 0 报告成功**。

证据：

- [CLI waitFor](/Users/dongxin/aiphonecli开源/lib/vm-service.js:51)在返回状态成功前没有核对 operation 的 outcome/request_id；[restart](/Users/dongxin/aiphonecli开源/lib/vm-service.js:159)随后进入 start。
- 后端提交 `74f209d` 的 `backend/ai_phone/server/vm_lifecycle.py::accept_status` 接受匹配的 late stop fact，`finish_operation` 不覆盖已经结束的失败结果；真实提交快照函数验证见 [backend-operation-proof.json](./backend-operation-proof.json)。
- 新契约回放中，Android/iOS/Harmony 三端均观察到 `start_after_failed_stop=true`、CLI 退出码 `0`。结果见 [cli-contract-repro.json](./cli-contract-repro.json) 中 `late_failed_stop`。

最小修正：生命周期命令记录本次返回的 request_id，等待时检查对应 operation 的 pending/outcome；失败就中止链路，不继续 start，不重试。iOS 的操作数据在 `instance.runtime_state`，Android/Harmony 在 `instance.runtime`，不能把 iOS 的 runtime 系统版本字符串当作操作记录。旧 Server 没有 operation 字段时可保留原有状态判断，以维持向后兼容。

独立执行 `wait --until stopped` 可以继续表示“等待事实状态”；它与 `restart` 内等待某一次 stop 成功应区分。

### 2. 换机已产生新配置，但新机启动失败时，错误主体仍指旧 ID

新后端的换机结果可以是：旧 ID 已从总账删除、新 ID 已创建，但新机启动未成功，返回：

```json
{
  "sent": false,
  "switched": true,
  "old_vm_id": "old-vm",
  "instance": { "id": "new-vm", "state": "error" }
}
```

当前 [CLI dispatch](/Users/dongxin/aiphonecli开源/lib/vm-service.js:70)看到 `sent=false` 后抛出 `dispatch_not_sent`，其 `error.vm_id` 仍写输入的旧 ID；新 ID 只留在深层 `error.response.instance.id`。调用方照错误主体继续查旧 ID，会得到 404。

三端回放均复现：`error.vm_id=old-vm`、`response.instance.id=new-vm`，没有自动重试。后端依据是提交 `74f209d` 三端 API 的 `_switch_agent()`：先建立替换配置，再捕获新机下发错误，仍返回包含新配置的结果。

最小修正：只要回包包含新 `instance.id`，错误主体和 next_commands 就指向新 ID，同时保留 old_vm_id、switched、原始 response。仍然非零退出，不自动补发、不重复 create。

## 需要同步的等待时限与说明

当前 CLI 默认等待 180 秒；新后端对一次操作明确给出了 `operation.deadline_at/timeout_sec`。使用源码默认设置时：

| 操作 | Server 一次操作预算 | CLI 当前默认 |
|---|---:|---:|
| Android start | 120 + 120 = 240 秒 | 180 秒 |
| Harmony start | 180 + 120 = 300 秒 | 180 秒 |
| iOS start | 180 + WDA 300 + 120 = 600 秒 | 180 秒 |
| stop | 120 秒 | 180 秒 |
| restart | stop 与 start 各有 Server 预算 | 整条链共用 180 秒 |

这些是默认最大预算，不是正常耗时。设置可改变预算，因此 CLI 不宜硬编码另一份三端时间表。

CLI 提前结束等待**不会取消 Server 操作**，它当前也没有自动重试，这一点应保留；需要明确显示“本地等待时限已到，本次 Server 操作仍 pending”，避免与操作已经失败混淆。建议默认读取本次 Server deadline；用户显式传入的 `--timeout` 继续优先。临时使用较长 `--timeout` 可以避免提前停止等待，但不能替代前述两项代码修正。

说明还应同步：首次完整启动成功才确认绑定；换 Agent 产生新 ID 和空白实例；删除配置与原生清理结果分别展示；操作中、任务/安装/观看占用时收到 409 应结束本次命令。

## 验证与可复现性

1. 个人 CLI 原有 `test/vm.test.js`：**15/15 通过**，全部使用该测试文件自己的 `127.0.0.1` mock Server；没有读取默认 profile 取服务地址。
2. 本轮真实 CLI 子进程执行四种新契约场景 × 三端，共 **12 个回放**：失败停止后继续启动、换机新启动失败、成功换 ID、离线配置删除。
3. 所有回放都装有 [网络/profile 防护](./cli-network-guard.cjs)：真实 `~/.aiphone/config.json` 的读取被拒绝，fetch 只允许本次随机端口的 `127.0.0.1`；全部记录 `profileReads=0`。
4. 回放命令只改变内存 mock 状态，不创建任何原生 VM，不访问已安装 CLI 的默认服务，不调用外部模型。

复现入口：

```bash
node docs/experiments/vm-lifecycle-audit-20261015/handoff/cli-contract-repro.cjs
```

该脚本固定调用个人 CLI 源码，预期重现当前缺陷，而不是替 CLI 打补丁；它不接受外部 Server 地址。结果写入同目录 JSON。

**修复边界：只更新个人 CLI 的等待结果、失败 ID 保留及 VM 专属回归用例；原有命令、正常请求和公共投递链路继续沿用。**

## 授权后的兼容修复候选

候选提交：`e65dad549d2d667972c44718962d6c6f87bf8eb9`，分支 `codex/vm-lifecycle-compat`。

工作区：`/Users/dongxin/.codex/worktrees/cli-vm-lifecycle-compat/aiphone-cli`。个人 CLI main 已从 `dba438b2ede72b1c43b545fbb7a55bb0a2b5b07d` 本地 fast-forward 到 `e65dad5`；未推送、未发布 npm、未修改全局或公司 CLI，版本号仍为 0.3.0。

本次只修改四个文件：

- `lib/vm-service.js`：按发起命令返回的 request_id/outcome 等待；失败或被其他操作替换就中止；提取 HTTP 409/502 和 `200 + sent:false` 中的新实例 ID。
- `lib/vm-commands.js`：start/stop/dispatch 的等待接到上述同一套 VM 服务处理，明确未下发的非等待命令返回非零。
- `test/vm-lifecycle-compat.test.js`：三端新旧结果与失败分支回放。
- `test/helpers/vm-network-guard.cjs`：测试只允许本次 localhost mock，拒绝真实 profile 读取。

验证：**58/58 通过**，包括原有 15 项及新增 43 项；`git diff --check` 和 JavaScript 语法检查通过。新增测试覆盖失败停止事实晚到、request_id 替换、pending 不能当成功、200/409 换机失败的新 ID、iOS 502、无 operation 的旧 Server、非等待 sent:false、create --start 失败保留配置和删除清理通知失败。

旧 Server 的特殊兼容边界：`stop` 返回 `sent:false` 且确实 `stopped`、完全没有 operation 元数据时，显式 `--wait` 可以确认停止事实；非等待命令仍返回非零。`DELETE cleanup_sent:false` 保持配置删除成功。

本候选保留原有 180 秒等待默认值与显式 `--timeout`，没有暗中扩大等待或添加自动重试。前述 Server deadline 对齐属于后续等待参数策略，不能把它描述为本次已经修改。

交付与验证记录见 [cli-fix-handoff.json](./cli-fix-handoff.json)。总控已独立复核并本地合入个人 CLI main；当前 Server 路由到 CLI 子进程的 12 项兼容检查也通过，详见 [真实本地接口桥接证据](vm-cli-independent-current-server.json)。
