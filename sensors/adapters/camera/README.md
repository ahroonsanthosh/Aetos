# `sensors/adapters/camera` — the device camera

## Responsibility

The operator's own camera (a phone or a laptop webcam) as a sensor. The console's **Camera**
page opens it in the browser, finds the regions of each frame that moved, and sends only those
boxes to `POST /api/v1/camera/frames`. No video leaves the device. This adapter turns each box
into a canonical `Detection`.

Doing the vision in the browser is what makes a camera usable on a hosted deployment: the
server has no camera, and cannot reach one on the operator's network, but the operator's
browser can.

| File | Owns |
|---|---|
| `adapter.py` | `CameraAdapter`: boxes in, detections out, lifecycle and health |
| `config.py` | `CameraMount`: where the camera is, which way it points, the lens, the assumed range |

The browser side is `frontend/src/components/camera/motion.ts` (frame differencing against a
running background, then connected regions) and `frontend/src/app/pages/CameraPage.tsx`. The
backend wiring is `PipelineCamera` in `backend/main.py` and the routes in
`backend/api/camera.py`.

## What a fixed monocular camera can honestly report

A **bearing** and an **elevation angle**. It measures no range, and therefore no speed over
ground, so `kinematics` is left `None`. The schema needs a position, so the contact is placed
at the operator's `assumed_range_m` along the line of sight, with a covariance that says how
little that means: along the line of sight it is the full `range_sigma_m`, across it a fraction
of a degree. The covariance is rotated into ENU, so it is right whichever way the camera points.

Detections are stamped on arrival from the injected clock, never with the browser's clock.

## Lifecycle and labelling

The camera is attached to the pipeline when an operator turns on **Feed the picture**, and
detached when they turn it off, leave the page, or the browser sends nothing for 60 s. While it
is attached it counts as a real sensor, so the mode badge reads **LIVE**, or **HYBRID** when a
simulated scenario is also running. Previewing motion without feeding needs no role; feeding
needs `operator`.

## What it cannot do

The same as any motion detector on a fixed camera: a hovering target is learned into the
background within seconds, a textured background (trees, roofs) hides small targets, and a
camera that moves sees the whole scene move. The last is detected in the browser and reported
as a scene change rather than as a burst of contacts. Point it at open sky and keep it still.

Browsers open cameras only on https or `localhost`.
