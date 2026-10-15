# iOS 模拟器身份、端口与升级交接

核对日期：2026-10-16。基础比较范围：`452e7d7` → `74f209d`，产品修复提交为 `0e9bde8`；交接中发现的升级兼容补丁另列于第 6 节，iOS 相关产品提交 `9034204` 已包含 `d7cbac0` 的该补丁（整体交付为 `2dc9844`，后续增量涉及 Android 停止预算、同步屏障、镜像核验和已有数据容量保护），不能只把 `74f209d` 当作交接完成版。本次交接仅核对源码、已有隔离测试和已保存的个人原生证据；没有新建、启停或查询真实设备，也没有访问公司环境。

**正常实例不需要重新创建、改 UDID 或重新分配端口。升级必须先让 Server 具备新的对账规则，再重启/升级 Agent；保留原模拟器数据、端口预留和相同的 Agent 存储目录。** 新增的宿主身份文件首次生成后也必须保留。三种对象不能混成一个：VM ID 是后台配置身份，UDID 是原生实例身份，端口是宿主上的控制地址。

## 1. 哪些值必须唯一，唯一到哪里

| 值 | 谁产生、有什么用途 | 唯一范围与限制 | 正常启停和升级 |
|---|---|---|---|
| VM ID | Server 创建配置时生成；现有默认仍是 UUID 的前 12 个十六进制字符，数据库主键 | 当前 Server 配置的逻辑身份；程序不主动复用删除的 ID。代码没有历史 ID 墓碑表，因此不把随机生成描述为数学上绝对永不碰撞 | 不变；换 Agent 或复制配置生成新 ID |
| 原生名字 `aiphone_sim_<VM ID>` | Agent 根据 VM ID 计算；重启后从名字识别本平台实例 | 必须在受管实例清单中唯一；底层允许出现重名时，Manager 明确拒绝猜测目标。它不同于页面可修改的名称/别名 | 不变；标准 12 字符历史 VM ID 的名字完全兼容 |
| UDID | `simctl create` 生成；simctl 操作的精确目标，也是通用设备层的 `serial` | 每个原生实例的 UUID 身份；普通新建不主动复用。当前 Server 以 UDID 路由设备，VM ID 不能直接替代 simctl 的 UDID 参数 | 停止、再次启动、Agent 重启不变；删除原生实例后失效，新建得到新 UDID |
| WDA 端口 | Agent 根据 UDID 选择首选槽位，冲突后选未预留槽位并落盘 | **宿主本地**端口，生产范围 `8300–8399`；Server 不分配 iOS 全局端口租约 | 停止仍保留；实例确认删除后才释放预留 |
| MJPEG 端口 | 与 WDA 同一个槽位配对 | **宿主本地**端口，生产范围 `9300–9399`，配对关系为 `MJPEG = WDA + 1000` | 与 WDA 一起保留、一起释放 |
| Agent ID | `stable_agent_id()` 生成主机名/名称加随机后缀；标识运行中的连接会话 | 同一进程稳定，进程重启可以变化；不是 VM ID，也不是永久宿主 ID | 允许变化，不能因此要求重建 VM |
| host ID | 新增的 `storage_dir/agent-host-id`，由本机首次生成 UUID | 标识这份 Agent 安装，不是硬件序列号；不能复制到另一台电脑冒充原宿主 | 相同安装升级、重启必须保持 |

来源：[iOS 模型](../../../../backend/ai_phone/server/ios_sim/models.py) 第 31、55、69–75 行；[实例名字与身份检查](../../../../backend/ai_phone/agent/ios_sim/manager.py) 的 `managed_sim_name`、`_owned_target`；[端口分配](../../../../backend/ai_phone/agent/drivers/ios_simulator_wda.py) 第 69–88、217–250 行；[会话 ID](../../../../backend/ai_phone/agent/ws_client.py) 第 627 行；[持久宿主身份](../../../../backend/ai_phone/agent/identity.py) 第 9–40 行。

