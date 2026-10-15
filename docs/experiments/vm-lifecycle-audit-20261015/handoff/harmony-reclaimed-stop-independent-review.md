# P1：鸿蒙无进程句柄实例停止后缺少进程复核

审查日期：2026-10-16。**当前收口状态：两个窄修均通过独立复核；正式 8 条安全回归加现有 81 条 hardening 检查，共 89 passed，未发现这两个修复的剩余阻断。** 具体修后代码指纹与证据见第 6 节。

修前已独立复现：从 registry 恢复或已重新认领的 runtime 没有 Popen 句柄时，Emulator 停止命令失败、准确匹配的实例进程仍存在，冻结旧代码仍返回 `ok=True / cleanup_confirmed=True`；删除接口随后继续执行原生删除。这是停止成功的判据缺失，不是日志文字或测试超时问题。

本次仅写反例、日志和交接记录，没有修改产品 Manager；没有执行真实 Emulator/HDC/ADB、进程枚举或公司访问。因为鸿蒙负责人正在修改同一文件，测试对三个当前鸿蒙模块和现有 fixture 做了冻结快照，避免并行修改污染证据。

## 1. 反例结果

四个用例都使用真实 `stop_sync`、`delete_sync`、`_runtime_from_known`、`_active_instance_serial` 和 `_wait_removed`。只替换宿主/原生命令的返回事实：

- 精确名字、instancePath、HDC port、lease token 都匹配；没有通过身份不一致绕过上游保护。
- 假进程清单始终包含这台精确实例的 Emulator。
- 假 `Emulator -stop` 返回 rc=1，没有终止进程。
- 假 HDC 清单为空，假端口 bind 检查为可用；**没有把 `_wait_removed` 直接替换成成功**。
- crash helper 清理和 fport 移除用无副作用桩替代，避免触碰其他进程。

| 入口及 runtime 来源 | 生命周期内部准确进程查询次数 | 实际进入的假原生命令 | 返回结果 | 测试中的实例进程/私有定义 |
|---|---:|---|---|---|
| stop：仅有 registry，临时恢复 runtime | 1，仅停止前 | `-stop` | `ok=True`、`cleanup_confirmed=True` | 进程仍活、定义保留 |
| stop：已认领 runtime 在内存中，`process=None` | 0 | `-stop` | `ok=True`、`cleanup_confirmed=True` | 进程仍活、定义保留 |
| delete：仅有 registry | 1，仅停止前 | `-stop` → `-delete` | `ok=True`、`reason=deleted` | 进程仍活，假删除函数已删掉 pytest 私有定义 |
| delete：已认领 runtime 在内存中，`process=None` | 0 | `-stop` → `-delete` | `ok=True`、`reason=deleted` | 进程仍活，假删除函数已删掉 pytest 私有定义 |

上述每条的安全断言都是“活进程存在时不能确认停止/不能继续删除”，实际均失败：**4 个独立安全反例，不是 4 个通过用例。** 删除掉的是测试临时目录中的假 `config.ini`，没有删除真实虚拟机；它证明的是产品控制流已经允许执行删除命令。

汇总：[完整观察 JSON](harmony-reclaimed-stop-evidence.json)；[独立安全回归](test_harmony_reclaimed_stop_independent.py)。日志分别为：

- [registry → stop](harmony-reclaimed-stop-registry-stop-regression.log)
- [reclaimed → stop](harmony-reclaimed-stop-reclaimed-stop-regression.log)
- [registry → delete](harmony-reclaimed-stop-registry-delete-regression.log)
- [reclaimed → delete](harmony-reclaimed-stop-reclaimed-delete-regression.log)

## 2. 为什么现有上游检查挡不住

下列行号对应冻结 Manager SHA256 `f9927f728d5efb185cfddc25de9fcb530b57c137c62926ba8458daa3a1c105f5`；后续负责人修复会改变工作树行号，应以[冻结文件清单](harmony-reclaimed-stop-manifest.json)及[快照位置](harmony-reclaimed-stop-snapshot.txt)回看。

1. `stop_sync` 第 1165 行在 registry 恢复分支调用 `_active_instance_serial`，这只能证明**停止前**该实例确实在运行；它不是停止后的退出证据。
2. `_runtime_from_known` 第 1914–1936 行没有填入 `process`，所以从 registry/认领链路得到的 runtime 保持 `process=None`。已有 runtime 进入 stop 时不再走前一条进程检查。
3. `_stop_runtime_locked` 第 1281–1300 行将 native-stop 失败放进 `stop_errors`，没有因此直接失败；这种“以最终事实为准”的原则本身可以保留。
4. 第 1332–1346 行先采信 HDC/端口清理结果，然后只在 `runtime.process is not None` 时检查 `poll()`；句柄为空时没有补查准确实例进程。
5. `_wait_removed` 第 2132–2143 行只查通道和端口；HDC 不存在且端口可 bind 就成功，或 Offline+端口可用累计达标就成功。**控制端口释放不能单独证明 Emulator 进程已退出。**
6. 第 1365–1374 行把假成功持久化为 `cleanup_pending=False / cleanup_confirmed=True`；第 1379 行又直接令 `ok=cleanup_confirmed`。
7. `delete_sync` 第 1415–1417 行只检查 stop 的 `ok`，之后便进入第 1444 行的 `-delete`。没有第二道独立的进程退出检查。

