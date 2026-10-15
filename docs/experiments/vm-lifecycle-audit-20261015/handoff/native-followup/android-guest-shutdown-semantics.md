# Android 停止：guest 关机、快照与 sync 的边界

日期：2026-10-16。只读核对产品 `8a2a3be1bb60f719282349e6135be3c226c3a7fe`、个人 SDK Emulator **36.6.11.0 / build 15507667 / macOS arm64 headless**、SDK adb、a4 原始材料和公开主源。没有启动 VM、执行 ADB/guest 命令、修改产品或已有 AVD。文件指纹和原始材料索引见 [android-guest-shutdown-evidence.json](android-guest-shutdown-evidence.json)。

## 有界决定

**本轮建议验证“普通 stop 先一次准确 guest sync，取得完成证据后再沿用原 emu kill”；不把所有 stop 改成 svc power shutdown。** 这是针对 guest 已提交写入的最小同步补强，保留现有快照退出方式。它不是任意 App 的保存协议，也尚未关闭 a2/a4 的数据失败。

完整 guest 关机与 `save=保留退出状态` 存在语义冲突，是放行阻断，不能用一次 `no_snapshot` 成功代替验证；不需要先证明每种快照必定损坏，才能承认这个冲突。

## a4 已证事实与未证因果

- 首次原页面 raw localStorage 读到原 token/count 1，重启后同 origin、同 key、同 WebView 109.0.5414.123 读到 null；missing=true，readError/parseError=null，documentReadyState=complete。App 私有 witness 的 SHA/inode/mtime 保留，重启没有补写。
- [a4 归档](android-app-install-a4-results.json) 中两次 `emu kill` 都 rc=0；PID 54738/55015 的准确 wait 分别约 1.718/1.544 秒，结果均 0，`fallback_signals=[]`。当前 20 秒窗口修正没有让产品提前发 TERM/KILL，仍复现数据缺失。
- 这不能证明 raw 值曾提交到 LevelDB、进入 guest 内核缓存或完成设备写入；也不能从 witness 保留反推 WebView 的另一份文件已经落盘。a4 的内核/数据库中间事件没有采到。

## emu kill 不等于 guest 正常关机

已核现场二进制，详见 [前轮二进制证据](android-native-exit-readonly.md)：headless `requestClose` 依据 FastSnapshotV1 分流，一支直接请求 QEMU HOST_UI shutdown；另一支尝试内部 `shell reboot -p`，5000 ms callback 不检查结果，随后也请求 QEMU shutdown。`-no-snapshot` 不是该处分支直接读取的条件；没有当时 effective feature 证据，不能追认 a4 走哪支。