**不同电脑上的两个 Agent 可以同时使用 8300/9300。** 地址实际是各自宿主的 `127.0.0.1:端口`，不在同一个监听空间。

**同一电脑上的两个 Agent 不能仅靠不同 Agent ID 隔离这些资源。** 本地监听端口、默认 CoreSimulator 设备集合仍共享；每个进程的锁也不能代替跨进程协调。生产升级不要同时运行新旧 Agent。个人验收使用独立 device set、独立 storage 和私有端口段，这是测试隔离方案，不是已经提供了同宿主多 Agent 产品能力。

100 个槽位代表最多 100 对持久预留，不代表可以同时运行 100 台；已停止的实例也占预留。有效旧预留不能因扫描失败或 runtime 暂时不可用而回收。

## 2. 每个操作到底改变什么

| 操作 | Server 总账 | 宿主上的原生实例与数据 | 身份和端口 |
|---|---|---|---|
| 创建配置 | 新建未绑定配置 | 尚不创建 Simulator | 只有 VM ID，没有 UDID/端口 |
| 首次绑定成功 | 本次启动成功 ACK 才确认 Agent 归属 | 创建并启动实例，WDA 就绪 | 记录原生名字、UDID 和本机端口 |
| 首次绑定失败 | 保留失败配置，不确认绑定 | **仅本次实际新建的实例**做一次停止、精确删除；本来就在磁盘上的实例不能因 Server 误带首次绑定标记而被删除 | 终态携带 `created_in_attempt`、`instance_deleted`、`cleanup_confirmed`；清理失败不自动重试 |
| 停止成功 | 保留配置、归属和 UDID，状态 stopped；展示用 WDA/MJPEG 字段可清空 | 确认 Shutdown，保留 App、数据、原生实例 | `ios_sim_ports.json` 的预留仍在，不能把页面端口空白理解成槽位已释放 |
| 再次启动 | 使用原归属、原实例 | 启动原 UDID；找不到原实例或配置不一致就报错，不偷偷重建 | 使用原预留端口 |
| 删除 | 在允许的状态下立即删除总账、别名及该 VM 设备路由，再尽力通知 Agent 一次 | Agent 在线时精确删除；离线时本地实例可能暂时保留，等原 Agent 连接对账清理 | Server 404 证明总账已删，**不单独证明宿主已清理**；端口只在原生删除确认后归还 |
| 换 Agent | 原配置退出总账，建立一份新 ID 的配置，再尝试新绑定 | 新 Agent 创建空白实例，不迁移旧数据；旧宿主离线时可留下待重连处理的旧副本 | 新 VM ID、新原生名字、新 UDID；跨宿主端口数字可以相同，也可以不同 |
| 普通 Agent 重启 | 同宿主的新会话重新核对并接管已有记录 | 不应重建已有原生实例；WDA 是否仍可用单独判断 | VM ID、原生名字、UDID、持久端口保留；Agent 会话 ID 可以变化 |

这里采用最终用户约定：**删除/换 Agent 不等旧离线宿主确认后才删账**。不要再套用早期草案中的“待清理记录一直留在正常列表、先等旧宿主删完才能新建”的规则。

来源：[删除与换 Agent](../../../../backend/ai_phone/server/ios_sim/api.py) 的 `_retire_configuration`、`_switch_agent`；[启动消息字段](../../../../backend/ai_phone/server/ios_sim/api.py) 第 408–415 行；[原生停止与删除](../../../../backend/ai_phone/agent/ios_sim/manager.py) 的 `_stop_sync_locked`、`_delete_sync_locked`；[Server 状态落库](../../../../backend/ai_phone/server/ios_sim/service.py) 第 293–330 行。

## 3. 升级时必须保留什么

