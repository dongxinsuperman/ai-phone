import test from 'node:test'
import assert from 'node:assert/strict'
import { decodedGeneration, directionLabel, matchingGeometry } from '../src/lib/workbenchOrientation.js'

test('same-size left and right frames require the new generation and decoded pixels', () => {
  const video = { readyState: 2, src: 'blob:new', currentSrc: 'blob:new', videoWidth: 600, videoHeight: 300 }
  const observation = { generation: 'new', width: 1200, height: 600, orientation: 'landscape_right' }
  assert.equal(decodedGeneration(video, 'old', observation), false)
  assert.equal(decodedGeneration({ ...video, readyState: 1 }, 'new', observation), false)
  assert.equal(decodedGeneration({ ...video, currentSrc: 'blob:old' }, 'new', observation), false)
  assert.equal(decodedGeneration(video, 'new', observation), true)
  assert.equal(decodedGeneration({ ...video, videoWidth: 300, videoHeight: 600 }, 'new', observation), false)
})

test('render matching tolerates mirror downsampling and does not invent a direction', () => {
  assert.equal(matchingGeometry(640, 320, { width: 1200, height: 600 }), true)
  assert.equal(matchingGeometry(640, 320, { width: 600, height: 1200 }), false)
  assert.equal(directionLabel(null), '待确认')
})
