# Android 身份、端口与旧实例升级交接

更新日期：2026-10-16（Asia/Shanghai）。最终产品为 `2dc9844ffd86b88f2e41acfc79fae87ae222d631`（`2dc9844`）。本文只核对源码、Git 历史和已保存的个人测试证据；本次文档更新没有运行原生命令、连接公司环境或修改产品代码。初始审计范围 `452e7d7` → `74f209d` 及 C1–C10 的当时行号作为历史证据保留，不代表最终源码行号。

**最终交付：`2dc9844`。** 最终统一回归为 **1,549 passed / 2 skipped / 73 warnings，73 个测试文件、33 个产品文件指纹一致**，见[最终清单](final-delivery-manifest.json)。最终行为以下列修正和[主交接指南](../../../vm-lifecycle-upgrade-guide.md)为准：普通 start 不会转成换机；总账存在但安装身份冲突时不接管也不删实例；缺少显式完整清单不能证明实例缺失；Android 缓存按 VM ID 与生命周期代次换代；已有 AVD 核验实际镜像，已有容量不被旧请求写小。相关[独立升级审查](upgrade-independent-review.md)、[缓存复核](android-cache-independent-review.md)及下述 a6-a3/a7 最终原生验收已通过。历史版本的计数与结果不累加为最终验收。`9034204` 补充的 wildcard 监听保护仍保留；端口检查不替代精确实例进程退出证据，见[端口补查](native-followup/harmony-port-stop-followup.md)。

### 最终行为：同步屏障、原生退出窗口与既有数据保护

普通停止先对唯一、准确受管的 guest 执行一次 `/system/bin/sync`，同一次 shell 命令随后输出内部生成的唯一 32 位 hex nonce。只有返回码为 0、标准输出精确等于本次 nonce 加换行、且仍在本次截止内，才再次核验目标并继续原 `adb emu kill`、SDK 退出和快照流程；缺标记、旧标记、多余输出、身份歧义或超时均失败，不继续退出、不重试。显式删除和本次确实新建资源回滚使用内部销毁路径，可跳过同步。这是 `e26e3d7` 引入并保留至最终版的保护，没有改 API、WS、数据库或真机路径。同步只证明这次内核同步命令完成，不保证 App 用户态已经提交或全部底层 I/O 无错；nonce 也不是任意 App 数据持久性的证明。[技术边界](native-followup/android-guest-shutdown-semantics.md)、[独立修复审查](native-followup/android-sync-independent-review.md)

已经确认进程退出、相关端口可用的实例，停止保持幂等成功，无需再向不存在的 guest 发同步命令；仍活着却无法确认同步通道的实例则明确失败并保留身份。

`8a2a3be` 修复的是 **Agent 在原生退出已被接受后，仅等 5 秒便可能提前 terminate** 的独立缺口。普通停止继续使用既有 `adb emu kill` 方式：从精确受管进程读取原生退出窗口，未设置覆盖值时采用 SDK 默认 20 秒，合法正十进制整数覆盖值按本次剩余预算约束；无法确认旧进程环境时不猜新 Agent 的环境，只在剩余预算内保守等待。同一 VM 的多个 PID 共用截止时间，覆盖最后一个已接受的原生请求；进程已经退出就立即完成核验。[实现与独立复验](native-followup/android-stop-grace-implementation.md)

普通 stop 使用 Server 既有的 **一次 120 秒操作预算**，通过已有 `operation_timeout_sec` 扣除 WS 排队、进度发送、线程池与 VM 锁等待，并贯穿原生命令、退出等待和进程/端口核验；不会在每个阶段重新计时。显式 delete 同样用一次有界预算贯穿停止和 AVD 删除；首次启动失败的已创建资源另做且仅做一次有界收尾，旧实例数据保护不变。预算耗尽就明确失败、保留身份及未确认清理证据，不再为了返回成功而追加信号或删除操作，也不续跑启动。

已有实例还增加了两项启动前保护：

- **镜像核对。** 在写屏幕/硬件配置前，核对真实 AVD 目录及 `image.sysdir.1` 指向的 `package.xml` SDK 包 ID；已认领活进程的复用路径也不能绕过。请求与实际不符、路径或元数据无法确认时，保留旧盘和旧进程并拒绝启动，不自动重建；恢复正确请求后，在确实已停的前提下可以启动原实例。同一包 ID 不代表二进制 build/revision 完全相同。[独立镜像审查](native-followup/android-existing-image-independent-review.md)
- **容量不倒退。** 已有 AVD 的唯一有效容量按字节或二进制 K/M/G 归一比较；较小或相等请求保留 SDK 原值及其表示，明确增大仍沿用原扩容配置。未知、重复、非法或溢出容量在任何写盘前拒绝；没有实际配置变化就不重写文件，保留原字节和修改时间。新建逻辑不变，没有固定 6 GiB、批量改盘或自动迁移。`internal_storage_mb` **继续表示请求容量，不是实测容量**；SDK 可能上调，已有盘也不会因较小请求被收缩。真实增大或其他硬件变化不保证兼容旧快照。[容量缺口与独立复验](native-followup/android-a6-a2-independent-cause-review.md)

