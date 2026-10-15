# Android 停止与数据保留：独立只读判断

日期：2026-10-16。产品核对到 `6da8a95`（Android 停止实现仍为 `9034204` 所含逻辑）；未修改产品，未运行 ADB、Emulator 或其他原生命令。只读 a2 既有材料，并读取 Android Developers、AOSP 与 W3C 主源。

## 判断

**a2 是尚未关闭的跨停止/启动数据验收失败；同 AVD、同 APK、没有 wipe/delete，不能替代页面数据保留通过。当前证据也不能把它归因为本轮端口修正。**

当前可以承诺的是：普通 stop 不删除或重建 AVD、用户数据镜像、已装应用及绑定；它确认精确实例进程退出和端口可复用。不能由这个回包推导“应用刚显示成功的所有写入已经提交到持久介质”。`wipe_data=false` 且不回载旧快照是讨论正常数据保留的前提；`no_snapshot` 不等于清空 userdata，也不提供应用写入提交屏障。

## 直接证据

1. 产品 `AndroidVmManager._stop_sync_locked` 先校验真实 AVD 名，再调用 `adb -s <serial> emu kill`；该命令返回后，对已知准确进程最多先等 5 秒，必要时 terminate/kill。最终成功条件只有 `_runtime_exit_confirmed` 的进程和端口事实，没有应用提交或 guest 正常关机证据。`delete_sync` 和启动失败清理也复用此 stop，所以不能仅在共用 helper 里换一条命令而认为其他语义不变。
2. [Android Emulator 官方命令文档](https://developer.android.com/studio/run/emulator-commandline) 将持久 userdata 与 Quick Boot 区分：`-no-snapshot` 不加载、也不保存快照；`-wipe-data` 才明确清空应用和设置。未存入磁盘的运行内存不会因保留 userdata 文件而得到保护。
3. [官方 console 文档](https://developer.android.com/studio/run/emulator-console) 将 `kill` 定义为结束虚拟设备，`adb emu` 是发出后返回的接口。命令回执不是 guest 正常关机完成证明；它也不能直接等同 SIGKILL。
4. [公开 Emulator console.cpp](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/android-emu/android/console.cpp) 的 `do_kill` 调用 libui `requestExit`；[headless requestClose](https://android.googlesource.com/platform/external/qemu/+/refs/heads/emu-master-dev/android/android-ui/modules/aemu-ui-headless/src/android/skin/qt/emulator-no-qt-no-window.cpp) 在 FastSnapshotV1 分支直接请求 HOST_UI 原因的 QEMU 退出，另一分支先尝试内部 `adb shell reboot -p`，再退出。这证明退出机制有分支，**不能追认 a2 一定绕过/一定完成了 guest 关机**：本次读到该公开分支的 source.properties 是 35.6.3，而 a2 是 36.6.11 build 15507667；尚未获得该实际二进制到公开源码提交的对应关系。
5. [W3C Web Storage 第二版 §4.1](https://www.w3.org/TR/2021/SPSD-webstorage-20210128/#the-storage-interface) 明确不要求 setItem 等方法等待物理磁盘写入。它支持“页面回读不等于 fsync”的接口边界；不证明当前 WebView 版本采用某个具体延时，也不证明 a2 的唯一原因就是缓冲。

## svc power shutdown 能做什么

[AOSP Android 13 PowerCommand](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-13.0.0_r1/cmds/svc/src/com/android/commands/svc/PowerCommand.java) 调用 PowerManager 的 shutdown，参数要求等待关机。[PowerManagerService](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-13.0.0_r1/services/core/java/com/android/server/power/PowerManagerService.java) 需要 REBOOT 权限，AOSP Shell 已声明该权限；服务未就绪、ADB 不可用或 guest 卡死时仍可能无法完成。

正常链路进入 [ShutdownThread](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-13.0.0_r1/services/core/java/com/android/server/power/ShutdownThread.java)，先发有时限的关机广播，再关闭系统服务；[init/reboot.cpp](https://android.googlesource.com/platform/system/core/+/refs/tags/android-13.0.0_r1/init/reboot.cpp) 关闭卷、sync、尝试 umount，最终关机。它比只证明 Emulator 已退出提供更明确的 guest 收尾路径，但仍不能保证任意应用没有提交到系统的内存状态。

**不能一行替换 `_kill_emulator`。** 正常关机可能先断开 ADB/Binder，shell 返回码不可靠地代表最终关机结果；AOSP 本身处理关机期间的 RemoteException。若命令非零就立刻 terminate，或仅给 guest 5 秒就强制结束，就可能截断新引入的正常关机流程。

## 最小纠正边界

- 先完成不改变生产 stop 策略的双轨诊断：页面写入前，对同一 App 的独立私有 witness 写入并 fsync；原页面、原 key、原 no_snapshot/wipe 参数保持；不在页面写入后补 sync、force-stop 或额外等待来制造通过。
- witness 保留而 localStorage 丢失，只能把范围缩到应用/WebView 提交或恢复路径，仍需精确 provider 版本和存储证据；不能直接认定某个缓冲时长。两者均丢失则继续查数据镜像/启动恢复链。两者本轮通过也不能抹去 a2 的失败。
- 记录实际 `emu kill` 返回/超时、准确进程等待时长，以及是否进入 terminate/kill；a2 没记录这些分支，不能倒推。只记录，不改变关机时序。
- 如果之后决定把普通 stop 提升为 guest 正常关机：只在受管 Android VM、真实 AVD 身份校验后发一次 guest 请求，在同一次预算内核对精确退出；失败结束本次操作，不自动重试或重启。不能把 ADB 断线或 rc=0 当成功。
- 普通停止、已有数据的启动失败收尾，与显式 delete/首次确实新建资源 rollback 的目的不同。共用停止函数必须保留这种内部意图区别；不能要求坏 guest 在删除/回滚前一定能正常关机，也不能把准确强制清理说成应用数据持久化成功。无需新增对外架构或重试工作流。
- 不改真机通用驱动、扫描、睡眠/唤醒路径；`svc power shutdown` 对真机也会真的关机，必须始终留在当前受管 AVD 的身份边界内。

对用户应准确说：**“停止会保留这台机器的配置、已装应用和数据盘，再次启动继续使用原实例；不会通过删机重建来恢复。应用尚未完成持久化的写入不能只凭停止成功来保证，这次页面数据丢失仍在核查，尚不能宣称完整数据保留验收通过。”**

原始公开源码文本保存在 `/tmp/android-shutdown-primary/`；AOSP Android 13 标签是对应 API 级别的设计依据，不冒充该 Google APIs 系统镜像逐字节源码证明。a2 原生材料仅引用已保存的 `handoff/native-followup/a2-readonly-findings.md`。
