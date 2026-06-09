# Agent 虚拟机环境准备（Android Emulator）

> 目标：让一台 **Agent 宿主机**具备跑 Android Emulator 的能力。环境装好后，「虚拟机」页创建的虚拟手机就能下发到这台 Agent 上启动，**启动后作为一台普通 android 设备进入设备池，被调度执行任务**（与真机同一条执行链路）。
>
> 你（本机）只需要按本文把**环境**装好并启动 Agent；建 AVD、起模拟器、上报状态都由 Agent 自动完成（见 §6）。
>
> 适配代码：`backend/ai_phone/agent/android_vm/`（`capability.py` 工具发现/探查、`manager.py` 生命周期）。本文命令路径与代码里的发现逻辑一一对应，照做即可被自动识别。
>
> **安装口径以本文为准**：Agent 环境不是按某台机器当前状态“凑齐”，而是按本文镜像矩阵安装。设备库里会出现不同 Android 版本的真实设备档案，缺哪个 API 的 system image，就会在下发对应机型时探查失败。

---

## 1. 适用范围与硬性前提

- **操作系统**：macOS（本仓库 Agent 主力，§2–§10 以 macOS 为例）。Linux 亦可（emulator 支持）。**Windows 见 §11 专章**（工具发现已跨平台适配，原理一致、路径/文件名按 Windows 习惯）。
- **CPU 架构（关键）**：
  - Apple Silicon（M 系列）→ 必须用 **`arm64-v8a`** 镜像。
  - Intel → 必须用 **`x86_64`** 镜像（依赖 Hypervisor.framework / 虚拟化）。
  - ⚠️ ABI 必须与宿主一致：Agent 探查时按宿主架构（`host_abi()`）匹配镜像——M 芯片只认 `arm64-v8a`，Intel 只认 `x86_64`。**装错 ABI 的镜像 = 探查直接失败**。
- **内存 / 磁盘**：每台模拟器约 2–4GB 内存；SDK + 1–2 个 system-image 约需 10–20GB 磁盘。建议 16GB+ 内存。
- **同时实例数**：**不拦截**——能起几台由你机器实际资源决定。内存偏低时探查只给**风险提醒**（"已运行 N 台、可用内存偏低"），是否下发你自己定（不弹二级确认）。不做任何硬性数量/内存兜底。

---

## 2. 安装 JDK（avdmanager / sdkmanager 依赖 Java）

```bash
# macOS（Homebrew）。任选其一可用的 JDK 17+
brew install --cask temurin
# 验证
java -version
```

---

## 3. 安装 Android SDK 命令行工具（cmdline-tools）

推荐**纯命令行**方式（无需 Android Studio GUI，适合 Agent 宿主）。也可直接装 Android Studio（自带 SDK Manager），效果相同。

### 3.1 放置 cmdline-tools

从官方下载 “Command line tools only”（macOS）：<https://developer.android.com/studio#command-line-tools-only>

解压后**目录结构必须是** `cmdline-tools/latest/bin/sdkmanager`：

```bash
export ANDROID_HOME="$HOME/Library/Android/sdk"
mkdir -p "$ANDROID_HOME/cmdline-tools"
# 把解压出的 cmdline-tools 目录改名为 latest 放进去：
#   $ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager
#   $ANDROID_HOME/cmdline-tools/latest/bin/avdmanager
```

### 3.2 环境变量（写入 `~/.zshrc` 后 `source ~/.zshrc`）

```bash
export ANDROID_HOME="$HOME/Library/Android/sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator"
```

> 工具与镜像发现（`capability._sdk_roots` / `_find_tool` / `_scan_system_images`）：
> ① 先定位 SDK 根——`ANDROID_SDK_ROOT`/`ANDROID_HOME` →（从 PATH 里的 emulator/avdmanager/sdkmanager/adb 反推所属 SDK）→ 默认 `~/Library/Android/sdk`、`~/Android/Sdk`；
> ② 工具**优先从 SDK 根取**，`which`(PATH) 只兜底——避免误用 Homebrew 等装在 PATH、却指向另一个空 SDK 的 sdkmanager；
> ③ 列镜像**直接扫各候选 SDK 的 `system-images/` 目录**（实地、与 sdkmanager 无关），扫不到才退回 `sdkmanager --list_installed`。
> 所以装在默认路径 `~/Library/Android/sdk`、或配了 `ANDROID_HOME`、或 PATH 里有对的 emulator，都能被正确发现。

