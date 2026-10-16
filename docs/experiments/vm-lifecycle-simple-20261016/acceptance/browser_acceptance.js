async (page) => {
  const t = page.__simpleLifecycle
  if (!t || new URL(page.url()).origin !== t.base) throw new Error('Dedicated personal fixture required')
  const headers = { Authorization: `Bearer ${t.token}` }
  const fixtureHeaders = { 'x-personal-fixture-token': t.token }
  const check = (ok, label, detail) => {
    t.checks.push({ passed: Boolean(ok), label, detail })
    if (!ok) throw new Error(label)
  }
  const fixtureState = async () => (await page.request.get(t.base + '/fixture/state', { headers: fixtureHeaders })).json()
  const initial = await fixtureState()
  check(initial.scope === 'personal-simple-fake-native' && initial.run_id === t.runId, 'fixture ownership handshake')
  const rows = initial.scenarios
  const id = (platform, key) => Object.values(rows).find(row => row.platform === platform && row.case === key).vm_id
  const operation = vm => (vm.runtime_state || vm.runtime || {}).operation || {}
  const failure = vm => (vm.runtime_state || vm.runtime || {}).last_failure || null
  const validate = vm => {
    const value = vm.lifecycle
    check(value && ['draft', 'stopped', 'starting', 'running', 'stopping', null].includes(value.state)
      && typeof value.agent_online === 'boolean' && typeof value.notice === 'string' && typeof value.error === 'string'
      && ['start', 'stop', 'dispatch', 'delete', 'edit', 'copy'].every(key => typeof value.actions?.[key] === 'boolean'),
    `${vm.id}: real Server lifecycle shape`)
    return value
  }
  const until = async (read, predicate, label, timeout = 15000) => {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
      const value = await read()
      if (predicate(value)) return value
      await page.waitForTimeout(100)
    }
    throw new Error('Timed out: ' + label)
  }

  for (const [platform, label, domain] of [
    ['android', 'Android 虚拟机', 'vm'],
    ['ios', 'iOS 虚拟机', 'ios-sim'],
    ['harmony', '鸿蒙虚拟机', 'harmony-vm'],
  ]) {
    await page.getByRole('button', { name: label, exact: true }).click()
    const section = page.locator('section.page:visible')
    const card = key => section.locator(`.vm-card:has([title="vm_id: ${id(platform, key)}"]), .card:has([title="vm_id: ${id(platform, key)}"])`)
    const endpoint = key => t.base + `/api/internal/${domain}/instances/${id(platform, key)}`
    const get = async key => {
      const response = await page.request.get(endpoint(key), { headers })
      check(response.status() === 200, `${platform}/${key}: real GET route`)
      return response.json()
    }
    const counts = async key => (await fixtureState()).scenarios[id(platform, key)].calls
    const screenshot = async (key, suffix) => {
      const name = `${platform}-${suffix}.png`
      await card(key).screenshot({ path: t.output + '/' + name, scale: 'css' })
      t.screenshots.push(name)
    }
    const expectState = async (key, text) => {
      await card(key).locator('.state').filter({ hasText: text }).waitFor()
      check((await card(key).locator('.state').innerText()).trim() === text, `${platform}/${key}: visible ${text}`)
    }
    const click = async (key, action) => {
      const response = page.waitForResponse(r => r.url() === endpoint(key) + '/' + action && r.request().method() === 'POST')
      await card(key).getByRole('button', { name: action === 'start' ? '启动' : '停止', exact: true }).click()
      return response
    }
    const waitTerminal = async (key, outcome, oldId) => until(() => get(key), vm => {
      const op = operation(vm)
      return op.request_id && op.request_id !== oldId && op.outcome === outcome && op.pending === false
    }, `${platform}/${key} exact new ${outcome}`)

    await card('busy').waitFor()
    const states = {}
    for (const key of ['stale_cleanup', 'start_failure', 'stop_failure', 'offline', 'unknown_stopped', 'unknown_running', 'busy']) {
      states[key] = await get(key)
      validate(states[key])
    }
    const actionOrder = ['start', 'stop', 'dispatch', 'delete', 'edit', 'copy']
    const expectedActions = {
      stale_cleanup: [true, false, true, true, true, true], start_failure: [true, false, true, true, true, true],
      stop_failure: [false, true, false, false, true, true], offline: [false, false, true, true, true, true],
      unknown_stopped: [true, true, true, true, true, true], unknown_running: [true, true, true, true, true, true],
      busy: [false, false, false, false, false, false],
    }
    for (const [key, expected] of Object.entries(expectedActions)) {
      check(JSON.stringify(actionOrder.map(action => states[key].lifecycle.actions[action])) === JSON.stringify(expected),
        `${platform}/${key}: all six Server action permissions match the case`, states[key].lifecycle.actions)
    }
    check(states.stale_cleanup.lifecycle.state === 'stopped' && states.stale_cleanup.lifecycle.actions.start
      && !states.stale_cleanup.lifecycle.actions.stop, platform + ': fresh stopped observation overrides old cleanupFalse')
    const legacyRuntime = states.stale_cleanup.runtime_state || states.stale_cleanup.runtime
    const legacyDetails = legacyRuntime.last_status?.details || legacyRuntime.details
    check(legacyDetails.cleanup_confirmed === false && legacyDetails.instance_observation.resources_released === true,
      platform + ': stale false flag and fresh released proof both reached real Server')
    check(states.start_failure.lifecycle.state === 'stopped' && states.start_failure.lifecycle.error.includes('启动失败')
      && states.start_failure.lifecycle.actions.start && !states.start_failure.lifecycle.actions.stop,
    platform + ': failed start remains visibly stopped and normally startable')
    check(states.stop_failure.lifecycle.state === 'running' && states.stop_failure.lifecycle.error.includes('停止失败')
      && states.stop_failure.lifecycle.actions.stop && !states.stop_failure.lifecycle.actions.start,
    platform + ': failed stop remains visibly running and normally stoppable')
    check(states.offline.lifecycle.state === null && !states.offline.lifecycle.agent_online
      && !states.offline.lifecycle.actions.start && !states.offline.lifecycle.actions.stop
      && states.offline.lifecycle.actions.delete && states.offline.lifecycle.actions.dispatch,
    platform + ': offline owner disables start/stop but retains retire/switch options')
    for (const key of ['unknown_stopped', 'unknown_running']) {
      check(states[key].lifecycle.state === null && states[key].lifecycle.actions.start,
        `${platform}/${key}: ordinary Start may inspect unknown state`)
      await expectState(key, '状态未确认')
    }
    check(states.busy.lifecycle.state === 'starting' && Object.values(states.busy.lifecycle.actions).every(value => value === false),
      platform + ': in-progress forbids every conflicting action')
    await expectState('stale_cleanup', '已停止')
    await expectState('start_failure', '已停止')
    await expectState('stop_failure', '运行中')
    await expectState('offline', '状态未确认')
    for (const action of ['启动', '停止']) {
      check(await card('offline').getByRole('button', { name: action, exact: true }).count() === 0,
        `${platform}/offline: ${action} is not offered`)
    }
    const busyButtons = card('busy').locator('.actions button')
    for (let index = 0; index < await busyButtons.count(); index += 1) {
      check(await busyButtons.nth(index).isDisabled(), platform + ': in-progress visible action is disabled')
    }
    const overview = `${platform}-initial-states.png`
    await section.locator('.cards, .vm-cards').screenshot({ path: t.output + '/' + overview, scale: 'css' })
    t.screenshots.push(overview)

    // Direct HTTP must enforce the same restrictions as the built page.
    for (const key of ['offline', 'busy']) {
      for (const action of ['start', 'stop']) {
        const response = await page.request.post(endpoint(key) + '/' + action, { headers })
        check(response.status() === 409, `${platform}/${key}: Server rejects ${action}`)
      }
      const value = await counts(key)
      check(value.start === 0 && value.stop === 0, `${platform}/${key}: denied calls never send native commands`)
    }
    for (const [method, suffix, data] of [
      ['delete', '', undefined], ['patch', '', { alias: 'forbidden-busy-alias' }], ['post', '/dispatch', { agent_id: 'fixture-spare' }],
    ]) {
      const response = await page.request[method](endpoint('busy') + suffix, { headers, ...(data ? { data } : {}) })
      check(response.status() === 409, `${platform}/busy: Server rejects ${method}${suffix}`)
    }

    // One failed user attempt, passive health/reconnect facts, then one new
    // ordinary click. No custom recovery button or hidden retry is invoked.
    for (const [key, action, failedState, labelText, successState, successLabel] of [
      ['start_failure', 'start', 'stopped', '已停止', 'running', '运行中'],
      ['stop_failure', 'stop', 'running', '运行中', 'stopped', '已停止'],
    ]) {
      const previous = operation(await get(key)).request_id
      const response = await click(key, action)
      check(response.status() === 200, `${platform}/${key}: explicit ordinary ${action} accepted once`)
      const failed = await waitTerminal(key, 'failed', previous)
      const failedId = operation(failed).request_id
      check(failed.lifecycle.state === failedState && failed.lifecycle.error.includes(action === 'start' ? '启动失败' : '停止失败'),
        `${platform}/${key}: failed result preserves current physical state and concrete reason`)
      check(failure(failed)?.request_id === failedId, `${platform}/${key}: failure belongs to this exact operation`)
      await expectState(key, labelText)
      await card(key).locator('.vm-operation-error').waitFor()
      const passive = await page.request.post(t.base + '/fixture/passive-observation/' + id(platform, key), { headers: fixtureHeaders })
      check(passive.status() === 200, `${platform}/${key}: real service receives a passive current observation`)
      const afterPassive = await get(key)
      const passiveRuntime = afterPassive.runtime_state || afterPassive.runtime || {}
      check(passiveRuntime.instance_observation?.reason === '被动观察：' + failedId,
        `${platform}/${key}: Server accepted the new owner observation rather than merely ignoring it`)
      check(operation(afterPassive).outcome === 'failed' && operation(afterPassive).request_id === failedId
        && afterPassive.lifecycle.error === failed.lifecycle.error && failure(afterPassive)?.request_id === failedId,
      `${platform}/${key}: passive success facts cannot erase the unresolved failed attempt`)
      check((await counts(key))[action] === 1, `${platform}/${key}: passive facts do not retry native action`)
      await screenshot(key, key + '-reason-retained')
      const retryResponse = await click(key, action)
      check(retryResponse.status() === 200, `${platform}/${key}: new ordinary click creates a separate attempt`)
      const succeeded = await waitTerminal(key, 'succeeded', failedId)
      check(succeeded.lifecycle.state === successState && succeeded.lifecycle.error === '' && !failure(succeeded),
        `${platform}/${key}: explicit successful operation clears prior failure`)
      check((await counts(key))[action] === 2, `${platform}/${key}: exactly two user-requested native attempts total`)
      await expectState(key, successLabel)
      await until(async () => card(key).locator('.vm-operation-error').count(), value => value === 0, 'old failure removed from card')
    }

    // A confirmed stopped row must not be permanently trapped by an old flag.
    const staleStart = await click('stale_cleanup', 'start')
    check(staleStart.status() === 200, platform + ': old cleanupFalse no longer blocks ordinary Start')
    await waitTerminal('stale_cleanup', 'succeeded', operation(states.stale_cleanup).request_id)
    await expectState('stale_cleanup', '运行中')
    const staleRunningId = operation(await get('stale_cleanup')).request_id
    check((await click('stale_cleanup', 'stop')).status() === 200, platform + ': ordinary Stop after stale-flag recovery')
    await waitTerminal('stale_cleanup', 'succeeded', staleRunningId)
    await expectState('stale_cleanup', '已停止')

    const unknownStopOld = operation(states.unknown_stopped).request_id
    check((await click('unknown_stopped', 'start')).status() === 200, platform + ': unknown stopped starts after read-only inspection')
    const unknownStarted = await waitTerminal('unknown_stopped', 'succeeded', unknownStopOld)
    check(unknownStarted.lifecycle.state === 'running', platform + ': unknown stopped becomes running through real ACK')
    const checked = await fixtureState()
    const inspectedEvents = checked.events.filter(event => event.vm_id === id(platform, 'unknown_stopped'))
    const inspectEvent = inspectedEvents.find(event => event.event === 'probe-resolved' && event.inspect)
    const inspectSend = inspectedEvents.find(event => event.event === 'agent-send' && event.inspect)
    const startEvent = inspectedEvents.find(event => event.event === 'lifecycle-send' && event.action === 'start')
    const stoppedCounts = checked.scenarios[id(platform, 'unknown_stopped')].calls
    check(inspectEvent?.accepted === true && inspectSend && inspectSend.request_id === inspectEvent.request_id
      && inspectSend.agent_id === inspectEvent.agent_id && startEvent && inspectEvent.seconds < startEvent.seconds
      && stoppedCounts.inspect === 1 && stoppedCounts.start === 1 && stoppedCounts.stop === 0,
    platform + ': one correlated inspection precedes exactly one Start, with no Stop/restart')

    const runningResponse = await click('unknown_running', 'start')
    check([200, 409].includes(runningResponse.status()), platform + ': unknown running observation has an explicit response')
    await until(() => get('unknown_running'), vm => vm.lifecycle.state === 'running', 'unknown running was observed')
    await expectState('unknown_running', '运行中')
    const runningCounts = await counts('unknown_running')
    check(runningCounts.inspect === 1 && runningCounts.start === 0 && runningCounts.stop === 0,
      platform + ': observed running cannot be stopped/restarted or started again by inspection')
    const repeated = await page.request.post(endpoint('unknown_running') + '/start', { headers })
    check(repeated.status() === 409, platform + ': known-running repeat Start is refused')
    check((await counts('unknown_running')).start === 0 && (await counts('unknown_running')).stop === 0,
      platform + ': repeat Start has no native side effect')
    await screenshot('unknown_running', 'unknown-running-observed')

    // Show the offline owner still has the existing switch entry; retain two
    // candidates so Android's legacy single-candidate auto-dispatch cannot fire.
    await card('offline').getByRole('button', { name: '换 Agent（探查）', exact: true }).click()
    await card('offline').getByText('个人验收备用主机', { exact: true }).waitFor()
    await until(async () => card('offline').locator('.candidate button:enabled').count(), value => value === 2,
      'both isolated replacement candidates are available')
    check(await card('offline').locator('.candidate').count() === 2, platform + ': offline owner can probe replacement Agents')
    await card('offline').getByRole('button', { name: '收起', exact: true }).click()
    page.once('dialog', dialog => dialog.accept())
    const deletion = page.waitForResponse(response => response.url() === endpoint('offline') && response.request().method() === 'DELETE')
    await card('offline').getByRole('button', { name: '删除', exact: true }).click()
    check((await deletion).status() === 200, platform + ': offline configuration can be deleted immediately')
    check((await page.request.get(endpoint('offline'), { headers })).status() === 404, platform + ': deleted ledger is absent')

    const finished = await fixtureState()
    check(finished.events.filter(event => event.event === 'lifecycle-send' && event.vm_id === id(platform, 'busy')).length === 0,
      platform + ': in-progress VM never received conflicting lifecycle commands')
    check(finished.blocked.length === 0, platform + ': no external/native fixture call was attempted', finished.blocked)
    check((finished.fake_errors || []).length === 0, platform + ': fake protocol delivery completed without fixture errors', finished.fake_errors)
  }
  const end = await fixtureState()
  check(Object.values(end.pending).every(value => value === 0), 'all real Server probe waiters released')
  check(t.blockedUrls.length === 0, 'browser never requested another origin', t.blockedUrls)
  check(t.runtimeErrors.length === 0, 'no browser runtime exception', t.runtimeErrors)
  return { checks: t.checks, summary: { passed: t.checks.filter(row => row.passed).length,
    failed: t.checks.filter(row => !row.passed).length, screenshots: t.screenshots.length },
  screenshots: t.screenshots, runtimeErrors: t.runtimeErrors, blockedUrls: t.blockedUrls,
  scope: 'Built Vue plus actual FastAPI lifecycle projection/status persistence with controlled fake Agent observations; no native VM/device acceptance.' }
}