以下历史失败必须保留，不能被最终成功覆盖或过度归因：

- **a2 停启后页面数据丢失的原因仍未知。** 没有当时原生强制收尾分支或 WebView 实际落盘事件的证据，不能把 SDK 退出窗口修复冒认为 a2 根因或已修复证明。[a2 保留证据](native-followup/a2-readonly-findings.md)
- **a3 数据诊断通过，但重启截图白屏、原生 XML 无标记，视觉验收未通过。** 已同步文件及原页面数据回读保留，不等于页面已经可见；实际 WebView 109 的提交机制与因果限制另见[版本核对](native-followup/android-webview-109-persistence.md)。
- **a4 在 `8a2a3be` 上再次丢失原页面记录。** 同步文件、origin/key/provider 保留，重启 localStorage 为 null；两次退出均没有 Agent TERM/KILL，第三图为可读的空记录页面，视觉门槛尚未进入。原失败盘封存；[a4 结果](native-followup/android-app-install-a4-results.json)、[root 实看三图](native-followup/android-a4-root-visual-review.json)。a5 和最终 a7 的成功证明各自实际场景通过，不能追认 a2/a4 的唯一根因，也不能把它们归给已修的 SDK 等待窗口或本次容量问题。
- **a6-a2 在 `f263af3` 上保存了快照，却因容量配置从 6 GiB 退回 4 GiB 而拒载。** 这是已复现并修正的配置缺口，不是已证明用户数据镜像被截断。最终 a6-a3 在新独立实例上实际恢复了快照，旧失败盘与原始证据仍封存，见第 8 节。

## 先明确结论

1. **VM ID 是机器身份，端口是找到这台机器的地址，两者不是同一种编号。** 旧配置、AVD 名和数据不能因为升级而重新生成。
2. **当前默认仍生成 12 位 VM ID。** Agent 已兼容完整 32 位名称，但 Server 尚未切换默认生成规则，不能宣称“新配置已统一采用 32 位 UUID”。
3. **停止释放宿主端口占用，但保留总账里的端口预留、归属和 AVD。** 因此“进程已经停止”和“这个槽位可以分给另一份配置”不同。
4. **删除按最终产品约定先删总账，再尝试一次宿主清理。** 离线残留等 Agent 重连对账清理；后台删除成功不证明宿主此刻已清空。
5. **旧实例升级依赖原 Server 总账、AVD 路径和安装身份保持连续。** 换成空库会把旧实例看作孤儿；已有 `owner_host_id` 后丢失身份文件，也可能被看作另一台宿主。
6. **最终 `2dc9844` 的 a6-a3 验证快照与镜像拒绝/恢复，a7 验证正式安装链、原页面停启保留和可见终态。** a2/a4 历史失败仍单列；所有启动方式下 Agent 被杀或宿主重启后的常驻能力仍未作通用保证。

## 1. 四类标识分别做什么

| 标识 | 当前用途 | 生命周期 |
|---|---|---|
| `AndroidVmInstance.id` | Server 配置的主键，也是下发、对账和删除的目标 | 普通停启、重连和升级保留；换 Agent 按删除旧配置、创建新配置处理 |
| `aiphone_vm_<vm_id>` | 宿主 AVD 的持久名称，可反解 VM ID | 不随 Agent 进程重启改变；对应该实例自己的定义和用户数据 |
| `emulator-<console_port>` | ADB 设备地址；当前 Device 表、Hub 路由仍以 serial 为键 | 与端口有关，不是永久身份；不同宿主本来可以产生相同字符串 |
| `agent_id` / `host_id` | 前者标识 Agent 会话，后者标识这份 Agent 安装 | `agent_id` 默认带随机后缀；`host_id` 保存在 `storage_dir/agent-host-id`，重启继续读取 |

AVD 的真实名称是认领依据，启动参数里的 `debug.aiphone.vmid` 不是可靠身份来源。代码已有说明，保存的 Emulator 日志也出现自定义非 `qemu.*` 属性被忽略的警告。不要为“统一 VM ID”而取消 AVD 名核验或把端口当身份。[C1][C2]

### 12 位与 32 位的实际状态

