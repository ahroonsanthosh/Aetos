/**
 * Turning a place name into coordinates.
 *
 * The single biggest thing missing from the first version of the editor: it asked for a
 * decimal latitude and longitude, which nobody knows for their own site, and defaulted
 * those fields to a real inhabited location — so pressing Import without editing them
 * produced a neighbourhood of unrelated houses. Searching by name removes both problems at
 * once.
 *
 * Nominatim is OpenStreetMap's own geocoder: free, keyless, and the natural counterpart to
 * the Overpass API already used for footprints. It is *not* Google Places, which needs a
 * paid key this project does not have. Its usage policy allows at most one request per
 * second and requires an identifying User-Agent — both respected below, and both reasons
 * this is wired to an explicit search button rather than to keystrokes.
 */

const NOMINATIM_ENDPOINT = 'https://nominatim.openstreetmap.org/search';

export interface GeocodeResult {
  /** Full display name, e.g. "Moneypoint Power Station, County Clare, Ireland". */
  label: string;
  latitude: number;
  longitude: number;
  /**
   * Suggested import radius, metres, derived from the result's own bounding box — a power
   * station and a postbox should not both default to 400 m.
   */
  suggestedRadius: number;
}

interface NominatimItem {
  display_name?: string;
  lat?: string;
  lon?: string;
  /** [south, north, west, east] as strings. */
  boundingbox?: string[];
}

/** Radius bounds, metres. Below the floor an import returns nothing useful; above the
 *  ceiling it pulls in a whole town. */
const MIN_RADIUS = 120;
const MAX_RADIUS = 3000;

/**
 * Parse a Nominatim response into results. Pure, so the shape of a real response can be
 * pinned by a fixture test without a network call.
 */
export function parseGeocodeResponse(body: unknown): GeocodeResult[] {
  if (!Array.isArray(body)) return [];

  const results: GeocodeResult[] = [];

  for (const raw of body as NominatimItem[]) {
    const latitude = Number.parseFloat(raw?.lat ?? '');
    const longitude = Number.parseFloat(raw?.lon ?? '');
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) continue;

    results.push({
      label: raw.display_name ?? `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`,
      latitude,
      longitude,
      suggestedRadius: radiusFromBoundingBox(raw.boundingbox, latitude),
    });
  }

  return results;
}

/**
 * Half the diagonal of the result's bounding box, clamped — enough to cover the thing that
 * was found, without reaching far past it.
 */
function radiusFromBoundingBox(box: string[] | undefined, latitude: number): number {
  if (!box || box.length < 4) return 400;

  const [south, north, west, east] = box.map((v) => Number.parseFloat(v));
  if (![south, north, west, east].every((v) => Number.isFinite(v))) return 400;

  const metresPerDegreeLat = 111_320;
  const metresPerDegreeLon = metresPerDegreeLat * Math.cos((latitude * Math.PI) / 180);

  const heightM = Math.abs(north! - south!) * metresPerDegreeLat;
  const widthM = Math.abs(east! - west!) * metresPerDegreeLon;

  const radius = Math.hypot(widthM, heightM) / 2;
  return Math.round(Math.max(MIN_RADIUS, Math.min(MAX_RADIUS, radius)));
}

export interface GeocodeOptions {
  endpoint?: string;
  fetchImpl?: typeof fetch;
  limit?: number;
}

/**
 * Search for a place by name. Runs only on an explicit user action — never per keystroke,
 * per Nominatim's rate policy.
 */
export async function geocode(query: string, options: GeocodeOptions = {}): Promise<GeocodeResult[]> {
  const trimmed = query.trim();
  if (trimmed.length === 0) return [];

  const endpoint = options.endpoint ?? NOMINATIM_ENDPOINT;
  const doFetch = options.fetchImpl ?? fetch;

  const url = new URL(endpoint);
  url.searchParams.set('q', trimmed);
  url.searchParams.set('format', 'jsonv2');
  url.searchParams.set('limit', String(options.limit ?? 5));
  url.searchParams.set('addressdetails', '0');

  const response = await doFetch(url.toString(), {
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`Place search failed: ${response.status} ${response.statusText}`);
  }

  return parseGeocodeResponse(await response.json());
}
