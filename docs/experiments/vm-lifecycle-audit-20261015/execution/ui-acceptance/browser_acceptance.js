async (page) => {
  const report = page.__vmUiAcceptance
  if (!report || new URL(page.url()).origin !== report.base) throw new Error('Local fixture page required')
  const output = '/Users/dongxin/代码文件/sonic合集/ai-phone/.playwright-mcp/vm-ui-acceptance-final'
  const specs = [
    {platform:'android',tab:'Android 虚拟机',domain:'vm'},
    {platform:'harmony',tab:'鸿蒙虚拟机',domain:'harmony-vm'},
    {platform:'ios',tab:'iOS 虚拟机',domain:'ios-sim'},
  ]
  const check=(condition,label,detail)=>{
    report.checks.push({label,passed:Boolean(condition),detail})
    if(!condition) throw new Error(label+': '+JSON.stringify(detail))
  }
  const expectEnabled=async(card,names,label)=>{
    const buttons=await card.locator('.actions > button').evaluateAll(elements=>elements.map(el=>({text:el.textContent.trim(),disabled:el.disabled})))
    const actual=buttons.filter(item=>!item.disabled).map(item=>item.text).sort()
    check(JSON.stringify(actual)===JSON.stringify([...names].sort()),label,buttons)
  }
  const shot=async(locator,filename)=>{
    await locator.screenshot({path:output+'/'+filename,scale:'css'})
    report.screenshots.push(filename)
  }
  const acceptDialog=platform=>page.once('dialog',async dialog=>{
    report.dialogs ||= []
    report.dialogs.push({platform,type:dialog.type(),message:dialog.message()})
    await dialog.accept()
  })
  for(const {platform,tab,domain} of specs){
    await page.getByRole('button',{name:tab,exact:true}).click()
    const section=page.locator('section.page:visible')
    const card=key=>section.locator(`.vm-card:has([title="vm_id: ${platform}-${key}"]), .card:has([title="vm_id: ${platform}-${key}"])`)
    await card('running').waitFor({state:'visible'})
    await section.locator('.panel.list').evaluate(el=>{el.scrollTop=0})
    await page.screenshot({path:output+'/'+platform+'-overview.png',scale:'css'})
    report.screenshots.push(platform+'-overview.png')
    check(await section.count()===1,platform+': only selected platform visible')
    check(!(/人工确认释放|force.release|待清理|重试清理|等待旧实例清理/.test(await section.innerText())),platform+': no cleanup queue or manual force-release workflow')
    check(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),platform+': no horizontal page overflow')
    await expectEnabled(card('running'),['停止','复制配置'],platform+': running actions unchanged')
    await expectEnabled(card('starting'),[],platform+': starting blocks operations')
    await expectEnabled(card('stopping'),[],platform+': stopping blocks operations')
    await expectEnabled(card('error'),['换 Agent（探查）','重试停止','复制配置','删除'],platform+': bound failure blocks restart but permits delete/switch exceptions')
    await expectEnabled(card('bind-failed'),['探查','复制配置','删除'],platform+': failed first bind stays unbound and actionable')
    await expectEnabled(card('offline'),['换 Agent（探查）','重试停止','复制配置','删除'],platform+': offline row can be deleted or replaced')
    await expectEnabled(card('stopped'),['换 Agent（探查）','启动','复制配置','删除'],platform+': stopped workflow unchanged')
    check((await card('bind-failed').innerText()).includes('首次启动失败，配置尚未绑定 Agent。'),platform+': first-bind failure is visible')

    acceptDialog(platform)
    await card('offline').getByRole('button',{name:'删除',exact:true}).click()
    await card('offline').waitFor({state:'detached'})
    check(await card('offline').count()===0,platform+': offline delete removes ledger row immediately')

    const aUrl=report.base+`/api/internal/${domain}/instances/${platform}-probe-a/dispatch-candidates`
    const bUrl=report.base+`/api/internal/${domain}/instances/${platform}-probe-b/dispatch-candidates`
    const aResponse=page.waitForResponse(r=>r.url()===aUrl)
    const bResponse=page.waitForResponse(r=>r.url()===bUrl)
    await card('probe-a').getByRole('button',{name:'探查',exact:true}).click()
    await card('probe-b').getByRole('button',{name:'探查',exact:true}).click()
    await aResponse
    check(await card('probe-b').getByRole('button',{name:'探查中…',exact:true}).first().isDisabled(),platform+': earlier probe cannot clear newer row busy state')
    check(!(await section.innerText()).includes('个人候选 A'),platform+': stale probe not rendered in selected row')
    await bResponse
    await card('probe-b').getByText('个人候选 B',{exact:true}).waitFor()
    check(!(await card('probe-b').innerText()).includes('个人候选 A'),platform+': candidates remain attached to correct VM')
    await shot(card('probe-b'),platform+'-probe-current.png')

    await card('probe-b').getByRole('button',{name:'下发',exact:true}).click()
    await card('probe-b').getByText('启动中',{exact:true}).waitFor()
    check((await card('probe-b').innerText()).includes('绑定中：personal-candidate-B'),platform+': sent start is displayed only as binding progress')
    await expectEnabled(card('probe-b'),[],platform+': first bind in progress blocks conflicting actions')
    await shot(card('probe-b'),platform+'-first-bind-progress.png')
    await page.request.post(report.base+'/fixture/complete-bind',{data:{domain,vm_id:platform+'-probe-b',state:'error'}})
    await page.getByRole('button',{name:'刷新',exact:true}).click()
    await card('probe-b').getByText('异常',{exact:true}).waitFor()
    check((await card('probe-b').innerText()).includes('未分配')&&!(await card('probe-b').innerText()).includes('绑定中'),platform+': failed first bind clears displayed binding')
    await expectEnabled(card('probe-b'),['探查','复制配置','删除'],platform+': failed first bind can probe again or delete')
    check((await card('probe-b').innerText()).includes('首次启动失败'),platform+': failed bind reason stays visible')
    await shot(card('probe-b'),platform+'-first-bind-failed.png')

    await card('switch-source').getByRole('button',{name:'换 Agent（探查）',exact:true}).click()
    await card('switch-source').getByText('个人候选 目标',{exact:true}).waitFor()
    acceptDialog(platform)
    await card('switch-source').getByRole('button',{name:'换到这台',exact:true}).click()
    await card('switch-source').waitFor({state:'detached'})
    await card('switched-1').getByText('启动中',{exact:true}).waitFor()
    check(await card('switch-source').count()===0,platform+': switch removes old ID even when old Agent offline')
    check((await card('switched-1').innerText()).includes('绑定中：personal-candidate-目标'),platform+': switch creates a new ID with one new start in progress')
    await expectEnabled(card('switched-1'),[],platform+': replacement progress is not reported as running success')
    await shot(card('switched-1'),platform+'-switch-new-id.png')

    await card('probe-a').getByRole('button',{name:'探查',exact:true}).click()
    await card('probe-a').getByText('个人候选 A',{exact:true}).waitFor()
    await card('probe-a').getByRole('button',{name:'下发',exact:true}).click()
    await card('probe-a').getByText('启动中',{exact:true}).waitFor()
    await page.request.post(report.base+'/fixture/complete-bind',{data:{domain,vm_id:platform+'-probe-a',state:'running'}})
    await page.getByRole('button',{name:'刷新',exact:true}).click()
    await card('probe-a').getByText('运行中',{exact:true}).waitFor()
    check((await card('probe-a').innerText()).includes('personal-candidate-A')&&!(await card('probe-a').innerText()).includes('绑定中'),platform+': only running ACK displays a confirmed binding')
    await expectEnabled(card('probe-a'),['停止','复制配置'],platform+': successful first bind enters normal running actions')
    await shot(card('probe-a'),platform+'-first-bind-confirmed.png')

    await card('error').getByRole('button',{name:'重试停止',exact:true}).click()
    await card('error').getByText('已停止',{exact:true}).waitFor()
    await expectEnabled(card('error'),['换 Agent（探查）','启动','复制配置','删除'],platform+': confirmed stop permits restart')
    await page.request.post(report.base+'/fixture/arm-list-delay',{data:{platform,seconds:2.5}})
    const listUrl=report.base+`/api/internal/${domain}/instances`
    const listRequest=page.waitForRequest(r=>r.url()===listUrl&&r.method()==='GET')
    const delayedResponse=page.waitForResponse(r=>r.url()===listUrl&&r.headers()['x-fixture-delayed']==='1')
    await page.getByRole('button',{name:'刷新',exact:true}).click()
    await listRequest
    await card('error').getByRole('button',{name:'启动',exact:true}).click()
    await card('error').getByText('启动中',{exact:true}).waitFor()
    await delayedResponse
    await page.waitForTimeout(100)
    check(await card('error').locator('.state').innerText()==='启动中',platform+': old list cannot overwrite a newer start result')
    await expectEnabled(card('error'),[],platform+': late list cannot reopen conflicting actions')
    await shot(card('error'),platform+'-late-list-protected.png')
  }
  const events=await(await page.request.get(report.base+'/fixture/events')).json()
  for(const {platform} of specs){
    const aBegin=events.find(e=>e.event==='probe_begin'&&e.vm_id===platform+'-probe-a')
    const aEnd=events.find(e=>e.event==='probe_end'&&e.vm_id===platform+'-probe-a')
    const bBegin=events.find(e=>e.event==='probe_begin'&&e.vm_id===platform+'-probe-b')
    const bEnd=events.find(e=>e.event==='probe_end'&&e.vm_id===platform+'-probe-b')
    check(aBegin.seconds<bBegin.seconds&&bBegin.seconds<aEnd.seconds&&aEnd.seconds<bEnd.seconds,platform+': HTTP evidence confirms overlapping probes',{aBegin:aBegin.seconds,bBegin:bBegin.seconds,aEnd:aEnd.seconds,bEnd:bEnd.seconds})
    const deletes=events.filter(e=>e.event==='delete_ledger'&&e.vm_id===platform+'-offline')
    check(deletes.length===1,platform+': offline deletion runs once without automatic retry',deletes)
    const switches=events.filter(e=>e.event==='vm_action'&&e.switched&&e.old_vm_id===platform+'-switch-source')
    check(switches.length===1&&switches[0].vm_id!==switches[0].old_vm_id,platform+': switch retires old ID and starts new ID once',switches)
    const binds=events.filter(e=>e.event==='vm_action'&&e.action==='dispatch'&&e.vm_id===platform+'-probe-b')
    check(binds.length===1,platform+': failed first bind does not trigger another background start',binds)
    const old=events.find(e=>e.event==='list_snapshot'&&e.platform===platform&&e.delay===2.5)
    check(old.states[platform+'-error']==='stopped',platform+': delayed HTTP snapshot was genuinely stale',old)
  }
  check(report.consoleErrors.length===0,'no browser console or runtime errors',report.consoleErrors)
  check(report.blockedUrls.length===0,'no non-local request attempted',report.blockedUrls)
  check(report.requestUrls.every(url=>new URL(url).origin===report.base),'all page HTTP requests use dedicated localhost fixture')
  report.finishedAt=new Date().toISOString()
  report.contract='immediate-ledger-delete-and-replacement; first binding confirmed only by running ACK'
  report.browser='Playwright Chromium, one fresh localhost page, 1440x1150'
  report.summary={passed:report.checks.filter(item=>item.passed).length,failed:report.checks.filter(item=>!item.passed).length,screenshots:report.screenshots.length}
  return report
}