- 基线与 `74f209d` 的 `models.py` 均为 `uuid.uuid4().hex[:12]`；Android 模型虽然使用 `String(32)`，默认生成函数仍是 `_short_id`。
- Agent 的 `_safe_avd_name` 从保留最多 24 位改为最多 32 位，旧 12 位名称原样保留；两个前 24 位相同的 32 位 ID 不再撞名，已有单元回归。
- **支持读取 32 位不等于已经默认生成 32 位。** 最终 `2dc9844` 的模型默认仍是 `_short_id`；a6-a3 的 `49cc71b3076c`、a7 的 `810137b52abc` 也是 12 位。`74f209d` 的 `24f99a116d25` 仅为历史验收样本。
- 主键约束只防止当前表内同时存在同一个 ID；删除后没有永久 ID 墓碑。随机生成是“不主动复用”的实现，不能解释成跨全部历史的数学保证。

后续若单独决定切换生成器，应只影响新配置，并同步调整验收脚本；不能给现存配置补零、截断、改 ID 或重命名 AVD。本文不执行该变更。[C1][C2][E1]

## 2. 端口规则及“全局唯一”的范围

| 项目 | 项目当前规则 |
|---|---|
| Emulator console 端口 | `5554…5682` 的偶数，共 65 个可分配编号；不是 65 台可并发运行承诺 |
| 对应 guest ADB 端口 | console `p` 对应 `p+1`，即 `5555…5683` 的奇数 |
| ADB serial | `emulator-p` 使用 console 端口编号，而不是 `p+1` |
| 宿主 ADB server | 与上述两端口不同的服务；个人验收使用独立 `15037`，不能当成产品必须改动的端口 |

这里的 65 个编号是**本项目当前分配池**，不是 Android 所有版本的能力上限，也不证明资源或 ADB 能承接 65 台并发。最终个人验收使用 Emulator 36.6.11、API 33 ARM64、console `5666` / guest ADB `5667`；实际 SDK 日志警告 `5667` 超过其推荐 ADB 范围 `[5555,5586]`。这是该 SDK 的版本特性提示；这一个高端口组合通过，不代表整个池、所有版本或最大并发已经实测。[C3][E2][E3]

**操作系统端口与 Server 地址空间分开理解。** 不同宿主的 TCP 端口彼此独立，同一数字可以分别监听；Server 因 serial 单值路由而在同一总账中全局避重。同一宿主上的多个 Agent 进程却仍共享本机端口，另开 Agent、使用不同 `agent_id` 或独立 ADB server 端口并不会自动隔离 Emulator 的 console/ADB 端口和 AVD 文件。个人验收的私有 ADB、目录与受管 ID 是测试隔离设置，不是产品创建了新的操作系统网络隔离层。

产品共享端口探测针对 IPv4 loopback，并检查与其冲突的监听；这不是对 IPv6-only 监听的全面探测。a6/a7 清理证据额外检查了私有端口的 IPv4/IPv6 可绑定性，不能把该测试范围倒写成生产 helper 的双栈保证。

为什么要跨宿主统一分配：当前 `Device.serial` 是主键，Hub 也采用 `serial → agent_id` 路由；如果两台 Agent 都上报 `emulator-5554`，只看本机空闲并不能避免 Server 认错地址。因此 Server 在同一总账里预留全局 serial，再下发指定端口；Agent 本机确认这对端口可用，冲突就返回错误，不能偷偷改选后上报成功。[C3][C4]

预留的具体边界：

- 所有 `adb_serial != NULL` 的 Android 配置都参与占用统计，**包括停止和离线的配置**；65 是跨这一总账的保留槽位总量，不是每台宿主各 65 台。
- 正常重新启动优先保留该配置原 serial；若旧数据已经存在重复 serial，服务端可能另分配槽位，不能保证这类异常存量的地址不变。
- PostgreSQL 使用事务级 advisory lock，使分配到提交串行；SQLite 分支是单进程、同 event loop 的锁，不能宣称跨 SQLite 多进程也有同等保证。
- 数据表只有普通 serial 索引，没有唯一约束；升级不会自动修复已有重复记录。
- “全局”仅指同一 Server 数据库及正常分配路径，不涵盖其他部署、手工启动的模拟器、绕过 Server 的本地调用或删除后仍离线的旧实例。Agent 在没有 `assigned_port` 的兼容调用中仍保留本机选端口逻辑。[C3][C4]

因此升级前必须检查当前总账的重复 serial，不能只因新分配器有锁就宣称旧存量已全局唯一；也不能随意换掉正在运行的旧实例端口来掩盖冲突。

## 3. 停止、删除、离线和复用不是同一件事