| 内容 | 保留理由 | 本次是否改格式/要求重建 |
|---|---|---|
| Server 的 `ios_sim_vm_instances` 记录及 `runtime_state` | 保存 VM ID、原 UDID、归属、后续 `owner_host_id` 与操作结果 | iOS 模型和迁移文件未变化；新增信息写入原有 JSON，不需要删除表或重新导入配置 |
| 当前运行账号的 CoreSimulator 设备集合 | 保存 `aiphone_sim_<id>`、UDID、已装 App 和私有数据；默认集合位于 `~/Library/Developer/CoreSimulator/Devices` | 不搬集合、不擦除数据、不重建实例 |
| `AI_PHONE_STORAGE_DIR` 指向的实际目录及 `ios_sim_ports.json` | 端口预留格式仍为 `{UDID: WDA端口}`；碰撞后的槽位依赖这张表，删掉后重算可能换号 | 有效旧文件直接读取，不做格式迁移；新增校验会拒绝重复、越界或损坏的预留 |
| `storage_dir/agent-host-id` | 新版用它认定是原安装的新连接 | 旧版没有时，新版首次自动生成；生成后应备份、原样保留，不能跨电脑复制 |
| 原 `.env`、启动脚本/LaunchAgent、工作目录与账号 | `storage_dir` 默认是相对路径 `./data`；换目录会悄悄读到另一份存储，造成端口表与 host ID 丢失 | 不因切换代码目录而切换实际 storage；可先明确原路径再安排升级 |
| Xcode/runtime 安装、`wda_project_dir`、WDA bundle 配置及已验证缓存 | 保持已有机型/系统兼容；避免将代码升级同时变成工具链升级 | 本次不要求升级 vendored WDA、Xcode、runtime 或 Python 依赖 |
| `storage_dir/ios_sim_wda_build` | 可重建的 Simulator WDA 产物和每实例 xctestrun；保留可减少冷构建耗时 | 不是 VM 身份台账，但正常升级没有必要删除；不要照搬个人验收清理私有缓存的动作 |
| `storage_dir/ios_sim_runtime` | 当前 Manager 仅创建该目录，没有把它作为独立持久实例注册表 | 不应误以为只备份这个目录就备份了实例身份、数据和端口 |

不要根据“预留表损坏”错误提示直接删除生产端口表试运气；先保留原文件并核对真实占用。新校验拒绝启动，比把两个已存在实例的端口互换更安全。

来源：[配置默认存储路径](../../../../backend/ai_phone/config.py) 第 43–46 行；[端口预留文件](../../../../backend/ai_phone/agent/drivers/ios_simulator_wda.py) 的 `_reservations_path`、`_load_reservations`、`drop_port_reservation`、`default_build_dir`；[身份文件](../../../../backend/ai_phone/agent/identity.py)。在本比较范围内，`ios_sim/models.py`、`migrations/ios_sim_v1.sql`、`config.py`、`requirements.txt`、`pyproject.toml` 无变更。

## 4. 建议的受控升级顺序

这是一份后续执行顺序，本次没有执行以下设备动作。它采用明确维护窗口，不承诺混版本滚动期间无感。

