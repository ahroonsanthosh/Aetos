/**
 * Which track carries on an object after the backend retired a duplicate of it (ADR 0021).
 *
 * The backend says so in the `TRACK_LOST` event itself — `duplicate_of` names the survivor —
 * so nothing here infers anything; it only remembers what was said. That is what lets an
 * operator's selection follow the aircraft rather than vanish with the second identifier it
 * briefly had.
 */

/** How many retirements are remembered. Only recent ones can still be someone's selection. */
const MEMORY = 64

/** The survivor a `TRACK_LOST` event names, if the track was retired as a duplicate. */
export function survivorNamedBy(detail: Record<string, unknown>): string | null {
  const survivor = detail.duplicate_of
  return detail.reason === 'duplicate' && typeof survivor === 'string' ? survivor : null
}

/** A copy of `mergedInto` with one more retirement, oldest forgotten beyond the bound. */
export function rememberRetirement(
  mergedInto: ReadonlyMap<string, string>,
  retired: string,
  survivor: string,
): Map<string, string> {
  const next = new Map(mergedInto)
  next.delete(retired)
  next.set(retired, survivor)
  while (next.size > MEMORY) {
    const oldest = next.keys().next().value as string
    next.delete(oldest)
  }
  return next
}

/**
 * The track now carrying `trackId`'s object: itself if it was never retired, otherwise the
 * end of the chain of survivors. Bounded, so a malformed cycle in the data cannot hang the UI.
 */
export function currentIdentity(trackId: string, mergedInto: ReadonlyMap<string, string>): string {
  let current = trackId
  for (let step = 0; step < MEMORY && mergedInto.has(current); step++) {
    current = mergedInto.get(current) as string
  }
  return current
}
