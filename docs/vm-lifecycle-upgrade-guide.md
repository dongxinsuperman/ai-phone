# 三端虚拟机改动与升级交接

2026-10-16 审查补修：修正换 Agent 目标掉线后新配置无法再次手动下发，以及 iOS/Harmony 普通停止丢失剩余预算的问题；保留资源预估仅提示的规则。范围和按源码冻结的复验见[两项审查补修](experiments/vm-review-fixes-20261016/README.md)。

2026-10-16 最新增量：页面与 Server 共用生命周期判断，去掉“待恢复”和“重试停止”流程；启动失败与停止失败按本次原生事实显示，并保留原因。增加实例响应 `lifecycle`，不增加表或列、不要求重建存量机器；详见[普通启停与统一状态](experiments/vm-lifecycle-simple-20261016/README.md)。以下旧版验收计数不代表本轮已经再次完成相同实机测试。

2026-10-16 后续增量：下发探查改为先列出在线 Agent，再逐台显示结果，已可用的机器可以先下发；见[逐台探查交接](experiments/vm-progressive-probe-20261016/README.md)。下文 `2dc9844` 与相应测试计数是生命周期主修复的冻结验收记录，新的探查体验与兼容说明以该增量记录为准。

核对日期：2026-10-16。覆盖 Android、iOS Simulator、Harmony 的 Server、Agent、页面、个人 CLI，以及这些改动涉及的执行、安装、观看链路。

**这次不需要新增数据库表或列，现有虚拟机也不应批量删除重建。** 升级应安排维护窗口：先让 Server 具备新对账保护，再用原账号、原配置和原存储目录更新 Agent；页面与 CLI 一并更新后再开放操作。个人 CLI 的修复已本地合入其 main，但没有改全局 CLI、没有发布 npm 或操作公司项目。

本文包括交接补查中新发现并修正的问题，最终产品提交为 **`2dc9844`**（包含升级保护、端口/停止核实、Android 退出窗口、停止前同步、已有镜像一致性及原数据分区容量保留）；不能仅部署第一次的 `74f209d` 就认为已经包含全部升级保护。个人 CLI 对应 `e65dad5`。测试证据与版本范围见第 10 节。

## 1. 过去与现在，用户操作上有什么差别

| 操作 | 过去的风险/实际行为 | 现在的规则 |
| --- | --- | --- |
| 创建配置 | 本来就是 Server 配置，不要求 Agent | 保持；创建不创建宿主实例，不占用运行端口 |
| 第一次下发 | 可能先写绑定，启动失败仍挂着归属 | 目标仅作为本次操作记录；匹配的运行成功回报到达后，才确认绑定 |
| 启动已有机器 | 缺失/查询失败可能被误当成需要新建；交接补查还发现普通启动可能被会话变化误判为换机 | 只启动原实例；缺失就明确失败；普通启动绝不会撤销旧 ID 或进入换机流程 |
| 停止 | 某些平台曾把通道失联、关机命令返回或异常吞掉当成停止成功；Android 退出前缺少 guest 写入同步 | 核对原生退出事实；Android 普通停止先确认一次系统同步；保留配置和原实例，未确认就明确失败 |
| 删除 | 基线已有先删账再清理的路径，但迟到回报、归属及收尾保护不完整 | 总账立即删除，Agent 离线也允许；在线发送一次准确清理，未清掉的残留重连时对账 |
| 换 Agent | 新 ID 规则基线已存在，三端前置条件和人工释放流程不一致 | 先在一个数据库事务中退旧、建新，再向目标启动一次；旧 Agent 离线不阻塞；不搬旧 App 或数据 |
| 下发失败 | 可能留下绑定、继续迟到创建，或者把旧数据当失败残留删除 | 本次操作结束，不自动续跑；只允许回滚本次确实新建的资源，旧实例数据不作为兜底被删 |
| 重连 | 会话变化、旧回报或不完整清单可能覆盖归属与状态 | 安装身份和会话身份分开；核对原生证据、操作编号与期限；不完整清单不能证明实例没了 |
| 两个 Agent 争认领 | 首轮修复仍可能把另一个安装身份报告的实例当副本删除 | 总账还有该 ID 时，不抢归属，也不据此删除磁盘实例；记录身份冲突，保留原归属 |
| 页面 | 跨行忙碌、旧请求回写、错误被轮询抹掉；鸿蒙有人工释放原因弹窗 | 保留原布局；每行独立忙碌、过期响应失效、错误保留；正常流程不再要求人工释放 |

删除 Agent 侧资源，指这台 VM 的原生实例定义及其私有数据；共享 SDK、系统镜像包以及其他实例继续保留。换 Agent 同样是退掉原实例，再在目标新建空实例。

“操作一次”包含原生启动/关机自身所需的有限等待。`starting` 不代表已经成功，也不等于排队等待未来自动恢复。WebSocket 重连和同一次结果补送仍存在，它们不重新执行一次启动。

配置修改也有原生边界：别名可以在运行时修改，但启停中的冲突修改会拒绝；iOS 机型/系统、鸿蒙目录锁定的机型/镜像/屏幕不能通过 PATCH 偷换。鸿蒙已绑定实例的内存、存储和 config_json 也不允许暗中重建后替换。Android 沿用原 PATCH 范围，但启动和已运行实例复用都会核对真实 AVD 的 SDK 镜像包；不匹配或证据缺失就拒绝，保留原盘，不能后台改个值就算换了系统。显式 `system_image` 优先于 API/ABI 等推导字段；系统/profile 的重复标签没有被本次自动统一。历史误配可以恢复成实际镜像再启动，真正换镜像应使用新的空实例。复制只是复制配置并生成新身份，不复制 App 或磁盘数据。

交接补丁恢复了明确发送失败的 HTTP 错误返回。换机可能已经退掉旧账、产生新 ID，但新机没有启动成功；页面和新版 CLI 会保留并展示这个新 ID，便于查询或删除，不会把失败隐藏在旧 ID 下。

## 2. VM ID 与端口：必须分成三层理解