已有 `test_owned_process_still_running_prevents_false_stop_confirmation` 仅覆盖持有 Popen 的分支；本次用现有 hardening fixture 的 `_runtime` 及禁真实工具保护，并通过真实 `_runtime_from_known` 去掉句柄，补出了这条未覆盖分支。内存来源用例模拟认领**完成后的 runtime 形态**，没有声称运行过真实认领/driver 握手。

## 3. 最小修正建议

在 `_stop_runtime_locked` 把清理结果写入 registry 和返回成功之前，补充准确实例的**停止后**进程事实：

- `process=None` 时，使用精确 VM ID、原生名字、instancePath、HDC port 再核实实例进程。
- 只有查询成功且明确返回“该实例不存在”，同时现有 HDC/端口清理成立，才能设 `cleanup_confirmed=True`。
- 找到仍在运行的精确实例，返回未确认；查询失败/身份无法核实也返回未确认。尤其不能写成 `if not serial`，因为 helper 的 `None` 是未知，空字符串才是明确缺席。
- 把本次进程核查结果、时间和原因放进已有 `exit_evidence`；保留 `stop_errors`，不因 CLI 文本报错就覆盖已经明确的退出事实，也不因 HDC 端口释放就反向推断进程退出。
- 保持当前 `delete_sync` 的 stop gate：stop 未确认就不下发 `-delete`，不用另建重试流程，也不要按端口或进程名全局杀进程。

假成功还会污染后续幂等路径：已落盘的 `cleanup_confirmed=True` 会让第 1143–1162 行直接返回 not_running。修复时应同时判断是否允许继续信任缺少进程退出证据的旧缓存；不能只修本次 stop，却让过去错误写入的确认位永久绕过复核。

## 4. 回归范围

至少保留以下覆盖：

1. 本文四条反例全部转绿：无句柄的 registry/reclaimed × stop/delete，活进程必须失败且保留定义。
2. 同样的无句柄入口，停止后准确进程确实消失且通道清理成功，应允许成功；CLI 的“原本不在运行/No target available”可只保留为诊断。
3. 停止后进程查询返回未知、身份不匹配或权限错误，不能确认清理或进入删除。
4. 现有持有 Popen 的“仍活/已退出”两条检查不能退化。
5. 保留 `_wait_removed` 关于 HDC absent、稳定 Offline、端口仍忙、查询失败及预算的原有测试；不要用进程检查替代端口释放。
6. 明确处理既有 `cleanup_confirmed=True` 但缺少可靠进程证据的 registry 情况。

## 5. 隔离与后续状态

每个反例都启用现有 hard-I/O guard，禁止网络、设备命令和现有项目环境文件。每轮均为 `1 failed`，冻结产品指纹在执行前后不变；唯一导入阶段 IPv6 bind 尝试被守卫拦截，没有真实监听。原始失败日志保留，不覆盖为修后结果。

本报告和最初反例已即时发送总控和鸿蒙负责人，后续没有覆盖原失败日志。产品修改由鸿蒙负责人完成；本审查只新增独立正式测试和证据，没有编辑 Manager。

## 6. 修后独立复核

当前 Manager SHA256：`efac19845e45b5c0a7aa2faefc95f8c6dc9cecd6842442fee303765dc9a3999e`。审查结束再次计算与被测文件一致。

已核对两处修复：

1. 无 Popen 句柄时，在 HDC/端口清理后使用既有 `_active_instance_serial` 重新核实准确实例；running/unknown 均不能确认停止，只有明确 absent 才可继续，结果写入 `exit_evidence`。
2. 删除历史 `cleanup_confirmed=True` 的直接快返；显式 stop/delete 重新核对当前进程和通道事实，确认本来就不在运行时仍返回 not_running，不必再发送一次 native-stop。

正式回归文件：[test_harmony_vm_stop_identity.py](../../../../backend/tests/test_harmony_vm_stop_identity.py)，含最初 4 条反例，以及“旧确认位为 True，但进程仍活/不可查 × stop/delete”4 条历史记录回归。后四条在修前冻结代码上也分别失败：停止不查进程就成功，删除直接进入 `-delete`；修后全部通过。

本次只将**正式 8 条**计入最终统一回归，不再重复计入 handoff 中内容相同的旧 4 条。与当前 hardening 文件联跑的独立结果为 **89 passed**，证据：[修后日志](harmony-reclaimed-stop-fixed-regression.log)、[JUnit](harmony-reclaimed-stop-fixed-regression.xml)、[隔离记录](harmony-reclaimed-stop-fixed-isolation.json)、[修后源码指纹](harmony-reclaimed-stop-fixed-manifest.json)。全部目标函数仍是真实产品链路，没有替换 `_active_instance_serial`、`_wait_removed`、stop/delete 本身；外部原生事实使用假对象。

补充旧确认位的四份失败日志以 `harmony-reclaimed-stop-original-legacy-*` 保留，完整观测并入[汇总证据](harmony-reclaimed-stop-evidence.json)。最初一次额外旧版测试的 pytest 路径导入选择了当前代码，已单独归档为 `import-calibration-*` 并排除统计；正式修前重放把测试也复制到冻结快照内，确认四项安全断言均失败。

本结论仅覆盖这两处退出事实判定与旧缓存保护，不等于新增实机停止/删除验收。是否继续个人原生验收由总控决定，本审查没有发起原生命令。
