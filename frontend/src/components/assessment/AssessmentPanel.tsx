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
            Nothing has been concluded about this track yet. Both modules abstain rather than
            guess — classification when a track is coasting or no evidence has reached it, risk
            until a track is displayable and there is something to assess.
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
  // Factors that were consulted and found nothing. Worth showing — it is how an operator sees
  // a concern was considered rather than overlooked — but not at the same weight as findings.
  const silent = assessment.riskFactors.filter((factor) => !factor.material)

  return (
    <section className="assessment">
      <div className="label assessment__heading">Aetos Assessment</div>

      <div className="assessment__machine">
        <div className="assessment__row">
          <span className="label">Classification</span>
          <span className="numeric">
            {assessment.label.replace(/_/g, ' ').toUpperCase()}
            <span className="assessment__confidence">
              {(assessment.labelConfidence * 100).toFixed(0)}% confidence
            </span>
          </span>
        </div>

        {/* The distribution, not just the winner. `small_uas 0.55 / bird 0.40` and
            `small_uas 0.55 / medium_uas 0.40` have the same top label and completely
            different operational meaning, and only this shows the difference.

            Note the confidence figure above is deliberately *not* the top bar's percentage:
            it folds how peaked this distribution is, how much evidence produced it, and how
            mature the track is, so a sharp answer built on one detection reads as uncertain
            (ADR 0012). */}
        {assessment.distribution.length > 0 && (
          <div className="assessment__distribution">
            {assessment.distribution.map(({ label, probability }) => (
              <div key={label} className="assessment__bar-row">
                <span className="assessment__bar-label">{label.replace(/_/g, ' ')}</span>
                <div className="assessment__bar-track">
                  <div
                    className={`assessment__bar ${
                      label === assessment.label ? 'is-leading' : ''
                    }`}
                    style={{ width: `${Math.max(1, probability * 100)}%` }}
                  />
                </div>
                {/* A label that rounds to zero is not a label at zero. Printing "0%" beside
                    a visible bar states something the classifier did not conclude. */}
                <span className="assessment__bar-value numeric">
                  {probability < 0.005 ? '<1%' : `${(probability * 100).toFixed(0)}%`}
                </span>
              </div>
            ))}
          </div>
        )}

        {assessment.classificationReasons.length > 0 && (
          <div className="assessment__reasons">
            <span className="label">Classified because</span>
            <ul>
              {assessment.classificationReasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Risk is reported only if the engine actually ran against this track. A score of
            zero from a module that ran means "we looked, and nothing here is concerning"; the
            same zero from one that did not means nothing at all. Rendering them the same way
            is the more dangerous of the two misreadings, and precisely what ADR 0011 exists
            to prevent. */}
        {assessment.riskAssessed ? (
          <>
            <div className="assessment__row">
              <span className="label">Risk</span>
              <span
                className={`numeric assessment__risk assessment__risk--${assessment.riskLevel}`}
              >
                {assessment.riskLevel.toUpperCase()} · {assessment.riskScore}
                {/* Confidence sits beside the score and is deliberately not folded into it:
                    a thin-evidence track on alarming geometry is exactly the case an operator
                    must see, and multiplying the two would quietly suppress it. */}
                <span className="assessment__confidence">
                  {(assessment.riskConfidence * 100).toFixed(0)}% confidence
                </span>
              </span>
            </div>

            {/* The score never travels without its reasons. A bare number is a black-box
                output an operator can neither act on nor challenge. Ordered by how much each
                factor actually moved the score. */}
            <div className="assessment__reasons">
              <span className="label">Because</span>
              {assessment.reasons.length ? (
                <ul>
                  {assessment.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              ) : (
                <span className="assessment__none">
                  Nothing about this track is currently concerning.
                </span>
              )}
            </div>

            {/* What was checked and found absent. Shown collapsed, because an operator asking
                "did it consider X" deserves an answer, and an operator not asking should not
                have to read nine lines to get past it. */}
            {silent.length > 0 && (
              <details className="assessment__checked">
                <summary className="label">
                  {silent.length} further {silent.length === 1 ? 'check' : 'checks'}, nothing
                  found
                </summary>
                <ul>
                  {silent.map((factor) => (
                    <li key={factor.code}>{factor.detail}</li>
                  ))}
                </ul>
              </details>
            )}

            <div className="assessment__row">
              <span className="label">Recommendation</span>
              <span className={`assessment__recommendation assessment__recommendation--${tone}`}>
                {assessment.recommendation.replace(/_/g, ' ')}
              </span>
            </div>
          </>
        ) : (
          <div className="assessment__row">
            <span className="label">Risk</span>
            <span className="assessment__none">Not assessed</span>
          </div>
        )}
      </div>

      {/* The separation. Deliberately a different block, differently styled, below a rule. */}
      <div className="assessment__divider" role="separator" />

      <div className="assessment__human">
        <div className="label assessment__heading">Operator Decision</div>
        {/* Worded to match what is actually above it. Referring to "the recommendation" when
            no recommendation was rendered would describe a screen the operator is not
            looking at. */}
        <p className="assessment__human-body">
          {assessment.riskAssessed
            ? 'The recommendation above is machine-generated advice. Any decision or action is taken by a human operator, outside this system.'
            : 'Everything above is machine-generated assessment. Any decision or action is taken by a human operator, outside this system.'}
        </p>
        <div className="assessment__human-status">
          <span className="label">Status</span>
          <span>Awaiting operator</span>
        </div>
      </div>
    </section>
  )
}
