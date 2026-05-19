# HarmonyOS 接入指南

> 走 `hdc` + `hmdriver2`（社区版鸿蒙 UI 自动化），镜像走 hypium Captures MJPEG（hmdriver2 内部 RecordClient 同款协议）。
> 与 iOS / Android 同级，调用方 API 完全一致，只需在投递里写 `platforms: ["harmony"]`。

---

## 一、安装鸿蒙可选依赖

```bash
cd backend && source .venv/bin/activate
pip install -e ".[harmony]"   # 拉 hmdriver2，纯 Python，~5MB
```

`hmdriver2` 依赖纯 Python，不拉重量级编码库，对 Windows / Linux 同事没有副作用。

---

## 二、安装 `hdc`

`hdc` 二进制随 **DevEco Studio** 一起装。Mac 默认路径：

```
~/Library/Huawei/Sdk/openharmony/<版本>/toolchains/hdc
```

agent 启动时会自动从常见安装路径补上 `PATH`，**多数情况下不用手动 export**。如果 agent 启动报 "hdc: command not found"，手动补一下：

```bash
export PATH="$HOME/Library/Huawei/Sdk/openharmony/<版本>/toolchains:$PATH"
```

---

## 三、启动顺序

和 iOS / Android 完全一样，**只是不需要 tunneld**（DVT 是 iOS 专属）：

| 终端 | 命令 |
|---|---|
| A | 后端 Server：`uvicorn ai_phone.server.app:app ...` |
| B | 后端 Agent：`python -m ai_phone agent`（自动扫 `hdc list targets`） |
| C | 前端：`npm run dev` |

设备打开开发者模式 + USB 调试 + 连数据线即可，agent 会自动 rescan 入池。

---

## 四、镜像后端切换

鸿蒙镜像两选一（env：`AI_PHONE_HARMONY_MIRROR_BACKEND`）：

| 后端 | 路径 | 说明 |
|---|---|---|
| `hypium`（**默认**） | hypium Captures MJPEG socket，设备主动 push JPEG 帧序列 | 实测 ~30 fps、延迟 < 100 ms，**包含完整合成画面（含视频图层）**，折叠 / 异形屏天然自适应 |
| `screenshot`（兜底） | hdc shell `snapshot_display` 截图轮询 | ~8-10 fps，**视频期间 XComponent / SurfaceView 抓不到 → 全黑**，hypium 不可用时回退 |

默认从 `screenshot` 升级为 `hypium` 是 P0 工程决策：视频不黑屏 + 性能更好，无回退理由。若 hypium 在某些 OEM 设备上行为异常，可临时切回 `screenshot`。

---

## 五、稳定性工程（HarmonyOS 三级自愈）

agent 内部已经实现三级自愈，业务无感（详见 [`architecture（架构设计）.md`](./architecture（架构设计）.md)）：

- **L1 socket 重连**：uitest socket 短断后自动重连
- **L2 重建 Driver**：连续断流 → 销毁旧 driver、重新 attach
- **L3 杀 uitest daemon 重拉**：L2 也失败 → 进程级重启

并发串行化锁 + `__del__` 定向屏蔽：消除原生库析构误杀 fport 导致的全设备失联。

---

## 六、黑屏待机与 Run 前唤醒

当前部署推荐不是长期常亮，而是让设备空闲自然息屏，真正执行前用纯 `hdc` 唤醒：

```env
AI_PHONE_HARMONY_SETUP_STAY_AWAKE=false
AI_PHONE_HARMONY_SCREEN_OFF_DISPATCHABLE=true
AI_PHONE_HARMONY_WAKE_BEFORE_RUN=true
AI_PHONE_HARMONY_WAKE_SWIPE_ENABLED=true
AI_PHONE_HARMONY_WAKE_SETTLE_MS=500
AI_PHONE_HARMONY_WAKE_SWIPE_SETTLE_MS=500
AI_PHONE_HARMONY_WAKE_ON_ENTER=true
```

含义：

- `SETUP_STAY_AWAKE=false`：不再用 `power-shell timeout -o` 做长期常亮续约。
- `SCREEN_OFF_DISPATCHABLE=true`：黑屏但可唤醒的设备仍可进队列派发。
- `WAKE_BEFORE_RUN=true`：hmdriver2 初始化、首张截图、缓存回放前先走纯 `hdc shell power-shell wakeup`。
- `WAKE_ON_ENTER=true`：手动进入工作台、启动镜像、手动 input 前也先点亮屏幕；只 wake，不自动上滑。
- `WAKE_SWIPE_ENABLED=true` 只是能力开关；真正自动上滑还必须命中 `AI_PHONE_WAKE_SWIPE_DEVICE_ALLOWLIST`。

如果设备存在安全密码 / 生物识别锁，wake + 上滑只能到锁屏认证页，不能绕过系统安全锁。

---

## 七、目前已知限制

- **折叠屏动态形态切换**：设备固定形态（展开 / 折叠其一）下执行稳定；折叠过渡瞬间屏幕尺寸缓存与物理状态严格一致性作为后续专项优化。当前自动化 case 推荐在固定形态下编排。
- **中文输入**：hmdriver2 `input_text` 原生 Unicode，无需切 IME

---

## 八、相关链接

- [本地开发指南](./getting-started（本地开发指南）.md)
- [iOS 接入指南](./ios-setup（iOS接入指南）.md)
- [architecture（架构设计）](./architecture（架构设计）.md)
- [推荐部署 Env 清单](./recommended-env（推荐部署Env清单）.md)