1. **保留对照表和备份。** 记录每台受管 VM 的 VM ID、Agent、原生名字、UDID、端口、原本是否运行；备份总账和上节中的关键文件。
2. **暂停新任务和生命周期操作。** 结束正在执行的任务、镜像/工作台会话、安装以及未结束的启动/停止操作；不要把旧版 starting 状态带进升级。
3. **记录原运行集后，确认受管实例停止，但先不要重启 Agent。** 以精确 UDID 的原生 Shutdown 作为依据，保留原数据。旧版“页面已停止”本身不是充分证据。若准备阶段必须退出 Agent，要保持其停止，不能让守护进程立即以新会话连回旧 Server。
4. **先升级 Server/前端到包含第 6 节兼容补丁的新版本。** 继续暂停生命周期操作；原 Agent 的同进程连接可以重连，但此时不是开放混版本功能使用。iOS 无需重建数据库表。
5. **新 Server 已就绪后，再退出旧 Agent、在原路径更新代码。** 确认旧会话离线，不在同一宿主并行启动新旧 Agent；特别确认本安装自己持有的 iPad xcodebuild 已退出，不能全局杀别人的 Xcode/WDA。
6. **用原账号、原存储和原设备集合启动一个新版 Agent。** 原 `agent-host-id` 已有则沿用；旧版本没有时首次生成后记录下来。不要把旧 Server 回滚到 `452e7d7` 后再接收这个新会话。
7. **先核对旧实例，再恢复使用。** 应看到原 VM ID、原原生名字、原 UDID；新的 Agent 会话 ID 可以被更新，Server 应保存对应 host ID。不要用“换 Agent”按钮修复普通 Agent 升级后的显示问题——那个按钮会创建新机器。
8. **只恢复升级前记录为运行中的实例。** 同一实例再次启动后核对原端口、WDA 身份和实际自动化可用性，再检查 App 数据仍在；随后恢复派单。原本停止的实例继续保持停止。

旧记录尚无 `owner_host_id` 时，接管并非任意上报即成立：旧 Agent 必须离线；运行实例需要同 UDID 的认领证据，已停止实例需要完整清单中的精确原生名字与 UDID。一次接管后会写入 host ID，后续只能由同一安装的新会话认回。

**只回退 Agent 代码也不是无条件安全的回滚。** 新记录已经保存 `owner_host_id` 后，旧 Agent 不发送 host ID、重启又改变会话 ID，新 Server 可能拒绝认领并把 VM 挡在设备池外。回滚需连同版本与状态快照另行核对，不能靠删除身份文件或清空归属字段强行恢复。

数据库快照回滚也要单独核账：若旧快照缺少较新 VM ID，Agent 重连时会按既定孤儿规则收到删除。恢复旧库前必须先隔离对账并核对需要保留的新实例，不能把“恢复旧数据库再重启”当成通用无损回滚方式。

来源：[归属接管规则](../../../../backend/ai_phone/server/vm_lifecycle.py) 第 117–139 行；[旧版停止实例接管](../../../../backend/ai_phone/server/ios_sim/service.py) 第 353–366 行；[相关跨端契约测试](../../../../backend/tests/test_vm_lifecycle_contract.py) 的 `test_stopped_legacy_instance_adopts_verified_restarted_host_once`；[iOS 接管测试](../../../../backend/tests/test_ios_sim_server_api.py) 的 `test_legacy_reclaimed_requires_exact_udid_and_offline_owner`。

## 5. WDA、iPad 与真机影响

| 路径 | 保留的基底 | 本次变化和升级注意点 |
|---|---|---|
| iPhone Simulator | 构建/复用 Simulator WDA，经 simctl install/launch；按本实例名字核验 WDA 身份 | 没有更换 WDA 实现或包版本；新增启动总预算、编译锁等待预算、阶段超时检查。有效旧构建和预留可复用 |
| iPad Simulator | 保留 `xcodebuild test-without-building` 常驻路径 | 显式停止会确认 xcodebuild 退出；正常 Agent 退出仍有原来的 atexit 回收。guest Booted 而 WDA 未就绪时现报 error，不再伪报 stopped；按当前统一规则先明确停止、再启动原实例 |
| iOS 真机 | 原 USB/usbmux、配对/签名、真机 WDA launcher 和镜像实现 | 这些原生驱动文件未改；共享扫描、WS 调度、readiness 和任务/安装占用保护有改动，因此不能声称整个真机链路“完全没有变化”或已做现场验收 |

默认真机 WDA 从 `8100` 递增，Simulator 使用 `8300–8399`；**这不是任何自定义配置下都不会重叠的硬隔离**。真机分配没有固定上界，若原宿主把真机 WDA 起点改到模拟器范围，或者已占用该范围，本次不会自动重映射生产端口。升级前应保留并核对原配置，不能通过删预留或随机换号掩盖冲突。

