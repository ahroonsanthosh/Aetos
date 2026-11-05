/**
 * The public front page.
 *
 * Two moments, after the calm, editorial feel of unseen.co:
 *
 * 1. **The intro.** A blush screen, the letters of the name appearing one at a time, a line on
 *    what Aetos is, and an Enter pill. "Enter without motion" goes in with every animation
 *    stilled, for anyone who would rather not have them. Shown once per tab.
 * 2. **The world.** A soft pastel sky over a tilted radar plane: rings, a slow sweep, a few
 *    contacts and a protected area, with light orbs drifting in front. It tilts with the
 *    pointer. Over it, a headline mixing an italic serif with a clean sans, and one pill: into
 *    the console.
 *
 * The readouts along the bottom are real: link state, run mode, subsystem count and log head,
 * read from the two endpoints that answer without a session. They say "——" when they do not
 * know rather than inventing a healthy-looking number.
 *
 * Nothing here says "neutralise". This system stops at a recommendation to a human, and the
 * front page is the last place to claim a capability the product deliberately does not have.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { AetosMark } from '../brand/AetosMark'
import { api } from '../../data/client/api'
import type { SystemHealthView } from '../../data/models/api'
import './AetosHero.css'

const INTRO_KEY = 'aetos.intro-seen'
const LETTERS = ['A', 'E', 'T', 'O', 'S'] as const
const LETTER_MS = 260

interface Props {
  onLaunch: () => void
}

export function AetosHero({ onLaunch }: Props) {
  const [intro, setIntro] = useState(() => readIntroSeen() !== 'yes')
  const [still, setStill] = useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  )
  const readouts = useSystemReadouts()

  const enter = useCallback((withMotion: boolean) => {
    if (!withMotion) setStill(true)
    writeIntroSeen()
    setIntro(false)
  }, [])

  return (
    <div className={`hero ${still ? 'hero--still' : ''} ${intro ? 'hero--behind-intro' : ''}`}>
      <World still={still} />

      <header className="hero__nav">
        <span className="hero__wordmark">
          <AetosMark size={30} className="hero__mark" />
          aetos <span className="serif hero__wordmark-aside">counter-uas</span>
        </span>
        <nav className="hero__links" aria-label="Front page">
          <button type="button" className="hero__link" onClick={onLaunch}>
            Console
          </button>
          <button type="button" className="hero__round" onClick={onLaunch} aria-label="Sign in">
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </button>
        </nav>
      </header>

      <main className="hero__centre">
        <p className="label hero__eyebrow">A counter-UAS command platform · software only</p>
        <h1 className="hero__title" aria-label="Guarding the open sky">
          <BlurLine className="serif hero__title-serif" text="Guarding the" delay={intro ? 9999 : 200} />
          <BlurLine className="hero__title-sans" text="open sky" delay={intro ? 9999 : 520} />
        </h1>
        <button type="button" className="pill hero__cta" onClick={onLaunch}>
          Enter the console <span className="pill__arrow" aria-hidden="true">↘</span>
        </button>
      </main>

      <footer className="hero__foot">
        <Readouts state={readouts} />
        <span className="label hero__strap">Detect · Track · Assess</span>
        <span className="hero__copy">©2026</span>
      </footer>

      {intro && <Intro onEnter={enter} />}
    </div>
  )
}

/** The blush intro: the name letter by letter, then the line and the Enter pill. */
function Intro({ onEnter }: { onEnter: (withMotion: boolean) => void }) {
  const [shown, setShown] = useState(0)
  const [leaving, setLeaving] = useState<null | boolean>(null)

  useEffect(() => {
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      setShown(LETTERS.length + 1)
      return
    }
    const timers = Array.from({ length: LETTERS.length + 1 }, (_, i) =>
      window.setTimeout(() => setShown(i + 1), 250 + i * LETTER_MS),
    )
    return () => timers.forEach(window.clearTimeout)
  }, [])

  const go = (withMotion: boolean) => {
    if (leaving !== null) return
    setLeaving(withMotion)
    window.setTimeout(() => onEnter(withMotion), withMotion ? 900 : 0)
  }

  const ready = shown > LETTERS.length
  const letter = shown >= 1 && shown <= LETTERS.length ? LETTERS[shown - 1] : null

  return (
    <div className={`intro ${leaving !== null ? 'is-leaving' : ''}`} role="dialog" aria-label="Aetos">
      <div className="intro__glyph" aria-hidden="true">
        {ready ? (
          <AetosMark size={96} className="intro__mark" />
        ) : (
          letter && (
            <span key={letter} className="intro__letter">
              {letter}
            </span>
          )
        )}
      </div>
      <p className="intro__name">AETOS®</p>
      <p className="intro__line">
        Command software for counter-drone teams. It fuses every sensor into one picture, scores
        what it sees, and tells a human what it recommends.
      </p>
      <button
        type="button"
        className={`pill intro__enter ${ready ? 'is-ready' : ''}`}
        onClick={() => go(true)}
        disabled={!ready}
      >
        Enter <span className="pill__arrow" aria-hidden="true">↘</span>
      </button>
      <button type="button" className="intro__quiet" onClick={() => go(false)}>
        Enter without motion
      </button>
    </div>
  )
}

