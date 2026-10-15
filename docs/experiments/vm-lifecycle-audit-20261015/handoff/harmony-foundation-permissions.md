# 鸿蒙基础能力、历史“权限”问题与升级边界

核对时间：2026-10-16（Asia/Shanghai）。调查源码基线 `74f209d`，比较基线 `452e7d7`；生命周期实现提交为 `0e9bde8`，`74f209d` 保存个人验收证据。初始基础调查只读核对源码、Git 历史和已有个人实验材料，没有修改产品源码；本文件后续补充的隔离复验也没有执行 Emulator/HDC 命令或接触公司环境。

补充复验：后续另经总控授权修正了旧未确认租约的窄边界，包含 `74f209d` 之后的工作树增量；具体链路和隔离测试见第四节，不把它计为重新执行过原生设备验收。

## 先给用户讲清楚的结论

1. **过去“后来加了一个东西”，最可能指 2026-08-21 加的本机协议确认开关 `AI_PHONE_HARMONY_AUTO_ACCEPT_AGREEMENTS`。** 它解决 Emulator 启动时等待协议输入的问题，默认关闭，不是本次新加，也不是给 App 授权。
2. **另一个容易被叫成“权限补丁”的东西，是 hmdriver2 的 FPort 隔离补丁。** 它避免自动化转发端口与虚拟机 HDC 端口冲突；不授予系统权限，本次也没有换掉它。
3. **本次修的是生命周期、身份核对、探查和失败收尾，不是把所有权限弹窗一并消除。** 特别是全新鸿蒙系统的输入法首次引导：个人 a7 成功验证前，已显式完成基础模式和键盘初始化；不能把这说成产品已经做到空白镜像零准备即可输入。
4. **VM ID 已经可以作为平台唯一身份；端口和原生 UUID 是另两层信息。** HDC/FPort 有端口独占要求，不妨碍平台按 VM ID 管机器；共享原生 UUID 也不能成为串台的理由。

“最可能”是根据用户描述和历史提交作出的定位判断，不是断言用户记忆只可能对应其中一项。

## 一、不能混在一起的七类问题

| 用户看到的现象 | 实际属于哪一层 | 历史和当前实现 | 本次状态 |
| --- | --- | --- | --- |
| Emulator 刚启动就退出，日志提示先接受协议 | **DevEco/HarmonyOS 启动协议** | `c7dd3ba` 新增本机开关；开启时仅向真实 Emulator 进程 stdin 写一次 `y`，保留原进程和日志；默认关闭，Server 不能覆盖 | 历史已处理，本次保留；未重演全新、未接受协议的宿主 |
| 探查说还有别的 license 未确认 | **能力探查的前置判断** | `ffab56a` 初次提交已经采用 `deferred_to_install_or_create_cli`，不以“全部厂商协议已接受”为硬前提；`_license_status` 函数仍存在，但当前探查路径不调用它 | 原有规则正确，本次没有“取消所有 license 前置”的新修改 |
| macOS 不能读取其他进程端口、GUI 会话不可用 | **宿主权限/会话/硬件环境** | 端口探查容忍 `psutil.net_connections` 无权限，继续使用有限 lsof 查询和本机 bind 检测；GUI/虚拟化环境仍是本地 DevEco 运行条件 | 没有新增管理员、辅助功能、录屏授权步骤；Windows 和全新宿主初始化未实测 |
| HDC 看不到设备，UITest/driver 建连失败 | **设备控制与自动化通道** | 真机仍需要开发者模式、USB 调试及信任主机；受管 VM 使用 HDC TCP 通道。hmdriver2 建立 UITest/Hypium 通道，失败不等于操作系统协议未同意 | 共享驱动实现未改；本次提升生命周期与健康状态的区分，个人 VM 通道已实测 |
| 提示“hmdriver2 FPort 隔离不可用”或版本不支持 | **Python 依赖与端口补丁** | 已有补丁要求 `hmdriver2==1.4.4`，把本地转发端口分配到 `16556..20000`，避免抢占 HDC 的 `10000..16555` | 原有基础保留；不是权限要求，不承诺任意新版 hmdriver2 自动兼容 |
| HAP 安装失败、设备不在开发登记名单、包架构不匹配 | **应用安装/签名/开发设备身份** | 安装走 `hdc install -r`；ABI 预检另行检查包与 VM 架构。共享 UUID/UDID 用于需要登记调试设备身份的场景，不负责同意运行时权限 | 本次安装器、包 ABI 模块、共享 UUID 规则均未修改；a7 使用内置浏览器，不能据此宣称公司 HAP 安装/签名已验收 |
| 点输入框后反复出现输入法声明、基础/完整模式、键盘选择 | **Guest 内应用首次引导** | a3/a4/a5 均观察到输入法初始化阻挡输入；a7 在正式 Case 前显式完成基础模式和 26 键设置，没有账号登录或额外权限授权 | 仅环境准备已完成，不是产品自动修复；新建空实例仍可能再遇到 |