| 操作/事实 | Server 记录 | 宿主实例 | 后续行为 |
|---|---|---|---|
| 正常停止成功 | 保留 VM ID、Agent/host 归属、`adb_serial` | 进程退出，双端口可按 Emulator 规则重新绑定；AVD 和用户数据保留 | 显式启动原 AVD |
| ADB 暂时缺席 | 保留归属与地址；不能仅据此证明 stopped | 保留 runtime，核验进程与端口 | 通道未知不会触发自动重建 |
| Agent 离线 | 已绑定配置保留归属与 serial；未完成操作结束为失败 | 不能从 WebSocket 判断 VM 是否退出 | 重连核实事实，不自动重试原启动 |
| 删除配置 | 总账、相关设备记录和别名先删除；发送一次清理请求 | 在线尝试停止、删除该 VM 私有 AVD；离线时无法立即完成 | 下一次对账发现总账无此 ID，再清理残留 |
| 换 Agent | 原配置退出总账，创建新配置、新 ID，再对新目标执行一次首次启动 | 旧宿主收到一次删除；旧宿主离线时残留可能暂存 | 不迁移旧 AVD 或 App 数据，不自动续跑失败的新绑定 |

**最终版本允许删除/换机的账面完成先于离线宿主清理。** 这是本轮后期确定的产品契约；不能沿用早期“旧宿主确认删除后才能生成新配置”的讨论来解释当前代码。[C5]

普通 stop 保留 AVD 不代表所有启动参数都保证数据保留：`wipe_data=True` 本来就允许启动时擦除，snapshot 策略也有不同加载/保存含义。历史 E1 仅使用 `wipe_data=False`、`snapshot_policy=no_snapshot`，不能用该历史样本代替其他策略的验收。[C2][E1]

最终版保持四种策略及原参数不变；上段 `E1` 是历史 `no_snapshot` 样本，最终覆盖见第 8 节：

| `snapshot_policy` | 启动参数 | 实际含义及边界 |
|---|---|---|
| `save` | 不增加禁用快照参数 | 允许加载兼容快照并在退出时保存状态；必须验证以后真的恢复，文件存在或停止成功不够 |
| `discard_changes` | `-no-snapshot-save` | 仍允许加载旧快照，但不把本次运行保存回去；再次加载旧快照可回到旧状态，不是普通持久盘写入保证 |
| `cold_boot` | `-no-snapshot-load` | 本次不恢复旧 RAM/内核快照，仍允许退出保存；冷启动不是擦除用户数据 |
| `no_snapshot` | `-no-snapshot` | 本次不加载、也不保存快照；停止后使用原持久数据盘重新启动，不承诺 App 尚未提交的内存内容落盘 |

这些模式都独立于 `wipe_data`。镜像包、硬件配置或 SDK 发生真实变化时，原快照可能失效；本次修正避免的是不必要的容量倒退，不绕过 SDK 兼容检查。

### 删除后端口重用如何防止串台

例：旧 VM A 使用 `emulator-5666`，删除总账后新 VM B 再取得这个 serial。

1. 新 B 的 VM ID 和 AVD 名不同；serial 相同不等于身份相同。
2. 新 Agent 必须检查本机 `5666/5667`。如果 A 仍占用同一宿主的端口，B 启动明确失败，不改选地址绕过总账。
3. 旧 A 在另一宿主仍运行时，其带 `vm_instance_id=A` 的设备快照不能进入 B 的路由；Server 要求记录存在、归属/host 匹配、状态 running、serial 一致。
4. 旧 A 重连上报后，总账没有 A，Server 向报告者清理 A；Agent 通过 AVD 名和受管进程确认对象，不按旧 serial 直接关闭 B。
5. 删除路由时再检查当前 VM ID 和 Agent，避免迟到的 A 清理把已经属于 B 的路由摘掉；迟到状态另受 request ID/观察代次约束。[C2][C5][C6]

这些措施保护的是**按正常协议上报、带 VM 身份的受管实例**。它们不是全网端口防火墙，也不是已存重复 serial 的自动迁移方案；Hub 底层仍是按 serial 的单值映射。[C4][C6]

## 4. 旧实例首次升级后的实际认领顺序

1. 新 Agent 使用原运行用户、原 AVD 环境和原 Server 地址启动；从 `storage_dir/agent-host-id` 读取安装身份。首次升级尚无该文件时生成一次，以后必须保留。
2. hello 前 Android Manager 执行 `adb devices`，只检查 emulator serial；调用 `emu avd name` 取得实际 AVD 名，反解已有 VM ID。它不会为了认领而创建、重命名或启动 AVD。
3. 已 boot 完成的实例先建立本机 runtime，再上报 `reason=reclaimed`、原 VM ID、真实 AVD 名和 serial；新进程认领时可以没有原 `Popen` 句柄。
4. Server 核对旧归属；通过后更新当前会话 Agent，并记住 `owner_host_id`。随后设备快照才能进入可用设备池。
5. Agent 再枚举 AVD 定义、受管残留目录/索引与进程，提交运行、停止、未知及清单是否完整；停止实例的升级证明只取成功的 `avdmanager list` 实际名称，不用猜测的目录名替代。[C2][C7][C8]

