/**
 * Importing real buildings from OpenStreetMap.
 *
 * This is the honest version of "pull it from the map". Google's satellite tiles and Places
 * data need a paid API key this application does not have and should not fabricate calls
 * against; OSM's Overpass API is free, keyless, and — for anywhere a mapper has traced it —
 * carries real building footprints as polygons, not just a marker. For a solar farm, a
 * substation compound, most industrial sites, that is usually enough to skip tracing
 * entirely.
 *
 * It is not always enough. OSM coverage is patchy outside urban areas, and a building with
 * no tags becomes a generic massing box with a guessed height — see `overpassToAssets`. The
 * image-tracing tool exists for exactly the gap this leaves.
 *
 * The network call (`fetchOsmSiteData`) is a thin wrapper around two pure functions —
 * `buildOverpassQuery` and `overpassToAssets` — so the only untestable part of this module is
 * the fetch itself.
 */

import type { Asset, Ground } from '@/types';
import { boundingBoxAround, haversineMetres, latLonToEnu, type LatLon } from './geo';
import { suggestBoundary } from './hull';

const OVERPASS_ENDPOINT = 'https://overpass-api.de/api/interpreter';

/** Overpass QL for every building way (and the multipolygon relations that stand in for
 *  complex ones) inside the box, with tags — `out geom` returns each way's own node
 *  coordinates directly, avoiding a second round trip to resolve node IDs. */
export function buildOverpassQuery(box: { south: number; west: number; north: number; east: number }): string {
  const bbox = `${box.south},${box.west},${box.north},${box.east}`;
  return `[out:json][timeout:25];(way["building"](${bbox});relation["building"](${bbox}););out geom;`;
}

interface OverpassElement {
  type: 'way' | 'relation' | 'node';
  id: number;
  tags?: Record<string, string>;
  geometry?: { lat: number; lon: number }[];
  // Relations carry per-member geometry rather than a flat list; only the outer ring is used.
  members?: { type: string; role: string; geometry?: { lat: number; lon: number }[] }[];
}

interface OverpassResponse {
  elements: OverpassElement[];
}

export interface OsmImportResult {
  assets: Asset[];
  /** Convex hull of every imported footprint, buffered outward — a starting boundary, not
   *  a final one. */
  suggestedBoundary: Ground[];
  /** Elements Overpass returned with no usable ring, so the operator can see what was
   *  skipped rather than silently losing count. */
  skipped: number;
}

/** Metres per building level, when only `building:levels` is tagged rather than `height`. */
const METRES_PER_LEVEL = 3;
const DEFAULT_HEIGHT = 6;
const BOUNDARY_MARGIN = 25;

/** OSM's `building=*` value, where it maps cleanly onto one of Aetos's known types. */
const BUILDING_TAG_TO_ASSET_TYPE: Record<string, string> = {
  transformer_tower: 'transformer',
  substation: 'substation',
  industrial: 'building',
  warehouse: 'building',
  service: 'building',
};

function parseHeight(tags: Record<string, string> | undefined): number {
  if (!tags) return DEFAULT_HEIGHT;

  const height = tags.height ?? tags['building:height'];
  if (height) {
    const metres = Number.parseFloat(height);
    if (Number.isFinite(metres) && metres > 0) return metres;
  }

  const levels = tags['building:levels'];
  if (levels) {
    const count = Number.parseFloat(levels);
    if (Number.isFinite(count) && count > 0) return count * METRES_PER_LEVEL;
  }

  return DEFAULT_HEIGHT;
}

function assetTypeFor(tags: Record<string, string> | undefined): string {
  if (!tags) return 'building';
  if (tags.power === 'substation') return 'substation';
  const building = tags.building;
  if (building && building !== 'yes' && BUILDING_TAG_TO_ASSET_TYPE[building]) {
    return BUILDING_TAG_TO_ASSET_TYPE[building]!;
  }
  return 'building';
}