- **VM ID**：后台这条配置的身份，负责总账、操作和对账。
- **原生身份**：Android 的 AVD 名、iOS 的 UDID、鸿蒙的实例名称和路径，负责确认“磁盘上和进程里的究竟是哪台”。
- **通道地址**：ADB、WDA、HDC、FPort 等端口，负责连接；它们可以回收复用，不能代替机器身份。

改别名不会重命名原生实例。正常停止、再次启动和升级保留 VM ID；复制配置和换 Agent 生成新配置 ID。底层工具仍需要自己的 serial/UDID/地址，不能直接把 VM ID 填进所有原生命令。

### 2.1 唯一性到底约束到哪里

| 项目 | Android | iOS Simulator | Harmony |
| --- | --- | --- | --- |
| 平台机器身份 | VM ID；AVD 名 `aiphone_vm_<id>` | VM ID；原生名 `aiphone_sim_<id>` | VM ID；设备层键 `harmony-vm:<id>` |
| 原生连接身份 | `emulator-<console端口>` | simctl UDID | HDC 目标，如 `127.0.0.1:10001` |
| 主要端口 | console 偶数 `5554–5682`；ADB 为对应 `p+1` | WDA `8300–8399`；MJPEG `9300–9399`，一一配对 | HDC `10000–16555`；自动化 FPort `16556–20000` |
| Server 是否全局分配 | 是；同一总账的 Android serial 全局避重 | 否；Server 按 UDID 路由，端口由宿主分配 | HDC 租约表仍以端口作为全局主键；FPort 由宿主分配 |
| 两台不同电脑能否用相同端口数值 | OS 可以，但本项目正常 Android 分配路径不会重复预留同一 serial | 可以；不同电脑各自的 8300/9300 互不占用 | OS 可以，但同一 Server 的有效 HDC 租约仍全局避重；FPort 可在不同电脑重复 |
| 同一电脑两个 Agent 是否天然隔离 | 否，共享本机端口、ADB 和可能相同的 AVD 目录 | 否，共享本机端口和可能相同的 Simulator 集合 | 否，共享本机端口、HDC 和可能相同的实例目录 |

**Android 当前是整套 Server 共 65 个预留槽位，不是每个 Agent 各 65 个。** 停止、离线或失败后仍保留 `adb_serial` 的配置也占总账槽位；未下发的纯配置不占。这个范围是项目现有分配池，不是 Android 所有版本的技术极限。

本次实际 SDK 对测试 ADB 端口 `5667` 提示超出其建议范围 `5555–5586`；显式连接的私有通道实际通过了全部验收，但这不等于任何 SDK/工具都能自动发现全池设备，更不等于已验证 65 台并发。保留分配池是为了避免改动已有 serial 规则，环境升级仍需核对所用 SDK 的连接能力。

鸿蒙 HDC 池有 6556 个数字，仍受本机实际资源、其他监听者及现有租约约束。iOS 每个宿主的默认池有 100 对端口，也不代表该宿主有能力同时运行 100 台模拟器。内存和实例数量目前仍是软提示，没有新增跨平台容量调度器。

Android 的全局避重依赖正常 Server 分配流程，`adb_serial` 没有新增数据库唯一约束；PostgreSQL 使用事务锁，SQLite 的保护限于单进程、同事件循环，不能直接扩大为多个 SQLite worker 的并发保证。升级不会自动清洗历史重复值。这里保留现有表结构，也意味着不能声称已解决所有部署模式的容量与并发问题。

### 2.2 停止后，哪些端口还被保留

| 平台 | 原生进程停止后 | 总账/本机预留 | 下次启动 |
| --- | --- | --- | --- |
| Android | console/ADB 不再被该运行进程监听 | Server 保留该 VM 的 serial 预留；AVD 和数据保留 | 通常继续原端口；旧数据本来存在重复 serial 时不能保证地址不变 |
| iOS | WDA/MJPEG 服务停止 | 本机 `ios_sim_ports.json` 仍保留 UDID 对应槽位，不能仅看页面端口空白就当作已释放 | 使用原 UDID 与保留槽位；确认删除原生实例后才释放预留 |
| Harmony | 关闭实例/driver，清理本次转发 | Server 确认停止后释放当次 HDC 租约；Agent 保留旧地址/token 作核验资料 | 产生新租约，端口可变，VM ID 和实例数据不变 |

Android 的宿主 ADB server 端口是另一层；个人测试用了 `15037`，这不是要求生产都改成 15037。也没有执行全局 `adb kill-server`、HDC 全局重启或删除默认 Simulator 集合来做验收。

### 2.3 删除后复用端口，如何防止认错机器

假设 A 删除后，B 又拿到 `emulator-5666`：B 的 VM ID 和 AVD 名是新的；Agent 要检查本机双端口，A 还占着就明确失败。旧 A 的设备快照、状态和清理回报要核对 A 的 VM ID、归属与操作编号，不能覆盖 B 的路由。清理按准确 AVD/实例身份进行，不按相同端口数字杀进程。

本轮交接还补上 Android **端上缓存**的换代：新 B 不能沿用 A 的输入法就绪标记、屏幕尺寸、机型或 driver。只保护 Server 路由不足以解决这个问题。

本轮原生补验还发现了端口判据的具体缺口：鸿蒙普通 bind 可能把关闭连接留下的状态当成占用；安卓仅做复用 bind，又可能漏掉 macOS 上绑定全部地址的监听者。最终三处检查使用相同的小型函数：普通空闲口一次检查就结束，只有确实遇到地址冲突才增加有限核验；真实监听、未知状态不放行，Windows 保留独占规则。**端口可用只说明可以新建监听，不能单独证明原虚拟机进程已退出。** 鸿蒙停止因此还要单独查准确实例进程，旧清理标记不能代替本次事实。没有新增自动换端口、延长启动预算或失败重试。证据见[端口与停止补查](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/harmony-port-stop-followup.md)。

