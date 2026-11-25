import assert from 'node:assert/strict'
import test from 'node:test'

import { buildAiConnectGuide } from '../src/lib/aiConnectGuide.js'

test('one reusable guide discovers and claims an opted-in device without an instance credential', () => {
  const origin = 'https://phones.example'
  const { query, text } = buildAiConnectGuide(origin, false)
  const commands = text.split('\n').filter(line => line.startsWith('curl '))
  assert.deepEqual(commands.slice(0, 2), [
    "curl -fsS --max-time 10 'https://phones.example/api/ai-connect/devices'",
    `curl -sS -X POST 'https://phones.example/api/ai-connect/sessions' -H 'Content-Type: application/json' -d '{"serial":"<设备列表中的 serial>"}'`,
  ])
  assert.equal(query, commands[0])
  assert.match(text, /release.*DELETE/)
  assert.equal(buildAiConnectGuide(origin, false).text, text)
})

test('a paste with no task guides discovery, a brief screenshot lease and release', () => {
  const origin = 'https://phones.example'
  const { text } = buildAiConnectGuide(origin, false)
  const entry = text.slice(0, text.indexOf('领用设备\n'))
  assert.match(entry, /只是粘贴这份说明.*立即.*查询/)
  assert.ok(entry.split('\n').includes(`实时设备清单  ${origin}/api/ai-connect/devices`))
  assert.match(entry, /HTTP 200 和设备列表后.*已连接到 AI Phone/)
  assert.match(entry, /空列表.*设备全被占用.*查询失败/)
  assert.match(entry, /只粘贴说明时，先做一次看图体验/)
  assert.match(entry, /available.*临时领用.*GET api\.screenshot.*api\.observe.*native/)
  assert.match(entry, /release 发 DELETE.*204.*取图失败也要归还/)
  assert.match(entry, /直接展示刚取回的图片/)
  assert.match(entry, /你想用哪台设备，或者需要哪种系统的模拟器/)
  assert.match(entry, /唯一符合条件的设备时，直接领用/)
  assert.match(entry, /Android、iOS 和鸿蒙模拟器/)
  assert.ok(entry.includes(`AIPHONE_BASE_URL='${origin}' aiphone vm ios catalog --limit 5`))
  assert.doesNotMatch(entry.split('只粘贴说明时，先做一次看图体验')[0], /-X POST|Bearer/)
})

test('one claim delivers native access and any available auxiliary endpoints', () => {
  const { text, operations } = buildAiConnectGuide('https://phones.example', false)
  const section = operations.join('\n')
  assert.ok(text.includes(section))
  assert.match(text, /native 和 api 同时指向所领设备/)
  assert.match(text, /api 为空时直接使用 native/)
  assert.match(text, /Android USB 真机和本机 Android 模拟器使用原生 ADB/)
  assert.match(text, /adb-connect\.pyz.*lease\.json.*adb_serial/)
  assert.match(text, /回到操作清单选号前，先 observe/)
  assert.match(section, /Authorization: Bearer <本次领取的 token>/)
  assert.match(section, /每帧同时返回.*截图.*session_id、native、api/)
  assert.match(section, /screenshot\.data.*Base64.*PNG/)
  assert.match(section, /图上的框号与 targets\[\]\.id 一致/)
  assert.match(section, /没有目标时清单为空，截图仍随帧返回/)
  assert.match(section, /name_source.*system.*screen.*null/)
  assert.match(section, /action_status.*observation.*格式与 observe 相同.*changes/)
  assert.match(section, /取图失败.*只重新观察/)
  assert.doesNotMatch(text, /include_screenshot|无图模式/)
})

test('both HTTP examples save the complete response and use that frame for the next action', () => {
  const { text } = buildAiConnectGuide('https://phones.example', false)
  const examples = text.split('\n').filter(line => line.startsWith('curl ') && line.includes('$TOKEN'))
  assert.equal(examples.length, 2)
  assert.match(examples[0], /"\$OBSERVE_URL".*-o frame\.json/)
  assert.match(examples[1], /-X POST "\$ACT_URL".*-o result\.json/)
  for (const example of examples) assert.ok(example.includes('Authorization: Bearer $TOKEN'))
  const json = examples[1].match(/ -d '([^']+)'$/)[1]
  assert.deepEqual(JSON.parse(json), { action: 'tap', frame_id: '<这帧的 frame_id>', target: '<选中的 id>' })
  assert.match(text, /structure\.children.*target.*targets\[\]\.id/)
  assert.match(text, /tap 和 scroll 使用最新 frame_id/)
})

test('action examples encode the selected target, numeric pixels and focused input value', () => {
  const { operations } = buildAiConnectGuide('https://phones.example', false)
  const examples = operations.filter(line => line.startsWith('{')).map(line => JSON.parse(
    line.replaceAll('<最新 frame_id>', 'frame-7')
      .replace('<所选目标的 id>', 'target-4')
      .replace('<要设置的完整文字>', 'abc')
  ))
  assert.deepEqual(examples, [
    { frame_id: 'frame-7', action: 'tap', target: 'target-4' },
    { frame_id: 'frame-7', action: 'tap', x: 108, y: 240 },
    { action: 'input', text: 'abc' },
  ])
})

test('native use leads the guide and discovery and raw screenshot fallback are runnable', () => {
  const { text } = buildAiConnectGuide('https://phones.example', false)
  assert.ok(text.indexOf('优先使用原生直连') < text.indexOf('主动式可操作暴露'))
  assert.match(text, /cmd package query-activities --brief --components.*android.intent.category.LAUNCHER/)
  assert.match(text, /webkit-connect\.pyz .* lease\.json --web-inspector/)
  assert.match(text, /hdc fport、rport、reconnect.*端口转发跟随本次连接关闭/)
  assert.match(text, /sideload、update、flash.*单个文件上限 32 GiB/)
  assert.match(text, /GET api.screenshot.*X-AI-Phone-Frame-Id/)
  assert.match(text, /POST api.tap.*"frame_id".*"x":100,"y":200/)
})
