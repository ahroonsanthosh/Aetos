/**
 * Motion in a camera image, found in the browser.
 *
 * Frame differencing against a slowly updated background: a pixel that differs from the
 * background by more than `threshold` is "changed", changed pixels are grouped into connected
 * regions, and each region big enough to matter becomes a box. Deliberately simple and
 * dependency-free. It runs on a small greyscale copy of each frame, a few times a second, so it
 * costs almost nothing and no video ever leaves the device: only the boxes are sent.
 *
 * What it cannot do is the same as any motion detector on a fixed camera. A hovering target
 * is learned into the background within seconds, textured backgrounds (trees, roofs) hide small
 * targets in their own noise, and a camera that moves sees the whole world move. The last is
 * detected and reported as a scene change rather than as a hundred contacts.
 */

export interface MotionBox {
  x: number
  y: number
  width: number
  height: number
  /** Fraction of the box's pixels that changed, 0 to 1. */
  strength: number
}

export interface MotionResult {
  boxes: MotionBox[]
  /** Not reporting yet: the background is still being learned. */
  warming: boolean
  /** So much of the frame changed that the camera itself probably moved. Background reset. */
  sceneChanged: boolean
}

export interface MotionOptions {
  /** Grey-level difference, 0-255, that counts as change. */
  threshold: number
  /** Smallest region reported, in pixels of the analysed frame. */
  minArea: number
  /** How fast the background follows the scene, 0-1 per frame. */
  learningRate: number
  /** Frames to learn the background before reporting anything. */
  warmupFrames: number
  /** Above this fraction of changed pixels, treat it as the camera moving. */
  sceneChangeFraction: number
  maxBoxes: number
}

export const DEFAULT_MOTION_OPTIONS: MotionOptions = {
  threshold: 28,
  minArea: 6,
  learningRate: 0.05,
  warmupFrames: 8,
  sceneChangeFraction: 0.35,
  maxBoxes: 16,
}

/** RGBA pixels to one grey byte per pixel (Rec. 601 luma). */
export function toGrey(rgba: Uint8ClampedArray, out?: Uint8Array): Uint8Array {
  const n = rgba.length / 4
  const grey = out && out.length === n ? out : new Uint8Array(n)
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    grey[i] = (rgba[j]! * 77 + rgba[j + 1]! * 150 + rgba[j + 2]! * 29) >> 8
  }
  return grey
}

export class MotionDetector {
  private background: Float32Array | null = null
  private frames = 0
  private readonly mask: Uint8Array
  private readonly options: MotionOptions

  constructor(
    readonly width: number,
    readonly height: number,
    options: Partial<MotionOptions> = {},
  ) {
    this.options = { ...DEFAULT_MOTION_OPTIONS, ...options }
    this.mask = new Uint8Array(width * height)
  }

  reset(): void {
    this.background = null
    this.frames = 0
  }

  process(grey: Uint8Array): MotionResult {
    const { width, height } = this
    if (grey.length !== width * height) {
      throw new Error(`expected ${width * height} pixels, got ${grey.length}`)
    }
    const opts = this.options

    if (this.background === null) {
      this.background = Float32Array.from(grey)
      this.frames = 1
      return { boxes: [], warming: true, sceneChanged: false }
    }

    const bg = this.background
    const mask = this.mask
    let changed = 0
    for (let i = 0; i < grey.length; i++) {
      const value = grey[i]!
      const base = bg[i]!
      const differs = Math.abs(value - base) > opts.threshold
      mask[i] = differs ? 1 : 0
      if (differs) changed++
      // Changed pixels are learned more slowly, so a moving object is not absorbed into the
      // background in the frames it takes to cross it.
      const rate = differs ? opts.learningRate * 0.25 : opts.learningRate
      bg[i] = base + (value - base) * rate
    }

    this.frames++
    if (this.frames <= opts.warmupFrames) {
      return { boxes: [], warming: true, sceneChanged: false }
    }

    if (changed / grey.length > opts.sceneChangeFraction) {
      this.background = Float32Array.from(grey)
      this.frames = 1
      return { boxes: [], warming: true, sceneChanged: true }
    }

    return { boxes: this.regions(mask), warming: false, sceneChanged: false }
  }

  /** Connected changed regions (4-neighbour), as boxes, largest first. Clears the mask. */
  private regions(mask: Uint8Array): MotionBox[] {
    const { width, height } = this
    const boxes: (MotionBox & { area: number })[] = []
    const stack: number[] = []

    for (let start = 0; start < mask.length; start++) {
      if (mask[start] !== 1) continue
      mask[start] = 2
      stack.push(start)
      let minX = width, minY = height, maxX = -1, maxY = -1, area = 0

      while (stack.length) {
        const p = stack.pop() as number
        const x = p % width
        const y = (p - x) / width
        area++
        if (x < minX) minX = x
        if (x > maxX) maxX = x
        if (y < minY) minY = y
        if (y > maxY) maxY = y
        if (x > 0 && mask[p - 1] === 1) { mask[p - 1] = 2; stack.push(p - 1) }
        if (x < width - 1 && mask[p + 1] === 1) { mask[p + 1] = 2; stack.push(p + 1) }
        if (y > 0 && mask[p - width] === 1) { mask[p - width] = 2; stack.push(p - width) }
        if (y < height - 1 && mask[p + width] === 1) { mask[p + width] = 2; stack.push(p + width) }
      }

      if (area >= this.options.minArea) {
        const w = maxX - minX + 1
        const h = maxY - minY + 1
        boxes.push({ x: minX, y: minY, width: w, height: h, strength: area / (w * h), area })
      }
    }

    mask.fill(0)
    return boxes
      .sort((a, b) => b.area - a.area)
      .slice(0, this.options.maxBoxes)
      .map(({ area: _area, ...box }) => box)
  }
}