生产共享检查针对 IPv4 回环及与其冲突的监听，不是全面探测所有 IPv6-only 监听；个人验收额外做了私有端口 IPv4/IPv6 可绑定性检查，不能把验收脚本能力当成产品已经实现的双栈隔离。不同 Agent 也没有因此获得独立的网络命名空间。

这不自动修复历史库里已经存在的重复 serial。升级前需要查旧总账的重复值；不能靠改 VM ID、删除旧机器或随机改端口来掩盖存量冲突。

### 2.4 ID 是否已经“永远不会重复”

当前仍是 `uuid4().hex[:12]` 的随机 ID，数据库主键阻止有效记录同时重号，程序不主动复用删除的 ID；没有历史 ID 墓碑表，不能写成跨全部历史的数学保证。Android 名称现在支持完整 32 位，但 Server 默认生成器没有因此改为 32 位。**不能给已有 ID 补零、截断或重命名原生目录来升级。**

鸿蒙还存在原生 UUID/派生 UDID 的共享设置，用于开发包设备登记等既有场景；多台 guest 原生 UUID 可能相同。平台不会用它当配置主键，不能把共享 UUID 当作同一台 VM，也不能擅自改变它来“修权限”。

## 3. 三端对账统一了什么，哪里仍各自实现

现有实现是：Agent 连接/重连后枚举本机受管实例并报告；Server 对照总账，通过原有 WS 命令要求 Agent 清理。不是 Agent 直接修改 Server 数据库，也不是一个新建的全网扫描系统。

三端统一的是判据，各自仍用 AVD、simctl、鸿蒙 registry/进程来证明原生事实：

| 对账情况 | 最终处理 |
| --- | --- |
| 总账已没有这个受管 VM ID | 有准确目标证明时清理本地残留；包括离线删除后重连、换机后的旧 ID |
| 总账有配置，当前安装身份和原生证据匹配 | 接续原机器和归属，不重建 |
| 总账有配置，但两个安装身份争认领 | 保留原归属，不让新上报者抢走，也不自动删原生实例；记录 `VM ownership conflict; no adoption or cleanup` |
| 完整、可信清单确实没有应有实例 | 标记缺失异常，不自动新建空机代替 |
| 扫描失败、部分清单或旧 Agent 没有声明完整 | 不是“没有实例”的证据，不据此释放资源或判定停止 |
| 失败操作迟到返回成功/旧健康结果 | 不重新确认已失败的订单，不覆盖较新的操作；必要时只接受准确的物理事实或清理本次确实新建的残留 |

身份冲突不能直接删的原因很实际：**同一台电脑运行两份 Agent 时，二者可能看到同一个 AVD/UDID，根本不是两份磁盘副本。** 安装身份不同不能证明有两台物理宿主；直接删报告者看到的实例，会把原 owner 使用的唯一实例也删掉。

这一保护不会取消离线删除例外：用户已经明确删除了总账 ID，就仍然按孤儿对账清理。连接中断不等于实例已停，Server 删除成功也不等于离线磁盘已同步清空。

## 4. 鸿蒙过去说的“权限”，分别是什么

| 项目 | 准确解释 | 本次结论 |
| --- | --- | --- |
| `AI_PHONE_HARMONY_AUTO_ACCEPT_AGREEMENTS` | 历史 `c7dd3ba`（2026-08-21）加入的本机开关；解决 Emulator 非交互启动等待 stdin 协议输入，默认关闭，启用后只写一次 `y` | 最可能是你记得“后来又加的那个东西”；不是本次新增，不是 App 系统授权，Server 不能远程替你开启 |
| 全部 license 都要先接受 | 历史 capability 路径已经不把所有厂商 license 当统一前提 | 不能把本次描述成“取消了所有协议限制”；真实 create/start 仍可能报告具体前提不足 |
| hmdriver2 FPort 隔离补丁 | 现有 `hmdriver2==1.4.4` 相关端口隔离，防止自动化转发抢占 HDC 池 | 是通道补丁，不是权限补丁；本次保留，未承诺任意新依赖版本可用 |
| 共享 UUID/UDID | Guest 开发身份/签名登记相关，和 VM ID 不同 | 本次没有替换共享身份算法，也没有验收公司 HAP 签名 |
| 小艺输入法首次说明、基本/全量模式、键盘布局 | 全新 guest 内应用引导 | 原生测试前显式选基本模式和键盘布局；没有写进产品自动同意流程，新建空机仍可能出现 |
| 换 Agent 要勾选并填写确认原因 | 过去平台的人工释放租约流程，不是鸿蒙系统权限 | 普通页面入口已移除；按退旧建新处理，兼容接口仍在但不允许文字确认代替资源证明 |
| macOS 读取全机端口表报权限不足 | 原来就有 psutil 无权限后使用有限 lsof/本机端口检查的路径 | 本次没有要求额外管理员权限；读取失败不能当成端口空闲，也不把内核权限错误伪装为某台 VM 占用 |
| 真机开发者模式、USB 调试/信任、HAP ABI/签名、宿主 GUI/虚拟化 | 各自的原生环境条件 | 没有声称被本次全部消除；没有新增统一管理员、录屏或辅助功能授权步骤 |

镜像名称还要分清四种版本：系统版本、API 级别、镜像包构建号、Emulator 工具版本。个人鸿蒙实测的 `HarmonyOS 6.0.31(23)` 与 `info.json` 的 `6.1.0.115` 并不矛盾，后者是包构建号。本次归一化了 Windows/macOS 元数据和 ABI 别名，保留 Beta/API/架构差异，不能拿相似字符串强行视为同一镜像。真实 Windows 创建/执行尚未验收。

完整历史提交与七类权限证据见 [鸿蒙基础核对](experiments/vm-lifecycle-audit-20261015/handoff/harmony-foundation-permissions.md)。

## 5. 数据库到底有没有加表、加字段

**没有新增数据库表、列、索引或迁移脚本；原有 SQLAlchemy 模型没有改变。** 前提是当前部署本来已经具备原三端 VM 表；这不是替未执行的历史迁移补表，本轮没有连接公司数据库确认其现状。

