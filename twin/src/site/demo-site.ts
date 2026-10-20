/**
 * Ardnacrusha Solar — the fictional demonstration facility.
 *
 * This is data, not geometry. Nothing here describes how the site is drawn; it describes
 * what is there, where, and how much it matters. The 3D layer reads this file and builds
 * a representation from it, which is what allows a real site survey to replace this file
 * without touching the renderer.
 *
 * Frame: local ENU metres, origin at the site datum, +e east, +n north, +u up.
 */

import type {
  Asset,
  Ground,
  ResponseResource,
  Sensor,
  Site,
  SolarArrayBlock,
  Zone,
} from '@/types';

const DEG = Math.PI / 180;

/** Axis-aligned rectangle as a closed ground polygon, counter-clockwise. */
function rect(centreE: number, centreN: number, width: number, depth: number): Ground[] {
  const he = width / 2;
  const hn = depth / 2;
  return [
    { e: centreE - he, n: centreN - hn },
    { e: centreE + he, n: centreN - hn },
    { e: centreE + he, n: centreN + hn },
    { e: centreE - he, n: centreN + hn },
  ];
}

// ---------------------------------------------------------------------------
// Boundary
// ---------------------------------------------------------------------------

/**
 * Site boundary. Deliberately not a plain rectangle — the north-east corner is chamfered
 * where the access easement cuts across, which is the kind of irregularity that makes a
 * digital twin read as a real place rather than a demo box.
 */
const BOUNDARY: Ground[] = [
  { e: -470, n: -340 },
  { e: 470, n: -340 },
  { e: 470, n: 190 },
  { e: 360, n: 340 },
  { e: -470, n: 340 },
];

// ---------------------------------------------------------------------------
// Solar field
// ---------------------------------------------------------------------------

const ROW_PITCH = 9;
const TABLE_PITCH = 5.2;
const ROWS = 12;
const TABLES = 30;

/**
 * Six blocks in two bands, split by the central access road. Each block expands to 360
 * tables at render time; the data model never holds 2,160 objects.
 */
const ARRAYS: SolarArrayBlock[] = [
  { id: 'BLK-A1', assetId: 'ARRAY-A', origin: { e: -430, n: 40 }, heading: 0, rows: ROWS, tablesPerRow: TABLES, rowPitch: ROW_PITCH, tablePitch: TABLE_PITCH },
  { id: 'BLK-A2', assetId: 'ARRAY-A', origin: { e: -430, n: -160 }, heading: 0, rows: ROWS, tablesPerRow: TABLES, rowPitch: ROW_PITCH, tablePitch: TABLE_PITCH },
  { id: 'BLK-B1', assetId: 'ARRAY-B', origin: { e: -250, n: 40 }, heading: 0, rows: ROWS, tablesPerRow: TABLES, rowPitch: ROW_PITCH, tablePitch: TABLE_PITCH },
  { id: 'BLK-B2', assetId: 'ARRAY-B', origin: { e: -250, n: -160 }, heading: 0, rows: ROWS, tablesPerRow: TABLES, rowPitch: ROW_PITCH, tablePitch: TABLE_PITCH },
  { id: 'BLK-C1', assetId: 'ARRAY-C', origin: { e: -70, n: 40 }, heading: 0, rows: ROWS, tablesPerRow: TABLES, rowPitch: ROW_PITCH, tablePitch: TABLE_PITCH },
  { id: 'BLK-C2', assetId: 'ARRAY-C', origin: { e: -70, n: -160 }, heading: 0, rows: ROWS, tablesPerRow: TABLES, rowPitch: ROW_PITCH, tablePitch: TABLE_PITCH },
];

// ---------------------------------------------------------------------------
// Roads
// ---------------------------------------------------------------------------

/** Centrelines. The renderer builds ribbons from these. */
const ROADS: Ground[][] = [
  // Main east-west spine, from the west gate to the substation compound.
  [
    { e: -470, n: -6 },
    { e: 120, n: -6 },
    { e: 200, n: -6 },
    { e: 240, n: 20 },
    { e: 300, n: 20 },
  ],
  // North-south laterals between array blocks.
  [
    { e: -262, n: -300 },
    { e: -262, n: 300 },
  ],
  [
    { e: -82, n: -300 },
    { e: -82, n: 300 },
  ],
  // Spur to the control building and battery compound.
  [
    { e: 200, n: -6 },
    { e: 200, n: -140 },
    { e: 300, n: -140 },
  ],
];

// ---------------------------------------------------------------------------
// Assets
// ---------------------------------------------------------------------------

/**
 * `sensorsCovering`, `activeThreats` and `protectionScore` are seeded here and overwritten
 * every tick by the snapshot. The values below are what the interface shows before the
 * first tick arrives.
 */
