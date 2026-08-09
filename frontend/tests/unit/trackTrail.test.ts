/**
 * Trail subsampling: the rendering cap that keeps a long-coasting track's history legible
 * without discarding the fetched data itself (see `useTrackHistory`, which stays unbounded).
 */

import { describe, expect, it } from 'vitest'

import { subsample } from '../../src/components/map/TrackTrail'
import type { TrackHistoryPoint } from '../../src/data/models/track'

function point(seq: number): TrackHistoryPoint {
  return {
    seq,
    eventType: 'TRACK_UPDATED',
    timestamp: new Date(2026, 7, 8, 12, 0, seq),
    state: 'TRACKING',
    enu: { eastM: seq, northM: seq, upM: 0 },
    altitudeM: 100,
    speedMps: 10,
    headingDeg: 90,
  }
}

describe('subsample', () => {
  it('returns every point unchanged when already within the cap', () => {
    const points = Array.from({ length: 10 }, (_, i) => point(i))

    expect(subsample(points, 400)).toEqual(points)
  })

  it('reduces to exactly the requested count when over the cap', () => {
    const points = Array.from({ length: 1000 }, (_, i) => point(i))

    expect(subsample(points, 400)).toHaveLength(400)
  })

  it('always keeps the first and last point', () => {
    const points = Array.from({ length: 1000 }, (_, i) => point(i))
    const sampled = subsample(points, 50)

    expect(sampled[0]).toEqual(points[0])
    expect(sampled[sampled.length - 1]).toEqual(points[points.length - 1])
  })

  it('preserves chronological order', () => {
    const points = Array.from({ length: 500 }, (_, i) => point(i))
    const sampled = subsample(points, 37)

    const seqs = sampled.map((p) => p.seq)
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b))
  })

  it('does not duplicate points', () => {
    const points = Array.from({ length: 500 }, (_, i) => point(i))
    const sampled = subsample(points, 41)

    expect(new Set(sampled.map((p) => p.seq)).size).toBe(sampled.length)
  })

  it('handles the boundary where length equals the cap exactly', () => {
    const points = Array.from({ length: 400 }, (_, i) => point(i))

    expect(subsample(points, 400)).toEqual(points)
  })
})
