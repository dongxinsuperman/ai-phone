# 本地开发指南（Mac）

> 起一份完整链路：Server + Agent + 前端三个进程 + 一台真机即可。
> 新 Mac 从 0 到 1 完成部署，请优先看 [`deployment-from-zero（从0到1部署指南）.md`](./deployment-from-zero（从0到1部署指南）.md)。
> 如果这台 Mac 只作为 Agent 接手机，请看 [`agent-deployment（Agent接入部署指南）.md`](./agent-deployment（Agent接入部署指南）.md)。
> iOS / HarmonyOS 额外配置见 [`ios-setup（iOS接入指南）.md`](./ios-setup（iOS接入指南）.md) 和 [`harmony-setup（HarmonyOS接入指南）.md`](./harmony-setup（HarmonyOS接入指南）.md)。
> 部署推荐默认值见 [`recommended-env（推荐部署Env清单）.md`](./recommended-env（推荐部署Env清单）.md)：iOS stable 线路优先，Android / HarmonyOS 黑屏待机线路优先。

---

## 一、前置依赖

- macOS，**Python 3.11**（`brew install python@3.11`，**不要用系统自带的 3.9**：pmd3 9.x / aiokafka 0.11+ / ruff py311 都要求 3.11+）
- Node 18+（仅启用 `midscene-bridge` 外接执行器时需要 Node >=20.19）
- `brew install android-platform-tools ffmpeg`
  - **`ffmpeg` 是镜像必需依赖**（agent 内部子进程调用）
- PostgreSQL：本机 Homebrew Postgres 或远程实例皆可，连接串走 `AI_PHONE_DB_URL`
- Android 真机 + USB 线，开发者模式 + USB 调试已开

---

## 二、配置 `.env`

```bash
cd backend
cp .env.example .env
```

至少改这几类：

| 变量 | 用途 |
|---|---|
| `AI_PHONE_DB_URL` | Postgres 连接串 |
| `AI_PHONE_AGENT_TOKEN` | Agent ↔ Server 鉴权（开发用 `dev` 即可） |
| `AI_PHONE_PHONE_VLM_*` | 碰手机的主视觉模型：provider / model / api key / base url |
| `AI_PHONE_AUX_*` | 不碰手机的辅助模型：provider / model / api key / base url |

可选（有需要再开）：

| 变量 | 用途 |
|---|---|
| `AI_PHONE_PHONE_VLM_PROVIDER` | 切换主 VLM：`doubao` / `claude` / `openai` |
| `AI_PHONE_AUX_PROVIDER` | 切换辅助模型：`doubao` / `claude` / `openai` |
| `AI_PHONE_MIRROR_*` | Android 画质 / 延迟参数（详见 `.env.example` §8） |
| `AI_PHONE_VLM_SESSION_RESET_PROMPT_THRESHOLD` | Doubao Responses 超阈值自动切段（默认 30000，≤0 关闭） |
| `AI_PHONE_WDA_PROJECT_DIR` | iOS 接入入口，留空走"手动 Xcode + iproxy"过渡态 |
| `AI_PHONE_IOS_WDA_LIFECYCLE_MODE` | iOS WDA 生命周期；部署推荐 `stable`，详见 [`recommended-env（推荐部署Env清单）.md`](./recommended-env（推荐部署Env清单）.md) |
| `AI_PHONE_ANDROID_*WAKE*` / `AI_PHONE_HARMONY_*WAKE*` | Android / HarmonyOS 黑屏待机与 Run 前唤醒策略，详见 [`recommended-env（推荐部署Env清单）.md`](./recommended-env（推荐部署Env清单）.md) |

`.env.example` 是用户填表说明；高级调参项见 `.env.full.example`，项目默认策略见 `.env.defaults`。

---

## 三、起 Server

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000 --reload
```

启动时自动建表（`db.py::Base.metadata.create_all()`，**无 alembic**）。

---

## 四、起 Agent（另开终端）

```bash
cd backend && source .venv/bin/activate
python -m ai_phone agent
```

本机开发时参数全走 `.env`，不需要再传命令行。Agent 启动后自动 `adb devices` 扫描 → WS 注册到 Server。首次跑 VLM `type` 时会自动 push + install ADBKeyBoard。

远端办公区电脑只接 Agent、不跑 Server 时，直接把 Server 地址和 token 带上即可：

```bash
python -m ai_phone agent --server http://<server-host>:8000 --token <AI_PHONE_AGENT_TOKEN>
```

`--server` 可以填普通 HTTP(S) 地址，Agent 会自动推导 `ws(s)://.../ws/agent` 和 HTTP 上传地址；也兼容直接填写 `ws://.../ws/agent`。

