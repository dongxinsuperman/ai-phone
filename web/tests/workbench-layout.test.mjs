import test from 'node:test'
import assert from 'node:assert/strict'
import { DEFAULT_MIRROR_RATIOS, mirrorColumnWidth, readMirrorRatios } from '../src/lib/workbenchLayout.js'

test('landscape default provides more usable width while preserving task controls', () => {
  const total = 1222
  const landscape = mirrorColumnWidth(total, DEFAULT_MIRROR_RATIOS.landscape)
  const portrait = mirrorColumnWidth(total, DEFAULT_MIRROR_RATIOS.portrait)
  assert.ok(landscape > portrait * 1.5)
  assert.ok(total - landscape - 14 >= 320)
})

test('saved wide-screen widths remain bounded after viewport shrink', () => {
  for (const total of [840, 1024, 1440, 2560]) {
    assert.ok(mirrorColumnWidth(total, 0.9) <= total - 14 - 320)
    assert.ok(mirrorColumnWidth(total, 0.1) >= 280)
  }
})

test('portrait and landscape preferences survive independently; invalid storage recovers', () => {
  assert.deepEqual(readMirrorRatios({ getItem: () => '{"portrait":0.45,"landscape":0.72}' }), { portrait: 0.45, landscape: 0.72 })
  assert.deepEqual(readMirrorRatios({ getItem: () => '{"portrait":"bad","landscape":1.5}' }), { portrait: 0.36, landscape: 0.9 })
  assert.deepEqual(readMirrorRatios({ getItem: () => '{' }), DEFAULT_MIRROR_RATIOS)
  assert.deepEqual(readMirrorRatios({ getItem() { throw new Error('storage disabled') } }), DEFAULT_MIRROR_RATIOS)
})
