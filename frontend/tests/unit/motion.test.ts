import { describe, expect, it } from 'vitest'

import { MotionDetector, toGrey } from '../../src/components/camera/motion'

const W = 40
const H = 30

function frame(fill = 100, square?: { x: number; y: number; size: number; value: number }) {
  const grey = new Uint8Array(W * H).fill(fill)
  if (square) {
    for (let y = square.y; y < square.y + square.size; y++)
      for (let x = square.x; x < square.x + square.size; x++) grey[y * W + x] = square.value
  }
  return grey
}

function warm(detector: MotionDetector) {
  for (let i = 0; i < 10; i++) detector.process(frame())
}

describe('MotionDetector', () => {
  it('reports nothing while it learns the background', () => {
    const detector = new MotionDetector(W, H)
    const first = detector.process(frame(100, { x: 5, y: 5, size: 4, value: 250 }))
    expect(first.warming).toBe(true)
    expect(first.boxes).toEqual([])
  })

  it('finds a bright object that appears, with the right box', () => {
    const detector = new MotionDetector(W, H)
    warm(detector)
    const result = detector.process(frame(100, { x: 10, y: 8, size: 4, value: 250 }))
    expect(result.warming).toBe(false)
    expect(result.boxes).toHaveLength(1)
    expect(result.boxes[0]).toMatchObject({ x: 10, y: 8, width: 4, height: 4, strength: 1 })
  })

  it('ignores changes smaller than the minimum area', () => {
    const detector = new MotionDetector(W, H, { minArea: 6 })
    warm(detector)
    expect(detector.process(frame(100, { x: 3, y: 3, size: 2, value: 250 })).boxes).toEqual([])
  })

  it('separates two objects into two boxes, largest first', () => {
    const detector = new MotionDetector(W, H)
    warm(detector)
    const grey = frame(100, { x: 2, y: 2, size: 3, value: 250 })
    for (let y = 20; y < 26; y++) for (let x = 30; x < 36; x++) grey[y * W + x] = 0
    const { boxes } = detector.process(grey)
    expect(boxes).toHaveLength(2)
    expect(boxes[0]).toMatchObject({ x: 30, y: 20, width: 6, height: 6 })
    expect(boxes[1]).toMatchObject({ x: 2, y: 2, width: 3, height: 3 })
  })

  it('treats the whole frame changing as the camera moving, not as contacts', () => {
    const detector = new MotionDetector(W, H)
    warm(detector)
    const result = detector.process(frame(220))
    expect(result.sceneChanged).toBe(true)
    expect(result.boxes).toEqual([])
  })

  it('rejects a frame of the wrong size', () => {
    expect(() => new MotionDetector(W, H).process(new Uint8Array(3))).toThrow()
  })
})

describe('toGrey', () => {
  it('weights green most, as luma does', () => {
    const grey = toGrey(new Uint8ClampedArray([255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255]))
    expect(grey[1]!).toBeGreaterThan(grey[0]!)
    expect(grey[0]!).toBeGreaterThan(grey[2]!)
  })
})
