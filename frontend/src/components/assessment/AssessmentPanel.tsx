/**
 * Assessment: what Aetos concluded, and — separately — what a human must decide.
 *
 * The specification requires these two never be merged into one visual unit, and that is the
 * whole point of this component. They are rendered as two distinct blocks with different
 * treatments and an explicit divider, because merging them would let a machine recommendation
 * read as a decision already taken. Aetos produces advice. The authority is the operator, and
 * the interface has to say so rather than imply it.
 *
 * Nothing here can act. There is no control on this panel, or anywhere in this application,
 * that does anything in the physical world — see `response/README.md`.
 */

import type { DisplayTrack } from '../../data/models/track'
import './AssessmentPanel.css'

interface Props {
  track: DisplayTrack
}

const RECOMMENDATION_TONE: Record<string, string> = {
  MONITOR: 'info',
  VERIFY: 'info',
  ALERT: 'warning',
  ESCALATE: 'elevated',
  RESPONSE_RECOMMENDED: 'critical',
}

export function AssessmentPanel({ track }: Props) {
  const { assessment } = track

  if (!assessment.available) {
    // The default an unassessed track carries is `unknown` at zero confidence with a zero
    // score. Rendering that as a finding would present an unassessed track as
    // assessed-and-benign, which is the more dangerous of the two possible mistakes.
    return (
      <section className="assessment">
        <div className="label assessment__heading">Aetos Assessment</div>
        <div className="assessment__unavailable">
          <div className="assessment__unavailable-title">Not assessed</div>
          <p className="assessment__unavailable-body">
            Classification and threat assessment are not implemented in this build. This track
            has a position and a motion estimate; nothing has judged what it is or whether it
            matters.
          </p>
        </div>

        {/* The operator block is shown even with nothing to assess. The separation between
            machine output and human authority is a standing property of the interface, not a
            decoration that appears alongside a recommendation — and an operator should never
            learn it by its absence. */}
        <div className="assessment__divider" role="separator" />

        <div className="assessment__human">
          <div className="label assessment__heading">Operator Decision</div>
          <p className="assessment__human-body">
            Aetos produces advice. Any decision or action is taken by a human operator, outside
            this system.
          </p>
        </div>
      </section>
    )
  }

  const tone = RECOMMENDATION_TONE[assessment.recommendation] ?? 'info'

  return (
    <section className="assessment">
      <div className="label assessment__heading">Aetos Assessment</div>

      <div className="assessment__machine">
        <div className="assessment__row">
          <span className="label">Classification</span>
          <span className="numeric">
            {assessment.label.replace(/_/g, ' ').toUpperCase()}
            <span className="assessment__confidence">
              {(assessment.labelConfidence * 100).toFixed(0)}%
            </span>
          </span>
        </div>

        <div className="assessment__row">
          <span className="label">Risk</span>
          <span className={`numeric assessment__risk assessment__risk--${assessment.riskLevel}`}>
            {assessment.riskLevel.toUpperCase()} · {assessment.riskScore}
          </span>
        </div>

        {/* The score never travels without its reasons. A bare number is a black-box output
            an operator can neither act on nor challenge. */}
        <div className="assessment__reasons">
          <span className="label">Because</span>
          {assessment.reasons.length ? (
            <ul>
              {assessment.reasons.map((reason) => (
                <li key={reason}>{reason.replace(/_/g, ' ')}</li>
              ))}
            </ul>
          ) : (
            <span className="assessment__none">No reason codes supplied</span>
          )}
        </div>

        <div className="assessment__row">
          <span className="label">Recommendation</span>
          <span className={`assessment__recommendation assessment__recommendation--${tone}`}>
            {assessment.recommendation.replace(/_/g, ' ')}
          </span>
        </div>
      </div>

      {/* The separation. Deliberately a different block, differently styled, below a rule. */}
      <div className="assessment__divider" role="separator" />

      <div className="assessment__human">
        <div className="label assessment__heading">Operator Decision</div>
        <p className="assessment__human-body">
          The recommendation above is machine-generated advice. Any decision or action is
          taken by a human operator, outside this system.
        </p>
        <div className="assessment__human-status">
          <span className="label">Status</span>
          <span>Awaiting operator</span>
        </div>
      </div>
    </section>
  )
}
