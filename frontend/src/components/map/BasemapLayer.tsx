/**
 * An optional geographic basemap beneath the PPI.
 *
 * The PPI stays authoritative. This follows its viewport and never drives it, so turning the
 * basemap off — or failing to load it — leaves the display exactly as it was. That is the
 * property ADR 0010 was built around and ADR 0014 preserves: a display that cannot fail to
 * render is a better foundation than one that can, and the tactical picture must not depend on
 * tiles arriving.
 *
 * **MapLibre is loaded on demand.** It is a large dependency and the basemap is off by
 * default, so importing it eagerly would make every operator pay for a feature most sessions
 * never turn on. The dynamic import means the bundle an idle display loads is unchanged.
 *
 * **Missing tiles are reported, not painted over.** Without a `.pmtiles` archive this renders
 * nothing and says so through `onStatus`. A blank grey rectangle under the tracks would read
 * as "there is nothing on the ground there", which is a statement about the world rather than
 * about the software, and a false one.
 */

import { useEffect, useRef } from 'react'

import type { LocalFrame } from '../../data/models/geo'
import { cameraFor } from './basemapCamera'
import type { MapViewport } from './useMapViewport'
import './BasemapLayer.css'

export type BasemapStatus = 'loading' | 'ready' | 'unavailable'

interface Props {
  viewport: MapViewport
  frame: LocalFrame
  /** Where the tile archive lives. Served by the backend's static mount. */
  tilesUrl: string
  /** Glyph fonts for map labels. Optional in practice: without them the terrain still draws,
   *  just unlabelled. */
  fontsUrl: string
  onStatus: (status: BasemapStatus, detail?: string) => void
}

/** Minimal MapLibre surface this component uses, so the dynamic import stays typed without
 *  a top-level import pulling the library into the main bundle. */
interface MapLike {
  jumpTo(options: { center: [number, number]; zoom: number }): void
  resize(): void
  remove(): void
  on(event: string, handler: (payload?: unknown) => void): void
}

