# Handoff — sensor ingestion and radar preparation

Context for picking this work up in a fresh session. Written at commit `b554d3b`, 17 commits
ahead of the default branch, 581 tests passing, working tree clean.

**Branch:** `claude/sensor-ingestion-layer-5n19kz`
**Base:** `claude/cuas-os-architecture-jhdfek` (the repo's default branch, at `d820679`)

There are two other remote branches — `claude/radar-sensor-integration-efhzhy` and
`claude/repo-state-review-7pyqgv` — that this work has not looked at and may overlap with.

---

## The one thing to know first

**`sensors/live.py` is the composition point.** It is the only file in the repository that
names a concrete sensor adapter class. Everything downstream reads the `SensorAdapter`
interface and the canonical `Detection` schema, and cannot tell what produced a detection.

Swapping the stub radar for the physical unit is a change to the marked line there and its
import. That claim is enforced, not asserted: `tests/unit/test_module_boundaries.py` fails the
build if any other module names an adapter class, and the swap has been applied and reverted to
confirm the suite stays green with the unimplemented adapter attached.

---

## What runs today

```bash
# simulated, driven by a scenario (unchanged behaviour)
python -m backend.main --scenario a_benign_transit

# live, driven by the physical sensors in sensors/config/sensor_config.yaml
python -m backend.main --live --site mtu_bishopstown --no-auth

# camera only, standalone bench check — not part of the pytest suite
python -m sensors.tests.manual_camera_test
```

`--live` and `--scenario` are mutually exclusive and refuse to combine (`RunModeConflict`): a
scenario is fabricated truth and a physical sensor observes the real world, and presenting both
as one picture would make neither trustworthy.

Full suite: `python -m pytest -q` (581 tests, ~70s). Lint: `python -m ruff check .`

---

## Layout of what was added

| Path | What it is |
|---|---|
| `sensors/live.py` | **The composition point.** Builds physical adapters from config. Edit this to add/remove/replace a sensor. |
| `sensors/config/loader.py` | Strict pydantic schema for `sensor_config.yaml`. `extra="forbid"` — an unknown key is a startup error. |
| `sensors/config/sensor_config.yaml` | Deployment config: stream URL, serial port, coverage, uncertainty, thresholds. |
| `sensors/adapters/radar/stub_radar_adapter.py` | Fabricated returns. **The one file deleted when the radar arrives.** |
| `sensors/adapters/radar/ld2451_radar_adapter.py` | Empty shell, every method raises. Its docstring is the implementation spec. |
| `sensors/adapters/radar/ld2451_protocol.py` | **Done and tested.** Byte stream → target reports. 29 tests. |
| `sensors/adapters/camera/` | Adapter (I/O, geometry, health), `motion_detector.py` (CV), `classifier.py` (model). |
| `config/sites/mtu_bishopstown.json` | Bench site. Zone radii 60/150/400 m. |
| `docs/radar_bringup.md` | **Read before trusting any radar output.** |
| `tests/integration/test_live_pipeline.py` | Drives adapters → ingestion → tracking → a formed track, naming no sensor kind. |

---

## Next step: the radar swap

1. **Run `docs/radar_bringup.md` first.** Every byte-level constant in
   `ld2451_protocol.py`'s `WireFormat` is **unverified** — written from documentation, not
   from a capture. The framing *logic* is proven and fuzzed (3,000 adversarial trials, zero
   failures; 5,000-frame soak exact). The *constants* are hypotheses.

2. **Implement `ld2451_radar_adapter.py`** per its own docstring. The protocol module already
   does the framing; what remains is the serial transport, the ENU/WGS-84 conversion (copy
   `StubRadarAdapter._fabricate`), the anisotropic covariance, and the confidence mapping.

3. **Flip the marked line in `sensors/live.py`** and delete the stub.

### Failure modes that do not announce themselves

None of these raise, and all survive a demo that looks like it worked:

- **Inverted direction flag** → tracks that close when they open. The single most consequential
  constant. Verify by walking toward the unit and away, watching `radial_velocity_mps` change
  sign.
- **Wrong header bytes** → every frame discarded as garbage, adapter reports an empty sky while
  the unit transmits normally. Signature: `bytes_discarded` climbing with `frames_decoded` at
  zero.
- **Checksum, if the device has one.** Nothing computes one here, and whether the device appends
  a checksum — and over which bytes — is unverified. If it does, the length/footer arithmetic is
  off by the checksum width and *every* frame fails. **Most likely bring-up blocker.**
- **Wrong speed scale** → plausible targets at the wrong speed.

---

## Known gaps, deliberately left

Each of these is a decision someone should make, not an oversight.

**1. `altitude_measured` is dead in production.**
`TrackEvidence.altitude_measured` / `.vertical_rate_measured` exist so an unmeasured dimension
abstains instead of vetoing — but nothing sets them, because a `Track` does not carry which of
its sensors measured height. **The veto is still live.** A radar has no elevation channel, so it
reports the sensor's mounting height; `KinematicProfile.membership` takes a geometric mean where
any zero is decisive, so that constant silently and permanently rules out `vehicle` — which for
a ground-sited radar is among the likeliest true labels for its own clutter. Closing it needs a
track to carry altitude provenance. Noted in the adapter's implementation requirements.

**2. Radial velocity is measured but discarded.**
`IngestionPipeline._reduce` does not read `kinematics`, and the Kalman `H` matrix has the
velocity columns structurally zero. A radar's best measurement (σ ≈ 0.1 m/s against metres of
position noise) contributes nothing to the state estimate. Admitting it changes the measurement
dimension, `MEASUREMENT_DOF` in `gnn.py`, and would fail the
provenance guard at `tests/integration/test_pipeline.py:160` **by design** — that guard exists
to catch exactly this. (Note: ADR 0002 cites a standalone
`test_detection_provenance_guard.py`; no such file exists, the guard lives in
`test_pipeline.py`.) Deserves an ADR, not a patch.

