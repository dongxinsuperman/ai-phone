// Isolated browser process; no existing browser profile or user tabs are used.
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const { performance } = require('node:perf_hooks')

async function main() {
  const modulePath = process.argv[2]
  if (!modulePath) throw new Error('Pass an already installed Playwright module path; no install is performed')
  const { chromium } = require(modulePath)
  const output = __dirname
  const runtime = JSON.parse(fs.readFileSync(path.join(output, 'fixture-runtime.json'), 'utf8'))
  const base = runtime.url
  if (new URL(base).hostname !== '127.0.0.1') throw new Error('Personal loopback fixture required')
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'aiphone-probe-browser-'))
  const env = { ...process.env, TMPDIR: scratch }
  for (const key of Object.keys(env)) {
    if (/proxy$/i.test(key) || /^(AI_PHONE_|AIPHONE_)/.test(key)) delete env[key]
  }
  let browser, context
  const test = { base, output, checks: [], screenshots: [], runtimeErrors: [], blockedUrls: [] }
  const started = performance.now()
  try {
    browser = await chromium.launch({ headless: true, env, args: ['--no-proxy-server'] })
    context = await browser.newContext({ viewport: { width: 1280, height: 1000 }, serviceWorkers: 'block' })
    await context.route('**/*', route => {
      const url = route.request().url()
      if (new URL(url).origin === base) return route.continue()
      test.blockedUrls.push(url)
      return route.abort()
    })
    const page = await context.newPage()
    test.page = page
    page.__progressiveProbe = test
    page.on('pageerror', error => test.runtimeErrors.push(String(error)))
    await page.goto(base + '/virtual-machines')
    await page.getByRole('button', { name: 'Android 虚拟机', exact: true }).waitFor()
    const acceptance = eval('(' + fs.readFileSync(path.join(output, 'browser_acceptance.js'), 'utf8') + ')')
    const report = await acceptance(page)
    report.duration_ms = Math.round(performance.now() - started)
    report.playwright_version = require(path.join(modulePath, 'package.json')).version
    fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n')
    console.log(JSON.stringify(report.summary))
  } catch (error) {
    fs.writeFileSync(path.join(output, 'browser-failure.json'), JSON.stringify({
      error: String(error), checks: test.checks, screenshots: test.screenshots,
      runtimeErrors: test.runtimeErrors, blockedUrls: test.blockedUrls,
    }, null, 2) + '\n')
    throw error
  } finally {
    await context?.close()
    await browser?.close()
    fs.rmSync(scratch, { recursive: true, force: true })
  }
}

main().catch(error => { console.error(error); process.exitCode = 1 })
