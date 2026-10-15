# Android sync 屏障候选：独立审查

日期：2026-10-16。受审产品只修改 `backend/ai_phone/agent/android_vm/manager.py`，SHA256：

`c609f1b69eb7af48fa34ecaf757d1617d46e4afc3356e30344322d1fb521a0d0`

**结论：该精确源码候选没有剩余已复现的阻断；源代码层面可进入既定个人隔离 a5 验收。** 统一回归及 a5 原生结果另由总控收口；本报告不是原生通过证明，也不把该改动追认为 a2/a4 的唯一根因。

## 已核对的最终边界

- 普通 stop 在唯一受管 AVD 通道上发送一次固定 sync 命令，退出码 0 且 stdout 精确匹配本次内部 UUID hex 标记后，才继续原 Emulator 退出路径。标记不来自 VM ID、request ID 或消息自带参数；未增加重试或重新计算总预算。
- 空输出、旧标记、附加输出、非零退出、超时、迟到标记都不构成完成证据；这些路径保留实例身份和数据，不发送 emu kill/TERM/KILL。
- sync 前后均核对 AVD 身份；多个不同地址或进程端口未知时，在任何 sync/退出动作前拒绝歧义。普通 USB/TCP 真机不能进入该受管 VM 路径。
- 已明确退出的实例保持幂等，不要求对不存在的 guest 发送 sync；重认领的无 Popen 实例仍依赖精确进程与端口事实确认退出。
- `destroy=True` 只由内部明确删除路径调用；首次确实新建资源回滚经由该删除路径。旧实例启动失败收尾、普通 stop 与 stop_all 都使用保数据模式，不能从 stop 消息的 force/destroy 字段绕过屏障。
- 原 `emu kill`、SDK 退出窗口、单次总预算和精确退出验证保留；未引入 svc power shutdown，未改启动参数、快照策略、默认值、API、数据库或状态体系。

## 本次审查发现及修正

初版只统计成功验证的地址。若完整候选中有 5554/5556 两个精确 AVD 进程，5556 查询失败，就可能只同步 5554，随后仍 TERM 未同步的 5556 并返回成功。

[初始反例](android-sync-ambiguous-process-independent.json) 直接执行当时未修改的生产方法 AST，观察到第二个进程被终止。修后先检查完整候选地址与未知端口，两个歧义反例均在任何 sync/kill 前失败并保留身份；明确删除的原精确收尾能力未移走。

## 独立验证

- [17 个独立边界回放](android-sync-independent-replay.json) 使用当前生产方法 AST、假 ADB/进程/时钟，覆盖 ACK 误判、身份变化、多目标歧义、shell 不接收外部文本、已退出/重认领、stop_all 和明确删除。
- 冻结源码上的两份正式测试独立复跑：**139 passed，0 failed、0 skipped**。[日志](android-sync-independent-final.log)、[JUnit](android-sync-independent-final.xml)、[源码与测试哈希](android-sync-independent-final-manifest.json)。执行前后 manager SHA 一致。
- 实现负责人的五文件回归为 325 passed / 2 个既有 skip，见其单独记录；不与上述重叠集合相加。

独立运行使用空环境、临时 cwd/storage、内存 SQLite、假原生边界；仅已有端口测试使用自己创建的 socket。未执行 ADB、Emulator 或任何原生设备命令，未接触公司端点或封存 AVD。

## 不能扩大成的结论

`sync_completed` 只说明本次系统同步命令返回并得到标记，不证明底层介质没有 I/O 故障，也不证明应用用户态尚未提交的数据已经落盘。AOSP 旧 ADB 的 EOF/退出帧兼容风险不是现场 ARM64 ADB 已证实缺陷；现场版本的默认 EOF 处理已由另一份二进制证据校准。a4 的原始 localStorage 缺失失败继续保留，a5 必须用原页面和相同判据实际验收。
