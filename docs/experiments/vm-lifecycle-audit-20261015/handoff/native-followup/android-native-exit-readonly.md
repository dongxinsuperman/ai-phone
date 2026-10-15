# 现场 Android Emulator 退出路径：只读二进制证据

对象：个人 SDK 中的 **36.6.11.0 / build 15507667 / macOS arm64 headless**。仅执行 `nm`、`llvm-objdump`、文件读取和 SHA256；没有执行 Emulator、ADB、VM、guest 命令，没有改 SDK、配置、产品或停止策略。

最小证据入口：[摘录与地址](android-native-exit-disassembly.txt)、[二进制及日志 SHA256](android-native-exit-metadata.json)。相关函数和 ADB 参数解析片段另见 `android-static-*.txt`；不保留无关的完整镜像反汇编。

## 1. 现场版本确实仍有两种 requestClose 分支

`EmulatorNoQtNoWindow::requestClose()` 位于 `0x10059ea1c`，不是用旧 35.6.3 源码代替现场版本。

| 条件 | 该函数内实际行为 |
| --- | --- |
| `featurecontrol::isEnabled(7)` 为真 | 请求 SDK 后备退出计时；随后以 cause=4 调用 VM operations 的 `+0x100` 槽，最终进入 `qemu_system_shutdown_request` |
| 为假 | 请求同一个后备计时；构造 `shell`, `reboot`, `-p`，向 AdbInterface 提交，等待参数为 5000 毫秒；其 callback **不检查命令结果**，继续调用同一个 QEMU shutdown 槽 |

编号 7 已通过**同一二进制**的 `FeatureControlImpl::toString` 表确认是 `FastSnapshotV1`：表 `0x101a930d0 + 7*8` 的指针指向字符串 `FastSnapshotV1`。VM operations 槽也已按实际数据表解引用，`0x1019e4030 + 0x100` 指向 `0x10043fff4`，该 thunk 直接跳到 `qemu_system_shutdown_request@0x1001c52ec`。

控制台 `do_kill` 通过 LibuiAgent 的退出槽进入 `skin_winsys_quit_request`；headless 的主循环也有对 `requestClose` 的直接调用。这里没有把收到 `emu kill` 等同于 guest 已完成关机。

**`-no-snapshot` 不是 requestClose 这次分支判断直接读取的条件。** 安装包 `lib/advancedFeatures.ini` 的 `FastSnapshotV1 = on` 只是默认值；a2 启动参数有 `-no-snapshot`、`-no-window`，日志有“snapshot disabled，save ignored”，但这些仍不足以证明退出当刻的 feature 布尔值。a2 的私有目录中存在 `emu-last-feature-flags.protobuf`，本轮没有把未完成精确解释的缓存文件当作 effective feature 状态。不能从以上静态证据断言 a2 必定走哪支。

## 2. 20 秒可以被环境变量覆盖，且是后备强退预算

headless 的两个分支都传 `20` 给 `System::waitAndKillSelf`。实际链接的安装目录 `libandroid-emu-metrics.dylib` 中：

- `0x1b7d2c` 读取 `ANDROID_EMULATOR_WAIT_TIME_BEFORE_KILL`。
- `0x1b7d68` 调用 `atoi`，`0x1b7d70` **只在解析结果大于 0 时**用该值替换入参；空值或非正解析结果保留默认 20。
- 它构造同目录 launcher 的 **`-kill <pid> -sleep <N>`** 后备进程。注意参数是 `-sleep`。
- 实际 launcher 的 `handle_kill_command` 每次检查 PID、调用 `sleepMs(1000)`，预算耗尽后调用 `killProcess`；后者明确为 `kill(pid, 9)`。

所以这里的 20 秒是“若进程仍未退出，SDK 后备进程最终发 SIGKILL”的预算，**不是 Android/App 已完成落盘的承诺**；仅见日志也不能证明后备子进程创建成功。

a2 日志第 104、209 行分别对 PID 39771、39992 报出 20 秒。反汇编确认日志中的第一个秒数来自选中后的变量，末尾默认值才是字面量 20，因此能证明 SDK 当时**报告的有效预算是 20**，不能证明项目的后备信号实际触发。

本轮读取的项目 manager（SHA `0d4528d299c67ca244c207ae678cab8c616824d82ef38f12d21469a7e12beb0d`）则是：`emu kill` 返回码为 0 → `proc.wait(timeout=5)` → 仍存活才 `terminate()`（SIGTERM）→ 再等 5 秒仍不退出才 `kill()`（SIGKILL）。因此其首次干预最早约在成功回包后 5 秒，强制 kill 最早约 10 秒；**确实可能先于 SDK 的默认后备窗口**。实际相对时刻还受回包、UI/主循环处理、后备进程创建和宿主调度影响，不能写成固定精确的 5/10/20 秒时间线。

对齐这两个预算可以作为独立的健壮性问题讨论；**不能把静态预算不一致直接认定为 a2 localStorage 丢失根因**。本轮没有改策略，也没有给 a2 补造 terminate/kill 证据。

## 3. 私有 ADB 15037 有明确的支持路径

三段现场代码证据形成闭环：

1. headless 的 `AdbHostServer::getClientPort@0x100a07dd0` 读取 `ANDROID_ADB_SERVER_PORT`；有效 1–65535 返回该值，缺值回落 5037，因此 15037 在支持范围内。
2. 外部 `AdbThroughExe` 路径构造 `-s <serial>` 等参数；实际 `HostSystem::runCommand` 在 `0x1bb598` 调 `__NSGetEnviron`，把当前进程环境指针作为 `posix_spawnp` 的 envp 传下去。
3. 现场 SDK 的 `platform-tools/adb`（只反汇编 arm64 slice，未执行）在命令行处理器中读取 `ADB_SERVER_SOCKET`，另有 `ANDROID_ADB_SERVER_PORT` 的读取路径。没有显式 `-P` 并不等于必定连接 5037。

因此，**若上述私有变量确实进入 Emulator 进程，所核查的 SDK 外部 ADB 路径能够继承并使用私有 server 设置**。此外该版还包含运行时条件选择的 `AdbDirect` 后端；本轮没有证明 a2 退出当刻选中了哪种后端、哪份 adb 可执行文件，也未分析日志里另一份 Homebrew adb 的二进制。

a2 的 `device offline` / `device not found` 警告本身不足以证明内部 ADB 忽略 15037；也不足以证明 guest 的 `reboot -p` 已经成功。a2 的具体关闭分支、实际信号轨迹与 localStorage 丢失因果仍需分别保留为未知。
