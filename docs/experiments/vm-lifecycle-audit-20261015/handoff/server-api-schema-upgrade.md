# 三端 Server API、数据库与升级交接

> 审计日期：2026-10-16（Asia/Shanghai）。基线 `452e7d7`，初始交付 `74f209d`。**最终产品提交：`2dc9844ffd86b88f2e41acfc79fae87ae222d631`（`2dc9844`）**。
> “最终行为”包含本次升级审计追加修复；不能把修复后的结论倒写成 `74f209d` 已具备。最终 Git blob、schema 与源码核验见 [final-schema-verification.json](final-schema-verification.json)，33 个产品文件 SHA-256 见 [combined-regression-evidence.json](combined-regression-evidence.json)；此前专项摘要 [server-upgrade-source-manifest.json](server-upgrade-source-manifest.json) 保留为历史记录。API/schema审计使用 Git 快照、内存数据库、假 Agent、ASGI/个人 loopback；本文更新只读取已有证据。文中的原生验收引用独立个人任务已归档结果，本项未重跑、未访问公司端点或调用默认 CLI 下发。

## 1. 可以直接用来决策的结论

- **数据库新增表 0、增删改列 0、迁移文件改动 0。** VM ID 生成格式、现有实例 ID、三端原生入口均保留。
- **三端共 41 个 HTTP 操作，分布在 31 个 URL 路径；新增、删除、改名均为 0。** 计数以“方法＋路径”为一项。13 个请求体模型的字段、类型、默认值、约束均未改。
- **19 个生命周期操作的处理规则改变**：每端的修改、删除、探查、下发、启动、停止各一项，共 18；另有鸿蒙 `force-release` 一项。6 个列表/详情查询承载新的运行信息，但外层实例字段结构未变；其余 16 个操作保持原接口契约。
- 前端仍走原操作入口；首次下发的 `starting` 是处理中，**只有对应运行成功回包才确认绑定**。运行成功、可接单与本次操作成功需要分别核实。
- **不能安全混跑新旧 Server/Agent 来做生命周期操作。** 特别是“新 Agent 先连旧 Server”已有 iOS 误删旧实例的隔离重放证据。升级顺序见第 7 节。
- 现装外部 CLI 0.3.0 不能被称为完全兼容。Server 已恢复已知发送失败的 HTTP 错误；CLI 的操作关联与换机新 ID 修复已合入个人仓库的本地 main，尚未推送、发布或更新现装包，见第 8 节。

### 统计依据与边界

| 口径 | 结果 | 证据 |
|---|---:|---|
| VM HTTP 方法＋路径 | Android 15、iOS 12、Harmony 14，共 41；均保留 | 两份提交的 OpenAPI 与 AST 清单 |
| 请求体模型 | 13 个；0 个结构变化 | 三端 `schemas.py` Git blob 完全相同；OpenAPI components 相同 |
| 数据模型/迁移 | 新增表/列/迁移均 0 | [unchanged-schema-blobs.json](unchanged-schema-blobs.json)，22 个相关 blob 一致 |
| 直接改动的路由函数体 | 15 个（每端 5）；仅用于代码差异核对 | [route-ast-comparison.json](route-ast-comparison.json)，不把共享 service 的影响漏算 |
| 最终 OpenAPI 参数/请求/声明响应 schema | 0 个功能性结构变化 | [openapi-workingtree-diff.json](openapi-workingtree-diff.json) 只剩 2 处说明文字变化 |

`74f209d` 曾无意去掉 Android/Harmony DELETE 的 `vm_id` 长度校验（1–64）。本轮已恢复，长于 64 的路径参数仍为 422。原始差异保留在 [openapi-route-diff.json](openapi-route-diff.json)，最终快照见 [openapi-workingtree.json](openapi-workingtree.json)。OpenAPI 的 `Dict[str, Any]` 响应没有列举任意 JSON 子字段，因此“声明 schema 未改”不代表结果语义完全相同。

上一轮 `8a2a3be` 相对其父提交改动 3 个产品文件：Android manager、Agent WS 调度、Android VM API。从该提交提取源码生成的 OpenAPI 为31个路径、41个方法＋路径、13个请求体模型，与前次快照一致；相对 `452e7d7` 的参数/请求/声明响应功能结构差异为0。

后续 `e26e3d7`、`f263af3` 与最终 `2dc9844` 的产品增量均只涉及 Android manager；最新增量另修改1个镜像测试文件、新增1个容量测试文件。整个 Server 目录、Agent WS、协议、配置与 migrations 的 Git 对象均与 `8a2a3be` 相同；22个已有模型、schema、配置和迁移相关 Git blob 仍与基线一致。因此沿用上述 OpenAPI 证据，没有再次生成 OpenAPI，没有 DDL/API/WS/启动参数改动。各次增量证明追加在 [final-schema-verification.json](final-schema-verification.json)，原 OpenAPI、22个 blob 记录及历史原始 JSON 均未改写。

本文件只枚举 VM 模块的 41 个 HTTP 操作；通用 Run、真机、安装、镜像与设备池影响见 [main-flow-impact.md](main-flow-impact.md)。

