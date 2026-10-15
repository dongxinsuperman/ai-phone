# iOS 个人原生后续验收准备

本轮已按总控顺序放行执行一次，产品源码 `90342046a932273e54d7f96cb2947d753476477b`、执行时 HEAD `6da8a953b54362fc81fc519391752459edc9e051`：**149.762 秒完整通过**。未自动重跑，私有原生实例和端口均已清理，旧 74f 证据未改。

最小可提交证据在 `ios-evidence/ios-20261016-035022-ddd0e8/`：两张真实 PNG、JSON、原生命令/UI 日志和源码指纹，约 449 KiB，没有运行数据库或构建二进制；完整 runtime 保留在 ignored 的 `ios-runs/`。本轮只清理自身 WDA DerivedData 187.8 MiB，具体路径和大小已归档。

两图均已由执行者实际查看：保存的 `personal-eb12ee512800`、确认次数 1、本地回执已记录；重启后输入框为空但保存结果仍在。新 agent_id 读取原私有 `agent-host-id` 文件完成同宿主对账清孤儿。独立收尾核验：26 项运行源码、33 项冻结产品指纹均无变化；55 条 guarded 原生命令全部使用私有集合；18303/19303/56907 可重新绑定，SQLite 对应 VM 行为 0、run 为 success。

入口为 `ios_personal_native_followup.py`，不带参数只输出计划；总控放行后使用当前仓库的 `backend/.venv/bin/python` 执行该入口并传 `--execute-personal-ios`。每次自动生成全新 run ID，输出只落在本目录的 `ios-runs/<new-run>/`，不覆盖此前 `execution/` 中的 74f 证据。

## 保留和新增的隔离检查

- 原生工具仍由 guard 强制使用随机短 ASCII `/private/tmp/aiphone-personal-ios-<run>`，只允许本轮创建并登记的精确 VM ID/UDID，禁止集合目标、未知目标、覆盖 `--set`，补充拒绝带前置参数绕过 guard 的 simctl 调用。
- 仅 loopback Server/WS、独立 SQLite/storage/token、私有 WDA 构建与 18300/19300 端口域；删除继承的 `AI_PHONE_*` 和代理配置，不运行完整多端 provider、不读公司配置。
- 使用生产 `load_host_identity()` 在本轮私有 `storage/agent-host-id` 创建 0600 文件；两次 Agent 会话读取同一文件、保持同一 host ID，新会话使用新 agent ID。重连前和结束后检查文件值与 SHA256 不变。
- 运行开始记录 26 个相关源码/fixture/harness 的 SHA256，结束后检查无变化。包括新增身份文件实现、端口公共实现及 Server 升级相关文件。
- 沿用已通过的同一页面：真实输入和确认；同 UDID 停启后读取原 token/count=1；等待原生可见结果与 load 回执后截图；离线立即删账、同 host 新会话重连清孤儿。
- 只清理本轮精确实例与端口；WDA DerivedData 仅在本轮新 root 内生成，结束可在记录大小后精确清理。结果、日志、截图、SQLite、源码指纹保留。

准备阶段已完成编译、ruff、plan-only 检查；6 个 guard 负向场景均在调用 Apple 工具前拒绝。身份函数在临时个人目录验证了同目录稳定、不同目录不同、0600 权限、损坏值拒绝；临时测试目录已删除。详情见 `ios-preparation.json`。

## 资源

一次只运行 1 台 iPhone 16e / iOS 26.0，使用现有 Xcode 26.0.1。需要独占原生 VM 槽；预留约 3 GiB 私有磁盘预算，其中重新构建 WDA 约 190 MiB。此前完整通过耗时约 158 秒，仅作排期参考。

不把本次检查扩大为 iPad、真机、生产 VLM/Runner、完整实时镜像或公司业务 App 的验收。