此外，过去“换 Agent 要输入一段确认原因”属于**平台人工释放租约的产品流程**，不是鸿蒙系统权限。`73ded56` 加入过受控释放入口；本次改为既定的删除旧配置、新建空实例流程，前端不再要求通过强制释放弹窗完成正常操作。兼容的 `/force-release` API 不再允许用一句确认文字替代实际清理证据。

### 能直接定位的历史提交

| 提交与日期 | 做了什么 | 能证明什么，不能证明什么 |
| --- | --- | --- |
| `20911c4`，2026-04-28 | 已包含屏蔽 hmdriver2 `Driver.__del__` 全局副作用的补丁 | 针对旧 driver 析构清空其他设备单例、撤掉新 fport 的风险；不是接受权限，本轮没有重跑当时的真机事故 |
| `ffab56a`，2026-07-31 | 引入鸿蒙 VM 生命周期、独立 FPort 池、共享原生 UUID 设置；探查已经不要求接受所有协议 | 这些是本次基线之前已有的基础；不能把它们都称为本次新增修复 |
| `5a30d4c`，2026-08-19 | 同 serial 的 driver 共享状态和锁，恢复后同步 fport，镜像跟随实际端口 | 修的是并发访问和通道失配，不是授予新权限 |
| `c7dd3ba`，2026-08-21 | 增加 `_spawn_harmony_emulator` 与本机 `AI_PHONE_HARMONY_AUTO_ACCEPT_AGREEMENTS` | Git 可直接证明新增 stdin PIPE/一次性输入、默认关闭及本机配置边界；历史复盘记录把原故障定位为非交互启动时 stdin EOF 导致 exit 1，本轮未重演该旧宿主状态 |
| `73ded56`，2026-09-24 | 受管设备身份改用 `harmony-vm:<vm_id>`，并校验租约、修复端口复用和折叠态问题 | 平台身份与 HDC 地址的分离在本次之前已存在；不是本轮才让 VM ID 成为稳定身份 |
| `0e9bde8`，2026-10-16 | 生命周期归属、单次操作、超时、清理、探查和镜像识别加固 | 本轮改变的主要范围；保留既有 DevEco/HDC/hmdriver2 技术路线 |

核对命令包括 `git show c7dd3ba`、`git show ffab56a:backend/ai_phone/agent/harmony_vm/capability.py`、各相关文件的 `git log -S`。在已提交历史中，未找到“最近另加一个通用系统权限后，再由本轮删除”的证据；不能据模糊记忆补造这个因果。

## 二、VM ID、UUID、租约和端口分别管什么

