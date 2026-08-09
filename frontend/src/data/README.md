# `data`

## Responsibility

Everything that crosses the boundary to the backend, and the types on either side of it.

| Path | Contents |
|---|---|
| `models/api.ts` | Wire types, transcribed from the backend schemas — not interpreted |
| `models/track.ts` | Presentation types, and the one conversion between the two |
| `models/geo.ts` | Geodetic to site-local conversion, for display only |
| `client/api.ts` | REST client. Fetches, checks status, parses. Does not reshape or cache |
| `client/stream.ts` | Resuming WebSocket client with its own reconnection policy |
| `client/useTrackPicture.ts` | The live picture: REST snapshot kept current by the stream |
| `client/useTrackHistory.ts` | One track's recorded history, fetched only while selected |

## Interface boundary

**The frontend owns no authoritative state.** The picture held here is a cache of backend
state. Nothing computes a track, decides a track's state, or infers a value the backend did
not send. Where they disagree, the backend is right by definition.

`models/api.ts` matches the backend field for field so a contract change is a type error
rather than an undefined at runtime. Reshaping happens only in `models/track.ts`, which is the
single place a backend change has to be absorbed.

Range and bearing computed in `geo.ts` are **site-relative** and for display only. The backend
does the authoritative conversion in an exact ENU frame; this is an approximation good to well
under a metre at operational ranges, used to place a marker and never to make a decision.
