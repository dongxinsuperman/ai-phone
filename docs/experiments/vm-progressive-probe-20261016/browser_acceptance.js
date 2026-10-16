async (page) => {
  const test = page.__progressiveProbe
  if (!test || new URL(test.page.url()).origin !== test.base) throw new Error('Dedicated personal fixture required')
  const p = test.page
  const check = (ok, label, detail) => {
    test.checks.push({ passed: Boolean(ok), label, detail })
    if (!ok) throw new Error(label)
  }
  const state = async () => (await p.request.get(test.base + '/fixture/state')).json()
  const waitIdle = async () => {
    for (let i = 0; i < 100; i += 1) {
      const current = await state()
      if (Object.values(current.pending).every(n => n === 0)) return current
      await p.waitForTimeout(20)
    }
    throw new Error('Probe state was not released')
  }
  for (const [platform, label, domain] of [
    ['android', 'Android 虚拟机', 'vm'],
    ['harmony', '鸿蒙虚拟机', 'harmony-vm'],
    ['ios', 'iOS 虚拟机', 'ios-sim'],
  ]) {
    await p.getByRole('button', { name: label, exact: true }).click()
    const section = p.locator('section.page:visible')
    const card = key => section.locator(`.vm-card:has([title="vm_id: stream-${platform}-${key}"]), .card:has([title="vm_id: stream-${platform}-${key}"])`)
    const pick = card('pick')
    await pick.getByRole('button', { name: '探查', exact: true }).click()
    await pick.getByText('已就绪的测试电脑', { exact: true }).waitFor()
    const rows = pick.locator('.candidate')
    check(await rows.count() === 4, platform + ': complete online list is visible')
    const fast = rows.filter({ hasText: '已就绪的测试电脑' })
    await fast.getByRole('button', { name: '下发', exact: true }).waitFor()
    check(await fast.getByRole('button', { name: '下发', exact: true }).isEnabled(), platform + ': fast Agent can dispatch before batch completion')
    for (const slowName of ['发送较慢的测试电脑', '响应较慢的测试电脑']) {
      const slow = rows.filter({ hasText: slowName })
      check(await slow.getByRole('button').isDisabled() && (await slow.innerText()).includes('探查中'), platform + ': ' + slowName + ' remains pending independently')
    }
    check((await state()).pending[platform] === 1, platform + ': fast row enabled while real Server batch is still open')
    await pick.screenshot({ path: test.output + '/' + platform + '-fast-ready.png', scale: 'css' })
    test.screenshots.push(platform + '-fast-ready.png')
    check((await state()).pending[platform] === 1, platform + ': batch remains unfinished immediately before dispatch click')
    const dispatched = p.waitForResponse(r => r.url().endsWith(`/stream-${platform}-pick/dispatch`))
    await fast.getByRole('button', { name: '下发', exact: true }).click()
    check((await dispatched).status() === 200, platform + ': actual dispatch route accepts the chosen Agent')
    await waitIdle()
    const sent = (await state()).events.filter(e => e.event === 'lifecycle_transport_only' && e.vm_id === `stream-${platform}-pick`)
    check(sent.length === 1 && sent[0].agent_id === 'c-fast', platform + ': exactly one lifecycle send to the selected Agent')
    check(await pick.locator('.candidate').count() === 0, platform + ': successful dispatch closes the probe list')

    const timeout = card('timeout')
    await timeout.getByRole('button', { name: '探查', exact: true }).click()
    await timeout.getByText('已就绪的测试电脑', { exact: true }).waitFor()
    const initialOrder = await timeout.locator('.candidate strong').allTextContents()
    await timeout.getByRole('button', { name: '超时', exact: true }).first().waitFor()
    await waitIdle()
    check(await timeout.getByRole('button', { name: '超时', exact: true }).count() === 2, platform + ': slow send and slow reply each reach a terminal timeout')
    check(await timeout.locator('.candidate').filter({ hasText: '已就绪的测试电脑' }).getByRole('button', { name: '下发', exact: true }).isEnabled(), platform + ': another Agent timeout does not disable the good row')
    check(JSON.stringify(await timeout.locator('.candidate strong').allTextContents()) === JSON.stringify(initialOrder), platform + ': result updates preserve row positions')
    const timeoutRows = await timeout.locator('.candidate').allInnerTexts()
    check(timeoutRows.some(text => text.includes('通道异常的测试电脑') && !text.includes('探查中')), platform + ': failed send is visible independently')
    await timeout.screenshot({ path: test.output + '/' + platform + '-independent-timeout.png', scale: 'css' })
    test.screenshots.push(platform + '-independent-timeout.png')
    await timeout.getByRole('button', { name: '收起', exact: true }).click()

    const close = card('close')
    await close.getByRole('button', { name: '探查', exact: true }).click()
    await close.getByText('已就绪的测试电脑', { exact: true }).waitFor()
    await close.getByRole('button', { name: '收起', exact: true }).click()
    await waitIdle()
    await p.waitForTimeout(1000)
    check(await close.locator('.candidate').count() === 0, platform + ': closing does not reopen on a late reply')
    const late = (await state()).events.filter(e => e.event === 'late_reply')
    check(late.length > 0 && late.every(e => e.accepted === false), platform + ': cancelled request replies are rejected by Server')

    const endpoint = test.base + `/api/internal/${domain}/instances/stream-${platform}-timeout/dispatch-candidates`
    const legacy = await p.request.post(endpoint, { headers: { Authorization: 'Bearer dev' } })
    const json = await legacy.json()
    check(legacy.status() === 200 && typeof json.request_id === 'string' && json.agents.length === 4 && json.agents.every(row => !('probe_state' in row)), platform + ': existing JSON client contract remains intact')
    const denied = await p.request.post(endpoint + '?stream=true', { headers: { Authorization: 'Bearer wrong-fixture-token' } })
    check(denied.status() === 401 || denied.status() === 403, platform + ': stream retains authorization check')
  }
  const end = await waitIdle()
  check(end.blocked.length === 0, 'fixture made no blocked external/native call', end.blocked)
  check(test.blockedUrls.length === 0, 'browser made no non-fixture request', test.blockedUrls)
  check(test.runtimeErrors.length === 0, 'no browser runtime exception', test.runtimeErrors)
  test.summary = { passed: test.checks.filter(x => x.passed).length, failed: test.checks.filter(x => !x.passed).length, screenshots: test.screenshots.length }
  return { checks: test.checks, summary: test.summary, runtimeErrors: test.runtimeErrors, blockedUrls: test.blockedUrls, screenshots: test.screenshots, scope: 'Built Vue UI and actual FastAPI VM routes with controlled fake Agents; transport acceptance only, no native VM execution.' }
}
