# Agent 鸿蒙虚拟机环境准备

> 目标：一台新 Mac 拉取 ai-phone 代码后，按本文完成准备，即可承接 Server
> 下发的鸿蒙虚拟机。虚拟机启动后会带 `virtual` 标识进入设备池，像鸿蒙真机
> 一样进入工作台和任务调度。
>
> 本文只负责**当前已经实现的 Agent 本地 GUI 模式**。官方本地 Emulator 一旦
> 提供真正 Headless，现有 Agent 部署和业务链路直接转换，只调整能力探查与启动
> 参数；规划中的 Linux gRPC 资源池则是另一条集中式接入通道。完整边界见
> [鸿蒙虚拟机当前架构与演进规划](./harmony-vm-architecture（鸿蒙虚拟机当前架构与演进规划）.md)。

---

## 1. 宿主要求

- macOS，Apple Silicon 使用 ARM64 镜像，Intel 使用 x86_64 镜像。
- 建议至少 16GB 内存；需要同时运行多台时建议 32GB 以上，并按目标机实测并发数。
- 必须存在已登录的 macOS GUI 会话。当前 DevEco Emulator 没有
  Android `-no-window` 的等价参数，启动后会出现 Emulator 窗口。
- **不要求一直连接物理显示器。** Mac mini 可以不接显示器，但必须保持用户
  桌面会话登录，并让 Agent 运行在该用户会话中。首次部署建议通过屏幕共享完成；
  正式使用前必须实测一次“无物理显示器启动 VM”。如果无屏时系统没有可用显示
  表面，可使用 macOS 屏幕共享的虚拟显示或 HDMI 显示器模拟器。
- Agent 不要配置成系统级 `LaunchDaemon`；需要自启动时使用登录用户的
  `LaunchAgent`。

检查当前终端是否属于 GUI 登录用户：

```bash
CURRENT_UID="$(id -u)"
CONSOLE_UID="$(stat -f '%u' /dev/console)"

test "$CURRENT_UID" = "$CONSOLE_UID" \
  && launchctl print "gui/$CURRENT_UID" >/dev/null 2>&1 \
  && echo "GUI session ready" \
  || echo "GUI session unavailable"
```

---

## 2. 安装和初始化 DevEco Studio

1. 从华为官网下载 DevEco Studio：
   <https://developer.huawei.com/consumer/cn/deveco-studio/>
2. 安装到默认位置：

```text
/Applications/DevEco-Studio.app
```

3. 在 Mac 桌面中打开一次 DevEco Studio，完成初始化、账号登录和协议确认。
4. 打开：

```text
Tools → Device Manager
```

5. **镜像目录和实例目录全部保持 DevEco 默认值，不要修改，也不用手工新建：**

```text
镜像：~/Library/Huawei/Sdk
实例：~/.Huawei/Emulator/deployed
```

Agent 会自动发现 DevEco、HDC、镜像和实例目录。不要给每台 Agent 配置
Emulator、镜像或实例的绝对路径 Env。

6. 下载镜像后，从 Device Manager 启动任意一台 Emulator，一直等到进入系统
   桌面并完成首次启动时出现的全部协议确认，然后停止它。优先直接使用 DevEco
   提供的预配置 Emulator；只有当前界面要求时才临时创建一台。

`Emulator -license accept` 不能代替首次启动时的全部协议确认，因此这一步必须
在 GUI 中完成一次。平台正式使用的实例仍由 Agent 自动创建，不需要人工预建。

---

## 3. 按需下载镜像

不需要把 DevEco 提供的全部镜像下载到每台 Agent。先确认平台准备在这台 Agent
上运行哪些配置，再只下载对应镜像。

需要对应的三个条件：

```text
设备类型：Phone / Foldable / Tablet 等
系统版本：平台配置中选择的 HarmonyOS 版本
架构：Apple Silicon 使用 ARM64，Intel 使用 x86_64
```

在 DevEco Studio 中进入：

```text
Tools → Device Manager → Image
```

下载需要的镜像即可。后续平台新增其他设备类型或系统版本时，再补下载对应镜像。

Agent 探查会检查目标配置需要的镜像是否已经安装：

- 已安装：允许下发。
- 未安装：明确提示缺少的设备类型和系统版本。
- 不会自动下载，也不会使用其他系统版本或其他架构的镜像顶替。

---

## 4. 安装并启动 Agent

从项目根目录执行：

```bash
cd backend

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e ".[harmony]"
```

`.[harmony]` 会安装项目验收版本 `hmdriver2==1.4.4`，不要在单台 Agent 上
自行升级。

在 `backend/.env` 中填写：

```env
AI_PHONE_SERVER_HTTP_BASE=https://<公司Server地址>
AI_PHONE_SERVER_WS_URL=wss://<公司Server地址>/ws/agent
AI_PHONE_AGENT_TOKEN=<管理员提供的Agent token>
AI_PHONE_AGENT_NAME=<这台Mac的唯一名称>
```