## 2. 全部 HTTP 操作清单

前缀：Android `/api/internal/vm`；iOS `/api/internal/ios-sim`；Harmony `/api/internal/harmony-vm`。`L` 为上述 19 项生命周期语义变化，`Q` 为 6 项查询运行信息变化，`U` 为 16 项保持契约。

### Android

| 方法 | 路径（加该端前缀） | 请求体模型 | 变化口径 |
|---|---|---|---|
| GET | `/instances` | `无` | Q：查询结构原样，runtime/状态语义更新 |
| POST | `/instances` | `AndroidVmCreateReq` | U：本轮未改接口契约 |
| GET | `/instances/{vm_id}` | `无` | Q：查询结构原样，runtime/状态语义更新 |
| PATCH | `/instances/{vm_id}` | `AndroidVmPatchReq` | L：操作/绑定后配置准入 |
| DELETE | `/instances/{vm_id}` | `无` | L：退账与宿主清理结果分开 |
| POST | `/instances/{vm_id}/dispatch-candidates` | `无` | L：有界探查、发送失败不空等 |
| POST | `/instances/{vm_id}/dispatch` | `AndroidVmDispatchReq` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/start` | `无` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/stop` | `无` | L：单次操作、归属/失败规则 |
| GET | `/device-profiles` | `无` | U：本轮未改接口契约 |
| POST | `/device-profiles/import-play-catalog` | `AndroidDeviceProfileImportReq` | U：本轮未改接口契约 |
| POST | `/device-profiles/import-google-supported-devices` | `AndroidDeviceProfileImportReq` | U：本轮未改接口契约 |
| GET | `/device-brands` | `无` | U：本轮未改接口契约 |
| GET | `/device-facets` | `无` | U：本轮未改接口契约 |
| GET | `/coverage-profiles` | `无` | U：本轮未改接口契约 |

### iOS

| 方法 | 路径（加该端前缀） | 请求体模型 | 变化口径 |
|---|---|---|---|
| GET | `/instances` | `无` | Q：查询结构原样，runtime/状态语义更新 |
| POST | `/instances` | `IosSimVmCreateReq` | U：本轮未改接口契约 |
| GET | `/instances/{vm_id}` | `无` | Q：查询结构原样，runtime/状态语义更新 |
| PATCH | `/instances/{vm_id}` | `IosSimVmPatchReq` | L：操作/绑定后配置准入 |
| DELETE | `/instances/{vm_id}` | `无` | L：退账与宿主清理结果分开 |
| POST | `/instances/{vm_id}/copy` | `IosSimVmCreateReq` | U：本轮未改接口契约 |
| POST | `/instances/{vm_id}/dispatch-candidates` | `无` | L：有界探查、发送失败不空等 |
| POST | `/instances/{vm_id}/dispatch` | `IosSimVmDispatchReq` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/start` | `无` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/stop` | `无` | L：单次操作、归属/失败规则 |
| GET | `/catalog` | `无` | U：本轮未改接口契约 |
| GET | `/health` | `无` | U：本轮未改接口契约 |

### Harmony

| 方法 | 路径（加该端前缀） | 请求体模型 | 变化口径 |
|---|---|---|---|
| GET | `/instances` | `无` | Q：查询结构原样，runtime/状态语义更新 |
| POST | `/instances` | `HarmonyVmCreateReq` | U：本轮未改接口契约 |
| GET | `/instances/{vm_id}` | `无` | Q：查询结构原样，runtime/状态语义更新 |
| PATCH | `/instances/{vm_id}` | `HarmonyVmPatchReq` | L：操作/绑定后配置准入 |
| DELETE | `/instances/{vm_id}` | `无` | L：退账与宿主清理结果分开 |
| POST | `/instances/{vm_id}/force-release` | `HarmonyVmForceReleaseReq` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/dispatch-candidates` | `无` | L：有界探查、发送失败不空等 |
| POST | `/instances/{vm_id}/dispatch` | `HarmonyVmDispatchReq` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/start` | `无` | L：单次操作、归属/失败规则 |
| POST | `/instances/{vm_id}/stop` | `无` | L：单次操作、归属/失败规则 |
| GET | `/settings` | `无` | U：本轮未改接口契约 |
| PUT | `/settings` | `HarmonyVmSettingReq` | U：本轮未改接口契约 |
| GET | `/catalog` | `无` | U：本轮未改接口契约 |
| POST | `/catalog/import` | `HarmonyVmCatalogImportReq` | U：本轮未改接口契约 |

## 3. 请求体与返回字段

### 3.1 原请求体完整索引

以下字段清单来自最终 OpenAPI，并与 `452e7d7` 相同。JSON Schema 的 required 不替代原有业务校验；完整类型、枚举、上下限、默认值保存在 [openapi-452e7d7.json](openapi-452e7d7.json)、[openapi-74f209d.json](openapi-74f209d.json) 与最终快照中。