---

## 4. 安装 platform-tools / emulator / system-images

```bash
# 1) 接受全部 license（必须，否则 avdmanager create 会失败）
yes | sdkmanager --licenses

# 2) 核心组件
sdkmanager "platform-tools" "emulator"

# 3) 系统镜像（按宿主 ABI + API 版本 + 类型）
#    推荐完整兼容性覆盖：API 21 / 23 / 26 / 28 / 30 / 33 / 34 / 35 / 36。
#    Apple Silicon（M 芯片）：
sdkmanager \
  "system-images;android-21;google_apis;arm64-v8a" \
  "system-images;android-23;google_apis;arm64-v8a" \
  "system-images;android-26;google_apis;arm64-v8a" \
  "system-images;android-28;google_apis;arm64-v8a" \
  "system-images;android-30;google_apis;arm64-v8a" \
  "system-images;android-33;google_apis;arm64-v8a" \
  "system-images;android-34;google_apis;arm64-v8a" \
  "system-images;android-35;google_apis;arm64-v8a" \
  "system-images;android-36;google_apis;arm64-v8a"

#    Intel 宿主：把 arm64-v8a 换成 x86_64，API 版本清单保持一致。
```

要点：

- **镜像坐标格式必须是** `system-images;android-<API>;<type>;<abi>`，与代码 `default_system_image()` 完全一致。
- `<type>`：前端默认 **`google_apis`**（带 Google API，通用测试推荐）。若要测纯净 AOSP，再加装 `...;default;...`。
- `<API>`：**装了哪个版本的镜像，就只能起哪个版本的机型**。设备库可创建口径是 **API 21 起（Android 5+）**——这是本系统当前的产品口径（低于 API21 的老机型不进可创建模板），不是说 Emulator 没有更老镜像。
- **镜像覆盖建议**：只装一个镜像 = 只保障那一个系统版本；点其它版本的机型探查会"正常失败（缺镜像）"。推荐覆盖 `android-21 / 23 / 26 / 28 / 30 / 33 / 34 / 35 / 36`（都带对应宿主 ABI）。其中 `android-34` 是 Android 14，设备库按 Android 14 筛出的机型会明确依赖它，不能省略。
- 想用别的机型 / 版本组合时，按需补装对应 `system-images;...` 即可（缺哪个探查会明确提示缺哪个）。

---

## 5. 验证环境（装完先自检）

```bash
adb version
emulator -version
avdmanager list avd
# 关键：必须能列出你装的镜像，否则 Agent 会判“不可用”
sdkmanager --list_installed | grep system-images
```

> Agent 探查（`probe_android_vm_capability`）的判定：缺 `adb/emulator/avdmanager` → 不可用；宿主架构与目标 ABI 不符 → 不可用；**列不出已安装 system-image（缺 sdkmanager 或没装镜像）→ 不可用**。所以第 4 行能看到镜像，是探查通过的前提。
>
> 按本文完整安装后，Apple Silicon 至少应能看到这些镜像：
>
> ```text
> system-images;android-21;google_apis;arm64-v8a
> system-images;android-23;google_apis;arm64-v8a
> system-images;android-26;google_apis;arm64-v8a
> system-images;android-28;google_apis;arm64-v8a
> system-images;android-30;google_apis;arm64-v8a
> system-images;android-33;google_apis;arm64-v8a
> system-images;android-34;google_apis;arm64-v8a
> system-images;android-35;google_apis;arm64-v8a
> system-images;android-36;google_apis;arm64-v8a
> ```

---

## 6. 与 Agent 的对接（无需手动建 AVD）

环境就位后，**不要手动 `avdmanager create`**。Agent 收到「下发」后会自动完成整条生命周期：

