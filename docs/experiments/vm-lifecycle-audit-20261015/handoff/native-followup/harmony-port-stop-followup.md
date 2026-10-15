# Harmony b1 后续：端口复用与接管实例退出证明

2026-10-16；基于 `d7cbac0` 的后续窄修。仅个人 worktree、fake 原生命令、自有临时 socket；本轮没有运行 Emulator/HDC/ADB，没有操作保留实例、公司服务或设备。

## 已确认与未确认

1. b1 的旧租约升级、同一原生 UUID 和 guest marker 读回已经成功；最后正常停止未能确认端口释放，保留了目录、registry 和租约，没有假报成功。
2. b1 失败窗口的同步证据是：进程句柄返回 5、HDC Offline、9 次端口检查不可 bind、16.676 秒未确认；没有捕获该窗口 TCP 状态，**不能把 b1 的确切原因写成已证明 TIME_WAIT**。
3. 在自有临时 socket 上已稳定复现：关闭连接后内核 TIME_WAIT 尚在，旧 Harmony plain bind 判占用，允许复用的原生 listener 实际能够重建。这是独立证实的判断缺口。
4. 对安装的 macOS Emulator 仅做静态反汇编，确认其 HDC bridge 在 bind/listen 前设置 `SO_REUSEADDR=1`；证据是 [路径、SHA256 和调用片段](harmony-native-listener-disassembly.txt)。这不是 Windows 原生验收。
5. 直接加 `SO_REUSEADDR` 不够：macOS 上 specific bind 可绕过已有 wildcard/dual-stack listener，Android 原先的 reuse-only helper 也有这个缺口。
6. 独立 fake 复现另确认：重连接管的 Harmony runtime 没有 Popen 句柄，原 stop 在 CLI 停止失败且精确实例进程仍活着时，可能只凭 HDC/端口误报停止，进而允许删除；旧误报还会写入 `cleanup_confirmed=True` 并绕过后续停止检查。

原始 b1：`/tmp/aiphone-personal-harmony-legacy-20261016b1/result.json`；43 秒后端口恢复空闲只能作事后证据，不能补造失败窗口事实。

## 最小变更

`agent/vm_ports.py` 供 Android manager、Harmony manager 与 capability 共用一个端口判断：

- 普通 bind 成功直接返回，不逐口 connect。
- Windows 继续使用 `SO_EXCLUSIVEADDRUSE`，没有 reuse 兜底。
- POSIX 只有普通 bind 报 `EADDRINUSE` 才尝试 reuse bind；先关闭该探针，再进行最多 100 ms 的 loopback connect；只有 `ECONNREFUSED` 放行，成功、超时或未知结果都拒绝。
- 首次 bind 的 EPERM/EACCES 等未知错误继续区分：manager 保守拒绝；capability 沿用原有“不把权限失败伪装成端口占用”的语义。启动仍会做实际检查。
- capability 每个端口检查启动 deadline，connect 上限也不超过剩余预算；普通空闲池不会多出连接探查。

“端口可用”只表示新原生监听能够建立，不能代替旧 VM 退出事实。停止仍保留 HDC 判据、原有 15 秒窗口及 Offline 连续四次规则；有 Popen 时仍核验退出，无 Popen 时在原生停止后再按既有实例名、路径、端口精确复核一次进程，只有明确不存在才确认。新增 `exit_evidence.instance_process_status`（absent/running/unknown）和采样时间。

显式 stop 不再直接信任 registry 的历史 `cleanup_confirmed` 位，统一走现有身份与通道检查；真正已停止仍 `not_running`，不再发原生 stop。无法确认的实例保留目录、registry 和待清理状态；没有新增状态机、重试循环或延长等待。

原注释所说“Offline target 永久排除后续租约”与实现不符，已改为诊断信息；租约规则未变化。

## 实验与回归

- [修正前 socket 证据](harmony-port-probe-before.json)：两组明确 TIME_WAIT 被旧 Harmony 判占用；wildcard/dual-stack 上 reuse bind 会成功，证明不能只换 socket 选项。旧文件的 scope 写“loopback only”不够精确：创建了自有 wildcard listener，但所有连接只去 loopback。
- [修正后 8 组实际 socket 矩阵](harmony-port-probe-after.json)：两组关闭连接可复用；specific/wildcard/dual-stack 的 LISTEN 和 bound 未 listen 均拒绝；三处判断完全一致。实验脚本 [harmony_port_probe_evidence.py](harmony_port_probe_evidence.py) 使用临时端口，不触碰固定 VM 端口。
- [独立矩阵](vm-port-independent-matrix.json) 以及 [已建立旧连接与新监听四元组隔离](vm-port-independent-established-reuse.json)：旧 accepted socket 不等于旧 listener，复用后旧、新连接各只收到自身 marker；无需增加全局 TCP 清理或 netstat 恢复机制。
- 正式新增 `backend/tests/test_vm_port_availability.py`：21 项通过，覆盖真实 loopback 关闭/占用、fake wildcard 结果、未知/权限、Windows 独占、探针关闭顺序和剩余 deadline；真实 wildcard/dual-stack 只在上述受控专项实验，不放宽总回归的 loopback 限制。
- `test_harmony_vm_agent_hardening.py` 新增无句柄的 absent/running/unknown × stop/delete，旧确认位遇未知/占用仍拒绝，以及实际已停止不重复发 stop 的用例。
- 当前六组定向回归 **257 passed**，含 Android/Harmony 管理器、旧租约桥接和独立原生进程反例；产品和新增测试 Ruff 无问题，`git diff --check` 无问题。独立 review 另补正式 `test_harmony_vm_stop_identity.py` 后由 root 纳入统一回归。

