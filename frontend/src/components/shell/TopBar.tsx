/**
 * Top bar: identity, live system status, mode, time and operator.
 *
 * The mode indicator is the important element here. It reads `GET /system/mode`, where
 * `simulated` is computed from whether a real adapter is actually feeding the pipeline —
 * not from a configured string, and never from a frontend constant. Synthetic data being
 * presentable as real is the failure this element exists to prevent, so it is deliberately
 * the most visually assertive thing in the bar.
 *
 * Status chips report what the backend says. A chip whose data has not arrived shows as
 * unknown rather than optimistically green (ADR 0011).
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { AetosMark } from '../brand/AetosMark'
import { api } from '../../data/client/api'
import { useAuth } from '../../data/client/auth'
import type { ModeView, SystemHealthView } from '../../data/models/api'
import type { StreamStatus } from '../../data/client/stream'
import './TopBar.css'

interface Props {
  streamStatus: StreamStatus
  trackCount: number
}

const HEALTH_POLL_MS = 15_000

export function TopBar({ streamStatus, trackCount }: Props) {
  const { session, signOut } = useAuth()
  const [mode, setMode] = useState<ModeView | null>(null)
  const [health, setHealth] = useState<SystemHealthView | null>(null)
  const [utc, setUtc] = useState(() => new Date())

  useEffect(() => {
    const timer = window.setInterval(() => setUtc(new Date()), 1_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    const abort = new AbortController()

    async function poll() {
      try {
        const [nextMode, nextHealth] = await Promise.all([
          api.mode(abort.signal),
          api.health(abort.signal),
        ])
        setMode(nextMode)
        setHealth(nextHealth)
      } catch {
        // Leave the previous values in place and let the stream-status chip carry the
        // connectivity story. Blanking the bar on one failed poll would flicker the mode
        // indicator, and an indicator that flickers is one an operator stops reading.
      }
    }

    void poll()
    const timer = window.setInterval(() => void poll(), HEALTH_POLL_MS)
    return () => {
      abort.abort()
      window.clearInterval(timer)
    }
  }, [])

  const unavailable =
    health?.components.filter((c) => c.state === 'unavailable' || c.state === 'degraded') ?? []

  return (
    <header className="topbar">
      {/* The mark links to the front page rather than to the console root. An operator who
          clicks a logo expects to go somewhere; sending them to the page they are already on
          is the more surprising of the two options, and `/landing` is the only route the logo
          uniquely identifies. */}
      <Link to="/landing" className="topbar__identity" title="Aetos — front page">
        <AetosMark size={26} className="topbar__mark" />
        <span className="topbar__wordmark">AETOS</span>
        <span className="topbar__subtitle">Counter-UAS / Sensor Fusion</span>
      </Link>

      <div className="topbar__status">
        <Chip
          label="System"
          value={systemLabel(health, unavailable.length)}
          tone={health ? (unavailable.length ? 'warning' : 'nominal') : 'unknown'}
        />
        <Chip
          label="Stream"
          value={streamStatus.toUpperCase()}
          tone={streamStatus === 'live' ? 'nominal' : streamStatus === 'closed' ? 'critical' : 'warning'}
        />
        <Chip label="Tracks" value={String(trackCount)} tone="info" />
        <FusionChip health={health} />
      </div>

      <div className="topbar__right">
        <ModeIndicator mode={mode} />
        <span className="topbar__utc numeric" title="Coordinated Universal Time">
          {formatUtc(utc)}
        </span>
        {session && (
          <span
            className="topbar__operator"
            title={`Signed in as ${session.display_name} (${session.role}). Every action is recorded against this account.`}
          >
            {session.display_name}
            <span className="topbar__role">{session.role}</span>
          </span>
        )}
        <button
          type="button"
          className="topbar__signout"
          onClick={() => void signOut()}
          title="End this session"
        >
          Sign out
        </button>
      </div>
    </header>
  )
}

/**
 * Fusion's state, read from the health endpoint's `fusion` row — never a constant. It used to
 * say NOT BUILT as a literal, and the obvious fix, a literal ON, would be the same mistake the
 * other way round: a site that switches fusion off in its policy would still be told it was on.
 */
function FusionChip({ health }: { health: SystemHealthView | null }) {
  const fusion = health?.components.find((c) => c.component === 'fusion')
  if (!fusion) {
    return <Chip label="Fusion" value="—" tone="unknown" title="Not yet reported by the backend" />
  }
  const running = fusion.state === 'ok'
  return (
    <Chip
      label="Fusion"
      value={running ? 'ON' : 'OFF'}
      tone={running ? 'nominal' : 'inert'}
      title={fusion.detail}
    />
  )
}

function ModeIndicator({ mode }: { mode: ModeView | null }) {
  if (!mode) {
    // Unknown is shown as its own state. Defaulting to "SIMULATION" while the answer is still
    // in flight would be a guess about the one thing that must never be guessed.
    return (
      <span className="mode mode--unknown" title="Run mode has not been reported by the backend">
        MODE: UNKNOWN
      </span>
    )
  }

  const simulated = mode.simulated
  return (
    <span
      className={`mode ${simulated ? 'mode--simulation' : 'mode--live'}`}
      title={
        simulated
          ? `Configured mode: ${mode.mode}. No real sensor adapter is feeding the pipeline, so all data on screen is synthetic.`
          : `${mode.real_adapters_connected} real sensor adapter(s) feeding the pipeline. The hardware is unvalidated — treat every track as evidence to be checked. See docs/live_mode.md.`
      }
    >
      MODE: {simulated ? 'SIMULATION' : 'LIVE'}
    </span>
  )
}

type Tone = 'nominal' | 'info' | 'warning' | 'critical' | 'inert' | 'unknown'

function Chip({
  label,
  value,
  tone,
  title,
}: {
  label: string
  value: string
  tone: Tone
  title?: string
}) {
  return (
    <span className={`chip chip--${tone}`} title={title}>
      <span className="chip__label">{label}</span>
      <span className="chip__value numeric">{value}</span>
    </span>
  )
}

function systemLabel(health: SystemHealthView | null, degraded: number): string {
  if (!health) return 'UNKNOWN'
  return degraded ? `${degraded} DEGRADED` : 'ONLINE'
}

function formatUtc(date: Date): string {
  return `${date.toISOString().slice(11, 19)}Z`
}