| 旧实例情况 | `74f209d` 的处理 | 必须守住的边界 |
|---|---|---|
| 旧 Agent ID 相同，host 未冲突 | 继续原归属，恢复事实状态 | 不改 VM ID/AVD 名 |
| 新 Agent ID、已有相同 `owner_host_id`，旧 Agent 已离线 | 接受为同一安装的新会话 | 不重建实例，不更换数据 |
| 历史记录没有 `owner_host_id`，运行实例上报精确 AVD 名；已存 serial 若存在也要一致 | 旧 Agent 离线后允许一次兼容认领，随后写入 host 归属 | 只是迁移兼容桥，不是硬件认证 |
| 历史记录没有 host，实例已停止 | 完整清单中有精确 `instance_proofs[vm_id].avd_name` 时认领 | 缺证明或查询失败不能猜归属 |
| 旧 Agent 仍在线 | 不因另一会话先报告同名实例就抢占 | 不能为升级方便绕过旧连接归属保护 |
| 已有 host 归属，另一不同 host 报告同 ID | 旧版曾清理所谓异地副本；`d7cbac0` 已取消这一危险判断 | 最终保留原归属，不自动接管或删实例；安装身份不同不能证明有两份磁盘副本 |
| 总账有配置，但完整清单确实没有原实例 | 标记 `instance_missing` | 不自动重建、不擦数据、不改 ID |
| ADB/AVD 枚举失败或进程状态未知 | 不把失败当空清单或停止证据 | 保留记录与资源责任 |
| 总账没有此受管 VM ID | 向报告者下发一次该 ID 的清理 | 这会实际删除实例，不是只读对账 |

补充两个老版本差异：

- 基线 stop/Server 重启可能已经把 `adb_serial` 清空。已停止的旧 AVD 仍应保留；下一次启动重新分配端口是地址变化，不是重新创建机器。
- 已有旧端口与实际运行 serial 不一致时，不能伪造匹配或改 VM ID解决。运行回报会被拒绝或报 mismatch，需要先核对旧总账和实际实例。

认领与启动是两条不同路径：**升级不能把一个已有 AVD 当“首次绑定”去 dispatch。** 新首次绑定采用 create-only，发现同名旧 AVD 或已追踪 runtime 就拒绝，既不改配置，也不启动/删除旧数据；只有证明由本次调用新建的实例才允许失败回滚。`created_in_attempt=False` 会继续阻止 Server 在后续对账中把这份旧数据当本次残留删除。[C2][C8]

**升级本身不要求重建、重新编号、改 AVD 名或把所有实例改成 6 GiB。** 最终已有实例镜像/容量检查只核验并保护原资源；旧 SDK 元数据缺失、自定义 AVD 实际目录与当前受管写入路径不一致、或已有容量无法确认时会明确失败，不能用自动重建“修好”。API34 请求指向实际 API33 的实例时拒绝，恢复原 API33 后仍能启动同一份 AVD，已由 a6-a3 的真实反例和恢复步骤验证。[E2]

## 5. 升级时必须原样保留的内容

| 必须保留 | 原因 |
|---|---|
| 原 Server 地址、租户/数据库环境及对应认证配置 | 新接一个空总账会把旧受管 AVD 全部判作孤儿并下发删除 |
| VM 配置行、VM ID、原 AVD 名和私有数据 | 对账靠这组持久身份；不能靠删除重建完成“升级” |
| `storage_dir` 与 `agent-host-id` | 这是安装身份；不是由硬件自动推导的指纹，丢失或换目录会改变归属判断 |
| 运行用户、`ANDROID_AVD_HOME`、SDK/AVD 环境 | 同一名字换了目录可能指向不同数据；当前发现和文件清理不负责自动迁移旧目录 |
| 当前在跑/已停止清单及实际启动器配置 | Agent 服务恢复不代表原 VM 都应自动启动；也不能通过启动所有配置来“修复”缺失 |

身份文件不能复制到另一台宿主冒充同一个安装；文件内容损坏时当前实现报错，不静默换一个 ID。**`74f209d` 曾在身份变化时触发副本清理；`d7cbac0` 已修复为明确冲突、不接管也不删数据。** 仍须保存原身份才能正常接续使用，而不能把拒绝危险操作当作目录迁移机制。[C7][C8]

## 6. Agent 重启与 VM 常驻的实际限制

当前 Android 启动仍是直接 `subprocess.Popen(emulator …)`，日志写入实例目录。没有在本模块新建独立 launchd/systemd/Windows 服务，也没有添加 `start_new_session` 或 Windows 独立进程组。Agent 主入口退出时清理镜像服务，没有调用 Android Manager 的 `stop_all()`。[C2][C9]

