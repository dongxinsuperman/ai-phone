# Midscene 执行器接入方案

> **本文档定位**：把 Midscene 作为一个"独立工程"寄居在 ai-phone 项目下，给手动调试 run 多提供一个执行器选项。**不做横评、不做对比、不做策略吸收**。本质上是"项目寄居 + 一行 run 入口"。
>
> **预算**：1 人 · 3-4 个工作日（含真机调通 5 条 case 验收）。

---

## 0. 阅读路径

- 5 分钟看完干啥 → §1 + §2
- 想动手改代码 → §3 ~ §6
- 想知道哪些不做 → §7
- 想知道怎么验收 → §8
- 想卸载 → §9

---

## 1. 设计哲学：项目寄居四条铁律

整个方案的灵魂只有一句：

> **Midscene 是一个独立工程，恰好放在 ai-phone 目录下。**

展开成四条工程纪律：

### 1.1 完全寄居

- bridge 目录有自己的 `package.json`、自己的 `node_modules`、自己的 `.env.midscene`
- ai-phone Python 后端只有一个动作：spawn Node 子进程，传 `--serial` 和 `--goal`
- 跑完拿到 stdout 输出的报告路径和 PASS/FAIL 即可，**其他什么都不管**
- 删除整个目录 = 完全卸载

### 1.2 主链路一行不动

- `vlm_loop.py` 一行不动
- 7 套辅助系统一行不动
- driver / mirror / 截图 / 报告链路一行不动
- 只在 `agent/main.py` 的 `_handle_start_run` 加一个工厂分流

### 1.3 不二开

- Midscene 缺什么就缺着——不在 ai-phone 这边给它打补丁
- 它自己的 HTML 报告就是它的报告，ai-phone 不解析、不增强
- 它的 step 流 / token 统计我们不收、不展示
- 想要它的某个能力？回 ai-phone 自己长，**绝不去改它**

### 1.4 一个开关全决定

- ENV 关：`AI_PHONE_MIDSCENE_ENABLED=false`，Web 不显示选项、API 拒收 `engine=midscene`
- ENV 开：才暴露口子
- 默认关闭，明确启用才生效

### 1.5 不阉割 Midscene 执行能力

Android 入口下，**Midscene 一切原生执行能力全部保留**：

- aiAct 自动规划 / planning 缓存：原生（移动端**无** locate 缓存，每步仍调模型 locate；locate 缓存是 Web 端 XPath 专属能力）
- HTML 报告：原生（只改 output 目录，不改报告内容）
- yadb 中文输入：原生
- instant action 系列 API（aiTap / aiInput / aiSwipe / aiPinch / aiLongPress 等）：原生（虽然本期不用，但 bridge 不阻止 Midscene 内部调度）
- 任何 Midscene 升级带来的新能力：原生（升级 bridge 的 npm 依赖即可）

ai-phone 这边只做三件事：**启动子进程、收 stdout JSON、把报告链接展示给用户**。这三件事不触碰 Midscene 内部任何决策、缓存、动作执行。

iOS / Harmony / 批次投递这三个**入口**本期不开放（Harmony 上游已支持，仅是本期路由层暂未启用，理由见 §2.2）。

### 1.6 执行模式与能力对照（客观说明，避免被旧调研误导）

**为什么写这一节**：早期我们对 Midscene 移动端的判断（"移动端不怎么样 / 兜底不足 / 缓存和老脚本一样"）是基于一年多前的版本。这一年 Midscene 在周边能力（HarmonyOS、PC 桌面、MCP / Skills 生态、scrcpy 截图、多模型分意图、deepThink / deepLocate）有大量进化，但**核心执行模式没变**。为避免"接入方案"读者被两侧旧描述误导，这里给一份事实对照。

#### 1.6.1 核心执行模式（一年前到今天没变）