但是，不能把“没有加列”写成“没有新增任何状态资料”：

| 位置 | 本次新增/强化的内容 | 是否数据库迁移 |
| --- | --- | --- |
| 原 VM 的 runtime JSON | owner_host_id、当前操作 request_id/action/target/deadline/outcome、当前创建来源与清理证据等 | 否，使用原有 JSON 列 |
| Agent hello/回报 | host_id、操作预算、完整清单/实例证明、观察代次等 | 否，是 WS 消息内容 |
| `storage_dir/agent-host-id` | 这份 Agent 安装的持久 UUID | 否，是必须保留的本地文件 |
| 平台原有状态资料 | 鸿蒙 registry 原子保存；iOS 端口预留核验；Android 受管 AVD/进程核验 | 否，保留原技术路线 |

host_id 是安装身份，不是硬件序列号。首次从旧版升级没有这个文件时会生成一次；之后必须保留，也不能复制到另一台宿主冒充原安装。

## 6. 接口改变多少，外部 CLI 是否必须更新

统计范围是三端 VM 模块：**31 个 HTTP 路径、41 个 method+path 操作、13 个请求模型；最终新增 0、删除 0，请求 schema 结构变化 0。** 两处曾误去掉的 DELETE 路径长度限制已经恢复；基础 VM 消息类型和前端 API 路径未另起一套。

这不等于语义零变化：41 项中，19 项涉及生命周期行为，6 项查询会展示新的事实/状态信息，16 项基本不变；逐个清单见 [API 全表](experiments/vm-lifecycle-audit-20261015/handoff/server-api-schema-upgrade.md)。

| 返回/调用 | 最终含义 |
| --- | --- |
| start/dispatch/stop 正常接受，HTTP 200 | 进入 starting/stopping，不代表原生已完成；首次 assigned_agent_id 可能仍空 |
| 已明确发送失败 | Android/Harmony 409，iOS 502；detail 有 message、sent:false、vm_id、request_id、instance |
| 换机已退旧但新启动失败 | 三端 409；detail 有 switched:true、old_vm_id、old_cleanup_sent 和新实例；极端并发下新实例又被删除时 instance 可为空，但新 vm_id 仍保留 |
| DELETE 返回 200 | 总账删除成功；cleanup_sent=false 表示远端尚未发送/清理，不应因此重新创建旧账或误报总账删除失败 |
| 查询 state=stopped | 原生事实已经停止，不一定代表刚才那次操作按时成功；自动组合操作还需核对其 request_id/outcome |
| force-release | URL 保留，但有实际归属或未确认资源时返回冲突，不能靠 confirmed/reason 绕过 |

你说的“CRI”在本仓库和个人命令行仓库中没有对应的 Container Runtime Interface 实现；这里按实际外部 **CLI + HTTP API** 核对。

个人 `aiphone-cli` 需要同步兼容补丁，已完成独立复核并本地合入个人 main：

1. restart 必须先确认本次停止操作成功，不能因为迟到的物理 stopped 就在失败后继续 start。
2. 同一次等待要关联 request_id，不能把后来另一个操作的成功当成本次成功。
3. sent:false 或明确的 HTTP 409/502 要以失败退出；不带 --wait 也不能把已知失败当接受成功。
4. 换机失败要把新 vm_id 和旧 retired_vm_id 提到错误输出，便于查新配置。
5. DELETE 的 cleanup_sent:false 仍按总账删除成功处理；旧 Server 没有 operation 元数据时保留已有兼容路径。

个人 CLI 修复提交为 `e65dad5`，源码 package 名是 `aiphone-cli`、`private:true`、版本号仍 `0.3.0`；**只看 --version 不能确认是否有补丁。** 公司已安装的 `@guanghe-pub/aiphone-cli`、默认 profile、全局二进制和 npm 发行都未修改。发行方需要按其流程同步并发新版本，不能把个人源码提交写成公司 CLI 已更新。

还有两个保留行为：CLI 默认等待 180 秒，可能短于 Server 的原生启动预算；等候超时不是自动撤销 Server 的当前操作，必须查原 ID，不能重发创建。`list --agent` 仍按已确认归属筛选，首绑尚未成功时可能不在该筛选结果里，应按创建返回的 VM ID 查状态。running、readiness、busy 也是三个不同状态，任务仍需设备真正就绪且未被占用。

代码默认的一次操作预算：停止 120 秒；Android 启动为 boot timeout + 120 秒（默认 240）；Harmony 为 boot timeout + 120 秒（默认 300）；iOS 为 boot timeout + WDA startup timeout + 120 秒（默认 600）。它们可被既有配置影响，应以本次返回的 operation.deadline_at 为准；CLI 的观察时间要另行设置到足够覆盖这次预算。这里是同一次原生操作的上限，不是失败后不断重新启动。

## 7. 已有虚拟机升级时怎么处理

**正常存量无需新建、改 VM ID、改 UDID、重命名目录或按“换 Agent”迁移。** 推荐维护窗口，避免把实际 Agent/服务管理器重启当作一定不影响正在运行的 VM。

1. 暂停新的任务、安装、观看和 VM 操作；等待在途执行、安装/卸载及原生工作结束，并退出观看会话；尚未结束就先暂停升级，不直接停止 Agent。记录每台配置 ID、Agent 归属、运行/停止状态、原生身份、端口和实例目录。
2. 正常停止需维护的虚拟机并核实原生状态，**只停止，不删除**。首次从旧版升级时，Android 旧停止路径没有本次同步保护：应先完成应用自己的保存，再由维护人员使用该 Agent 原来的 ADB 通道，核对准确 AVD 名后，在该实例内执行一次系统同步并确认完成回执，再停止；新版会自动做这一步。真实手机只结束自动化任务，不需要重置或关机。旧版本可能假报停止，因此不能只看一个 HTTP 200。
3. 在一致时间点备份 Server 总账、Agent 配置/状态资料及需保留的虚拟机数据；确认没有 Android 重复 serial 或同机重复 Agent 争用。
4. 停住旧 Agent 服务，防止升级窗口内自动重连到旧 Server；保存实际启动器配置，不使用 killall/全局 ADB/HDC 重启。
5. **先升级并启动 Server**，使用原数据库和原地址；确认原 VM ID 仍在总账，再允许 Agent 接入。页面同步更新。
6. 在原账号、原工作目录或明确指向原存储的绝对路径下更新 Agent，保留下面的文件与环境；不要新建一份空 storage 冒充原安装。
7. 启动新版 Agent，等待其报告原生库存；检查 VM ID/原生身份/数据未变、归属接续、没有身份冲突或缺失误报。原本停止的实例仍应停止，不自动启动全部配置。
8. 用“启动”恢复选定的原配置，确认实际原生运行、设备注册与健康就绪，再做一条轻量执行/观看/安装检查；最后开放正常流量和更新后的 CLI。

