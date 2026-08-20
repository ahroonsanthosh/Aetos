/**
 * The public landing page.
 *
 * Reskinned from 21st.dev's `halide-topo-hero` (kept verbatim alongside this file as
 * `HalideTopoHero.tsx`, so this is readable as a diff against the original). Every mechanism
 * that carries its quality is preserved and none is simplified:
 *
 *   - real CSS `perspective` + `transform-style: preserve-3d`, not a fake 2D skew
 *   - mouse-reactive tilt of the whole canvas, with per-layer `translateZ` depth
 *   - SVG `feTurbulence` film grain over everything
 *   - a topographic contour plane floating above the stack
 *   - a staged entrance that rotates the canvas up from flat
 *   - HUD monospace typography in a fixed interface grid
 *
 * What changed is the skin, the imagery, and the honesty of the readouts.
 *
 * ## The readouts are real
 *
 * `GET /system/mode` and `GET /system/health` are deliberately ungated (see
 * `backend/api/system.py`), so this page reads actual system state rather than printing
 * decorative numbers. When there is no backend — the page served statically, or the service
 * down — it says LINK OFFLINE and shows dashes. It never invents a figure.
 *
 * That is not scrupulousness for its own sake. This is a product whose entire argument is that
 * it does not display things it has not verified (ADR 0011); a landing page that opened with
 * invented telemetry would undercut the claim on the first screen.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { AetosMark } from '../brand/AetosMark'
import { api } from '../../data/client/api'
import './AetosHero.css'

/** Resting attitude of the canvas. The mouse moves the tilt around this, and it is where the
 *  entrance lands — also the fixed attitude for touch and reduced-motion. */
const REST_X = 55
const REST_Z = -25

interface Props {
  onLaunch: () => void
}

export function AetosHero({ onLaunch }: Props) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const layersRef = useRef<(HTMLDivElement | null)[]>([])
  const readouts = useSystemReadouts()

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    // Coarse pointers cannot hover, so a tilt driven by mouse position would leave the canvas
    // stuck at whatever attitude it was born with. Locked to rest instead.
    const coarse = window.matchMedia('(pointer: coarse)').matches

    if (reduced || coarse) {
      canvas.style.transform = `rotateX(${REST_X}deg) rotateZ(${REST_Z}deg) scale(1)`
      canvas.style.opacity = '1'
      // Still give the layers their depth: the parallax is what makes the stack read as
      // stacked at all. It just stops responding to the pointer.
      layersRef.current.forEach((layer, i) => {
        if (layer) layer.style.transform = `translateZ(${(i + 1) * 26}px)`
      })
      return
    }

    const handleMouseMove = (e: MouseEvent) => {
      const x = (window.innerWidth / 2 - e.pageX) / 25
      const y = (window.innerHeight / 2 - e.pageY) / 25

      canvas.style.transform = `rotateX(${REST_X + y / 2}deg) rotateZ(${REST_Z + x / 2}deg)`

      layersRef.current.forEach((layer, index) => {
        if (!layer) return
        // Deeper than the original's 15px step: three near-monochrome line drawings need more
        // separation than three photographs to read as distinct planes.
        const depth = (index + 1) * 26
        const moveX = x * (index + 1) * 0.2
        const moveY = y * (index + 1) * 0.2
        layer.style.transform = `translateZ(${depth}px) translate(${moveX}px, ${moveY}px)`
      })
    }

    canvas.style.opacity = '0'
    canvas.style.transform = 'rotateX(90deg) rotateZ(0deg) scale(0.8)'

    const timeout = window.setTimeout(() => {
      canvas.style.transition = 'all 2.5s cubic-bezier(0.16, 1, 0.3, 1)'
      canvas.style.opacity = '1'
      canvas.style.transform = `rotateX(${REST_X}deg) rotateZ(${REST_Z}deg) scale(1)`
    }, 300)

    window.addEventListener('mousemove', handleMouseMove)
    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.clearTimeout(timeout)
    }
  }, [])

  return (
    <div className="hero">
      {/* The grain filter. `fractalNoise` over the whole viewport, desaturated — the same
          construction as the source, at lower opacity because this palette is darker and
          grain reads much more strongly on it. */}
      <svg className="hero__filter" aria-hidden="true">
        <filter id="aetos-grain">
          <feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="3" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
      </svg>
      <div className="hero__grain" style={{ filter: 'url(#aetos-grain)' }} />

      <div className="hero__viewport" aria-hidden="true">
        <div className="hero__canvas" ref={canvasRef}>
          <div className="hero__layer hero__layer--terrain" ref={(el) => (layersRef.current[0] = el)} />
          <div className="hero__layer hero__layer--airframe" ref={(el) => (layersRef.current[1] = el)} />
          <div className="hero__layer hero__layer--returns" ref={(el) => (layersRef.current[2] = el)} />
          <div className="hero__contours" />
        </div>
      </div>

      <div className="hero__grid">
        <div className="hero__sys">AETOS SYS // 001</div>
        <Readouts state={readouts} />

        <div className="hero__centre">
          <AetosMark size="clamp(96px, 13vw, 190px)" className="hero__mark" />
          <h1 className="hero__title">AETOS</h1>
        </div>

        <div className="hero__foot">
          <div className="hero__strap">
            <p>[ COUNTER-UAS PLATFORM ]</p>
            {/* Not "neutralize". This system stops at a recommendation to a human — the
                boundary is architectural and enforced by test, and the front page is the last
                place to claim a capability the product deliberately does not have. */}
            <p className="hero__strap-line">DETECT · TRACK · ASSESS</p>
          </div>
          <button type="button" className="hero__cta" onClick={onLaunch}>
            LAUNCH SYSTEM
          </button>
        </div>
      </div>

      <div className="hero__hint" aria-hidden="true" />
    </div>
  )
}

/** What the HUD shows, and whether it is real. */
interface ReadoutState {
  linked: boolean
  mode: string | null
  /** Healthy subsystems over total, from `GET /system/health`.
   *
   *  Labelled SUBSYSTEMS, not SENSORS. This began as a sensor count and was neither — it was
   *  counting health components. `GET /sensors` requires a session, so a genuinely public page
   *  cannot know the sensor count, and printing one anyway is exactly the invented figure this
   *  page refuses to show. */
  subsystems: { ok: number; total: number } | null
  logSeq: number | null
}

function Readouts({ state }: { state: ReadoutState }) {
  const dash = '——'
  return (
    <dl className="hero__readouts">
      <Readout label="LINK" value={state.linked ? 'ESTABLISHED' : 'OFFLINE'} live={state.linked} />
      <Readout label="MODE" value={state.mode ?? dash} live={state.linked} />
      <Readout
        label="SUBSYSTEMS"
        value={state.subsystems === null ? dash : `${state.subsystems.ok} / ${state.subsystems.total}`}
        live={state.linked}
      />
      <Readout
        label="LOG SEQ"
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
      <dd>{value}</dd>
    </div>
  )
}

/**
 * Poll the two ungated system endpoints.
 *
 * Failure is a state, not an error to swallow: `linked: false` renders as OFFLINE with dashes,
 * which is the truth when the page is served without a backend behind it. Nothing here ever
 * falls back to a plausible-looking number.
 */
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
        mode: mode.mode.toUpperCase(),
        subsystems: {
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
