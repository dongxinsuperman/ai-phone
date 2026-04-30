# midscene-bridge

ai-phone 项目下寄居的独立 Node 工程，把 Midscene 包装成一个"接收 serial + goal、跑完输出报告路径"的命令行进程。

完整方案见 `../Midscene执行器接入方案.md`。本 README 只讲怎么装、怎么跑、怎么调试。

---

## 目录定位

```
ai-phone/
├── backend/           # ai-phone 主仓 Python
└── midscene-bridge/   # 本目录：完全独立的 Node 工程
    ├── package.json
    ├── tsconfig.json
    ├── .env.midscene.example
    ├── src/run.ts
    └── dist/run.js   # tsc 编译产物
```

`ai-phone/backend` 的 Python 端通过 `node midscene-bridge/dist/run.js ...` 调用，**两边唯一接口是命令行参数和 stdout JSON**。

---

## 一次性安装

每台 Mac Agent 部署时执行一次：

```bash
cd midscene-bridge
npm install
cp .env.midscene.example .env.midscene
# 编辑 .env.midscene 填入 doubao 的 OPENAI_API_KEY 等
npm run build
```

> 升级 Midscene 版本：`npm update @midscene/android @midscene/core` 后重新 `npm run build`。

---

## ENV 与 ai-phone 主仓的关系

**完全独立**：

- 不读 `../backend/.env` / `../.env` 任何 `AI_PHONE_*` 变量
- bridge 启动时 `dotenv` 加载 `./.env.midscene`
- ai-phone Python 端 spawn 子进程时通过白名单透传 `PATH` / `HOME` / `ANDROID_HOME` / `LANG` 等系统级 env，**主仓的 vlm key 不会泄漏到 bridge**

升级 doubao 模型时**两份 .env 都要改**：
1. `ai-phone/backend/.env` 的 `AI_PHONE_VLM_*`
2. `midscene-bridge/.env.midscene` 的 `OPENAI_*` / `MIDSCENE_MODEL_NAME`

---

## 命令行调用

```bash
node dist/run.js \
  --serial <android_serial> \
  --goal  "<自然语言目标>" \
  --report-dir /abs/path/to/report-output-dir \
  --run-id <ai-phone-run-id>
```

参数说明：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--serial` | ✅ | Android 设备 serial（adb devices 里那个） |
| `--goal` | ✅ | 自然语言目标，传给 Midscene `aiAct(goal)` |
| `--report-dir` | ✅ | Midscene 报告 HTML 输出根目录（被设为 `MIDSCENE_RUN_DIR`） |
| `--run-id` | ✅ | ai-phone 主仓的 run id，仅做关联记录 |

---

## stdout JSON 协议

**bridge 退出前最后一行**写一份固定 schema 的 JSON 给 Python 端解析：

```json
{"result":"pass","report":"file:///abs/path/report.html"}
{"result":"fail","report":"file:///abs/path","reason":"..."}
{"result":"error","report":null,"reason":"<err>"}
```

| `result` | 语义 | exit code |
|---|---|---|
| `pass` | Midscene `aiAct` 正常返回，**任务判定成功** | 0 |
| `fail` | Midscene 抛错（包括 aiAssert 失败、目标不可达等） | 1 |
| `error` | 框架级错误：参数错、import 失败、agent_init 失败、SIGTERM 中断 | 2-130 |

bridge 自己的运行日志（包括 Midscene 库的 console 输出）走 **stderr**，不会被 Python 端误解析为 JSON。

---

## 本地调试

不依赖 ai-phone Python 端，直接命令行跑：

```bash
# 1. 准备
cd midscene-bridge
cp .env.midscene.example .env.midscene
vim .env.midscene  # 填密钥
npm install && npm run build

# 2. 真机跑
adb devices  # 确认设备 serial
mkdir -p /tmp/midscene-test
node dist/run.js \
  --serial $(adb devices | sed -n '2p' | awk '{print $1}') \
  --goal  "打开计算器" \
  --report-dir /tmp/midscene-test \
  --run-id local-debug

# 3. 看产物
ls /tmp/midscene-test/
```

最后一行 stdout 应该是合法 JSON。如果不是，说明 `getReportPath()` 没找到产物或 Midscene 上游 API 不匹配，需要看 stderr 排查。

---

## 卸载

```bash
rm -rf midscene-bridge/
```

ai-phone 主仓侧再把 `AI_PHONE_MIDSCENE_ENABLED=false`，效果与本目录从未存在过一致。

---

## 兼容性提示

- Midscene 上游 SDK 时常微调（构造函数签名 / 事件名 / 报告路径命名规则）
- 本 bridge 的 `src/run.ts` 用 `require + 兼容判断` 写法尽量兜住版本差异
- 如果某天升级后 stdout 协议失败，**优先排查这两处**：
  1. `getReportPath()` 扫描的目录命名规则是否变
  2. `AndroidAgent` 构造函数签名是否变（`new AndroidAgent(serial)` vs `new AndroidAgent(deviceObj)`）