命令（worktree `backend/`）：

```text
.venv/bin/python -m pytest -q tests/test_vm_port_availability.py tests/test_harmony_vm_agent_hardening.py tests/test_harmony_vm.py tests/test_harmony_legacy_lease_upgrade.py tests/test_android_vm_manager_reliability.py ../docs/experiments/vm-lifecycle-audit-20261015/handoff/test_harmony_reclaimed_stop_independent.py --disable-warnings --maxfail=3
```

空闲路径微基准：同一个刚释放的自有临时端口反复 1000 次，plain bind 21 ms，共享 helper 31 ms；这是本机微基准，不能当成 Windows 或整台设备发现延迟承诺。兜底连接只发生于窄冲突分支，单次上限 100 ms。

## 兼容边界

Python 官方说明 POSIX `SO_REUSEADDR` 可复用 TIME_WAIT；Windows 官方说明其 `SO_REUSEADDR` 可能抢占地址，独占语义和关闭后的可重用时机不同。本次保留 Windows 独占，只有 fake 分支回归，没有 Windows native 证据：[Python socket 文档](https://docs.python.org/3/library/socket.html)、[Microsoft Winsock 文档](https://learn.microsoft.com/en-us/windows/win32/winsock/using-so-reuseaddr-and-so-exclusiveaddruse)。

端口探查始终是一个时间点的观察，原生创建仍可能遇到后续竞争；既有启动失败和资源保留契约负责该结果。本次没有替代原生所有权、数据、租约或设备注册规则。b2 真实验收由 root 单独授权执行，本报告不把隔离测试冒充原生通过。

## 统一回归后的旧夹具校准

产品提交 `9034204` 后，统一回归前 526 项中出现 5 个失败，原始日志由 root 保存为 `combined-post-port-first-run.*`。逐项确认是旧夹具缺少新增安全检查所需的事实，产品保持冻结，仅修改 `test_harmony_vm_identity.py` 和 `test_harmony_vm_recovery.py`：

- 延迟清理 A 的两例以前只把 B 塞进 manager 字典，没有登记其 managed route；现在提供只含 B 的精确 fake 进程清单并登记 B 的真实内存归属，继续禁止触碰 B 的 HDC，还额外禁止查询 B 的 HDC 和端口。
- 停止重试例原来始终声称原生实例存在却期望成功；现在只有第三次显式 fake native stop 后才转为实际不存在，并断言本次退出证据为 absent，前两次失败及 B 占用时完全不发操作的断言保留。
- 删除孤儿 crash helper 例提供三份不同扫描事实：准确实例不存在、删除前 helper 不在、删除后 orphan 出现；显式桩 HDC 查询，断言采样顺序及 orphan 退出。
- crash helper 查询失败不阻断的例提供独立已退出 Popen 证据；无句柄且进程未知仍拒绝的正式反例保持不变。

严格沿用统一 runner 的 I/O guard，只改测试名单与输出文件：两文件 **18 passed**；全部 `test_harmony_vm*.py` 共 **178 passed**，包括 HTTP/WS E2E。两轮产品 source hashes 均不变、`blocked_calls=[]`、`allowed_subprocesses=[]`，临时 runtime 已删除；计数互有重叠，不相加。执行入口 [run_harmony_fixture_regression.py](run_harmony_fixture_regression.py)，证据为 handoff 的 `harmony-fixture-focused.*` 与 `harmony-fixture-all.*`。

## 最终统一与原生复验

根 Agent 的最终 70 文件统一回归为 **1377 passed / 2 skipped**，33 个产品源码与测试前后摘要一致，保护器无越界拦截。此计数包含前述正式用例，不能再与分组数字相加。

最终产品 `9034204` 的个人原生 **b2 完整通过**，并由 root 独立核验[原始摘要](harmony-legacy-b2-summary.json)及[核验记录](harmony-b2-root-review.json)。三次故障窗口观察直接捕获 10001 的 TIME_WAIT：普通 bind 失败、复用 bind 成功、connect 被拒绝、无监听者，新产品判据为可用；随后原实例使用同端口成功重启，原 UUID 和随机数据仍在。新 Manager 会话以 `process=None` 认领运行实例后，普通停止提供准确进程缺席证据，保存的租约行确实删除，最后一次原生删除与 Server 404 均完成。

b1 仍为失败历史，其原始结果、事后端口观察和独立补删回执保存在 [b1 归档索引](b1-evidence/archive-index.json)；不把 b2 捕获的窗口倒写成 b1 当时已捕获。