| 必须保留 | 具体内容 |
| --- | --- |
| Server 总账与环境 | 原 DB、Server URL、认证配置、VM/别名/租约记录；绝不能把老 Agent 接到空库或另一套环境试一试 |
| 安装身份 | `AI_PHONE_STORAGE_DIR` 的实际路径与 `agent-host-id`；原启动工作目录、账号和服务配置 |
| Android | 原 `ANDROID_AVD_HOME`/用户 AVD 根目录、受管 `.ini`、`config.ini` 与 `.avd` 数据；SDK 镜像目录及 `package.xml` 身份资料；原启动参数和 wipe_data/snapshot 策略 |
| iOS | 原 Simulator device set 和 UDID 数据；`storage_dir/ios_sim_ports.json`；Xcode/runtime 配置；WDA 构建缓存可重建但升级没必要随手删除 |
| Harmony | 原实例目录/config/数据、`storage_dir/harmony_vm_runtime/registry.json`、镜像目录、共享 UUID 设置和既有本机协议开关 |

Android 若原配置启用了 `wipe_data=True`，再次启动本来就会擦除用户数据，不能把这种配置承诺成“停启保数据”；本轮数据保留实测使用的是不擦数据的配置。

**Android 已有数据分区比配置请求更大时，新版保留实际配置容量，不把它写小。** 实测 SDK 把新建请求的 4 GiB 调整为 6 GiB，旧代码下次启动又写回 4 GiB，导致快照被判为不同硬件配置；本次已修正。`internal_storage_mb` 仍是请求值，不是容量探测结果；不要为升级批量把所有实例改为 6 GiB。历史上已经被改错的容量或快照元数据不自动修复，应先核对原配置和备份；缺失、重复或非法容量证据会明确失败，不能靠清空磁盘绕过。明确增大配置仍沿用原有 SDK 路径，本次没有新增磁盘扩容或迁移工具。

异常处理同样固定：Android 的实际镜像包与配置不一致时先纠正配置；SDK 包资料缺失、目录不一致或不可读时先恢复原环境，不能靠清空 AVD 越过核验。总账有记录但原实例缺失就报错，不自动建空机；身份文件丢失/换 storage 导致冲突时不自动抢占或删数据，先核对并恢复原安装资料；不要用删除、换 Agent 或强制释放来冒充无损修复。新 Server 对旧 Agent 的不完整清单会保守处理，混跑时可能暂时显示离线/未确认，这是升级过渡，不是长期混版本兼容承诺。

### 为什么必须 Server 先，不能随意回滚

已用旧版实际函数隔离复现：`452e7d7` 的 iOS Server 遇到新的 Agent 会话报告停止实例时，会先认领，又用未更新的旧集合把它判成孤儿，随后下发删除。因此**先更新/重启 Agent、让它连回旧 Server 有真实风险**。

没有 DDL 变化不等于可以任意单边回退。旧 Agent 缺少新证据字段，旧 Server 有旧认领逻辑；恢复旧 DB 快照还可能让后来创建的 VM 变成“总账不存在”的孤儿。回滚前应隔离 Agent 连接，核对总账与本机资料的一致时间点，不能恢复旧库后立刻让所有 Agent 对账。本次没有发布一个自动回滚/迁移系统。

## 8. 常驻能力与主流程影响

三端沿用原技术底座，统一的是操作与对账规则：

| 平台 | 原生底座与控制通道 | 本次保留的技术前提 |
| --- | --- | --- |
| Android | Android Emulator/AVD，经 ADB 操作 | SDK、CPU 架构和虚拟化能力匹配；进程仍由原启动方式管理 |
| iOS Simulator | macOS 的 Xcode/CoreSimulator，经 simctl 和 WDA 操作 | 已安装匹配的 Simulator runtime；Simulator 身份与 WDA 服务/端口分别管理 |
| Harmony | DevEco Emulator，经 HDC 和 hmdriver2 操作 | 工具、镜像、架构及本机协议条件成立；原实例目录、registry 与共享开发身份设置继续使用 |

Agent 能重新认领仍活着的 VM，不等于所有进程管理方式下 VM 都一定常驻。Android 仍用原有 Popen 路线，没有新建独立 launchd/systemd/Windows VM 服务；关闭终端、SSH 会话、服务管理器停止或宿主重启，都有各自的进程生命周期。宿主重启后也没有承诺自动启动全部 VM。已有独立 KeepAlive job 不能在升级时随意改，否则可能与 stop/delete 互相对抗。

| 主流程 | 改动影响及保护 |
| --- | --- |
| 真机发现/驱动 | 共享扫描与健康逻辑有变化；无 VM ID 的 USB/TCP 真机保留原路径，VM 不套用到真机账本 |
| 执行/取消/调度 | 不替换 V3/VLM 执行算法；新增维护与身份约束，旧 ready 不再把已停止 VM 放回可调度池 |
| 安装/卸载 | 四种安装分发路径保留；外层超时不再提前释放原生工作占用，正在读取的临时安装包也保留到真实退出 |
| 观看/手动输入 | 占用期间保护 VM 生命周期；换 VM 后不使用上一台 Android 的 driver/输入法/尺寸缓存 |
| WS/结果 | hello、快照顺序、有界并发和同一次状态补送加固；旧会话和迟到结果被核验 |
| 页面/CLI | 不改基本操作布局；明确失败、禁止冲突操作，新 ID 不在错误路径丢失，失败不自动续做下一步 |

