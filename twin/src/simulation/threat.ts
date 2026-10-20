/**
 * Simulated threat assessment.
 *
 * This is not the real Aetos threat engine and does not pretend to be. It is a
 * deterministic model that produces believable, explainable numbers so the interface can
 * demonstrate the one idea that matters:
 *
 *   Proximity is not threat. A bird over the substation is not a threat. An unidentified
 *   UAV 400 m out whose predicted path enters the substation volume is.
 *
 * Every contribution is named. The score is never a bare number — `factors` travels with
 * it so the panel can explain itself without recomputing anything.
 */

import type {
  Asset,
  Behaviour,
  Classification,
  Enu,
  Identity,
  ThreatAssessment,
  ThreatFactor,
  ThreatLevel,
  TrackPoint,
  Zone,
} from '@/types';
import { clamp, clamp01, firstEntryIntoVolume, horizontalDistance, pointInVolume } from '@/lib/geometry';

/**
 * How much each classification can contribute at all. A confirmed bird is capped near
 * zero however close it flies; this single line is what stops the demo crying wolf.
 */
const CLASS_WEIGHT: Record<Classification, number> = {
  uav: 1,
  probable_uav: 0.9,
  unknown: 0.6,
  helicopter: 0.35,
  aircraft: 0.3,
  balloon: 0.15,
  bird: 0.04,
};

const IDENTITY_POINTS: Record<Identity, number> = {
  unknown: 14,
  known: 5,
  authorised: 0,
};

const BEHAVIOUR_POINTS: Record<Behaviour, number> = {
  probing: 16,
  loitering: 12,
  surveying: 11,
  approaching: 9,
  transiting: 2,
  departing: 0,
};

const BEHAVIOUR_LABEL: Record<Behaviour, string> = {
  probing: 'Repeated perimeter probing',
  loitering: 'Loitering over site',
  surveying: 'Survey pattern',
  approaching: 'Approaching site',
  transiting: 'Transiting',
  departing: 'Departing',
};

/** Level bands. Hysteresis is applied on top so the level does not chatter on the edge. */
const BANDS: { level: ThreatLevel; min: number }[] = [
  { level: 'critical', min: 75 },
  { level: 'high', min: 50 },
  { level: 'medium', min: 25 },
  { level: 'low', min: 0 },
];

/** Points of overshoot required before a level rises or falls. Mirrors ADR 0007. */
const HYSTERESIS = 4;

export interface ThreatInputs {
  position: Enu;
  speed: number;
  classification: Classification;
  /** Confidence in the classification, 0–1. */
  confidence: number;
  identity: Identity;
  behaviour: Behaviour;
  /** Extrapolated path, nearest first. */
  predicted: TrackPoint[];
  /** Milliseconds since epoch, matching `predicted[].t`. */
  now: number;
  assets: Asset[];
  zones: Zone[];
  /** How many UAV-like tracks are up at once. Coordinated activity is worse than one. */
  simultaneousUavCount: number;
  /** Aggregate effectiveness of the sensors holding this track, 0–1. */
  sensorConfidence: number;
  /** Level from the previous tick, for hysteresis. */
  previousLevel?: ThreatLevel;
}

function push(factors: ThreatFactor[], code: string, label: string, contribution: number): void {
  if (Math.abs(contribution) < 0.5) return;
  factors.push({ code, label, contribution: Math.round(contribution * 10) / 10 });
}

/** The nearest asset weighted by how much it matters — not simply the nearest asset. */
function findThreatenedAsset(
  position: Enu,
  assets: Asset[],
): { asset: Asset; distance: number } | undefined {
  let best: { asset: Asset; distance: number; weight: number } | undefined;
  for (const asset of assets) {
    if (asset.criticality < 5) continue;
    const distance = horizontalDistance(position, asset.position);
    // Criticality raises the distance at which an asset is considered the relevant target.
    const weight = asset.criticality / Math.max(distance, 1);
    if (!best || weight > best.weight) best = { asset, distance, weight };
  }
  return best ? { asset: best.asset, distance: best.distance } : undefined;
}

