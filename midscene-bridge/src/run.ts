/**
 * Midscene Bridge — 单文件入口
 *
 * 责任：
 *   1. 读 .env.midscene 自带配置（不读 ai-phone 主 .env，由 ai-phone Python 端的
 *      白名单透传决定哪些 ENV 进入这里的 process.env）
 *   2. 解析命令行参数 --serial / --goal / --report-dir / --run-id
 *   3. 把 --report-dir 落到 Midscene 的输出根目录（MIDSCENE_RUN_DIR）
 *   4. 实例化 AndroidAgent.aiAct(goal)，让 Midscene 自己跑完
 *   5. 退出前最后一行写一个固定 schema 的 JSON 给 Python 端解析（其它日志用 stderr）
 *   6. 收到 SIGTERM 时尽力优雅终止；超时由 Python 端 SIGKILL 兜底
 *   7. 跑完后把 Midscene 自动生成的 cache 里的 yamlWorkflow 扒出来打包成
 *      <reportDir>/replay.yaml，方便用 `npx midscene replay.yaml` 回放
 *      （cache 走 write-only：只写不读 → ai-phone run 行为不变 / 永远全程 LLM）
 *
 * 不做的事：
 *   - 不解析 Midscene 内部 step 流 / token 统计（让它原生输出，ai-phone 不消费）
 *   - 不做"绕过 Midscene 直接调 VLM"的事情（铁律：不阉割能力）
 *   - 不读 ai-phone 主仓的 .env / 任何 AI_PHONE_* 变量
 *
 * stdout JSON 协议（给 Python 端 parse 的最后一行）：
 *   {"result":"pass","report":"file:///abs/path/report.html"}        // 任务声称成功
 *   {"result":"fail","report":"file:///abs/path","reason":"..."}      // 任务声称失败
 *   {"result":"error","report":null,"reason":"<err>"}                 // 框架/网络/启动异常
 */

import * as fs from 'fs';
import * as path from 'path';

import * as dotenv from 'dotenv';
import * as yaml from 'js-yaml';

// dotenv 默认查 cwd 下 .env，明确指到 .env.midscene 避免和系统 .env 串台
const envFile = path.resolve(__dirname, '..', '.env.midscene');
if (fs.existsSync(envFile)) {
  dotenv.config({ path: envFile });
}

// -----------------------------------------------------------------------------
// 命令行参数解析（最小 argv 解析器，避免引入 yargs / commander 给寄居方加负担）
// -----------------------------------------------------------------------------
type Args = {
  serial: string;
  goal: string;
  reportDir: string;
  runId: string;
};

function parseArgs(argv: string[]): Args {
  const out: Partial<Args> = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    const next = argv[i + 1];
    switch (a) {
      case '--serial':
        out.serial = next;
        i++;
        break;
      case '--goal':
        out.goal = next;
        i++;
        break;
      case '--report-dir':
        out.reportDir = next;
        i++;
        break;
      case '--run-id':
        out.runId = next;
        i++;
        break;
      default:
        // 未知参数走 stderr 警告，不打断（Midscene 上游可能未来加新参数）
        process.stderr.write(`[bridge] unknown arg ignored: ${a}\n`);
    }
  }
  if (!out.serial || !out.goal || !out.reportDir || !out.runId) {
    throw new Error(
      'missing required args: --serial <s> --goal <g> --report-dir <d> --run-id <id>',
    );
  }
  return out as Args;
}

// -----------------------------------------------------------------------------
// stdout 协议输出
// -----------------------------------------------------------------------------
type FinalResult = {
  result: 'pass' | 'fail' | 'error';
  report: string | null;
  reason?: string;
};

function emitFinal(r: FinalResult): void {
  // 严格保证最后一行是 JSON。Python 端按"最后一行"解析。
  // 不主动 print 在最后追加换行的 JSON 之前的多行（让 Midscene 自己决定它怎么打日志）。
  process.stdout.write(JSON.stringify(r) + '\n');
}