超时线程保留占用，不等于新增“等待后自动续单”：外部结果仍然失败且只回一次；本地只是不能谎称底层已经退出。这个保护针对 VM 维护排他，不是新建所有业务互斥或跨进程恢复框架。

Android 的停止仍沿用 `adb emu kill`，没有改成新的关机系统。`e26e3d7` 在普通停止前加入一次系统同步：准确核对 AVD，执行内置 `sync` 并收到本次唯一完成标记，再次核对身份，才进入原退出流程。同步、通道或身份确认失败就报错，不继续退出、不重试；多个地址或身份不明时也不会挑一个继续。显式删除及本次确实新建资源的回滚仍可以走准确销毁清理，不要求坏 guest 先完成同步。旧实现先等 5 秒就可能发 TERM，再等 5 秒发 KILL；现场 SDK 的默认后备窗口是 20 秒，因此项目可能比 SDK 更早打断正常退出。新版读取准确受管进程的设置，给这次原生退出合理窗口，但排队、锁、命令、等待和核验共同受一次 120 秒上限约束，不能每一步重新获得完整预算。预算耗尽不再继续发强制信号、释放身份或删除磁盘；首绑失败仅对本次确实新建的资源做一次有界清理。见[实现和独立复验](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-stop-grace-implementation.md)。

这会带来一处有意的失败行为变化：过去 ADB 不可用时可能直接强制终止，普通停止现在会明确失败并保留实例，删除仍可处理需要销毁的资源。同步使用 Android 内置工具，无需新增 root/REBOOT 权限、配置项或表字段；原有四种快照启动参数保持不变。

**同步完成不等于所有 App 用户态写入已经提交，也不保证硬盘没有 I/O 故障。** 它确认系统已走完一次内核写回路径；既不增加页面等待，也不强制结束 App。a2/a4 的原始失败继续保留，不能仅凭这个补丁就宣称唯一根因已关闭；[技术边界与快照核对](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-guest-shutdown-semantics.md)、[独立修复审查](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-sync-independent-review.md)。

四种 Android 快照策略仍采用原来的启动参数；“保留磁盘数据”和“恢复内存快照”是两回事：

| 配置 | 启动/停止含义 | 本次实际验证 |
| --- | --- | --- |
| `save` | 可以加载已有快照，退出时保存本次快照 | a6-a3 恢复同一内核 boot ID 和完整进程标记，证明真正加载了快照 |
| `discard_changes`（默认） | 禁止退出时保存新快照，但可以加载上次快照；不是清空 App 数据 | a6-a3 恢复旧标记 A，本次新标记 B 不被保存；不能仅按相同 PID 判断进程还在 |
| `cold_boot` | 本次不加载旧快照，仍可在退出时保存 | a6-a3 获得新 boot ID，旧内存标记均不在 |
| `no_snapshot` | 不加载也不保存内存快照；不等于 `wipe_data` | a7 个人合成测试页面与独立落盘文件在停止/启动后均保留 |

`discard_changes` 的验收证明旧内存快照被恢复，没有覆盖所有 App 在该策略下最新写入的保留规则；不能把 a7 的 `no_snapshot` 结果直接推广到其他快照策略。

最终快照验收还找到了“配置写小”的确切反例：保存有文件并不证明能恢复，必须下一次真实加载。修后较小或相等请求不改已有容量，其他配置也无变化时不重写 `config.ini`；错误镜像则在原生启动与文件改写之前被拒绝，恢复正确配置后可继续启动原盘。

现场两次 Android `exit_code=-6` 也已单独查明：实际 SDK 的 ARM 无窗口快照退出路径主动调用了 `abort()`，进程栈和同一二进制指令一致，并非 Agent 强杀。**只对这两个现场样本成立，不能把所有 `-6` 当正常，也不能只凭一行 saving done 判快照成功。** 本次保留真实退出码和资源核验，并用下一次真实恢复作为快照证据；未修改 SDK 或设置通用退出码白名单。[现场最小取证](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-a6-sdk-quit-excerpts.json)；[厂商对应源码分支](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/android-ui/modules/aemu-ui-headless/src/android/skin/winsys-headless.cpp)。

## 9. 哪些旧问题已修，哪些不能冒领

原 23 项已逐项记录在 [审计归档](experiments/vm-lifecycle-audit-20261015/execution/AUDIT-CLOSURE.md)。ADB/HDC 缺席误当停机、iOS 关机失败仍成功、查不到旧实例就重建、注册表并发覆盖、端口/回报串台、旧 ready 恢复调度、扫描阻塞控制、页面旧响应覆盖等，都有对应修复和回归。

交接补查中的修复逐项列在下面，每项的失败与修后证据都保留：