引自 [Midscene 模型策略](https://midscenejs.com/zh/model-strategy.html)：

> 从 1.0 版本开始，Midscene 只支持纯视觉方案，不再提供"提取 DOM"的兼容模式。

引自 [Midscene API 参考](https://midscenejs.com/zh/api.html)：

> 在实际运行时，Midscene 会将用户指令规划（Planning）成多个步骤，然后逐步执行。

也就是说，Auto Planning（`aiAct`）走的是 **"plan 一次 → 拆 N 步 → 每步 locate → 执行"** 循环。这与 ai-phone vlm runner 的 **"截图 → 主 VLM → 单步动作 → 截图 → 主 VLM"** 是同一类视觉循环范式，**架构同档**——不同的是 ai-phone 主链路在外面包了 7 层辅助系统（起跑线 / 通道判定 / 审判 / 双图断言 / 卡死 / 起码自愈 / 瞬态 UI），Midscene 没有。

#### 1.6.2 兜底机制（当前真实情况）

Midscene 自带的兜底有 2 档：

| 档 | 行为 |
|---|---|
| 缓存 miss / 失效 | 自动 fallback 到模型重 locate |
| 单步 plan 失败 | `replanningCycleLimit` 内重 plan |

**没有**ai-phone 的"起跑线包名匹配 / 通道判定 / 审判 / 双图断言 / 卡死检测 / 设备级自愈"等多层护栏。所以"兜底不足"这条调研结论**至今依然成立**——但更精准的说法是"层数比 ai-phone 主链路少"，不是"没有兜底"。

#### 1.6.3 缓存机制（移动端命中后仍调模型，与老脚本不同档）

引自 [Midscene 缓存文档](https://midscenejs.com/caching)：

> Element location caching ... is currently web-only and has certain limitations.
> When the cache is not hit or not available, the process will fall back to using AI services to find the element.

| 维度 | 老脚本（`sonic_all_ai/`） | Midscene 移动端 |
|---|---|---|
| 缓存什么 | 具体坐标 / 滑动方向 / 包名 / 等待秒数 | planning 步骤序列（每步 locate 不缓存） |
| 命中后调不调模型 | **0 次 LLM**（直接 tap 缓存坐标） | **仍 N 次 LLM**（每步 locate 重新调模型） |
| 跨设备复用 | 归一化坐标，换设备无需重建 | 像素坐标，分辨率变需重跑 |
| 失效检测 | 命中错元素时不校验（假阳性陷阱） | 同（命中错元素时不校验） |

→ "缓存与老脚本一样"这条结论**只在'命中错元素时不校验'这一假阳性陷阱上成立**，在节省效果上**两者完全不同档**：老脚本是"录制 + 回放"，Midscene 是"plan 录制 + locate 实时跑"。这也是 ai-phone bridge 主链路把 cache 设为 `write-only`（§7.1）的根本理由。

#### 1.6.4 这一年 Midscene 真增量（公允承认）

| 真增量 | 说明 |
|---|---|
| **Scrcpy 截图模式** | Android 默认 `adb shell screencap` 500-2000ms/帧 → scrcpy 模式 100-200ms/帧（[Android API doc](https://midscenejs.com/zh/android-api-reference.html)），与 ai-phone Android 镜像同档 |
| 多模型分意图 | 默认 / Planning / Insight 三种模型可独立配置 |
| HarmonyOS NEXT 接入 | `@midscene/harmony` |
| PC 桌面接入 | `@midscene/computer`（macOS / Windows / Linux） |
| MCP / Skills 生态 | Claude Code / Cline 等 AI 编程助手可直接 CLI 驱动 |
| Pinch / DragAndDrop / PullGesture / LongPress | 移动端动作集补全 |
| `deepThink` / `deepLocate` | 复杂界面提精度，代价是耗时 |
| Android Planning Cache 落地 | [issue #1026](https://github.com/web-infra-dev/midscene/issues/1026)：移动端有了 planning 维度的缓存 |
| AbstractInterface | 任意界面集成（IoT / 车机 / 内部 App 都能接入），见 [自定义界面](https://midscenejs.com/zh/integrate-with-any-interface.html) |

#### 1.6.5 镜像 / Playground（与 ai-phone 镜像不是同一档）

Midscene 的 "playground" 是**开发者调试工具**——写完 prompt 后看 AI 规划过程是否合理，零代码试用。[社区项目 midscene-ios](https://github.com/lhuanyu/midscene-ios) 借 macOS 自带的 "iPhone 镜像" app 做了 iOS 真机控制。

对照 ai-phone Web 镜像（业务测试同学的浏览器工作终端：抢锁 / 实时手机画面 / 手动点屏 / 切 AI 自动跑 / 跨网段跨人）—— **目标用户和定位都不是同一档**：

| 维度 | ai-phone Web 镜像 | Midscene Playground / iOS 镜像 |
|---|---|---|
| 定位 | 业务测试 / 调用方 / 运维三类人共用的工作终端 | 库开发者本人的调试工具 |
| 用户 | 跨网段、跨人 | 本机 localhost |
| 抢锁 | 有（多人抢一台真机） | 无 |
| 手动 + AI 混合 | 支持（用户手动几步再交给 AI） | 不支持（playground 偏 prompt → 看 AI 跑） |
| 帧率 | scrcpy fmp4 60fps（Android）/ MJPEG passthrough（iOS） | Midscene Android 默认 adb screencap，开 scrcpy 模式后 100-200ms/帧 |

**因此 ai-phone 与 Midscene 不是替代关系**，是不同形态、不同目标用户的两套系统。本接入方案让 ai-phone 在"手动调试 run"这一个垂直场景下复用 Midscene 的 playground 视角，仅此而已——业务测试 / 批次投递 / iOS / Harmony 入口仍走 ai-phone 主链路。

---

## 2. 范围

### 2.1 接入

- ✅ Android 真机
- ✅ 手动调试 run（单 case 调试入口）
- ✅ Midscene 自带的 HTML 报告路径透传给 web

### 2.2 不接入

- ❌ iOS（Midscene iOS 与 ai-phone iOS 都是 WDA 客户端，能力同层无新意；且涉及 WDA session 排他性，调研成本不值得）
- ⏸ Harmony（**上游已支持** `@midscene/harmony`，HDC 连接 + 视觉模型方案，2026-04-16 起；本期暂缓接入的工程理由：HDC fport / uitest daemon 与 ai-phone hmdriver2 的资源争用未真机摸底、海外 Mac 改造优先级压制、ai-phone harmony 主链路本身已成熟。二期评估接入，工作量约 3 人天）
- ❌ 批次投递（`POST /api/submissions`）—— 业务投递永远走 vlm，不接外接引擎
- ❌ Midscene 的 step 事件流、token 统计、内部日志收集
- ❌ 真机镜像在 Midscene run 期间的状态遮罩 / 主动停启
- ❌ Mobile-Agent / 其它执行器（不在本方案范围）

### 2.3 模型选择

Midscene 用**与 ai-phone 主链路同款的 vision 模型**（当前是 doubao-seed-vision），通过 bridge 自己的 `.env.midscene` 配置。

- ai-phone 这边的 ENV 一个不进 bridge
- bridge 这边的 ENV 一个不进 ai-phone
- 但两边内容**重复填一份**（参考 §5.3 的 `.env.midscene.example`）
- 升级模型时需要**手工同步两份 .env**——这是寄居哲学的代价，可接受

---

## 3. 目录布局

```
ai-phone/
├── backend/                           # Python 主仓（既有）
│   └── ai_phone/
│       ├── agent/
│       │   ├── main.py               ◀ 改：build_runner 分流（§4.1）
│       │   └── runner/
│       │       ├── factory.py        ◀ 新增（§4.2）
│       │       ├── midscene_runner.py◀ 新增（§4.3）
│       │       └── vlm_loop.py       — 不动
│       ├── server/
│       │   └── models.py             ◀ 改：Run.engine 字段（§4.4）
│       └── config.py                 ◀ 改：加开关（§5.1）
│
├── midscene-bridge/                  ◀ 全新独立 Node 项目（§3.1）
│   ├── package.json
│   ├── tsconfig.json
│   ├── .env.midscene.example
│   ├── .gitignore                   # node_modules / .env.midscene
│   ├── README.md                    # 部署 / 调试说明
│   └── src/
│       └── run.ts                   # 单文件入口
│
├── web/                              # 前端（既有）
│   └── src/pages/
│       └── ManualRun.vue             ◀ 改：加引擎下拉框（§4.5）
│
└── Midscene执行器接入方案.md         ◀ 本文档
```

### 3.1 `midscene-bridge/` 是一个独立 npm 项目

- 独立 `package.json`，独立 `node_modules`
- 独立 `.env.midscene`（**不读** ai-phone 的 `.env`）
- 单文件入口 `src/run.ts`，编译产物 `dist/run.js`
- ai-phone Python 端只通过 `node dist/run.js --serial X --goal "..."` 调用

---

## 4. 接入点设计

### 4.1 入口分流（`agent/main.py`）

唯一改动点是 `_handle_start_run`：

```python
# 当前
runner = VLMRunner(run_id=run_id, driver=driver, goal=goal, emit=bridge.emit)

# 改成
from ai_phone.agent.runner.factory import build_runner
engine = msg.get("engine") or "vlm"
runner = build_runner(
    engine=engine,
    run_id=run_id,
    serial=serial,
    driver=driver,           # vlm 用；midscene 不用，传进去也无所谓
    goal=goal,
    emit=bridge.emit,
)
```

### 4.2 工厂函数（`agent/runner/factory.py`）

```python
def build_runner(engine, run_id, serial, driver, goal, emit) -> AbstractRunner:
    settings = get_settings()

    if engine in (None, "", "vlm"):
        return VLMRunner(run_id=run_id, driver=driver, goal=goal, emit=emit)

    if engine == "midscene":
        if not settings.midscene_enabled:
            raise RuntimeError("midscene not enabled (AI_PHONE_MIDSCENE_ENABLED=false)")
        return MidsceneRunner(run_id=run_id, serial=serial, goal=goal, emit=emit)

    raise RuntimeError(f"unknown engine: {engine}")
```

`AbstractRunner` 只要求两个方法（最小协议）：

```python
class AbstractRunner(Protocol):
    async def run(self) -> None: ...      # 跑完通过 emit 上报结束
    async def cancel(self) -> None: ...   # 用户点停止时调
```

### 4.3 MidsceneRunner（`agent/runner/midscene_runner.py`）

实际实现约 460 行（含完整 docstring + 错误兜底），核心逻辑：

```python
class MidsceneRunner:
    """单台 Android 设备上的单次任务执行器（外接 Midscene 通道）。"""

    async def run(self) -> None:
        # 1) 定位 bridge 目录（优先 settings.midscene_bridge_dir，否则按
        #    repo 布局自动寻址；找不到直接 fail，不让 node 起在错误目录）
        bridge_dir = _resolve_bridge_dir(self._settings)
        bridge_entry = bridge_dir / "dist" / "run.js"

        # 2) 报告目录：ai-phone storage 下 external-reports/midscene/{run_id}/
        report_dir = self._build_report_dir()
        report_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            self._settings.midscene_node_bin,   # 默认 "node"，可配置
            str(bridge_entry),
            "--serial", self.serial,
            "--goal", self.goal,
            "--report-dir", str(report_dir),
            "--run-id", self.run_id,
        ]
        env = self._build_env()                  # 严格白名单，详见 §5.2

        await self._emit_run_start()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(bridge_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timeout_sec = max(60, int(self._settings.midscene_run_timeout_sec))
        try:
            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    self._proc.communicate(), timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                await self._kill_subprocess()
                stdout_data, stderr_data = await self._drain_remaining()
                await self._emit_run_finish(ok=False,
                    reason=f"error: midscene_hard_timeout({timeout_sec}s)")
                return
        except asyncio.CancelledError:
            # _handle_stop_run → task.cancel() 走到这里。**不 re-raise**：
            # 与 VLMRunner.run() 对齐（vlm 也是自吞 CancelledError、发完
            # EVT_RUN_FINISH 就正常返回）。re-raise 会让外层 _run_task 的
            # except CancelledError 又发一条 MSG_RUN_DONE，重复入库。
            await self._kill_subprocess()
            await self._drain_remaining()
            await self._emit_run_finish(ok=False, reason="cancelled: stopped_by_user")
            return

        # bridge 退出后解析 stdout 最后一行 JSON
        result, report_url, reason = self._parse_bridge_stdout(stdout_data)
        # result → ai-phone RunResult 映射：
        #   pass  → ok=True,  reason="finished"           （等价 vlm finished）
        #   fail  → ok=False, reason="fail: <msg>"        （case 失败）
        #   error → ok=False, reason="error: <msg>"       （bridge / 进程异常）
        ok, run_result = (True, "finished") if result == "pass" \
            else (False, "fail") if result == "fail" \
            else (False, "error")
        await self._emit_run_finish(
            ok=ok,
            reason=run_result if not reason else f"{run_result}: {reason}",
            external_report_url=report_url,
        )

    async def cancel(self) -> None:
        await self._kill_subprocess()

    async def _kill_subprocess(self) -> None:
        """SIGTERM → 等 5s → 还活着就 SIGKILL → 最后 wait 回收僵尸。"""
        ...
```

**关键约束**：

- `cwd` 设到 `midscene-bridge/`，让 Node 能找到 `node_modules`
- `env` 通过 §5.2 白名单构造，**主仓 AI_PHONE_VLM_API_KEY 等密钥一概不进 bridge**
- `report_dir` 通过命令行参数 + ENV 双重透传给 bridge，让 Midscene HTML 落到 ai-phone storage
- 硬超时由 `settings.midscene_run_timeout_sec` 控制（默认 60 分钟，可调）
- `bridge_dir` 找不到 / `node` 不在 PATH，都走 `_emit_run_finish(ok=False, reason="error: …")` 而不是抛异常上扔，让 server 端 `_finalize_run` 正常落库为 `failed` 而不是 ghost run
- stdout 解析协议见 §4.6
- **不 re-raise CancelledError**：与 VLMRunner 行为严格对齐，避免外层 `_run_task` 二次发 MSG_RUN_DONE

### 4.4 Run 表加 engine + external_report_url 字段（`server/models.py`）

```python
class Run(Base):
    ...
    engine: Mapped[str] = mapped_column(String(32), default="vlm", server_default="vlm")
    external_report_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
```

- `engine` 默认 `"vlm"`，老 run 全部默认这个值；新 run 由 `runs.create_run` 从
  请求 body 读取，再透传给 `MSG_START_RUN.engine`，agent 收到后落库
- `external_report_url` 仅在外接引擎（如 Midscene）时由
  `_finalize_run` 从 `MSG_RUN_DONE.external_report_url` 写入；vlm 路径永远是 NULL
- 两个字段都已加入 `Run.to_dict()`，`/api/runs/{id}` 直接可见，前端依赖此字段
  渲染"打开外部报告 →"链接
- schema 重建（与 v1.7 同套路），测试阶段直接 drop & create 即可

### 4.5 Web UI（`web/src/pages/DeviceWork.vue`）

实际入口在"单设备工作页"`DeviceWork.vue` 而不是 `ManualRun.vue`（仓库里手动调试
run 的入口长这样：先抢锁、然后在 goal 输入框下面提交）。在 goal 输入框上方插入
一个**原生 `<select>`**（项目本身没引 element-plus，保持轻量）：

```html
<div v-if="midsceneEnabled" class="engine-row">
  <label>执行引擎</label>
  <select v-model="selectedEngine" :disabled="!!currentRunId || lock.readonly.value">
    <option value="vlm">vlm（默认 / ai-phone 主链路）</option>
    <option value="midscene">midscene（外接寄居）</option>
  </select>
</div>
```

- `midsceneEnabled` 在 `onMounted` 调 `api.getConfig()` 读取
  （后端 `/api/config` 暴露 `{midscene_enabled: bool}`）
- 失败兜底：`midsceneEnabled = false`，下拉框完全不渲染，新人 clone 后
  无配置即"看不见这个能力"，不会误触
- `selectedEngine` 默认 `'vlm'`，`startRun` 时塞进 `createRun({...,
  engine: selectedEngine.value || 'vlm'})`
- 批次投递页（`Queue.vue`）**完全不动**，不暴露 engine 字段

**run 结束后展示报告链接**（同一页 right 栏）：

- `run_done` 事件触发时调一次 `api.getRun(finishedId)` 刷新 `currentRun.value`，
  把 `external_report_url` 拉过来
- 模板侧仅在 `currentRun.external_report_url` 有值时渲染：

  ```html
  <p v-if="!currentRunId && currentRun?.external_report_url" class="info">
    <a :href="currentRun.external_report_url" target="_blank" rel="noopener">
      打开 {{ currentRun.engine || 'external' }} 报告 →
    </a>
  </p>
  ```

- vlm run 永远没有 `external_report_url`，链接自然不会出现，旧路径完全无感

**停止按钮共用（前后端零分叉）**：

- 前端永远是同一个"停止"按钮 → `POST /api/runs/{id}/stop`
- 后端 `_handle_stop_run` → `task.cancel()` → runner 内部各自处理：
  - vlm：`VLMRunner.run()` 自捕获 `CancelledError`，发 `EVT_RUN_FINISH(ok=False, reason="cancelled")` 后正常返回
  - midscene：`MidsceneRunner.run()` 同样自捕获，先 SIGTERM/SIGKILL 子进程，
    再发 `EVT_RUN_FINISH(ok=False, reason="cancelled: stopped_by_user")`
- 两边都**不 re-raise**，外层 `_run_task` 的 `except CancelledError` 实际从不触发，
  避免重复发 MSG_RUN_DONE
- 用户视角无差异，按钮 / 文案 / 状态机全部共用

### 4.6 Bridge stdout 协议

bridge 进程退出前必须在 stdout 打印一个**单行 JSON**（其余行任意，会被忽略）：

```
{"result":"pass","report":"/abs/path/to/report.html"}
```

或

```
{"result":"fail","report":"/abs/path/to/report.html","reason":"<错误描述>"}
```

或

```
{"result":"error","report":null,"reason":"<错误描述>"}
```

- `result ∈ {"pass","fail","error"}` 三选一；其它值或解析失败一律降级为 `error`
- `report` 字段：可空。bridge 自己负责从 Midscene HTML 输出目录里挑出最新的
  报告路径；为空时 web 不显示"打开报告"链接
- `reason`：可选。在 `fail` / `error` 时 bridge 应填一个简短人类可读字符串，
  会被前缀化为 `"<run_result>: <reason>"` 写进 RunLog
- MidsceneRunner 用 `_parse_bridge_stdout` **倒序遍历**所有非空行，找第一个
  `json.loads` 成功且开头 `{` 结尾 `}` 的行；这样就算 Midscene 自己往 stdout
  灌了几十行调试信息也不会污染协议
- `report` 字段允许相对路径 / 绝对路径 / `file://` URL：
  `_normalize_report_url` 会统一转成可被 ai-phone `/files/` 静态服务到的路径，
  最终落到 `Run.external_report_url`

---

## 5. ENV 与配置

### 5.1 ai-phone 这边只加一个开关（`config.py` + `.env.example`）

```python
# config.py
midscene_enabled: bool = Field(
    default=False,
    description="是否暴露 Midscene 执行器选项。默认关闭。"
)
midscene_run_timeout_sec: int = Field(
    default=60 * 60,
    description="Midscene run 单次硬超时（秒）。默认 60 分钟，留出长链 case 余量。",
)
```

```bash
# backend/.env.example
AI_PHONE_MIDSCENE_ENABLED=false
AI_PHONE_MIDSCENE_RUN_TIMEOUT_SEC=3600
```

**就这两个开关，无别的配置**。Midscene 怎么跑、用什么模型、缓存怎么用、报告什么参数，全在 bridge 自己的 `.env.midscene` 里管。

> **超时为什么默认设 60 分钟而不是 30 分钟（与 vlm 对齐）**？
> Midscene 移动端走"plan + 每步 locate 调模型"的视觉循环（无 locate 缓存），长链 case（≥ 30 步）合理耗时可能超过 30 分钟。**为了不阉割长任务能力**，默认值放到 60 分钟；如果业务 case 永远短链，可以通过 ENV 调小。

### 5.2 ENV 透传白名单

`MidsceneRunner._build_env()` 严格控制传给 Node 子进程的 ENV：

```python
def _build_env(self, report_dir: Path) -> Dict[str, str]:
    # 1. 不继承 os.environ（避免把 AI_PHONE_VLM_API_KEY 等密钥暴露给 bridge）
    env = {}
    # 2. 系统级必须项
    for k in ("PATH", "HOME", "LANG", "LC_ALL", "USER", "TMPDIR",
              "ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    # 3. 显式传给 bridge 的运行参数
    env["AI_PHONE_MIDSCENE_REPORT_DIR"] = str(report_dir)
    env["AI_PHONE_MIDSCENE_RUN_ID"] = self.run_id
    env["AI_PHONE_MIDSCENE_SERIAL"] = self.serial
    # 4. bridge 自己的 .env.midscene 由 dotenv 在 src/run.ts 里加载
    return env
```

**关键**：ai-phone 后端的所有 `AI_PHONE_*` 变量**全部不传**。bridge 想要什么自己写 `.env.midscene`。

### 5.3 `midscene-bridge/.env.midscene.example`

提交到 git 的模板（实际 `.env.midscene` 在 `.gitignore` 里）：

```bash
# === Midscene 自己的配置（与 ai-phone 主仓 .env 完全独立）===
# 升级 ai-phone vlm 模型时记得手工同步这一份

# Midscene 用的 VLM（与 ai-phone 主链路同款）
OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
OPENAI_API_KEY=<同 ai-phone 后端 AI_PHONE_VLM_API_KEY>
MIDSCENE_MODEL_NAME=doubao-seed-1-6-vision-250815

# 视觉模型族声明（Midscene 1.0+ 推荐 MIDSCENE_MODEL_FAMILY；旧 MIDSCENE_USE_* 仍兼容但已废弃，参考 https://midscenejs.com/zh/model-strategy.html）
MIDSCENE_MODEL_FAMILY=doubao-seed-1.6
# 旧版本兼容写法（Midscene <1.0 时使用，新版本无需）：
# MIDSCENE_USE_VLM_UI_TARS=0
# MIDSCENE_USE_QWEN_VL=0

# 调试 / 日志
MIDSCENE_DEBUG_AI_PROFILE=1
MIDSCENE_DEBUG_AI_RESPONSE=0

# Android 工具链
ANDROID_HOME=/Users/<you>/Library/Android/sdk
```

**手工维护**：升级 doubao 模型时记得改两处（ai-phone 主 `.env` 和这份 `.env.midscene`）。这是寄居哲学的代价。

---

## 6. 终止 run 行为（共用按钮，后端多态）

### 6.1 用户视角

只有一个"停止 run"按钮，无论 engine 是什么，行为一致：

- 点击 → 后端 cancel runner → run 状态变 `stopped` → web 显示停止结果

### 6.2 后端实现

`AbstractRunner.cancel()` 是多态接口：

| 引擎 | cancel 实现 |
|---|---|
| vlm | 现有：抛 `asyncio.CancelledError`，主循环 finally 块清理 |
| midscene | SIGTERM 子进程 → 等 5 秒 → 仍未退则 SIGKILL → wait |

### 6.3 硬超时

无论用户主动停还是任务自然超时，都走相同 cancel 路径：

- vlm：现有超时机制
- midscene：默认 60 分钟硬上限（`AI_PHONE_MIDSCENE_RUN_TIMEOUT_SEC` 可调，给长链 case 留余量），超时即触发 cancel

---

## 7. 报告产物路径（落 ai-phone storage）

### 7.1 路径规约

```
<storage_dir>/external-reports/midscene/<run_id>/
    ├── report/                # Midscene 自动生成的 HTML 报告（带视频回放/局部缩放）
    │   └── android-<ts>-<hash>.html
    ├── cache/                 # Midscene 自动生成的 cache 文件（write-only 模式落盘）
    │   └── <run_id>.cache.yaml
    ├── replay.yaml            # bridge 跑完后扒 cache 自动打包，可被 cli 直接重放
    └── ...其它 Midscene 产物
```

**`replay.yaml` 是什么 / 为什么有它**：

Midscene 跑 `aiAct(goal)` 时，会自动把模型 plan 出来的具体动作（aiTap/aiInput/sleep 等）序列化成 yaml flow，塞进 cache 文件的 `yamlWorkflow` 字段。但 cache 文件本身是 cache 格式（`midsceneVersion / caches[]` 嵌套结构），**不能直接给 `npx midscene` 跑**。bridge 跑完后会读 cache 文件、扒出 `yamlWorkflow`、拼上顶层 `android: / agent:` 配置，落成独立的 `replay.yaml` —— 同事/跨设备拿到这个文件就能直接：

```bash
cd midscene-bridge
export $(grep -v '^#' .env.midscene | xargs)
npx midscene <storage_dir>/external-reports/midscene/<run_id>/replay.yaml
```

**cache 走 `write-only`**（`run.ts:153-156`）：只写不读 → ai-phone 主链路行为 100% 不变（永远全程调 LLM，不会因为命中历史 cache 跳过 plan）。cache 文件只是"副产物"，存在唯一目的就是给 bridge 抠 `yamlWorkflow` 用。永远不要把它改成 `read-write` —— Midscene cache 命中错元素时不会校验（假阳性陷阱），用在 ai-phone 的"调试 / 探索"场景下害大于利。

外部 URL：

```
/files/external-reports/midscene/<run_id>/report.html
```

`StaticFiles` 已经把 `<storage_dir>` 整体 mount 到 `/files`，**不需要新增挂载**。

### 7.2 怎么让 Midscene 落到这里

bridge 的 `src/run.ts` 在调 Midscene SDK 前设环境变量（Midscene 支持 `MIDSCENE_REPORT_DIR` 一类的 ENV，具体看它当时的版本文档）：

```typescript
process.env.MIDSCENE_RUN_DIR = process.env.AI_PHONE_MIDSCENE_REPORT_DIR;
// 或它的 SDK 配置
```

跑完后从 Midscene 拿到实际产物的绝对路径，转成 `/files/...` 形式写到 stdout 给 Python 端。

### 7.3 `run.external_report_url` 字段

`Run` 表已有 `engine` 字段，再加一个 `external_report_url`（可空）：

```python
external_report_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
```

vlm run 永远是 null；midscene run 跑完写入 `/files/external-reports/midscene/<run_id>/report.html`。

---

## 8. 不做的事（明确边界）

为了防止落地过程中被悄悄扩展，下面这些事**这一期不做**：

| 项 | 原因 |
|---|---|
| iOS 通道 | Midscene iOS 能力同层无新意；WDA session 排他成本不划算 |
| Harmony 通道 | **上游已支持**（`@midscene/harmony`，HDC + 视觉），二期评估接入；本期暂缓的工程理由见 §2.2 |
| 批次投递（submission）支持 engine 字段 | 业务投递永远走 vlm |
| Mobile-Agent / 其它执行器 | 不在本方案范围 |
| Midscene 的 step 流 / token 统计接入 ai-phone timeline | 它日志怎么打就怎么打，我们不解析 |
| 横评 / 决策矩阵 / SQL view / 报告脚本 | 本期目的不是横评 |
| 同模型强制对照（关缓存对比） | 本期不做对比 |
| ai-phone driver 让位机制（ADB / scrcpy 主动停） | ADB 多 client 安全；mirror lazy 启动；Midscene yadb 不切默认 IME。默认共存 |
| Midscene run 期间的浏览器镜像独占遮罩 | 镜像状态用户无所谓 |
| ENV 全继承 | 已改为白名单透传 |
| 子进程 stdout/stderr 实时流式上报 web | 跑完一次性给一份就够了 |
| 修任何 Midscene 的 bug | 不二开 |

---

## 9. 卸载

### 9.1 一键关闭

```bash
AI_PHONE_MIDSCENE_ENABLED=false
```

重启后端，效果：

- web 不显示引擎下拉框
- API 收到 `engine=midscene` 直接 400
- 主链路 100% 与今天一致

### 9.2 完全卸载

```bash
# 删 bridge 目录
rm -rf ai-phone/midscene-bridge/

# 删 4 个改动点
#   agent/runner/factory.py
#   agent/runner/midscene_runner.py
#   web/src/pages/ManualRun.vue 引擎下拉框
#   models.py / config.py 的相关字段（可选，留着也无害）
```

不会残留任何耦合。

---

## 10. 工作量与阶段

### 10.1 总预算：3-4 个工作日

| 阶段 | 工作量 | 出口标准 |
|---|---|---|
| **阶段 0**：摸底 Midscene 是否能跑通 | 0.5 天 | `npx @midscene/cli ... --serial xxx --goal "..."` 在你常见的 3-5 条 Android case 上能产出 HTML 报告 |
| **阶段 1**：bridge 独立项目 | 1 天 | `node midscene-bridge/dist/run.js --serial xxx --goal "..." --report-dir /tmp/x` 能跑通，stdout 输出 §4.6 协议 JSON |
| **阶段 2**：MidsceneRunner + factory + Run schema | 1-1.5 天 | Internal API（`POST /api/runs` 带 `engine=midscene`）能跑通；停止按钮能 cancel |
| **阶段 3**：Web UI 引擎下拉框 + 报告按钮 + 5 条 case 自测 | 0.5-1 天 | 5 条 case 能在 web 上手动跑通、停止、看报告 |

### 10.2 阶段 0 失败的退出条件

如果 Midscene Android 在你的 3-5 条 case 上跑不通（fps、识别精度、稳定性任一明显不可用），**直接放弃整个方案**，文档归档备查。**不要进阶段 1**。

---

## 11. 验收

5 条手工 case 能在 web 上跑通即算验收（不做横评、不做对比）：

| case | 验证点 |
|---|---|
| 打开计算器 → 1+1= → 看结果 | 基础点击 + 输入 |
| 微信首页 → 切到通讯录 tab | 简单导航 |
| 设置 → 进入 wifi → 切换开关 | 多步骤 |
| 短文本输入 case（含中文）| yadb 中文输入路径走通 |
| 故意失败 case（"点击不存在的紫色按钮"） | result=fail 路径正常返回 |

每条 case：

- ✅ 能跑通到结束
- ✅ Web 上能看到 PASS/FAIL 状态
- ✅ 能点开 Midscene 报告
- ✅ 跑到一半点停止能立即终止

5 条都过 = 上线。

---

## 12. 给接手开发者的提醒

1. **不要把 ai-phone 的 ENV 继承给 bridge 子进程**——白名单透传严格执行（§5.2），否则密钥泄漏风险
2. **不要为 Midscene 改 ai-phone 的 driver / mirror / 主循环**——铁律 §1.2
3. **bridge 编译产物 `dist/` 不进 git**，仓库里只有 `src/`，部署时 `npm install && npm run build`
4. **新 Mac 部署时 bridge 要单独装**：`cd midscene-bridge && npm install && cp .env.midscene.example .env.midscene && 填密钥 && npm run build`，写进 §部署手册
5. **doubao 模型升级时**两份 .env 都要改：`backend/.env`（vlm runner 用）+ `midscene-bridge/.env.midscene`（midscene 用）
6. **stdout 协议固定**：bridge 任何错误最后都要 emit 一行合法 JSON（即使是 catch 兜底里的 error），否则 Python 端解析会失败误判成 error
7. **测试停止按钮一定要测真机**：起一条慢 case 跑到中段再点停，确保 SIGTERM → SIGKILL 路径完整能终止 Node 子进程

---

## 13. 变更记录

| 日期 | 版本 | 内容 |
|---|---|---|
| 2026-04-29 | v1.0 | 首版：项目寄居 + 手动 run 入口 + Android Only + 同 doubao vision |
| 2026-04-29 | v1.1 | 阶段 1-3 落地后回填实际实现：§4.3 BRIDGE_DIR 由 `settings.midscene_bridge_dir` 配置或自动寻址、`midscene_node_bin` 可配置、错误兜底改为 `_emit_run_finish(ok=False, reason="error: …")`、CancelledError 不 re-raise（与 vlm 对齐）；§4.4 加 `external_report_url` 字段说明；§4.5 入口实际在 `DeviceWork.vue`（用原生 `<select>`）+ run_done 触发刷新拿报告 URL；§4.6 stdout 解析改为倒序找 JSON、`_normalize_report_url` 兜底相对/绝对/`file://` 三种写法；新增 `/api/config` 端点暴露 `midscene_enabled` |
| 2026-04-30 | v1.2 | §7.1 路径规约扩充：bridge 改造支持"sdk 跑后留 yaml → cli 重放"——AndroidAgent 构造加 `cache: { id: runId, strategy: 'write-only' }`（write-only 严格保证 ai-phone 主链路行为零变化），跑完后 `dumpReplayYaml()` 从 cache 文件抠 `yamlWorkflow` 拼上顶层 `android/agent` 配置另存 `replay.yaml`；同事/跨设备拿到这一个文件即可 `npx midscene replay.yaml` 重放，不依赖 ai-phone 服务运行 |
| 2026-05-01 | v1.3 | **对 Midscene 描述做事实校准（公允性修正）**：① §1.5 "locate 缓存：原生" 改为"移动端**无** locate 缓存，每步仍调模型 locate；locate 缓存是 Web 端 XPath 专属"，并把 instant action 列表展开成 aiTap / aiInput 等具体 API；② §1.5 末尾 "iOS / Harmony / 批次投递入口不开放，是路由层选择不是能力阉割"修订为 "Harmony 上游已支持，仅本期路由层暂未启用"；③ **新增 §1.6 执行模式与能力对照（客观说明）**：分 5 小节正面回应"早期调研结论是否依然成立"——核心执行模式（plan + 每步 locate）一年前到今天没变 / 兜底仍是 fallback + replan 两档 / 移动端命中 planning 缓存仍需 N 次 locate / 这一年真增量（Scrcpy 截图 / 多模型分意图 / HarmonyOS / PC / MCP / Skills / Android Planning Cache）/ Midscene Playground 不是 ai-phone 那种业务测试通道；④ §2.2 第 2 行 / §8 表格 Harmony 行：从"❌ 上游不支持" 改为"⏸ 上游已支持 `@midscene/harmony`，本期暂缓接入（HDC fport / uitest daemon 资源争用未真机摸底等工程理由）"；⑤ §5.1 60 分钟超时小贴士的"planning + locate 缓存"修正为"plan + 每步 locate 调模型（无 locate 缓存）"；⑥ §5.3 .env.midscene.example：`MIDSCENE_USE_VLM_UI_TARS / MIDSCENE_USE_QWEN_VL` 注释为旧版兼容写法，新增推荐的 `MIDSCENE_MODEL_FAMILY`。本次校准的源码与实测证据见 [执行架构对比.md §11.6.5 / §11.6.7](./docs-internal/执行架构对比.md)，未触动本方案"项目寄居 + 一行 run 入口 + 不二开"的整体设计 |

