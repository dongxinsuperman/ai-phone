import test from 'node:test'
import assert from 'node:assert/strict'
import { acceptsVideoInit, decodedGeneration, directionLabel, matchingGeometry, sameVideoStream, videoStreamIdentity } from '../src/lib/workbenchOrientation.js'

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

test('old or unsequenced initialization cannot take over a newer stream from the same agent', () => {
  const identity = (seq, generation, agent = 'agent') => videoStreamIdentity({
    mirror_stream_seq: seq, orientation_generation: generation, mirror_agent_id: agent,
  })
  const current = identity(8, 'right')
  assert.equal(acceptsVideoInit(current, identity(7, 'left')), false)
  assert.equal(acceptsVideoInit(current, identity(undefined, null)), false)
  assert.equal(acceptsVideoInit(current, identity(8, 'left')), false)
  assert.equal(acceptsVideoInit(current, identity(8, 'right')), true)
  assert.equal(acceptsVideoInit(current, identity(9, null)), true)
  assert.equal(sameVideoStream(current, identity(9, null)), false)
  assert.equal(sameVideoStream(current, identity(8, 'right')), true)
  assert.equal(sameVideoStream(null, current), false)
})

test('legacy playback and an agent owner change can still initialize the mirror', () => {
  const legacy = videoStreamIdentity({})
  const current = videoStreamIdentity({ mirror_stream_seq: 8, mirror_agent_id: 'old' })
  const newOwner = videoStreamIdentity({ mirror_agent_id: 'new' })
  assert.equal(acceptsVideoInit(null, legacy), true)
  assert.equal(acceptsVideoInit(legacy, legacy), true)
  assert.equal(acceptsVideoInit(legacy, current), true)
  assert.equal(acceptsVideoInit(current, legacy), false)
  assert.equal(acceptsVideoInit(current, newOwner), true)
  assert.equal(sameVideoStream(newOwner, current), false)
})
