/**
 * Draft persistence tests.
 *
 * The case that matters most is the one that was broken: a draft with traced buildings but
 * no boundary yet must round-trip. Validating that against the finished-site schema
 * rejected it and then deleted it, so tracing a building, looking at it in the command view
 * and coming back lost the work every time.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Asset, Site } from '@/types';
import { clearDraft, draftIsWorthKeeping, loadDraft, saveDraft } from './draftStore';

/** Minimal in-memory localStorage, since these tests run in node. */
function installStorage() {
  const store = new Map<string, string>();
  const mock = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  };
  vi.stubGlobal('window', { localStorage: mock });
  return store;
}

function asset(overrides: Partial<Asset> = {}): Asset {
  return {
    id: 'A1',
    name: 'Traced Building',
    type: 'building',
    position: { e: 12, n: -8, u: 0 },
    footprint: { width: 20, depth: 14, height: 6 },
    footprintPolygon: [
      { e: -10, n: -7 },
      { e: 10, n: -7 },
      { e: 10, n: 7 },
      { e: -10, n: 7 },
    ],
    heading: 0,
    criticality: 5,
    status: 'operational',
    sensorsCovering: [],
    activeThreats: [],
    protectionScore: 0,
    ...overrides,
  };
}

function draft(overrides: Partial<Site> = {}): Site {
  return {
    id: 'SITE-DRAFT',
    name: 'New Site',
    kind: 'Site',
    datum: { latitude: 0, longitude: 0 },
    boundary: [],
    assets: [],
    sensors: [],
    zones: [],
    resources: [],
    roads: [],
    arrays: [],
    ...overrides,
  };
}

beforeEach(() => {
  installStorage();
});

describe('saveDraft / loadDraft', () => {
  it('round-trips a draft that has buildings but no boundary yet', () => {
    // The exact shape that used to be rejected and then deleted.
    const original = draft({ assets: [asset()] });
    saveDraft(original);

    const restored = loadDraft();
    expect(restored).not.toBeNull();
    expect(restored!.site.assets).toHaveLength(1);
    expect(restored!.site.assets[0]!.name).toBe('Traced Building');
    expect(restored!.site.boundary).toHaveLength(0);
  });

  it('preserves a traced footprint polygon exactly', () => {
    saveDraft(draft({ assets: [asset()] }));
    const restored = loadDraft();
    expect(restored!.site.assets[0]!.footprintPolygon).toEqual(asset().footprintPolygon);
  });

  it('does not delete a boundary-less draft while trying to read it', () => {
    saveDraft(draft({ assets: [asset()] }));
    loadDraft();
    // A second read must still find it — the old code cleared it on the first failure.
    expect(loadDraft()).not.toBeNull();
  });

  it('round-trips a complete site with a boundary too', () => {
    const complete = draft({
      assets: [asset()],
      boundary: [
        { e: -50, n: -50 },
        { e: 50, n: -50 },
        { e: 50, n: 50 },
      ],
      datum: { latitude: 52.6, longitude: -9.4 },
    });
    saveDraft(complete);

    const restored = loadDraft()!;
    expect(restored.site.boundary).toHaveLength(3);
    expect(restored.site.datum.latitude).toBeCloseTo(52.6, 6);
  });

  it('records when it was saved', () => {
    const before = Date.now();
    saveDraft(draft({ assets: [asset()] }));
    expect(loadDraft()!.savedAt).toBeGreaterThanOrEqual(before);
  });

  it('returns null when nothing has been saved', () => {
    expect(loadDraft()).toBeNull();
  });

  it('discards a genuinely malformed payload rather than throwing', () => {
    window.localStorage.setItem('aetos-twin-editor-draft', '{ not json');
    expect(loadDraft()).toBeNull();
  });

  it('discards a draft whose assets no longer satisfy the schema', () => {
    window.localStorage.setItem(
      'aetos-twin-editor-draft',
      JSON.stringify({ site: { ...draft(), assets: [{ id: 'broken' }] }, savedAt: Date.now() }),
    );
    expect(loadDraft()).toBeNull();
  });
});

describe('clearDraft', () => {
  it('removes a saved draft', () => {
    saveDraft(draft({ assets: [asset()] }));
    expect(loadDraft()).not.toBeNull();
    clearDraft();
    expect(loadDraft()).toBeNull();
  });
});

describe('draftIsWorthKeeping', () => {
  it('is false for an untouched empty draft', () => {
    expect(draftIsWorthKeeping(draft())).toBe(false);
  });

  it('is true once anything has been traced, drawn or generated', () => {
    expect(draftIsWorthKeeping(draft({ assets: [asset()] }))).toBe(true);
    expect(
      draftIsWorthKeeping(
        draft({ boundary: [{ e: 0, n: 0 }, { e: 1, n: 0 }, { e: 1, n: 1 }] }),
      ),
    ).toBe(true);
  });
});
