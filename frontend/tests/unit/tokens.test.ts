/**
 * The palette's one hard promise: everything that carries content or judgement is readable on
 * every surface it can land on (WCAG AA, 4.5:1). Measured from tokens.css itself, so a tweak
 * that breaks it fails here rather than in a lit operations room.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(__dirname, '../../src/styles/tokens.css'), 'utf8')
const tokens = Object.fromEntries(
  [...css.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\b/g)].map((m) => [m[1], m[2]]),
) as Record<string, string>

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p)
  return (x! + 0.05) / (y! + 0.05)
}

const SURFACES = ['surface-void', 'surface-base', 'surface-panel', 'surface-raised', 'surface-inset']
const CONTENT = [
  'text-primary',
  'text-secondary',
  'text-muted',
  'brand-accent',
  'status-nominal',
  'status-info',
  'status-warning',
  'status-elevated',
  'status-critical',
  'track-default',
]

describe('palette contrast', () => {
  for (const fg of CONTENT) {
    for (const bg of SURFACES) {
      it(`${fg} on ${bg} clears 4.5:1`, () => {
        expect(tokens[fg], fg).toBeDefined()
        expect(tokens[bg], bg).toBeDefined()
        expect(contrast(tokens[fg]!, tokens[bg]!)).toBeGreaterThanOrEqual(4.5)
      })
    }
  }

  it('the light-on-ink text is readable on the dark pills', () => {
    expect(contrast(tokens['text-on-ink']!, tokens['surface-ink']!)).toBeGreaterThanOrEqual(7)
  })
})
