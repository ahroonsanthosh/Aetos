import { describe, expect, it, vi } from 'vitest';
import { geocode, parseGeocodeResponse } from './geocode';

/** Shape of a real Nominatim jsonv2 response, trimmed to the fields the parser reads. */
const MONEYPOINT = {
  display_name: 'Moneypoint Power Station, County Clare, Munster, Ireland',
  lat: '52.6086',
  lon: '-9.4008',
  boundingbox: ['52.6036', '52.6136', '-9.4108', '-9.3908'],
};

describe('parseGeocodeResponse', () => {
  it('reads label and coordinates from a result', () => {
    const [result] = parseGeocodeResponse([MONEYPOINT]);
    expect(result!.label).toContain('Moneypoint');
    expect(result!.latitude).toBeCloseTo(52.6086, 4);
    expect(result!.longitude).toBeCloseTo(-9.4008, 4);
  });

  it('derives a radius from the bounding box rather than using a fixed one', () => {
    const [big] = parseGeocodeResponse([MONEYPOINT]);
    const [small] = parseGeocodeResponse([
      { ...MONEYPOINT, boundingbox: ['52.6085', '52.6087', '-9.4009', '-9.4007'] },
    ]);
    expect(big!.suggestedRadius).toBeGreaterThan(small!.suggestedRadius);
  });

  it('clamps the radius so a country-sized result cannot request the world', () => {
    const [huge] = parseGeocodeResponse([
      { ...MONEYPOINT, boundingbox: ['51.0', '55.0', '-11.0', '-5.0'] },
    ]);
    expect(huge!.suggestedRadius).toBeLessThanOrEqual(3000);

    const [tiny] = parseGeocodeResponse([
      { ...MONEYPOINT, boundingbox: ['52.60860', '52.60861', '-9.40081', '-9.40080'] },
    ]);
    expect(tiny!.suggestedRadius).toBeGreaterThanOrEqual(120);
  });

  it('falls back to a default radius when the bounding box is missing or malformed', () => {
    const [noBox] = parseGeocodeResponse([{ ...MONEYPOINT, boundingbox: undefined }]);
    expect(noBox!.suggestedRadius).toBeGreaterThan(0);

    const [badBox] = parseGeocodeResponse([{ ...MONEYPOINT, boundingbox: ['a', 'b', 'c', 'd'] }]);
    expect(badBox!.suggestedRadius).toBeGreaterThan(0);
  });

  it('skips entries with unusable coordinates instead of emitting NaN results', () => {
    const results = parseGeocodeResponse([
      { ...MONEYPOINT, lat: 'not-a-number' },
      MONEYPOINT,
      { ...MONEYPOINT, lon: undefined },
    ]);
    expect(results).toHaveLength(1);
  });

  it('returns nothing for a non-array body', () => {
    expect(parseGeocodeResponse({ error: 'nope' })).toEqual([]);
    expect(parseGeocodeResponse(null)).toEqual([]);
  });

  it('falls back to coordinates as the label when display_name is absent', () => {
    const [result] = parseGeocodeResponse([{ ...MONEYPOINT, display_name: undefined }]);
    expect(result!.label).toContain('52.6086');
  });
});

describe('geocode', () => {
  it('returns nothing for an empty query without making a request', async () => {
    const fetchImpl = vi.fn();
    await expect(geocode('   ', { fetchImpl: fetchImpl as unknown as typeof fetch })).resolves.toEqual([]);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('sends the query and requested limit to the endpoint', async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      expect(url).toContain('q=Moneypoint');
      expect(url).toContain('limit=3');
      return { ok: true, json: async () => [MONEYPOINT] } as unknown as Response;
    });

    const results = await geocode('Moneypoint', {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      limit: 3,
    });

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(results).toHaveLength(1);
  });

  it('raises a clear error when the geocoder rejects the request', async () => {
    const fetchImpl = vi.fn(
      async () => ({ ok: false, status: 429, statusText: 'Too Many Requests' }) as unknown as Response,
    );
    await expect(
      geocode('anywhere', { fetchImpl: fetchImpl as unknown as typeof fetch }),
    ).rejects.toThrow(/429/);
  });
});