const ASSETS: Asset[] = [
  {
    id: 'SUB-01',
    name: 'Substation 01',
    type: 'substation',
    position: { e: 300, n: 45, u: 0 },
    footprint: { width: 110, depth: 84, height: 14 },
    heading: 0,
    criticality: 10,
    protectionZoneId: 'ZONE-CRIT-SUB',
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.97,
  },
  {
    id: 'TX-01',
    name: 'Transformer 01',
    type: 'transformer',
    position: { e: 252, n: 14, u: 0 },
    footprint: { width: 12, depth: 8, height: 7 },
    heading: 0,
    criticality: 9,
    protectionZoneId: 'ZONE-CRIT-SUB',
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.95,
  },
  {
    id: 'TX-02',
    name: 'Transformer 02',
    type: 'transformer',
    position: { e: 252, n: 76, u: 0 },
    footprint: { width: 12, depth: 8, height: 7 },
    heading: 0,
    criticality: 9,
    protectionZoneId: 'ZONE-CRIT-SUB',
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.94,
  },
  {
    id: 'CTL-01',
    name: 'Control Building',
    type: 'control_building',
    position: { e: 300, n: -140, u: 0 },
    footprint: { width: 30, depth: 17, height: 8.5 },
    heading: 0,
    criticality: 8,
    protectionZoneId: 'ZONE-CRIT-CTL',
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.64,
  },
  {
    id: 'BESS-01',
    name: 'Battery Storage 01',
    type: 'storage',
    position: { e: 200, n: -140, u: 0 },
    footprint: { width: 44, depth: 15, height: 3.2 },
    heading: 0,
    criticality: 7,
    protectionZoneId: 'ZONE-CRIT-CTL',
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.71,
  },
  {
    id: 'INV-01',
    name: 'Inverter Station 01',
    type: 'inverter',
    position: { e: -160, n: 176, u: 0 },
    footprint: { width: 14, depth: 6, height: 3.4 },
    heading: 0,
    criticality: 6,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.88,
  },
  {
    id: 'INV-02',
    name: 'Inverter Station 02',
    type: 'inverter',
    position: { e: -160, n: -196, u: 0 },
    footprint: { width: 14, depth: 6, height: 3.4 },
    heading: 0,
    criticality: 6,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.52,
  },
  {
    id: 'ARRAY-A',
    name: 'Array A — West',
    type: 'solar_array',
    position: { e: -352, n: -60, u: 0 },
    footprint: { width: 156, depth: 308, height: 3 },
    heading: 0,
    criticality: 4,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.66,
  },
  {
    id: 'ARRAY-B',
    name: 'Array B — Central',
    type: 'solar_array',
    position: { e: -172, n: -60, u: 0 },
    footprint: { width: 156, depth: 308, height: 3 },
    heading: 0,
    criticality: 4,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.79,
  },
  {
    id: 'ARRAY-C',
    name: 'Array C — East',
    type: 'solar_array',
    position: { e: 8, n: -60, u: 0 },
    footprint: { width: 156, depth: 308, height: 3 },
    heading: 0,
    criticality: 4,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.9,
  },
  {
    id: 'GATE-01',
    name: 'West Gate',
    type: 'gate',
    position: { e: -466, n: -6, u: 0 },
    footprint: { width: 6, depth: 16, height: 3 },
    heading: 0,
    criticality: 3,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0.58,
  },
];

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

const FULL_CIRCLE = Math.PI * 2;

const SENSORS: Sensor[] = [
  {
    id: 'RADAR-01',
    name: 'Radar 01 — North',
    type: 'radar',
    position: { e: 60, n: 306, u: 0 },
    mastHeight: 14,
    status: 'online',
    health: 0.98,
    effectiveness: 0.98,
    coverage: {
      minRange: 25,
      maxRange: 1800,
      bearing: 0,
      azimuthWidth: FULL_CIRCLE,
      minElevation: -2 * DEG,
      maxElevation: 35 * DEG,
    },
  },
  {
    id: 'RADAR-02',
    name: 'Radar 02 — South West',
    type: 'radar',
    position: { e: -392, n: -302, u: 0 },
    mastHeight: 14,
    status: 'online',
    health: 1,
    effectiveness: 1,
    coverage: {
      minRange: 25,
      maxRange: 1500,
      bearing: 0,
      azimuthWidth: FULL_CIRCLE,
      minElevation: -2 * DEG,
      maxElevation: 35 * DEG,
    },
  },
  {
    id: 'CAM-01',
    name: 'EO Camera 01 — Substation',
    type: 'camera',
    position: { e: 392, n: 45, u: 0 },
    mastHeight: 9,
    status: 'online',
    health: 0.94,
    effectiveness: 0.94,
    coverage: {
      minRange: 8,
      maxRange: 650,
      bearing: 270 * DEG,
      azimuthWidth: 62 * DEG,
      minElevation: -6 * DEG,
      maxElevation: 42 * DEG,
    },
  },
  {
    id: 'THERM-01',
    name: 'Thermal 01 — South East',
    type: 'thermal',
    position: { e: 356, n: -276, u: 0 },
    mastHeight: 9,
    status: 'online',
    health: 0.88,
    effectiveness: 0.88,
    coverage: {
      minRange: 8,
      maxRange: 480,
      bearing: 315 * DEG,
      azimuthWidth: 56 * DEG,
      minElevation: -6 * DEG,
      maxElevation: 40 * DEG,
    },
  },
  {
    id: 'RF-01',
    name: 'RF Sensor 01 — Control',
    type: 'rf',
    position: { e: 322, n: -170, u: 0 },
    mastHeight: 11,
    status: 'online',
    health: 0.96,
    effectiveness: 0.96,
    coverage: {
      minRange: 0,
      maxRange: 2400,
      bearing: 0,
      azimuthWidth: FULL_CIRCLE,
      minElevation: 0,
      maxElevation: 60 * DEG,
    },
  },
];