// -----------------------------------------------------------------------------
// 主流程
// -----------------------------------------------------------------------------
async function main(): Promise<number> {
  let args: Args;
  try {
    args = parseArgs(process.argv);
  } catch (e: any) {
    emitFinal({ result: 'error', report: null, reason: `bad_args: ${e?.message || e}` });
    return 2;
  }

  // 把 report-dir 同时写到 ENV，让 Midscene 内部如果有 fallback 路径也走这里
  fs.mkdirSync(args.reportDir, { recursive: true });
  process.env.MIDSCENE_RUN_DIR = args.reportDir;
  process.env.MIDSCENE_RUN_ID = args.runId;

  // 容器化 / CI 下 Midscene 不允许 console 输出彩色码 → 关掉
  process.env.MIDSCENE_DEBUG_NO_COLOR = '1';

  process.stderr.write(
    `[bridge] start | serial=${args.serial} run_id=${args.runId} report_dir=${args.reportDir}\n`,
  );

  // 动态 require：如果 npm install 没装 @midscene/android，给清晰错误
  let AndroidAgent: any;
  let AndroidDevice: any;
  try {
    const mod = require('@midscene/android');
    // 兼容 Midscene 不同版本导出形态
    AndroidAgent = mod.AndroidAgent || mod.default?.AndroidAgent;
    AndroidDevice = mod.AndroidDevice || mod.default?.AndroidDevice;
    if (!AndroidAgent) throw new Error('AndroidAgent not exported by @midscene/android');
  } catch (e: any) {
    emitFinal({
      result: 'error',
      report: null,
      reason:
        `import_failed: @midscene/android 不可用，` +
        `请在 midscene-bridge 目录执行 npm install。原始错误：${e?.message || e}`,
    });
    return 3;
  }

  // 关键：Midscene 的具体构造形态以其当前版本文档为准
  // 这里按"AndroidDevice + AndroidAgent" 的常见组合落实，不可用时 fallback
  // 单参数 AndroidAgent({serial})。两条路径都跑过一遍：哪条不抛就用哪条。
  //
  // cache: write-only —— 只写不读：
  //   - 当前 run 行为不变（永远全程调 LLM，不会因为命中历史 cache 跳过 plan）
  //   - 但跑完会把 yamlFlow 写到 <reportDir>/cache/<runId>.cache.yaml
  //   - 我们后面把这个 cache 里的 yamlWorkflow 扒出来另存成 replay.yaml 给 cli 用
  //   - 详见 §5.1 / §11.6.3：Midscene cache 命中错元素时不会校验，所以 ai-phone
  //     主链路绝不能用 cache 加速；write-only 既保证留了"可重放副本"，又零行为变化
  const agentOpts: Record<string, unknown> = {
    cache: { id: args.runId, strategy: 'write-only' },
    generateReport: true,
  };
  let agent: any;
  try {
    if (AndroidDevice) {
      const device = new AndroidDevice(args.serial);
      // connect 在某些版本是 async；await 兜底
      if (typeof device.connect === 'function') {
        await device.connect();
      }
      agent = new AndroidAgent(device, agentOpts);
    } else {
      agent = new AndroidAgent({ serial: args.serial, ...agentOpts });
    }
  } catch (e: any) {
    emitFinal({
      result: 'error',
      report: null,
      reason: `agent_init_failed: ${e?.message || e}`,
    });
    return 4;
  }

  // 优雅 SIGTERM：转 cancel agent（如有），其它情况让 Python 端 SIGKILL 兜底
  let aborting = false;
  const onSignal = (sig: NodeJS.Signals) => {
    process.stderr.write(`[bridge] received ${sig}, aborting...\n`);
    aborting = true;
    try {
      if (typeof agent?.cancel === 'function') agent.cancel();
      else if (typeof agent?.abort === 'function') agent.abort();
    } catch (e) {
      process.stderr.write(`[bridge] agent cancel error: ${e}\n`);
    }
  };
  process.on('SIGTERM', onSignal);
  process.on('SIGINT', onSignal);

  // 主调用：aiAct(goal) 是 Midscene 推荐的"自然语言目标"入口
  try {
    await agent.aiAct(args.goal);
    if (aborting) {
      emitFinal({ result: 'error', report: getReportPath(args.reportDir), reason: 'aborted' });
      return 130;
    }
    emitFinal({ result: 'pass', report: getReportPath(args.reportDir) });
    return 0;
  } catch (e: any) {
    if (aborting) {
      emitFinal({
        result: 'error',
        report: getReportPath(args.reportDir),
        reason: `aborted: ${e?.message || e}`,
      });
      return 130;
    }
    // Midscene 抛错的语义层面：既可能是任务判定 fail（assert / aiAssert 路径），
    // 也可能是框架 error。两者无法 100% 区分，统一标 fail；error 留给上面的
    // import / init / signal 路径
    emitFinal({
      result: 'fail',
      report: getReportPath(args.reportDir),
      reason: e?.message || String(e),
    });
    return 1;
  } finally {
    // 尽量释放 ADB / WDA 连接；不抛，让 final emit 已经发出
    try {
      if (typeof agent?.destroy === 'function') await agent.destroy();
      else if (typeof agent?.close === 'function') await agent.close();
    } catch (e) {
      process.stderr.write(`[bridge] agent destroy error: ${e}\n`);
    }

    // 跑完后异步落 replay.yaml；任何异常都吞掉，只 stderr 提示（绝不影响 final emit）
    try {
      const replayPath = dumpReplayYaml({
        reportDir: args.reportDir,
        runId: args.runId,
        serial: args.serial,
        goal: args.goal,
      });
      if (replayPath) {
        process.stderr.write(`[bridge] replay yaml dumped: ${replayPath}\n`);
      }
    } catch (e) {
      process.stderr.write(`[bridge] dump replay yaml failed (ignored): ${e}\n`);
    }
  }
}

