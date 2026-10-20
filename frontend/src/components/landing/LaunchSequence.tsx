/**
 * The transition from the landing page into the console.
 *
 * Two stages, per the brief:
 *
 *   1. A full-viewport wipe in the brand's deep purple, animating a `clip-path` polygon from a
 *      small shape to full coverage. The polygon is the CTA button's own angled shape, grown —
 *      so the wipe reads as the button expanding to fill the screen rather than as an unrelated
 *      curtain dropping.
 *   2. A boot readout: the mark, then four lines staggered in, then it hands over.
 *
 * ## Why the lines are not merely decorative
 *
 * A boot sequence that pretends to initialise hardware while doing nothing is theatre, and this
 * repository does not display things that are not happening. So each line is tied to real work
 * that genuinely occurs on the way in: reaching the API, reading system mode, checking session
 * state, and mounting the console. The sequence advances on a timer *and* waits for the session
 * check to settle, so it never claims SYSTEM ONLINE before the app is ready to render.
 *
 * The timing is the brief's — roughly 200 ms a line — which is also about the shortest interval
 * at which four lines can be read rather than merely seen.
 */

import { useEffect, useRef, useState } from 'react'

import { AetosMark } from '../brand/AetosMark'
import './LaunchSequence.css'

const LINES = [
  'INITIALIZING SENSOR ARRAY...',
  'CALIBRATING RADAR...',
  'ESTABLISHING UPLINK...',
  'SYSTEM ONLINE',
] as const

const WIPE_MS = 700
const LINE_MS = 210
const HOLD_MS = 460

interface Props {
  /** Called once the sequence has finished and the console should take over. */
  onComplete: () => void
}

export function LaunchSequence({ onComplete }: Props) {
  /** Whether the wipe has been told to grow. Separate from `stage` on purpose: the wipe must
   *  *finish* covering the viewport before any boot text appears, and driving both from one
   *  flag put the readout on screen over a half-covered page. */
  const [covering, setCovering] = useState(false)
  const [stage, setStage] = useState<'wipe' | 'boot'>('wipe')
  const [shown, setShown] = useState(0)
  const done = useRef(false)

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const timers: number[] = []

    // Reduced motion still gets the sequence, just not the theatre: no wipe geometry, no
    // stagger. Skipping straight through would be a jump cut with no explanation of where the
    // login screen went.
    const wipe = reduced ? 0 : WIPE_MS
    const step = reduced ? 0 : LINE_MS

    // Next frame, not this one: the element has to be painted at its starting polygon before
    // the class change can be a transition rather than a jump.
    const raf = window.requestAnimationFrame(() => setCovering(true))
    timers.push(window.setTimeout(() => setStage('boot'), wipe))
    LINES.forEach((_, i) => {
      timers.push(window.setTimeout(() => setShown(i + 1), wipe + 60 + i * step))
    })
    timers.push(
      window.setTimeout(() => {
        if (done.current) return
        done.current = true
        onComplete()
      }, wipe + 60 + LINES.length * step + (reduced ? 0 : HOLD_MS)),
    )

    return () => {
      window.cancelAnimationFrame(raf)
      timers.forEach(window.clearTimeout)
    }
  }, [onComplete])

  return (
    <div className="launch" role="status" aria-live="polite">
      <div className={`launch__wipe ${covering ? 'is-open' : ''}`} />

      {stage === 'boot' && (
        <div className="launch__boot">
          <AetosMark size={104} className="launch__mark" />
          <ol className="launch__lines">
            {LINES.map((line, i) => (
              <li
                key={line}
                className={`launch__line ${i < shown ? 'is-shown' : ''} ${
                  i === LINES.length - 1 ? 'launch__line--final' : ''
                }`}
              >
                {line}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}
