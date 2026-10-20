import { describe, expect, it } from 'vitest';
import {
  boundingBoxAround,
  enuToLatLon,
  haversineMetres,
  latLonToEnu,
  metresPerDegreeLatitude,
  metresPerDegreeLongitude,
} from './geo';

const CORK = { latitude: 51.8985, longitude: -8.4756 };

describe('metresPerDegreeLatitude', () => {
  it('is close to the textbook ~111.32 km/degree', () => {
    expect(metresPerDegreeLatitude() / 1000).toBeCloseTo(111.19, 0);
  });
});

describe('metresPerDegreeLongitude', () => {
  it('shrinks with latitude, and vanishes at the pole', () => {
    expect(metresPerDegreeLongitude(0)).toBeCloseTo(metresPerDegreeLatitude(), -1);
    expect(metresPerDegreeLongitude(90)).toBeCloseTo(0, 6);
    expect(metresPerDegreeLongitude(51.9)).toBeLessThan(metresPerDegreeLongitude(0));
  });
});

describe('latLonToEnu / enuToLatLon', () => {
  it('places the datum itself at the origin', () => {
    const g = latLonToEnu(CORK, CORK);
    expect(g.e).toBeCloseTo(0, 9);
    expect(g.n).toBeCloseTo(0, 9);
  });

  it('moves north for increasing latitude', () => {
    const g = latLonToEnu({ latitude: CORK.latitude + 0.001, longitude: CORK.longitude }, CORK);
    expect(g.n).toBeGreaterThan(0);
    expect(g.e).toBeCloseTo(0, 6);
    // ~111 m per 0.001°.
    expect(g.n).toBeCloseTo(111.2, 0);
  });

  it('moves east for increasing longitude', () => {
    const g = latLonToEnu({ latitude: CORK.latitude, longitude: CORK.longitude + 0.001 }, CORK);
    expect(g.e).toBeGreaterThan(0);
    expect(g.n).toBeCloseTo(0, 6);
  });

  it('round-trips through enuToLatLon', () => {
    const point = { latitude: CORK.latitude + 0.004, longitude: CORK.longitude - 0.006 };
    const ground = latLonToEnu(point, CORK);
    const back = enuToLatLon(ground, CORK);
    expect(back.latitude).toBeCloseTo(point.latitude, 9);
    expect(back.longitude).toBeCloseTo(point.longitude, 9);
  });
});

describe('boundingBoxAround', () => {
  it('produces a box that encloses points at the requested radius', () => {
    const box = boundingBoxAround(CORK, 500);
    expect(box.south).toBeLessThan(CORK.latitude);
    expect(box.north).toBeGreaterThan(CORK.latitude);
    expect(box.west).toBeLessThan(CORK.longitude);
    expect(box.east).toBeGreaterThan(CORK.longitude);

    // A point 500 m north should fall at or inside the north edge.
    const north500 = enuToLatLon({ e: 0, n: 500 }, CORK);
    expect(north500.latitude).toBeLessThanOrEqual(box.north + 1e-9);
  });
});

describe('haversineMetres', () => {
  it('is zero for the same point', () => {
    expect(haversineMetres(CORK, CORK)).toBeCloseTo(0, 6);
  });

  it('agrees with latLonToEnu at short range', () => {
    const point = { latitude: CORK.latitude + 0.002, longitude: CORK.longitude + 0.001 };
    const ground = latLonToEnu(point, CORK);
    const straightLine = Math.hypot(ground.e, ground.n);
    expect(haversineMetres(CORK, point)).toBeCloseTo(straightLine, 0);
  });
});
