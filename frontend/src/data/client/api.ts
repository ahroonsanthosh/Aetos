/**
 * REST client.
 *
 * Thin on purpose. It fetches, checks the status, parses JSON and returns wire types — it
 * does not reshape, cache or interpret. The frontend owns no authoritative state, so a client
 * that quietly merged or remembered responses would be inventing one.
 *
 * Paths are origin-relative. The dev server proxies `/api` to the backend, so no base URL is
 * compiled into the bundle and moving between environments is a deployment concern rather
 * than a rebuild.
 */

import { API_PREFIX } from '../models/api'
import type {
  AlertListResponse,
  ModeView,
  ScenarioListResponse,
  SensorListResponse,
  SituationView,
  SiteListResponse,
  SystemHealthView,
  TrackHistoryResponse,
  TrackListResponse,
  TrackView,
} from '../models/api'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = `${API_PREFIX}${path}`
  let response: Response

  try {
    response = await fetch(url, { signal, headers: { Accept: 'application/json' } })
  } catch (cause) {
    // A network failure is not a 500 and must not be reported as one. The interface shows
    // "backend unreachable", which is a different operator problem from "backend is broken".
    if (signal?.aborted) throw cause
    throw new ApiError(0, url, `Could not reach the backend: ${String(cause)}`)
  }

  if (!response.ok) {
    throw new ApiError(response.status, url, `${response.status} ${response.statusText}`)
  }

  return (await response.json()) as T
}

export const api = {
  tracks: (signal?: AbortSignal) => get<TrackListResponse>('/tracks', signal),
  track: (id: string, signal?: AbortSignal) => get<TrackView>(`/tracks/${id}`, signal),
  trackHistory: (id: string, signal?: AbortSignal) =>
    get<TrackHistoryResponse>(`/tracks/${id}/history`, signal),
  situation: (signal?: AbortSignal) => get<SituationView>('/situation', signal),
  alerts: (signal?: AbortSignal) => get<AlertListResponse>('/alerts', signal),
  sensors: (signal?: AbortSignal) => get<SensorListResponse>('/sensors', signal),
  sites: (signal?: AbortSignal) => get<SiteListResponse>('/sites', signal),
  scenarios: (signal?: AbortSignal) => get<ScenarioListResponse>('/scenarios', signal),
  mode: (signal?: AbortSignal) => get<ModeView>('/system/mode', signal),
  health: (signal?: AbortSignal) => get<SystemHealthView>('/system/health', signal),
}