| 模型 | 字段 | JSON Schema required |
|---|---|---|
| `AndroidDeviceProfileImportReq` | `csv_text`, `source_url`, `collected_at` | `csv_text` |
| `AndroidVmCreateReq` | `name`, `alias`, `profile_ref_type`, `profile_ref_id`, `profile_id`, `profile_name`, `config_version`, `config_json`, `capability_marks`, `api_level`, `abi`, `system_type`, `system_image`, `screen_width`, `screen_height`, `density`, `orientation` | 无 |
| `AndroidVmDispatchReq` | `agent_id` | `agent_id` |
| `AndroidVmPatchReq` | `name`, `alias`, `profile_ref_type`, `profile_ref_id`, `profile_id`, `profile_name`, `config_version`, `config_json`, `capability_marks`, `api_level`, `abi`, `system_type`, `system_image`, `screen_width`, `screen_height`, `density`, `orientation` | 无 |
| `HarmonyVmCatalogImportReq` | `manifest`, `source_url`, `collected_at` | `manifest` |
| `HarmonyVmCreateReq` | `name`, `alias`, `device_type`, `os_version`, `api_version`, `abi`, `image_id`, `screen_profile`, `fold_state`, `screen_width`, `screen_height`, `density`, `screen_size_in`, `memory_gb`, `storage_gb`, `boot_mode`, `config_json` | 无 |
| `HarmonyVmDispatchReq` | `agent_id` | `agent_id` |
| `HarmonyVmForceReleaseReq` | `confirmed`, `reason` | `reason` |
| `HarmonyVmPatchReq` | `name`, `alias`, `device_type`, `os_version`, `api_version`, `abi`, `image_id`, `screen_profile`, `screen_width`, `screen_height`, `density`, `screen_size_in`, `memory_gb`, `storage_gb`, `boot_mode`, `config_json` | 无 |
| `HarmonyVmSettingReq` | `instance_uuid` | 无 |
| `IosSimVmCreateReq` | `alias`, `device_type`, `runtime`, `name`, `config_json` | `alias`, `device_type`, `runtime` |
| `IosSimVmDispatchReq` | `agent_id` | `agent_id` |
| `IosSimVmPatchReq` | `alias`, `name`, `device_type`, `runtime`, `config_json` | 无 |
### 3.2 成功响应仍保留的结构

- 创建/复制返回实例对象；列表返回数组；详情返回实例对象。
- 探查返回 `request_id, agents`；每个候选继续用 `ok/reason/details` 等原字段。
- 启停/下发正常受理仍返回 `sent, instance`；`sent:true` 表示命令受理，不表示原生实例已运行或就绪。
- 换 Agent 成功受理仍返回 `sent, switched:true, old_vm_id, instance`，其中 `instance.id` 是新 ID。
- 删除 `deleted:true` 只确认总账、别名与旧设备路由已经退役；宿主本地清理另由 `cleanup_sent` 表示是否发出，不能据此声称已清干净。

### 3.3 正常响应外层键的实际差异

| 操作/分支 | 基线 `452e7d7` | 最终行为 |
|---|---|---|
| Android DELETE | `id, deleted, alias_deleted, avd_cleanup_sent` | 增加通用 `cleanup_sent`；原键保留 |
| iOS DELETE | `id, deleted, cleanup_sent` | 增加 `alias_deleted, avd_cleanup_sent`；后者为兼容性同义发送结果，不表示 iOS 使用 AVD |
| Harmony DELETE | `id, deleted, cleanup_sent, quarantined_port` | 增加 `alias_deleted, avd_cleanup_sent`，移除 `quarantined_port`；总账退役时释放该行租约，旧原生残留由身份核验/孤儿清理隔离 |
| Android/Harmony 下发中的换 Agent 分支 | 无 `old_cleanup_sent` | 增加 `old_cleanup_sent`；不等待旧宿主确认，新的配置已原子落账 |
| Harmony force-release 的空资源 no-op | 有 `warning` | 保留 `released, instance`，不再返回旧强制释放警告；持有绑定或资源时直接拒绝 |

实例 `to_dict()` 的外层键未改。iOS 的系统版本标识 `runtime` 仍是字符串；其运行信息在 **`runtime_state`**，另两端在 **`runtime`**。

### 3.4 失败 HTTP 语义与准确操作结果

| 场景 | 基线 | `74f209d` 中间状态 | 最终行为 |
|---|---|---|---|
| Android start/dispatch 实际发送失败 | 409 | 200 + `sent:false` | 恢复 409，保留失败操作/实例证据 |
| iOS start/普通 dispatch 实际发送失败 | 502 | 200 + `sent:false` | 恢复 502 |
| iOS stop 在线但发送失败 | 502 | 200 + `sent:false` | 恢复 502 |
| iOS stop 已离线 | 200 + `sent:false` | 200 + `sent:false` | 502；该次停止失败，不能当已停止 |
| Android/Harmony stop 实际发送失败 | 200 + `sent:false` | 200 + `sent:false` | 409；这是明确的失败语义收紧 |
| Harmony start/dispatch 实际发送失败 | 409 | 409 | 仍为 409，改为保留可查证据的 detail |
| 换机已经退旧，但新目标启动未受理/失败 | 不同分支可能失败；旧记录是否保留取决于旧实现的提交顺序 | 200 + `sent:false` | 三端 409，必须携带新 ID；不恢复旧 ID，不自动续做 |
| 原生执行已受理，随后 Agent 回报失败 | 先 200，再从实例状态读结果 | 同左 | 仍先 200；后续查询为 error / operation failed，不能把这种情况误改为“请求发不出” |
| DELETE 已退总账，旧宿主通知失败 | 200 | 200 | 仍为 200 + `cleanup_sent:false`，删除例外保持 |
| 没有需要下发的停止 no-op | 某些端 200 + `sent:false` | 保留 | 不属于发送后失败；客户端须读取实际实例状态，不把它显示成“已发出命令” |

