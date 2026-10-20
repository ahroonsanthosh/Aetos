/**
 * Named scenarios.
 *
 * Each one exists to demonstrate a specific claim about the system. The most important is
 * `false-positive`: a flock of birds directly over the substation that stays LOW. Any
 * C-UAS demo can turn something red when it gets close; staying calm when the object is
 * close and harmless is the harder and more valuable behaviour to show.
 */

import type { Enu, SensorStatus, WeatherCondition } from '@/types';
import type { FlightPlan } from './flight';
import { TAU } from '@/lib/geometry';

export interface SensorOverride {
  status: SensorStatus;
  /** Device health, 0–1. Defaults to the site value when omitted. */
  health?: number;
  reason?: string;
}

export interface Scenario {
  id: string;
  name: string;
  /** One line, shown on the control. Says what this proves, not what it contains. */
  description: string;
  flights: FlightPlan[];
  weather: WeatherCondition;
  sensorOverrides: Record<string, SensorOverride>;
}

function wp(e: number, n: number, u: number): Enu {
  return { e, n, u };
}

/**
 * A closed orbit, for loiter and survey patterns. Returned open-ended so it can be spliced
 * into a longer path; callers close the overall loop themselves.
 */
function orbit(centre: Enu, radius: number, altitude: number, points = 10, from = 0): Enu[] {
  const out: Enu[] = [];
  for (let i = 0; i < points; i++) {
    const a = from + (i / points) * TAU;
    out.push(wp(centre.e + Math.sin(a) * radius, centre.n + Math.cos(a) * radius, altitude));
  }
  return out;
}

const SUBSTATION: Enu = { e: 300, n: 45, u: 0 };

/** Defaults shared by most plans, so each scenario states only what makes it different. */
function plan(p: Partial<FlightPlan> & Pick<FlightPlan, 'id' | 'waypoints'>): FlightPlan {
  return {
    classification: 'probable_uav',
    confidence: 0.94,
    identity: 'unknown',
    speed: 14,
    loop: true,
    startDelay: 0,
    contributingSensors: ['RADAR-01', 'RF-01'],
    ...p,
  };
}

// ---------------------------------------------------------------------------
// Reusable paths
// ---------------------------------------------------------------------------

/** Well clear to the north, west to east and back. Never threatens anything. */
const TRANSIT_NORTH: Enu[] = [
  wp(-1100, 760, 95),
  wp(1100, 760, 95),
  wp(1150, 860, 100),
  wp(-1150, 860, 100),
  wp(-1100, 760, 95),
];

/** Closes on the site from the south-west, hesitates at the fence, withdraws. */
const APPROACH_SOUTH_WEST: Enu[] = [
  wp(-1500, -1200, 145),
  wp(-1000, -800, 115),
  wp(-680, -520, 85),
  wp(-500, -390, 62),
  wp(-430, -330, 52),
  wp(-520, -300, 58),
  wp(-700, -470, 78),
  wp(-1100, -880, 118),
  wp(-1500, -1200, 145),
];

/** A direct run at the substation, an orbit over it, and an exit to the south-east. */
const RUN_AT_SUBSTATION: Enu[] = [
  wp(1500, 820, 145),
  wp(1050, 500, 115),
  wp(720, 270, 88),
  wp(490, 145, 62),
  wp(370, 82, 46),
  ...orbit(SUBSTATION, 70, 40, 8),
  wp(400, -80, 55),
  wp(700, -300, 90),
  wp(1200, -620, 125),
  wp(1650, -520, 148),
  wp(1780, 200, 152),
  wp(1500, 820, 145),
];

/** Starts inside the fence and leaves. */
const DEPARTING: Enu[] = [
  wp(150, 180, 55),
  wp(-150, 520, 82),
  wp(-620, 980, 112),
  wp(-1150, 1420, 138),
  wp(-1420, 1150, 140),
  wp(-950, 700, 112),
  wp(-380, 330, 78),
  wp(150, 180, 55),
];

/** Runs the fence line at low level — the classic reconnaissance profile. */
const PERIMETER_PROBE: Enu[] = [
  wp(-560, -430, 38),
  wp(560, -430, 38),
  wp(560, 430, 42),
  wp(-560, 430, 42),
  wp(-560, -430, 38),
];

/** A lawnmower grid over the array field. */
const SURVEY_GRID: Enu[] = (() => {
  const out: Enu[] = [];
  for (let i = 0; i < 6; i++) {
    const n = -200 + i * 80;
    const east = i % 2 === 0;
    out.push(wp(east ? -430 : 60, n, 52));
    out.push(wp(east ? 60 : -430, n, 52));
  }
  out.push(wp(-430, -200, 52));
  return out;
})();

/** Birds. Slow, low, erratic, and directly over the most critical asset on the site. */
const BIRD_FLOCK: Enu[] = [
  ...orbit({ e: 290, n: 60, u: 0 }, 110, 46, 7),
  ...orbit({ e: 250, n: 20, u: 0 }, 70, 38, 7, 1.1),
  wp(400, 60, 46),
];

/** High, fast, authorised — a scheduled inspection flight. */
const AUTHORISED_INSPECTION: Enu[] = [
  wp(-430, 90, 65),
  wp(60, 90, 65),
  wp(60, -210, 65),
  wp(-430, -210, 65),
  wp(-430, 90, 65),
];

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

