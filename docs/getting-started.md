# 本地开发指南（Mac）

> 起一份完整链路：Server + Agent + 前端三个进程 + 一台真机即可。
> iOS / HarmonyOS 额外配置见 [`ios-setup.md`](./ios-setup.md) 和 [`harmony-setup.md`](./harmony-setup.md)。

---

## 一、前置依赖

- macOS，**Python 3.11**（`brew install python@3.11`，**不要用系统自带的 3.9**：pmd3 9.x / aiokafka 0.11+ / ruff py311 都要求 3.11+）
- Node 18+
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

至少改这 3 项：

| 变量 | 用途 |
|---|---|
| `AI_PHONE_DB_URL` | Postgres 连接串 |
| `AI_PHONE_AGENT_TOKEN` | Agent ↔ Server 鉴权（开发用 `dev` 即可） |
| `AI_PHONE_VLM_API_KEY` | VLM key（不填只能手动调试，VLM 任务会 401） |

可选（有需要再开）：

| 变量 | 用途 |
|---|---|
| `AI_PHONE_VLM_BACKEND` | 切换主 VLM 协议：`doubao_responses`（默认）/ `claude_cu` / `gpt_cu` |
| `AI_PHONE_ASSISTANT_BACKEND` | 切换非执行型辅助系统协议：`doubao_chat` / `claude` / `openai`。注意：轨迹缓存回放中会产出手机动作的 gate / recovery 不属于普通辅助聊天链路，必须遵守 [可执行链路契约](./executable-logic-contract.md)。 |
| `AI_PHONE_MIRROR_*` | Android 画质 / 延迟参数（详见 `.env.example` §8） |
| `AI_PHONE_VLM_SESSION_RESET_PROMPT_THRESHOLD` | Doubao Responses 超阈值自动切段（默认 30000，≤0 关闭） |
| `AI_PHONE_WDA_PROJECT_DIR` | iOS 接入入口，留空走"手动 Xcode + iproxy"过渡态 |

`.env.example` 顶部按 §1–§20 分组，每组都有详细中文注释。

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

v1.7 对 submission 协议做了破坏性统一（`device_alias` → `device_alias_pool`，详见 [对外调用清单.md §变更记录](../对外调用清单.md#变更记录)）。**因平台尚未对外发布、零外部用户**，老库直接清空重建即可：

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
正常。`<video>` 用 `object-fit: contain` 按比例缩放，旋转后容器会自动 W/H 互换。手动操作的坐标映射会自动剥离黑边（详见 [`架构设计.md`](../架构设计.md) §10.7）。

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
`backend/.env.example` §5 / §6 已经把"换协议示例"写在每个字段下方注释里。最少改 4 行：`AI_PHONE_VLM_BACKEND` + `VLM_API_URL` + `VLM_API_KEY` + `VLM_MODEL`。辅助系统也想换的话再改 §6 的 4 行。

**Q：辅助系统的"卡死检测 / 审判 / 断言"误 kill 太多怎么办？**
见 `backend/.env.example` §17–§20，全部 26 项阈值都是可配的。常见做法：把 `AI_PHONE_AUDIT_PERIODIC_INTERVAL` 调大（默认 30 步主动召唤，可以调到 50 让 VLM 多跑几步再监督），或把 `AI_PHONE_AUDIT_ALLOW_LIMIT` 调到 50–100。

---

## 八、相关链接

- [对外调用 API 契约（投递 / 查询 / 取消 / Kafka / Webhook）](../对外调用清单.md)
- [架构设计](../架构设计.md)
- [Server 大脑架构说明（next/server-brain）](./server-brain.md)
- [主 VLM / 回放可执行链路契约](./executable-logic-contract.md)
- [轨迹缓存 V1 / V2 方案契约](./trajectory-cache-v1-v2.md)
- [使用功能介绍（产品手册）](../使用功能介绍.md)
- [辅助系统核心逻辑及效果（含 26 项阈值调参）](../ai-phone的辅助系统核心逻辑及效果.md)
- [Midscene 执行器挂载方案](../Midscene执行器接入方案.md)