明确发送失败的响应示例（普通 iOS 为 502，Android/Harmony 为 409）：

```json
{
  "detail": {
    "message": "启动下发失败，本次操作已结束；请查询当前实例与操作结果",
    "sent": false,
    "vm_id": "current-vm-id",
    "request_id": "current-attempt-id",
    "instance": {"id": "current-vm-id", "state": "error", "assigned_agent_id": null}
  }
}
```

换机失败在该 detail 内还带 `switched:true, old_vm_id, old_cleanup_sent`；`vm_id` 与 `instance.id` 都指向新配置。若另一请求已删除新配置，`instance:null`，仍保留新 `vm_id` 和旧 `old_vm_id`。Server 不新增 `retired_vm_id/new_vm_id` 字段；CLI 可在展示层把 `old_vm_id` 映射为 `retired_vm_id`。其他原有的 404/409/422 仍可能使用字符串或旧字典 detail，调用方不能假定所有错误都具有 instance。

原始无 `--wait` CLI 对 2xx 直接退出 0，因此上述发送失败 HTTP 修复属于兼容必要项。个人 HTTP 回放覆盖三端 start/stop/dispatch 与换机失败新 ID；不曾向默认 CLI 环境发请求。

## 4. 数据库：没有 DDL，新增信息写在已有 JSON 内

三个 VM ORM、全局模型、三个请求 schema 文件、`shared/protocol.py`、`config.py`、`pyproject.toml` 与全部既有 migration 在指定两提交间未变。新 helper 使用既有 VM 行和 PostgreSQL 事务锁，不新建表、列、索引、扩展或持久化任务队列。SQLite 测试用进程内锁验证。

| 已有表 | 保持不变的关键列 | 运行 JSON 列 |
|---|---|---|
| `android_vm_instances` | `id, assigned_agent_id, adb_serial, state` | `runtime` |
| `ios_sim_vm_instances` | `id, assigned_agent_id, udid, state` | `runtime_state` |
| `harmony_vm_instances` | `id, assigned_agent_id, hdc_port, hdc_serial, lease_token, state` | `runtime` |

新增/统一使用的 JSON 主体键：

| 键 | 含义 |
|---|---|
| `owner_host_id` | 已确认归属的 Agent 安装身份，供跨进程会话认领；不是硬件指纹 |
| `instance_created` | 已确认原生实例存在的事实；不等于“本次创建”，后者另有 provenance |
| `operation` | 当前/最近一次原生操作的关联与终态；没有原生操作的自动重试或恢复队列；重复回报事实不等于重复执行 |
| `last_status` | Android/Harmony 已有；iOS 在共享失败/清理证据路径也会写入 |
| `details` | iOS 已有；另两端共享失败/清理证据路径也会写入 |

`operation` 的完整新增键集合（按发生阶段可选）：

- 开始：`action, request_id, pending, outcome, target_agent_id, target_host_id, first_bind, started_at, deadline_at, timeout_sec`。
- 结束：`completed_at, failure_reason`。
- 资源证明：`created_in_attempt`。
- 已失败首次绑定的确切残留清理记录：`cleanup_sent, cleanup_request_id`；仅匹配清理回执释放证据，不触发下一次 start/switch。

`outcome` 为 `pending / succeeded / failed`；这是单次操作结果，不能与 VM 的物理 `state` 混为一谈。`details` 本来就是开放字典，本轮用于决定行为的证据包括 `cleanup_confirmed, instance_created, instance_deleted, created_in_attempt, instance_preexisting, rollback_new_instance`。`instance_created:true` 不能代替 `created_in_attempt:true`；明确的 `created_in_attempt:false` 会保护已有原生数据。

没有新增 VM 的 `deleting` 或 `pending_cleanup` 状态/工作流。Harmony 原本已有的顶层 `cleanup_pending` 布尔字段仍表示其 HDC 资源/租约尚持有，不是新建的后台重试订单。

**零迁移结论仅相对 `452e7d7` 的数据库模型成立。** 本轮没有连接待部署数据库核查其真实 schema；若环境比该基线更旧，仍须先满足已有的 Android/Harmony/iOS migration，不得由“本次零 DDL”推断任何历史数据库都已齐备。

## 5. WS 与本机存储的兼容边界

消息类型和原 `request_id` 保留；`shared/protocol.py` 的 Git blob 未变。新字段是附加语义，不是版本协商：

