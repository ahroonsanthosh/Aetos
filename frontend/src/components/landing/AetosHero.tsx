/**
 * The public front page.
 *
 * Two moments, after the calm, editorial feel of unseen.co:
 *
 * 1. **The intro.** A dark screen, the letters of the name appearing one at a time, a line on
 *    what Aetos is, and an Enter pill. "Enter without motion" goes in with every animation
 *    stilled, for anyone who would rather not have them. Shown once per tab.
 * 2. **The world.** A WebGL night airspace (`NightWorld`): dotted terrain under fog, range
 *    rings, a sweep, a protected dome and moving contacts, leaning toward the pointer. Over it,
 *    a headline mixing a clean sans with an italic serif, and one pill: into the console.
 *
 * The readouts along the bottom are real: link state, run mode, subsystem count and log head,
 * read from the two endpoints that answer without a session. They say "——" when they do not
 * know rather than inventing a healthy-looking number.
 *
 * Nothing here says "neutralise". This system stops at a recommendation to a human, and the
 * front page is the last place to claim a capability the product deliberately does not have.
 */

import { Suspense, lazy, useCallback, useEffect, useState } from 'react'

import { AetosMark } from '../brand/AetosMark'
import { api } from '../../data/client/api'
import type { SystemHealthView } from '../../data/models/api'
import './AetosHero.css'

// three.js only loads for someone who opens the front page, never for the console.
const NightWorld = lazy(() => import('./NightWorld'))

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
      <div className="hero__sky" aria-hidden="true" />
      <Suspense fallback={null}>
        <NightWorld still={still} />
      </Suspense>
      <div className="hero__vignette" aria-hidden="true" />

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
        <p className="label hero__eyebrow">
          <span className="hero__eyebrow-index numeric">[01]</span> Counter-UAS command · software only
        </p>
        <h1 className="hero__title" aria-label="Know what is overhead.">
          <BlurLine className="hero__title-sans" text="Know what is" delay={intro ? 9999 : 200} />
          <BlurLine className="serif hero__title-serif" text="overhead." delay={intro ? 9999 : 560} />
        </h1>
        <p className="hero__sub">
          Every sensor fused into one picture. Every contact tracked, scored and explained. A
          human makes the call.
        </p>
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