因此可承诺的是“重新认领还活着的实例”，不能据此承诺所有场景下实例一定活着：

| 场景 | 当前证据/能力 | 不能扩大成的承诺 |
|---|---|---|
| WebSocket 断开再连接，Agent OS 进程仍在 | 个人原生验收通过，Emulator PID 不变 | 不是 Agent 被 SIGKILL 后的测试 |
| 新建 Manager/WS 客户端，Agent ID 改变，沿用 host 文件 | 个人原生验收通过，从无内存 runtime 恢复到原 PID | Python OS 进程刻意没有退出，不是服务管理器重启测试 |
| 真正停止/重启 Agent 进程、关闭终端或 SSH 会话 | 结果受父进程、进程组、登录会话和服务管理器策略影响 | 无法仅从 Popen 代码断言所有 Mac/Windows/Linux 都存活 |
| 宿主重启 | 进程必然终止；AVD 文件可以继续存在 | 本实现没有自动恢复所有 VM 到运行中的保证 |
| 既有 VM 由独立 launchd job 托管 | 新 Manager 可以通过 AVD 名认领这种无原 Popen 句柄的实例 | Manager 的 stop/delete 没有卸载或修改外部 job；若外部 KeepAlive 自动拉起，仍可能与停止要求冲突 |

历史维护记录曾使用独立 launchd job 保持 VM，并记录过 Agent/LaunchAgent 恢复而 VM 未恢复的情况。这些是**旧记录的风险线索，可能过时，本轮未重新检查远端宿主**；不应把旧宿主的做法直接套到所有电脑。[H1]

交接时应分别记录“Agent 怎么常驻”和“VM 由谁常驻”。如果旧机器已经有独立 VM job，不能为了本次升级擅自改服务名、KeepAlive、父进程归属或卸载 job；也不能把暂时的 SSH `nohup` 启动当成已建立长期常驻策略。

## 7. 真机 USB/TCP 与端上执行边界

- Android 扫描仍通过 ADB 识别 USB serial 和 TCP 地址；VM Manager 只为符合 emulator 地址且已纳管的实例加 VM 标签。普通 USB/TCP 设备原样通过，未纳管 emulator 不发布为平台 VM。
- 新 Server 生命周期过滤只作用于带 `vm_instance_id` 的受管实例；普通真机没有因此被要求绑定 VM 配置。生命周期清理使用精确受管 AVD 名和对象，不发送全局 `adb kill-server` 或“停止所有设备”。
- ADB daemon 仍是宿主共享基础设施。不能为单 VM 故障整体重启它；SDK 自己调用 ADB 的方式也不等同项目的所有私有 ADB 配置都已覆盖。
- **端上缓存缺口已在 `d7cbac0` 修复。** 同 serial 的不同 VM ID，或同一 VM 新生命周期，都会更换 driver、输入法和元数据缓存；迟到构造与旧保活标记也不能跨代生效。独立可控时序和实际 AndroidDriver 类复验通过；普通 USB/TCP 真机保留原行为，不把隔离测试写成真机业务验收。
- 本轮个人原生验收没有控制真机，不能写成“USB/TCP 真机已完成端到端零回归”。[C6][C10][E1]

## 8. 已有原生证据覆盖到哪里

### 最终 `2dc9844`：a6-a3 快照与镜像保护

`android-snapshot-a6-2dc9844f-20261016-a3`，VM `49cc71b3076c`，98.596 秒，独立核验通过：

- 六代真实启动；13 次关联到独立 request ID 的生命周期操作中，12 次成功，1 次镜像不符按要求失败。
- `save` 恢复原 boot ID 和标记 A 的 PID、starttick、完整命令签名；证明实际加载 RAM 状态，不仅是快照文件存在。
- `discard_changes` 仍恢复 A，不恢复后加入的 B；B 的数字 PID 被其他进程复用也没有误算成 B 恢复。
- `cold_boot` 产生新内核 boot ID，旧 A/B 签名不在；恢复正确镜像后的冷启动也成立。
- 对已存在的 API33 AVD 改请求为本机确实已安装的 API34，操作明确失败，原生启动次数增加 0，原 AVD 的 33 个文件内容及元数据不变；恢复 API33 后原实例启动成功。
- 最终删除后，Server 总账、AVD/index、六个 Emulator 进程、私有 ADB 和私有端口均核实清空。封存的 a6-a2 失败盘未改。

证据：[a6-a3 结果](native-followup/android-snapshot-a6-a3-results.json)、[原始记录](native-followup/android-snapshot-a6-a3.evidence.json)、[root 独立验收](native-followup/android-a6-root-review.json)。[E2]

### 最终 `2dc9844`：a7 原页面与安装全链路