/** Outer ring of one element, as lat/lon, or undefined if Overpass gave nothing usable. */
function outerRing(element: OverpassElement): { lat: number; lon: number }[] | undefined {
  if (element.geometry && element.geometry.length >= 3) return element.geometry;

  const outer = element.members?.find((m) => m.role === 'outer' && m.geometry);
  if (outer?.geometry && outer.geometry.length >= 3) return outer.geometry;

  return undefined;
}

function centroidOf(polygon: Ground[]): Ground {
  return polygon.reduce(
    (acc, p) => ({ e: acc.e + p.e / polygon.length, n: acc.n + p.n / polygon.length }),
    { e: 0, n: 0 },
  );
}

/**
 * Turn a raw Overpass response into draft assets, purely — no network, no side effects, so
 * it can be tested against a fixed fixture and trusted not to drift from what a live query
 * actually returns.
 */
export function overpassToAssets(response: OverpassResponse, datum: LatLon): OsmImportResult {
  const assets: Asset[] = [];
  const allPoints: Ground[] = [];
  let skipped = 0;

  for (const element of response.elements) {
    const ring = outerRing(element);
    if (!ring) {
      skipped += 1;
      continue;
    }

    // A closed ring repeats its first node as its last; drop the duplicate before it
    // becomes a zero-length polygon edge.
    const points = ring.map((n) => latLonToEnu({ latitude: n.lat, longitude: n.lon }, datum));
    const closed =
      points.length > 1 &&
      Math.abs(points[0]!.e - points[points.length - 1]!.e) < 1e-6 &&
      Math.abs(points[0]!.n - points[points.length - 1]!.n) < 1e-6
        ? points.slice(0, -1)
        : points;

    if (closed.length < 3) {
      skipped += 1;
      continue;
    }

    allPoints.push(...closed);

    const centroid = centroidOf(closed);
    const es = closed.map((p) => p.e);
    const ns = closed.map((p) => p.n);
    const width = Math.max(...es) - Math.min(...es);
    const depth = Math.max(...ns) - Math.min(...ns);
    const height = parseHeight(element.tags);
    const name = element.tags?.name ?? `Building ${element.id}`;

    assets.push({
      id: `osm-${element.type}-${element.id}`,
      name,
      type: assetTypeFor(element.tags),
      position: { e: centroid.e, n: centroid.n, u: 0 },
      footprint: { width: Math.max(width, 1), depth: Math.max(depth, 1), height },
      footprintPolygon: closed.map((p) => ({ e: p.e - centroid.e, n: p.n - centroid.n })),
      heading: 0,
      criticality: 5,
      status: 'operational',
      sensorsCovering: [],
      activeThreats: [],
      protectionScore: 0,
    });
  }

  return {
    assets,
    suggestedBoundary: allPoints.length >= 3 ? suggestBoundary(allPoints, BOUNDARY_MARGIN) : [],
    skipped,
  };
}

export interface OsmFetchOptions {
  /** Override for tests; defaults to the public Overpass instance. */
  endpoint?: string;
  fetchImpl?: typeof fetch;
}

/**
 * Fetch real building footprints around a point and convert them to draft assets.
 *
 * Runs in the browser, at the point the operator clicks "Import from OpenStreetMap" — never
 * from anywhere that isn't a live user action, and never silently in the background: OSM's
 * usage policy expects Overpass calls to be deliberate, not automatic or repeated.
 */
export async function fetchOsmSiteData(
  centre: LatLon,
  radiusMetres: number,
  options: OsmFetchOptions = {},
): Promise<OsmImportResult> {
  const endpoint = options.endpoint ?? OVERPASS_ENDPOINT;
  const doFetch = options.fetchImpl ?? fetch;

  const query = buildOverpassQuery(boundingBoxAround(centre, radiusMetres));
  const response = await doFetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: query,
  });

  if (!response.ok) {
    throw new Error(`Overpass request failed: ${response.status} ${response.statusText}`);
  }

  const body = (await response.json()) as OverpassResponse;
  return overpassToAssets(body, centre);
}

/** Radius, metres, that comfortably covers most single-facility imports without over-fetching. */
export const DEFAULT_IMPORT_RADIUS_M = 400;

export { haversineMetres };