/** One headline line whose letters unblur and rise into place in sequence. */
function BlurLine({ text, className, delay }: { text: string; className: string; delay: number }) {
  return (
    <span className={`hero__line ${className}`} aria-hidden="true">
      {[...text].map((ch, i) => (
        <span key={i} className="hero__char" style={{ animationDelay: `${delay + i * 38}ms` }}>
          {ch === ' ' ? ' ' : ch}
        </span>
      ))}
    </span>
  )
}

/** The pastel world: sky, a tilted radar plane that follows the pointer, drifting orbs. */
function World({ still }: { still: boolean }) {
  const planeRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const plane = planeRef.current
    if (!plane || still) return
    if (window.matchMedia?.('(pointer: coarse)').matches) return

    let frame = 0
    let tx = 0
    let ty = 0
    let x = 0
    let y = 0
    const onMove = (e: PointerEvent) => {
      tx = (e.clientX / window.innerWidth - 0.5) * 2
      ty = (e.clientY / window.innerHeight - 0.5) * 2
    }
    // Eased toward the pointer every frame, so the tilt glides rather than jumps.
    const tick = () => {
      x += (tx - x) * 0.05
      y += (ty - y) * 0.05
      plane.style.setProperty('--tilt-x', `${(62 + y * 4).toFixed(2)}deg`)
      plane.style.setProperty('--tilt-z', `${(-18 + x * 6).toFixed(2)}deg`)
      frame = window.requestAnimationFrame(tick)
    }
    window.addEventListener('pointermove', onMove)
    frame = window.requestAnimationFrame(tick)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.cancelAnimationFrame(frame)
    }
  }, [still])

  return (
    <div className="world" aria-hidden="true">
      <div className="world__sky" />
      <div className="world__stage">
        <div className="world__plane" ref={planeRef}>
          <svg className="world__rings" viewBox="-500 -500 1000 1000">
            <defs>
              <radialGradient id="world-face" cx="50%" cy="50%" r="50%">
                <stop offset="0%" style={{ stopColor: 'var(--scope-face-inner)', stopOpacity: 0.95 }} />
                <stop offset="70%" style={{ stopColor: 'var(--scope-face-mid)', stopOpacity: 0.6 }} />
                <stop offset="100%" style={{ stopColor: 'var(--scope-face-outer)', stopOpacity: 0 }} />
              </radialGradient>
              <radialGradient id="world-zone" cx="50%" cy="50%" r="50%">
                <stop offset="0%" style={{ stopColor: 'var(--zone-protected)', stopOpacity: 0.18 }} />
                <stop offset="100%" style={{ stopColor: 'var(--zone-protected)', stopOpacity: 0 }} />
              </radialGradient>
            </defs>
            <circle r="500" fill="url(#world-face)" />
            {[90, 180, 270, 360, 450].map((r) => (
              <circle key={r} r={r} className="world__ring" />
            ))}
            {[0, 30, 60, 90, 120, 150].map((a) => (
              <line
                key={a}
                className="world__spoke"
                x1={-480 * Math.cos((a * Math.PI) / 180)}
                y1={-480 * Math.sin((a * Math.PI) / 180)}
                x2={480 * Math.cos((a * Math.PI) / 180)}
                y2={480 * Math.sin((a * Math.PI) / 180)}
              />
            ))}
            <circle r="120" fill="url(#world-zone)" />
            <circle r="72" className="world__zone" />
            <g className="world__contacts">
              <circle cx="210" cy="-160" r="7" className="world__contact" />
              <circle cx="-260" cy="90" r="6" className="world__contact world__contact--2" />
              <circle cx="80" cy="300" r="5" className="world__contact world__contact--3" />
              <path d="M 330 -260 Q 270 -205 210 -160" className="world__trail" />
            </g>
          </svg>
          <div className="world__sweep" />
        </div>
      </div>
      <div className="world__orb world__orb--a" />
      <div className="world__orb world__orb--b" />
      <div className="world__orb world__orb--c" />
      <div className="world__haze" />
    </div>
  )
}

