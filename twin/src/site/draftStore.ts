/**
 * Keeping the editor's work-in-progress across navigation and reloads.
 *
 * The editor held its draft in React state alone, so the obvious thing to do — trace a
 * building, hit "Preview in Command" to see it, come back — destroyed the work. There is
 * no server here to save to, so the draft lives in `localStorage`: it survives navigation,
 * a reload, and closing the tab, which is the behaviour anyone editing a document expects.
 *
 * Deliberately `localStorage` rather than the `sessionStorage` used for the one-shot
 * command handoff. That handoff is a message consumed once; this is a document, and losing
 * it because a tab was closed would be the same failure in a slower form.
 *
 * Everything is wrapped: storage throws in private-browsing modes and when a quota is hit,
 * and an editor that crashes rather than merely forgetting is strictly worse.
 */

import type { Site } from '@/types';
import { parseDraftSite, serialiseSite } from './schema';

const DRAFT_KEY = 'aetos-twin-editor-draft';

export interface StoredDraft {
  site: Site;
  /** When it was last written, so the editor can say how old the restored work is. */
  savedAt: number;
}

/** Persist the current draft. Safe to call often; the caller debounces. */
export function saveDraft(site: Site): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ site: JSON.parse(serialiseSite(site)), savedAt: Date.now() }),
    );
  } catch {
    // Quota exceeded or storage unavailable. The draft stays in memory for this session;
    // failing loudly here would interrupt editing to report a problem the operator cannot
    // act on mid-trace.
  }
}

/**
 * Restore the saved draft, or null if there is none or it no longer parses.
 *
 * Validated through `parseDraftSite`, not the strict site schema. A draft legitimately has
 * no boundary yet — tracing a building before drawing the fence line is a normal order of
 * work — and validating it as a finished site rejected it, then deleted it on the way out.
 * A draft written by an older build whose shape has genuinely changed is still discarded
 * rather than allowed to crash the editor on mount.
 */
export function loadDraft(): StoredDraft | null {
  if (typeof window === 'undefined') return null;

  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(DRAFT_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw) as { site?: unknown; savedAt?: number };
    if (!parsed.site) return null;
    return {
      site: parseDraftSite(parsed.site),
      savedAt: typeof parsed.savedAt === 'number' ? parsed.savedAt : 0,
    };
  } catch {
    // Unreadable or no longer schema-valid — drop it rather than half-restore.
    clearDraft();
    return null;
  }
}

export function clearDraft(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.removeItem(DRAFT_KEY);
  } catch {
    // Nothing useful to do; a stale draft is a far smaller problem than a thrown error.
  }
}

/** True when there is anything worth restoring — an untouched empty draft is not. */
export function draftIsWorthKeeping(site: Site): boolean {
  return site.assets.length > 0 || site.boundary.length >= 3 || site.sensors.length > 0;
}
