# Server 大脑架构说明

> 本文档适用于 `next/server-brain` 分支。
>
> 核心目标：对调用方保持无感知，把 VLM 决策从 Agent 上移到 Server。Agent 只负责发现设备、截图、点击、输入、滑动等真机动作。

---

## 一、架构一句话

当前稳定分支更接近：

```text
Server / Web：管理设备、创建任务、展示日志
Agent：连接手机、调用 VLM、决定下一步、执行动作
```

`next/server-brain` 分支改成：

```text
Server / Web：管理设备、创建任务、调用 VLM、决定下一步、记录日志
Agent：连接手机、执行 Server 下发的设备动作
```

也就是：

```text
Server 是大脑，Agent 是手脚
```

这样做的直接收益是：多台办公区电脑只需要启动 Agent 并连接同一个 Server，不需要在每台 Agent 机器上维护 VLM key、模型地址和复杂执行策略。

---

## 二、对外 API 是否变化

对外调用方无感知。

创建任务仍然走原来的接口：

```http
POST /api/runs
```

调用方继续传原来的字段，例如：

```json
{
  "device_serial": "6ad9243",
  "goal": "点击全部功能",
  "engine": "vlm"
}
```

调用方不需要知道这次 Run 是由 Server 决策，还是由 Agent 决策。执行架构是平台内部实现细节。

当前分支的默认规则：

| engine | 执行位置 | 说明 |
|---|---|---|
| `vlm` | Server 大脑 | Server 调 VLM，Agent 执行动作 |
| `midscene` | Agent 大脑 | 外接 Midscene 仍留在 Agent 端 |

因此，如果外部系统原来只调用 `POST /api/runs`，通常不需要改调用代码。

---

## 三、部署者需要准备什么

调用方无感知，但部署者需要做三件事：

1. Server 端配置数据库、VLM、Agent token
2. 数据库补充 Server 大脑需要的新字段和新表
3. 每台接手机的电脑启动 Agent，并指向同一个 Server

### 3.1 Server 端配置

`backend/.env` 至少需要：

```bash
AI_PHONE_DB_URL=postgresql+asyncpg://...
AI_PHONE_AGENT_TOKEN=dev
AI_PHONE_VLM_API_KEY=...
```

生产环境建议把 `AI_PHONE_AGENT_TOKEN` 换成更长的随机字符串。

Server 启动示例：

```bash
cd backend
source .venv/bin/activate
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000
```

当前 Server Hub 仍在进程内存中，部署时请先使用单进程：

```bash
--workers 1
```

多 pod / 多 worker 需要额外的共享路由和锁机制，不属于当前 PoC 范围。

### 3.2 数据库补充

当前项目不使用 Alembic 迁移链。Server 大脑分支新增的字段和表，通过 SQL 脚本手动补充：

```bash
psql "$AI_PHONE_DB_URL" -f backend/migrations/server_brain_v2.sql
```

这个脚本用于补充：

- `runs` 上的执行模式、派发来源、Agent 快照等字段
- `run_steps` / `run_logs` 上的 trace 和错误归因字段
- `run_commands` 表，用于记录 Server 大脑下发给 Agent 的设备 RPC 命令

注意：如果你的 Postgres 版本较老，也应优先使用该脚本，不要只依赖 SQLAlchemy model 自动建表。旧库里已有表时，`create_all()` 不会自动给已有表补字段。

### 3.3 Agent 端启动

接手机的电脑只需要启动 Agent。

本机开发时可以不带参数，读取 `.env`：

```bash
cd backend
source .venv/bin/activate
python -m ai_phone agent
```

远程办公区电脑推荐显式传 Server 地址和 token：

```bash
cd backend
source .venv/bin/activate
python -m ai_phone agent --server http://<server-host>:8000 --token <AI_PHONE_AGENT_TOKEN>
```

`--server` 可以填普通 HTTP 地址，Agent 会自动推导 WebSocket 地址：

```text
http://10.8.201.101:8000
→ ws://10.8.201.101:8000/ws/agent
```

Agent 机器仍然需要具备本机设备环境，例如：

- Android：`adb devices` 能看到设备，USB 调试已授权
- iOS：仍按现有 iOS WDA 方式配置
- 镜像：需要 `ffmpeg`

Server 大脑不会消除真机连接本身的系统依赖，它只是把 VLM 决策和密钥集中到 Server。

---

## 四、多 Agent 使用方式

典型拓扑：

```text
公司云服务 / 内网机器
  ├─ Server + Web + Postgres
  ├─ VLM 配置和密钥
  └─ 统一 Agent token

办公区电脑 A
  ├─ Agent
  └─ Android / iOS 真机

办公区电脑 B
  ├─ Agent
  └─ Android / iOS 真机
```

所有 Agent 指向同一个 Server：

```bash
python -m ai_phone agent --server http://<server-host>:8000 --token <token>
```