API 33 对应的 [AOSP reboot.c](https://android.googlesource.com/platform/system/core/+/refs/tags/android-13.0.0_r1/reboot/reboot.c) 将 `reboot -p` 写为 `sys.powerctl=shutdown,shell` 后返回，直接进入 init；[svc PowerCommand](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-13.0.0_r1/cmds/svc/src/com/android/commands/svc/PowerCommand.java) 则调用 PowerManager shutdown(wait=true)。后者经 [ShutdownThread](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-13.0.0_r1/services/core/java/com/android/server/power/ShutdownThread.java) 发有时限的关机广播、收尾 ActivityManager/PackageManager，再走 [init/reboot.cpp](https://android.googlesource.com/platform/system/core/+/refs/tags/android-13.0.0_r1/init/reboot.cpp) 的服务停止、sync、umount 和 poweroff。init 自身也有超时及异常强关分支，所以不是无条件干净关机证明。

以上 Android 13 主源解释 API 级别设计；不是对现场 Google APIs 镜像 TE1A.240213.009 内每个 guest 二进制逐字节匹配。`svc` 等待本来就可能不正常返回，EOF/命令超时不能当关机完成；最终仍须准确实例退出事实。完整关机不能由 rc=0、ADB 消失或 boot_completed 推导。

## 为什么完整 guest 关机会影响现有快照

当前产品 `android_vm/manager.py` 的四种实际启动规则，与页面选择对应：

| 策略 | 参数 | 当前技术含义 |
| --- | --- | --- |
| save | 无禁用快照参数 | 可加载并保存退出时快照；页面写“保留退出状态” |
| discard_changes（默认） | `-no-snapshot-save` | 不保存本次快照，仍可能加载已有快照；不等于删除 userdata |
| cold_boot | `-no-snapshot-load` | 不加载，但可以保存本次快照 |
| no_snapshot | `-no-snapshot` | 不加载也不保存 |

本轮补取现场二进制的直接证据：

1. `hvf_handle_exception@0x10012ec8c` 调用 `arm_handle_psci_call`；后者匹配 ARM SYSTEM_OFF `0x84000008`，在 `0x10013c9b8–9bc` 传 cause=5 给 `qemu_system_shutdown_request`，然后进入 CPU off 路径。见 [HVF](android-guest-shutdown-static-hvf-exception.txt)、[PSCI](android-guest-shutdown-static-arm-psci.txt)。
2. [shutdown_request](android-guest-shutdown-static-shutdown-request.txt) 只记录该 cause 并通知主循环。现场 [main_loop](android-guest-shutdown-static-main-loop.txt) 在取出任意非零 shutdown request 后，先按 Android 模式及独立 `invalidate_exit_snapshot` 标志选择 quickboot save/invalidate；`0x1001ccfbc–cfc0` 调用 `androidSnapshot_quickbootSave(NULL)`。这一步没有先按 cause=4/5 区别保存；guest cause 的比较在之后 `0x1001ccffc–d008` 用于发送事件。
3. [Quickboot::save](android-guest-shutdown-static-quickboot-save.txt) 确有现存 liveness、FastSnapshot、命令选项、共享 RAM、短运行等条件；通过条件后，`0x1004e5c64–c68` 调 `Snapshotter::save(true, name)`。不存在可以在本轮证据中认定的“guest 正常关机总会自动回到关机前前台状态”。公开 [Quickboot.cpp](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/android-emu/android/snapshot/Quickboot.cpp) 仅辅助理解这些已核指令，不冒充当前版本逐字节源码。

因此 guest 先把 Activity/服务/CPU 关掉，再请求保存，与“捕获原运行中退出状态”有实际先后差异；可能保存关机中的状态、跳过保存或作废快照。具体哪一种以及下一次是否挂住，仍依赖 runtime 条件，**本轮没有实测，不能写成已发生故障**。这一不确定性已足够阻止直接全面替换为 svc。

## sync 能证明什么，不能证明什么

[Android 13 Toybox sync](https://android.googlesource.com/platform/external/toybox/+/refs/tags/android-13.0.0_r1/toys/lsb/sync.c) 是标准构建工具；[Android.bp](https://android.googlesource.com/platform/external/toybox/+/refs/tags/android-13.0.0_r1/Android.bp) 第 150/380 行分别编入实现并列入 `sync` symlink。它的无参数实现只调用 `sync()`；无需引入 App 包、root 或 REBOOT 权限。实际镜像上的存在与执行仍需后续个人隔离 native 验证。

[Android 内核 sync 实现](https://android.googlesource.com/kernel/common/+/refs/heads/android13-5.15/fs/sync.c) 的 `ksys_sync` 唤醒写回、等待 inode、文件系统和 block device 写回；该 syscall 最后固定返回 0。因此“命令完成”是经过这个同步路径的证据，**不是所有底层 I/O 均无错误、宿主物理介质绝不丢数据的证明**。

它不能强迫 Chromium/应用把尚在用户态队列的数据提交下来，也不会阻止 App 在 sync 返回到 emu kill 之间继续产生新写入。不能叫“完整 guest 关机”，不能承诺任意刚显示成功的业务数据已经保存。该点对当前 WebView 尤其明确：同版本 [WebView 启动代码](https://raw.githubusercontent.com/chromium/chromium/109.0.5414.123/android_webview/lib/aw_main_delegate.cc) 启用 aggressive DOM storage flushing 的原因就是不能依赖 shutdown 保存；一秒调度不是完成屏障。详见 [精确版本分析](android-webview-109-persistence.md)。

`svc` 也不会自动调用并等完任意 WebView 的 `LocalStorageImpl::ShutDown`。关机广播有时限，ActivityManager 等待的是 Activity 休眠/收尾，不是浏览器数据库提交回执。因此完整关机比 sync 多系统收尾步骤，但同样没有 App RAM 全保存保证。

## 命令回执必须区分具体 adb 版本

[AOSP 13 adb client](https://android.googlesource.com/platform/packages/modules/adb/+/refs/tags/android-13.0.0_r1/client/commandline.cpp) 的 `read_and_dump_protocol` 初始 exit_code=0，缺失 exit packet 而提前 EOF 也可能返回 0；legacy shell 根本不传远端退出码。

**现场 SDK adb 已改进这一点。** 只读提取其 ARM64 slice 并反汇编 [read_and_dump_protocol](android-guest-shutdown-static-adb-exit-receipt.txt)：`0x1000063b0` 和初次 Read 失败分支 `0x100006418` 均设默认 255；只有读到 kIdExit 才替换为远端码。因此不能把旧 AOSP13 的 EOF 问题归到现场 SDK 的 shell-v2 路径。这里只分析了 SDK adb，不代表其他 Agent 使用的工具版本或 legacy fallback。

跨 Agent 的小而稳妥的确认方式：**同一次非交互 shell 命令执行 `/system/bin/sync && printf ...`，标记由 Agent 新生成的安全字符 nonce 组成；要求 rc=0 且完整 stdout 精确等于本次标记。** sync 缺失、异常退出、断连、缺标记或超时都不能继续 emu kill；不加第二次 sync、不自动改用别的命令。标记只能证明 sync 命令已返回，仍不是其内部所有 I/O 没错误的回执。

## 最小修改与验证边界

- 保留原 Android VM manager/per-VM 锁/生命周期协议；仅准确受管 AVD 正常 stop（含已有实例启动失败的保数据收尾）增加一次 sync 屏障。真机路径不能进入。sync 与后续关闭、准确退出核验共用当前一次截止时间，不能各开 120 秒。
- sync 失败/超时立即返回本次错误，保留绑定、AVD、runtime 与资源未确认事实，不能掉进原 `graceful=False → terminate` 分支。显式 delete、对账删除以及有本次确实创建证据的首次绑定 rollback，可走原精确强制清理；不能靠 Server 首绑标志猜测 Agent 旧目录可删。
- sync 成功后沿用原 emu kill 和已修正的 SDK grace 窗口；不改 snapshot 参数、不额外 force-stop App、不加等待来制造测试通过。较长 sync 之后、emu kill 之前仍应核对当前 serial 的准确 AVD，防串台。
- 隔离单测必须覆盖：一次 sync 及顺序；错误/超时/rc0 缺标记不 kill；旧标记不认；身份不符零 guest 操作；共享 deadline；准确进程/端口未释放不成功；delete/新建 rollback 可跳 sync；已有数据启动失败不得跳过屏障或删盘。
- 原生验证沿用原页面、key、点击与读取，不给页面补 flush/delay。至少分开记录 no_snapshot 的磁盘数据保留、save 的实际前台/内存状态恢复、default discard_changes 在已有快照时的加载行为、cold_boot 的不加载行为；不能都在没有旧快照的新建 AVD 上替代。所有观察都留原失败，有限次验证不转成重复跑到通过。

本轮结论仅为**可测试的最小修正方向**。没有宣称 svc 或 sync 已在现场成功执行，没有宣称 a4 根因已经被证明，也没有凭源码把完整数据保留验收写成通过。
