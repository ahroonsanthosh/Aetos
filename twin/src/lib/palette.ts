/**
 * The Aetos Command palette.
 *
 * One palette, shared by the 3D scene and the DOM interface, so that a HIGH track and the
 * word HIGH in a panel are the same colour by construction rather than by coincidence.
 *
 * The restraint is deliberate. Saturated colour is a signal here, not decoration: the
 * environment sits in cool, desaturated blues, and the moment anything goes amber or red
 * it is because the situation warrants attention. Glow comes from bloom picking up
 * emissive intensity, not from stacking bright colours.
 */

import type { CoverageQuality, SensorStatus, ThreatLevel, ZoneClass } from '@/types';

export const palette = {
  /** Deep, slightly blue void. Not pure black — pure black kills depth cues. */
  void: '#04070b',
  abyss: '#020407',

  /** The holographic base. Everything structural is a tint of this. */
  holo: '#58d8e6',
  holoDim: '#2b6b78',
  holoDeep: '#123138',

  /** Physical structure that is not part of the hologram's data layer. */
  steel: '#6f93ad',
  steelDim: '#3c5567',

  /** Ground plane and grid. */
  ground: '#0d1822',
  grid: '#1a4453',
  gridMajor: '#2a6577',

  /** Text. */
  text: '#d8e9f2',
  textDim: '#8ba4b4',
  textFaint: '#54697a',

  /** Status. */
  nominal: '#3fd39b',
  caution: '#e9b24c',
  warning: '#ff7a45',
  danger: '#ff4d5e',
  offline: '#5d4550',

  /** Selection and focus. */
  select: '#ffffff',
  authorised: '#5ad1ff',
} as const;

export const threatColour: Record<ThreatLevel, string> = {
  low: palette.nominal,
  medium: palette.caution,
  high: palette.warning,
  critical: palette.danger,
};

/**
 * How strongly a threat level should read. Fed to emissive intensity, marker scale and
 * animation rate together — the brief is explicit that colour alone must not carry level.
 */
export const threatIntensity: Record<ThreatLevel, number> = {
  low: 0.5,
  medium: 1.0,
  high: 1.8,
  critical: 2.8,
};

export const threatLabel: Record<ThreatLevel, string> = {
  low: 'LOW',
  medium: 'MEDIUM',
  high: 'HIGH',
  critical: 'CRITICAL',
};

export const coverageColour: Record<CoverageQuality, string> = {
  strong: palette.nominal,
  reduced: palette.caution,
  gap: palette.danger,
};

export const sensorColour: Record<SensorStatus, string> = {
  online: palette.holo,
  degraded: palette.caution,
  offline: palette.offline,
};

/**
 * Base visibility of a zone volume before threat emphasis is applied. A perimeter zone is
 * almost invisible at rest; a critical zone is faintly present so the operator always
 * knows where it is.
 */
/**
 * Scaled by area, not just by class. The perimeter zone wraps the entire site, so the
 * same opacity that makes a 150 m substation volume read as a faint presence makes the
 * perimeter read as a slab of glass laid over everything. It is drawn almost entirely by
 * its floor outline instead.
 */
export const zoneBaseOpacity: Record<ZoneClass, number> = {
  perimeter: 0.018,
  restricted: 0.05,
  critical: 0.08,
};

/**
 * Ceiling on emphasis-driven opacity, per class.
 *
 * Kept low because the volumes blend additively: a zone inside a zone inside the
 * perimeter sums three times over, so the value that looks right in isolation is roughly
 * three times too strong in the place it actually matters.
 */
export const zoneMaxOpacity: Record<ZoneClass, number> = {
  perimeter: 0.1,
  restricted: 0.4,
  critical: 0.62,
};

/**
 * Zone colour is keyed to state, not class.
 *
 * A critical zone permanently drawn in amber is amber for the 99% of the time nothing is
 * happening, which spends the operator's alarm response on the resting state and leaves
 * nothing to escalate with. At rest every zone sits in the environment's own cool blue and
 * is distinguished by opacity and altitude; warm colour is reserved for a volume something
 * is actually predicted into.
 */
export const zoneRestColour: Record<ZoneClass, string> = {
  perimeter: palette.holoDim,
  restricted: palette.holoDim,
  critical: palette.holo,
};

export const zoneActiveColour: Record<ZoneClass, string> = {
  perimeter: palette.caution,
  restricted: palette.caution,
  critical: palette.warning,
};
