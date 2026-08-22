import { describe, expect, it } from 'vitest';
import { DEMO_SITE } from './demo-site';
import {
  parseFacadeDesignText,
  parseSiteFile,
  parseSiteFileText,
  SiteFileError,
  serialiseSite,
} from './schema';

describe('parseSiteFile', () => {
  it('accepts the demo site unchanged', () => {
    const parsed = parseSiteFile(JSON.parse(serialiseSite(DEMO_SITE)));
    expect(parsed.id).toBe(DEMO_SITE.id);
    expect(parsed.assets).toHaveLength(DEMO_SITE.assets.length);
    expect(parsed.sensors).toHaveLength(DEMO_SITE.sensors.length);
  });

  it('accepts an asset type outside the seven built-in ones', () => {
    const site = minimalSite({ assets: [minimalAsset({ type: 'grain_silo' })] });
    const parsed = parseSiteFile(site);
    expect(parsed.assets[0]!.type).toBe('grain_silo');
  });

  it('accepts a footprintPolygon on an asset', () => {
    const site = minimalSite({
      assets: [
        minimalAsset({
          footprintPolygon: [
            { e: -5, n: -5 },
            { e: 5, n: -5 },
            { e: 5, n: 5 },
          ],
        }),
      ],
    });
    const parsed = parseSiteFile(site);
    expect(parsed.assets[0]!.footprintPolygon).toHaveLength(3);
  });

  it('fills in defaults for optional operational fields', () => {
    const site = minimalSite({ assets: [minimalAsset({})] });
    const parsed = parseSiteFile(site);
    expect(parsed.assets[0]!.status).toBe('operational');
    expect(parsed.assets[0]!.sensorsCovering).toEqual([]);
  });

  it('rejects a criticality outside 1–10', () => {
    const site = minimalSite({ assets: [minimalAsset({ criticality: 11 })] });
    expect(() => parseSiteFile(site)).toThrow(SiteFileError);
  });

  it('rejects a polygon with fewer than three points', () => {
    const site = minimalSite({ boundary: [{ e: 0, n: 0 }, { e: 1, n: 1 }] });
    expect(() => parseSiteFile(site)).toThrow(SiteFileError);
  });

  it('rejects a datum outside valid lat/lon range', () => {
    const site = minimalSite({ datum: { latitude: 200, longitude: 0 } });
    expect(() => parseSiteFile(site)).toThrow(SiteFileError);
  });

  it('reports every problem, not just the first', () => {
    const site = minimalSite({
      assets: [minimalAsset({ criticality: 99 })],
      datum: { latitude: 999, longitude: 0 },
    });
    try {
      parseSiteFile(site);
      expect.unreachable();
    } catch (error) {
      expect(error).toBeInstanceOf(SiteFileError);
      expect((error as SiteFileError).issues.length).toBeGreaterThanOrEqual(2);
    }
  });

  it('rejects a sensor with an unknown type', () => {
    const site = minimalSite({
      sensors: [
        {
          id: 'S1',
          name: 'Sensor',
          type: 'lidar',
          position: { e: 0, n: 0, u: 0 },
          mastHeight: 5,
          coverage: {
            minRange: 0,
            maxRange: 100,
            bearing: 0,
            azimuthWidth: 1,
            minElevation: 0,
            maxElevation: 1,
          },
        },
      ],
    });
    expect(() => parseSiteFile(site)).toThrow(SiteFileError);
  });
});

describe('parseSiteFileText', () => {
  it('parses valid JSON text', () => {
    const parsed = parseSiteFileText(serialiseSite(DEMO_SITE));
    expect(parsed.id).toBe(DEMO_SITE.id);
  });

  it('reports malformed JSON without throwing a raw SyntaxError', () => {
    expect(() => parseSiteFileText('{ not json')).toThrow(SiteFileError);
  });
});

describe('serialiseSite / parseSiteFile round trip', () => {
  it('recovers an equivalent site', () => {
    const text = serialiseSite(DEMO_SITE);
    const parsed = parseSiteFileText(text);
    expect(parsed.assets.map((a) => a.id)).toEqual(DEMO_SITE.assets.map((a) => a.id));
    expect(parsed.zones.map((z) => z.id)).toEqual(DEMO_SITE.zones.map((z) => z.id));
  });
});

describe('parseFacadeDesignText', () => {
  const valid = JSON.stringify({
    walls: {
      north: { windows: [{ x: 0.1, y: 0.3, width: 0.08, height: 0.15 }], doors: [] },
    },
  });

  it('parses a bare JSON object', () => {
    const facade = parseFacadeDesignText(valid);
    expect(facade.walls.north?.windows).toHaveLength(1);
  });

  it('strips a markdown code fence and any surrounding prose', () => {
    const wrapped = `Here you go:\n\n\`\`\`json\n${valid}\n\`\`\`\n\nLet me know if you want changes.`;
    const facade = parseFacadeDesignText(wrapped);
    expect(facade.walls.north?.windows).toHaveLength(1);
  });

  it('leaves unmentioned sides absent rather than defaulting them to empty walls', () => {
    const facade = parseFacadeDesignText(valid);
    expect(facade.walls.south).toBeUndefined();
    expect(facade.walls.east).toBeUndefined();
    expect(facade.walls.west).toBeUndefined();
  });

  it('rejects a fraction outside [0, 1] rather than silently clamping it', () => {
    const bad = JSON.stringify({ walls: { north: { windows: [{ x: 1.4, y: 0, width: 0.1, height: 0.1 }] } } });
    expect(() => parseFacadeDesignText(bad)).toThrow(SiteFileError);
  });

  it('reports text with no JSON object at all, rather than throwing a raw parse error', () => {
    expect(() => parseFacadeDesignText('Sorry, I could not analyse that image.')).toThrow(SiteFileError);
  });

  it('reports malformed JSON without throwing a raw SyntaxError', () => {
    expect(() => parseFacadeDesignText('{ walls: not json }')).toThrow(SiteFileError);
  });
});

// ---------------------------------------------------------------------------

function minimalAsset(overrides: Record<string, unknown>) {
  return {
    id: 'A1',
    name: 'Asset',
    type: 'building',
    position: { e: 0, n: 0, u: 0 },
    footprint: { width: 10, depth: 10, height: 5 },
    heading: 0,
    criticality: 5,
    ...overrides,
  };
}

function minimalSite(overrides: Record<string, unknown>) {
  return {
    id: 'SITE-TEST',
    name: 'Test Site',
    datum: { latitude: 51.9, longitude: -8.5 },
    boundary: [
      { e: -50, n: -50 },
      { e: 50, n: -50 },
      { e: 50, n: 50 },
      { e: -50, n: 50 },
    ],
    assets: [],
    ...overrides,
  };
}
