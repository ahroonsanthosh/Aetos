/**
 * Camera: the operator's own device camera as a sensor.
 *
 * The browser opens the camera, finds motion in each frame on a small greyscale copy, and draws
 * what it found over the live view. With "Feed the picture" on, the boxes (never the video) are
 * sent to the backend a few times a second, where the camera adapter turns each one into a
 * bearing-only detection that enters the pipeline like any other sensor's
 * (`sensors/adapters/camera`).
 *
 * Previewing needs nothing. Feeding needs the operator role, because it puts detections into
 * the shared picture; a viewer sees the preview and is told why the switch is off.
 *
 * The camera measures direction only. Where along that direction a contact sits is the
 * "assumed range" below, and the detections carry an uncertainty that says so.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { MotionDetector, toGrey, type MotionBox } from '../../components/camera/motion'
import { ApiError, api } from '../../data/client/api'
import { useAuth } from '../../data/client/auth'
import type { CameraSessionRequest, CameraStatusView } from '../../data/models/api'
import { MODE_CHANGED_EVENT } from '../../components/shell/TopBar'
import { PageHeader } from './TracksPage'
import './pages.css'
import './CameraPage.css'

/** Analysis width in pixels. Small on purpose: motion at this size is plenty for a bearing,
 *  and it keeps the per-frame cost negligible on a phone. */
const ANALYSIS_WIDTH = 160
const ANALYSE_EVERY_MS = 250
const STATUS_EVERY_MS = 2000

const DEFAULT_MOUNT: CameraSessionRequest = {
  height_m: 1.5,
  boresight_deg: 0,
  tilt_deg: 10,
  horizontal_fov_deg: 66,
  vertical_fov_deg: 40,
  assumed_range_m: 300,
  range_sigma_m: 250,
}

type Field = {
  key: keyof CameraSessionRequest
  label: string
  unit: string
  min: number
  max: number
  step: number
  help: string
}

const FIELDS: Field[] = [
  { key: 'boresight_deg', label: 'Pointing', unit: '°', min: 0, max: 359, step: 1, help: 'Compass bearing the camera faces. 0 is north, 90 east.' },
  { key: 'tilt_deg', label: 'Tilt', unit: '°', min: -30, max: 80, step: 1, help: 'How far above the horizon the camera points.' },
  { key: 'horizontal_fov_deg', label: 'Horizontal view', unit: '°', min: 10, max: 160, step: 1, help: 'About 66° for a phone main camera, 100°+ for ultra-wide.' },
  { key: 'vertical_fov_deg', label: 'Vertical view', unit: '°', min: 5, max: 140, step: 1, help: 'About 40° for a phone held in landscape.' },
  { key: 'assumed_range_m', label: 'Assumed range', unit: 'm', min: 20, max: 5000, step: 10, help: 'The camera cannot measure distance. Contacts are placed this far out.' },
  { key: 'range_sigma_m', label: 'Range uncertainty', unit: 'm', min: 10, max: 5000, step: 10, help: 'How wrong that range may be. Keep it large; it is a guess.' },
  { key: 'height_m', label: 'Camera height', unit: 'm', min: 0, max: 500, step: 0.5, help: 'Above the site reference point.' },
]