| 方向 | 附加信息 | 缺失时的实际边界 |
|---|---|---|
| Agent hello → Server | `host_id` | 缺失不能提供持久宿主保护；原 agent_id 仍是会话身份 |
| Server start → Agent | `require_existing_instance`；iOS 还带已知 `udid` | 旧 Agent 忽略后仍可能隐式重建；旧 Server不下发则新 Agent也无法完整知道原实例本应存在 |
| Server start → Agent | `rollback_new_instance` | 只有首次新建才允许回滚本次资源；旧 Server不发时新 Agent不会替它承诺该事务语义 |
| Server lifecycle → Agent | `operation_timeout_sec` | 旧 Agent没有整次预算，底层命令可能在 Server 结束后继续；Server仍拒绝超时绑定 |
| Agent 观察 → Server | `observed_request_id` | 旧观察没有同等操作隔离能力 |
| Agent 对账 → Server | `inventory_complete, unknown_vm_ids, instance_proofs` | 最终 Server仅显式 `true` 才用缺席/停止信息作否定结论；旧/部分清单只保守使用可确认事实 |
| Agent 回包 details → Server | `created_in_attempt` 等资源证据 | 没有“本次确实创建”的证据，就不自动删除保留配置所对应的原生实例 |

`8a2a3be` 将已有的 `operation_timeout_sec` 字段同时带入 Android stop 指令，值取本次 Server operation 的 **120 秒**预算；Server 的停止时限未增加，HTTP stop 的路径、请求体和参数均不变。Agent 内部将排队、进度发送、线程/VM 锁等待、原生退出和释放核验计入一次收到的预算，给原生退出保留其等待窗口，不再每一步重新计时。缺少该字段的旧 Android stop/delete 指令在 Agent 入队前补同样的 120 秒预算。超时仍结束本次操作，保留未确认事实，不新增自动重试或重新启动。

`e26e3d7` 只在 Android manager 的普通停止路径增加本次 guest sync 完成检查：无法唯一确认原实例通道或同步未确认时，不继续停止，保留未确认结果；已有120秒预算不变。可选的 `details.sync_completed` 是原有开放诊断字典内的信息，不是新增 HTTP 请求字段。同步完成仅表示这次系统同步命令得到确认，不代表应用尚未提交的内容或全部底层 I/O 都已成功；显式删除仍走独立销毁意图。

`f263af3` 在复用已有 Android AVD 前，只读核对真实原生目录、`image.sysdir.1` 和 SDK `package.xml` 的唯一 package ID；与有效 `system_image` 不符或不能核实时，拒绝本次启动并保留旧盘/原实例，不改镜像、不隐式重建。Server PATCH 保持原入口，历史误配仍可修回正确要求；该守卫不自动统一 profile、config_json.system 与顶层标签，也不保证同 package ID 的二进制/revision 一致。详见 [已有 AVD 镜像配置审查及修后范围](android-existing-image-configuration-audit.md)。

`2dc9844` 在复用已有 AVD 时还保留其已确认的数据分区容量：请求值等于或小于现有值时，不把旧请求写回成更小容量；无法读取或唯一核实容量时拒绝修改/启动、保留原盘。这是 Agent 内部存量配置保护，没有新增 HTTP/WS 参数或数据库字段。

新增本机文件为 `<storage_dir>/agent-host-id`（0600，原子生成/读取）。它属于这份 Agent 安装，而非物理硬件编号。**更换 storage、丢失该文件，或同一机器开两份不同 storage 的 Agent，都会产生不同 host_id，不能据此认定存在两份可删除的原生数据。**

最终规则是：**总账仍有该 VM、但宿主身份冲突时，不自动接管、不自动删除，只隔离报告并记日志；总账已明确退役该 ID，才清理精确匹配的受管孤儿。** 这是 `74f209d` 的 host-id 冲突误删路径在本轮被修正的地方，原 owner 在线也不构成删除另一报告者的充分条件。

## 6. 新旧混合版本矩阵

证据口径：字段是否存在、默认值与分支为【代码事实】；旧 iOS 对账误删、host-id变更风险等为【隔离重放】；混合部署中的完整原生启动/数据后果属于【代码推断，未做混版本实机验收】，不得将消息能解析写成已通过生产混跑。

| 组合 | 可以确认的兼容事实 | 不具备的保证/风险 | 发布建议 |
|---|---|---|---|
| 旧 Server＋旧 Agent | 原系统行为 | 原问题仍在 | 仅为原运行基线 |
| 新 Server＋新 Agent | 本轮契约、请求关联、资源证明与库存完整性配套 | 仍须尊重各端原生硬件/通道限制；已受理不等于可接单 | 最终目标组合 |
| 新 Server＋旧 Agent | 消息名称、成功 ACK 的 request_id 可解析；无 schema 阻挡 | 旧 Agent不执行新预算/回滚/禁止重建；旧错误停止回包也可能被采信；停止实例可能不能完整认领 | 仅维护窗口的保守过渡；禁止发起生命周期写操作 |
| 旧 Server＋新 Agent | 消息字段大体可解析，旧 Server会忽略新字段 | **已复现 iOS 旧对账把仍在总账的实例当孤儿下发 DELETE**；不能只看“能连接” | 不采用 Agent-first 发布 |
| 新 Server＋回退旧 Agent | 旧消息能到达 | 保存过 owner_host_id 后，旧 Agent不发 host_id且换新会话，可能无法进入设备池；不能视为透明回退 | 不单独回退 Agent |