| 名称 | 用途和唯一范围 | 停止后 | 删除后/重用规则 |
| --- | --- | --- | --- |
| **VM ID** | Server 配置主键；公共设备键为 `harmony-vm:<vm_id>`；任务、别名与设备路由围绕它关联 | 保留，仍是原机器 | 删除配置后旧 ID 不主动分给新配置；换 Agent 生成新配置 ID |
| **实例名和路径** | Agent 用 `aiphone_harmony_<vm_id>` 的安全名称及准确 `instance_path` 定位本地磁盘定义；原生进程认领同时核对名称、路径与 HDC 参数 | 原实例目录和私有数据保留 | 只删除对应实例及私有数据，不卸载 DevEco 或共享系统镜像；不凭前缀猜测其他目录归属 |
| **原生 UUID / 派生 UDID** | DevEco 配置中的 `uuid`，可被共享设备身份设置覆盖；用于 guest/开发包所见身份，**不是平台设备主键** | 通常保留；若管理员改了全局共享 UUID，下次启动按设置更新 | 默认新实例由 DevEco 生成；若开启共享设置，多台 VM 可以有相同原生身份，这属于现有产品策略 |
| **lease_token** | Server 为一次 HDC 端口占用生成的令牌，关联 VM、Agent 与端口；防止旧命令或旧回报操作新占用者 | 正常停止确认后，Server 释放租约；Agent 保留上次 token/地址作为本地清理与对账证据 | 再启动生成新的 token；旧 token 不得释放或认领新 token 的资源 |
| **HDC 端口/地址** | `10000..16555`，运行时参数 `-hdcPort`，地址形如 `127.0.0.1:10001`；OS 层只在所在宿主有效，当前 Server 租约表另外以 port 主键实施全局避冲突 | 确认停止后可归还；不保证下次仍拿到同一端口 | 新实例可以使用已归还的数字，但不能继承旧 VM 的任务、driver、别名或缓存；本机 bind/HDC 检查仍拦截真实占用 |
| **FPort** | `16556..20000`，hmdriver2 使用的宿主 TCP 转发端口，指向设备自动化服务；是宿主资源，不是 VM 的永久身份 | 关闭 driver 并清理其转发 | 可重新分配；镜像从当前 driver 的准确 fport 获取通道，不能扫描一个“看起来可用”的其他端口代替 |
| **request_id / observed_request_id** | 操作编号及观察所依据的操作编号；区分一次启动、停止以及迟到的扫描结果 | 不作为机器身份变化 | 新操作不能被旧结果覆盖；`created_in_attempt` 必须属于该次实际创建，不能沿用上一次的注册表标记 |
| **host_id** | Agent 安装目录里的持久身份，和一次运行的 Agent 会话 ID 分离 | Agent 重启保留 | 不应复制到另一台宿主；独立 Agent 测试使用独立 storage 目录 |

有两个表述必须纠正：

- **“停止保留机器”不等于“停止后永远占着同一个端口”。** 鸿蒙本来就是保留实例定义和数据、释放当次 HDC 租约；再次启动重新取得运行资源。
- **“Server 已删除”不等于“离线宿主已经清理”。** 最终契约下 DELETE 先删除 Server 总账、路由、别名和租约，再向原 Agent 发送一次清理；离线则由重连孤儿对账补删。仍在线的旧进程由本机实时端口/归属检查隔离，不得只凭 Server 已释放租约宣称宿主资源已消失。

当前 `_short_id()` 是 `uuid.uuid4().hex[:12]`，没有主动复用旧 ID 的逻辑；数据库主键保证有效记录不重号。它仍是随机 ID，不是覆盖已删除历史的“数学上绝不重复”分配账本。本次为兼容存量没有改旧 ID 格式，不能把概率唯一夸大成形式化保证。

### a7 的实际例子

个人 macOS/ARM64 记录中，同一个 VM `b554f5abd1d9` 停止再启动后：

- VM ID、实例路径、原生 UUID `ddf4b2d0-71d2-4daa-9a1c-2f71999d297e` 和页面持久数据保留。
- HDC 仍恰好分到 `10001`，FPort 仍恰好分到 `16556`；**这不是固定端口承诺**。
- lease token 从首次记录的值变成另一个值，`created_in_attempt` 从 `true` 变为 `false`，说明是原实例再次启动，而不是新建。
- 删除后 Server 返回 404，Agent 只收到一次 delete；目录、registry、runtime、对应原生进程和监听端口均独立检查为空。

证据：`execution/harmony-evidence-summary.json`、`execution/native-evidence/harmony/evidence-index.json` 与原始 `/tmp/aiphone-personal-harmony-20261016a7/result.json`。