export function BasemapLayer({ viewport, frame, tilesUrl, fontsUrl, onStatus }: Props) {
  const container = useRef<HTMLDivElement | null>(null)
  const map = useRef<MapLike | null>(null)
  // The camera is applied from an effect that must not re-run on every viewport change, so the
  // latest viewport is read through a ref rather than closed over.
  const latest = useRef({ viewport, frame })
  latest.current = { viewport, frame }

  useEffect(() => {
    let cancelled = false

    async function create() {
      const element = container.current
      if (!element) return

      try {
        const [{ Map: MapLibreMap, addProtocol, setWorkerUrl }, { Protocol }, themes, worker] =
          await Promise.all([
            import('maplibre-gl'),
            import('pmtiles'),
            import('@protomaps/basemaps'),
            // MapLibre otherwise derives its worker URL at runtime from `import.meta.url`,
            // which a bundler cannot see — so the file is never emitted, the request falls
            // through to the app shell, and a module worker handed HTML fails. The symptom is
            // a basemap that reports no error and never finishes loading, which is a
            // miserable thing to diagnose from the outside. Asking the bundler for the worker
            // explicitly makes it an emitted asset with a hashed name.
            import('maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'),
          ])
        if (cancelled) return

        setWorkerUrl(worker.default)

        // Fail before building a map around an archive that is not there, so the operator
        // gets "no basemap installed" rather than an empty map that looks like empty ground.
        //
        // A one-byte ranged GET rather than a HEAD. It tests the capability pmtiles actually
        // needs — the archive is read by byte range, never downloaded whole — so it catches
        // both a missing file and a server that serves it but ignores `Range`, which would
        // otherwise pull hundreds of megabytes through the browser one tile at a time. HEAD
        // tests neither, and is not even routed by the backend's SPA handler.
        const problem = await probeTiles(tilesUrl)
        if (cancelled) return
        if (problem) {
          onStatus('unavailable', problem)
          return
        }

        const protocol = new Protocol()
        addProtocol('pmtiles', protocol.tile)

        const instance = new MapLibreMap({
          container: element,
          style: {
            version: 8,
            // Local, like the tiles. A remote glyph server would put a network dependency
            // back into a feature whose whole point is not having one — and it would be the
            // kind that fails quietly, leaving an unlabelled map with no explanation. Absent
            // fonts cost labels and nothing else; see the error handling below.
            glyphs: fontsUrl,
            sources: {
              protomaps: { type: 'vector', url: `pmtiles://${tilesUrl}`, attribution: '© OpenStreetMap' },
            },
            layers: themes.layers('protomaps', themes.namedFlavor('black'), { lang: 'en' }),
          },
          // Every interaction is disabled. The PPI owns navigation, and a basemap that could be
          // dragged independently would slide out of register with the tracks drawn over it —
          // silently, and in a way an operator would read as the tracks having moved.
          interactive: false,
          attributionControl: false,
          center: [0, 0],
          zoom: 1,
        }) as unknown as MapLike

        // An error *before* load means there is no basemap and the operator should be told.
        // An error after it usually means one optional piece is missing — most often the
        // glyph fonts, which cost labels and nothing else — and tearing down working terrain
        // over that would be a worse outcome than an unlabelled map.
        let loaded = false
        instance.on('error', () => {
          if (!loaded && !cancelled) {
            onStatus('unavailable', 'The basemap failed to render.')
          }
        })
        instance.on('load', () => {
          if (cancelled) return
          loaded = true
          onStatus('ready')
          applyCamera(instance, element, latest.current)
        })

        map.current = instance
      } catch (cause) {
        if (!cancelled) onStatus('unavailable', `Basemap could not start: ${String(cause)}`)
      }
    }

    onStatus('loading')
    void create()

    return () => {
      cancelled = true
      map.current?.remove()
      map.current = null
    }
    // Deliberately once per mount. The component is unmounted entirely when the basemap is
    // toggled off, so there is no case where the tile source changes under a live map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tilesUrl, fontsUrl])

  // Follow the PPI. `jumpTo` rather than an animated move: the basemap has to be in register
  // with the tracks on *this* frame, and an eased camera would lag behind a drag by a few
  // hundred milliseconds — during which every track sits over the wrong ground.
  useEffect(() => {
    const element = container.current
    if (map.current && element) applyCamera(map.current, element, { viewport, frame })
  }, [viewport, frame])

  return <div ref={container} className="basemap" aria-hidden="true" />
}

/** Magic bytes every PMTiles v3 archive starts with. */
const PMTILES_MAGIC = 'PMTiles'

/**
 * Returns a reason the archive cannot be used, or null if it can.
 *
 * Checks the *content*, not the status code, because a status code cannot answer the question
 * here. The backend serves the interface as a single-page app, so any unmatched path returns
 * the HTML shell — with a 200, and with a 206 if a range was asked for. A missing archive is
 * therefore indistinguishable from a present one by status alone, and a probe that trusted the
 * status would hand MapLibre a page of HTML to parse as a tile archive and then report the
 * resulting failure as some obscure rendering error.
 *
 * Reading the first seven bytes settles it, and settles the range-support question at the same
 * time: pmtiles reads the archive by byte range and never downloads it whole, so a server that
 * ignores `Range` has to be caught here rather than discovered as a slow leak of bandwidth.
 */
async function probeTiles(tilesUrl: string): Promise<string | null> {
  let response: Response
  try {
    response = await fetch(tilesUrl, { headers: { Range: `bytes=0-${PMTILES_MAGIC.length - 1}` } })
  } catch (cause) {
    return `Could not reach ${tilesUrl}: ${String(cause)}`
  }

  if (response.status === 404) return `No tile archive installed at ${tilesUrl}.`
  if (!response.ok) {
    void response.body?.cancel()
    return `Tile archive at ${tilesUrl} returned ${response.status}.`
  }
  if (response.status !== 206) {
    // Cancel rather than read: if the range was ignored, this body is the entire archive, and
    // reading it to find that out would be the download being guarded against.
    void response.body?.cancel()
    return `${tilesUrl} was served without range support, which pmtiles requires.`
  }

  const head = new Uint8Array(await response.arrayBuffer())
  const magic = String.fromCharCode(...head.slice(0, PMTILES_MAGIC.length))
  if (magic !== PMTILES_MAGIC) {
    return `No tile archive installed at ${tilesUrl}.`
  }
  return null
}

function applyCamera(
  map: MapLike,
  element: HTMLElement,
  { viewport, frame }: { viewport: MapViewport; frame: LocalFrame },
): void {
  // The SVG's square viewBox is fitted to the shorter dimension and centred, so that is the
  // side the camera scale has to be computed against.
  const side = Math.min(element.clientWidth, element.clientHeight)
  if (side <= 0) return

  const camera = cameraFor(viewport, frame, side)
  map.resize()
  map.jumpTo({ center: [camera.longitude, camera.latitude], zoom: camera.zoom })
}