| 问题 | 最终处理 |
| --- | --- |
| 普通启动遇到 Agent 会话变化可能误走换机，撤销原 ID | 普通 start 禁止进入 switch，归属竞争明确失败 |
| 同机换 storage/安装身份可能被误当异地副本并删除唯一实例 | 总账仍有 ID 时只报告归属冲突，不接管、不删磁盘 |
| 旧 Agent 清单缺少完整性字段可能被当成完整空清单 | 必须显式声明完整，才允许据此判断缺失或停止 |
| 已知发送失败仍返回成功，外部调用以为已经下发 | 返回明确 HTTP 失败，并携带本次操作与实例信息 |
| CLI 在失败停止之后仍可能继续启动 | 等待关联本次 request_id/outcome，失败立即结束组合命令 |
| 换机失败后新 ID 在错误输出或页面中丢失 | 页面与 CLI 保留新 ID，旧账不因错误回滚复活 |
| 安装/卸载超时后原生工作仍在，却提前释放占用和临时包 | 保留到原生工作真实退出，只回一次失败，不迟到重报成功 |
| 安卓复用端口后沿用旧 VM 的 driver、输入法或尺寸缓存 | 按 VM ID 和生命周期代次隔离，拒绝迟到构造污染 |
| 鸿蒙旧 registry 缺清理确认时无法接续旧租约 | 准确完整证据只接续归属，再由普通停止核实释放 |
| 鸿蒙把可复用的 TCP 残留误当持续占用 | 按当前原生监听的复用规则检查，同时保留真实占用阻挡 |
| 安卓复用 bind 可能漏掉绑定全部地址的真实监听者 | 三处端口检查统一，疑似冲突增加有界核验 |
| 鸿蒙没有原进程句柄时，活进程可能被报成已停并继续删除 | 停止后补准确进程复查，并取消旧确认位直接成功的快路 |
| Android 在 SDK 正常退出窗口结束前就可能发送强制终止，分阶段等待还会重复获得预算 | 尊重受管进程实际 SDK 退出窗口，排队到核验共用一次预算；到期明确失败并保留身份 |
| Android 普通退出缺少系统写入同步，退出成功不能证明已走完这一步 | 增加一次带准确完成回执的系统同步；失败不继续退出，已有快照策略保留 |
| Android 后台改了系统镜像，已有 AVD 却仍用旧镜像，成功结果还回显新值 | 启动和复用前只读核对实际 SDK 包身份；不符或未知就拒绝，保留原数据供纠正配置 |
| Android 再次启动把 SDK 已调整的数据容量写回较小请求，破坏快照硬件一致性 | 保留已有较大容量；配置不变时不重写文件；真实快照恢复及错误镜像保护均已通过 |



仍保留的限制也没有隐藏：整批候选查询可能等待至 60 秒；首次元数据获取仍重；完整重连会话还受恢复钩子影响；内存/数量仍是软提示。本轮没有新增高频自动扩缩容调度器，也不能据此承诺生产容量。

旧的协议确认开关、FPort 补丁、共享 UUID 以及若干 driver 并发修复是历史已有能力；不是本轮新做。a3 和本轮 b1 的鸿蒙退出确认超时都保留为真实失败；b1 没有捕获故障瞬间的 TCP 表，不能把其唯一原因武断写成 TIME_WAIT。独立自有端口实验已经复现探查缺陷，并修正判据。一次 Apple bootstatus 等待 System App 超时同样没有被后续通过覆盖。

## 10. 测试与交付版本

产品提交：`2dc9844`（包含 `0e9bde8`、`d7cbac0`、`9034204`、`8a2a3be`、`e26e3d7`、`f263af3` 的前序修复）；个人 CLI 提交：`e65dad5`。本次没有推送、发布包、部署公司服务或修改全局 CLI/profile。下面是不同层次的检查，不能重复相加成一个“总测试数”。

| 验证层 | 实际结果与范围 |
| --- | --- |
| 当前最终源码统一回归 | **1,549 passed / 2 skipped**，73 个测试文件；33 个产品文件 SHA256 前后一致；外网/原生命令保护无拦截事件，说明本轮没有尝试越出隔离范围；唯一允许的子进程为 Node 页面测试 |
| 跳过与警告 | 2 项为 Android 不适用的 Server 探查参数分支；73 条既有 Pillow/FastAPI 弃用及 SQLite 测试清理警告，不称作“无警告”，也不据此推断生产连接泄漏 |
| 页面 | 42 项 Node 行为检查、构建通过；旧 102 项浏览器验收保留，新增失败路径另有 44 项浏览器检查和 6 张截图 |
| 个人 CLI | 58 项兼容回归；实际在 `8a2a3be` 跑真实本地 Server 路由→CLI 子进程 [12 项通过](experiments/vm-lifecycle-audit-20261015/handoff/vm-cli-final-8a2a3be.json)，30 次精确 loopback 请求，真实 profile 读取为 0；对应 Server 源码与最终 `2dc9844` 一致，不冒记为重跑 |
| 升级反例和数据保护 | 真实旧函数重放、普通启动认领竞争、同机安装身份冲突、旧清单缺字段、旧鸿蒙租约接续、安装/卸载超时占用与 Android 缓存时序均纳入；旧函数冻结为准确测试样本，不依赖浅克隆中不存在的 Git 历史 |
| 首轮个人三端原生证据 | `74f209d` 的 Android、iOS、Harmony 均完成同一合成页面真实输入、停止/再启动保留数据、删除清理；iOS 额外验证离线删账后重连清孤儿；不将它们冒记为后来全部补丁重跑过三端 |

最终统一回归证据：[结果与源码摘要](experiments/vm-lifecycle-audit-20261015/handoff/combined-regression-evidence.json)、[完整日志](experiments/vm-lifecycle-audit-20261015/handoff/combined-regression.log)。产品、73 个测试文件及历史重放样本的当前摘要均与最终回归一致，没有测试后产品改动；[最终交付清单](experiments/vm-lifecycle-audit-20261015/handoff/final-delivery-manifest.json)记录代码与个人 CLI 的准确版本。

原生专项按实际运行版本和结果单独归档：`9034204` 上完成鸿蒙 b2 与 iOS 重验；后续 `8a2a3be` 只改 Android manager、Android API 与 WS 中 Android stop/delete 的默认预算分支，`e26e3d7` 增加 Android 同步屏障，`f263af3` 增加已有 AVD 镜像一致性核验，`2dc9844` 保留已有数据容量；没有重跑已通过的其他两端原生测试。最终统一回归覆盖这些共享路径，不能把旧原生记录的源码摘要冒写为新版本。

