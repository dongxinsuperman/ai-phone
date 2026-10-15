# Android 已有 AVD 的系统配置修改边界

审查日期：2026-10-16。下列修前证据基于冻结提交 `e26e3d77e88b6a769ce80ccd86807e92ec7c881f`；修前行号均指该快照。镜像守卫阶段提交为 `f263af3`，最终产品为 `2dc9844ffd86b88f2e41acfc79fae87ae222d631`。阶段证据与最终验收分别保留在文末；原4例 JSON 保持原样。本项核实系统镜像与存量 AVD 的一致性，并记录最终容量保护及其关联原生复验。

## 修前结论（e26e3d7）

**缺口成立：Server 可以保存新的有效系统镜像，而同名已有 AVD 仍保留旧镜像指向；profile 和 config_json 的优先级不会消除这个问题。** 新旧镜像均已安装、宿主 ABI 合适、其他启动条件正常时，新要求可通过探查，随后仍按旧 AVD 启动。若新镜像未安装或 ABI 不合适，预检会失败，不属于“成功启动旧镜像”的路径。

这里有源码与纯内存函数重放证据，没有运行真实 Emulator，不能把“按旧 AVD 配置启动”的代码结论写成实机启动验收。原始单项结果见 [android-existing-image-configuration-audit.json](android-existing-image-configuration-audit.json)。

## 实际优先级

1. Server `vm_payload` 直接取 VM 列中的 `api_level / abi / system_type / system_image`；`config_json.system` 不覆盖这四项，profile 引用也不会在下发时重新解析镜像。
2. Agent 优先使用非空 `system_image`；为空时才用顶层 API、归一化 ABI、system_type 拼出 package ID。`abi=auto` 按宿主解析，`arm64-v8a` 与 `arm64` 归一化。
3. 能力探查检查的是这个有效 package ID 是否已安装及 ABI 是否可承接，没有把它与已有 AVD 的 `image.sysdir.1` 比较。
4. `e26e3d7` 的 `_ensure_avd` 找到同名 AVD 后，只改显示、内存、部分硬件配置；`image.sysdir.1 / target / abi.type` 不变。后续命令使用 `-avd <原名>`，没有系统镜像覆盖参数。
5. 成功回包中的 `details.system_image` 回显计算出的请求值，不是从已有 AVD 读回的镜像事实，不能用它证明历史记录已匹配。

证据位置（均以 `backend/ai_phone/` 为前缀）：

| 环节 | e26e3d7 文件/行 | 事实 |
|---|---|---|
| PATCH 准入与保存 | `server/android_vm/api.py:189-247` | stopped/error/unavailable/agent_offline 等允许修改这些字段，没有已有实例系统配置限制 |
| 下发字段 | `server/android_vm/service.py:110-165` | 系统字段取 VM 列；config_json 仅展开性能、存储、网络、硬件、启动等字段 |
| 保存字段 | `server/android_vm/service.py:182-203` | 包含四个系统字段及 config_json/profile 元数据 |
| Agent 镜像计算 | `agent/android_vm/manager.py:332-351` | 显式 system_image 优先；否则使用顶层字段 |
| 探查 | `agent/android_vm/capability.py:105-155` | 依据请求的有效镜像检查安装与 ABI |
| 已有 AVD | `agent/android_vm/manager.py:1078-1099,1132-1179` | 更新部分 config.ini 字段后直接返回，旧镜像指向保留 |
| 启动/回包 | `agent/android_vm/manager.py:397-405,469-490` | 启动原 AVD，回显请求镜像 |

## 纯内存重放

从 e26 Git AST 提取现有 `apply_vm_patch / vm_payload / _normalize_vm_config_dict / default_system_image / _ensure_avd / _write_avd_screen_config` 及其纯函数依赖。AVD config、目录操作与 avdmanager 列表回包均为内存对象；唯一假命令是“列出同名 AVD”，没有 HTTP、WS、原生命令、数据库写入或真实文件改动。

| 修改输入 | 实际下发/计算的镜像 | 已有 AVD 保留的镜像 | 结果解释 |
|---|---|---|---|
| 顶层 API 33→35，并指定 image 35 | Android 35 | Android 33 | 真实缺口；屏幕配置写入已执行，但镜像未变 |
| system_image 为空，只改顶层 API 35 | Android 35 | Android 33 | 同一缺口，默认拼包路径也受影响 |
| 只改 config_json.system.API 为35 | Android 33 | Android 33 | 顶层 API 仍33；嵌套元数据变化不改变有效镜像 |
| 只改 profile 引用与名称 | Android 33 | Android 33 | 下发时没有 profile 镜像覆盖 |

另一个由优先级直接得到的边界：如果 `system_image` 仍显式指向33，仅把顶层 API 写成35，有效镜像仍是33。因此“有效镜像与原生 AVD 一致”的守卫，不等于自动统一所有重复的 API/profile/config_json 标签。

## 本次最小收口范围与存量兼容

确定的最小收口范围是在 Agent 的 existing-AVD 分支、任何 config.ini 写入之前，核对真实 `image.sysdir.1` 对应的 SDK package ID 与有效 `system_image`。不匹配或不能确认时，结束本次启动并保留原实例/数据，不修改旧镜像指向，不删除或隐式新建。SDK 相对路径、绝对路径和 Windows 分隔符需要规范化后精确比较，不能做 substring 匹配；不改 HTTP schema、Server PATCH 或数据库结构。