现有空闲 iOS Simulator WDA 健康恢复入口仍在，新增维护占用保护；它不等于失败绑定/启停操作会自动重试。Agent 重启后 WDA 未被认定可用的实例，也不能仅靠“Simulator 进程在”开放调度。

源码比较确认：`drivers/ios.py`、`drivers/ios_wda_launcher.py`、`drivers/ios_simulator_driver.py`、`drivers/simctl.py`、`mirror/ios_capture_mjpeg.py` 和 vendored WebDriverAgent 在本范围未修改。变化位于 Simulator Manager/WDA launcher，以及三端共用的 main、扫描、WS、readiness/占用保护。参见 [WDA 两条启动路径](../../../../backend/ai_phone/agent/drivers/ios_simulator_wda.py)、[真机端口分配](../../../../backend/ai_phone/agent/drivers/ios.py) 第 337 行、[共享健康恢复入口](../../../../backend/ai_phone/agent/main.py) 第 3285–3327 行。

保留既有已验证 WDA，不等于已证明所有历史 WDA/Xcode/runtime 组合都兼容；外部 WDA 工程和顺带升级工具链需要另行验证。读取不出真实机型时现在明确失败，不再把未知 iPad 猜成 iPhone 路径。

## 6. 升级兼容发现与补丁要求

**Server 必须先升级，不能任意交换先后或回滚旧 Server 接收新会话。** 两个问题的性质不同：

### A. 旧 Server 对新会话的历史误删问题

`452e7d7` 的 `server/ios_sim/service.py` 第 409–420 行只按当前上报的 Agent ID 查询配置并生成 `known_ids`；第 434–446 行虽然把上报的停止实例重新绑定到新会话，却没有同步更新 `known_ids`；第 448–459 行随后仍把这些实例当成孤儿发出删除命令。

触发例子：数据库中旧 Agent ID 拥有一台已停止、数据仍在的 VM；Agent 进程重启后换了会话 ID，上报这台停止实例。旧 Server 能找到配置并改归属，却仍可能下发删除。总控的独立审查已做隔离复现；本交接也重新核对了上述版本源码。

旧 Server 还只允许 `reason=reclaimed` 跨旧会话认领，而新版对“guest Booted、WDA 不可用”上报 `wda_not_ready`；因此 iPad 等重启后 WDA 未就绪的实例也有被旧 Server 拒绝认领、随后误作孤儿的代码路径。

隔离证据：[旧 Server 升级顺序单项复现](upgrade-legacy-server-order.log)；[对应测试](../../../../backend/tests/test_vm_upgrade_independent_review.py) `test_legacy_server_replay_demonstrates_agent_first_upgrade_delete_hazard` 从 `git show 452e7d7` 提取真实旧处理函数，断言“数据库归属已更新，新会话仍收到同一 VM 的删除命令”。该用例的 `1 passed` 表示旧缺陷被成功复现，不代表旧升级顺序安全。

这不是“新版端口需要重建”的问题，而是旧 Server 的对账判定问题。**不能先升级/重启 Agent，让它以新会话连接旧 Server；也不能随意把 Server 降回旧版接收新会话。** 新版查询包含上报 VM ID 和已知 host ID，兼容接管还要求原生身份依据。

### B. 新 Server 接收缺少完整性字段的旧清单

在 `74f209d` 中还发现如下混版本缺口；当前交接工作树已补上最小 Server 修正，本文不修改产品代码：

- 旧 Agent 的 `_list_all_instances()` 在查询失败时返回空表，`report_orphan_reconcile()` 不带 `inventory_complete`。
- 新 Server 的 iOS `handle_vm_reconcile()` 第 376 行使用 `msg.get("inventory_complete", True)`。
- 因此，若旧进程的 Agent ID 仍是记录的归属，已停止实例不在内存运行表里，而重连时 simctl 查询失败，旧 Agent 可能发送空清单；新 Server 会把“没有拿到清单”解释成“原实例缺失”。