- **鸿蒙 b2：通过并经 root 独立核验。** 从旧式 registry/租约、丢失一次停止回报开始，只接续归属，普通停止后直接查旧 lease 行确实消失；同 UUID 重启读回随机数据；第三个新 Manager 会话没有原 Popen 句柄，也能正确确认原进程退出；最后一次精确删除，Server 404，实例、registry、runtime、进程和端口清空。三次现场抓到真实 TIME_WAIT，新判据正确放行；[摘要](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/harmony-legacy-b2-summary.json)与[root 核验](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/harmony-b2-root-review.json)。
- **iOS `9034204` 重验：通过并经 root 实看两图。** 149.8 秒完成真实输入、同 UDID/端口停启保留页面 token/count、离线立即删账与同 host 新会话重连清孤儿；原生集合/实例数据/私有端口释放，187.8 MiB 本轮 WDA 缓存精确清理；[摘要与截图](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/ios-evidence/ios-20261016-035022-ddd0e8/summary.json)。
- **安卓 a2：真实上传包→安装任务→Agent→原生安装通过，但整轮持久化未通过。** 原页面确认后 count=1，正常停止/启动后变为 0；原实例和 APK 仍在，AVD 已封存。原因仍未知，不能用单次安装成功或后续通过代替这轮失败；[失败取证](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/a2-readonly-findings.md)。

- **安卓 a3 诊断：数据通过，但视觉不完整。** App 的独立落盘见证文件与原页面 localStorage 都保留，原页面读到 token/count=1；两次原生退出均未走 TERM/KILL。然而最后截图是白屏，不能记为整轮 E2E 通过；[诊断记录](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-app-install-a3-results.json)。
- **安卓 a4 `8a2a3be` 验收：未通过，重复出现页面记录丢失。** 独立落盘见证文件同 SHA/inode/mtime 保留，原 localStorage 从确认后的 token/count=1 变为重启后的 null/count=0，origin/key/provider 一致且没有读取异常；实际两次停止都未触发 Agent TERM/KILL。第三张图是可读的空记录页面，新增视觉门槛尚未执行，不能归为白屏。已停止并封存本轮实例，随后增加普通停止前的系统同步屏障；[完整结果](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-app-install-a4-results.json)、[root 实看三图](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-a4-root-visual-review.json)。

- **安卓 a5 `f263af3`：完整通过，root 已独立实看三图。** 76.8 秒完成真实上传安装、同一页面输入、准确操作回执、停止、原实例重启读回原 token/count=1，以及删除和资源释放；两次真实系统同步取得本次标记，耗时约 38/36 毫秒，后续退出无 Agent TERM/KILL；这只是该环境实测时延，不是性能保证。[结果](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-app-install-a5-results.json)、[root 核验](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-a5-root-review.json)。原 a2/a4 失败没有被覆盖，也不能据这一次通过宣称任意 App 未提交写入都能保留。

- **安卓 a6-a2 `f263af3`：快照恢复失败，已找到并修复明确原因。** 两代启动参数相同，但原生配置被从 6 GiB 写回 4 GiB，SDK 拒绝加载不同硬件配置的快照；原生进程已停止，失败磁盘封存。没有证据证明底层磁盘文件被物理截短，也不把这个原因套到 a2/a4 的 localStorage 失败。[失败结果](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-snapshot-a6-a2-results.json)、[独立因果核对](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-a6-a2-independent-cause-review.md)。此前 a6-a1 是私有 ADB 启动参数的测试脚本错误，原生创建为 0，单独保留。
- **安卓 a6-a3 最终 `2dc9844`：完整通过，98.6 秒。** 六代原生启动完成 save/discard/cold_boot；13 个关联操作中 12 个成功、1 个预期镜像错误准确失败。错误配置为本机已安装的另一镜像，但启动次数为 0，原 AVD 的 33 个文件内容/大小/inode/mtime 均不变；明确恢复原配置后启动成功，再停止删除。全部私有进程/端口及实例释放。[结果](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-snapshot-a6-a3-results.json)、[root 核验](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-a6-root-review.json)。
- **安卓 a7 最终 `2dc9844`：个人合成 App Case 完整通过，74.3 秒，root 实看三图。** 真实上传包、一次原生安装、合成测试页面输入、WS 重连/同安装身份新会话接续、普通停止、同 AVD 启动后读回原 token/count=1、删除；两次系统同步约 40/27 毫秒，四个准确操作回执均成功，没有 Agent TERM/KILL。页面与独立落盘文件均保留，清理和源码摘要独立核验通过。[结果](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-app-install-a7-results.json)、[root 三图与终态核验](experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android-a7-root-review.json)。这是重连和 Manager 重建，未冒充操作系统级 Agent 崩溃/宿主重启测试，也不是公司 App 或生产 VLM 业务验收。

b1 的失败原件、故障后端口观察及精确补删回执已经单独保存于 `handoff/native-followup/b1-evidence/`，没有被 b2 成功覆盖。

成功测试创建的原生实例、私有端口和运行进程均已清理；**另保留了 Android a2、a4、a6-a2 三份已停止的失败磁盘**，用于追溯，不能把它们写成全部测试磁盘已删除。它们位于个人隔离测试目录，不占运行端口，不会被再次自动启动。原生数据库、APK、签名文件、磁盘镜像与构建缓存不随 Git 证据提交。

不能保证的范围：任意混合版本、历史异常存量全部自动修复、任意工具版本/操作系统常驻方式、真实 Windows/真机/iPad、长期高并发压力与生产业务链路。实际存量升级前仍需按第 7 节核对原环境，不应以这些测试替代现场总账盘点。

完整原始证据与平台细节：

- [Server/API/数据库及升级](experiments/vm-lifecycle-audit-20261015/handoff/server-api-schema-upgrade.md)
- [Android 端口、缓存与常驻](experiments/vm-lifecycle-audit-20261015/handoff/android-ports-upgrade.md)
- [iOS 身份与端口预留](experiments/vm-lifecycle-audit-20261015/handoff/ios-ports-upgrade.md)
- [鸿蒙权限历史与版本识别](experiments/vm-lifecycle-audit-20261015/handoff/harmony-foundation-permissions.md)
- [主流程影响与新增故障复验](experiments/vm-lifecycle-audit-20261015/handoff/main-flow-impact.md)
- [独立升级审查](experiments/vm-lifecycle-audit-20261015/handoff/upgrade-independent-review.md)
- [个人 CLI 兼容说明](experiments/vm-lifecycle-audit-20261015/handoff/cli-compatibility.md)
