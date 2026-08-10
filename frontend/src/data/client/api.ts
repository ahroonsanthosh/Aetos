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
  AlertView,
  ModeView,
  ScenarioListResponse,
  SensorListResponse,
  SessionView,
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
  return request<T>('GET', path, signal)
}

/** The only non-GET call the interface makes. Kept alongside `get` rather than given its own
 *  helper, because the error handling is the part that matters and duplicating it is how the
 *  two drift into reporting the same failure differently. */
async function post<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  return request<T>('POST', path, signal, body)
}

async function request<T>(
  method: string,
  path: string,
  signal?: AbortSignal,
  body?: unknown,
): Promise<T> {
  const url = `${API_PREFIX}${path}`
  let response: Response

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  try {
    response = await fetch(url, {
      method,
      signal,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      // Cookies on every request, including the WebSocket upgrade the browser makes on its
      // own. `same-origin` rather than `include`: the bundle is served by the backend, so
      // there is no cross-origin case to support, and `include` would send the session to any
      // origin this code was ever hosted from.
      credentials: 'same-origin',
    })
  } catch (cause) {
    // A network failure is not a 500 and must not be reported as one. The interface shows
    // "backend unreachable", which is a different operator problem from "backend is broken".
    if (signal?.aborted) throw cause
    throw new ApiError(0, url, `Could not reach the backend: ${String(cause)}`)
  }

  if (!response.ok) {
    throw new ApiError(response.status, url, await describe(response))
  }

  // 204, which logout answers with. `response.json()` on an empty body throws, and the caller
  // wants nothing back anyway.
  if (response.status === 204) return undefined as T

  return (await response.json()) as T
}

/** The backend's own `detail`, when it sent one.
 *
 * Worth the extra parse: the refusals this client has to surface are ones an operator can act
 * on — "this action requires the operator role or above" is a different problem from "your
 * session expired", and `403 Forbidden` alone does not distinguish them.
 */
async function describe(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail) return body.detail
  } catch {
    // Not JSON, or empty. Fall through to the status line.
  }
  return `${response.status} ${response.statusText}`
}

export const api = {
  tracks: (signal?: AbortSignal) => get<TrackListResponse>('/tracks', signal),
  track: (id: string, signal?: AbortSignal) => get<TrackView>(`/tracks/${id}`, signal),
  trackHistory: (id: string, signal?: AbortSignal) =>
    get<TrackHistoryResponse>(`/tracks/${id}/history`, signal),
  situation: (signal?: AbortSignal) => get<SituationView>('/situation', signal),
  alerts: (signal?: AbortSignal) => get<AlertListResponse>('/alerts', signal),
  acknowledgeAlert: (alertId: string, signal?: AbortSignal) =>
    post<AlertView>(`/alerts/${encodeURIComponent(alertId)}/acknowledge`, signal),

  login: (username: string, password: string, signal?: AbortSignal) =>
    post<SessionView>('/auth/login', signal, { username, password }),
  logout: (signal?: AbortSignal) => post<void>('/auth/logout', signal),
  me: (signal?: AbortSignal) => get<SessionView>('/auth/me', signal),
  sensors: (signal?: AbortSignal) => get<SensorListResponse>('/sensors', signal),
  sites: (signal?: AbortSignal) => get<SiteListResponse>('/sites', signal),
  scenarios: (signal?: AbortSignal) => get<ScenarioListResponse>('/scenarios', signal),
  mode: (signal?: AbortSignal) => get<ModeView>('/system/mode', signal),
  health: (signal?: AbortSignal) => get<SystemHealthView>('/system/health', signal),
}