## 三、Windows/macOS 镜像名称和版本：修了识别，没有承诺跨架构运行

镜像有几种不同的“版本”，不能拿一个名字代替全部条件：

| 字段 | 本次个人证据中的值 | 含义 |
| --- | --- | --- |
| 产品系统版本 | `HarmonyOS 6.0.31(23)` | 创建命令的 `-osVersion`；其中 23 是 API 级别 |
| 磁盘目录 | `HarmonyOS-6.0.31/phone_all_arm` | 本地镜像布局；`phone_all` 可对应 Phone/Foldable/WideFold/TripleFold 候选 |
| 镜像 `info.json.version` | `6.1.0.115` | 镜像包构建版本，保存在 `software_version`，不能覆盖产品系统版本 |
| `info.json.abi` | `arm` → `arm64` | 归一化后的实际架构；Windows/x86 别名归一化为 `x86_64`，不会冒充 ARM |
| Emulator 工具版本 | 已保存只读工具证据为 `6.1.0.410` | 宿主工具版本，和系统镜像/包构建版本是不同对象 |

本次 `0e9bde8` 做到：

1. 归一化系统版本的空格和前缀；保留 API、Beta 的区别，互相矛盾则拒绝，不能偷换版本。
2. 镜像清单按设备类型、系统版本、ABI 区分，避免同版本不同架构相互覆盖。
3. 识别完整 `phone_all` 本地镜像对应的多个屏幕形态；本地元数据只说明候选存在，仍需实际 create 探查确认该工具/机型组合可创建。
4. 正常本地镜像不必每次先联网查询；只缓存短期、成功的静态兼容结果，工具/配置/镜像变化会使缓存失效，端口和内存仍实时检查。

**已验证**：macOS/ARM64 的上述真实镜像，及模拟的 Windows/x86_64 元数据、架构和版本拼写差异。**未验证**：真实 Windows 宿主的 DevEco 安装、GUI、驱动、镜像创建和完整执行链。目录解析通过不能替代 Windows 实机验收，也不能保证所有历史目录布局和机型组合都可用。

## 四、存量升级：保留什么，哪些情况明确报错

| 存量情况 | 当前处理 | 证据范围 |
| --- | --- | --- |
| 原有 VM ID、原生实例目录和数据 | 不要求批量重建或重新编号；重启复用原实例 | 源码、回归；a7 证明停止/启动保留数据，不代表所有历史宿主已升级 |
| 老 registry 缺少 `created` 字段 | 仅在准确记录的实例名、路径及真实 `config.ini` 同时匹配时承认已有定义，不凭生成名称猜测 | `test_bound_restart_failure_never_deletes_existing_userdata` 的 legacy 参数覆盖 |
| 本机默认实例路径变了 | 优先沿用该实例原来记录的路径，不自动删除再创建 | 源码与隔离回归 |
| 旧目录含非 ASCII、原实例缺失、创建参数已变 | 明确报错；不静默迁移、换镜像或重建丢数据 | 本次代码边界；未做历史中文路径迁移实机试验 |
| Agent 会话 ID 重启后变化 | 新版持久 `host_id` 保持归属；无 host_id 的旧记录只允许旧 owner 离线且有精确原生证据的有限接管 | Server/Agent 隔离回归，不是任意“谁上报归谁” |
| 旧租约或旧 HDC 地址晚到 | 校验当前 VM、Agent/host、lease 和操作编号；旧清理不能碰复用端口的新实例 | 端口复用、旧 driver、旧镜像帧和迟到回报测试 |
| 旧 driver/依赖/共享 UUID 设置 | 本次没有更换 hmdriver2 版本、FPort 补丁、共享 UUID 算法、安装器或公共 HarmonyDriver 实现 | `git diff 452e7d7..74f209d` 对上述文件为空 |
| Server 升级而旧 Agent 长期不升级 | 旧 Agent 可能不理解 `rollback_new_instance`、操作期限和创建来源证明 | 不能承诺长期混跑满足新契约；本轮没有跨版本舰队实机验证 |
| 旧 registry 只有 `ready=False`、没有 `cleanup_confirmed`，Server 却仍持旧租约，且 Agent 会话 ID 已变化 | 后续隔离复现确认了会话接续丢失与假停止两个缺口，并作窄修：完整清单、准确实例/路径/原租约及宿主证据通过后只接续会话，保留原状态和租约；普通停止再走实际进程/HDC/端口检查 | 15 项真实 Agent payload→Server→普通 stop 桥接用例通过；证据不足/冲突仍拒绝，不删除旧数据，也不承诺全历史异常自动恢复 |

