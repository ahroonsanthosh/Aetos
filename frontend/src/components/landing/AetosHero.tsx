/**
 * The public front page: the mark, the name, one line on what Aetos does, and a way in to
 * sign-in. Nothing else stands between a visitor and the console. Neither way in animates
 * anything; "Enter without motion" is kept because visitors look for it.
 *
 * Nothing here says "neutralise". This system stops at a recommendation to a human, and the
 * front page is the last place to claim a capability the product deliberately does not have.
 */

import { AetosMark } from '../brand/AetosMark'
import './AetosHero.css'

interface Props {
  onLaunch: () => void
}

export function AetosHero({ onLaunch }: Props) {
  return (
    <div className="front">
      <main className="front__centre">
        <AetosMark size={88} className="front__mark" />
        <h1 className="front__name">Aetos</h1>
        <p className="front__line">
          Command software for counter-drone teams. It fuses every sensor into one picture,
          scores what it sees, and tells a human what it recommends.
        </p>
        <button type="button" className="pill pill--ink front__enter" onClick={onLaunch}>
          Enter
        </button>
      </main>
      <footer className="front__foot">
        <button type="button" className="front__quiet" onClick={onLaunch}>
          Enter without motion
        </button>
      </footer>
    </div>
  )
}