`android-app-install-2dc9844f-20261016-a7`，VM `810137b52abc`，74.337 秒，原生检查及 root 三张原图独立验收通过：

- 真实上传包 → 安装任务 → Agent handler → 原生安装各走一次；原页面依次显示空记录 count 0、确认随机标记 count 1、停止重启后同一标记 count 1。
- 原页面 localStorage 回读和独立已同步文件分别保留；重启分支没有补写 witness，页面最终确实绘制出来。
- WS 重连及同安装的 Manager/Agent 会话重建保持原 VM ID 与 Emulator PID；这不等于测试了 Agent OS 进程崩溃。
- 四次生命周期操作均取得相应成功终态；两次正常停止都有精确的本次 nonce 回执，没有 Agent TERM/KILL 后备信号。
- 最终删除后的总账、AVD/index、Emulator/私有 ADB、端口和默认受保护元数据逐项确认。此轮使用 `no_snapshot`，与 a6 的其他三种快照策略互补。

证据：[a7 结果](native-followup/android-app-install-a7-results.json)、[原始记录](native-followup/android-app-install-a7.evidence.json)、[root 独立三图验收](native-followup/android-a7-root-review.json)。两轮均仅证明冻结版本在这台个人 SDK/宿主的限定场景，不是公司部署、VLM 业务、外网、真机或任意 App 内存持久性验收。[E3]

### SDK 退出码与快照成功必须分别判断

a6-a2 的两个准确 PID 崩溃报告、对应二进制指令和调用栈已证实：**此现场 macOS ARM64 headless SDK 的快照退出分支主动调用 `abort()`，产生 `-6`**，不是 Agent 在这两次停止中发了 TERM/KILL。不能推成“所有 `-6` 都正常”，也没有新增 `-6` 成功白名单；停止仍看准确进程退出与资源释放，退出码保留在证据中。停止成功、快照保存日志及文件存在都不能替代实际恢复；最终 a6-a3 已以 boot ID 和完整进程签名验证加载成功。[SDK 退出与因果审查](native-followup/android-a6-a2-independent-cause-review.md)

### 历史 `74f209d` / E1 样本

以下 `android-frozen-20261016-4cdbe5` 结果保留为历史记录，不计入最终 a6/a7 的通过范围：

- 配置创建 → 探查 → 首次启动 → 原生 boot/package/screenshot → 自建 APK 页面输入和回执通过。
- WS 重连、Manager/客户端重建、同安装更换 Agent 会话 ID后，旧 Emulator PID 与 VM ID 保留。
- stop 后 AVD、绑定保留，双端口可复用；显式 start 后页面数据仍在。
- delete 后 Server 记录、私有 AVD、受管进程和私有 ADB 均清空，保存的默认实例元数据未变化。
- 实际停止瞬间两端口旧 plain-bind 返回 macOS errno 48，而与 Emulator 一致的 reuse-bind 成功；当前停止确认因此采用 POSIX `SO_REUSEADDR` / Windows 独占地址语义，仍检查精确进程退出和实际端口可用。

该历史样本范围只有个人隔离环境的合成原生控制链；未覆盖公司部署、VLM 业务 Case、真机控制、OS Agent 进程崩溃及全部快照策略。最终 a6/a7 的新增覆盖见上文。

**外网也未验收。** 私有 ADB 运行方式下保存过 guest `wlan0 NO-CARRIER`、`eth0 DOWN`、没有默认网络及 `10.0.2.2` 不通的现场；最终页面回执通过精确 serial 的 `adb reverse` 完成。不能把该局部隔离环境现象归因于公司 Android 网络，也不能把回执通过理解成 guest 外部联网正常。[E1]

## 9. 交接前仍需明确的范围

以下区分最终仍保留的边界和交接补查已修复项：

1. 新配置默认 32 位 ID 尚未落地；永久历史不复用也没有单独机制。
2. 现存重复 serial、超过 65 个保留槽位、SQLite 多进程部署不在当前全局预留保证内。
3. 旧实例首次 host 认领是基于名称证据的兼容规则；存储路径/host-id 丢失与复制必须在升级操作层防止。
4. Agent/VM 的真实服务管理器存活、宿主重启后的恢复、旧 per-VM launchd job 与 stop 的关系，须按实际宿主验证。
5. Android 缓存换代已修复并通过隔离并发复验；USB/TCP 真机业务及原生跨 VM 复用端口仍没有本轮现场验收证据。
6. **`74f209d` 的普通 `/start` 存在归属竞态缺口。** 路由先读取 `assigned_agent_id`，之后才在 `_send_start` 取得 VM 事务锁；如果期间停止实例被重连的新 Agent 会话认领，锁内发现归属与旧参数不同会进入 `_switch_agent`，把普通启动解释成删旧、新建。升级不能依赖这条异常路径保持 ID。`d7cbac0` 已为普通 start 增加 `allow_switch=False`，归属变化时明确返回 409；三端竞态回归已通过。[C5]
7. **另两项升级保护已在 `d7cbac0` 完成。** 只报告归属冲突，不凭不同 host-id 删除总账仍存在的实例；缺少 `inventory_complete` 不再默认为完整。三端正反例通过，详细证据见独立升级审查；不把最终行为倒写为 `74f209d` 当时已具备。[C8]
8. **镜像包核验和容量不倒退已在最终版完成，a6-a3/a7 已原生通过。** 不据此批量重建旧实例、调整到统一 6 GiB、强行加载不兼容快照，或宣称历史 a2/a4 的唯一原因已确定。