**3. Risk policy is scaled for the 8 km simulated radar.**
`proximity_to_protected_area.far_m: 4000`, `loitering_near_asset.relevant_range_m: 3000`,
`low_altitude_near_asset.gate_range_m: 1500`. With a ~100 m real sensor every detection sits
inside every gate, so proximity saturates near 1.0 and the low-altitude factor always fires on
an altitude nobody measured. Rescaling would break the simulated scenarios, so it likely needs
policy to become site-scoped.

**4. Track lifecycle counts are tuned for a slow camera poll.**
`confirm_after_detections: 4`, `tentative_max_misses: 2`, `coast_seconds: 6.0`. A serial radar
at 10 Hz reaches 4 detections in 0.4 s, so clutter will confirm far more readily. Re-derive
against the radar's actual update rate.

**5. Camera detections carry a fabricated range.**
Every camera detection is pinned at `assumed_range_m: 150`. A track forms and looks confident;
its velocity is fiction — near zero when the target closes, several times inflated when it
crosses. Structural to a bearing-only sensor. The camera also **cannot** classify a target as a
UAS: `_LABEL_MAP` has no UAS entry by deliberate design (best case `OTHER`, likely `BIRD`).

**6. `get_mode` docstring is stale.**
`backend/api/system.py` still describes `real_adapters_connected` as "structurally zero" and
`simulated` as always true. Accurate before this branch, not now. Cosmetic.

---

## Camera state, if it stays in use

The original bench target (a 335 × 253 mm RC aircraft) was lost, so `min_area_px: 150` and the
sizing table in `sensor_config.yaml` are provisional — **re-derive them from whatever the new
target is** using the geometry in that comment block.

Three defects were fixed that each independently made the target undetectable, all silently:

- MOG2 `detectShadows=True` labelled any airframe paler than silhouette-black as shadow and
  deleted 100% of its pixels. Now off by default.
- The 5×5 morphological opening erased anything under 5 px in either axis (a 100×4 component
  survived with zero pixels). Now 3×3.
- `min_area_px: 3000` was sized to the noise population, which is *larger* than the signal.

Measured envelope is in `sensors/adapters/camera/README.md`. Two hard limits: a target against
ground clutter is **0/6** — sky background is required, not preferred — and a stationary target
is absorbed into the background in ~4 frames, so the camera must not be panned to follow.

---

## Conventions this codebase enforces

Worth knowing before editing, because tests fail on all of them:

- **No wall-clock calls.** All time comes from an injected `Clock` (ADR 0003). `datetime.now()`,
  `time.monotonic()` etc. fail `test_module_boundaries.py` anywhere in `backend/` or `sensors/`.
- **Hardware specifics stay in their adapter package.** `ld2451`/`uart`/`pyserial`/`baud` may
  not appear outside `sensors/adapters/radar/`; `cv2`/`opencv`/`yolo`/`ultralytics` outside
  `sensors/adapters/camera/`. Exemptions are per-term, not per-file.
- **No effector/weapons vocabulary.** The system ends at a recommendation to a human.
- **Config loaders are strict.** Unknown key = error. JSON site files permit no comments; notes
  go in the directory README.
- **Silence must never look like an empty sky (FM-010).** A fault, an unstarted adapter and a
  quiet sensor must be distinguishable. Most of the design decisions in this branch trace back
  to this one rule.

---

## Security

A full security review ran against this branch: **no findings met the reporting bar.** Two
candidates were raised and both filtered out as false positives on independent validation —
plaintext MJPEG on a LAN (operator-configured, not attacker-controlled; a hardening question),
and the `/system/mode` adapter count (the field already existed; only its fidelity changed).

Verified clean: `yaml.safe_load` throughout, the frame decoder bounded against hostile input on
every attacker-controlled field, no path traversal reachable from a CLI flag, `guard_bind`
refusals unchanged by `--live`.