鸿蒙模型表在此次比较中没有变化，新增操作/归属证据放在原有 runtime JSON 和本机 registry；这不等于升级时可以删除这些文件。`agent-host-id` 属于本 Agent 安装目录，不应复制给另一宿主；测试隔离目录的做法也不应套用为生产存量搬迁。

正常停止仍保留数据；首次绑定失败只对本次确实新建的实例执行一次回滚。已有实例重启失败不删除旧定义。对不确定的创建来源不授予自动删除许可，Server 也不会把历史 `created=true` 当成本次新建的证明。

### 旧未确认租约的增量复验

使用真实旧格式 registry 文件和真实 `config.ini`/数据 marker，在新建 Manager 中读回；调用实际 `report_orphan_reconcile` 生成 payload，再进入 Server handler、普通 stop API、实际 Agent `handle_stop/stop_sync`。只替换原生检查为 fake，不手填一份“成功”的 Agent 消息。

复现前：Server 在证据不足以释放 lease 时返回，连已核实的会话接续也没有提交；Agent 则把旧 `ready=False` 快速当作清理成功，连替换的进程检查都未调用。修正后：

- Server 只在完整清单、精确受管实例、非空路径、token/serial/端口和实际 lease 行均吻合，且旧 owner 离线、宿主归属不冲突时提交会话接续；不改变运行状态、不释放 lease、不自动停止或删除。
- 当前显式停止统一重新检查进程/HDC/端口；`cleanup_confirmed=True` 只是历史事实，不能跳过本次检查。已实际停止仍幂等返回 `not_running`，不重复发原生停止命令。
- 两个正例（旧记录无 host_id、已有同安装 host_id）均能通过普通 stop 确认退出、释放旧租约，并保留原 `config.ini` 和数据 marker。
- 10 个证明不完整/冲突负例及仍 Connected、进程不可确认、端口仍忙三个负例继续拒绝；没有放宽停止判据或增加重试。

隔离测试：`tests/test_harmony_legacy_lease_upgrade.py` 的 **15 项**通过；与 Agent 专项合跑 **88 passed**，另六份 Harmony 回归 **104 passed**（集合有重叠，不相加）。后续 b1 复核又发现旧确认位可能来自接管实例的误判，因此当前已取消缓存位快返回；新增 spy 用例证明重新核实后仍不重复发停止命令，见 [端口与停止补充证据](native-followup/harmony-port-stop-followup.md)。

这证明的是有完整证据时的保数据操作路径，仍不代表已在真实历史宿主上完成升级。证据冲突、失去 registry、不同宿主、缺失/错误 lease 等情况应单列处理，不能用删配置代替保数据恢复。

## 五、此次真实验收到底覆盖了什么

最终 a7 的正式链路为：创建未绑定配置 → 真实探查 → 首次绑定并启动 → 真实 HDC/hmdriver2 readiness → 内置浏览器输入、确认并取得真实页面回执 → 停止 → 原实例重启 → 回读相同 token/count → 删除并核对资源退出。

这里有明确前置：

- `AI_PHONE_HARMONY_AUTO_ACCEPT_AGREEMENTS=false`；宿主原本已有 DevEco 初始化状态，本次没有改 `.emu_config` 或旧实例配置。
- 空白 guest 的输入法基础模式及 26 键初始化在正式 Run 前完成；记录声明 `account_login=false`、`extra_permissions_granted=false`。这是被授权的个人测试准备，未写成产品自动同意所有弹窗的能力。
- a3 首次清理曾返回未确认。`No target available` 只是附加诊断，现场没有当时的同步采样，不能把事后无监听认定为当时已满足。此后只增加 `exit_evidence` 摘要，未放宽 15 秒/4 次 Offline+可 bind 的门槛、未增加自动重试；a3 后续一次准确清理及 a7 最终一次 delete 都另有回执。