关键证据链：默认 Agent 重启会获得新的会话 `agent_id`；旧 iOS Server只允许 `reclaimed` 跨旧 owner，新 Agent对 Booted/WDA 未就绪改报 `wda_not_ready`。在“新会话＋WDA未就绪”这个触发条件下，旧 Server拒绝归属更新，随后只查询“归当前 Agent 的 VM”，把上报但不在该子集的现有实例当孤儿删。固定配置的 agent_id 可能避开这个具体分支，不能据此推出混合版本安全。证据源为 `452e7d7:server/ios_sim/service.py:319-335,412-420,448-459` 与 `74f209d:agent/ios_sim/manager.py:1025-1036`（均以 `backend/ai_phone/` 为前缀），并由 [独立升级审查](upgrade-independent-review.md) 的提取旧函数隔离重放确认。

旧 iOS Agent `shutdown` 失败仍返回 stopped、查询失败可能创建新实例等行为也有旧源码证据；Server 无法从一个错误的成功回包推断真实关机。新老消息可解析不等于运行契约完整兼容。

## 7. 保留已有实例的可执行升级顺序

### 升级前

1. 暂停 VM 创建、探查绑定、启停、换 Agent、删除，以及依赖这些设备的自动调度；等待当前操作终态、任务/安装/观看占用结束。新版需确认没有 `operation.pending:true`；旧版没有该元数据，还要检查 VM 不在 starting/stopping 等过渡态。不要在原生启动中途切版本。
2. 从**明确选定的目标环境**导出三类 VM 列表、别名与当前 Agent 归属；保存 VM ID → AVD/UDID/实例名/端口、启动状态、数据检查点。不要用未确认指向的默认 CLI。
3. 备份 Server 当前数据库、原 Agent 配置与启动方式、完整 storage；保留原生实例数据。Android 的 AVD 目录、iOS CoreSimulator UDID、Harmony实例目录并不等于 storage，不能只备份 storage 就声称备份了 App 数据。
4. 目标数据库必须包含这些受管 VM ID 的最新总账。**不要把有现存 VM 的 Agent 连接到空数据库、旧数据库快照或另一个环境的 Server**；孤儿清理会按总账缺席执行删除。单独改 storage 并不能隔离同一 OS 用户下共享的原生模拟器空间。

### 实施

5. 先退出旧 Agent 服务，防止新 Agent意外接入旧 Server；退出 Agent不等于要求删除 VM。旧 iPad WDA 的常驻 xcodebuild可能随 Agent结束，属于自动化暂不可用，不应据此重建原模拟器。
6. 更新 Server 与前端到同一最终代码版本，使用原数据库。该版本不要求额外 SQL migration；核验服务启动后再继续。
7. 在**原 OS 用户、原配置、原 storage、原 SDK/实例路径**启动新版 Agent。首次升级生成的 `agent-host-id` 必须保存；之后升级不能更换或复制它到别的宿主。一次一台 Agent 接回并核验后再扩大。
8. 只让对账恢复已有归属与实际状态；不要为了“升级接回”点击换 Agent、复制或重新创建。换 Agent的定义是退旧 ID、另起空白机器，不能用作恢复工具。

### 验收后才恢复业务

- Server仍是原 VM ID，原生 AVD/UDID/实例名与升级前对应；端口是通道地址，不能用“端口一样”代替身份核对。
- 既有 running/stopped 数据事实得到确认，别名与当前 Agent 一致；host-id冲突必须先检查原 storage/身份文件，不通过删除/强制释放来绕过。
- 对每端至少选一个原有实例做停止→原实例启动→确认 App 数据检查点→真实执行/观看；运行中/HTTP 200/Agent在线都不单独算验收通过。
- 若 iOS仅 WDA未恢复，保留原 UDID，使用原实例的正常停止/启动或受支持 WDA恢复路径；不执行 erase/delete/换机。
- 原有配置、关联和数据不应要求用户重建。异常未确认停止时先查清并安全停止，不能靠反复 dispatch 修复。

最终 `2dc9844` 的升级守卫与反例已通过统一隔离回归，见 [最终回归证据](combined-regression-evidence.json)。该版本的 Android a6-a3 已通过 save/discard_changes/cold_boot 快照行为、错误但已安装镜像拒启（0次新 Popen、原33个AVD文件不变）、恢复原配置再启动及清理验证：[a6-a3 汇总](native-followup/android-snapshot-a6-a3-results.json)、[总控复核](native-followup/android-a6-root-review.json)。a7 原个人 `no_snapshot` App 场景也已完成真实安装、启停/重启、重连、页面数据保留及清理，且总控三图与回执独立核验通过：[a7 汇总](native-followup/android-app-install-a7-results.json)、[总控三图核验](native-followup/android-a7-root-review.json)。这些结果覆盖已测个人宿主与场景，不代表 Windows原生、真机、任意App、外网/VLM/公司业务验收；此前 `74f209d` 的其他原生结果也不自动覆盖后续补丁。