当前交接补丁已把 iOS 的“标记停止”和“标记缺失”都收紧为 **`inventory_complete is True`**；字段缺省不代表完整。旧消息可以被解析，但不能因此直接把状态收敛为 stopped 或 instance_missing；升级窗口应等待新版 Agent 的完整清单，再恢复使用。

这是从版本源码确认的兼容触发链；当前新版 Agent 查询失败不上报空清单，所以完整新版组合没有这一条输入。本交接已核对修后的第 369、376 行；Server 负责人报告升级专项与既有 9 个文件回归为 335 passed、2 skipped，最终提交与独立复验统一见下节记录。该追加修复不能反过来写成 `74f209d` 原本已覆盖所有旧 Agent 消息。

### C. 同批升级防线以独立审查收口

总控同时处理的两项跨端保护也适用于 iOS：**总账存在但安装身份冲突时不自动删本地实例；普通 `/start` 遇到同宿主新会话认领时不能误转成显式换 Agent。** 旧存储目录丢失或同机启了第二份 Agent，不能被当成已有两份可独立删除的模拟器。

这两项的历史复现、修后回归及最终提交统一记录在[升级安全独立审查](upgrade-independent-review.md)。升级放行应核对该记录的最终结果，不能只看本文的端口规则或先前的新建实例验收。

另外，默认历史 VM ID 为 12 字符，原生名字兼容；如果有人手工导入过超过 24 字符或带特殊字符的非标准 VM ID，旧版曾截断/替换名字，新版不再这样做，这些实例需要逐个核对原名与 UDID，不应自动改名或重建。

## 7. 已有证据能证明什么

| 证据 | 已支持的结论 | 不能据此扩展的结论 |
|---|---|---|
| [最终个人 iOS 验收摘要](../execution/ios-evidence-summary.json)、[完整记录](../execution/ios-personal-validation.md) | iPhone VM `bc1095238b06`、UDID `57122E96-DA8A-488C-90B5-AA09CE780B22`：真实 HTTP/WS、生产驱动输入、回执和截图；停止/再次启动同 UDID、同端口、数据保留；离线 DELETE 后 Server 404，原 Agent 新会话重连清除实例、目录和预留 | iPad、真机、生产 VLM/Runner、完整实时镜像、公司业务 App 尚无本轮现场通过证据 |
| [原生证据索引](../execution/native-evidence/ios/evidence-index.json) | 该轮使用私有端口 `18309/19309` 和独立 device set，避免碰默认实例；58 条 guarded 命令均在专用集合。此次在兼容补丁前只读核对 16 个记录中的源码哈希，与 `74f209d` 工作树全部一致 | 私有端口的成功不能证明所有生产宿主的 `8300–8399` 都无外部占用；专用 iPhone 集合的结果不能替代 iPad xcodebuild 现场验收；后续 Server 兼容补丁依赖单独隔离回归，不能冒称重新跑过这轮原生验收 |
| [最终回归 XML](../execution/final-regression.xml) | 已保存结果为 934 passed、2 skipped；端口跨重启稳定、碰撞不换号、停止保留预留、删除归还、旧记录接管和真机/模拟器隔离都有对应隔离测试 | 不能替代真实宿主升级演练或高频扩缩容压力验收 |

关键回归入口：[端口与 WDA 测试](../../../../backend/tests/test_ios_simulator_wda.py)、[Manager 测试](../../../../backend/tests/test_ios_sim_manager.py)、[iOS Server 测试](../../../../backend/tests/test_ios_sim_server_api.py)、[生命周期契约测试](../../../../backend/tests/test_vm_lifecycle_contract.py)、[身份与设备池测试](../../../../backend/tests/test_vm_identity_health.py)。

本交接没有安排新的原生实验，也没有把此前失败轮次或中间白屏截图当作通过证据；最终通过截图仅取 `ios-20261016-012711-37f48c`。