interface ReadoutState {
  linked: boolean
  mode: string | null
  subsystems: { ok: number; total: number } | null
  logSeq: number | null
}

function Readouts({ state }: { state: ReadoutState }) {
  const dash = '——'
  return (
    <dl className="hero__readouts">
      <Readout label="Link" value={state.linked ? 'established' : 'offline'} live={state.linked} />
      <Readout label="Mode" value={state.mode ?? dash} live={state.linked} />
      <Readout
        label="Subsystems"
        value={state.subsystems === null ? dash : `${state.subsystems.ok} / ${state.subsystems.total}`}
        live={state.linked}
      />
      <Readout
        label="Log"
        value={state.logSeq === null ? dash : state.logSeq.toLocaleString('en-GB')}
        live={state.linked}
      />
    </dl>
  )
}

function Readout({ label, value, live }: { label: string; value: string; live: boolean }) {
  return (
    <div className={`hero__readout ${live ? 'is-live' : 'is-dark'}`}>
      <dt>{label}</dt>
      <dd className="numeric">{value}</dd>
    </div>
  )
}

/** The unauthenticated health answer is liveness only; its one row is not a subsystem count. */
function isLivenessOnly(health: SystemHealthView): boolean {
  return health.components.length === 1 && health.components[0]?.component === 'api'
}

function useSystemReadouts(): ReadoutState {
  const [state, setState] = useState<ReadoutState>({
    linked: false,
    mode: null,
    subsystems: null,
    logSeq: null,
  })

  const poll = useCallback(async (signal: AbortSignal) => {
    try {
      const [mode, health] = await Promise.all([api.mode(signal), api.health(signal)])
      setState({
        linked: true,
        mode: mode.mode,
        subsystems: isLivenessOnly(health)
          ? null
          : {
              ok: health.components.filter((c) => c.state === 'ok').length,
              total: health.components.length,
            },
        logSeq: health.latest_event_seq,
      })
    } catch {
      if (!signal.aborted) {
        setState({ linked: false, mode: null, subsystems: null, logSeq: null })
      }
    }
  }, [])

  useEffect(() => {
    const abort = new AbortController()
    void poll(abort.signal)
    const timer = window.setInterval(() => void poll(abort.signal), 5000)
    return () => {
      abort.abort()
      window.clearInterval(timer)
    }
  }, [poll])

  return state
}

/** Session storage throws in some locked-down browsers; then the intro simply shows again. */
function readIntroSeen(): string | null {
  try {
    return window.sessionStorage.getItem(INTRO_KEY)
  } catch {
    return null
  }
}

function writeIntroSeen(): void {
  try {
    window.sessionStorage.setItem(INTRO_KEY, 'yes')
  } catch {
    // Not remembered; the intro shows again next time. Harmless.
  }
}