### 历史鸿蒙停止确认缺失

旧 Agent 注册表可能已有 `ready:false`，却没有 `cleanup_confirmed`，同时 Server仍持有该 VM 的旧租约。这种情况不能直接当作“已停止”，也不能为了接回实例删除总账或强制释放。

旧总账尚无持久宿主身份时，最终实现只在完整库存、真实受管实例名/路径证明、原 token/serial/port 与当前租约记录全部相符，并且原 owner 离线、宿主身份不冲突时，保存同一实例的新会话归属。已有持久宿主身份的记录按原来的同 host_id 会话接续规则处理。**原状态、错误、停止未确认事实和租约全部保留，不自动执行任何操作。** 用户再使用原来的“停止”入口，新 Agent实际检查实例进程、HDC 与端口；确认清理后才向 Server报 stopped、释放租约，原实例目录与数据保留。

最终实现已经取消依据历史 `cleanup_confirmed:true` 直接返回成功的快路径；每次显式 stop 都重新核对当前原生事实，`ready:false` 或旧确认位均不能替代本次检查。没有 Popen 句柄的恢复实例也会在停止后复查精确实例进程，仍活或归属未知均不允许释放/继续删除。确实已经停止的实例经核验后仍可返回 `not_running`，不必额外发送 native-stop。证据见 [鸿蒙恢复实例停止独立复核](harmony-reclaimed-stop-independent-review.md)。

证明不全、身份冲突、实例仍连接、端口仍占用或进程归属不可查时，继续保持未确认。这个修复没有把所有历史异常变成自动恢复；不要用换 Agent/删配置代替数据保留型排查。该链通过临时注册表和真实 manager payload→Server→普通 stop 的隔离桥接验证，原生检查使用显式 fake，不能称为实机升级验收。

### 回退限制

回退也应先冻结写操作、退出 Agent，再恢复一套配套 Server/Agent/前端。**不要直接恢复一个缺少新 VM ID 的旧 DB 备份后让 Agent连上**；这些新实例会被旧总账认作孤儿。没有 DDL变化意味着不需要为了代码回退而回滚数据库，但旧代码仍不理解新 operation 元数据且原缺陷会回来，不能把“数据库能打开”当成回退验收。

保留 storage 与原生数据；不要单独回退旧 Agent接新 Server。回退前需终结新版本未完成操作，并核对当前全量总账包含本机受管实例；若无法证明，保持维护窗口，不启动会自动对账的混合组合。

## 8. 外部 CLI 与其他调用方

本机现装为 `@guanghe-pub/aiphone-cli 0.3.0`，静态源码在 `~/.nvm/versions/node/v20.20.0/lib/node_modules/@guanghe-pub/aiphone-cli/`。Server仓库更新不会升级该独立包。详细证据见 [cli-compatibility.md](cli-compatibility.md) 和 [cli-fix-handoff.json](cli-fix-handoff.json)。

| 项目 | 原 0.3.0 | 已合入本地 main 的修复/最终接口要求 |
|---|---|---|
| 非 `--wait` 启停 | HTTP2xx直接退出0，忽略 sent:false | Server已恢复实际发送失败的非2xx；CLI修复也会拒绝明确 sent:false |
| restart 的 stop→start | 只轮询 stopped，可能跨过迟到 stopped事实中的 failed操作 | CLI修复关联本次 request_id，先检查 operation.outcome；失败不继续start |
| 换机后失败的 VM ID | 错误外层仍可能指旧 ID，新 ID藏在response.instance | CLI修复从 detail.instance/detail.vm_id提取新 ID，同时保留退役旧 ID |
| `list --agent` | 只按 assigned_agent_id筛选 | 本次修复未改变：首绑未确认时不属于该 Agent；查看启动目标需读 operation.target_agent_id或完整列表 |
| 默认等待 | 180秒 | 本次修复未改变。Server默认启动预算 Android240/Harmony300/iOS600秒，均由现有设置计算；客户端等待超时不是 Server取消，不能直接自动重试 |
| DELETE清理未送达 | 配置删除成功与宿主清理区分 | 保持：HTTP200/配置删除成功，cleanup_sent:false不伪装为宿主已清干净 |
| Harmony force-release | CLI没有暴露命令 | Server收紧不会直接破坏该版本CLI入口；直接HTTP调用者须改用正常停止/删除 |

修复源自个人 worktree `/Users/dongxin/.codex/worktrees/cli-vm-lifecycle-compat/aiphone-cli`，提交 `e65dad549d2d667972c44718962d6c6f87bf8eb9`；58 项隔离测试通过。该提交已在本地以 fast-forward 合入个人仓库 main，版本号仍为 0.3.0，**尚未推送、未发布，也未更新现装 CLI**。本地合并不代表用户安装的0.3.0已获得修复。

