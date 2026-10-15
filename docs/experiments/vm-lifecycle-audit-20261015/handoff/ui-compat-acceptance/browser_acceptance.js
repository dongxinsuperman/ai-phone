async(page)=>{
  const report=page.__uiCompat
  if(!report||new URL(page.url()).origin!==report.base)throw new Error('Dedicated localhost fixture required')
  const out='/Users/dongxin/代码文件/sonic合集/ai-phone/.playwright-mcp/vm-ui-compat-acceptance'
  const check=(ok,label,detail)=>{report.checks.push({passed:Boolean(ok),label,detail});if(!ok)throw new Error(label)}
  const platforms=[['android','Android 虚拟机','vm'],['harmony','鸿蒙虚拟机','harmony-vm'],['ios','iOS 虚拟机','ios-sim']]
  const dialog=()=>page.once('dialog',async d=>d.accept())
  for(const [platform,tab,domain] of platforms){
    await page.getByRole('button',{name:tab,exact:true}).click()
    const section=page.locator('section.page:visible')
    const card=key=>section.locator(`.vm-card:has([title="vm_id: ${platform}-${key}"]), .card:has([title="vm_id: ${platform}-${key}"])`)
    const listUrl=report.base+`/api/internal/${domain}/instances`
    await card('switch-source').getByRole('button',{name:'换 Agent（探查）',exact:true}).click()
    await card('switch-source').getByText('个人候选 目标',{exact:true}).waitFor()
    let readFinished=false
    const readback=page.waitForResponse(r=>r.url()===listUrl&&r.headers()['x-fixture-delayed']==='1').then(()=>{readFinished=true})
    const failed=page.waitForResponse(r=>r.url().endsWith(`/${platform}-switch-source/dispatch`))
    dialog()
    await card('switch-source').getByRole('button',{name:'换到这台',exact:true}).click()
    const response=await failed
    check(response.status()===409,platform+': failed replacement uses non-2xx',{status:response.status()})
    await card('switched-1').getByText('异常',{exact:true}).waitFor()
    check(!readFinished,platform+': error response applies new ID before delayed GET completes')
    check(await card('switch-source').count()===0,platform+': retired old ID is removed immediately')
    check(await card('switched-1').locator('.vm-operation-error').innerText()==='新配置启动下发失败',platform+': new instance error is visible')
    await readback
    await page.waitForTimeout(50)
    check(await section.locator(':scope > .error').innerText()==='新配置启动下发失败',platform+': quiet GET preserves operation error')
    await card('switched-1').screenshot({path:out+'/'+platform+'-switch-error-new-id.png',scale:'css'})
    report.screenshots.push(platform+'-switch-error-new-id.png')

    await card('probe-a').getByRole('button',{name:'探查',exact:true}).click()
    await card('probe-a').getByText('个人候选 A',{exact:true}).waitFor()
    const sendFailure=page.waitForResponse(r=>r.url().endsWith(`/${platform}-probe-a/dispatch`))
    const bindReadback=page.waitForResponse(r=>r.url()===listUrl&&r.headers()['x-fixture-delayed']==='1')
    await card('probe-a').getByRole('button',{name:'下发',exact:true}).click()
    const sendResponse=await sendFailure
    check(sendResponse.status()===(platform==='ios'?502:409),platform+': ordinary send failure preserves platform HTTP status',{status:sendResponse.status()})
    await card('probe-a').getByText('异常',{exact:true}).waitFor()
    check((await card('probe-a').innerText()).includes('未分配'),platform+': failed first bind remains unbound')
    await bindReadback
    await page.waitForTimeout(50)
    check(await section.locator(':scope > .error').innerText()==='启动下发失败，本次操作已结束',platform+': first-bind HTTP error survives quiet GET')
    await card('probe-a').screenshot({path:out+'/'+platform+'-first-bind-send-error.png',scale:'css'})
    report.screenshots.push(platform+'-first-bind-send-error.png')

    await card('stopped').getByRole('button',{name:'换 Agent（探查）',exact:true}).click()
    await card('stopped').getByText('个人候选 目标',{exact:true}).waitFor()
    const conflict=page.waitForResponse(r=>r.url().endsWith(`/${platform}-stopped/dispatch`))
    const conflictReadback=page.waitForResponse(r=>r.url()===listUrl&&r.request().method()==='GET')
    dialog()
    await card('stopped').getByRole('button',{name:'换到这台',exact:true}).click()
    check((await conflict).status()===409,platform+': plain conflict remains non-2xx')
    await conflictReadback
    await page.waitForTimeout(50)
    check(await card('stopped').count()===1&&await card('stopped').locator('.state').innerText()==='已停止',platform+': conflict without instance does not retire the old card')
    check(await section.locator(':scope > .error').innerText()==='设备正在被使用',platform+': ordinary conflict message stays visible')
  }
  const events=await(await page.request.get(report.base+'/fixture/events')).json()
  for(const [platform] of platforms){
    check(events.filter(e=>e.event==='vm_action'&&e.action==='dispatch'&&e.old_vm_id===platform+'-switch-source').length===1,platform+': no replacement dispatch retry')
    check(events.filter(e=>e.event==='vm_action'&&e.action==='dispatch'&&e.vm_id===platform+'-probe-a').length===1,platform+': no failed first-bind retry')
    check(events.filter(e=>e.event==='plain_conflict'&&e.vm_id===platform+'-stopped').length===1,platform+': no plain-conflict retry')
  }
  check(report.runtimeErrors.length===0,'no browser runtime exception',report.runtimeErrors)
  check(report.blockedUrls.length===0,'no non-local HTTP request attempted',report.blockedUrls)
  report.summary={passed:report.checks.filter(x=>x.passed).length,failed:report.checks.filter(x=>!x.passed).length,screenshots:report.screenshots.length}
  report.finishedAt=new Date().toISOString()
  report.note='Incremental dispatch error-catch acceptance only; baseline 102 checks and old build hashes are unchanged.'
  return report
}