export const SCENARIOS: Scenario[] = [
  {
    id: 'idle',
    name: 'Reset',
    description: 'Quiet sky. All sensors nominal.',
    flights: [],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'single-uav',
    name: 'Single UAV',
    description: 'One UAV transiting well clear. Detected, tracked, correctly left alone.',
    flights: [plan({ id: 'TRK-001', waypoints: TRANSIT_NORTH, speed: 18 })],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'approach',
    name: 'UAV Approaching',
    description: 'Closes on the perimeter from the south-west, then withdraws.',
    flights: [plan({ id: 'TRK-004', waypoints: APPROACH_SOUTH_WEST, speed: 15 })],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'critical-approach',
    name: 'Critical Asset Approach',
    description: 'Unidentified UAV on a run at the substation. Threat rises with the trajectory, not the range.',
    flights: [
      plan({
        id: 'TRK-017',
        waypoints: RUN_AT_SUBSTATION,
        speed: 16,
        confidence: 0.96,
        contributingSensors: ['RADAR-01', 'CAM-01', 'RF-01', 'THERM-01'],
      }),
    ],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'departing',
    name: 'UAV Departing',
    description: 'Object leaving the site. Threat decays as the geometry opens.',
    flights: [plan({ id: 'TRK-021', waypoints: DEPARTING, speed: 17 })],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'multiple',
    name: 'Multiple UAVs',
    description: 'Four simultaneous tracks with different intent. Coordinated activity raises every score.',
    flights: [
      plan({ id: 'TRK-031', waypoints: RUN_AT_SUBSTATION, speed: 15 }),
      plan({ id: 'TRK-032', waypoints: PERIMETER_PROBE, speed: 13, declaredBehaviour: 'probing', startDelay: 6 }),
      plan({ id: 'TRK-033', waypoints: SURVEY_GRID, speed: 11, declaredBehaviour: 'surveying', startDelay: 14 }),
      plan({ id: 'TRK-034', waypoints: APPROACH_SOUTH_WEST, speed: 16, startDelay: 22 }),
      plan({
        id: 'TRK-035',
        waypoints: AUTHORISED_INSPECTION,
        speed: 9,
        classification: 'uav',
        confidence: 0.99,
        identity: 'authorised',
        contributingSensors: ['RADAR-01', 'RF-01', 'CAM-01'],
      }),
    ],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'false-positive',
    name: 'False Positive',
    description: 'A bird flock directly over the substation. Close, harmless, and the system says so.',
    flights: [
      plan({
        id: 'TRK-041',
        waypoints: BIRD_FLOCK,
        speed: 7,
        classification: 'bird',
        confidence: 0.87,
        identity: 'unknown',
        contributingSensors: ['RADAR-01', 'CAM-01'],
      }),
      plan({
        id: 'TRK-042',
        waypoints: TRANSIT_NORTH,
        speed: 62,
        classification: 'aircraft',
        confidence: 0.98,
        identity: 'known',
        contributingSensors: ['RADAR-01'],
      }),
    ],
    weather: 'clear',
    sensorOverrides: {},
  },
  {
    id: 'sensor-failure',
    name: 'Sensor Failure',
    description: 'RF sensor offline, camera degraded. Readiness, coverage and protection fall together.',
    flights: [plan({ id: 'TRK-051', waypoints: APPROACH_SOUTH_WEST, speed: 15, contributingSensors: ['RADAR-02'] })],
    weather: 'clear',
    sensorOverrides: {
      'RF-01': { status: 'offline', health: 0, reason: 'No response from sensor' },
      'CAM-01': { status: 'degraded', health: 0.42, reason: 'Lens obscuration' },
    },
  },
  {
    id: 'coverage-gap',
    name: 'Coverage Degradation',
    description: 'South-west radar down, and something approaching through the gap it leaves.',
    flights: [
      plan({
        id: 'TRK-061',
        waypoints: APPROACH_SOUTH_WEST,
        speed: 15,
        confidence: 0.71,
        contributingSensors: ['RADAR-01'],
      }),
    ],
    weather: 'clear',
    sensorOverrides: {
      'RADAR-02': { status: 'offline', health: 0, reason: 'Power fault at mast' },
    },
  },
  {
    id: 'high-threat',
    name: 'High-Threat Event',
    description: 'Run at the substation with a second track probing the fence, under degraded radar.',
    flights: [
      plan({
        id: 'TRK-018',
        waypoints: RUN_AT_SUBSTATION,
        speed: 19,
        confidence: 0.97,
        classification: 'uav',
        contributingSensors: ['RADAR-01', 'CAM-01', 'RF-01', 'THERM-01'],
      }),
      plan({
        id: 'TRK-019',
        waypoints: PERIMETER_PROBE,
        speed: 14,
        declaredBehaviour: 'probing',
        startDelay: 4,
        contributingSensors: ['RADAR-01', 'RF-01'],
      }),
    ],
    weather: 'clear',
    sensorOverrides: {
      'RADAR-02': { status: 'degraded', health: 0.55, reason: 'Rotation drive fault' },
    },
  },
  {
    id: 'weather',
    name: 'Weather Degradation',
    description: 'Fog. Electro-optical confidence drops; radar and RF are unaffected, and the system distinguishes them.',
    flights: [
      plan({
        id: 'TRK-071',
        waypoints: RUN_AT_SUBSTATION,
        speed: 14,
        confidence: 0.68,
        contributingSensors: ['RADAR-01', 'RF-01'],
      }),
    ],
    weather: 'fog',
    sensorOverrides: {},
  },
];

export const DEFAULT_SCENARIO_ID = 'critical-approach';

export function scenarioById(id: string): Scenario {
  return SCENARIOS.find((s) => s.id === id) ?? SCENARIOS[0]!;
}
