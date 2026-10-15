"use strict";
// No product/CLI changes, native devices, real profiles or remote endpoints.
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const CLI = "/Users/dongxin/aiphonecli开源/bin/aiphone.js";
const bases = { android: "/api/internal/vm", ios: "/api/internal/ios-sim", harmony: "/api/internal/harmony-vm" };

async function runScenario(platform, scenario) {
  const base = bases[platform] + "/instances";
  const calls = [];
  let stopFailedOutcome = false;
  let startAfterFailedStop = false;
  const runtimeFields = data => platform === "ios"
    ? { runtime: "com.apple.CoreSimulator.SimRuntime.iOS-26-0", runtime_state: data }
    : { runtime: data };
  let vm = { id: "old-vm", alias: "personal-compat", state: scenario === "late_failed_stop" ? "running" : scenario === "delete_offline" ? "agent_offline" : "stopped",
    assigned_agent_id: "old-agent", adb_serial: "emulator-5666", udid: "personal-test-udid", ...runtimeFields({}) };
  const serial = () => platform === "harmony" ? "harmony-vm:" + vm.id : platform === "ios" ? vm.udid : vm.adb_serial;
  const op = (action, outcome) => ({ action, request_id: action + "-attempt-1", pending: outcome === "pending", outcome,
    ...(outcome === "failed" ? { failure_reason: "operation_timeout" } : {}) });
  const server = http.createServer(async (req, res) => {
    const pathname = new URL(req.url, "http://127.0.0.1").pathname;
    calls.push({ method: req.method, path: pathname });
    const send = (value, status = 200) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(value)); };
    if (pathname === "/api/devices/statuses") return send(vm?.state === "running" ? [{ serial: serial(), platform,
      agent_id: vm.assigned_agent_id, status: "online", lock: null, extra: { readiness: { ready: true } } }] : []);
    if (pathname === "/api/devices/available") return send(vm?.state === "running" ? [{ serial: serial(), platform }] : []);
    if (pathname === base && req.method === "GET") return send(vm ? [vm] : []);
    if (!pathname.startsWith(base + "/")) return send({ detail: "Unexpected mock route" }, 404);
    const suffix = pathname.slice(base.length + 1).split("/");
    if (!vm || suffix[0] !== vm.id) return send({ detail: "retired or unknown VM" }, 404);
    if (!suffix[1] && req.method === "GET") return send(vm);
    if (!suffix[1] && req.method === "DELETE") {
      const id = vm.id; vm = null;
      return send({ id, deleted: true, alias_deleted: 1, cleanup_sent: false, avd_cleanup_sent: false });
    }
    if (suffix[1] === "dispatch-candidates") return send({ agents: [{ agent_id: "new-agent", agent_name: "personal mock", ok: true }] });
    if (suffix[1] === "dispatch") {
      const failed = scenario === "switch_new_start_failed";
      vm = { ...vm, id: "new-vm", state: failed ? "error" : "running", assigned_agent_id: failed ? null : "new-agent",
        error_message: failed ? "新配置启动失败：所属 Agent 离线" : "",
        ...runtimeFields(failed ? { last_status: { state: "error", ok: false, details: { cleanup_confirmed: true, instance_created: false } } }
          : { operation: op("start", "succeeded") }) };
      return send({ sent: !failed, switched: true, old_vm_id: "old-vm", old_cleanup_sent: false,
        instance: failed ? vm : { ...vm, state: "starting", assigned_agent_id: null, ...runtimeFields({ operation: op("start", "pending") }) } });
    }
    if (suffix[1] === "stop") {
      stopFailedOutcome = scenario === "late_failed_stop";
      const outcome = stopFailedOutcome ? "failed" : "succeeded";
      vm = { ...vm, state: "stopped", ...runtimeFields({ operation: op("stop", outcome),
        last_status: { state: "stopped", ok: true, details: { cleanup_confirmed: true } } }) };
      return send({ sent: true, instance: { ...vm, state: "stopping", ...runtimeFields({ operation: op("stop", "pending") }) } });
    }
    if (suffix[1] === "start") {
      startAfterFailedStop = stopFailedOutcome;
      vm = { ...vm, state: "running", ...runtimeFields({ operation: op("start", "succeeded") }) };
      return send({ sent: true, instance: { ...vm, state: "starting" } });
    }
    return send({ detail: "Unexpected mock operation" }, 404);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const args = scenario === "late_failed_stop"
    ? ["vm", platform, "restart", "old-vm", "--timeout", "2", "--interval", "0.01"]
    : scenario === "delete_offline"
      ? ["vm", platform, "delete", "old-vm"]
      : ["vm", platform, "dispatch", "old-vm", "--agent", "new-agent", "--wait", "--timeout", "2", "--interval", "0.01"];
  const env = { ...process.env };
  for (const name of Object.keys(env)) if (name.toLowerCase().endsWith("_proxy")) delete env[name];
  Object.assign(env, { AIPHONE_BASE_URL: `http://127.0.0.1:${port}`, AIPHONE_INTERNAL_TOKEN: "personal-mock-only",
    PERSONAL_COMPAT_PORT: String(port), NODE_OPTIONS: "--require " + path.join(__dirname, "cli-network-guard.cjs"),
    NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" });
  let result;
  try {
    result = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [CLI, ...args], { env });
      let stdout = "", stderr = "";
      const timer = setTimeout(() => { child.kill(); reject(new Error("mock CLI exceeded 10 seconds")); }, 10000);
      child.stdout.on("data", b => stdout += b);
      child.stderr.on("data", b => stderr += b);
      child.once("error", reject);
      child.once("exit", code => {
        clearTimeout(timer);
        const guardLine = stderr.split("\n").find(line => line.startsWith("PERSONAL_COMPAT_GUARD "));
        resolve({ exit_code: code, output: JSON.parse(stdout), guard: guardLine ? JSON.parse(guardLine.slice(22)) : null });
      });
    });
  } finally { await new Promise(resolve => server.close(resolve)); }
  assert.equal(result.guard?.profileReads, 0);
  assert.equal(result.guard?.loopbackOnly, true);
  if (scenario === "late_failed_stop") {
    assert.equal(result.exit_code, 0);
    assert.equal(startAfterFailedStop, true);
  } else if (scenario === "switch_new_start_failed") {
    assert.equal(result.exit_code, 1);
    assert.equal(result.output.error.vm_id, "old-vm");
    assert.equal(result.output.error.response.instance.id, "new-vm");
  } else if (scenario === "switch_success") {
    assert.equal(result.exit_code, 0);
    assert.equal(result.output.status.instance.id, "new-vm");
  } else {
    assert.equal(result.exit_code, 0);
    assert.equal(result.output.host_cleanup_confirmed, false);
  }
  return { platform, scenario, args, ...result, start_after_failed_stop: startAfterFailedStop, calls };
}

(async () => {
  const result = { audited_at_utc: new Date().toISOString(), personal_cli: CLI,
    scope: "Loopback mock contract replay only. CLI source unchanged; real profile blocked; no native operations.", results: [] };
  for (const platform of Object.keys(bases)) for (const scenario of ["late_failed_stop", "switch_new_start_failed", "switch_success", "delete_offline"]) {
    result.results.push(await runScenario(platform, scenario));
  }
  fs.writeFileSync(path.join(__dirname, "cli-contract-repro.json"), JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(JSON.stringify(result.results.map(r => ({ platform: r.platform, scenario: r.scenario,
    exit_code: r.exit_code, start_after_failed_stop: r.start_after_failed_stop, profile_reads: r.guard.profileReads,
    error_vm_id: r.output.error?.vm_id, replacement_id: r.output.error?.response?.instance?.id || r.output.status?.instance?.id })), null, 2) + "\n");
})().catch(err => { console.error(err.stack); process.exitCode = 1; });