1. `avdmanager create avd -n aiphone_vm_<vmid> -k "<system_image>" --force`（按所选机型/系统）。
2. 写入屏幕分辨率 / density / RAM 等配置。
3. `emulator -avd aiphone_vm_<vmid> -port <p> -no-window -no-audio …` 启动（默认无头）。
4. `adb` 等待 `sys.boot_completed` → 上报 `running` → 进入设备池（打 `virtual` 标）。
5. 停止 → `adb emu kill`；删除/换绑 → `avdmanager delete avd`（自动清远端 AVD，见主方案 §20.6）。

你需要做的只有两件：**把本文环境装好** + **启动 Agent 并确认连上 Server**。

> 开机预置：虚拟机启动后会自动设成**中文 `zh-CN` + 时区 `Asia/Shanghai` + 关动画/24 小时制**（默认国内中文机、对自动化友好），由 Server 下发控制、Agent 无需配置。其中中文需镜像可 `adb root`（`google_apis`/`default` 可，别用 `google_play`）。

---

## 7. 虚拟机行为参数：**Agent 端无需配置**

虚拟机的运行行为（并发上限、内存余量、无头、超时、密度、孤儿清理等）已全部由 **Server 端集中控制并下发**，**Agent 机器一个都不用配**。本环境准备不涉及这些；要调统一在 Server 端改。细节见代码 `config.py` 的 `android_vm_*` 字段。

排障用：模拟器运行日志在 `backend/<storage_dir>/vm_runtime/<vmid>/emulator.log`（`storage_dir` 默认 `./.data/storage`）。

> 注：模拟器默认**无头**运行（宿主看不到窗口属正常），平台画面靠 scrcpy 投屏抓取——**看不到窗口 ≠ 没起来**。

---

## 8. 端到端验收（环境装好后跑一遍）

1. 装好环境，启动 Agent，确认在 Server 端在线。
2. 前端「虚拟机」→ 选一台机型 → **创建配置**（右侧出现配置卡片）。
3. 卡片上 **探查**：应能看到这台 Agent 且“可用”。若“不可用”，看 `reason`：
   - `缺少 Android SDK 工具：…` → §3 没装对 / PATH 未生效。
   - `未发现已安装 Android system image` / `缺少 sdkmanager …` → §4 镜像没装或 sdkmanager 不在。
   - `宿主架构 X 与目标 ABI Y 不匹配` → 装了错 ABI 镜像（见 §1）。
4. **下发** → 状态 `启动中` → `运行中`。
5. **设备总览**出现该虚拟机（带 `virtual` 标）→ 可被调度执行任务。
6. **停止 / 删除**（删除会自动清理远端 AVD）。

---

## 9. 常见问题（FAQ）

- **探查“缺少 avdmanager”**：cmdline-tools 没放成 `cmdline-tools/latest/bin/`，或 `ANDROID_HOME`/PATH 没设。
- **`avdmanager create failed`**：license 没接受（`yes | sdkmanager --licenses`），或对应 `system-images` 没装。
- **探查“ABI 不匹配”**：M 芯片别装 `x86_64`；Intel 别只装 `arm64-v8a`。
- **emulator 黑屏/起不来**：Intel 需开启虚拟化；M 芯片确认装的是 `arm64-v8a`；查看 `vm_runtime/<vmid>/emulator.log`。
- **启动超时**：调大 `AI_PHONE_ANDROID_VM_BOOT_TIMEOUT_SEC`；首次冷启动较慢属正常。
- **Java 报错**：未装 JDK 或版本过低，装 JDK 17+。

---

## 10. 一页速查（macOS / Apple Silicon）

```bash
# 1) JDK
brew install --cask temurin && java -version

# 2) cmdline-tools 放到 ~/Library/Android/sdk/cmdline-tools/latest/
export ANDROID_HOME="$HOME/Library/Android/sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator"

# 3) 组件 + 镜像（完整兼容性覆盖，Apple Silicon / M 芯片）
yes | sdkmanager --licenses
sdkmanager "platform-tools" "emulator" \
  "system-images;android-21;google_apis;arm64-v8a" \
  "system-images;android-23;google_apis;arm64-v8a" \
  "system-images;android-26;google_apis;arm64-v8a" \
  "system-images;android-28;google_apis;arm64-v8a" \
  "system-images;android-30;google_apis;arm64-v8a" \
  "system-images;android-33;google_apis;arm64-v8a" \
  "system-images;android-34;google_apis;arm64-v8a" \
  "system-images;android-35;google_apis;arm64-v8a" \
  "system-images;android-36;google_apis;arm64-v8a"

# 4) 自检
adb version && emulator -version && sdkmanager --list_installed | grep system-images
```