Agent 上线后，Web 的设备总览页会显示：

- 在线 Agent 数量
- 每个 Agent 的名称
- 每台设备归属哪个 Agent
- 当前设备是否空闲 / 运行中 / 离线

创建 Run 时，Server 会根据设备 serial 找到对应 Agent，再通过 `driver_command` / `driver_result` 协议让 Agent 执行动作。

---

## 五、如何确认这次 Run 走的是 Server 大脑

Web 工作台里会显示当前 Run 的执行模式：

```text
Server 大脑
```

同时能看到：

- `run_id`
- Agent 名称
- 入口来源，例如 `api`

也可以从接口确认：

```bash
curl http://<server-host>:8000/api/runs/<run_id>
```

关键字段：

```json
{
  "engine": "vlm",
  "execution_mode": "server_brain",
  "agent_id_at_start": "xxx",
  "dispatch_source": "api"
}
```

Server 大脑模式下，还可以查询设备 RPC 命令：

```bash
curl http://<server-host>:8000/api/runs/<run_id>/commands
```

如果能看到 `screenshot_jpeg`、`click`、`swipe`、`type_text` 等命令记录，说明这次 Run 的决策确实在 Server，Agent 只是执行动作。

---

## 六、当前支持范围

当前分支已经支持：

- 单 Server + 单 Agent + Android 真机跑通 VLM Run
- 单 Server + 多 Agent 的在线状态展示
- VLM Run 走 Server 大脑
- 总设备池 / scheduler 队列派发走 Server 大脑
- Midscene Run 保持 Agent 大脑
- Web 显示执行模式、Agent、run_id、失败摘要
- Server 记录 driver RPC 命令
- Agent 启动时直接填写 HTTP Server 地址和 token

已完成的验证：

- Web / API 手动 Run：`execution_mode=server_brain`
- 内部 submission 队列烟测：scheduler 从 Android 总设备池选中设备，创建 Run，Run 为 `server_brain`，item 收口为 `success`，submission 收口为 `done`
- Server 大脑 RPC 记录：可查询到 `screenshot_jpeg`、`window_size`、`click` 等命令

当前仍建议继续验证：

- 多台真实电脑同时接 Agent
- Agent 断线 / 重连
- Run 运行中拔手机
- Run 运行中停止
- 多 item / 多平台批量派发
- iOS 在 Server 大脑模式下的完整链路

---

## 七、已知限制

### 7.1 当前不支持多 Server worker

Agent WebSocket、设备归属、Run 路由目前仍在 Server 进程内存里。

因此部署时请先使用：

```bash
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000 --workers 1
```

如果未来要上 K8s 多 pod，需要补共享路由、分布式锁、Run task 归属恢复等机制。

### 7.2 Midscene 仍在 Agent 端

`engine=midscene` 当前仍然走 Agent 大脑。

这是有意保留的兼容路径，因为 Midscene 是外接寄居执行器，不属于 ai-phone 主 VLM 循环。

### 7.3 Agent 仍需要本机设备环境

Server 大脑只上移 VLM 决策，不会替代：

- ADB
- Xcode / WDA
- USB 授权
- ffmpeg
- 系统权限

所以远程 Agent 机器仍然需要先把本机真机环境准备好。

### 7.4 iOS 无感向导暂未纳入当前版本

未来可以做：

- 自动检测 Xcode
- 自动检测 Team ID
- 自动生成 / 注入 WDA 配置
- Agent doctor 环境体检
- 一键修复常见 iOS 环境问题

但这些属于后续 Agent 产品化工作，不阻塞当前 Server 大脑主链路。

---

## 八、与 main 分支的关系

`main` 分支是当前稳定开源入口。

`next/server-brain` 是新架构实验分支，用来验证：

```text
Server 负责 VLM 决策
Agent 负责真机动作
调用方 API 保持不变
```

两条分支建议保持隔离：

- `main`：稳定、易理解、适合首次开源用户
- `next/server-brain`：实验架构、快速迭代、验证多 Agent 和 Server 大脑

当 `next/server-brain` 足够稳定后，可以再决定是否发布新的 tag 或新版本说明。

---

## 九、最小启动示例

Server 机器：

```bash
cd backend
source .venv/bin/activate
psql "$AI_PHONE_DB_URL" -f backend/migrations/server_brain_v2.sql
uvicorn ai_phone.server.app:app --host 0.0.0.0 --port 8000
```

Agent 机器：

```bash
cd backend
source .venv/bin/activate
python -m ai_phone agent --server http://<server-host>:8000 --token <AI_PHONE_AGENT_TOKEN>
```

Web：

```bash
cd web
npm install
npm run dev
```

浏览器打开：

```text
http://127.0.0.1:5180
```

进入设备工作台，选择 `vlm`，输入目标并开始 Run。看到 `Server 大脑` 标识，即说明新架构链路生效。
