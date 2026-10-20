/**
 * Threat model tests.
 *
 * These exist to pin the behaviour the whole interface is built to demonstrate: that
 * threat follows trajectory and asset criticality, not range. If a change makes a bird
 * over the substation dangerous, or a UAV heading for the substation merely "close", the
 * demonstration is worthless however good it looks — so those two cases are asserted
 * directly rather than left to be noticed in a screenshot.
 */

import { describe, expect, it } from 'vitest';

import { DEMO_SITE } from '@/site/demo-site';
import type { Enu, TrackPoint } from '@/types';
import { extrapolate } from './flight';
import { assessThreat, levelFor, type ThreatInputs } from './threat';

const NOW = 1_700_000_000_000;

function inputs(overrides: Partial<ThreatInputs> & { position: Enu; velocity?: Enu }): ThreatInputs {
  const velocity = overrides.velocity ?? { e: 0, n: 0, u: 0 };
  const predicted: TrackPoint[] = extrapolate(overrides.position, velocity, NOW);

  // `overrides` supplies `position` — its type requires it — so it is not restated here;
  // restating it before the spread only creates a key the spread immediately overwrites.
  return {
    speed: Math.hypot(velocity.e, velocity.n, velocity.u),
    classification: 'probable_uav',
    confidence: 0.95,
    identity: 'unknown',
    behaviour: 'approaching',
    predicted,
    now: NOW,
    assets: DEMO_SITE.assets,
    zones: DEMO_SITE.zones,
    simultaneousUavCount: 1,
    sensorConfidence: 0.95,
    ...overrides,
  };
}

/** The substation: criticality 10, and the target of the critical-approach scenario. */
const SUBSTATION = { e: 300, n: 45 };

describe('assessThreat', () => {
  it('scores a UAV closing on the substation far above one at the same range leaving it', () => {
    const position: Enu = { e: 700, n: 45, u: 60 };

    const inbound = assessThreat(
      inputs({ position, velocity: { e: -18, n: 0, u: -0.4 }, behaviour: 'approaching' }),
    );
    const outbound = assessThreat(
      inputs({ position, velocity: { e: 18, n: 0, u: 0.4 }, behaviour: 'departing' }),
    );

    // Identical objects at an identical range. Only the direction of travel differs.
    expect(inbound.score).toBeGreaterThan(outbound.score + 20);
    expect(inbound.threatenedAssetId).toBe('SUB-01');
  });

  it('keeps a bird directly over the substation at LOW', () => {
    const bird = assessThreat(
      inputs({
        position: { e: SUBSTATION.e, n: SUBSTATION.n, u: 45 },
        velocity: { e: 3, n: 1, u: 0 },
        classification: 'bird',
        confidence: 0.87,
        behaviour: 'loitering',
      }),
    );

    // As close as an object can get, and inside the critical volume. Still not a threat.
    expect(bird.level).toBe('low');
    expect(bird.score).toBeLessThan(25);
  });

  it('rates an unidentified UAV in the same position as far more serious than the bird', () => {
    const uav = assessThreat(
      inputs({
        position: { e: SUBSTATION.e, n: SUBSTATION.n, u: 45 },
        velocity: { e: 3, n: 1, u: 0 },
        classification: 'uav',
        behaviour: 'loitering',
      }),
    );

    expect(uav.score).toBeGreaterThan(60);
    expect(['high', 'critical']).toContain(uav.level);
  });

  it('discounts an authorised flight over the same ground', () => {
    const position: Enu = { e: 400, n: 45, u: 55 };
    const velocity: Enu = { e: -14, n: 0, u: 0 };

    const unknown = assessThreat(inputs({ position, velocity, identity: 'unknown' }));
    const authorised = assessThreat(inputs({ position, velocity, identity: 'authorised' }));

    expect(authorised.score).toBeLessThan(unknown.score);
  });

  it('reports an ETA when the predicted path enters a protected volume', () => {
    const assessment = assessThreat(
      inputs({ position: { e: 800, n: 45, u: 60 }, velocity: { e: -20, n: 0, u: -0.3 } }),
    );

    expect(assessment.etaToProtectedZone).toBeDefined();
    expect(assessment.etaToProtectedZone!).toBeGreaterThan(0);
    expect(assessment.predictedZoneId).toBeDefined();
  });

  it('always explains its score', () => {
    const assessment = assessThreat(
      inputs({ position: { e: 500, n: 45, u: 60 }, velocity: { e: -16, n: 0, u: 0 } }),
    );

    expect(assessment.factors.length).toBeGreaterThan(0);
    // Sorted by magnitude, so the panel can show the top few and be showing the reasons
    // that actually moved the number.
    const magnitudes = assessment.factors.map((f) => Math.abs(f.contribution));
    expect([...magnitudes].sort((a, b) => b - a)).toEqual(magnitudes);
  });

  it('raises the score when several UAVs are up at once', () => {
    const position: Enu = { e: 500, n: 45, u: 60 };
    const velocity: Enu = { e: -16, n: 0, u: 0 };

    const alone = assessThreat(inputs({ position, velocity, simultaneousUavCount: 1 }));
    const swarm = assessThreat(inputs({ position, velocity, simultaneousUavCount: 4 }));

    expect(swarm.score).toBeGreaterThan(alone.score);
  });
});

describe('levelFor', () => {
  it('bands a score with no previous level', () => {
    expect(levelFor(10)).toBe('low');
    expect(levelFor(30)).toBe('medium');
    expect(levelFor(60)).toBe('high');
    expect(levelFor(90)).toBe('critical');
  });

  it('holds the previous level inside the hysteresis band', () => {
    // 51 is nominally HIGH, but not by enough to justify moving off MEDIUM.
    expect(levelFor(51, 'medium')).toBe('medium');
    expect(levelFor(56, 'medium')).toBe('high');

    // Falling back needs the same clearance, so a score hovering on the boundary cannot
    // strobe the operator's display.
    expect(levelFor(49, 'high')).toBe('high');
    expect(levelFor(44, 'high')).toBe('medium');
  });
});