## 证据索引

以下 C1–C10 为初始 `74f209d` 源码行号；最终修正以本文链接的独立审查、`2dc9844` 冻结清单和 E2/E3 为准。

| 标记 | 源码/已存证据 |
|---|---|
| C1 | `backend/ai_phone/server/models.py:30-32,135-172`；基线与目标提交的默认 ID 函数均为 12 位，Device 主键为 serial |
| C2 | `backend/ai_phone/agent/android_vm/manager.py`：`start_sync/_ensure_avd`、`_stop_sync_locked/_runtime_exit_confirmed`、`reconcile_running_vms_sync`、`_safe_avd_name`；重点行 `490-610,684-785,934-1040,1411-1427` |
| C3 | `backend/ai_phone/server/android_vm/service.py:41-107`；`backend/ai_phone/server/vm_lifecycle.py:24-55`；`manager.py:1237-1261,1510-1518` |
| C4 | `backend/ai_phone/server/models.py:39-43,143-150`；`backend/ai_phone/server/hub.py:204-233` |
| C5 | `backend/ai_phone/server/android_vm/api.py:640-671,725-849`；`service.py:217-246,427-437`；`vm_lifecycle.py:253-283,429-452` |
| C6 | `backend/ai_phone/server/ws/agent_ws.py:614-687`；`backend/ai_phone/server/vm_lifecycle.py:174-198,264-283` |
| C7 | `backend/ai_phone/agent/identity.py:9-39`；`main.py:3038-3105,3335-3409`；`ws_client.py:214-275,627-630` |
| C8 | `backend/ai_phone/server/vm_lifecycle.py:117-139,286-331,455-503,582-592`；`android_vm/service.py:302-438`；`backend/tests/test_vm_lifecycle_contract.py:208-248,590-607` |
| C9 | `backend/ai_phone/agent/android_vm/manager.py:443` 的 Popen；`backend/ai_phone/agent/main.py:3498-3519` 的退出路径；仓库中没有本次新增的 Android per-VM launchd/systemd job |
| C10 | `backend/ai_phone/agent/android_vm/manager.py:123-141`；`agent/drivers/android.py:771-819`；`agent/main.py:232-308` 的 VM 标签记录与通用缓存 |
| E1 | [Android 历次结果](../execution/android-live-results.json)、[冻结证据索引](../execution/native-evidence/android/evidence-index.json)、[验收脚本](../execution/personal_e2e.py) 的 `755-763,787-858`；原始 `/Users/dongxin/.codex/personal-vm-e2e/android-frozen-20261016-4cdbe5/evidence.json` |
| E2 | 最终 `2dc9844`：[a6-a3 结果](native-followup/android-snapshot-a6-a3-results.json)、[原生日志](native-followup/android-snapshot-a6-a3.emulator.log)、[清理记录](native-followup/android-snapshot-a6-a3.cleanup.json)、[root 独立验收](native-followup/android-a6-root-review.json) |
| E3 | 最终 `2dc9844`：[a7 结果](native-followup/android-app-install-a7-results.json)、[清理记录](native-followup/android-app-install-a7.cleanup.json)、[root 原图独立验收](native-followup/android-a7-root-review.json) |
| H1 | 历史记忆摘要 `MEMORY.md:2852-2853,2881` 与 rollout `019ec146-08ac-7253-85e1-ad782e5b8ac5` 的“post-reboot”段；仅作为历史线索，不是当前远端状态证明 |

历史核对方式：比较 `452e7d7` 与 `74f209d` 的目标文件、逐段读取函数及已存验收材料；抽取的 8 个关键源码文件在 `74f209d` 的 Git blob SHA256 与当时验收 manifest 一致。最终更新另核对 `2dc9844` 的统一回归清单、当前 ID 默认值、a6-a3/a7 原始和独立验收结果、SDK 实际端口警告及容量/退出审查。文档证据链接已检查；本次没有修改原证据、运行设备命令、查询远端现状或部署。