启动：

```bash
python -m ai_phone agent
```

标准安装位置下无需设置 `hdc` PATH。自检：

```bash
DEVECO_EMULATOR="/Applications/DevEco-Studio.app/Contents/tools/emulator/Emulator"
HDC="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"

"$DEVECO_EMULATOR" -version
"$HDC" -v
python -c "import hmdriver2; print('hmdriver2 ok')"
```

---

## 5. 在平台中使用

1. Web 中确认 Agent 在线。
2. 进入「虚拟机 → 鸿蒙虚拟机」。
3. 选择设备、机型和系统版本，创建配置。
4. 点击「探查」，目标 Agent 应显示可用。
5. 下发到该 Agent。
6. 等待状态变为「运行中」。
7. 在「设备总览」确认设备带有虚拟机标识。
8. 进入工作台验证画面、点击和滑动。

Agent 会自动完成实例创建、端口分配、HDC 连接、系统启动等待、驱动握手、停止和
回收。用户不需要手工创建平台使用的 Emulator，也不需要填写端口。

### 共享开发设备身份

如果调试 HAP 要求提前登记设备 UDID：

1. 进入「设备配置 → 鸿蒙虚拟机共享设备身份」。
2. 随机生成或填写一份标准 UUID，点击「保存」。
3. 复制页面生成的设备 UDID，登记到开发配置中。

这是一份保存在 Server 的全局实例 UUID，所有 Agent 和所有鸿蒙虚拟机共用，不需要
逐台 Agent 填写。UUID 与 UDID 不是同一个字符串：Emulator 会根据实例 UUID 生成
页面展示的设备 UDID，开发配置登记的是 UDID。保存配置时不会给 Agent 广播消息；
Server 只会在虚拟机启动时把当前 UUID 随启动指令发给目标 Agent，Agent 在启动
Emulator 前写入该实例。已经运行的虚拟机不会立即变化，重新启动后使用最新配置。

点击「恢复默认」后，Server 清空共享配置；从未使用共享身份的实例保持原值，仍带
历史共享 UUID 的实例会在下次启动时生成各自独立的新 UUID。UUID 非空但无法写入，
或恢复独立 UUID 失败时，本次启动直接失败并显示错误，不会表面恢复、实际继续沿用
共享身份。

---

## 6. 注意事项

- DevEco 和镜像使用默认目录，不复制到项目目录，不放入 `/tmp`。
- Agent 必须由已登录 GUI 的用户启动；物理显示器不是硬要求。
- 镜像 ABI 必须与宿主一致，不能跨架构运行。
- Server 选择什么设备类型和系统版本，Agent 本机就必须已经安装对应镜像。
- Agent 不会自动下载缺失镜像，也不会用其他版本或其他 ABI 顶替。
- HDC 端口由 Server 统一分配；不要手工配置。
- 一台 Agent 同时连接真机和多台虚拟机时，各设备工作台画面必须分别验收，不能串设备。
- Emulator 日志位于：

```text
backend/.data/storage/harmony_vm_runtime/logs/<vm-id>/emulator.log
```

明确的兜底规则：

| 场景 | 行为 |
| --- | --- |
| `hdc` 不在 PATH | Agent 自动检查 DevEco/SDK 默认目录 |
| DevEco 在线镜像列表不可用 | Agent 只读检查默认目录中已经完整下载的本地镜像 |
| 共享 UUID 留空 | Agent 不注入，按 DevEco 实例配置启动 |
| 共享 UUID 已配置但写入失败 | 本次虚拟机启动失败，不回退到随机 UUID |
| 缺镜像、ABI 不匹配、无 GUI 会话、协议未确认、端口不一致 | 直接失败，不换设备、不换镜像、不切其他端口 |
| 内存不足 | 页面提示风险，不自动降低配置 |
| 未来官方 Headless 启动失败 | 直接失败，不静默回退 GUI |
| 未来 Linux gRPC Provider 不可用 | 不影响当前 Agent GUI 链路，也不把 Provider 任务偷偷改派给 Agent |

---

## 7. 相关文档

- [Agent 接入部署指南](./agent-deployment（Agent接入部署指南）.md)
- [HarmonyOS 接入指南](./harmony-setup（HarmonyOS接入指南）.md)
- [Android 虚拟机环境准备](./agent-vm-env-setup（Agent虚拟机环境准备）.md)
- [鸿蒙虚拟机当前架构与演进规划](./harmony-vm-architecture（鸿蒙虚拟机当前架构与演进规划）.md)
- 华为 DevEco Studio：<https://developer.huawei.com/consumer/cn/deveco-studio/>
- 华为 Emulator 创建说明：<https://developer.huawei.com/consumer/en/doc/harmonyos-guides/ide-emulator-create>
