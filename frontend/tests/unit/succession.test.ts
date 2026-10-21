import { describe, expect, it } from 'vitest'

import {
  currentIdentity,
  rememberRetirement,
  survivorNamedBy,
} from '../../src/data/models/succession'

describe('track succession after a duplicate is retired', () => {
  it('reads the survivor only from a duplicate retirement', () => {
    expect(survivorNamedBy({ reason: 'duplicate', duplicate_of: 'TRK-00001' })).toBe('TRK-00001')
    // An ordinary loss names no successor: the object may genuinely be gone.
    expect(survivorNamedBy({})).toBeNull()
    expect(survivorNamedBy({ reason: 'duplicate' })).toBeNull()
  })

  it('follows a selection to the track carrying on, through a chain', () => {
    let merged = rememberRetirement(new Map(), 'TRK-00045', 'TRK-00012')
    merged = rememberRetirement(merged, 'TRK-00012', 'TRK-00001')
    expect(currentIdentity('TRK-00045', merged)).toBe('TRK-00001')
    expect(currentIdentity('TRK-00007', merged)).toBe('TRK-00007')
  })

  it('cannot hang on a cycle and stays bounded', () => {
    let merged = rememberRetirement(new Map(), 'A', 'B')
    merged = rememberRetirement(merged, 'B', 'A')
    expect(['A', 'B']).toContain(currentIdentity('A', merged))

    for (let i = 0; i < 500; i++) merged = rememberRetirement(merged, `T${i}`, `S${i}`)
    expect(merged.size).toBeLessThanOrEqual(64)
    expect(currentIdentity('T499', merged)).toBe('S499')
  })
})
