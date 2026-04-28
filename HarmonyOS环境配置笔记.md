# HarmonyOS 环境配置（macOS）

**一句话**：装 `hdc` 二进制 + 装 Python `hmdriver2` 库 + 设备开发者模式。三件事做完即插即用。

> **角色界定**：本文档面向 **agent 运维者**（在自己 Mac 上拉起 ai-phone agent 进程的人）。
> **业务测试同事只用 web 浏览器**操控真机，**零安装**——不需要看本文档。
> 第 6 节的"后门"是给**会写 Python 的高级脚本人员/UI 自动化工程师**绕过 VLM 直接驱动 hmdriver2 用的。

---

## 1. 安装 `hdc`（鸿蒙版 adb）

### 方式 A：装 DevEco Studio（推荐，agent 运维者顺带能起模拟器/查 hap）

1. 官网下载：<https://developer.huawei.com/consumer/cn/deveco-studio/>，选 **Mac (ARM)** 版（Apple Silicon）或 **Mac (X86)** 版（Intel）
2. 拖进 `/Applications/`；**不用启动 IDE**，只是借里面的 `hdc` 二进制
3. DevEco **6.1** 的 `hdc` 路径（2026-04 实测）：
   ```
   /Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc
   ```
   旧版本（≤5.x）路径里没有 `default/` 那一层；真装的时候以 `find /Applications/DevEco-Studio.app -name hdc` 为准。
4. 把这个目录加进 PATH：

```bash
# zsh（DevEco 6.1 路径）
cat >> ~/.zshrc <<'EOF'

# HarmonyOS hdc (DevEco Studio 6.1)
export PATH="/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
EOF
source ~/.zshrc
```

### 方式 B：单独装 `hdc`（不想要 DevEco 的极简选项）

鸿蒙 SDK 命令行工具单独包：
<https://developer.huawei.com/consumer/cn/doc/harmonyos-guides-V5/environment_config-0000001052902427-V5>

下载 Command Line Tools for HarmonyOS，解压后把 `toolchains/` 加进 PATH 即可。

### 自检

```bash
hdc -v
# 期望输出（DevEco 6.1 自带）：
# Ver: 3.2.0c
```

---

## 2. 装 Python 依赖

```bash
cd ai-phone/backend
pip install -e ".[harmony]"   # 只装 harmony extras，不打扰 iOS / Android 同事
# 如果之前已经装过 [ios]，可以合并：pip install -e ".[ios,harmony]"
```

这会拉 `hmdriver2>=1.4.4`，间接依赖 `requests` / `lxml`（纯 Python，约 5MB，不会装 ffmpeg / pymobiledevice3 这种重的）。

### 自检

```bash
python -c "import hmdriver2; print(hmdriver2.__version__)"
# 1.4.x
```

---

## 3. 设备准备

### 3.1 打开开发者模式

1. 设置 → 关于本机 → 连击"版本号" 7 次（和 Android 一样）
2. 返回设置 → 系统 → 开发者选项
3. 打开：
   - 开发者选项（总开关）
   - USB 调试
   - 允许通过 USB 安装应用

### 3.2 首次连接授权

用数据线插 Mac 后，**手机上会弹一个"允许 USB 调试？"**，勾"一律信任" + 确认。

### 3.3 验证连通

```bash
hdc list targets -v
# 期望看到：
# 7AAX06CT47XXXXXX    USB    Connected    xxxxx    HarmonyOS
```

- 看到 `[Empty]` → 线 / 开发者模式 / 授权三件事检查一遍
- 看到 `Unauthorized` → 手机弹过授权但被拒了，重插触发重新授权
- 看到设备但状态怪 → `hdc kill-server && hdc start-server` 重启 daemon

---

## 4. 接入 ai-phone

配好上述三件事后，**直接启 agent**：

```bash
cd ai-phone/backend
python -m ai_phone.agent.main
```

Agent rescan 会自动把鸿蒙设备纳入（和 Android / iOS 同级）。Web 端设备卡出现 `harmony` 平台标识。**iOS / Android 走现有高可用链路不受影响**，harmony 走独立链路。