export function CameraPage() {
  const { session } = useAuth()
  const canFeed = session?.role === 'operator' || session?.role === 'admin'

  const videoRef = useRef<HTMLVideoElement>(null)
  const overlayRef = useRef<HTMLCanvasElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const detectorRef = useRef<MotionDetector | null>(null)
  const sendingRef = useRef(false)
  const feedingRef = useRef(false)

  const [running, setRunning] = useState(false)
  const [feeding, setFeeding] = useState(false)
  const [mount, setMount] = useState<CameraSessionRequest>(DEFAULT_MOUNT)
  const [boxes, setBoxes] = useState<MotionBox[]>([])
  const [note, setNote] = useState<string>('Background not learned yet.')
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<CameraStatusView | null>(null)

  feedingRef.current = feeding

  // Backend view of the camera, polled. It is the backend's state that matters: a browser
  // that thinks it is feeding while the server detached the camera must be shown that.
  useEffect(() => {
    const abort = new AbortController()
    const poll = () =>
      api
        .camera(abort.signal)
        .then(setStatus)
        .catch(() => {
          if (!abort.signal.aborted) setStatus(null)
        })
    poll()
    const timer = window.setInterval(poll, STATUS_EVERY_MS)
    return () => {
      abort.abort()
      window.clearInterval(timer)
    }
  }, [])

  const stopFeeding = useCallback(async () => {
    setFeeding(false)
    try {
      setStatus(await api.stopCamera())
    } catch {
      // The server detaches an abandoned camera by itself within a minute.
    }
    window.dispatchEvent(new Event(MODE_CHANGED_EVENT))
  }, [])

  const stopCamera = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    detectorRef.current = null
    setRunning(false)
    setBoxes([])
    if (feedingRef.current) void stopFeeding()
  }, [stopFeeding])

  const startCamera = useCallback(async () => {
    setError(null)
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('This browser cannot open a camera here. Cameras need https or localhost.')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' } },
        audio: false,
      })
      streamRef.current = stream
      const video = videoRef.current
      if (video) {
        video.srcObject = stream
        await video.play()
      }
      setRunning(true)
      setNote('Learning the background. Hold the camera still.')
    } catch (cause) {
      setError(
        cause instanceof DOMException && cause.name === 'NotAllowedError'
          ? 'Camera permission was refused. Allow it in the browser and try again.'
          : `Could not open a camera: ${String(cause)}`,
      )
    }
  }, [])

  // Stop the camera and the feed when leaving the page.
  useEffect(() => () => stopCamera(), [stopCamera])

  // The analysis loop.
  useEffect(() => {
    if (!running) return
    const canvas = document.createElement('canvas')
    const context = canvas.getContext('2d', { willReadFrequently: true })
    let grey: Uint8Array | undefined

    const timer = window.setInterval(() => {
      const video = videoRef.current
      if (!video || !context || video.videoWidth === 0) return

      const width = ANALYSIS_WIDTH
      const height = Math.max(16, Math.round((video.videoHeight / video.videoWidth) * width))
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
        detectorRef.current = new MotionDetector(width, height)
      }
      const detector = detectorRef.current ?? (detectorRef.current = new MotionDetector(width, height))

      context.drawImage(video, 0, 0, width, height)
      grey = toGrey(context.getImageData(0, 0, width, height).data, grey)
      const result = detector.process(grey)

      setBoxes(result.boxes)
      setNote(
        result.sceneChanged
          ? 'The whole view changed. Did the camera move? Relearning the background.'
          : result.warming
            ? 'Learning the background. Hold the camera still.'
            : result.boxes.length
              ? `${result.boxes.length} moving region(s).`
              : 'Watching. Nothing moving.',
      )
      drawOverlay(overlayRef.current, video, result.boxes, width, height)

      if (feedingRef.current && !result.warming && !sendingRef.current) {
        sendingRef.current = true
        api
          .sendCameraFrame({ width, height, regions: result.boxes })
          .catch((cause) => {
            if (cause instanceof ApiError && cause.status === 409) {
              // The server no longer has a session (expired or stopped elsewhere).
              setFeeding(false)
              setError('The server stopped the camera session. Turn feeding on again to resume.')
            }
          })
          .finally(() => {
            sendingRef.current = false
          })
      }
    }, ANALYSE_EVERY_MS)

    return () => window.clearInterval(timer)
  }, [running])

  const startFeeding = useCallback(async () => {
    setError(null)
    try {
      setStatus(await api.startCamera(mount))
      setFeeding(true)
      window.dispatchEvent(new Event(MODE_CHANGED_EVENT))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [mount])

  // Re-aim a camera that is already feeding when the mount changes.
  const applyMount = useCallback(async () => {
    if (!feeding) return
    try {
      setStatus(await api.startCamera(mount))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [feeding, mount])

  const serverState = status?.active ? status.state : 'not_started'

  return (
    <div className="page">
      <PageHeader title="Camera" subtitle="device camera · bearing-only sensor" />
      <div className="page__body camera-page">
        <p className="page__note">
          Uses this device's camera. Motion is found here in the browser; only the boxes are sent
          to Aetos, never the video. A camera measures direction, not distance, so each contact
          is placed at the assumed range below with an uncertainty that says how little that
          means. Hold the camera still and point it at open sky.
        </p>

        {error && <div className="page__error camera-page__error">{error}</div>}

        <div className="camera-page__grid">
          <section className="panel camera-page__view">
            <div className="camera-page__stage">
              <video ref={videoRef} className="camera-page__video" playsInline muted />
              <canvas ref={overlayRef} className="camera-page__overlay" />
              {!running && <div className="camera-page__idle">Camera off</div>}
            </div>
            <div className="camera-page__controls">
              {running ? (
                <button type="button" className="camera-page__button" onClick={stopCamera}>
                  Stop camera
                </button>
              ) : (
                <button type="button" className="camera-page__button camera-page__button--primary" onClick={startCamera}>
                  Start camera
                </button>
              )}
              <label className={`camera-page__toggle ${!canFeed || !running ? 'is-disabled' : ''}`}>
                <input
                  type="checkbox"
                  checked={feeding}
                  disabled={!canFeed || !running}
                  onChange={(event) => (event.target.checked ? void startFeeding() : void stopFeeding())}
                />
                Feed the picture
              </label>
              <span className="camera-page__note label">{running ? note : 'Preview is local to this device.'}</span>
            </div>
            {!canFeed && (
              <p className="camera-page__hint">
                Feeding the picture needs the operator role. You can still preview motion here.
              </p>
            )}
            {running && boxes.length > 0 && (
              <ul className="camera-page__boxes">
                {boxes.slice(0, 5).map((box, i) => (
                  <li key={i} className="numeric">
                    {bearingOf(box, mount).toFixed(1)}° · {Math.round(box.strength * 100)}% changed
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="panel camera-page__side">
            <div className="panel__header">
              <span className="label">Mount</span>
            </div>
            <form
              className="camera-page__form"
              onSubmit={(event) => {
                event.preventDefault()
                void applyMount()
              }}
            >
              {FIELDS.map((field) => (
                <label key={field.key} className="camera-page__field" title={field.help}>
                  <span className="label">{field.label}</span>
                  <span className="camera-page__input">
                    <input
                      type="number"
                      min={field.min}
                      max={field.max}
                      step={field.step}
                      value={mount[field.key] ?? ''}
                      onChange={(event) =>
                        setMount((m) => ({ ...m, [field.key]: Number(event.target.value) }))
                      }
                    />
                    <span>{field.unit}</span>
                  </span>
                  <span className="camera-page__help">{field.help}</span>
                </label>
              ))}
              <button type="submit" className="camera-page__button" disabled={!feeding}>
                Apply to running camera
              </button>
            </form>

            <div className="panel__header">
              <span className="label">In Aetos</span>
            </div>
            <dl className="camera-page__status">
              <dt className="label">State</dt>
              <dd className={`camera-page__state camera-page__state--${serverState}`}>
                {status ? serverState.replace('_', ' ') : 'unknown'}
              </dd>
              <dt className="label">Detail</dt>
              <dd>{status?.reason ?? 'Backend unreachable.'}</dd>
              <dt className="label">Frames</dt>
              <dd className="numeric">{status?.frames_received ?? 0}</dd>
              <dt className="label">Detections</dt>
              <dd className="numeric">{status?.detections_emitted ?? 0}</dd>
              {status?.mount && (
                <>
                  <dt className="label">Position</dt>
                  <dd className="numeric">
                    {status.mount.latitude.toFixed(5)}, {status.mount.longitude.toFixed(5)}
                  </dd>
                </>
              )}
            </dl>
          </section>
        </div>
      </div>
    </div>
  )
}

/** Same pinhole model as the backend adapter, for the readout only. */
export function bearingOf(box: MotionBox, mount: CameraSessionRequest, width = ANALYSIS_WIDTH): number {
  const offset = (box.x + box.width / 2 - width / 2) / (width / 2)
  const half = (mount.horizontal_fov_deg * Math.PI) / 360
  const angle = (Math.atan(offset * Math.tan(half)) * 180) / Math.PI
  return (((mount.boresight_deg + angle) % 360) + 360) % 360
}

function drawOverlay(
  canvas: HTMLCanvasElement | null,
  video: HTMLVideoElement,
  boxes: MotionBox[],
  width: number,
  height: number,
) {
  if (!canvas) return
  const rect = video.getBoundingClientRect()
  const ratio = window.devicePixelRatio || 1
  canvas.width = Math.round(rect.width * ratio)
  canvas.height = Math.round(rect.height * ratio)
  const context = canvas.getContext('2d')
  if (!context) return
  context.clearRect(0, 0, canvas.width, canvas.height)

  // The video is letterboxed inside its element (object-fit: contain); match that.
  const scale = Math.min(canvas.width / width, canvas.height / height)
  const offsetX = (canvas.width - width * scale) / 2
  const offsetY = (canvas.height - height * scale) / 2

  context.lineWidth = 2 * ratio
  context.strokeStyle = '#e07a3f'
  for (const box of boxes) {
    context.strokeRect(offsetX + box.x * scale, offsetY + box.y * scale, box.width * scale, box.height * scale)
  }
}