---

## 11. Windows 专章（Windows Agent）

> 适配代码已跨平台：工具发现走「**环境变量 + PATH(which) + `Path.home()`**」通用机制，并自动识别 Windows 的
> `.exe` / `.bat` 可执行名（`capability.py::_find_tool` / `_exe_candidates`）。**代码不写死任何盘符/用户名**——
> 你只要把环境配好(尤其环境变量)，任意盘、任意用户名都能被自动发现。

### 11.1 与 macOS 的差异（只有三处）

| 项 | macOS | Windows |
|---|---|---|
| 工具可执行名 | `adb` / `emulator` / `avdmanager` | `adb.exe` / `emulator.exe` / `avdmanager.bat` / `sdkmanager.bat` |
| SDK 默认目录 | `~/Library/Android/sdk` | `C:\Users\<用户>\AppData\Local\Android\Sdk`（Android Studio 默认） |
| AVD 目录 | `~/.android/avd` | `C:\Users\<用户>\.android\avd` |
| CPU/ABI | Apple Silicon → `arm64-v8a` | 通常 Intel/AMD → **`x86_64`** |

代码对这三处都做了通用处理，你**无需关心代码**，只需按下面把环境装好。

### 11.2 安装步骤

1. **JDK 17+**：装 Temurin/OpenJDK 17，`java -version` 能输出。
2. **Android SDK 命令行工具**：下载 “Command line tools only (Windows)”，解压成 `…\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat`。
3. **环境变量(关键，代码靠这个发现，不靠固定盘符)**——在「系统环境变量」里设：
   - `ANDROID_HOME` = `C:\Users\<用户>\AppData\Local\Android\Sdk`（或你实际的 SDK 根，任意盘均可）
   - `ANDROID_SDK_ROOT` = 同上
   - `PATH` 追加：`%ANDROID_HOME%\cmdline-tools\latest\bin`、`%ANDROID_HOME%\platform-tools`、`%ANDROID_HOME%\emulator`
4. **接受 license + 装组件/镜像**（PowerShell / CMD，`x86_64`）：
   ```bat
   sdkmanager --licenses
   sdkmanager "platform-tools" "emulator" ^
     "system-images;android-21;google_apis;x86_64" ^
     "system-images;android-23;google_apis;x86_64" ^
     "system-images;android-26;google_apis;x86_64" ^
     "system-images;android-28;google_apis;x86_64" ^
     "system-images;android-30;google_apis;x86_64" ^
     "system-images;android-33;google_apis;x86_64" ^
     "system-images;android-34;google_apis;x86_64" ^
     "system-images;android-35;google_apis;x86_64" ^
     "system-images;android-36;google_apis;x86_64"
   ```
5. **硬件加速(必须)**：Intel/AMD 装 **AEHD**（`sdkmanager "extras;google;Android_Emulator_hypervisor_driver"` 后按提示安装；或 Android Studio 里装），并确认 BIOS 开了虚拟化。验证：`emulator -accel-check` 显示 accel 可用。

### 11.3 关键注意

- **Agent 必须用「装 SDK 的那个 Windows 用户」启动**：环境变量和 `Path.home()`(即 `%USERPROFILE%`) 都按当前登录用户解析。用别的用户(或某些服务账户)启动，会找不到 SDK/AVD。
- **不依赖固定盘符**：SDK 装在 `D:\` 等任意位置都行，只要 `ANDROID_HOME`/`ANDROID_SDK_ROOT` 指对、PATH 配上——代码自动发现。
- 排错与 macOS 同 §9，外加日志在 `…\<storage_dir>\vm_runtime\<vmid>\emulator.log`。

### 11.4 自检

```bat
adb version
emulator -version
sdkmanager --list_installed
emulator -accel-check
```