Server `8a2a3be` 已与上述个人 CLI 修复完成真实 HTTP/CLI 子进程回放，**12 项通过**，证据见 [vm-cli-final-8a2a3be.json](vm-cli-final-8a2a3be.json)。该记录中的4个 Server 源文件 SHA-256 与当前 `2dc9844` 完全相同，故复用相同 Server 路由代码的验证证据；**没有在后续 `e26e3d7`、`f263af3` 或 `2dc9844` 再跑一次 CLI**，也不以此证明新增 Agent 原生路径。该回放使用隔离 SQLite 与假 Agent，不使用默认 CLI profile。

其他客户端的最小适配规则：

1. 受理后记录返回 `instance.id` 与 `runtime.operation.request_id`（iOS用 runtime_state），按本次 outcome 判断成功/失败；不要只看HTTP200、sent或某次物理state。
2. 首次 starting 的 assigned_agent_id为空是正确结果；确认归属要等同次成功ACK。
3. 换机无论成功、失败都接收新ID；失败HTTP detail也可能含已经生效的退旧结果，不能继续对旧ID重试。
4. 读取错误detail时兼容字符串、原字典及新增 evidence字典；忽略未知 JSON字段，不依赖固定中文错误文案。
5. 原生 state可在迟到证据下成为 stopped，而本次 operation仍failed；自动restart必须尊重关联操作失败，普通查询wait才只表示事实状态。
6. 客户端等待预算应覆盖返回的 deadline；超时后查询同一ID/同一操作，不自动创建替代机器。

## 9. 本轮追加升级修复与验证

| 审计发现 | 原 `74f209d` 风险 | 追加修复/回归 |
|---|---|---|
| Start读取owner与锁内owner变化 | 三端普通 `/start` 会误入换 Agent并删旧ID | start意图禁止switch，锁内发现变更409；旧ID、归属、原生身份不变，无DELETE命令 |
| 旧库存缺完整标记 | Android/iOS可能把失败空清单当缺失 | 三端只有显式 true才用missing/stopped否定证据；正向孤儿证明仍可清已退役ID |
| 宿主身份文件更换/双Agent | 即使同一原生目录，也可能被当foreign副本删除 | 总账仍在时冲突一律不接管、不删；日志保留owner/reporter信息 |
| DELETE参数校验回退 | 两路超长ID从422变404 | 恢复原Path 1–64 |
| 明确send失败的HTTP200 | 原CLI无wait误退出成功 | 恢复/收紧为平台失败码，并携带当前/新ID与request证据 |
| 鸿蒙旧记录停止确认缺失 | 原会话接续回滚；ready:false可能被普通stop误报已清理 | 完整精确实例/租约证明只保存会话接续；普通stop重新检查，保留数据与未确认租约 |
| Android停止退出等待与预算 | 原生退出可能在固定短等待后被过早强制收口，且各步骤各算超时 | stop沿用120秒operation预算并下发既有WS字段；Agent整次停止/清理共享预算，HTTP/schema/DDL不变 |
| Android已有AVD镜像与配置不符 | 新请求可通过镜像探查，但同名旧AVD仍指向旧镜像 | Agent写配置/复用前核实真实SDK package ID；不符或未知拒启保留数据，PATCH保留用于修正误配 |
| Android已有AVD容量被旧请求缩小 | SDK首次启动扩大的容量可能被旧请求重新写小，影响后续重启/快照 | 复用时核实原容量，等值/较小请求保留原值；无法确认则不修改/启动 |

普通启动误切机的修复前/后证据：[失败复现](server-start-reclaim-race-before.log)、[保护回归](server-start-reclaim-race-after.log)。

专属测试：[test_vm_upgrade_compat_audit.py](../../../../backend/tests/test_vm_upgrade_compat_audit.py)；独立审查：[upgrade-independent-review.md](upgrade-independent-review.md)。本模块关联回归 `350 passed / 2 skipped`（[server-upgrade-regression.log](server-upgrade-regression.log)，10个文件），包含新增 [鸿蒙旧租约桥接15例](../../../../backend/tests/test_harmony_legacy_lease_upgrade.py)；该历史问题的原始11例先得到 `3 failed / 8 passed`（[修复前日志](harmony-legacy-lease-before.log)），随后增加错租约、归属不可查、端口占用等负例并全部转绿。iOS HTTP/发送边界75项、此前Harmony四文件84项也分别通过；这些历史单项日志保持原样。

最终统一隔离回归为 **`1549 passed / 2 skipped / 73 warnings`**，覆盖 **73 个测试文件**，见 [combined-regression-evidence.json](combined-regression-evidence.json)。测试前后 **33 个产品文件** SHA-256 一致，且逐个与最终产品提交 `2dc9844` 的 Git blob/源码匹配；增量验证记录见 [final-schema-verification.json](final-schema-verification.json)。上述专项已包含在统一回归内，不重复累计通过数量。

这些测试使用隔离数据库、假Agent与真实HTTP处理链；CLI真实loopback回放由个人CLI任务完成。未把隔离回包测试写成原生设备验证，也未把提交74的旧截图冒充本轮追加补丁的原生证据。
