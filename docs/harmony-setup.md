# HarmonyOS 接入指南

> 走 `hdc` + `hmdriver2`（社区版鸿蒙 UI 自动化），镜像走 hypium Captures MJPEG（hmdriver2 内部 RecordClient 同款协议）。
> 与 iOS / Android 同级，调用方 API 完全一致，只需切换 `platform: harmony`。

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

agent 内部已经实现三级自愈，业务无感（详见 [`架构设计.md`](../架构设计.md) §10 鸿蒙篇）：

- **L1 socket 重连**：uitest socket 短断后自动重连
- **L2 重建 Driver**：连续断流 → 销毁旧 driver、重新 attach
- **L3 杀 uitest daemon 重拉**：L2 也失败 → 进程级重启

并发串行化锁 + `__del__` 定向屏蔽：消除原生库析构误杀 fport 导致的全设备失联。

---

## 六、防自动息屏

agent 自动通过 `power-shell timeout -o` 设置不息屏，**以 10 分钟为周期续约**（实测单次 override 在 18 小时长跑后会被系统抹掉）。rescan 步频自然驱动，无新增协程。

---

## 七、目前已知限制

- **折叠屏动态形态切换**：设备固定形态（展开 / 折叠其一）下执行稳定；折叠过渡瞬间屏幕尺寸缓存与物理状态严格一致性作为后续专项优化。当前自动化 case 推荐在固定形态下编排。
- **中文输入**：hmdriver2 `input_text` 原生 Unicode，无需切 IME

---

## 八、相关链接

- [本地开发指南](./getting-started.md)
- [iOS 接入指南](./ios-setup.md)
- [架构设计 §10 HarmonyOS 镜像架构](../架构设计.md)
