// Private Playwright BrowserServer; no existing profile, app tab or MCP browser.
const fs = require('node:fs')
const path = require('node:path')
const { performance } = require('node:perf_hooks')

async function main() {
  const runtimeFile = path.resolve(process.argv[2] || '')
  const modulePath = process.argv[3]
  if (!modulePath || !runtimeFile.startsWith(path.join(__dirname, 'runs') + path.sep)) throw new Error('Exact owned runtime and installed Playwright are required')
  const runtime = JSON.parse(fs.readFileSync(runtimeFile, 'utf8'))
  const output = path.dirname(runtimeFile)
  const url = new URL(runtime.url)
  if (runtime.scope !== 'personal-simple-fake-native' || runtime.output !== output || url.hostname !== '127.0.0.1'
    || url.protocol !== 'http:' || [8000, 5180].includes(Number(url.port)) || !runtime.auth_token) throw new Error('Wrong private fixture identity')
  const scratch = fs.mkdtempSync(path.join(output, 'browser-tmp-'))
  const previousTmp = process.env.TMPDIR
  process.env.TMPDIR = scratch
  const env = { ...process.env, TMPDIR: scratch }
  for (const key of Object.keys(env)) if (/proxy$/i.test(key) || /^(AI_PHONE_|AIPHONE_)/.test(key)) delete env[key]
  const t = { base: runtime.url, output, token: runtime.auth_token, runId: runtime.run_id,
    checks: [], screenshots: [], runtimeErrors: [], blockedUrls: [] }
  const started = performance.now()
  let server, browser, context, child, report, failure
  const cleanupErrors = []
  const bounded = async (promise, milliseconds, label) => {
    let timer
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(label + ' deadline expired')), milliseconds)
      })])
    } finally { clearTimeout(timer) }
  }
  try {
    const { chromium } = require(modulePath)
    server = await chromium.launchServer({ headless: true, host: '127.0.0.1', port: 0, env, args: ['--no-proxy-server'] })
    child = server.process()
    fs.writeFileSync(path.join(output, 'browser-runtime.json'), JSON.stringify({
      pid: child.pid, argv: child.spawnargs, scratch, scope: 'exact newly spawned private BrowserServer',
    }, null, 2) + '\n')
    browser = await chromium.connect(server.wsEndpoint())
    context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, serviceWorkers: 'block' })
    await context.route('**/*', route => {
      const requestUrl = route.request().url()
      if (new URL(requestUrl).origin === t.base) return route.continue()
      t.blockedUrls.push(requestUrl)
      return route.abort()
    })
    await context.addInitScript(({ token, origin }) => {
      if (location.origin === origin) localStorage.setItem('aiPhoneInternalToken', token)
    }, { token: t.token, origin: t.base })
    const page = await context.newPage()
    page.__simpleLifecycle = t
    page.on('pageerror', error => t.runtimeErrors.push(String(error)))
    page.setDefaultTimeout(15000)
    await page.goto(t.base + '/virtual-machines')
    await page.getByRole('button', { name: 'Android 虚拟机', exact: true }).waitFor()
    const acceptance = eval('(' + fs.readFileSync(path.join(__dirname, 'browser_acceptance.js'), 'utf8') + ')')
    report = await bounded(acceptance(page), 180000, 'browser acceptance')
    report.status = 'passed'
  } catch (error) {
    failure = error
    report = { status: 'failed', error: String(error), checks: t.checks, screenshots: t.screenshots,
      runtimeErrors: t.runtimeErrors, blockedUrls: t.blockedUrls }
    if (context?.pages()[0]) {
      await context.pages()[0].screenshot({ path: path.join(output, 'browser-failure.png'), fullPage: true }).catch(() => {})
    }
  } finally {
    for (const [resource, label] of [[context, 'context'], [browser, 'browser']]) {
      if (resource) await bounded(resource.close(), 5000, label + ' close').catch(error => cleanupErrors.push(String(error)))
    }
    if (server) {
      try { await bounded(server.close(), 10000, 'private browser server close') }
      catch (error) {
        cleanupErrors.push(String(error))
        await bounded(server.kill(), 5000, 'exact private browser kill').catch(value => cleanupErrors.push(String(value)))
      }
    }
    fs.rmSync(scratch, { recursive: true, force: true })
    report = report || { status: 'failed', error: 'Browser did not reach acceptance' }
    report.duration_ms = Math.round(performance.now() - started)
    report.playwright_version = require(path.join(modulePath, 'package.json')).version
    report.cleanup = { private_browser_pid: child?.pid, exit_code: child?.exitCode, signal_code: child?.signalCode,
      process_exited: !child || child.exitCode !== null || child.signalCode !== null, context_closed: true,
      scratch_removed: !fs.existsSync(scratch), errors: cleanupErrors }
    if (!report.cleanup.process_exited || !report.cleanup.scratch_removed || cleanupErrors.length) report.status = 'failed'
    if (previousTmp === undefined) delete process.env.TMPDIR
    else process.env.TMPDIR = previousTmp
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n')
    console.log(JSON.stringify({ status: report.status, summary: report.summary, cleanup: report.cleanup }))
  }
  if (failure) throw failure
  if (report.status !== 'passed') throw new Error('Private browser cleanup was not confirmed')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