export function assessThreat(input: ThreatInputs): ThreatAssessment {
  const factors: ThreatFactor[] = [];
  const classWeight = CLASS_WEIGHT[input.classification];

  // --- What is it, and how sure are we? -----------------------------------
  const objectConfidence = classWeight * input.confidence;
  push(
    factors,
    'OBJ_CONF',
    `${labelFor(input.classification)} at ${Math.round(input.confidence * 100)}% confidence`,
    objectConfidence * 18,
  );

  // --- Whose is it? -------------------------------------------------------
  const identityPoints = IDENTITY_POINTS[input.identity] * classWeight;
  push(
    factors,
    'IDENTITY',
    input.identity === 'unknown'
      ? 'No authorised identity'
      : input.identity === 'known'
        ? 'Known, not authorised for this airspace'
        : 'Authorised flight',
    identityPoints,
  );

  // --- What is it doing? --------------------------------------------------
  push(
    factors,
    'BEHAVIOUR',
    BEHAVIOUR_LABEL[input.behaviour],
    BEHAVIOUR_POINTS[input.behaviour] * classWeight,
  );

  // --- What does it threaten? ---------------------------------------------
  const target = findThreatenedAsset(input.position, input.assets);
  let threatenedAssetId: string | undefined;

  if (target) {
    // Proximity falls off over 600 m. Beyond that, distance alone contributes nothing.
    const proximity = clamp01(1 - target.distance / 600);
    const proximityPoints = proximity * proximity * 16 * classWeight;
    push(
      factors,
      'PROXIMITY',
      `${Math.round(target.distance)} m from ${target.asset.name}`,
      proximityPoints,
    );

    // Criticality only counts when something is actually near it.
    const criticalityPoints = (target.asset.criticality / 10) * 14 * proximity * classWeight;
    push(
      factors,
      'TARGET_CRIT',
      `${target.asset.name} criticality ${target.asset.criticality}/10`,
      criticalityPoints,
    );

    if (proximityPoints > 1) threatenedAssetId = target.asset.id;
  }

  // --- Where is it going? -------------------------------------------------
  // The heaviest single contribution, and the one that makes the picture asset-aware
  // rather than range-aware.
  let etaToProtectedZone: number | undefined;
  let predictedZoneId: string | undefined;

  let earliest: { zone: Zone; t: number } | undefined;
  for (const zone of input.zones) {
    const entry = firstEntryIntoVolume(input.predicted, zone.polygon, zone.floor, zone.ceiling);
    if (!entry) continue;
    if (!earliest || entry.t < earliest.t) earliest = { zone, t: entry.t };
    // A predicted entry into a critical volume is always reported, even if a perimeter
    // crossing happens sooner.
    if (zone.zoneClass === 'critical' && (!predictedZoneId || entry.t < (etaToProtectedZone ?? Infinity))) {
      predictedZoneId = zone.id;
      etaToProtectedZone = (entry.t - input.now) / 1000;
      if (zone.protectsAssetId) threatenedAssetId = zone.protectsAssetId;
    }
  }

  if (!predictedZoneId && earliest) {
    predictedZoneId = earliest.zone.id;
    etaToProtectedZone = (earliest.t - input.now) / 1000;
  }

  if (predictedZoneId && etaToProtectedZone !== undefined) {
    const zone = input.zones.find((z) => z.id === predictedZoneId)!;
    const classPoints = zone.zoneClass === 'critical' ? 26 : zone.zoneClass === 'restricted' ? 15 : 8;
    // Imminence: full weight inside 30 s, decaying to nothing at 180 s.
    const imminence = clamp01(1 - (etaToProtectedZone - 30) / 150);
    push(
      factors,
      'TRAJECTORY',
      `Predicted path enters ${zone.name} in ${Math.round(etaToProtectedZone)} s`,
      classPoints * imminence * classWeight,
    );
  }

  // --- Is it already inside? ----------------------------------------------
  for (const zone of input.zones) {
    if (!pointInVolume(input.position, zone.polygon, zone.floor, zone.ceiling)) continue;
    const points = zone.zoneClass === 'critical' ? 22 : zone.zoneClass === 'restricted' ? 13 : 6;
    push(factors, 'INCURSION', `Inside ${zone.name}`, points * classWeight);
    if (zone.zoneClass === 'critical' && zone.protectsAssetId) {
      threatenedAssetId = zone.protectsAssetId;
    }
  }

  // --- How fast? ----------------------------------------------------------
  // Speed matters as time-to-target, not as a virtue in itself.
  if (target && input.speed > 6) {
    const speedPoints = clamp01((input.speed - 6) / 22) * 7 * classWeight;
    push(factors, 'VELOCITY', `${input.speed.toFixed(1)} m/s`, speedPoints);
  }

  // --- How many? ----------------------------------------------------------
  if (input.simultaneousUavCount > 1) {
    push(
      factors,
      'MULTI_TRACK',
      `${input.simultaneousUavCount} simultaneous UAV tracks`,
      Math.min(input.simultaneousUavCount - 1, 4) * 3.5 * classWeight,
    );
  }

  // --- How well are we seeing it? -----------------------------------------
  // Degraded sensing does not make an object safer, so this never reduces the score to
  // comfort. It reduces the *confidence-driven* part only, and is surfaced as its own
  // line so the operator knows the assessment is standing on thinner evidence.
  if (input.sensorConfidence < 0.75) {
    push(
      factors,
      'SENSOR_CONF',
      `Degraded sensor coverage on this track (${Math.round(input.sensorConfidence * 100)}%)`,
      -(0.75 - input.sensorConfidence) * 10,
    );
  }

  const raw = factors.reduce((sum, f) => sum + f.contribution, 0);
  const score = clamp(Math.round(raw), 0, 100);

  factors.sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));

  return {
    score,
    level: levelFor(score, input.previousLevel),
    factors,
    threatenedAssetId,
    etaToProtectedZone: etaToProtectedZone !== undefined && etaToProtectedZone > 0 ? etaToProtectedZone : undefined,
    predictedZoneId,
  };
}

/**
 * Band the score, refusing to move the level until the score has cleared the boundary by
 * `HYSTERESIS`. A track sitting exactly on 50 should not strobe between MEDIUM and HIGH.
 */
export function levelFor(score: number, previous?: ThreatLevel): ThreatLevel {
  const band = BANDS.find((b) => score >= b.min)!.level;
  if (!previous || band === previous) return band;

  const order: ThreatLevel[] = ['low', 'medium', 'high', 'critical'];
  const rising = order.indexOf(band) > order.indexOf(previous);
  const boundary = rising
    ? BANDS.find((b) => b.level === band)!.min
    : BANDS.find((b) => b.level === previous)!.min;

  if (rising) return score >= boundary + HYSTERESIS ? band : previous;
  return score <= boundary - HYSTERESIS ? band : previous;
}

export function labelFor(classification: Classification): string {
  switch (classification) {
    case 'uav':
      return 'UAV';
    case 'probable_uav':
      return 'Probable UAV';
    case 'aircraft':
      return 'Aircraft';
    case 'helicopter':
      return 'Helicopter';
    case 'bird':
      return 'Bird';
    case 'balloon':
      return 'Balloon';
    case 'unknown':
      return 'Unknown object';
  }
}