// ---------------------------------------------------------------------------
// Protected zones
// ---------------------------------------------------------------------------

/**
 * Three classes, three ceilings. A perimeter zone that stopped at the fence line would be
 * useless against something that flies, so every zone is a volume with real altitude.
 */
const ZONES: Zone[] = [
  {
    id: 'ZONE-PERIM',
    name: 'Site Perimeter',
    zoneClass: 'perimeter',
    polygon: BOUNDARY,
    floor: 0,
    ceiling: 80,
    emphasis: 0,
    intruderTrackIds: [],
    predictedIntruderTrackIds: [],
  },
  {
    id: 'ZONE-REST-EAST',
    name: 'Restricted — East Compound',
    zoneClass: 'restricted',
    polygon: rect(276, -40, 280, 320),
    floor: 0,
    ceiling: 105,
    emphasis: 0,
    intruderTrackIds: [],
    predictedIntruderTrackIds: [],
  },
  {
    id: 'ZONE-CRIT-SUB',
    name: 'Critical — Substation',
    zoneClass: 'critical',
    polygon: rect(292, 45, 150, 120),
    floor: 0,
    ceiling: 130,
    protectsAssetId: 'SUB-01',
    emphasis: 0,
    intruderTrackIds: [],
    predictedIntruderTrackIds: [],
  },
  {
    id: 'ZONE-CRIT-CTL',
    name: 'Critical — Control & Storage',
    zoneClass: 'critical',
    polygon: rect(252, -140, 160, 70),
    floor: 0,
    ceiling: 100,
    protectsAssetId: 'CTL-01',
    emphasis: 0,
    intruderTrackIds: [],
    predictedIntruderTrackIds: [],
  },
];

// ---------------------------------------------------------------------------
// Response resources — inventory only
// ---------------------------------------------------------------------------

const RESOURCES: ResponseResource[] = [
  {
    id: 'RES-01',
    name: 'Interceptor 01',
    kind: 'Interceptor UAS',
    position: { e: 330, n: -160, u: 0 },
    status: 'available',
    readiness: 0.94,
    batteryPercent: 87,
    communications: 'connected',
  },
  {
    id: 'RES-02',
    name: 'Interceptor 02',
    kind: 'Interceptor UAS',
    position: { e: 330, n: -176, u: 0 },
    status: 'available',
    readiness: 0.9,
    batteryPercent: 78,
    communications: 'connected',
  },
  {
    id: 'RES-03',
    name: 'Interceptor 03',
    kind: 'Interceptor UAS',
    position: { e: 330, n: -192, u: 0 },
    status: 'maintenance',
    readiness: 0.1,
    batteryPercent: 34,
    communications: 'connected',
    currentTask: 'Scheduled airframe inspection',
  },
  {
    id: 'RES-04',
    name: 'Patrol — West',
    kind: 'Ground patrol',
    position: { e: -300, n: -6, u: 0 },
    status: 'available',
    readiness: 0.8,
    communications: 'intermittent',
  },
  {
    id: 'RES-05',
    name: 'Patrol — East',
    kind: 'Ground patrol',
    position: { e: 240, n: 20, u: 0 },
    status: 'committed',
    readiness: 0.85,
    communications: 'connected',
    currentTask: 'Perimeter inspection, south fence',
  },
];

// ---------------------------------------------------------------------------

export const DEMO_SITE: Site = {
  id: 'SITE-01',
  name: 'Ardnacrusha Solar',
  kind: 'Utility-scale solar generation',
  // Anchored near Cork, matching the datum the rest of Aetos demonstrates against.
  datum: { latitude: 51.8985, longitude: -8.4756 },
  boundary: BOUNDARY,
  assets: ASSETS,
  sensors: SENSORS,
  zones: ZONES,
  resources: RESOURCES,
  roads: ROADS,
  arrays: ARRAYS,
};

/** Half-extent of the site, metres. Used for camera limits and grid sizing. */
export const SITE_EXTENT = 520;
