/**
 * The site file format.
 *
 * `types/index.ts` is the contract inside the application — everything there is trusted,
 * because everything there came from code that already validated it. A site loaded from a
 * file did not: it may be hand-edited JSON, an OSM import with a missing tag, or a traced
 * polygon exported from the editor with a typo in a criticality field. This schema is the
 * checkpoint between those two worlds. `parseSiteFile` is the only way untrusted JSON
 * becomes a `Site`, and it fails loudly, with a path to the offending field, rather than
 * letting a malformed asset reach the renderer as a silent `undefined`.
 */

import { z } from 'zod';
import type { Site } from '@/types';

const groundSchema = z.object({ e: z.number(), n: z.number() });
const enuSchema = z.object({ e: z.number(), n: z.number(), u: z.number() });

/** At least a triangle. A one- or two-point "polygon" is a bug in whatever produced it. */
const polygonSchema = z.array(groundSchema).min(3);

const footprintSchema = z.object({
  width: z.number().positive(),
  depth: z.number().positive(),
  height: z.number().positive(),
});

const assetSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  // Deliberately z.string(), not a closed enum — see types/index.ts. Validation's job is
  // to catch a missing or malformed field, not to reject a building type Aetos has not
  // been told how to model yet.
  type: z.string().min(1),
  position: enuSchema,
  footprint: footprintSchema,
  footprintPolygon: polygonSchema.optional(),
  heading: z.number(),
  criticality: z.number().int().min(1).max(10),
  protectionZoneId: z.string().optional(),
  status: z.enum(['operational', 'degraded', 'offline', 'maintenance']).default('operational'),
  sensorsCovering: z.array(z.string()).default([]),
  activeThreats: z.array(z.string()).default([]),
  protectionScore: z.number().min(0).max(1).default(0),
});

const zoneSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  zoneClass: z.enum(['perimeter', 'restricted', 'critical']),
  polygon: polygonSchema,
  floor: z.number().min(0),
  ceiling: z.number(),
  protectsAssetId: z.string().optional(),
  emphasis: z.number().min(0).max(1).default(0),
  intruderTrackIds: z.array(z.string()).default([]),
  predictedIntruderTrackIds: z.array(z.string()).default([]),
});

const sensorCoverageSchema = z.object({
  minRange: z.number().min(0),
  maxRange: z.number().positive(),
  bearing: z.number(),
  azimuthWidth: z.number().min(0).max(Math.PI * 2 + 1e-6),
  minElevation: z.number(),
  maxElevation: z.number(),
});

const sensorSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  type: z.enum(['radar', 'camera', 'thermal', 'rf', 'acoustic']),
  position: enuSchema,
  mastHeight: z.number().min(0),
  status: z.enum(['online', 'degraded', 'offline']).default('online'),
  health: z.number().min(0).max(1).default(1),
  effectiveness: z.number().min(0).max(1).default(1),
  coverage: sensorCoverageSchema,
  degradationReason: z.string().optional(),
});

const resourceSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  kind: z.string().min(1),
  position: enuSchema,
  status: z.enum(['available', 'committed', 'unavailable', 'maintenance']).default('available'),
  readiness: z.number().min(0).max(1).default(1),
  batteryPercent: z.number().min(0).max(100).optional(),
  communications: z.enum(['connected', 'intermittent', 'lost']).default('connected'),
  currentTask: z.string().optional(),
});

const solarArrayBlockSchema = z.object({
  id: z.string().min(1),
  assetId: z.string().min(1),
  origin: groundSchema,
  heading: z.number(),
  rows: z.number().int().positive(),
  tablesPerRow: z.number().int().positive(),
  rowPitch: z.number().positive(),
  tablePitch: z.number().positive(),
});

export const siteSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  kind: z.string().default('Site'),
  datum: z.object({ latitude: z.number().min(-90).max(90), longitude: z.number().min(-180).max(180) }),
  boundary: polygonSchema,
  assets: z.array(assetSchema).default([]),
  sensors: z.array(sensorSchema).default([]),
  zones: z.array(zoneSchema).default([]),
  resources: z.array(resourceSchema).default([]),
  roads: z.array(z.array(groundSchema).min(2)).default([]),
  arrays: z.array(solarArrayBlockSchema).default([]),
});

export type SiteFile = z.infer<typeof siteSchema>;

/**
 * The same shape, with the boundary allowed to be incomplete.
 *
 * A finished site must have a real boundary — it is the perimeter zone, the import filter,
 * and what the camera frames — so `siteSchema` requires three points and export enforces
 * that. A site being *edited* legitimately has none yet: tracing a building before drawing
 * the fence line is a normal order of work.
 *
 * Validating a draft against the strict schema was a real bug, not a theoretical one: the
 * editor's autosave wrote a perfectly good draft, the restore rejected it for having no
 * boundary, and the catch-all cleanup then deleted it. Tracing a building, looking at it in
 * the command view and coming back lost the work every time.
 */
export const draftSiteSchema = siteSchema.extend({
  boundary: z.array(groundSchema),
});

/** Parse a work-in-progress draft. Throws `SiteFileError` on anything genuinely malformed. */
export function parseDraftSite(json: unknown): Site {
  const result = draftSiteSchema.safeParse(json);
  if (!result.success) {
    const issues = result.error.issues.map((issue) => ({
      path: issue.path.join('.') || '(root)',
      message: issue.message,
    }));
    throw new SiteFileError(
      `Draft has ${issues.length} problem${issues.length === 1 ? '' : 's'}: ` +
        issues.map((i) => `${i.path} — ${i.message}`).join('; '),
      issues,
    );
  }
  return result.data as Site;
}

export class SiteFileError extends Error {
  constructor(
    message: string,
    readonly issues: { path: string; message: string }[],
  ) {
    super(message);
    this.name = 'SiteFileError';
  }
}

/**
 * Parse and validate a site file. Throws `SiteFileError` with every problem found — a site
 * editor showing "one field was wrong, try again" ten times in a row is worse than showing
 * the whole list once.
 */
export function parseSiteFile(json: unknown): Site {
  const result = siteSchema.safeParse(json);
  if (!result.success) {
    const issues = result.error.issues.map((issue) => ({
      path: issue.path.join('.') || '(root)',
      message: issue.message,
    }));
    throw new SiteFileError(
      `Site file has ${issues.length} problem${issues.length === 1 ? '' : 's'}: ` +
        issues.map((i) => `${i.path} — ${i.message}`).join('; '),
      issues,
    );
  }
  return result.data as Site;
}

/** Parse from a JSON string, wrapping a syntax error in the same `SiteFileError` shape. */
export function parseSiteFileText(text: string): Site {
  let json: unknown;
  try {
    json = JSON.parse(text);
  } catch (error) {
    throw new SiteFileError(`Not valid JSON: ${(error as Error).message}`, [
      { path: '(root)', message: 'malformed JSON' },
    ]);
  }
  return parseSiteFile(json);
}

/** Serialise a site back to the file format. Stable key order, so diffs stay readable. */
export function serialiseSite(site: Site): string {
  return JSON.stringify(site, null, 2);
}