**不能从 a7 推出**：Windows 已通过、真实手机已回归、公司包安装和签名已通过、VLM/Onion 业务已通过、多实例高并发压力已通过、真实断网孤儿对账已通过。该实验为隔离安全关闭了默认孤儿扫描。

a7 截图和原生回执对应 `74f209d` 保存的验收基线；后续缓存和旧租约边界增量做了隔离回归，没有再次启停原生设备。证据索引的“当时源码指纹一致”不能扩大成所有后续改动都已重跑实机。

## 六、核对入口与本轮只读复验

以下行号按 `74f209d` 源码：

| 主题 | 代码入口 |
| --- | --- |
| 协议一次性 stdin、本机配置边界 | `backend/ai_phone/agent/harmony_vm/manager.py:64`；`backend/ai_phone/config.py:158`、`:1909` |
| 未启用全部 license 前置、版本与本地镜像归一化 | `backend/ai_phone/agent/harmony_vm/capability.py:188`、`:526`、`:623`、`:710`、`:947`、`:1025` |
| FPort 池和依赖版本 | `backend/ai_phone/agent/harmony_vm/fport.py:20`、`:71`；Agent 预安装入口 `backend/ai_phone/agent/main.py:3073` |
| 平台 VM 身份和复用地址校验 | `backend/ai_phone/agent/harmony_vm/registry.py:20`、`:84` |
| HDC 租约分配/释放与状态回报 | `backend/ai_phone/server/harmony_vm/service.py:196`、`:251`、`:461` |
| 账本删除/换 Agent/旧人工释放入口 | `backend/ai_phone/server/harmony_vm/api.py:612`、`:625`、`:826` |
| 本地停止、删除、完整对账、持久化 | `backend/ai_phone/agent/harmony_vm/manager.py:1254`、`:1398`、`:1654`、`:1764`、`:2215` |
| 原生 UUID/UDID | `backend/ai_phone/agent/harmony_vm/manager.py:2365`；`backend/ai_phone/shared/harmony_identity.py` |
| 真机信任、UITest/hypium 与通道历史补丁 | `backend/ai_phone/agent/drivers/harmony.py:74`、`:267`、`:582`；该文件此次未修改 |
| HAP 安装与 ABI 检查 | `backend/ai_phone/agent/app_install/harmony.py:12`；`backend/ai_phone/server/app_install/service.py:418`；`backend/ai_phone/server/harmony_vm/package_meta.py:35` |
| 旧归属接管及当前创建来源 | `backend/ai_phone/server/vm_lifecycle.py:117`、`:582`；`backend/ai_phone/agent/identity.py:9` |

本次文档复验命令（只运行隔离测试，不启动原生设备）：

```bash
cd backend
.venv/bin/python -m pytest -q \
  tests/test_harmony_vm.py \
  tests/test_harmony_vm_agent_hardening.py \
  tests/test_harmony_vm_identity.py \
  tests/test_harmony_vm_transport.py \
  -k 'agreement or spawn or shared_uuid or shared_udid or image_version or image_listing or shared_phone_image or architecture_mismatch or userdata or reused_hdc or old_vm'
```

结果：**32 passed, 115 deselected，1.07s**。覆盖默认关闭/本机协议开关、一次性 stdin、共享原生 UUID 与平台身份分离、镜像架构/版本归一化、旧数据保留和端口复用隔离；不把这些 mock 结果记成 Windows 或真实手机通过。

最初的基础调查只新增此文档；上述旧租约增量是在独立复现后经总控另行授权的最小产品修正，后续 `9034204` 还修正了端口复用/监听检查及无进程句柄停止证据，见[独立端口补查](native-followup/harmony-port-stop-followup.md)与[进程核验报告](harmony-reclaimed-stop-independent-review.md)；没有新增协议/系统授权或更换镜像基础。