/**
 * 从 Midscene 写下的 cache 文件里抠出 yamlWorkflow，拼上顶层 android/agent
 * 配置，落成可被 `npx midscene replay.yaml` 直接重放的独立 yaml。
 *
 * Midscene 在 cache 文件里把 yamlWorkflow 存为字符串字段（agent.mjs:379 用
 * js_yaml.dump 序列化进去），格式形如：
 *   tasks:
 *     - name: <原 goal>
 *       flow:
 *         - aiTap: "..."
 *         - sleep: 1500
 *
 * 我们：
 *   1. 读 <reportDir>/cache/<runId>.cache.yaml
 *   2. 找 type=plan 的 caches[i] → 拿 yamlWorkflow 字符串
 *   3. yaml.load 反序列化成 { tasks: [...] }
 *   4. 拼上 android/agent 顶层 → yaml.dump 写到 <reportDir>/replay.yaml
 *
 * 找不到 cache 文件 / 没 plan 类型的记录 → 返回 null（不抛）
 *
 * 注意：deviceId 写死当前 run 的 serial。跨设备重放需手动改 yaml 顶层
 * `android.deviceId` 字段，或 cli 传 `--android.device-id <serial>` 覆盖。
 */
function dumpReplayYaml(opts: {
  reportDir: string;
  runId: string;
  serial: string;
  goal: string;
}): string | null {
  // Midscene 通过 MIDSCENE_RUN_DIR 派生 cache 子目录：MIDSCENE_RUN_DIR/cache/<id>.cache.yaml
  const cacheFile = path.join(opts.reportDir, 'cache', `${opts.runId}.cache.yaml`);
  if (!fs.existsSync(cacheFile)) {
    process.stderr.write(`[bridge] cache file not found, skip replay yaml: ${cacheFile}\n`);
    return null;
  }
  const cacheRaw = fs.readFileSync(cacheFile, 'utf8');
  const cacheObj = yaml.load(cacheRaw) as
    | { caches?: Array<{ type?: string; yamlWorkflow?: string }> }
    | undefined;
  const caches = cacheObj?.caches;
  if (!Array.isArray(caches) || caches.length === 0) {
    process.stderr.write(`[bridge] cache file has no caches[], skip replay yaml\n`);
    return null;
  }

  // 一次 aiAct 通常只产生一条 type=plan 的记录；多次调用时合并到 tasks[]
  const planRecords = caches.filter(
    (c) => c?.type === 'plan' && typeof c.yamlWorkflow === 'string' && c.yamlWorkflow.trim(),
  );
  if (planRecords.length === 0) {
    process.stderr.write(`[bridge] no plan record with yamlWorkflow, skip replay yaml\n`);
    return null;
  }

  type FlowItem = Record<string, unknown>;
  type TaskItem = { name: string; flow: FlowItem[] };
  const allTasks: TaskItem[] = [];
  for (const rec of planRecords) {
    try {
      const inner = yaml.load(rec.yamlWorkflow as string) as { tasks?: TaskItem[] } | undefined;
      const tasks = inner?.tasks;
      if (Array.isArray(tasks)) {
        for (const t of tasks) {
          if (t && Array.isArray(t.flow) && t.flow.length > 0) {
            allTasks.push({ name: t.name || opts.goal, flow: t.flow });
          }
        }
      }
    } catch (e) {
      process.stderr.write(`[bridge] failed to parse one yamlWorkflow record (ignored): ${e}\n`);
    }
  }
  if (allTasks.length === 0) {
    process.stderr.write(`[bridge] all yamlWorkflow records empty, skip replay yaml\n`);
    return null;
  }

  // 拼装 cli 入口格式：android + agent + tasks
  // agent 不带 cache 配置 —— 重放时跑全程 LLM/locate（不命中我们 write-only 写的 cache）；
  // 想要"二次重放命中 cache 加速"，cli 跑时手动加 --agent.cache.id <runId> --agent.cache.strategy read-only
  const replayDoc = {
    android: { deviceId: opts.serial },
    agent: { generateReport: true },
    tasks: allTasks,
  };

  // 用 lineWidth: -1 跟 Midscene 自身 cache 文件保持一致，避免长字符串被折行
  const replayYamlStr =
    `# Midscene replay yaml (auto-generated by ai-phone midscene-bridge)\n` +
    `# Source run: ${opts.runId}\n` +
    `# Original goal: ${opts.goal}\n` +
    `# Replay command:\n` +
    `#   cd midscene-bridge && export $(grep -v '^#' .env.midscene | xargs)\n` +
    `#   npx midscene <path-to-this-file>\n` +
    `# 跨设备重放：改下面 android.deviceId，或 cli 加 --android.device-id <serial>\n\n` +
    yaml.dump(replayDoc, { lineWidth: -1 });

  const replayPath = path.join(opts.reportDir, 'replay.yaml');
  fs.writeFileSync(replayPath, replayYamlStr, 'utf8');
  return replayPath;
}

/**
 * 找 Midscene 实际产出的报告 HTML 路径。
 * Midscene 的命名规则会变（不同版本有 timestamp / agentId 后缀），所以走"扫目录拿最新 .html"
 * 而不是写死文件名。找不到就返回 null。
 */
function getReportPath(reportDir: string): string | null {
  const candidates = [
    path.join(reportDir, 'report'),
    reportDir,
  ];
  for (const dir of candidates) {
    try {
      if (!fs.existsSync(dir)) continue;
      const entries = fs
        .readdirSync(dir, { withFileTypes: true })
        .filter((e) => e.isFile() && e.name.endsWith('.html'))
        .map((e) => path.join(dir, e.name));
      if (entries.length === 0) continue;
      // 按 mtime 取最新
      entries.sort(
        (a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs,
      );
      return `file://${entries[0]}`;
    } catch (e) {
      process.stderr.write(`[bridge] scan ${dir} failed: ${e}\n`);
    }
  }
  return null;
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    // 兜底兜底：如果 main 自己抛了没接住的，最后一行也要给合法 JSON
    try {
      emitFinal({
        result: 'error',
        report: null,
        reason: `unhandled: ${e?.message || e}`,
      });
    } catch {
      // ignore
    }
    process.exit(255);
  });