---

## 5. 已知坑位 & 排障

### 5.1 首次进工作台卡在 "连接中"

第一次对某台设备用 hmdriver2 时，它会：
1. 把 `uitest_agent_v*.so` 推到设备 `/data/local/tmp/agent.so`
2. 起 `uitest start-daemon singleness` 做本地 socket 服务
3. `hdc fport tcp:xxx tcp:8012` 建端口转发

首次约 3-5 秒，之后秒级。如果超过 15 秒还没进去，看 agent 日志搜 `hmdriver2`，常见原因：
- 设备被**企业 MDM 管控**禁用 USB 调试 → 换台个人设备
- `uitest` 本身在设备侧被杀了 → `hdc shell uitest start-daemon singleness` 手动拉起

### 5.2 输入中文乱码

和 Android 不同，**鸿蒙不需要装 ADBKeyBoard 类的输入法中转**。`hmdriver2.input_text` 走 `uitest inputText`，原生支持 Unicode。乱码十有八九是**目标输入框没聚焦**——鸿蒙 uitest 要求调 input_text 前先 click 输入框 + 等聚焦。

### 5.3 旋转后坐标系错乱

`hmdriver2.Driver.display_size` / `display_rotation` 是 `@cached_property`，旋转后不主动失效就拿到老值。ai-phone 的 `HarmonyDriver.window_size()` / `rotation()` 内部会 `_invalidate_cache` 再读，**VLM 主循环这条没事**，但**直接用 `get_raw_driver().display_size` 的业务脚本要自己 `_invalidate_cache("display_size")`**。

### 5.4 镜像只有 ~8fps（说明你还在 `screenshot` 后端，可切到 `hypium`）

`screenshot` 走的是 hmdriver2 截图轮询（`hdc shell snapshot_display` + `hdc file recv`），**单帧 200-400ms 是 USB 2.0 + JPEG 编码 + hdc 往返的物理上限**，和 sonic 等同类方案一个量级。

**`hypium` 后端已落地**（实测 ~30fps、<100ms，详见 [`HarmonyOS接入方案_2026-04-20.md` §13](./HarmonyOS接入方案_2026-04-20.md#13-p3-b-落地完成--hypium-captures-mjpeg2026-04-21)）。注意它走的是 **hypium Captures MJPEG**（不是立项时设想的 H.264，hmdriver2 内部 RecordClient 同款协议），数据契约和 iOS `mjpeg_passthrough` 完全一致，前端零改动。

```bash
# backend/.env
AI_PHONE_HARMONY_MIRROR_BACKEND=hypium       # 推荐主路径
# AI_PHONE_HARMONY_MIRROR_BACKEND=screenshot # 兜底（hypium socket 异常时用）
```

> 代码层默认值仍是 `screenshot`（保新装环境第一次跑不会黑屏）。**已实测稳定的环境应在 `.env` 显式置 `hypium`**。
> 三端镜像后端的完整对比表见 [`启动终端清单.md §7`](./启动终端清单.md#7-切镜像后端三端总表-高级可选)。

---

## 6. 给 UI 自动化测试团队的"后门"

ai-phone 的 `HarmonyDriver` 保留了**拿原生 `hmdriver2.Driver` 的入口**，测试团队可以不走 VLM 视觉链路，直接写控件脚本：

```python
from ai_phone.agent.drivers import open_driver

d = open_driver("7AAX06CT47XXXXXX", platform="harmony")
raw = d.get_raw_driver()         # 这就是原生 hmdriver2.Driver

# 控件树 / XPath / 截图都能用
raw.xpath('//*[@text="登录"]').click()
raw(text="密码").input_text("123456")
raw.screenshot("/tmp/demo.jpeg")
raw.start_app("com.example.myapp")
```

ai-phone 不包装 hmdriver2 的任何 API，保留它的生态完整性。测试团队把脚本丢进自己的 pytest / 蓝盾流水线跑即可，不需要学 ai-phone 的私有接口。
