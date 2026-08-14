# `sensors/adapters/camera` — the camera source

## Responsibility

One fixed camera, from network stream to canonical `Detection`. The only place in the codebase
permitted to know how video is transported, what imaging library is in use, or which model
performs inference.

Enforced by `tests/unit/test_module_boundaries.py`, not by convention.

## Three files, three jobs

| File | Owns | Depends on |
|---|---|---|
| `camera_adapter.py` | Stream I/O, lifecycle, geometry, health, schema assembly | both of the below |
| `motion_detector.py` | Where something moved. Plain functions and one model object | imaging library only |
| `classifier.py` | What a crop is. Lazily loaded model behind two methods | inference library only |

The split is what makes each testable alone. `motion_detector` runs against two still images
with no stream; `classifier` runs against an array with no adapter; `camera_adapter` runs
against a scripted frame source and a stand-in classifier, with neither a handset nor a
downloaded model. Neither of the lower two imports the adapter, so replacing the motion
algorithm or the model touches one file.

## What a fixed monocular camera can honestly report

A **bearing**, and nothing else. It measures no range, and therefore no speed over ground —
apparent motion in the image is a function of range, so a distant fast object and a near slow
one are the same pixels. `kinematics` is left `None` rather than filled with a derived figure.

The canonical schema requires a position, so the adapter places the contact at the configured
`assumed_range_m` along the measured bearing and reports a covariance that says exactly how
little that means: the along-line-of-sight term is the full configured range uncertainty, of
the same order as the range itself, while cross-range is genuinely precise. That anisotropy is
the reason `position_covariance_enu` is populated rather than an isotropic scalar — a filter
handed one number applies it in every direction and becomes confident about a range nobody
measured.

## Degradation

The inference dependency is optional. Without it, motion detection still runs and the adapter
still emits detections, without a `classification` block, and reports itself DEGRADED with the
install hint in its `reason`. A camera that really did see something move and cannot name it
must say so; dropping the detection would report an empty sky (FM-010).

The three ways a stream fails are kept distinct: unreachable at open is OFFLINE, frames drying
up past `frame_timeout_s` is DEGRADED, and a warming background model is DEGRADED until it can
distinguish foreground from background.

## Dependencies

`pip install -e '.[camera]'` — optional, heavy, and not needed to run the radar path, the
simulator or the test suite.

## Configuration

`sensors/config/sensor_config.yaml`, under `camera:`. Stream URL, mounting position and
boresight, horizontal field of view, the assumed range and its uncertainty, motion thresholds
and classifier thresholds.

## Detection envelope

Measured against synthesised frames at 1920x1080 / 66 deg, for a 335 x 253 x 95 mm airframe.
Synthetic, so treat it as bounding the *pipeline's* behaviour, not as a field result.

**Holds up:**

| Condition | Result |
|---|---|
| Position in frame, centre to hard edge | 11/11 across a full traverse |
| Crossing speed, 10 to 200 px/frame | 12/12 at every speed |
| Horizontal motion blur to 41 px | 6/6 |
| Radial approach (growing silhouette) | 30/30, never absorbed |
| Airframe tone, 40 to 180 against sky at 205 | 6/6 at every tone |
| Range, top aspect | 6/6 at 10 m, 15 m, 20 m; holds to ~30 m |
| Range, side aspect | 6/6 at 10 m, 15 m, 20 m; lost by 30 m (19 x 5 px) |

**Does not:**

- **Against ground clutter: 0/6.** A target crossing a treeline or roofline is not detected at
  all. MOG2 estimates variance per pixel, so a textured region demands a far larger deviation
  than open sky does, and the target disappears into the background's own noise. Sky
  background is a requirement, not a preference.
- **Stationary target: absorbed after ~4 frames.** A hovering airframe, or a camera panned to
  follow one, is learned into the background within about 6 s at the default poll. The target
  must move across the frame, and the camera must not move at all.

**Untested and unknown until a field run:** lens distortion at frame edges (a bearing-accuracy
question rather than a detection one), true airframe contrast against real sky in varying
cloud, daylight auto-exposure behaviour, and the stream's actual delivered resolution — nothing
asserts the frame size, so a silently negotiated 640x480 would shrink every pixel figure above
by three and report nothing.