---

## 五、起前端（另开终端）

```bash
cd web
npm install
npm run dev   # http://127.0.0.1:5180
```

浏览器访问 <http://127.0.0.1:5180>，选设备 → 进工作台 → 输入 goal → 跑。

---

## 六、Schema 重建（仅在升级 v1.7 时需要）

v1.7 对 submission 协议做了破坏性统一（顶层 wrapper、`platforms`、`deviceAliasPools`，详见 [external-api（对外调用清单）](./external-api（对外调用清单）.md)）。**因平台尚未对外发布、零外部用户**，老库直接清空重建即可：

```bash
# 方式 1：只删 submission 相关表
psql "$AI_PHONE_DB_URL" -c 'DROP TABLE IF EXISTS submission_items, submissions CASCADE;'

# 方式 2：直接 drop schema 重建
psql "$AI_PHONE_DB_URL" -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'

# 启动 server，db.py 会按新 schema 自动建表
```

不走 alembic 的根因：v1 schema 一直是"启动期 create_all 自动建表"，没有迁移基线，业务零数据时直接 drop+rebuild 最简单。

---

## 七、常见问题 FAQ

**Q：画面有黑边怎么办？**
正常。`<video>` 用 `object-fit: contain` 按比例缩放，旋转后容器会自动 W/H 互换。手动操作的坐标映射会自动剥离黑边（详见 [`architecture（架构设计）.md`](./architecture（架构设计）.md)）。

**Q：画面延迟想再低一点？**
改 `backend/.env`：`AI_PHONE_MIRROR_FRAG_MS=33` + `AI_PHONE_MIRROR_GOP_SEC=0`，agent 重启生效。代价是 CPU 略高、WS 帧率密集（30 msg/s）。

**Q：画面想更清晰？**
`AI_PHONE_MIRROR_MAX_WIDTH=1920` + `AI_PHONE_MIRROR_BITRATE=12000000`。`1280 + 6M` 是默认甜点，`1920 + 12M` 接近原生。

**Q：`adb devices` 显示 unauthorized？**
拔插一次手机，弹出"允许 USB 调试"对话框点确认；勾选"始终允许"省得每次问。

**Q：ffmpeg 不存在？**
`brew install ffmpeg`。Linux 用 apt：`sudo apt install ffmpeg`。

**Q：端口 8000 被占？**
`lsof -i :8000` 查谁在用，或换 `--port 8001` + 改前端 vite proxy。

**Q：换 Claude / GPT 协议怎么配？**
改 `backend/.env` 的 `AI_PHONE_PHONE_VLM_*` 和 `AI_PHONE_AUX_*` 两块。Claude 主循环仍走已验证的 Claude Computer Use；GPT 主循环走 OpenAI Computer Use；手机层单次辅助由系统内部派生。

**Q：辅助系统的"卡死检测 / 审判 / 断言"误 kill 太多怎么办？**
见 `backend/.env.full.example` 的高级阈值说明。常见做法：把 `AI_PHONE_AUDIT_PERIODIC_INTERVAL` 调大（默认 30 步主动召唤，可以调到 50 让 VLM 多跑几步再监督），或把 `AI_PHONE_AUDIT_ALLOW_LIMIT` 调到 50–100。

---

## 八、相关链接

- [external-api（对外调用清单）](./external-api（对外调用清单）.md)
- [architecture（架构设计）](./architecture（架构设计）.md)
- [deployment-from-zero（从0到1部署指南）](./deployment-from-zero（从0到1部署指南）.md)
- [agent-deployment（Agent接入部署指南）](./agent-deployment（Agent接入部署指南）.md)
- [server-brain（Server大脑架构说明）](./server-brain（Server大脑架构说明）.md)
- [executable-logic-contract（可执行链路契约）](./executable-logic-contract（可执行链路契约）.md)
- [trajectory-cache-usage（轨迹缓存使用文档）](./trajectory-cache-usage（轨迹缓存使用文档）.md)
- [features（使用功能介绍）](./features（使用功能介绍）.md)
- [assistant-systems（辅助系统核心逻辑及效果）](./assistant-systems（辅助系统核心逻辑及效果）.md)
- [recommended-env（推荐部署Env清单）](./recommended-env（推荐部署Env清单）.md)
- [Midscene 执行器挂载方案](../Midscene执行器接入方案.md)