保留 PATCH 是存量误配记录恢复正确要求的入口。历史上已经出现漂移的 VM，在守卫生效后可能从“启动了旧镜像”变为明确拒绝启动；应按真实 AVD 的镜像身份修回总账要求。不能把这个拒绝当作需要删除重建，也不能把先前 `details.system_image` 回显当作实际镜像证据。确实想换系统镜像时，应使用用户明确选择的新配置/空白实例语义，不隐式复用旧 userdata。

该守卫解决有效镜像与磁盘实例不一致；它不会自动修复 config_json.system、profile、顶层字段与显式 package ID 之间已经存在的重复元数据差异。上文保留修前确认与范围；修后证据与未覆盖项目如下。

## 镜像守卫修后阶段（f263af3，保留历史证据）

`f263af3` 只修改 Android manager，新增 `test_android_vm_existing_image.py`，Server/API/WS/schema/PATCH 和启动参数保持原样。

- `_ensure_avd` 在任何显示/硬件配置写入之前调用 `_verify_existing_avd_image`，检查列表中的唯一 Name/Path、实际 config.ini、唯一 `image.sysdir.1`，再读取解析后镜像目录内 `package.xml` 的唯一 `localPackage` ID，与有效请求值精确比较。
- 已经 ready/重新认领的 runtime 也通过原生 AVD 索引做同一核验；不符或未知不替换原 operation/request ID，不停机或删除旧实例。
- 本次尚未启动的新占位遇到镜像错误，只撤销本次占位，保留原磁盘。停止是否已确认仍按真实进程/端口事实记录，不把“没有本次 Popen”当作旧实例已经退出。
- 核验的是 SDK package ID，不是同包 ID 下的二进制、build 或 revision 等价。缺失 package 元数据、路径歧义或原生目录与受管写入目录不一致时会拒绝而保留数据；不迁移目录、不隐式重建。

[独立审查](native-followup/android-existing-image-independent-review.md)运行该专属文件得到 **52 passed**，覆盖镜像相同/不符/不可核实、SDK相对/绝对路径、Windows路径语法、符号链接/`..`、已有 runtime、占位撤销和修回正确包后复用原盘。该次审查的产品与测试 SHA-256 对应 `f263af3`，阶段核验保留在 [增量记录](final-schema-verification.json)。这些是临时 SDK/AVD 与假原生边界的隔离测试，Windows路径语法通过不代表 Windows实机验收；52项已纳入当时的 **1497 passed / 2 skipped / 73 warnings**，不重复累加，也不把旧源码指纹改写成最终版本。

该阶段的 **a5 原 `no_snapshot` 个人合成场景已通过**：真实 Server安装链路、下发启动、停止、原实例重启后页面 token/count 保留、最终停止及私有资源清理均有回执；总控独立核验启动前、确认后、重启后三张原图，页面数据与回执一致。证据：[a5 汇总](native-followup/android-app-install-a5-results.json)、[总控三图核验](native-followup/android-a5-root-review.json)。这是 `f263af3` 正常同镜像复用路径的原生通过，不是历史失败的唯一因果证明，也不是任意应用用户态数据持久性的保证。

## 最终容量保护与现场复验（2dc9844）

最终增量只修改 Android manager、更新1个镜像测试文件并新增容量测试文件；Server/API/WS/schema/PATCH 不变。复用已有 AVD 时，会读取并核实唯一的 `disk.dataPartition.size`：请求不大于已有容量时保留原值和表示，不把 SDK 已扩大的分区写回较小旧请求；容量未知或无法读取时拒绝修改/启动，保留原实例。镜像 package ID 守卫继续有效，重复标签不会因此自动统一。

最终统一隔离回归为 **1549 passed / 2 skipped / 73 warnings**、73个测试文件；33个产品源码 hash/Git blob 与 `2dc9844` 一致。证据：[统一回归](combined-regression-evidence.json)、[最终增量核验](final-schema-verification.json)。旧阶段计数与原始4例没有被覆盖，也不重复累加。

**a6-a3 已在 `2dc9844` 现场通过**，见 [完整结果](native-followup/android-snapshot-a6-a3-results.json)、[总控独立复核](native-followup/android-a6-root-review.json)：

- `save` 恢复原运行状态；`discard_changes` 不保存后来增加的 B 状态，仍恢复原 A；`cold_boot` 获得新 boot ID，不含旧 A/B 标记。
- 把同一原实例请求改为**确实已安装**的错误镜像，操作按预期失败，新增 native Popen 为0，原33个AVD文件字节不变；不属于“缺镜像预检提前失败”的替代验证。
- 改回原镜像要求后，原实例恢复启动；最终总账删除、原生目录/进程及私有端口清理均读回确认。

**a7 原个人 App 场景也在 `2dc9844` 通过**：真实安装链路、原始 `no_snapshot` 页面输入与确认、停止/原实例重启后 token/count 保留、WS重连与同host新会话/Manager重建、最终删除/清理均通过；四次生命周期操作均成功，总控三图与回执独立核验为 PASS。证据：[a7 结果](native-followup/android-app-install-a7-results.json)、[总控三图复核](native-followup/android-a7-root-review.json)。Manager重建不等于整个 Agent OS进程崩溃重启，该测试未覆盖后者。

这些结论限于已测个人 SDK/宿主和上述场景。**尚未覆盖 Windows原生、真机、任意App、外网/VLM/公司业务**；不承诺同 package ID 的任意二进制/revision等价、所有重复标签自动统一或所有原生场景均通过。
